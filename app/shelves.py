"""Choosing a shelf for a bookmark with app.categorize, using the shelves in the database."""
import logging
from concurrent.futures import ThreadPoolExecutor

from . import categorize as categorizer
from . import db

log = logging.getLogger(__name__)
RESORT_LIMIT = 25
RESORT_WORKERS = 5


def choose_shelf(item: dict, shelves: list[dict]) -> int:
    """The id of the shelf categorize() picks for `item`; "Other" if it fails or picks no known shelf.

    `shelves` is db.list_categories() (or rows with id, name, description). Never raises.
    """
    fallback = next(s["id"] for s in shelves if s["name"].casefold() == db.FALLBACK.casefold())
    try:
        menu = [{"name": s["name"], "description": s["description"]} for s in shelves]
        name = categorizer.categorize(item, categories=menu)
    except Exception:
        log.exception("Categorizing bookmark %s failed; using %s", item.get("id"), db.FALLBACK)
        return fallback
    return next((s["id"] for s in shelves if s["name"] == name), fallback)


def shelve(bid: int) -> int | None:
    """Categorize a saved bookmark and store the result as the AI's choice.

    Leaves bookmarks the user or the digest shelved alone. Returns the shelf id, or None if
    the bookmark is gone or was skipped. Never raises, so it can't fail a save.
    """
    try:
        item = db.get(bid)
        if not item or item.get("category_set_by") in ("user", "digest"):
            return None
        cid = choose_shelf(item, db.list_categories())
        return cid if db.set_ai_category(bid, cid) else None
    except Exception:
        log.exception("Shelving bookmark %s failed", bid)
        try:
            db.set_ai_category(bid, db.fallback_category()["id"])
        except Exception:
            log.exception("Couldn't put bookmark %s on %s either", bid, db.FALLBACK)
        return None


def resort_other(limit: int = RESORT_LIMIT) -> dict:
    """Re-categorize up to `limit` items the AI left on "Other", against the current shelves.

    Returns {"checked": n, "moved": n, "by_shelf": {shelf_id: n}}. Items the user or the
    digest shelved are never candidates, and an item moved meanwhile is left where it is.
    """
    items = db.resort_candidates(limit)
    current = db.list_categories()
    other = next(s["id"] for s in current if s["is_fallback"])
    with ThreadPoolExecutor(RESORT_WORKERS) as pool:
        picks = list(pool.map(lambda it: choose_shelf(it, current), items))
    by_shelf: dict[int, int] = {}
    for it, cid in zip(items, picks):
        if cid != other and db.set_ai_category(it["id"], cid, from_cid=other):
            by_shelf[cid] = by_shelf.get(cid, 0) + 1
    return {"checked": len(items), "moved": sum(by_shelf.values()), "by_shelf": by_shelf}
