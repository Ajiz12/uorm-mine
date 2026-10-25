#!/usr/bin/env python3
"""
UORM Auto Mining Bot
====================
Backend: Supabase (PostgREST + GoTrue + RPC)

Fitur:
  1. Mining 24 jam (auto-start kalau belum jalan)
  2. Lucky box tiap 4 jam
  3. Boost mining (watch ad)
  4. Misi: daily / weekly / social / one-time (KECUALI referral)
  5. Leaderboard harian (setelah reset 24 jam)

Cara pakai:
  1. Isi .env (UORM_EMAIL, UORM_PASSWORD, UORM_TG_BOT_TOKEN, UORM_TG_CHAT_ID)
  2. python3 uorm_mine.py --login      # verifikasi kredensial + simpan token
  3. pm2 start ecosystem.config.js
"""

import os
import sys
import json
import time
import logging
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ═══════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
STATE_FILE = BASE_DIR / "uorm_state.json"
TOKENS_FILE = BASE_DIR / "uorm_tokens.json"

SUPABASE_URL = "https://tnxxckqqhwwajeljrnnl.supabase.co"
SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InRueHhja3FxaHd3YWplbGpybm5sIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODY3MzM0MDksImV4cCI6MjEwMjMwOTQwOX0.aLlLRxaltsW6Retxl4ddNKF9WkSHUJOiHVEsrcQvAJk"

WIB = timezone(timedelta(hours=7))

# Misi yang TIDAK boleh dikerjakan bot
SKIP_MISSION_KEYS = {"invite_friend"}          # referral
SKIP_MISSION_IDS = {"daily_invite", "weekly_invite"}   # referral — JANGAN sentuh

# Interval
MINING_CHECK_INTERVAL = 60          # detik — cek status mining
LUCKY_BOX_INTERVAL = 4 * 3600       # 4 jam
LEADERBOARD_INTERVAL = 24 * 3600    # 24 jam
TOKEN_REFRESH_MARGIN = 300          # refresh 5 menit sebelum expired

TG_MAX_PER_DAY = 1                  # notif Telegram per hari


# ═══════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(BASE_DIR / "uorm_bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("uorm")


def now_wib() -> datetime:
    return datetime.now(WIB)


def now_ts() -> float:
    return time.time()


# ═══════════════════════════════════════════════════════════════
# ENV
# ═══════════════════════════════════════════════════════════════

def load_env():
    """Load .env ke os.environ (tanpa dependency python-dotenv)."""
    if not ENV_FILE.exists():
        return False
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())
    return True


def cfg(key, default=None):
    return os.environ.get(key, default)


# Load .env langsung saat module di-import supaya cfg() selalu siap
load_env()


# ═══════════════════════════════════════════════════════════════
# STATE
# ═══════════════════════════════════════════════════════════════

DEFAULT_STATE = {
    "mining": {
        "active": False,
        "started_at": None,
        "last_check": 0,
        "rate": 0.0,
        "total_mined": 0.0,
    },
    "lucky_box": {
        "last_claim": 0,
        "next_claim": 0,
        "total_claims": 0,
    },
    "missions": {
        "completed": [],
        "last_sync": 0,
        "total_reward": 0.0,
    },
    "leaderboard": {
        "last_update": 0,
        "my_rank": None,
        "my_coins": None,
        "top_coins": None,
    },
    "telegram": {
        "sent_today": 0,
        "sent_date": None,
    },
    "balance": {
        "last_total": 0.0,
        "window_start": 0,
        "window_min": 0.0,
    },
}


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            # merge dengan default supaya key baru selalu ada
            merged = json.loads(json.dumps(DEFAULT_STATE))
            for k, v in data.items():
                if isinstance(v, dict) and k in merged and isinstance(merged[k], dict):
                    merged[k].update(v)
                else:
                    merged[k] = v
            return merged
        except Exception as e:
            log.warning("State rusak (%s), mulai fresh", e)
    return json.loads(json.dumps(DEFAULT_STATE))


def save_state(state: dict):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE_FILE)


