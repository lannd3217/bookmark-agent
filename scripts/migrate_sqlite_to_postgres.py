"""Copy every row from the local SQLite bookmarks.db into the Postgres database at DATABASE_URL.

Ids and dates are kept. Rows that already exist in Postgres are skipped, so it is safe to
re-run. Runs in one transaction; --dry-run does the same work and then rolls it back.

    .venv/bin/python scripts/migrate_sqlite_to_postgres.py --dry-run
    .venv/bin/python scripts/migrate_sqlite_to_postgres.py [--sqlite path/to/bookmarks.db]
"""
import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from app import db  # noqa: E402

BOOKMARK_COLUMNS = ("id", "url", "note", "created_at", "platform", "title", "author",
                    "text", "image", "status", "error")
DIGEST_COLUMNS = ("id", "watchlist", "run_at", "ranked_by")
ITEM_COLUMNS = ("digest_id", "bookmark_id", "rank", "why")


def _insert(pg, table: str, columns: tuple, rows: list[dict]) -> int:
    sql = (f"INSERT INTO {table}({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))}) "
           "ON CONFLICT DO NOTHING")
    return sum(pg.execute(sql, tuple(r[c] for c in columns)).rowcount for r in rows)


def _mismatched(pg, table: str, rows: list[dict], same: tuple) -> set[int]:
    """Ids whose row in Postgres isn't the same record (different `same` fields, or missing)."""
    found = {r["id"]: r for r in pg.execute(f"SELECT id, {', '.join(same)} FROM {table}")}
    return {r["id"] for r in rows
            if r["id"] not in found or any(str(found[r["id"]][f]) != str(r[f]) for f in same)}


def migrate(sqlite_path: Path, dry_run: bool) -> dict:
    src = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    tables = {r["name"] for r in src.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    read = lambda table, order: (   # noqa: E731
        [dict(r) for r in src.execute(f"SELECT * FROM {table} ORDER BY {order}")] if table in tables else []
    )
    bookmarks = read("bookmarks", "id")
    digests = read("digests", "id")
    items = read("digest_items", "digest_id, bookmark_id")
    for b in bookmarks:     # rows from before a column existed
        for c in BOOKMARK_COLUMNS:
            b.setdefault(c, None)
        b["status"] = b["status"] or "pending"
    for d in digests:
        d.setdefault("ranked_by", None)
    src.close()

    counts = {}
    with db.conn() as pg:
        for stmt in db.PG_SCHEMA:
            pg.execute(stmt)
        added = _insert(pg, "bookmarks", BOOKMARK_COLUMNS, bookmarks)
        bad_bookmarks = _mismatched(pg, "bookmarks", bookmarks, ("url",))
        counts["bookmarks"] = (len(bookmarks), added, len(bad_bookmarks))

        added = _insert(pg, "digests", DIGEST_COLUMNS, digests)
        bad_digests = _mismatched(pg, "digests", digests, ("watchlist", "run_at"))
        counts["digests"] = (len(digests), added, len(bad_digests))

        # Items pointing at a conflicting bookmark or digest would attach to the wrong row.
        ok_items = [i for i in items if i["bookmark_id"] not in bad_bookmarks and i["digest_id"] not in bad_digests]
        added = _insert(pg, "digest_items", ITEM_COLUMNS, ok_items)
        counts["digest_items"] = (len(items), added, len(items) - len(ok_items))

        # Explicit ids don't advance the identity sequences; move them past the copied ids.
        for table in ("bookmarks", "digests"):
            pg.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                       f"COALESCE((SELECT MAX(id) FROM {table}), 0) + 1, false)")
        if dry_run:
            pg.rollback()
    return counts


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--sqlite", type=Path, default=db.DB_PATH, help=f"SQLite file (default {db.DB_PATH})")
    parser.add_argument("--dry-run", action="store_true", help="show what would be copied, write nothing")
    args = parser.parse_args(argv)

    load_dotenv(ROOT / ".env")
    if not db.is_postgres():
        print("DATABASE_URL is not set (environment or .env).", file=sys.stderr)
        return 2
    if not args.sqlite.exists():
        print(f"No SQLite database at {args.sqlite}", file=sys.stderr)
        return 2

    counts = migrate(args.sqlite, args.dry_run)
    verb = "would copy" if args.dry_run else "copied"
    print(f"{'DRY RUN: nothing was written. ' if args.dry_run else ''}{args.sqlite} -> Postgres")
    conflicts = 0
    for table, (total, added, conflicted) in counts.items():
        skipped = total - added - conflicted
        conflicts += conflicted
        print(f"  {table:<13} {total:>6} in SQLite, {verb} {added:>6}, skipped {skipped:>6} already present"
              + (f" ({conflicted} conflict: same id, different row)" if conflicted else ""))
    if conflicts:
        print("Conflicting rows were not copied; check them by id before re-running.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
