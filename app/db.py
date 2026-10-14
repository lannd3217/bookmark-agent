import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "bookmarks.db"

# Columns added after the original (id, url, note, created_at) table.
NEW_COLUMNS = {
    "platform": "TEXT",
    "title": "TEXT",
    "author": "TEXT",
    "text": "TEXT",
    "image": "TEXT",
    "status": "TEXT NOT NULL DEFAULT 'pending'",
    "error": "TEXT",
}

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
          created_at TEXT DEFAULT (datetime('now')),
          platform TEXT,
          title TEXT,
          author TEXT,
          text TEXT,
          image TEXT,
          status TEXT NOT NULL DEFAULT 'pending',
          error TEXT
        )""")
        _migrate(c)
        # Background jobs don't survive a restart, so anything still pending
        # (including rows from before extraction existed) can never finish.
        # Mark them failed so they can be retried.
        c.execute(
            """UPDATE bookmarks
               SET status = 'failed', error = 'Extraction did not finish; retry to extract'
               WHERE status = 'pending'"""
        )

def _migrate(c):
    existing = {r["name"] for r in c.execute("PRAGMA table_info(bookmarks)")}
    for name, decl in NEW_COLUMNS.items():
        if name not in existing:
            c.execute(f"ALTER TABLE bookmarks ADD COLUMN {name} {decl}")

def insert(url: str, note: str | None, platform: str,
           title: str | None = None, created_at: str | None = None) -> tuple[int, bool]:
    """Insert a pending bookmark. Returns (id, created); an existing URL only gets its note updated.

    created_at may be any SQLite-parsable time (e.g. ISO 8601); it defaults to now.
    """
    with conn() as c:
        row = c.execute(
            """INSERT INTO bookmarks(url, note, platform, title, created_at)
               VALUES (?, ?, ?, ?, COALESCE(datetime(?), datetime('now')))
               ON CONFLICT(url) DO NOTHING
               RETURNING id""",
            (url, note, platform, title, created_at),
        ).fetchone()
        if row:
            return row[0], True
        return c.execute(
            """UPDATE bookmarks SET note = COALESCE(?, note) WHERE url = ?
               RETURNING id""",
            (note, url),
        ).fetchone()[0], False

def url_exists(url: str) -> bool:
    with conn() as c:
        return c.execute("SELECT 1 FROM bookmarks WHERE url = ?", (url,)).fetchone() is not None

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

def update_extracted(bid: int, data: dict):
    with conn() as c:
        c.execute(
            """UPDATE bookmarks
               SET title = COALESCE(?, title), author = ?, text = ?, image = ?, platform = COALESCE(?, platform),
                   status = 'ready', error = NULL
               WHERE id = ?""",
            (data.get("title"), data.get("author"), data.get("text"), data.get("image"),
             data.get("platform"), bid),
        )

def set_failed(bid: int, error: str):
    with conn() as c:
        c.execute(
            "UPDATE bookmarks SET status = 'failed', error = ? WHERE id = ?",
            (error, bid),
        )

def set_pending(bid: int) -> bool:
    """Move a failed bookmark back to pending. Returns False if it wasn't failed."""
    with conn() as c:
        cur = c.execute(
            "UPDATE bookmarks SET status = 'pending', error = NULL WHERE id = ? AND status = 'failed'",
            (bid,),
        )
        return cur.rowcount == 1
