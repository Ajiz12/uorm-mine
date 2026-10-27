# UORM Auto Mining Bot

Bot otomatis untuk UORM (`com.uorm.app`) — backend **Supabase**.

## Fitur

| # | Fitur | Interval |
|---|-------|----------|
| 1 | Mining 24 jam (auto-start kalau berhenti) | cek tiap 10 menit |
| 2 | Lucky box | tiap 4 jam |
| 3 | Boost mining (watch ad) | via misi |
| 4 | Misi daily / weekly / social / one-time | sync tiap 30 menit |
| 5 | Leaderboard harian | tiap 24 jam |
| 6 | **Chat grinding XP** (jawab room chat + Obrolan Global, selang-seling, cooldown 90s) | tiap 90s |
| 7 | Notifikasi laporan chat ke Telegram | tiap 6 jam |

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
| `python3 uorm_notify.py` | Kirim laporan chat ke Telegram (manual) |
| `pm2 logs uorm-chat` | Log chat grinding |

## Chat Grinding XP

Bot `uorm_chat_bot.py` (PM2 `uorm-chat`) grinding XP lewat **dua room sekaligus**,
dengan pola **selang-seling** (bergantian room chat ↔ Obrolan Global), cooldown 90 detik.

### Aturan XP resmi ("How chat XP works")

| Pesan qualifying ke- | XP |
|---|---|
| 1–5 | **8 XP** |
| 6–15 | **5 XP** |
| 16–30 | **3 XP** |
| 31+ | **0 XP** (di luar cap) |

- **Total 30 pesan qualifying per hari** — pesan di luar itu tidak dapat XP sama sekali
- **Chat level** menambah **+0.5% mining rate per level**, cap **+25% di Lv.50**
- Bot menghormati cap ini: setelah 30 pesan, bot berhenti kirim sampai reset hari berikutnya
  (kirim terus setelah cap = murni spam, tanpa XP, berisiko di-flag)
- Counter harian di-reset otomatis tiap ganti hari, dan di-sinkronkan dari DB saat bot start
  (jadi akurat walau bot restart atau ada pesan yang dikirim dari luar bot)

### Dua sistem XP yang berbeda

| Room | Tabel | Sistem XP | Keterangan |
|------|-------|-----------|------------|
| Room Chat | `room_messages` | **Real-time** | `xp_delta` + `awarded` langsung di response `send_room_message`. Terverifikasi naik rapi +3/pesan (13→52). Turun 8→5→3 kalau spam |
| Obrolan Global | `global_messages` | **Mingguan** | Response balik `qualified:true` tapi `awarded:false`, `xp_delta:0`. XP dibayar sekaligus via function internal `payout_chat_xp_weekly` (permission denied kalau dipanggil user — cron sistem) |

Karena sistemnya beda, bot selang-seling supaya **kedua jalur XP jalan bersamaan** —
XP harian dari room chat, XP mingguan dari Obrolan Global.

### RPC

- `send_room_message(p_content, p_reply_to_id, p_image_url)` — room chat
- `send_global_message(p_content, p_reply_to_id)` — Obrolan Global

### Aturan XP terverifikasi

- Konten duplikat ditolak (`you already sent that`) — tanpa XP
- Konten terlalu pendek → `qualified:false` — tanpa XP
- Reply substantif ke pesan orang → `awarded:true` (+3-8 XP)
- Server throttle → balas `slow down` (rate limit XP harian per akun)
- Room chat: XP naik real-time; Obrolan Global: `qualified:true` yang menentukan payout mingguan
- **Cap 30 pesan qualifying/hari** (lihat tabel di atas) — bot berhenti kirim setelah cap tercapai

### Perilaku bot

- **Selang-seling:** cek `last_src` — kalau terakhir kirim di global, cari target di room chat, dan sebaliknya. Prioritas 4 tahap (sumber pilihan + intent bagus → sumber pilihan apa aja → sumber lawan + intent bagus → apa aja yang ada), jadi kalau satu room sepi bot tetap kirim ke yang ramai
- **Deteksi intent:** sapaan / soal app / pertanyaan / obrolan biasa / identitas — tiap intent punya pool jawaban (greet 12, app 15, question 15, chat 18, identity 6)
- **Identitas:** kalau ada yang nanya "who are you" / "siapa kamu" → jawab sebagai **Miner**
- **Anti-duplikat:** simpan 300 ID terakhir + 200 konten terakhir, plus variasi prefix dinamis ("yeah,", "honestly,", "i think") kalau pool habis
- **State:** `uorm_chat_state.json` (chats, xp, replied_ids, last_src) — tahan restart
- **Room:** `<your-room-id>` (dari `room_members` akun)

