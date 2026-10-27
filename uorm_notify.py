#!/usr/bin/env python3
"""Kirim laporan UORM chat grinding ke Telegram.
Dipanggil cron tiap 6 jam. Angka dari query DB langsung, bukan log bot."""
import sys, json, urllib.request
sys.path.insert(0, 'uorm-mine')
import uorm_mine as U

MY = '<your-user-uuid>'
ROOM = '<your-room-id>'


def main():
    c = U.SupabaseClient()
    g = len(c.session.get(
        f'{U.SUPABASE_URL}/rest/v1/global_messages'
        f'?user_id=eq.{MY}&select=id&created_at=gte.2026-10-27T00:00:00',
        timeout=30).json())
    r = len(c.session.get(
        f'{U.SUPABASE_URL}/rest/v1/room_messages'
        f'?user_id=eq.{MY}&room_id=eq.{ROOM}&select=id&created_at=gte.2026-10-27T00:00:00',
        timeout=30).json())
    p = c.session.get(
        f'{U.SUPABASE_URL}/rest/v1/profiles?id=eq.{MY}'
        f'&select=xp,level,chat_streak_days', timeout=25).json()[0]

    msg = ('UORM CHAT GRINDING - LAPORAN\n\n'
           f'Pesan hari ini: {g + r}\n'
           f'- Obrolan Global: {g}\n'
           f'- Room Chat: {r}\n\n'
           f'XP Profile: {p["xp"]}\n'
           f'Level: {p["level"]}\n'
           f'Chat Streak: {p["chat_streak_days"]} hari\n\n'
           'Catatan: XP Obrolan Global dibayar MINGGUAN via payout_chat_xp_weekly. '
           'Room Chat: XP real-time +3-8 per pesan, turun kalau spam.')

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
