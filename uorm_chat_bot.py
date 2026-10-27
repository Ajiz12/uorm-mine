#!/usr/bin/env python3
"""UORM chat grinding bot — jawab pertanyaan orang di room chat, cooldown 90s.

Endpoint: RPC send_room_message(p_content, p_reply_to_id)
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


# ─── deteksi intent pesan orang ───

GREET = ["gm", "good morning", "hi", "hello", "hey", "salam", "سلام",
         "خوب", "وقت بخیر", "xin chào", "chào", "hola"]
QUESTION_WORDS = ["what", "how", "why", "when", "where", "who", "which",
                  "can you", "could you", "do you", "are you", "is it",
                  "?", "چه", "چی", "کی", "چرا", "چطور", "کجا", "sao",
                  "gì", "nào", "phải không"]
APP_TALK = ["uorm", "mine", "mining", "coin", "token", "withdraw", "saldo",
            "balance", "boost", "rate", "task", "mission", "referral",
            "app", "this", "what's this", "what is this", "help"]


def classify(text):
    t = text.lower().strip()
    if any(t.startswith(g) or t == g for g in GREET) and len(t) < 30:
        return "greet"
    if any(w in t for w in APP_TALK):
        return "app"
    if any(w in t for w in QUESTION_WORDS):
        return "question"
    return "chat"


# ─── template jawaban (variatif, anti-monoton) ───

REPLIES = {
    "greet": [
        "gm! hope your mining is running smooth today",
        "good morning! another day of mining, lets go",
        "hey, welcome! how's your mining going?",
        "hi there! yeah this app is pretty simple once you get the hang of it",
        "morning! dont forget to check your boost before the session ends",
    ],
    "app": [
        "this is UORM — you mine coins every 24h and can boost the rate with ads. what do you want to know?",
        "UORM mining runs 24h sessions, boost gives extra rate. anything specific you need help with?",
        "basically you start a mining session, claim after 24h, and do daily missions for extra. which part are you stuck on?",
        "you can boost up to 2x a day and the lucky box every 4h. want me to explain the rates?",
        "mining rate depends on your boost multiplier, higher multiplier = more coins per hour. what's your question?",
    ],
    "question": [
        "good question — in my experience the daily missions give the most consistent rewards. what are you trying to do?",
        "not sure about that one, but the mining sessions and boosts are the main way to earn here. what do you need?",
        "i think it depends on your rank tier, higher rank = better rates. what are you trying to figure out?",
        "hmm, i'd say just keep your sessions running and claim on time. what's your situation?",
        "if you mean the app itself — start mining, boost twice a day, claim lucky box. that's the routine",
    ],
    "chat": [
        "thats true, consistency is everything in mining",
        "same here, been grinding the daily missions every day",
        "yeah the community here is pretty active, good for tips",
        "makes sense. how long have you been mining?",
        "agreed, patience pays off with these apps",
        "thats the spirit, keep the sessions running",
    ],
}


def pick_reply(intent, st):
    pool = REPLIES[intent]
    sent = set(st["sent_contents"][-MAX_HISTORY:])
    fresh = [r for r in pool if r not in sent]
    if not fresh:
        fresh = pool
    return random.choice(fresh)


# ─── ambil pesan baru ───

def fetch_new_messages(st):
    c = U.SupabaseClient()
    q = (f"room_messages?room_id=eq.{ROOM_ID}"
         f"&select=id,user_id,username,content,created_at,reply_to_id"
         f"&order=created_at.desc&limit=30")
    r = c.session.get(f"{U.SUPABASE_URL}/rest/v1/{q}", timeout=30)
    if r.status_code != 200:
        log(f"fetch gagal: {r.status_code} {r.text[:120]}")
        return []
    msgs = r.json()
    # filter: bukan pesan sendiri, belum pernah di-reply
    replied = set(st["replied_ids"][-200:])
    fresh = [m for m in msgs
             if m["user_id"] != MY_USER_ID and m["id"] not in replied]
    fresh.reverse()  # terlama dulu biar urut wajar
    return fresh


def send_reply(content, reply_to):
    c = U.SupabaseClient()
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

    while True:
        try:
            now = time.time()
            wait = COOLDOWN - (now - st["last_send_ts"])
            if wait > 0:
                time.sleep(min(wait, POLL))
                continue

            fresh = fetch_new_messages(st)
            if not fresh:
                time.sleep(POLL)
                continue

            # pilih pesan: utamakan yang bertanya/soal app, fallback terbaru
            target = None
            for m in fresh:
                if classify(m["content"]) in ("app", "question"):
                    target = m
                    break
            if target is None:
                target = fresh[-1]

            intent = classify(target["content"])
            reply = pick_reply(intent, st)

            code, res = send_reply(reply, target["id"])
            if code == 200 and res.get("ok"):
                xp = res.get("xp_delta") or 0
                awarded = res.get("awarded", False)
                st["total_chats"] += 1
                st["total_xp"] += xp
                st["last_send_ts"] = time.time()
                st["sent_contents"].append(reply)
                st["replied_ids"].append(target["id"])
                st["sent_contents"] = st["sent_contents"][-MAX_HISTORY:]
                st["replied_ids"] = st["replied_ids"][-200:]
                save_state(st)
                log(f"CHAT #{st['total_chats']} -> {target['username'][:15]}: "
                    f"\"{target['content'][:40]}\" | balas: \"{reply[:50]}\" "
                    f"| xp+{xp} awarded={awarded} total_xp={st['total_xp']}")
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