# ═══════════════════════════════════════════════════════════════
# TOKENS
# ═══════════════════════════════════════════════════════════════

def load_tokens() -> dict:
    if TOKENS_FILE.exists():
        try:
            return json.loads(TOKENS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_tokens(t: dict):
    TOKENS_FILE.write_text(json.dumps(t, indent=2), encoding="utf-8")


# ═══════════════════════════════════════════════════════════════
# SUPABASE CLIENT
# ═══════════════════════════════════════════════════════════════

class SupabaseClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "apikey": SUPABASE_ANON_KEY,
            "Content-Type": "application/json",
            "User-Agent": "UORM-Android/1.0.12",
        })
        self.tokens = load_tokens()
        self._apply_auth()

    # ---------- auth header ----------
    def _apply_auth(self):
        tok = self.tokens.get("access_token")
        if tok:
            self.session.headers["Authorization"] = f"Bearer {tok}"
        else:
            self.session.headers.pop("Authorization", None)

    def token_expires_in(self) -> float:
        """Sisa detik sebelum access token expired. Negatif = sudah expired."""
        exp = self.tokens.get("expires_at")
        if not exp:
            return -1
        return exp - now_ts()

    def ensure_token(self):
        """Refresh access token kalau sudah dekat/sudah expired."""
        if self.token_expires_in() > TOKEN_REFRESH_MARGIN:
            return True

        refresh = self.tokens.get("refresh_token")
        if not refresh:
            log.warning("Tidak ada refresh token — perlu login ulang")
            return False

        try:
            r = self.session.post(
                f"{SUPABASE_URL}/auth/v1/token?grant_type=refresh_token",
                json={"refresh_token": refresh},
                timeout=30,
            )
            if r.status_code == 200:
                data = r.json()
                self._store_auth(data)
                log.info("Token di-refresh, expired dalam %ds",
                         int(self.token_expires_in()))
                return True
            log.error("Refresh gagal (%d): %s", r.status_code, r.text[:200])
        except Exception as e:
            log.error("Refresh error: %s", e)

        # refresh gagal — coba login ulang pakai kredensial
        return self.login()

    def _store_auth(self, data: dict):
        self.tokens = {
            "access_token": data.get("access_token"),
            "refresh_token": data.get("refresh_token"),
            "expires_at": now_ts() + data.get("expires_in", 3600) - 30,
            "user_id": (data.get("user") or {}).get("id"),
            "email": (data.get("user") or {}).get("email"),
        }
        save_tokens(self.tokens)
        self._apply_auth()

    def login(self) -> bool:
        """Login email + password. Token email (OTP) opsional."""
        email = cfg("UORM_EMAIL")
        password = cfg("UORM_PASSWORD")
        if not email or not password:
            log.error("UORM_EMAIL / UORM_PASSWORD belum diisi di .env")
            return False

        try:
            r = self.session.post(
                f"{SUPABASE_URL}/auth/v1/token?grant_type=password",
                json={"email": email, "password": password},
                timeout=30,
            )
            if r.status_code == 200:
                self._store_auth(r.json())
                log.info("Login berhasil: %s", email)
                return True
            log.error("Login gagal (%d): %s", r.status_code, r.text[:300])
        except Exception as e:
            log.error("Login error: %s", e)
        return False

    def verify_email_token(self, token: str) -> bool:
        """Verifikasi email pakai OTP token (grant_type=email)."""
        email = cfg("UORM_EMAIL")
        try:
            r = self.session.post(
                f"{SUPABASE_URL}/auth/v1/verify",
                json={"type": "email", "email": email, "token": token},
                timeout=30,
            )
            if r.status_code == 200:
                self._store_auth(r.json())
                log.info("Email token terverifikasi")
                return True
            log.error("Verifikasi token gagal (%d): %s", r.status_code, r.text[:200])
        except Exception as e:
            log.error("Verify error: %s", e)
        return False

    # ---------- REST ----------
    def rest_get(self, table: str, params: dict = None):
        self.ensure_token()
        try:
            r = self.session.get(
                f"{SUPABASE_URL}/rest/v1/{table}",
                params=params or {"select": "*"},
                timeout=30,
            )
            if r.status_code == 200:
                return r.json()
            log.warning("GET %s -> %d %s", table, r.status_code, r.text[:150])
        except Exception as e:
            log.error("GET %s error: %s", table, e)
        return None

    def rest_upsert(self, table: str, rows, on_conflict=None):
        self.ensure_token()
        hdrs = {"Prefer": "resolution=merge-duplicates,return=representation"}
        if on_conflict:
            hdrs["Prefer"] += f",on-conflict={on_conflict}"
        try:
            r = self.session.post(
                f"{SUPABASE_URL}/rest/v1/{table}",
                json=rows, headers=hdrs, timeout=30,
            )
            if r.status_code in (200, 201):
                return r.json()
            log.warning("UPSERT %s -> %d %s", table, r.status_code, r.text[:200])
        except Exception as e:
            log.error("UPSERT %s error: %s", table, e)
        return None

    def rest_patch(self, table: str, match: dict, data: dict):
        self.ensure_token()
        params = {k: f"eq.{v}" for k, v in match.items()}
        try:
            r = self.session.patch(
                f"{SUPABASE_URL}/rest/v1/{table}",
                params=params, json=data, timeout=30,
            )
            if r.status_code in (200, 204):
                return True
            log.warning("PATCH %s -> %d %s", table, r.status_code, r.text[:200])
        except Exception as e:
            log.error("PATCH %s error: %s", table, e)
        return False

    # ---------- RPC ----------
    def rpc(self, fn: str, payload: dict = None):
        self.ensure_token()
        try:
            r = self.session.post(
                f"{SUPABASE_URL}/rest/v1/rpc/{fn}",
                json=payload or {}, timeout=30,
            )
            if r.status_code == 200:
                return r.json()
            log.warning("RPC %s -> %d %s", fn, r.status_code, r.text[:250])
        except Exception as e:
            log.error("RPC %s error: %s", fn, e)
        return None

    def rpc_public(self, fn: str, payload: dict = None):
        """RPC yang tidak butuh auth (pakai anon key saja)."""
        try:
            r = self.session.post(
                f"{SUPABASE_URL}/rest/v1/rpc/{fn}",
                json=payload or {}, timeout=30,
            )
            if r.status_code == 200:
                return r.json()
        except Exception as e:
            log.error("RPC public %s error: %s", fn, e)
        return None


