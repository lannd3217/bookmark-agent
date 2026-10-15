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

def init(recover_pending: bool = True):
    """Create/migrate tables. recover_pending=False skips the restart cleanup (for other processes)."""
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
        c.execute("""
        CREATE TABLE IF NOT EXISTS digests(
          id INTEGER PRIMARY KEY,
          watchlist TEXT NOT NULL,
          run_at TEXT NOT NULL DEFAULT (datetime('now')),
          ranked_by TEXT
        )""")
        if "ranked_by" not in {r["name"] for r in c.execute("PRAGMA table_info(digests)")}:
            c.execute("ALTER TABLE digests ADD COLUMN ranked_by TEXT")
        c.execute("""
        CREATE TABLE IF NOT EXISTS digest_items(
          digest_id INTEGER NOT NULL REFERENCES digests(id),
          bookmark_id INTEGER NOT NULL REFERENCES bookmarks(id),
          rank INTEGER NOT NULL,
          why TEXT,
          PRIMARY KEY (digest_id, bookmark_id)
        )""")
        if not recover_pending:
            return
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

SEARCH_FIELDS = ("title", "url", "author", "note", "text")

def _like_pattern(word: str) -> str:
    escaped = word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"

def search(query: str, limit: int = 20, platform: str | None = None) -> list[dict]:
    """Every word must appear (case-insensitive for ASCII) in at least one text field. Newest first."""
    clauses, params = [], []
    for word in query.split():
        clauses.append("(" + " OR ".join(f"{f} LIKE ? ESCAPE '\\'" for f in SEARCH_FIELDS) + ")")
        params.extend([_like_pattern(word)] * len(SEARCH_FIELDS))
    if platform:
        clauses.append("platform = ?")
        params.append(platform)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(max(int(limit), 0))
    with conn() as c:
        return [dict(r) for r in c.execute(
            f"SELECT * FROM bookmarks {where} ORDER BY created_at DESC, id DESC LIMIT ?", params
        )]

def save_digest(watchlist: str, picks: list[dict],
                ranked_by: str | None = None) -> tuple[int, list[tuple[int, bool]]]:
    """Save a digest in one transaction. ranked_by: "ai", "prescore" or "prescore_fallback".

    Each pick has url, platform, title, author, text, note, rank, why. A pick whose URL is
    already bookmarked reuses that row unchanged (its note is kept). Returns
    (digest_id, [(bookmark_id, created), ...]) in pick order.
    """
    saved = []
    with conn() as c:
        digest_id = c.execute(
            "INSERT INTO digests(watchlist, ranked_by) VALUES (?, ?) RETURNING id", (watchlist, ranked_by)
        ).fetchone()[0]
        for p in picks:
            row = c.execute(
                """INSERT INTO bookmarks(url, platform, title, author, text, note, status)
                   VALUES (?, ?, ?, ?, ?, ?, 'ready')
                   ON CONFLICT(url) DO NOTHING
                   RETURNING id""",
                (p["url"], p["platform"], p["title"], p["author"], p["text"], p["note"]),
            ).fetchone()
            created = row is not None
            bid = row[0] if created else c.execute(
                "SELECT id FROM bookmarks WHERE url = ?", (p["url"],)
            ).fetchone()[0]
            c.execute(
                """INSERT INTO digest_items(digest_id, bookmark_id, rank, why) VALUES (?, ?, ?, ?)
                   ON CONFLICT(digest_id, bookmark_id) DO NOTHING""",
                (digest_id, bid, p["rank"], p["why"]),
            )
            saved.append((bid, created))
    return digest_id, saved

def latest_digests(watchlist: str | None = None) -> list[dict]:
    """The newest digest per watchlist (or just for `watchlist`), newest first, with ranked items."""
    with conn() as c:
        digests = [dict(r) for r in c.execute(
            """SELECT * FROM digests d
               WHERE d.id = (SELECT MAX(id) FROM digests WHERE watchlist = d.watchlist)
                 AND (? IS NULL OR d.watchlist = ?)
               ORDER BY d.run_at DESC, d.id DESC""",
            (watchlist, watchlist),
        )]
        for d in digests:
            d["items"] = [dict(r) for r in c.execute(
                """SELECT di.rank, di.why, b.id, b.title, b.url, b.author
                   FROM digest_items di JOIN bookmarks b ON b.id = di.bookmark_id
                   WHERE di.digest_id = ?
                   ORDER BY di.rank""",
                (d["id"],),
            )]
    return digests
