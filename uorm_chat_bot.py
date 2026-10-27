#!/usr/bin/env python3
"""UORM chat grinding bot — jawab pertanyaan orang di room chat, cooldown 90s.

Endpoint: RPC send_room_message(p_content, p_reply_to_id) untuk room chat
          RPC send_global_message(p_content, p_reply_to_id) untuk Obrolan Global
Baca pesan: tabel room_messages (room) + global_messages (Obrolan Global)
Room: <your-room-id> (dari room_members akun kita)
Aturan XP (terverifikasi):
- konten duplikat        -> "you already sent that", tanpa XP
- konten terlalu pendek  -> qualified:false, tanpa XP
- reply substantif       -> awarded:true, xp_delta 5-8
"""
import json
import os
import random
import sys
import time
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import uorm_mine as U  # noqa: E402

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE, "uorm_chat_state.json")
LOG_FILE = os.path.join(BASE, "logs", "uorm_chat.log")

ROOM_ID = "<your-room-id>"
MY_USER_ID = "<your-user-uuid>"
COOLDOWN = 90           # detik antar chat (aturan bos)
POLL = 15               # detik cek pesan baru
MAX_HISTORY = 60        # simpan N konten terakhir buat anti-duplikat

# Aturan resmi "How chat XP works":
#   5 pesan qualifying pertama/hari = 8 XP
#   pesan 6-15                    = 5 XP
#   pesan 16-30                   = 3 XP
#   total 30 pesan qualifying/hari (di luar itu TIDAK dapat XP)
# Chat level: +0.5% mining rate per level, cap +25% di Lv.50
DAILY_QUALIFYING_CAP = 30
XP_TIERS = [(5, 8), (15, 5), (30, 3)]   # (batas_atas_pesan_ke, xp)

WIB = timezone(timedelta(hours=7))


def log(msg):
    line = f"[{datetime.now(WIB):%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "last_seen_id": None,
        "last_send_ts": 0,
        "last_src": None,
        "sent_contents": [],
        "replied_ids": [],
        "total_chats": 0,
        "total_xp": 0,
        "started_at": time.time(),
    }


def save_state(st):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATE_FILE)


def tier_xp(n):
    """XP untuk pesan qualifying ke-n hari ini (1-indexed). 0 kalau sudah lewat cap."""
    for batas, xp in XP_TIERS:
        if n <= batas:
            return xp
    return 0


def today_key():
    return datetime.now(WIB).strftime("%Y-%m-%d")


def daily_count(st):
    """Jumlah pesan qualifying hari ini (reset otomatis kalau ganti hari)."""
    if st.get("daily_date") != today_key():
        st["daily_date"] = today_key()
        st["daily_qualifying"] = 0
        save_state(st)
    return st.get("daily_qualifying", 0)


def fetch_qualified_today():
    """Hitung pesan qualifying hari ini langsung dari DB (sumber kebenaran).
    Dipakai buat sinkronisasi state setelah restart / kalau state meleset."""
    try:
        c = U.SupabaseClient()
        since = f"{today_key()}T00:00:00"
        g = c.session.get(
            f"{U.SUPABASE_URL}/rest/v1/global_messages"
            f"?user_id=eq.{MY_USER_ID}&select=id&created_at=gte.{since}",
            timeout=30).json()
        r = c.session.get(
            f"{U.SUPABASE_URL}/rest/v1/room_messages"
            f"?user_id=eq.{MY_USER_ID}&room_id=eq.{ROOM_ID}&select=id&created_at=gte.{since}",
            timeout=30).json()
        return len(g) + len(r)
    except Exception as e:
        log(f"fetch_qualified_today gagal: {e}")
        return None


# ─── deteksi intent pesan orang ───

GREET = ["gm", "good morning", "hi", "hello", "hey", "salam", "سلام",
         "خوب", "وقت بخیر", "xin chào", "chào", "hola"]