# ═══════════════════════════════════════════════════════════════
# TELEGRAM
# ═══════════════════════════════════════════════════════════════

def tg_send(text: str, state: dict = None, force: bool = False):
    """Kirim notif Telegram. Dibatasi TG_MAX_PER_DAY kecuali force=True."""
    token = cfg("UORM_TG_BOT_TOKEN")
    chat_id = cfg("UORM_TG_CHAT_ID")
    if not token or not chat_id:
        return False

    if state is not None and not force:
        today = now_wib().strftime("%Y-%m-%d")
        tg = state["telegram"]
        if tg["sent_date"] != today:
            tg["sent_date"] = today
            tg["sent_today"] = 0
        if tg["sent_today"] >= TG_MAX_PER_DAY:
            return False
        tg["sent_today"] += 1

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=20,
        )
        return r.status_code == 200
    except Exception as e:
        log.error("Telegram error: %s", e)
    return False


# ═══════════════════════════════════════════════════════════════
# AUTO-DISCOVERY — petakan schema asli setelah login
# ═══════════════════════════════════════════════════════════════

# Tabel yang bisa dibaca publik (tanpa auth) — untuk referensi kolom
PUBLIC_TABLES = ["missions", "mining_sessions", "user_missions", "profiles",
                 "room_members", "room_messages", "referrals",
                 "announcements", "app_config"]

# Tabel yang butuh auth (RLS)
AUTH_TABLES = ["rooms"]

# RPC terkonfirmasi ada di database
KNOWN_RPCS = ["open_lucky_box", "claim_daily_bonus", "get_missions",
              "get_lucky_box_status", "get_announcements", "leave_room",
              "get_leaderboard"]  # get_leaderboard publik


