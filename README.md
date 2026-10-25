# UORM Auto Mining Bot

Bot otomatis untuk UORM (`com.uorm.app`) — backend **Supabase**.

## Fitur

| # | Fitur | Interval |
|---|-------|----------|
| 1 | Mining 24 jam (auto-start kalau berhenti) | cek tiap 60s |
| 2 | Lucky box | tiap 4 jam |
| 3 | Boost mining (watch ad) | via misi |
| 4 | Misi daily / weekly / social / one-time | sync tiap 30 menit |
| 5 | Leaderboard harian | tiap 24 jam |
| + | Monitor saldo 24 jam (alert kalau macet) | tiap 10 menit |

**Misi referral TIDAK dikerjakan bot** — `daily_invite`, `daily_share`, `weekly_invite`, dan semua misi dengan `requirement_key = invite_friend` dilewati.

## Setup

```bash
cd uorm-mine
nano .env        # isi UORM_EMAIL, UORM_PASSWORD, UORM_TG_BOT_TOKEN
python3 uorm_mine.py --login
```

Kalau login minta token email, isi `UORM_EMAIL_TOKEN` di `.env` lalu `--login` lagi.

## Menjalankan

```bash
pm2 start ecosystem.config.js
pm2 save
```

Watchdog (cron tiap 5 menit):

```cron
*/5 * * * * uorm-mine/watchdog.sh
```

## Perintah

| Perintah | Fungsi |
|----------|--------|
| `python3 uorm_mine.py --login` | Login + simpan token |
| `python3 uorm_mine.py --discover` | Petakan schema database |
| `python3 uorm_mine.py --status` | Lihat state bot |
| `./status.sh` | Ringkasan read-only |

## File

| File | Isi |
|------|-----|
| `uorm_mine.py` | Bot utama |
| `.env` | Kredensial (**gitignored**) |
| `uorm_tokens.json` | Access + refresh token (**gitignored**) |
| `uorm_state.json` | State runtime (**gitignored**) |
| `uorm_schema.json` | Hasil auto-discovery schema |
| `watchdog.sh` | Restart bot kalau mati |
| `ecosystem.config.js` | Konfigurasi PM2 |

## Backend (hasil reverse engineering)

- **URL:** `https://tnxxckqqhwwajeljrnnl.supabase.co`
- **Anon key:** ada di `assets/flutter_assets/.env` dalam APK
- **Auth:** Supabase GoTrue (`/auth/v1/token?grant_type=password`)
- **RLS:** aktif di semua tabel — semua operasi butuh JWT user

### Tabel

| Tabel | Akses |
|-------|-------|
| `missions` | publik (baca) |
| `mining_sessions` | RLS |
| `user_missions` | RLS |
| `profiles` | RLS |
| `room_members` | RLS |
| `room_messages` | RLS |
| `referrals` | RLS |
| `announcements` | RLS |
| `app_config` | RLS |
| `rooms` | RLS |

### RPC

| Fungsi | Parameter | Akses |
|--------|-----------|-------|
| `get_leaderboard` | `p_limit` | publik |
| `open_lucky_box` | — | auth |
| `claim_daily_bonus` | — | auth |
| `get_missions` | — | auth |
| `get_lucky_box_status` | — | auth |
| `get_announcements` | — | auth |
| `leave_room` | — | auth |
| `get_boost_status` | — | auth |
| `start_mining_session` | `p_rate_per_hour` | auth |
| `apply_mining_boost` | `p_session_id`, `p_multiplier`, `p_duration_ms`, `p_boosted_rate` | auth |
| `claim_mission` | `p_mission_id` | auth |
| `claim_mining_session` | `p_session_id` | auth |

### Edge Function

| Fungsi | Parameter | Keterangan |
|--------|-----------|------------|
| `claim-mining` | `sessionId` (camelCase) | Alternatif claim mining |

**Catatan penting:** semua RPC pakai prefix parameter `p_` (konvensi PL/pgSQL). `apply_mining_boost` punya **2 overload** — kalau tidak kirim keempat parameter, PostgREST balas `PGRST203` (tidak bisa pilih kandidat).

### Cara endpoint ditemukan

Brute-force 15.590 nama RPC gagal total. Yang berhasil: panggil RPC dengan nama yang **typo dekat** (misal `start_minin`), lalu baca hint Postgres *"Perhaps you meant to call the function public.start_mining_session"*. Postgres sendiri yang membocorkan nama fungsi aslinya.

### Misi aktif (19)

- **daily (6):** `daily_open`, `daily_start`, `daily_invite`*, `daily_bundle`, `daily_boost`, `daily_share`*
- **one-time (2):** `once_room`, `once_silver`
- **social (8):** `social_x`, `social_telegram`, `twitter_repost`, `twitter_repost3`, `twitter_7`, `social_post_x`, `repost11`, `rating`
- **weekly (3):** `weekly_streak`, `weekly_boosts`, `weekly_invite`*

\* dilewati (referral)

## Catatan

- Semua operasi mining/boost/misi pakai **RPC asli** — tidak ada INSERT/UPDATE langsung ke tabel (RLS menolak semua).
- APK adalah Flutter AOT tanpa native code (`libapp.so` tidak ada), jadi reverse engineering terbatas pada layer Supabase.
- Boost di app lewat rewarded ad (GamePix), tapi server hanya mencatat hasilnya via `apply_mining_boost` — tidak ada verifikasi iklan di backend. Limit 2 boost/hari dicek oleh `get_boost_status`.
- Pola kenaikan rate per boost di server: +12% dan +15%. Bot pakai multiplier default 1.15.