### Notifikasi Telegram

`uorm_notify.py` mengirim laporan tiap 6 jam (cron `0 */6 * * *`, jam 00:00/06:00/12:00/18:00 WIB)
ke chat Telegram. Isinya: jumlah pesan hari ini (dipisah global/room), XP profile, level, chat streak.
Angka diambil dari **query database langsung**, bukan log bot — karena `total_xp` di state bot hanya
menghitung XP room chat, sedangkan XP profile juga termasuk payout global.

## File

| File | Isi |
|------|-----|
| `uorm_mine.py` | Bot utama |
| `uorm_chat_bot.py` | Bot chat grinding XP (dual-room, selang-seling) |
| `uorm_notify.py` | Script notifikasi Telegram tiap 6 jam |
| `probe_rooms.py` | Probe struktur room chat |
| `ecosystem.chat.config.js` | Konfig PM2 chat bot |
| `.env` | Kredensial (**gitignored**) |
| `uorm_tokens.json` | Access + refresh token (**gitignored**) |
| `uorm_state.json` | State runtime (**gitignored**) |
| `uorm_chat_state.json` | State chat bot (**gitignored**) |
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
| `room_messages` | RLS (hanya room member) |
| `global_messages` | RLS (bisa select — beda dari `room_messages`) |
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
| `prestige_chat` | — | auth (butuh level 50 — belum relevan untuk akun silver) |
| `send_room_message` | `p_content`, `p_reply_to_id`, `p_image_url` | auth — room chat |
| `send_global_message` | `p_content`, `p_reply_to_id` | auth — Obrolan Global |
| `switch_room` | `p_new_room_id`, `p_join_pin` | auth |
| `create_room` | `p_name` | auth |
| `generate_room_code` | — | auth |
| `assert_chat_allowed` | — | auth |
| `payout_chat_xp_weekly` | — | **internal** (permission denied untuk user — cron sistem) |

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

- **Dua sistem XP chat yang berbeda.** Room Chat (`room_messages`) bayar XP **real-time** per pesan (`xp_delta` + `awarded` langsung di response). Obrolan Global (`global_messages`) **tidak** bayar per pesan — response balik `qualified:true` tapi `awarded:false`, `xp_delta:0`; XP-nya dibayar sekaligus mingguan via `payout_chat_xp_weekly` (function internal, permission denied untuk user). Makanya bot selang-seling: dua jalur XP jalan bersamaan.
- **Cap 30 pesan qualifying/hari.** Aturan resmi "How chat XP works": 5 pertama = 8 XP, 6–15 = 5 XP, 16–30 = 3 XP, di luar 30 = 0 XP. Bot menghormati cap ini dan berhenti kirim sampai reset hari berikutnya. Chat level menambah +0.5% mining rate per level, cap +25% di Lv.50.
- **`total_xp` di `uorm_chat_state.json` bukan angka XP sebenarnya** — itu cuma akumulasi `xp_delta` room chat. XP asli ada di kolom `profiles.xp` (termasuk payout global). Script notifikasi sengaja query `profiles.xp`, bukan state bot.
- Semua operasi mining/boost/misi pakai **RPC asli** — tidak ada INSERT/UPDATE langsung ke tabel (RLS menolak semua).
- **Sesi expired tidak ditandai server.** Kolom `claimed` di `mining_sessions` tetap `false` sampai user klaim manual. Bot mendeteksi siklus 24 jam selesai dengan menghitung `started_at + session_duration_ms`, bukan dengan membaca status — kalau cuma percaya `claimed`, bot akan menganggap sesi aktif selamanya dan tidak pernah claim.
- Error RPC (HTTP 400 dengan pesan `already claimed` / `session already active`) adalah **kondisi normal** (misal klaim dari HP), bukan kegagalan — makanya RPC pakai `rpc_full()` yang mempertahankan status code.
- APK adalah Flutter AOT tanpa native code (`libapp.so` tidak ada), jadi reverse engineering terbatas pada layer Supabase.
- Boost di app lewat rewarded ad (GamePix), tapi server hanya mencatat hasilnya via `apply_mining_boost` — tidak ada verifikasi iklan di backend. Limit 2 boost/hari dicek oleh `get_boost_status`.
- Pola kenaikan rate per boost di server: +12% dan +15%. Bot pakai multiplier default 1.15.
