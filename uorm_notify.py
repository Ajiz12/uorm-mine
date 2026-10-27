#!/usr/bin/env python3
"""Kirim laporan UORM chat grinding ke Telegram.
Dipanggil cron tiap 6 jam. Angka dari query DB langsung, bukan log bot.

Aturan resmi "How chat XP works":
  5 pesan qualifying pertama/hari = 8 XP
  pesan 6-15                    = 5 XP
  pesan 16-30                   = 3 XP
  total 30 pesan qualifying/hari (di luar itu TIDAK dapat XP)
  chat level +0.5% mining rate per level, cap +25% di Lv.50
"""
import sys, json, urllib.request
sys.path.insert(0, 'uorm-mine')
import uorm_mine as U

MY = '<your-user-uuid>'
ROOM = '<your-room-id>'
DAILY_CAP = 30
TIERS = [(5, 8), (15, 5), (30, 3)]


def tier_xp(n):
    for batas, xp in TIERS:
        if n <= batas:
            return xp
    return 0


def main():
    c = U.SupabaseClient()
    since = '2026-10-27T00:00:00'
    g = len(c.session.get(
        f'{U.SUPABASE_URL}/rest/v1/global_messages'
        f'?user_id=eq.{MY}&select=id&created_at=gte.{since}',
        timeout=30).json())
    r = len(c.session.get(
        f'{U.SUPABASE_URL}/rest/v1/room_messages'
        f'?user_id=eq.{MY}&room_id=eq.{ROOM}&select=id&created_at=gte.{since}',
        timeout=30).json())
    p = c.session.get(
        f'{U.SUPABASE_URL}/rest/v1/profiles?id=eq.{MY}'
        f'&select=xp,level,chat_streak_days', timeout=25).json()[0]

    total = g + r
    counted = min(total, DAILY_CAP)
    xp_hari_ini = sum(tier_xp(i) for i in range(1, counted + 1))
    sisa = max(0, DAILY_CAP - total)

    msg = ('UORM CHAT GRINDING - LAPORAN\n\n'
           f'Pesan hari ini: {total}/{DAILY_CAP} qualifying\n'
           f'- Obrolan Global: {g}\n'
           f'- Room Chat: {r}\n\n'
           f'XP chat hari ini (tier): {xp_hari_ini}\n'
           f'Sisa kuota hari ini: {sisa} pesan\n\n'
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
