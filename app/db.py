import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "bookmarks.db"

def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row     # rows behave like dicts
    return c

def init():
    with conn() as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS bookmarks(
          id INTEGER PRIMARY KEY,
          url TEXT UNIQUE NOT NULL,
          note TEXT,
          created_at TEXT DEFAULT (datetime('now'))
        )""")
def insert(url: str, note: str | None) -> int:
    with conn() as c:
        return c.execute(
            """INSERT INTO bookmarks(url, note) VALUES (?, ?)
               ON CONFLICT(url) DO UPDATE SET note = COALESCE(excluded.note, note)
               RETURNING id""",
            (url, note),
        ).fetchone()[0]

def get(bid: int):
    with conn() as c:
        r = c.execute("SELECT * FROM bookmarks WHERE id = ?", (bid,)).fetchone()
        return dict(r) if r else None

def list_all():
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM bookmarks ORDER BY id DESC")]

def delete(bid: int):
    with conn() as c:
        c.execute("DELETE FROM bookmarks WHERE id = ?", (bid,))