QUESTION_WORDS = ["what", "how", "why", "when", "where", "who", "which",
                  "can you", "could you", "do you", "are you", "is it",
                  "?", "چه", "چی", "کی", "چرا", "چطور", "کجا", "sao",
                  "gì", "nào", "phải không"]
# pertanyaan identitas — jawab: Miner
IDENTITY_WORDS = ["who are you", "your name", "what's your name", "whats your name",
                  "siapa kamu", "nama kamu", "lu siapa", "loe siapa", "kamu siapa",
                  "who u", "who you", "your real name", "introduce yourself",
                  "what should i call you", "whats ur name"]
APP_TALK = ["uorm", "mine", "mining", "coin", "token", "withdraw", "saldo",
            "balance", "boost", "rate", "task", "mission", "referral",
            "app", "this", "what's this", "what is this", "help"]


def classify(text):
    t = text.lower().strip()
    if any(w in t for w in IDENTITY_WORDS):
        return "identity"
    if any(t.startswith(g) or t == g for g in GREET) and len(t) < 30:
        return "greet"
    if any(w in t for w in APP_TALK):
        return "app"
    if any(w in t for w in QUESTION_WORDS):
        return "question"
    return "chat"


# ─── template jawaban (variatif, anti-monoton) ───

REPLIES = {
    "identity": [
        "im miner, nice to meet you",
        "miner here, just another miner in this room",
        "people call me miner, whats yours?",
        "im miner — been around here mining quietly",
        "miner, from indonesia. you?",
        "name is miner, good to see new faces in here",
    ],
    "greet": [
        "gm! hope your mining is running smooth today",
        "good morning! another day of mining, lets go",
        "hey, welcome! how's your mining going?",
        "hi there! yeah this app is pretty simple once you get the hang of it",
        "morning! dont forget to check your boost before the session ends",
        "gm, glad to see more people active in here today",
        "hey! how long have you been mining on this app?",
        "good morning everyone, hope the rates are treating you well",
        "hi! yeah the community here is pretty helpful for beginners",
        "morning! whats your mining rate looking like right now?",
        "hey there, welcome to the room",
        "gm! dont forget the lucky box resets every 4 hours",
    ],
    "app": [
        "this is UORM — you mine coins every 24h and can boost the rate with ads. what do you want to know?",
        "UORM mining runs 24h sessions, boost gives extra rate. anything specific you need help with?",
        "basically you start a mining session, claim after 24h, and do daily missions for extra. which part are you stuck on?",
        "you can boost up to 2x a day and the lucky box every 4h. want me to explain the rates?",
        "mining rate depends on your boost multiplier, higher multiplier = more coins per hour. what's your question?",
        "the trick is to claim your session right when the 24h cycle ends so you dont lose any coins",
        "daily missions are the easiest extra coins, just open the app and start a session",
        "if you want more per hour, stack the boost multiplier before the session starts",
        "the lucky box is free coins every 4 hours, dont sleep on that",
        "your rank tier goes up as you mine more, and higher tiers get better rates",
        "i just let the session run and claim when its done, simple and consistent",
        "boosting is the main lever for rate — everything else is pretty fixed",
        "start the session, do the daily missions, claim on time. thats the whole loop",
        "the app is basically a tap miner with a chat room attached, nothing too complicated",
        "if your rate looks low, check whether your boost is still active or already expired",
    ],
    "question": [
        "good question — in my experience the daily missions give the most consistent rewards. what are you trying to do?",
        "not sure about that one, but the mining sessions and boosts are the main way to earn here. what do you need?",
        "i think it depends on your rank tier, higher rank = better rates. what are you trying to figure out?",
        "hmm, i'd say just keep your sessions running and claim on time. what's your situation?",
        "if you mean the app itself — start mining, boost twice a day, claim lucky box. that's the routine",
        "honestly i dont know for sure, but being consistent every day has worked better for me than chasing big numbers",
        "from what ive seen, people who show up daily do better than people who go hard for a week then disappear",
        "thats a good one — i think the answer is just consistency, the app rewards showing up more than anything",
        "i cant say for certain, but the boost multiplier is where most of the difference comes from",
        "depends on what youre comparing it to, but for a free app the payout structure is pretty fair",
        "i dont want to guess and give you wrong info, but the basics are session plus boost plus daily missions",
        "no idea to be honest, but someone in here probably knows — worth asking around",
        "i think its mostly about the streak, missing a day costs you more than youd expect",
        "good question, i had the same one when i started. the missions tab explains it better than i can",
        "if youre asking about withdrawals, i havent gotten that far myself yet",
    ],
    "chat": [
        "thats true, consistency is everything in mining",
        "same here, been grinding the daily missions every day",
        "yeah the community here is pretty active, good for tips",
        "makes sense. how long have you been mining?",
        "agreed, patience pays off with these apps",
        "thats the spirit, keep the sessions running",
        "facts. small amounts add up faster than people think",
        "yeah i noticed that too, the room gets busy around the reset window",
        "same energy here, just trying not to miss a day",
        "well said, showing up is half the battle",
        "thats exactly how i see it too",
        "yeah, and its way better than doing nothing and waiting",
        "totally agree with you on that one",
        "thats a solid way to look at it, respect",
        "yeah the grind is slow but its honest work at least",
        "i feel that, some days are just about not breaking the streak",
        "couldnt have said it better myself",
        "yeah exactly, and its nice doing it alongside other people for once",
    ],
}


