#!/usr/bin/env python3
"""Auto-claim badge UORM harian, 1 per hari, urutan tertua dulu.

Dipanggil cron harian ~07:05 WIB (sesaat setelah reset reveal UTC 00:00).
Alur:
  1. get_my_badges (read-only): cek revealed_today vs reveals_per_day,
     dan daftar badge berstatus 'ready'.
  2. Kalau slot hari ini sudah penuh atau tidak ada badge ready: SKIP,
     tidak ada reveal, tidak ada notifikasi Telegram.
  3. Kalau ada slot dan ada badge ready: reveal badge ready dengan
     earned_at paling lama dulu (satu saja), via reveal_badge(p_badge_id).
  4. Setelah reveal, verifikasi ulang dengan get_my_badges dan kirim
     notifikasi Telegram (nama badge, serial, pin) hanya kalau statusnya
     sudah 'revealed'.
  5. Notifikasi HANYA ke Telegram, tidak ke chat Muse: sukses dan gagal
     sama-sama dikirim ke Telegram dari script ini. SKIP (slot penuh /
     tidak ada badge ready) tetap diam, supaya tidak spam harian.

Keamanan: tidak pernah reveal lebih dari 1 per run, tidak pernah reveal
badge berstatus locked/revealed, dan tidak mengulang reveal kalau server
sudah bilang slot penuh. Badge ID dan tanggal dipakai dari server,
bukan dari daftar hardcoded.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import uorm_mine as U  # noqa: E402


def send_tg(text):
    """Notif Telegram dimatikan — semua masuk laporan harian. Cuma log."""
    print(f"[TG dimatikan] {text[:120]}")
    return False


def catat_hasil(badge: dict):
    """Simpan badge ter-reveal ke state buat laporan harian."""
    try:
        st = U.load_state()
        st.setdefault("badges", {})["last_revealed"] = {
            "title": badge.get("title"),
            "id": badge.get("id"),
            "serial": badge.get("serial"),
            "date": U.now_wib().strftime("%Y-%m-%d"),
        }
        U.save_state(st)
    except Exception as e:
        print(f"catat hasil gagal: {e}")


def main():
    c = U.SupabaseClient()
    if not c.ensure_token():
        print("GAGAL: token tidak bisa di-refresh, tidak ada reveal")
        send_tg("⚠️ UORM Badge: gagal cek hari ini, token tidak bisa di-refresh. "
                "Tidak ada badge yang di-reveal.")
        return 1

    data = c.rpc("get_my_badges")
    if not isinstance(data, dict) or "badges" not in data:
        print(f"GAGAL: get_my_badges tidak mengembalikan data: {str(data)[:200]}")
        send_tg("⚠️ UORM Badge: gagal cek hari ini (get_my_badges tidak mengembalikan data). "
                "Tidak ada badge yang di-reveal.")
        return 1

    revealed_today = data.get("revealed_today", 0)
    per_day = data.get("reveals_per_day", 1)
    print(f"revealed_today={revealed_today}/{per_day} next_reveal_at={data.get('next_reveal_at')}")

    if revealed_today >= per_day:
        print("SKIP: slot reveal hari ini sudah penuh")
        return 0

    ready = [b for b in data["badges"] if b.get("status") == "ready"]
    if not ready:
        print("SKIP: tidak ada badge ready hari ini")
        return 0

    # Urutan tertua dulu: earned_at paling lama, lalu id sebagai tie-breaker.
    ready.sort(key=lambda b: (b.get("earned_at") or "9999", b.get("id") or ""))
    target = ready[0]
    print(f"REVEAL: {target['id']} ({target.get('title')}) earned_at={target.get('earned_at')} "
          f"antrean ready={len(ready)}")

    res = c.rpc("reveal_badge", {"p_badge_id": target["id"]})
    print(f"reveal_badge -> {str(res)[:300]}")
    if not isinstance(res, dict) or not res.get("ok"):
        print(f"GAGAL: reveal tidak berhasil: {str(res)[:300]}")
        send_tg(f"⚠️ UORM Badge: reveal {target.get('title')} ({target['id']}) gagal. "
                f"Server: {str(res)[:150]}. Tidak ada badge yang ter-reveal dari run ini.")
        return 1

    # Verifikasi ulang dari server, jangan percaya respon reveal saja.
    data2 = c.rpc("get_my_badges")
    verified = None
    if isinstance(data2, dict):
        for b in data2.get("badges", []):
            if b.get("id") == target["id"]:
                verified = b
                break

    if verified and verified.get("status") == "revealed":
        catat_hasil(verified)
        print(f"TERVERIFIKASI revealed: {verified.get('title')} "
              f"(serial {verified.get('serial')}) — masuk laporan harian")
        return 0

    print(f"GAGAL VERIFIKASI: status setelah reveal = "
          f"{verified.get('status') if verified else 'tidak ditemukan'}")
    send_tg(f"⚠️ UORM Badge: reveal {target.get('title')} ({target['id']}) belum terverifikasi "
            f"revealed di server (status: {verified.get('status') if verified else 'tidak ditemukan'}). "
            "Cek manual di aplikasi sebelum mengulang.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
