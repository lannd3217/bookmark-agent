import logging
import os
from datetime import datetime

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException
from pathlib import Path
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

# Before importing agent, whose Anthropic client reads ANTHROPIC_API_KEY when created.
load_dotenv(Path(__file__).parent.parent / ".env")

import anthropic
from . import agent, db
from .claude_log import instrument
from .extract import fetch_page
from .importers.facebook import parse_facebook_saves
from .platforms import detect_platform, normalize_url

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
instrument(agent.client)

app = FastAPI(title="Bookmark Agent")
db.init()

ROOT = Path(__file__).parent.parent
STATIC = ROOT / "static"
DEFAULT_FACEBOOK_EXPORT = "data/facebook/saved_items.json"

class SaveIn(BaseModel):
    url: str
    note: str | None = None
    title: str | None = Field(None, max_length=1000)
    saved_at: datetime | None = None

class ImportIn(BaseModel):
    path: str = DEFAULT_FACEBOOK_EXPORT

class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=500)

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
    saved_at = inp.saved_at.isoformat() if inp.saved_at else None
    bid, _ = _add_bookmark(url, inp.note, background, inp.title, saved_at)
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
def list_bookmarks(q: str | None = None, platform: str | None = None):
    if q or platform:
        return db.search(q or "", platform=platform)
    return db.list_all()

@app.get("/api/digests/latest")
def latest_digests(watchlist: str | None = None):
    return {"digests": db.latest_digests(watchlist)}

@app.post("/api/ask")
def ask(inp: AskIn):
    question = inp.question.strip()
    if not question:
        raise HTTPException(status_code=422, detail="Question must not be empty")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(status_code=503, detail="Ask is unavailable: ANTHROPIC_API_KEY is not set on the server")
    try:
        result = agent.ask(question)
    except anthropic.AuthenticationError:
        raise HTTPException(status_code=503, detail="Ask is unavailable: the Anthropic API key was rejected")
    except anthropic.APIError as exc:
        log.warning("Anthropic API error: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="The Anthropic API request failed; try again")
    items = []
    for bid in result.get("item_ids", []):
        row = db.get(bid)
        if row:
            items.append({k: row.get(k) for k in ("id", "title", "url", "platform")})
    return {"answer": result["answer"], "steps": result.get("steps"), "items": items}

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