def pick_reply(intent, st):
    pool = REPLIES[intent]
    sent = set(st["sent_contents"][-MAX_HISTORY:])
    fresh = [r for r in pool if r not in sent]
    if not fresh:
        # pool habis — tambah variasi supaya tidak "you already sent that"
        fresh = [f"{p} {r}" for r in pool
                 for p in ("yeah,", "honestly,", "i think", "for real,")]
        fresh = [f for f in fresh if f not in sent] or fresh
    return random.choice(fresh)


# ─── ambil pesan baru ───

def fetch_new_messages(st):
    """Ambil pesan baru dari room chat + Obrolan Global."""
    c = U.SupabaseClient()
    out = []
    # 1) room chat biasa
    q = (f"room_messages?room_id=eq.{ROOM_ID}"
         f"&select=id,user_id,username,content,created_at,reply_to_id"
         f"&order=created_at.desc&limit=30")
    r = c.session.get(f"{U.SUPABASE_URL}/rest/v1/{q}", timeout=30)
    if r.status_code == 200:
        for m in r.json():
            m["_src"] = "room"
            out.append(m)
    else:
        log(f"fetch room gagal: {r.status_code} {r.text[:120]}")
    # 2) Obrolan Global
    q2 = ("global_messages?select=id,user_id,username,content,created_at,reply_to_id"
          "&order=created_at.desc&limit=40")
    r2 = c.session.get(f"{U.SUPABASE_URL}/rest/v1/{q2}", timeout=30)
    if r2.status_code == 200:
        for m in r2.json():
            m["_src"] = "global"
            out.append(m)
    else:
        log(f"fetch global gagal: {r2.status_code} {r2.text[:120]}")
    # filter: bukan pesan sendiri, belum pernah di-reply
    replied = set(st["replied_ids"][-300:])
    fresh = [m for m in out
             if m.get("user_id") != MY_USER_ID and m.get("id") not in replied]
    fresh.reverse()  # terlama dulu biar urut wajar
    return fresh


def send_reply(content, reply_to, src="room"):
    c = U.SupabaseClient()
    if src == "global":
        payload = {"p_content": content}
        if reply_to:
            payload["p_reply_to_id"] = reply_to
        r = c.session.post(f"{U.SUPABASE_URL}/rest/v1/rpc/send_global_message",
                           data=json.dumps(payload), timeout=30)
    else:
        payload = {"p_content": content}
        if reply_to:
            payload["p_reply_to_id"] = reply_to
        r = c.session.post(f"{U.SUPABASE_URL}/rest/v1/rpc/send_room_message",
                           data=json.dumps(payload), timeout=30)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"raw": r.text[:200]}