def discover_schema(client: SupabaseClient) -> dict:
    """
    Petakan kolom tiap tabel + simpan ke uorm_schema.json.
    Dipanggil sekali setelah login. Kalau gagal, pakai cache.
    """
    schema_file = BASE_DIR / "uorm_schema.json"
    if schema_file.exists():
        try:
            return json.loads(schema_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    schema = {"tables": {}, "rpcs": KNOWN_RPCS, "discovered_at": None}
    log.info("[discovery] mulai petakan schema...")

    for t in PUBLIC_TABLES + AUTH_TABLES:
        rows = client.rest_get(t, {"select": "*", "limit": 1})
        if rows:
            schema["tables"][t] = {
                "columns": list(rows[0].keys()),
                "sample": rows[0],
            }
            log.info("[discovery] %s: %d kolom", t, len(rows[0]))
        else:
            # tabel ada tapi kosong / butuh auth — coba HEAD untuk dapat header kolom
            try:
                r = client.session.get(
                    f"{SUPABASE_URL}/rest/v1/{t}",
                    params={"select": "*", "limit": 0},
                    timeout=15,
                )
                # PostgREST mengembalikan Content-Range: 0-0/* ; kolom ada di
                # Prefer Applied / tidak. Jadi kita coba select per-kolom kandidat.
                schema["tables"][t] = {"columns": None, "note": f"status {r.status_code}"}
                log.info("[discovery] %s: tidak bisa baca (%d)", t, r.status_code)
            except Exception as e:
                schema["tables"][t] = {"columns": None, "note": str(e)[:100]}

    schema["discovered_at"] = now_wib().isoformat()
    schema_file.write_text(json.dumps(schema, indent=2, ensure_ascii=False),
                           encoding="utf-8")
    log.info("[discovery] selesai, disimpan ke uorm_schema.json")
    return schema


def probe_columns(client: SupabaseClient, table: str, candidates) -> list:
    """
    Cari kolom yang benar-benar ada dengan select satu per satu.
    PostgREST balas 400 kalau kolom tidak ada.
    """
    ada = []
    for col in candidates:
        try:
            r = client.session.get(
                f"{SUPABASE_URL}/rest/v1/{table}",
                params={"select": col, "limit": 1},
                timeout=12,
            )
            if r.status_code == 200:
                ada.append(col)
        except Exception:
            pass
    return ada


# ═══════════════════════════════════════════════════════════════
# FITUR 1 — MINING 24 JAM
# ═══════════════════════════════════════════════════════════════

# Kandidat nama kolom — sudah diverifikasi terhadap schema asli UORM
MINING_COL_CANDIDATES = {
    "user": ["user_id", "profile_id", "uid", "owner_id"],
    "started": ["started_at", "start_time", "start", "created_at", "begin_at"],
    "ended": ["ended_at", "end_time", "end", "finished_at", "stop_at", "claimed_at"],
    "rate": ["rate_per_hour", "rate", "mining_rate", "hash_rate", "speed", "boost_rate"],
    "total": ["claimed_amount", "total_mined", "total", "mined", "amount", "reward", "coins"],
    "active": ["claimed", "is_active", "active", "status", "is_running", "running"],
}

def check_mining(client: SupabaseClient, state: dict, schema: dict = None) -> bool:
    """
    Cek status mining. Kalau tidak aktif, start.
    Return True kalau mining aktif.
    """
    m = state["mining"]
    m["last_check"] = now_ts()

    # kolom dari discovery (kalau ada)
    cols = (schema or {}).get("tables", {}).get("mining_sessions", {}).get("columns")
    c_user = _pick_col(cols, MINING_COL_CANDIDATES["user"], "user_id")
    c_start = _pick_col(cols, MINING_COL_CANDIDATES["started"], "started_at")
    c_end = _pick_col(cols, MINING_COL_CANDIDATES["ended"], "ended_at")
    c_rate = _pick_col(cols, MINING_COL_CANDIDATES["rate"], "rate")
    c_total = _pick_col(cols, MINING_COL_CANDIDATES["total"], "total_mined")

    # ambil sesi mining terakhir
    rows = client.rest_get("mining_sessions", {
        "select": "*",
        "order": f"{c_start}.desc",
        "limit": 1,
    })

    if rows:
        last = rows[0]
        m["started_at"] = last.get(c_start)
        m["session_id"] = last.get("id")
        # UORM: claimed=False -> sesi masih aktif; claimed=True -> sudah di-claim
        m["active"] = not last.get("claimed", False)
        if last.get(c_rate):
            try:
                m["rate"] = float(last[c_rate])
                m["rate_per_hour"] = float(last[c_rate])
            except (TypeError, ValueError):
                pass
        # ambil rate terbaru dari segment terakhir kalau ada
        segs = last.get("segments")
        if isinstance(segs, list) and segs:
            try:
                m["rate_per_hour"] = float(segs[-1].get("ratePerHour") or m["rate_per_hour"])
            except (TypeError, ValueError):
                pass
        if last.get(c_total):
            try:
                m["total_mined"] = float(last[c_total])
            except (TypeError, ValueError):
                pass
    else:
        m["active"] = False

    if m["active"]:
        log.info("[mining] AKTIF sejak %s rate=%s total=%s",
                 m["started_at"], m["rate"], m["total_mined"])
        return True

    # belum ada sesi aktif — pakai RPC asli: start_mining_session(p_rate_per_hour)
    log.info("[mining] tidak aktif — start via RPC start_mining_session")
    res = client.rpc("start_mining_session", {"p_rate_per_hour": m.get("rate") or 1.371825})
    if isinstance(res, dict) and (res.get("ok") or res.get("id") or res.get("session_id")):
        log.info("[mining] start BERHASIL: %s", str(res)[:250])
        m["active"] = True
        m["started_at"] = datetime.now(timezone.utc).isoformat()
        m["session_id"] = res.get("id") or res.get("session_id")
        m["rate_per_hour"] = res.get("rate_per_hour", m.get("rate") or 1.371825)
        return True
    if isinstance(res, dict) and "message" in res:
        log.info("[mining] start_mining_session -> %s", res["message"])
        # "session already active" berarti sebenarnya aktif — re-sync
        if "already active" in res["message"]:
            m["active"] = True
            return True

    log.warning("[mining] gagal start: %s — akan coba lagi nanti", str(res)[:150])
    return False


def _pick_col(known_cols, candidates, default):
    """Pilih kolom yang ada di daftar kolom terdeteksi."""
    if not known_cols:
        return default
    for c in candidates:
        if c in known_cols:
            return c
    return default


# ═══════════════════════════════════════════════════════════════
# FITUR 2 — LUCKY BOX 4 JAM
# ═══════════════════════════════════════════════════════════════

def check_lucky_box(client: SupabaseClient, state: dict):
    lb = state["lucky_box"]
    now = now_ts()

    if lb["next_claim"] and now < lb["next_claim"]:
        sisa = int(lb["next_claim"] - now)
        log.info("[luckybox] cooldown, sisa %d menit", sisa // 60)
        return

    # cek status dulu
    status = client.rpc("get_lucky_box_status")
    if status is not None:
        log.info("[luckybox] status: %s", str(status)[:200])

    res = client.rpc("open_lucky_box")
    if res is not None:
        lb["last_claim"] = now
        lb["next_claim"] = now + LUCKY_BOX_INTERVAL
        lb["total_claims"] += 1
        log.info("[luckybox] CLAIM BERHASIL: %s", str(res)[:300])
        tg_send(f"🎁 <b>UORM Lucky Box</b>\nKlaim #{lb['total_claims']} berhasil!\n<code>{str(res)[:200]}</code>", state)
    else:
        # gagal — jangan spam, coba lagi 30 menit
        lb["next_claim"] = now + 1800
        log.warning("[luckybox] claim gagal, coba lagi 30 menit")


# ═══════════════════════════════════════════════════════════════
# FITUR 3 — BOOST MINING
# ═══════════════════════════════════════════════════════════════

def boost_mining(client: SupabaseClient, state: dict, session_id: str = None,
                 multiplier: float = 1.15, duration_ms: int = 3600000) -> bool:
    """
    Boost mining rate via rewarded ad.
    RPC asli: apply_mining_boost(p_session_id uuid, p_multiplier numeric,
                                 p_duration_ms bigint, p_boosted_rate numeric)
    Catatan: ada 2 overload — WAJIB kirim 4 parameter agar PostgREST
    tidak bingung (PGRST203). p_boosted_rate = rate baru setelah boost.
    Default multiplier 1.15 = +15%, sesuai pola segment asli di server.
    """
    if not session_id:
        session_id = state.get("mining", {}).get("session_id")
    if not session_id:
        log.info("[boost] tidak ada session_id aktif")
        return False

    # cek limit boost harian dulu
    st = client.rpc("get_boost_status")
    if isinstance(st, dict):
        used, limit = st.get("used", 0), st.get("limit", 2)
        if used >= limit:
            log.info("[boost] limit harian habis (%s/%s)", used, limit)
            return False

    # rate baru = rate sekarang x multiplier
    cur_rate = state.get("mining", {}).get("rate_per_hour") or 1.371825
    boosted = round(cur_rate * multiplier, 6)

    res = client.rpc("apply_mining_boost", {
        "p_session_id": session_id,
        "p_multiplier": multiplier,
        "p_duration_ms": duration_ms,
        "p_boosted_rate": boosted,
    })
    if isinstance(res, dict) and res.get("ok"):
        segs = res.get("segments") or []
        log.info("[boost] BERHASIL — rate %s -> %s, %d segment, expires %s",
                 cur_rate, res.get("rate_per_hour"), len(segs),
                 res.get("boost_expires_at"))
        state["mining"]["rate_per_hour"] = res.get("rate_per_hour", boosted)
        state["mining"]["boost_expires_at"] = res.get("boost_expires_at")
        return True

    log.info("[boost] gagal: %s", str(res)[:200])
    return False


# ═══════════════════════════════════════════════════════════════
# FITUR 4 — MISI
# ═══════════════════════════════════════════════════════════════

def sync_missions(client: SupabaseClient, state: dict):
    """
    Ambil daftar misi + progress dari RPC get_missions.
    - Claim semua misi yang completed=True dan claimed=False
    - LEWATI referral (daily_invite, weekly_invite, dll)
    - Misi yang belum completed -> coba selesaikan otomatis kalau bisa
    """
    ms = state["missions"]

    res = client.rpc("get_missions")
    if not isinstance(res, dict) or not res.get("ok"):
        log.warning("[misi] get_missions gagal: %s", str(res)[:150])
        return

    catalog = {m["id"]: m for m in res.get("catalog", [])}
    progress = {p["mission_id"]: p for p in res.get("progress", [])}

    # ── 1. Claim semua yang sudah completed tapi belum di-claim ──
    siap_claim = []
    for mid, p in progress.items():
        if p.get("completed") and not p.get("claimed"):
            if mid in SKIP_MISSION_IDS or _is_referral(catalog.get(mid, {})):
                log.info("[misi] SKIP referral (sudah selesai): %s", mid)
                continue
            siap_claim.append(mid)

    baru = []
    for mid in siap_claim:
        if complete_mission(client, mid):
            baru.append((mid, catalog.get(mid, {}).get("reward_amount", 0)))

    # ── 2. Coba selesaikan misi otomatis ──
    for mid, m in catalog.items():
        p = progress.get(mid, {})
        if p.get("claimed") or p.get("completed"):
            continue
        if mid in SKIP_MISSION_IDS or _is_referral(m):
            continue

        mtype = m.get("type")
        # daily: buka app (sudah dilakukan oleh bot ini yang jalan 24 jam)
        if mtype == "daily" and mid == "daily_open":
            if complete_mission(client, mid):
                baru.append((mid, m.get("reward_amount", 0)))
        # mining sudah dijamin aktif oleh check_mining()
        elif mid == "daily_start":
            if state.get("mining", {}).get("active"):
                if complete_mission(client, mid):
                    baru.append((mid, m.get("reward_amount", 0)))
        # boost mining (butuh nonton iklan di app — server hanya catat)
        elif mid == "daily_boost":
            if boost_mining(client, state):
                if complete_mission(client, mid):
                    baru.append((mid, m.get("reward_amount", 0)))
        else:
            log.info("[misi] manual (butuh aksi user): %s (%s)", mid, mtype)

    if baru:
        total = sum(r for _, r in baru)
        ms["total_reward"] += total
        for mid, r in baru:
            ms["completed"].append(mid)
        log.info("[misi] %d misi selesai, total reward +%s", len(baru), total)
        tg_send(
            f"✅ <b>UORM Misi</b>\n{len(baru)} misi selesai\nReward: +{total} koin",
            state,
        )
    ms["last_sync"] = now_ts()


def _is_referral(mission: dict) -> bool:
    """Deteksi misi referral — jangan disentuh."""
    mid = (mission.get("id") or "").lower()
    key = (mission.get("requirement_key") or "").lower()
    title = (mission.get("title") or "").lower()
    desc = (mission.get("description") or "").lower()
    for blob in (mid, key, title, desc):
        if any(w in blob for w in ("invite", "referral", "refer", "undang", "ajak")):
            return True
    return False


def complete_mission(client: SupabaseClient, mission_id: str, payload: dict = None) -> bool:
    """
    Klaim reward misi.
    RPC asli: claim_mission(p_mission_id text) -> {ok, amount, mission_id, total_coins}
    """
    res = client.rpc("claim_mission", {"p_mission_id": mission_id})
    if isinstance(res, dict) and res.get("ok"):
        log.info("[misi] claim_mission %s -> +%s koin (total %s)",
                 mission_id, res.get("amount"), res.get("total_coins"))
        return True
    if isinstance(res, dict) and "message" in res:
        msg = res.get("message", "")
        if "already claimed" in msg or "not complete" in msg:
            log.info("[misi] %s -> %s", mission_id, msg)
            return True

    # fallback nama lain (untuk robustness kalau server berubah)
    for fn in ("complete_mission", "submit_mission", "verify_mission"):
        res2 = client.rpc(fn, {"p_mission_id": mission_id})
        if isinstance(res2, dict) and res2.get("ok"):
            log.info("[misi] %s via %s -> %s", mission_id, fn, str(res2)[:150])
            return True

    log.info("[misi] gagal claim %s: %s", mission_id, str(res)[:150])
    return False


# ═══════════════════════════════════════════════════════════════
# FITUR 5 — LEADERBOARD HARIAN
# ═══════════════════════════════════════════════════════════════

def update_leaderboard(client: SupabaseClient, state: dict):
    lb = state["leaderboard"]
    now = now_ts()

    if lb["last_update"] and now - lb["last_update"] < LEADERBOARD_INTERVAL:
        return

    rows = client.rpc_public("get_leaderboard", {})
    if not rows:
        log.warning("[leaderboard] gagal ambil data")
        lb["last_update"] = now
        return

    uid = client.tokens.get("user_id")
    my = next((r for r in rows if r.get("id") == uid), None)
    top = rows[0] if rows else {}

    lb["last_update"] = now
    if my:
        lb["my_rank"] = rows.index(my) + 1
        lb["my_coins"] = my.get("total_coins")
    else:
        # user tidak masuk top-N — ambil saldo dari profiles, rank = None
        prof = client.rest_get("profiles", {
            "select": "total_coins,rank_id",
            "id": f"eq.{uid}", "limit": 1,
        }) or []
        lb["my_rank"] = None
        lb["my_coins"] = prof[0].get("total_coins") if prof else None
    lb["top_coins"] = top.get("total_coins")

    log.info("[leaderboard] rank=%s koin=%s (top: %s = %s)",
             lb["my_rank"], lb["my_coins"],
             top.get("username"), lb["top_coins"])

    tg_send(
        f"🏆 <b>UORM Leaderboard</b>\n"
        f"Rank lo: {('#' + str(lb['my_rank'])) if lb['my_rank'] else 'di luar top ' + str(len(rows))}\n"
        f"Koin lo: {lb['my_coins']}\n"
        f"Teratas: {top.get('username')} ({lb['top_coins']})",
        state,
    )


# ═══════════════════════════════════════════════════════════════
# MONITOR SALDO 24 JAM
# ═══════════════════════════════════════════════════════════════

def check_balance_growth(client: SupabaseClient, state: dict):
    """Alert kalau saldo tidak nambah dalam 24 jam."""
    rows = client.rest_get("profiles", {
        "select": "total_coins",
        "id": f"eq.{client.tokens.get('user_id')}",
        "limit": 1,
    })
    if not rows:
        return

    p = rows[0]
    total = float(p.get("total_coins") or 0)

    b = state["balance"]
    now = now_ts()

    if not b["window_start"]:
        b["window_start"] = now
        b["window_min"] = total
        b["last_total"] = total
        return

    elapsed = now - b["window_start"]
    growth = total - b["window_min"]

    if elapsed >= 24 * 3600:
        if growth < 0.4:
            tg_send(
                f"⚠️ <b>UORM Saldo Macet</b>\n"
                f"24 jam terakhir cuma nambah {growth:.4f}\n"
                f"Saldo: {total}\n"
                f"Mining mungkin berhenti — cek bot.",
                state, force=True,
            )
        # reset window
        b["window_start"] = now
        b["window_min"] = total

    b["last_total"] = total


# ═══════════════════════════════════════════════════════════════
# MAIN LOOP
# ═══════════════════════════════════════════════════════════════

def main():
    load_env()

    if "--login" in sys.argv:
        c = SupabaseClient()
        if c.login():
            tok = cfg("UORM_EMAIL_TOKEN")
            if tok:
                c.verify_email_token(tok)
            print("Login OK. Token disimpan di uorm_tokens.json")
        return

    if "--discover" in sys.argv:
        c = SupabaseClient()
        if not c.ensure_token():
            print("Gagal auth — jalankan --login dulu")
            sys.exit(1)
        sc = discover_schema(c)
        print(json.dumps(sc, indent=2, ensure_ascii=False)[:3000])
        return

    if "--status" in sys.argv:
        st = load_state()
        print(json.dumps(st, indent=2, ensure_ascii=False))
        return

    log.info("════════ UORM Auto Mining Bot start ════")

    client = SupabaseClient()
    if not client.ensure_token():
        log.error("Tidak bisa auth — isi .env lalu jalankan: python3 uorm_mine.py --login")
        sys.exit(1)

    state = load_state()
    schema = discover_schema(client)

    # notif start
    tg_send(
        f"⛏️ <b>UORM Bot Start</b>\n"
        f"Waktu: {now_wib().strftime('%d %b %Y %H:%M')} WIB\n"
        f"Misi: mining 24h, lucky box 4h, boost, misi, leaderboard",
        state, force=True,
    )

    last_mission_sync = 0
    last_balance_check = 0

    while True:
        try:
            # token refresh tiap iterasi
            client.ensure_token()

            # 1. mining
            mining_ok = check_mining(client, state, schema)

            # 2. lucky box (tiap 4 jam)
            check_lucky_box(client, state)

            # 3. misi (tiap 30 menit)
            if now_ts() - last_mission_sync > 1800:
                sync_missions(client, state)
                last_mission_sync = now_ts()

            # 4. leaderboard (tiap 24 jam)
            update_leaderboard(client, state)

            # 5. monitor saldo (tiap 10 menit)
            if now_ts() - last_balance_check > 600:
                check_balance_growth(client, state)
                last_balance_check = now_ts()

            save_state(state)

            if mining_ok:
                log.info("[status] MINING rate=%s total=%s",
                         state["mining"]["rate"], state["mining"]["total_mined"])

        except KeyboardInterrupt:
            log.info("Stop oleh user")
            break
        except Exception as e:
            log.exception("Loop error: %s", e)
            time.sleep(60)

        time.sleep(MINING_CHECK_INTERVAL)


if __name__ == "__main__":
    main()
