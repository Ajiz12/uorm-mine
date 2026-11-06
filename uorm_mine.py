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
PID_FILE = BASE_DIR / ".bot.pid"

SUPABASE_URL = "https://tnxxckqqhwwajeljrnnl.supabase.co"

WIB = timezone(timedelta(hours=7))

# Misi yang TIDAK boleh dikerjakan bot
SKIP_MISSION_KEYS = {"invite_friend"}          # referral
SKIP_MISSION_IDS = {"daily_invite", "weekly_invite"}   # referral — JANGAN sentuh

# Interval
MINING_CHECK_INTERVAL = 10 * 60       # 10 menit — claim tepat saat 24 jam habis
LOOP_INTERVAL = 300                    # 5 menit — tick loop internal
LUCKY_BOX_INTERVAL = 4 * 3600          # 4 jam
DAILY_BONUS_INTERVAL = 24 * 3600       # 24 jam — daily login bonus
LEADERBOARD_INTERVAL = 2 * 3600        # 2 jam — posisi cepat berubah
REPORT_INTERVAL = 4 * 3600             # 4 jam — notif status berkala
TOKEN_REFRESH_MARGIN = 300             # refresh 5 menit sebelum expired

# Boost mining — jadwal WIB (2 slot/hari, TIDAK di luar jam ini)
# 1 boost per slot: pagi 08:00-10:59, siang 12:00-15:59.
# Slot sengaja dilebar biar boost #2 nggak kepicu di slot pagi (numpuk),
# tapi kepakai di periode berbeda dalam sehari.
BOOST_SLOTS_WIB = [(8, 11), (12, 16)]  # pagi & siang WIB
BOOST_MAX_PER_DAY = 2                  # server limit 2/hari — slot di atas = 2

TG_MAX_PER_DAY = 1                  # notif Telegram per hari (notif biasa)
                                     # laporan status & boost selalu force=True

# Kuota pesan qualifying chat per hari (server: +0.5% rate/level, cap 30 pesan)
DAILY_QUALIFYING_CAP = 30


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

SUPABASE_ANON_KEY = cfg("SUPABASE_ANON_KEY")


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
        "session_id": None,
        "rate_per_hour": 0.0,
        "boost_expires_at": None,
        "boost_multiplier": 1,
        "boost_last_date": None,
        "boost_claimed_today": 0,
    },
    "lucky_box": {
        "last_claim": 0,
        "next_claim": 0,
        "total_claims": 0,
        "last_amount": 0.0,
    },
    "daily_bonus": {
        "last_check": 0,
        "last_claim_date": None,
        "streak_day": 0,
        "last_amount": 0.0,
        "total_claimed": 0.0,
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
        "rank_id": None,
    },
    "telegram": {
        "sent_today": 0,
        "sent_date": None,
        "last_report": 0,
    },
    "balance": {
        "last_total": 0.0,
        "window_start": 0,
        "window_min": 0.0,
    },
    "report": {
        "last_sent": 0,
        "last_rank_id": None,
    },
}


def load_chat_state() -> dict:
    """Baca state bot chat (uorm_chat_state.json) buat laporan. Read-only."""
    path = BASE_DIR / "uorm_chat_state.json"
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("[laporan] baca chat state gagal: %s", e)
    return {}


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

    def rpc_full(self, fn: str, payload: dict = None):
        """
        RPC yang mengembalikan (status_code, body) apa adanya.
        Dibutuhkan untuk membedakan error server: PostgREST balas 400 dengan
        pesan ("already claimed", "session already active") — kalau dibuang,
        penanganan kondisinya jadi mustahil.
        """
        self.ensure_token()
        try:
            r = self.session.post(
                f"{SUPABASE_URL}/rest/v1/rpc/{fn}",
                json=payload or {}, timeout=30,
            )
            try:
                body = r.json()
            except ValueError:
                body = r.text[:200]
            return r.status_code, body
        except Exception as e:
            log.error("RPC %s error: %s", fn, e)
            return None, None


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
            json={"chat_id": chat_id, "text": text},
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

