"""Put bookmarks on shelves with app.categorize, using the shelves in the database.

By default only bookmarks with no shelf are categorized. --redo also re-categorizes the ones
the AI shelved; items you shelved yourself, or that the weekly digest shelved, are never
touched. --dry-run prints what would happen without saving.

    .venv/bin/python scripts/categorize_backfill.py --dry-run
    .venv/bin/python scripts/categorize_backfill.py [--redo] [--limit N]
"""
import argparse
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")  # before app modules read DATABASE_URL / ANTHROPIC_API_KEY

from app import db, shelves  # noqa: E402


def backfill(redo: bool = False, limit: int | None = None, dry_run: bool = False) -> Counter:
    """Categorize the candidates and return how many landed on each shelf name."""
    current = db.list_categories()
    names = {s["id"]: s["name"] for s in current}
    items = db.backfill_candidates(redo=redo, limit=limit)
    print(f"{len(items)} item(s) to categorize{' (dry run: nothing is saved)' if dry_run else ''}")
    counts: Counter = Counter()
    for item in items:
        cid = shelves.choose_shelf(item, current)
        was = item.get("category")
        if not dry_run and not db.set_ai_category(item["id"], cid):
            print(f"  #{item['id']}: skipped (shelved by someone else meanwhile)")
            continue
        counts[names[cid]] += 1
        change = f"{was} -> {names[cid]}" if was and was != names[cid] else names[cid]
        print(f"  #{item['id']}: {change}  {(item.get('title') or item['url'])[:70]}")
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print what would happen without saving")
    parser.add_argument("--redo", action="store_true", help="also re-categorize items the AI already shelved")
    parser.add_argument("--limit", type=int, metavar="N", help="categorize at most N items")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")

    db.init(recover_pending=False)
    counts = backfill(redo=args.redo, limit=args.limit, dry_run=args.dry_run)
    print("\nPer shelf" + (" (would be)" if args.dry_run else "") + ":")
    for s in db.list_categories():
        print(f"  {s['name']:<30} {counts.get(s['name'], 0):>4}")
    print(f"  {'Total':<30} {sum(counts.values()):>4}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
