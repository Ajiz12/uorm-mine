#!/bin/bash
# Watchdog UORM bot — dipanggil cron tiap 2 menit
# Pola dari script ITLG (febfrmn/i-tlg): PID file + verifikasi cmdline
# + notif Telegram saat restart. Cron tidak punya PATH pm2 -> export eksplisit.

export PATH="$HOME/.nvm/versions/node/v20.19.0/bin:$PATH"

BOT_DIR="$HOME/bots/uorm-mine"
PID_FILE="$BOT_DIR/.bot.pid"
LOG="$BOT_DIR/watchdog.log"
PM2_LOG="$BOT_DIR/pm2-out.log"

# ── baca token/chat dari .env ──
TG_TOKEN=""
TG_CHAT=""
if [ -f "$BOT_DIR/.env" ]; then
  TG_TOKEN=$(grep -m1 '^UORM_TG_BOT_TOKEN=' "$BOT_DIR/.env" | cut -d= -f2- | tr -d '"'"'"' \r')
  TG_CHAT=$(grep -m1 '^UORM_TG_CHAT_ID=' "$BOT_DIR/.env" | cut -d= -f2- | tr -d '"'"'"' \r')
fi

notify() {
  [ -n "$TG_TOKEN" ] && [ -n "$TG_CHAT" ] || return 0
  curl -s --max-time 15 -X POST "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
    -d chat_id="$TG_CHAT" \
    -d text="$1" >/dev/null 2>&1 || true
}

ts() { date '+%Y-%m-%d %H:%M:%S'; }

# ── cek bot hidup: PID file dulu, fallback pgrep ──
# PENTING: jangan pakai "pgrep -f uorm_mine.py" polos — akan match
# shell/cron yang menjalankan script ini (false positive).
# Selalu verifikasi cmdline proses = python3 .../uorm_mine.py
is_bot_alive() {
  if [ -f "$PID_FILE" ]; then
    local pid
    pid=$(cat "$PID_FILE" 2>/dev/null)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      if tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null | grep -Eq 'python3? .*uorm_mine\.py'; then
        return 0
      fi
    fi
  fi
  # fallback: cari proses python uorm_mine.py, verifikasi cmdline
  local p cmd
  for p in $(pgrep -f 'uorm_mine\.py' 2>/dev/null); do
    [ "$p" = "$$" ] && continue
    cmd=$(tr '\0' ' ' <"/proc/$p/cmdline" 2>/dev/null || true)
    case "$cmd" in
      *watchdog*|*pgrep*|*"bash -c"*|*hermes-snap*) continue ;;
      *python3?*uorm_mine.py*)
        echo "$p" >"$PID_FILE"
        return 0
        ;;
    esac
  done
  return 1
}

# ── cek PM2 entry ada & status asli ──
# CATATAN: "pm2 describe" SELALU exit 0 walau proses mati/tidak ada.
# Harus baca status dari "pm2 jlist" (JSON) — jangan percaya exit code.
pm2_status() {
  pm2 jlist 2>/dev/null | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    for p in d:
        if p.get('name') == 'uorm-mine':
            print(p['pm2_env']['status'])
            break
    else:
        print('missing')
except Exception:
    print('missing')
" 2>/dev/null || echo "missing"
}

is_pm2_ok() {
  [ "$(pm2_status)" != "missing" ]
}

BOT_WAS_DOWN=0

if ! is_bot_alive; then
  BOT_WAS_DOWN=1
  echo "[$(ts)] bot MATI — restart" >> "$LOG"
  rm -f "$PID_FILE"

  cd "$BOT_DIR" || exit 1

  ST=$(pm2_status)
  if [ "$ST" = "missing" ]; then
    # entry tidak ada di PM2 — start fresh
    pm2 start ecosystem.config.js >> "$LOG" 2>&1
    pm2 save >> "$LOG" 2>&1
  else
    # entry ada. "pm2 restart" GAGAL kalau proses sudah mati total
    # ("Process N not found") — hapus lalu start ulang.
    pm2 delete uorm-mine >> "$LOG" 2>&1
    pm2 start ecosystem.config.js >> "$LOG" 2>&1
    pm2 save >> "$LOG" 2>&1
  fi

  sleep 6
fi

# ── notif hanya kalau ada yang di-restart ──
if [ "$BOT_WAS_DOWN" -eq 1 ]; then
  BOT_OK=0
  is_bot_alive && BOT_OK=1
  BOT_PID=$(cat "$PID_FILE" 2>/dev/null || echo '?')
  UPTIME=$(pm2 describe uorm-mine 2>/dev/null | grep -m1 uptime | awk '{print $NF}')

  if [ "$BOT_OK" -eq 1 ]; then
    notify "🔄 UORM bot restarted
🤖 PID $BOT_PID
🕐 $(date '+%d %b %H:%M') WIB
Bot mati dan sudah dihidupkan otomatis."
    echo "[$(ts)] restarted pid=$BOT_PID" >> "$LOG"
    exit 0
  fi

  notify "❌ UORM bot FAILED restart
🕐 $(date '+%d %b %H:%M') WIB
Cek VPS: pm2 logs uorm-mine"
  echo "[$(ts)] FAILED restart" >> "$LOG"
  exit 1
fi

exit 0
