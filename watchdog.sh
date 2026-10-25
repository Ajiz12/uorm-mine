#!/bin/bash
# Watchdog UORM bot — dipanggil cron tiap 5 menit
# Cron tidak punya PATH pm2, jadi export eksplisit.

export PATH="$HOME/.nvm/versions/node/v20.19.0/bin:$PATH"

LOG="$HOME/bots/uorm-mine/watchdog.log"
BOT_DIR="$HOME/bots/uorm-mine"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] watchdog cek" >> "$LOG"

if ! pgrep -f "uorm_mine.py" > /dev/null 2>&1; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] bot MATI — restart" >> "$LOG"
    cd "$BOT_DIR" && pm2 restart uorm-mine >> "$LOG" 2>&1
fi

# log makin besar -> potong
if [ -f "$LOG" ] && [ $(stat -c%s "$LOG") -gt 1048576 ]; then
    tail -200 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
