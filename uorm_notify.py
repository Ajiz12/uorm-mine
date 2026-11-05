#!/usr/bin/env python3
"""Kirim laporan UORM chat grinding ke Telegram.
Dipanggil cron tiap 6 jam. Angka dari query DB langsung, bukan log bot.

Aturan resmi "How chat XP works":
  5 pesan qualifying pertama/hari = 8 XP
  pesan 6-15                    = 5 XP
  pesan 16-30                   = 3 XP
  total 30 pesan qualifying/hari (di luar itu TIDAK dapat XP)
  chat level +0.5% mining rate per level, cap +25% di Lv.50

Target volume harian (perintah bos 29 Okt 2026):
  Obrolan Global: 50-100 pesan/hari
  Room Chat     : 20-30 pesan/hari

Reset kuota XP server = UTC-day (00:00 UTC = 07:00 WIB).
"""
import sys, json, os, urllib.request
from datetime import datetime, timezone, timedelta
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import uorm_mine as U

# MY diisi di main() dari user_id akun login (token), BUKAN hardcoded.
MY = None
ROOM = U.cfg("UORM_ROOM_ID", "")
DAILY_CAP = 30
TIERS = [(5, 8), (15, 5), (30, 3)]
WIB = timezone(timedelta(hours=7))
GLOBAL_TARGET = (50, 100)
ROOM_TARGET = (20, 30)


def tier_xp(n):
    for batas, xp in TIERS:
        if n <= batas:
            return xp
    return 0


def count_day(c, day_utc):
    """Jumlah pesan kita pada UTC-day tertentu (server reset per UTC)."""
    nxt = (datetime.strptime(day_utc, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y-%m-%d')
    g = c.session.get(
        f'{U.SUPABASE_URL}/rest/v1/global_messages'
        f'?user_id=eq.{MY}&select=id'
        f'&created_at=gte.{day_utc}T00:00:00&created_at=lt.{nxt}T00:00:00',
        timeout=30).json()
    r = c.session.get(
        f'{U.SUPABASE_URL}/rest/v1/room_messages'
        f'?user_id=eq.{MY}&room_id=eq.{ROOM}&select=id'
        f'&created_at=gte.{day_utc}T00:00:00&created_at=lt.{nxt}T00:00:00',
        timeout=30).json()
    return len(g), len(r)


def xp_for(count):
    """XP dari N pesan qualifying (cap 30/hari)."""
    counted = min(count, DAILY_CAP)
    return sum(tier_xp(i) for i in range(1, counted + 1))


def main():
    global MY
    c = U.SupabaseClient()
    MY = c.tokens.get('user_id')
    if not MY:
        print('GAGAL: tidak ada user_id login di token, laporan tidak dikirim')
        return
    now_utc = datetime.now(timezone.utc)
    today = now_utc.strftime('%Y-%m-%d')
    yesterday = (now_utc - timedelta(days=1)).strftime('%Y-%m-%d')

    g_t, r_t = count_day(c, today)
    g_y, r_y = count_day(c, yesterday)

    tot_t, tot_y = g_t + r_t, g_y + r_y
    xp_t, xp_y = xp_for(tot_t), xp_for(tot_y)

    p = c.session.get(
        f'{U.SUPABASE_URL}/rest/v1/profiles?id=eq.{MY}'
        f'&select=xp,level,chat_streak_days', timeout=25).json()[0]

    sisa_t = max(0, DAILY_CAP - tot_t)

    msg = ('UORM CHAT GRINDING - LAPORAN\n\n'
           f'Perolehan Jumlah XP {xp_y} (hari kemaren) + {xp_t} (hari ini) '
           f'= {xp_y + xp_t} XP\n\n'
           f'Hari ini ({tot_t} pesan):\n'
           f'- Obrolan Global: {g_t}/{GLOBAL_TARGET[1]}\n'
           f'- Room Chat: {r_t}/{ROOM_TARGET[1]}\n'
           f'- Sisa kuota XP: {sisa_t} pesan\n\n'
           f'Hari kemaren ({tot_y} pesan):\n'
           f'- Obrolan Global: {g_y}\n'
           f'- Room Chat: {r_y}\n\n'
           f'XP Profile: {p["xp"]}\n'
           f'Level: {p["level"]}\n'
           f'Chat Streak: {p["chat_streak_days"]} hari\n\n'
           'Aturan XP: 5 pertama = 8 XP, 6-15 = 5 XP, 16-30 = 3 XP. '
           'Di luar 30/hari tidak dapat XP. '
           'Chat level +0.5% mining rate per level (cap +25% di Lv.50).')

    tok = U.cfg('UORM_TG_BOT_TOKEN')
    chat = U.cfg('UORM_TG_CHAT_ID')
    req = urllib.request.Request(
        f'https://api.telegram.org/bot{tok}/sendMessage',
        data=json.dumps({'chat_id': chat, 'text': msg}).encode(),
        headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as resp:
        j = json.loads(resp.read())
    print('TERKIRIM' if j.get('ok') else 'GAGAL',
          '| message_id:', j.get('result', {}).get('message_id'))


if __name__ == '__main__':
    main()