def claim_mining(client: SupabaseClient, state: dict, session_id: str = None) -> float:
    """
    Claim sesi mining yang sudah selesai (24 jam).
    RPC asli: claim_mining_session(p_session_id uuid)
    Return jumlah koin yang di-claim (0 kalau gagal / sudah di-claim).

    Penting: error server (HTTP 400) TIDAK dibuang. PostgREST balas
    {"code":"P0001","message":"already claimed"} untuk sesi yang sudah
    diklaim — itu kondisi normal (misal klaim dari HP), bukan kegagalan.
    """
    if not session_id:
        session_id = state.get("mining", {}).get("session_id")
    if not session_id:
        return 0.0

    status, body = client.rpc_full("claim_mining_session", {"p_session_id": session_id})

    if status == 200 and isinstance(body, dict):
        if body.get("ok") or body.get("amount") is not None:
            amt = float(body.get("amount") or body.get("coins") or body.get("reward") or 0.0)
            log.info("[mining] CLAIM BERHASIL: %s UORM", amt)
            return amt

    if isinstance(body, dict):
        msg = (body.get("message") or "").lower()
        if "already claimed" in msg:
            log.info("[mining] sesi sudah di-claim sebelumnya (normal)")
            return 0.0
        if "not finished" in msg or "belum" in msg or "active" in msg:
            log.info("[mining] sesi belum selesai — %s", body.get("message"))
            return 0.0

    log.info("[mining] claim -> %s %s", status, str(body)[:200])
    return 0.0


def _session_expired(row: dict, now: float = None) -> bool:
    """
    True kalau sesi sudah lewat durasinya (default 24 jam).

    Server UORM TIDAK menandai sesi expired — kolom `claimed` tetap false
    sampai user klaim manual. Satu-satunya cara tahu siklus 24 jam selesai
    adalah hitung started_at + session_duration_ms.
    """
    if not row:
        return False
    now = now or now_ts()
    started = row.get("started_at")
    if not started:
        return False
    try:
        st = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    dur = row.get("session_duration_ms") or 86400000
    # now_ts() detik, st.timestamp() detik — konversi ke ms DULU, jangan
    # (detik - detik*1000): itu bikin angka negatif besar, expired selalu False
    return ((now - st.timestamp()) * 1000.0) >= float(dur)


def check_mining(client: SupabaseClient, state: dict, schema: dict = None,
                 force: bool = False) -> bool:
    """
    Cek status mining. Interval 4 jam (bareng lucky box & boost).
    - Sesi expired (24 jam lewat) -> claim, lalu start sesi baru
    - Tidak ada sesi aktif -> start sesi baru
    Return True kalau mining aktif.
    """
    m = state["mining"]
    now = now_ts()

    # throttle 4 jam — kecuali force (dipanggil saat bot baru start)
    if not force and m.get("last_check") and now - m["last_check"] < MINING_CHECK_INTERVAL:
        return bool(m.get("active"))

    m["last_check"] = now

    # kolom dari discovery (kalau ada)
    cols = (schema or {}).get("tables", {}).get("mining_sessions", {}).get("columns")
    c_start = _pick_col(cols, MINING_COL_CANDIDATES["started"], "started_at")
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
        # Sesi dianggap selesai kalau claimed ATAU sudah lewat 24 jam.
        # Server tidak pernah menandai expired — kalau cuma lihat `claimed`,
        # bot akan menganggap sesi aktif selamanya dan tidak pernah claim.
        expired = _session_expired(last, now)
        m["active"] = (not last.get("claimed", False)) and not expired
        if last.get(c_rate):
            try:
                m["rate"] = float(last[c_rate])
                m["rate_per_hour"] = float(last[c_rate])
            except (TypeError, ValueError):
                pass
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
        m["boost_expires_at"] = last.get("boost_expires_at")
        m["boost_multiplier"] = last.get("boost_multiplier") or 1
    else:
        m["active"] = False

    if m["active"]:
        log.info("[mining] AKTIF sejak %s rate=%s total=%s",
                 m["started_at"], m["rate"], m["total_mined"])
        return True

    # ── sesi tidak aktif ──
    # Kalau ada sesi lama yang belum di-claim, claim dulu
    if rows and not rows[0].get("claimed") and _session_expired(rows[0], now):
        amt = claim_mining(client, state, rows[0].get("id"))
        if amt > 0:
            log.info("[mining] sesi 24 jam selesai — klaim %s UORM", _fmt_coins(amt))
            # notif terpisah, dikirim SESUDAH aksi berhasil
            tg_send(
                f"🚨 Mining Claim: {_fmt_coins(amt)} UORM\n"
                f"Sesi 24 jam selesai.",
                state, force=True,
            )

    # start sesi baru
    log.info("[mining] tidak aktif — start via RPC start_mining_session")
    status, res = client.rpc_full("start_mining_session", {"p_rate_per_hour": m.get("rate") or 1.371825})
    if status == 200 and isinstance(res, dict) and (res.get("ok") or res.get("id") or res.get("session_id")):
        log.info("[mining] start BERHASIL: %s", str(res)[:250])
        m["active"] = True
        m["started_at"] = datetime.now(timezone.utc).isoformat()
        m["session_id"] = res.get("id") or res.get("session_id")
        m["rate_per_hour"] = res.get("rate_per_hour", m.get("rate") or 1.371825)
        log.info("[mining] mulai: rate %s UORM/jam", _fmt_coins(m["rate_per_hour"]))
        # notif terpisah, dikirim SESUDAH sesi beneran jalan
        tg_send(
            f"💎 Mining Start: Aktif\n"
            f"Rate: {_fmt_coins(m['rate_per_hour'])} UORM/jam\n"
            f"Siklus 24 jam.",
            state, force=True,
        )
        return True
    if isinstance(res, dict) and "message" in res:
        msg = res["message"].lower()
        # server sudah punya sesi jalan (misal klaim dari HP) — anggap aktif
        if "already active" in msg or "already running" in msg or "session already" in msg:
            log.info("[mining] server bilang sesi sudah jalan — sinkron ulang")
            m["active"] = True
            return True
        log.info("[mining] start_mining_session -> %s", res["message"])

    log.warning("[mining] gagal start: %s %s — akan coba lagi nanti",
                status, str(res)[:150])
    # notif kritis — mining nggak jalan = saldo nggak nambah. Sekali per 6 jam.
    st = state["mining"]
    now = now_ts()
    if now - st.get("start_fail_alerted", 0) > 6 * 3600:
        tg_send(
            "❌ UORM Mining Gagal Start\n"
            f"Error: {status} {str(res)[:120]}\n"
            "Sesi baru tidak bisa dimulai — cek pm2 logs uorm-mine.",
            state, force=True,
        )
        st["start_fail_alerted"] = now
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
# FITUR 1.5 — DAILY LOGIN BONUS 24 JAM
# ═══════════════════════════════════════════════════════════════

