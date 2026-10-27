"""Probe room chat UORM: list rooms, room_members, room_messages structure."""
import json
import sys

sys.path.insert(0, "uorm-mine")
import uorm_mine as U  # noqa: E402

c = U.SupabaseClient()


def get(path):
    r = c.session.get(f"{U.SUPABASE_URL}/rest/v1/{path}", timeout=30)
    print(f"--- {path} -> {r.status_code}")
    try:
        return r.json()
    except Exception:
        print(r.text[:300])
        return None


def main():
    print("== rooms ==")
    print(json.dumps(get("rooms?select=*&limit=5"), indent=1)[:2000])
    print("\n== room_members ==")
    print(json.dumps(get("room_members?select=*&limit=10"), indent=1)[:1500])
    print("\n== room_messages (latest 10) ==")
    print(json.dumps(get("room_messages?select=*&order=created_at.desc&limit=10"),
                     indent=1)[:2500])


if __name__ == "__main__":
    main()
