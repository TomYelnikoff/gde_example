"""Small admin helper until a payment system exists.

    python manage.py list-users
    python manage.py set-plan EMAIL free|paid
    python manage.py reset-usage EMAIL

Uses the same DB_PATH as the app (default: chat.db next to this file).
In Docker:  docker exec chatbot python manage.py list-users
"""
import os
import sqlite3
import sys

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "chat.db"))


def main(argv):
    conn = sqlite3.connect(DB_PATH)
    try:
        if argv[:1] == ["list-users"]:
            for row in conn.execute("SELECT email, plan, messages_used, created_at FROM users ORDER BY id"):
                print(f"{row[0]:40} {row[1]:5} used={row[2]:<4} created={row[3]}")
            return 0
        if len(argv) == 3 and argv[0] == "set-plan" and argv[2] in ("free", "paid"):
            cur = conn.execute("UPDATE users SET plan = ? WHERE email = ?", (argv[2], argv[1].lower()))
        elif len(argv) == 2 and argv[0] == "reset-usage":
            cur = conn.execute("UPDATE users SET messages_used = 0 WHERE email = ?", (argv[1].lower(),))
        else:
            print(__doc__)
            return 2
        conn.commit()
        print("updated" if cur.rowcount else "no such user")
        return 0 if cur.rowcount else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