def check_daily_bonus(client: SupabaseClient, state: dict):
    """
    Klaim daily login bonus. Cek 1x per 24 jam.
    Status asli ada di tabel daily_bonus_state (last_claim_date).
    RPC: claim_daily_bonus() — tanpa param, pakai JWT.
    """
    db = state["daily_bonus"]
    now = now_ts()

    if db["last_check"] and now - db["last_check"] < DAILY_BONUS_INTERVAL:
        return
    db["last_check"] = now

    uid = client.tokens.get("user_id")

    # cek status dari tabel asli — lebih akurat daripada state lokal
    rows = client.rest_get("daily_bonus_state", {
        "select": "last_claim_date,last_claimed_day",
        "user_id": f"eq.{uid}", "limit": 1,
    }) or []
    today = now_wib().strftime("%Y-%m-%d")
    if rows and rows[0].get("last_claim_date") == today:
        db["last_claim_date"] = today
        db["streak_day"] = rows[0].get("last_claimed_day", db["streak_day"])
        log.info("[daily bonus] sudah diklaim hari ini (streak day %s)",
                 db["streak_day"])
        return

    r = client.rpc("claim_daily_bonus")
    if r is None:
        return

    amount = 0.0
    if isinstance(r, dict):
        amount = float(r.get("amount") or r.get("coins")
                       or r.get("reward") or r.get("total_coins") or 0)
        db["streak_day"] = r.get("day") or r.get("streak_day") or r.get("day_number")

    db["last_claim_date"] = today
    db["last_amount"] = amount
    db["total_claimed"] += amount
    save_state(state)

    log.info("[daily bonus] BERHASIL klaim %s UORM (streak hari ke-%s)",
             _fmt_coins(amount), db["streak_day"])
    # notif terpisah — dikirim SESUDAH klaim beneran masuk
    tg_send(
        f"📅 Daily Bonus: +{_fmt_coins(amount)} UORM\n"
        f"Streak hari ke-{db['streak_day']}",
        state, force=True,
    )


# FITUR 2 — LUCKY BOX 4 JAM
# ═══════════════════════════════════════════════════════════════

