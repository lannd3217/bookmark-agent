import logging

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pathlib import Path
from fastapi.responses import FileResponse
from pydantic import BaseModel
from . import db
from .extract import fetch_page
from .importers.facebook import parse_facebook_saves
from .platforms import detect_platform, normalize_url

log = logging.getLogger(__name__)

app = FastAPI(title="Bookmark Agent")
db.init()

ROOT = Path(__file__).parent.parent
STATIC = ROOT / "static"
DEFAULT_FACEBOOK_EXPORT = "data/facebook/saved_items.json"

class SaveIn(BaseModel):
    url: str
    note: str | None = None

class ImportIn(BaseModel):
    path: str = DEFAULT_FACEBOOK_EXPORT

async def run_extraction(bid: int, url: str):
    # Runs after the response is sent; must never raise.
    try:
        try:
            data = await fetch_page(url)
        except Exception as exc:
            db.set_failed(bid, str(exc) or type(exc).__name__)
        else:
            db.update_extracted(bid, {**data, "platform": detect_platform(url)})
    except Exception:
        log.exception("Extraction bookkeeping failed for bookmark %s", bid)

@app.get("/")
def hello():
    return FileResponse(STATIC / "index.html")

@app.post("/api/bookmarks")
def save(inp: SaveIn, background: BackgroundTasks):
    url = normalize_url(inp.url.strip())
    if not url.startswith(("https://", "http://")):
        raise HTTPException(status_code=400, detail="URL must start with http:// or https://")
    bid, _ = _add_bookmark(url, inp.note, background)
    return db.get(bid)

def _add_bookmark(url: str, note: str | None, background: BackgroundTasks,
                  title: str | None = None, created_at: str | None = None) -> tuple[int, bool]:
    """Insert a normalized URL as pending and queue extraction if it's new."""
    bid, created = db.insert(url, note, detect_platform(url), title, created_at)
    if created:
        background.add_task(run_extraction, bid, url)
    return bid, created

@app.post("/api/import/facebook")
def import_facebook(background: BackgroundTasks, inp: ImportIn | None = None):
    path = Path((inp or ImportIn()).path).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    try:
        items = parse_facebook_saves(path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    added = 0
    for item in items:
        url = item["url"]
        # Facebook exports can contain non-web URIs (e.g. local media paths); skip those.
        if not url.startswith(("https://", "http://")) or db.url_exists(url):
            continue
        _, created = _add_bookmark(url, None, background, item["title"], item["created_at"])
        added += created
    return {"found": len(items), "added": added, "skipped": len(items) - added}

@app.get("/api/bookmarks")
def list_bookmarks():
    return db.list_all()

@app.post("/api/bookmarks/{bid}/retry")
def retry_bookmark(bid: int, background: BackgroundTasks):
    row = db.get(bid)
    if not row:
        raise HTTPException(status_code=404, detail="Bookmark not found")
    if not db.set_pending(bid):
        raise HTTPException(status_code=409, detail=f"Only failed bookmarks can be retried (status is {row['status']})")
    background.add_task(run_extraction, bid, row["url"])
    return db.get(bid)

@app.delete("/api/bookmarks/{bid}")
def delete_bookmark(bid: int):
    db.delete(bid)
    return {"ok": True}