def main():
    log("=== UORM chat grinding bot start ===")
    st = load_state()
    log(f"state: chats={st['total_chats']} xp={st['total_xp']}")

    # sinkronkan counter harian dari DB — state bisa meleset kalau bot
    # restart atau kalau ada pesan yang dikirim dari luar bot
    db_count = fetch_qualified_today()
    if db_count is not None:
        st["daily_date"] = today_key()
        st["daily_qualifying"] = db_count
        save_state(st)
        log(f"sinkron harian dari DB: {db_count}/{DAILY_QUALIFYING_CAP} pesan qualifying")
        if db_count >= DAILY_QUALIFYING_CAP:
            log("cap harian 30 sudah tercapai — bot tidak akan kirim sampai reset besok")

    while True:
        try:
            now = time.time()
            wait = COOLDOWN - (now - st["last_send_ts"])
            if wait > 0:
                time.sleep(min(wait, POLL))
                continue

            # cap harian 30 pesan qualifying — di luar itu TIDAK dapat XP
            # (aturan resmi "How chat XP works"), jadi bot berhenti kirim
            done = daily_count(st)
            if done >= DAILY_QUALIFYING_CAP:
                time.sleep(300)      # cek lagi 5 menit lagi
                continue

            fresh = fetch_new_messages(st)
            if not fresh:
                time.sleep(POLL)
                continue

            # selang-sing: kalau terakhir kirim di global -> cari target di room, dan sebaliknya
            want = "room" if st.get("last_src") == "global" else "global"
            target = None
            # pass 1: sumber yang diinginkan + intent bagus
            for m in fresh:
                if m.get("_src") == want and classify(m["content"]) in ("app", "question", "identity"):
                    target = m
                    break
            # pass 2: sumber yang diinginkan, pesan terbaru apa aja
            if target is None:
                for m in fresh:
                    if m.get("_src") == want:
                        target = m
                        break
            # pass 3: sumber lawan, intent bagus (kalau sumber pilihan kosong)
            if target is None:
                other = "global" if want == "room" else "room"
                for m in fresh:
                    if m.get("_src") == other and classify(m["content"]) in ("app", "question", "identity"):
                        target = m
                        break
            # pass 4: apa aja yang ada
            if target is None:
                target = fresh[-1]

            intent = classify(target["content"])
            reply = pick_reply(intent, st)
            src = target.get("_src", "room")

            code, res = send_reply(reply, target["id"], src)
            if code == 200 and res.get("ok"):
                xp = res.get("xp_delta") or 0
                awarded = res.get("awarded", False)
                st["total_chats"] += 1
                st["total_xp"] += xp
                st["last_send_ts"] = time.time()
                st["last_src"] = src
                # hitung posisi harian + XP sesuai tier aturan resmi
                n = daily_count(st) + 1
                expected = tier_xp(n)
                st["daily_qualifying"] = n
                st["sent_contents"].append(reply)
                st["replied_ids"].append(target["id"])
                st["sent_contents"] = st["sent_contents"][-MAX_HISTORY:]
                st["replied_ids"] = st["replied_ids"][-300:]
                save_state(st)
                log(f"CHAT #{st['total_chats']} [{src}] -> {target['username'][:15]}: "
                    f"\"{target['content'][:40]}\" | balas: \"{reply[:50]}\" "
                    f"| xp+{xp} awarded={awarded} | harian {n}/{DAILY_QUALIFYING_CAP} "
                    f"(tier {expected}) total_xp={st['total_xp']}")
            else:
                msg = res.get("message") or res.get("raw") or str(res)[:100]
                log(f"kirim gagal ({code}): {msg}")
                # konten duplikat / tidak qualified — tetap tandai sudah di-reply
                st["replied_ids"].append(target["id"])
                st["replied_ids"] = st["replied_ids"][-200:]
                st["last_send_ts"] = time.time()
                save_state(st)
        except KeyboardInterrupt:
            log("stop oleh user")
            break
        except Exception as e:
            log(f"error loop: {e}")
            time.sleep(30)


if __name__ == "__main__":
    main()