def check_lucky_box(client: SupabaseClient, state: dict):
    lb = state["lucky_box"]
    now = now_ts()

    # ── SELALU cek server dulu, jangan percaya timer lokal ──
    # Timer lokal bisa jadi tidak sinkron (bot restart, jam VPS geser,
    # claim dari HP). Server adalah sumber kebenaran: kalau can_open
    # true, klaim — jangan sampai ke-skip karena state lokal.
    status = client.rpc("get_lucky_box_status")
    if isinstance(status, dict):
        can_open = status.get("can_open")
        remaining_ms = status.get("remaining_ms")
        if can_open is False:
            # sinkronkan cooldown dari server
            if isinstance(remaining_ms, (int, float)) and remaining_ms > 0:
                lb["next_claim"] = now + remaining_ms / 1000
            sisa = int(lb["next_claim"] - now) if lb["next_claim"] > now else 0
            log.info("[luckybox] cooldown, sisa %d menit", sisa // 60)
            return
        # can_open True (atau field tidak ada) — lanjut klaim
    else:
        # status gagal — fallback ke timer lokal
        if lb["next_claim"] and now < lb["next_claim"]:
            sisa = int(lb["next_claim"] - now)
            log.info("[luckybox] status gagal, pakai timer lokal, sisa %d menit",
                     sisa // 60)
            return

    res = client.rpc("open_lucky_box")
    if res is not None:
        lb["last_claim"] = now
        # cooldown dari response server (paling akurat)
        rem = res.get("remaining_ms") if isinstance(res, dict) else None
        lb["next_claim"] = now + (rem / 1000 if isinstance(rem, (int, float)) and rem > 0
                                  else LUCKY_BOX_INTERVAL)
        lb["total_claims"] += 1
        # nominal hadiah — simpan untuk notif
        if isinstance(res, dict):
            lb["last_amount"] = float(res.get("amount") or res.get("coins")
                                      or res.get("reward") or 0.0)
        save_state(state)
        log.info("[luckybox] CLAIM BERHASIL: %s UORM (next %s)",
                 _fmt_coins(lb["last_amount"]),
                 datetime.fromtimestamp(lb["next_claim"], tz=WIB).strftime("%H:%M WIB"))
        # notif terpisah — dikirim SESUDAH klaim beneran masuk
        # Total saldo = saldo SEBELUM klaim + hasil lucky box (akumulasi klaim ini)
        saldo_live = _get_total_coins(client)
        if saldo_live is not None:
            total_saldo = saldo_live + lb["last_amount"]
            _fmt_coins_cache = _fmt_coins(total_saldo)
        else:
            _fmt_coins_cache = "-"
        tg_send(
            f"🎁 Lucky Box: {_fmt_coins(lb['last_amount'])} UORM\n"
            f"📈 Total Saldo: {_fmt_coins_cache} UORM\n"
            f"🕙 Next Claim: "
            f"{datetime.fromtimestamp(lb['next_claim'], tz=WIB).strftime('%H:%M WIB')}",
            state, force=True,
        )
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
        # simpan sebagai SET hari ini — append-only bikin state membengkak
        # (39 entri duplikat dalam 2 hari) dan nggak dipakai buat apa pun.
        claimed_today = set(ms.get("claimed_today") or [])
        claimed_today.update(mid for mid, _ in baru)
        ms["claimed_today"] = sorted(claimed_today)
        ms["completed"] = sorted(claimed_today)
        log.info("[misi] %d misi selesai, total reward +%s", len(baru), total)
        # notif terpisah per misi — dikirim SESUDAH klaim beneran masuk
        for mid, reward in baru:
            nama = catalog.get(mid, {}).get("title") or mid
            tg_send(
                f"✅ Misi Selesai: {nama}\n"
                f"Reward: +{_fmt_coins(reward)} UORM",
                state, force=True,
            )
    ms["last_sync"] = now_ts()


def claim_bundles(client: SupabaseClient, state: dict):
    """
    Klaim bonus bundle harian & mingguan (kalau syaratnya sudah penuh).
    RPC asli: claim_mission_bundle(p_type) — p_type = "daily" | "weekly".

    Bonus bundle cuma bisa diklaim kalau SEMUA misi di kategori itu sudah
    completed — termasuk misi referral. Jadi selama user skip referral,
    server balas "not complete" dan bonus tetap terkunci (bukan bug bot).
    Fungsi ini aman dipanggil berulang: 400 = belum waktunya, abaikan.
    """
    bn = state.setdefault("bundles", {})
    res = client.rpc("get_missions")
    if not isinstance(res, dict):
        return
    amounts = {"daily": res.get("daily_bundle_amount"),
               "weekly": res.get("weekly_bundle_amount")}
    flags = {"daily": res.get("daily_bonus_claimed"),
             "weekly": res.get("weekly_bonus_claimed")}

    for period in ("daily", "weekly"):
        if flags.get(period):
            continue
        status, body = client.rpc_full("claim_mission_bundle", {"p_type": period})
        if status == 200 and isinstance(body, dict):
            amt = float(body.get("amount") or amounts.get(period) or 0)
            bn[period] = {"last_claim": now_ts(), "last_amount": amt}
            save_state(state)
            log.info("[bundle] CLAIM %s BERHASIL: %s UORM", period, amt)
            # notif terpisah — dikirim SESUDAH klaim beneran masuk
            tg_send(
                f"🏆 Bundle {period.title()} Claim: +{_fmt_coins(amt)} UORM",
                state, force=True,
            )
        elif isinstance(body, dict) and "not complete" in (body.get("message") or "").lower():
            log.info("[bundle] %s belum lengkap (%s UORM menunggu)",
                     period, amounts.get(period))
        else:
            log.info("[bundle] %s -> %s %s", period, status, str(body)[:120])


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

def count_profiles_above(client: SupabaseClient, coins: float):
    """
    Hitung jumlah user dengan koin > coins, pakai header
    Prefer: count=exact. get_leaderboard cuma return top 100,
    jadi posisi asli harus dihitung manual.
    """
    try:
        r = client.session.get(
            f"{SUPABASE_URL}/rest/v1/profiles",
            params={"select": "total_coins", "total_coins": f"gt.{coins}"},
            headers={"Prefer": "count=exact", "Range": "0-0"},
            timeout=30,
        )
        if r.status_code in (200, 206):
            cr = r.headers.get("Content-Range", "")
            total = cr.rsplit("/", 1)[-1]
            if total.isdigit():
                return int(total)
    except Exception as e:
        log.warning("[leaderboard] count gagal: %s", e)
    return None


def _count_profiles_below(client: SupabaseClient, coins: float):
    """
    Hitung jumlah user dengan koin < coins (mirror count_profiles_above).
    Dipakai untuk menghitung total peserta leaderboard.
    """
    try:
        r = client.session.get(
            f"{SUPABASE_URL}/rest/v1/profiles",
            params={"select": "total_coins", "total_coins": f"lt.{coins}"},
            headers={"Prefer": "count=exact", "Range": "0-0"},
            timeout=30,
        )
        if r.status_code in (200, 206):
            total = r.headers.get("Content-Range", "").rsplit("/", 1)[-1]
            if total.isdigit():
                return int(total)
    except Exception as e:
        log.warning("[leaderboard] count-belum gagal: %s", e)
    return 0


def update_leaderboard(client: SupabaseClient, state: dict, force: bool = False):
    lb = state["leaderboard"]
    now = now_ts()

    # Refresh kalau interval lewat ATAU saldo naik signifikan (naik peringkat
    # langsung kelihatan). Tanpa ini, laporan bisa menyebut posisi basi
    # sampai 2 jam padahal user baru saja klaim.
    saldo = float((state.get("balance") or {}).get("last_total") or 0)
    saldo_naik = saldo and abs(saldo - float(lb.get("my_coins") or 0)) > 1
    if not force and lb["last_update"]:
        if now - lb["last_update"] < LEADERBOARD_INTERVAL and not saldo_naik:
            return

    uid = client.tokens.get("user_id")
    prof = client.rest_get("profiles", {
        "select": "total_coins,rank_id",
        "id": f"eq.{uid}", "limit": 1,
    }) or []
    if not prof:
        log.warning("[leaderboard] profil tidak ditemukan")
        lb["last_update"] = now
        return

    my_coins = float(prof[0].get("total_coins") or 0)
    lb["my_coins"] = my_coins
    lb["rank_id"] = prof[0].get("rank_id")
    lb["last_update"] = now

    rows = client.rpc_public("get_leaderboard", {}) or []
    my = next((r for r in rows if r.get("id") == uid), None)

    if my:
        lb["my_rank"] = rows.index(my) + 1
    else:
        # tidak masuk top-N — hitung posisi asli via count eksplisit
        above = count_profiles_above(client, my_coins)
        lb["my_rank"] = (above + 1) if above is not None else None

    top = rows[0] if rows else {}
    lb["top_coins"] = top.get("total_coins")
    # total user asli = posisi bos + jumlah user di bawah bos.
    # Dulu salah: lb["my_rank"] dipakai sebagai total, jadi angkanya
    # selalu sama dengan peringkat sendiri.
    if above is not None:
        lb["total_users"] = above + 1 + _count_profiles_below(client, my_coins)

    log.info("[leaderboard] rank=%s koin=%s (top: %s = %s)",
             lb["my_rank"], lb["my_coins"],
             top.get("username"), lb["top_coins"])


# ═══════════════════════════════════════════════════════════════
# MONITOR SALDO 24 JAM
# ═══════════════════════════════════════════════════════════════

def _get_total_coins(client: SupabaseClient):
    """Saldo total terkini dari server (None kalau gagal)."""
    try:
        rows = client.rest_get("profiles", {
            "select": "total_coins",
            "id": f"eq.{client.tokens.get('user_id')}",
            "limit": 1,
        })
        if rows:
            return float(rows[0].get("total_coins") or 0)
    except Exception as e:
        log.warning("[saldo] gagal ambil total_coins: %s", e)
    return None


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
            log.warning("[saldo] MACET: 24 jam terakhir cuma nambah %.4f (saldo %s)",
                        growth, total)
            # notif kritis — saldo macet = mining nggak jalan, kirim sekali per window
            if not b.get("macet_alerted"):
                tg_send(
                    "⚠️ UORM Saldo Macet\n"
                    f"24 jam terakhir cuma nambah {_fmt_coins(growth)} UORM.\n"
                    "Mining kemungkinan nggak jalan — cek pm2 logs uorm-mine.",
                    state, force=True,
                )
                b["macet_alerted"] = True
        else:
            b["macet_alerted"] = False
        # reset window
        b["window_start"] = now
        b["window_min"] = total

    b["last_total"] = total


# ═══════════════════════════════════════════════════════════════
# NOTIFIKASI STATUS 4 JAMAN
# ═══════════════════════════════════════════════════════════════

def _fmt_coins(v) -> str:
    """Format angka koin — buang nol desimal yang tidak perlu."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "0"
    if f == int(f):
        return f"{int(f):,}".replace(",", ".")
    return f"{f:,.3f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _boost_aktif(state: dict) -> bool:
    """True kalau boost masih aktif (boost_expires_at belum lewat)."""
    exp = state.get("mining", {}).get("boost_expires_at")
    if not exp:
        return False
    try:
        exp_dt = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
        return exp_dt > datetime.now(timezone.utc)
    except (ValueError, TypeError):
        return False


def _reset_boost_harian(state: dict):
    """Reset counter boost lokal tiap ganti hari WIB (server reset kuota juga)."""
    hari_ini = now_wib().strftime("%Y-%m-%d")
    m = state["mining"]
    if m.get("boost_last_date") != hari_ini:
        m["boost_last_date"] = hari_ini
        m["boost_claimed_today"] = 0
        m["boost_slot_used"] = {}   # slot -> sudah dipakai hari ini (1 boost/slot)
        # boost_expires_at basi dari hari sebelumnya → anggap tidak aktif
        if not _boost_aktif(state):
            m["boost_expires_at"] = None


def _boost_slot_index() -> int:
    """Index slot boost sekarang (0 = pagi, 1 = siang). -1 kalau di luar slot."""
    h = now_wib().hour
    for i, (start, end) in enumerate(BOOST_SLOTS_WIB):
        if start <= h < end:
            return i
    return -1


def _boost_slot_sekarang() -> bool:
    """True kalau sekarang jatuh di slot boost (pagi atau siang WIB)."""
    return _boost_slot_index() >= 0


def _boost_slot_berikutnya() -> str:
    """Label slot boost berikutnya, buat log/notif."""
    h = now_wib().hour
    for start, end in BOOST_SLOTS_WIB:
        if h < start:
            return f"{start:02d}:00-{end:02d}:00 WIB"
    # sudah lewat semua slot hari ini → slot pertama besok
    return f"{BOOST_SLOTS_WIB[0][0]:02d}:00-{BOOST_SLOTS_WIB[0][1]:02d}:00 WIB (besok)"


def send_status_report(client: SupabaseClient, state: dict, force: bool = False):
    """
    Kirim laporan grinding UORM ke Telegram — SATU-SATUNYA notif rutin.
    Interval: sekali sehari (setelah grinding selesai), atau force=True.
    """
    now = now_ts()
    rp = state["report"]

    # sekali sehari: cegah dobel kirim di hari yang sama (WIB)
    if not force and rp["last_sent"]:
        last_day = datetime.fromtimestamp(rp["last_sent"], tz=WIB).strftime("%Y-%m-%d")
        if last_day == now_wib().strftime("%Y-%m-%d"):
            return False

    uid = client.tokens.get("user_id")

    # ── profil: level, xp, streak, saldo, rank ──
    prof = client.rest_get("profiles", {
        "select": "total_coins,rank_id,level,xp,chat_streak_days,streak_count",
        "id": f"eq.{uid}", "limit": 1,
    }) or []
    if prof:
        p = prof[0]
        total_coins = p.get("total_coins", 0) or 0
        rank_id = p.get("rank_id", "-") or "-"
        level = p.get("level", 0) or 0
        xp = p.get("xp", 0) or 0
        streak = p.get("chat_streak_days", 0) or 0
    else:
        total_coins, rank_id, level, xp, streak = 0, "-", 0, 0, 0

    # ── chat hari ini (dari state bot chat) ──
    chat = load_chat_state()
    chat_global = chat.get("daily_global", 0)
    chat_room = chat.get("daily_room", 0)
    chat_qualifying = chat.get("daily_qualifying", 0)
    chat_xp = chat.get("daily_xp", 0)
    chat_total = chat.get("total_chats", 0)
    # sisa kuota jangan negatif — kalau server salah hitung, tampilkan 0
    sisa_kuota = max(0, DAILY_QUALIFYING_CAP - chat_qualifying)

    # ── lucky box: nominal terakhir + jam claim berikutnya (WIB) ──
    lb = state["lucky_box"]
    lb_amount = lb.get("last_amount", 0.0)
    if lb["next_claim"] and now < lb["next_claim"]:
        jam_next = datetime.fromtimestamp(lb["next_claim"], tz=WIB).strftime("%H:%M")
    else:
        jam_next = "sekarang"

    # ── leaderboard posisi (hitung LIVE saat laporan dikirim) ──
    ldb = state["leaderboard"]
    lb_pos = "-"
    try:
        my_coins_live = float(total_coins or 0)
        if my_coins_live > 0:
            above_live = count_profiles_above(client, my_coins_live)
            if above_live is not None:
                lb_pos = f"#{above_live + 1}"
                ldb["my_coins"] = my_coins_live
                ldb["my_rank"] = above_live + 1
                ldb["last_update"] = now
    except Exception as e:
        log.warning("[laporan] hitung peringkat gagal: %s", e)
    if lb_pos == "-" and ldb.get("my_rank"):
        lb_pos = f"#{ldb['my_rank']}"

    # ── badge: jumlah yang sudah dimiliki (revealed) ──
    badge_revealed, badge_line = 0, None
    bg = client.rpc("get_my_badges")
    if isinstance(bg, dict):
        badges = bg.get("badges", [])
        badge_revealed = sum(1 for b in badges if b.get("status") == "revealed")
    last_badge = state.get("badges", {}).get("last_revealed")
    if last_badge and last_badge.get("date") == now_wib().strftime("%Y-%m-%d"):
        badge_line = f"🏅 Hari ini: {last_badge.get('title')} ({last_badge.get('serial')})"

    # ── daily bonus: nominal klaim terakhir (hari ini kalau ada) ──
    db = state["daily_bonus"]
    bonus_amount = db.get("last_amount", 0.0)
    if db.get("last_claim_date") != now_wib().strftime("%Y-%m-%d"):
        bonus_amount = 0.0

    rp["last_rank_id"] = rank_id
    rp["last_sent"] = now

    text = (
        "📊 UORM Daily Report\n"
        "\n"
        f"💵 Saldo: {_fmt_coins(total_coins)} UORM\n"
        f"🚀 Peringkat: {lb_pos.lstrip('#')}\n"
        f"🎖️ Total Badge: {badge_revealed}\n"
        f"🗣️ Level Chat: {level}\n"
        f"📇 Jabatan: {rank_id.title()}"
    )

    return tg_send(text, state, force=True)


# ═══════════════════════════════════════════════════════════════
# AUTO-BOOST — aktifkan begitu slot harian tersedia
# ═══════════════════════════════════════════════════════════════

def auto_boost(client: SupabaseClient, state: dict):
    """
    Cek tiap loop: boost hanya diaktifkan kalau
      1. mining aktif,
      2. sekarang jatuh di slot WIB (08-09 / 13-14),
      3. boost belum aktif,
      4. slot harian server masih ada (max 2/hari).
    Di luar slot → tidak pernah klaim, tunggu jam berikutnya.
    """
    if not state.get("mining", {}).get("active"):
        return

    # ganti hari WIB → reset counter lokal
    _reset_boost_harian(state)

    # sudah aktif — jangan boros slot
    if _boost_aktif(state):
        return

    # di luar jam boost → jangan klaim
    slot_idx = _boost_slot_index()
    if slot_idx < 0:
        return

    # guard lokal: kalau state bilang sudah 2x hari ini, jangan panggil RPC
    if state["mining"].get("boost_claimed_today", 0) >= BOOST_MAX_PER_DAY:
        return

    # guard per-slot: 1 slot = 1 boost, biar nggak numpuk di slot pagi
    slot_used = state["mining"].setdefault("boost_slot_used", {})
    if slot_used.get(str(slot_idx)):
        log.info("[boost] slot %s sudah dipakai hari ini — tunggu slot berikutnya (%s)",
                 f"{now_wib().strftime('%H:%M')} WIB", _boost_slot_berikutnya())
        return

    st = client.rpc("get_boost_status")
    if not isinstance(st, dict):
        return
    used, limit = st.get("used", 0), st.get("limit", BOOST_MAX_PER_DAY)
    if used >= limit:
        log.info("[boost] limit harian habis (%s/%s) — slot berikutnya %s",
                 used, limit, _boost_slot_berikutnya())
        return

    log.info("[boost] slot %s aktif, slot harian %d/%d — aktifkan",
             f"{now_wib().strftime('%H:%M')} WIB", used, limit)
    if boost_mining(client, state):
        state["mining"]["boost_claimed_today"] = used + 1
        state["mining"].setdefault("boost_slot_used", {})[str(slot_idx)] = True
        log.info("[boost] AKTIF %s WIB — rate %s UORM/jam (slot %d/%d)",
                 now_wib().strftime("%H:%M"),
                 state["mining"].get("rate_per_hour"), used + 1, limit)
        # notif terpisah, dikirim SESUDAH boost beneran aktif
        tg_send(
            f"📈 Boost Sesi {used + 1} Aktif\n"
            f"Rate: {_fmt_coins(state['mining'].get('rate_per_hour'))} UORM/jam",
            state, force=True,
        )


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

    # tulis PID file untuk watchdog
    try:
        PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass

    client = SupabaseClient()
    if not client.ensure_token():
        log.error("Tidak bisa auth — isi .env lalu jalankan: python3 uorm_mine.py --login")
        sys.exit(1)

    state = load_state()
    schema = discover_schema(client)

    # laporan awal — format sama dengan laporan berkala
    send_status_report(client, state, force=True)

    last_mission_sync = 0
    last_balance_check = 0

    while True:
        try:
            # token refresh tiap iterasi
            if not client.ensure_token():
                # auth gagal — mining & semua RPC mati. Notif sekali per kejadian.
                log.error("[auth] token refresh gagal — loop tidak bisa lanjut")
                tg_send(
                    "❌ UORM Auth Gagal\n"
                    "Token tidak bisa di-refresh. Mining berhenti.\n"
                    "Perlu login ulang: python3 uorm_mine.py --login",
                    state, force=True,
                )
                time.sleep(300)
                continue

            # 1. mining (interval 4 jam — bareng lucky box & boost)
            mining_ok = check_mining(client, state, schema, force=True)

            # 2. daily login bonus (tiap 24 jam)
            check_daily_bonus(client, state)

            # 3. lucky box (tiap 4 jam)
            check_lucky_box(client, state)

            # 4. misi (tiap 30 menit)
            if now_ts() - last_mission_sync > 1800:
                sync_missions(client, state)
                claim_bundles(client, state)
                last_mission_sync = now_ts()

            # 5. leaderboard (tiap 24 jam)
            update_leaderboard(client, state)

            # 6. auto-boost — aktifkan kalau slot harian tersedia
            auto_boost(client, state)

            # 7. laporan status (tiap 4 jam)
            send_status_report(client, state)

            # 7. monitor saldo (tiap 10 menit)
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

        time.sleep(LOOP_INTERVAL)

    # bersihkan PID file saat keluar
    try:
        if PID_FILE.exists():
            PID_FILE.unlink()
    except OSError:
        pass


if __name__ == "__main__":
    main()
