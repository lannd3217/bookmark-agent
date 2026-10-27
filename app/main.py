import asyncio
import logging
import os
from datetime import datetime
from typing import Annotated, Literal

import anyio
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from pathlib import Path
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field, StringConstraints

# Before importing agent, whose Anthropic client reads ANTHROPIC_API_KEY when created.
load_dotenv(Path(__file__).parent.parent / ".env")

import anthropic
from . import agent, auth, db, shelves, study_notes, summaries
from .claude_log import instrument
from .extract import fetch_page
from .importers.facebook import parse_facebook_saves
from .platforms import detect_platform, normalize_url

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
instrument(agent.client)

app = FastAPI(title="Bookmark Agent")
db.require_database_on_vercel()
# On Vercel, instances start and stop all the time and pending rows wait for the
# process-pending cron, so skip the restart cleanup there.
db.init(recover_pending=not db.on_vercel())
if problem := auth.api_token_problem():
    log.error(problem)

ROOT = Path(__file__).parent.parent
STATIC = ROOT / "static"
DEFAULT_FACEBOOK_EXPORT = "data/facebook/saved_items.json"
EXTRACT_TIMEOUT = 10.0      # seconds, for inline (Vercel) and cron extraction
PENDING_BATCH = 20          # items per process-pending run
PENDING_CONCURRENCY = 5

class SaveIn(BaseModel):
    url: str
    note: str | None = None
    title: str | None = Field(None, max_length=1000)
    saved_at: datetime | None = None

class ImportIn(BaseModel):
    path: str = DEFAULT_FACEBOOK_EXPORT

class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=500)

class LoginIn(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=1000)

ShelfName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]
ShelfDescription = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]

class CategoryIn(BaseModel):
    name: ShelfName
    description: ShelfDescription = ""

class CategoryPatch(BaseModel):
    name: ShelfName | None = None
    description: ShelfDescription | None = None
    position: int | None = Field(None, ge=0)

class BookmarkPatch(BaseModel):
    category_id: int

class SummarizeIn(BaseModel):
    regenerate: bool = False

class StudyNoteIn(BaseModel):
    topic: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    bookmark_ids: list[int] = Field(min_length=1)
    summarize_first: bool = False

class StudyStepIn(BaseModel):
    regenerate: bool = False

class StudyBulkDeleteIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=100)

class StudyDraftIn(BaseModel):
    text: str = Field(max_length=study_notes.ANSWER_MAX)

class StudyAnswerIn(BaseModel):
    answer: Annotated[str, StringConstraints(strip_whitespace=True, min_length=study_notes.ANSWER_MIN,
                                             max_length=study_notes.ANSWER_MAX)]

PUBLIC_PATHS = {"/login", "/health"}
CRON_PREFIX = "/api/cron/"

def _unauthorized() -> JSONResponse:
    return JSONResponse({"detail": "Not logged in"}, status_code=401)

@app.middleware("http")
async def require_login(request: Request, call_next):
    # Everything except PUBLIC_PATHS needs a session, so new routes are protected by default.
    path = request.url.path
    if path in PUBLIC_PATHS:
        return await call_next(request)
    header = request.headers.get("authorization")
    if path.startswith(CRON_PREFIX):
        # Cron routes take CRON_SECRET only: no session, no API token, even with AUTH_DISABLED.
        return await call_next(request) if auth.cron_authorized(header) else _unauthorized()
    if auth.auth_disabled():
        return await call_next(request)
    if path.startswith("/api/") and header is not None:
        # A bearer token must be API_TOKEN; a wrong one doesn't fall back to the session.
        return await call_next(request) if auth.api_token_authorized(header) else _unauthorized()
    try:
        ok, refreshed = await auth.check_session(request.cookies)
    except auth.NotConfigured:
        log.error("Login is not configured: set SUPABASE_URL, SUPABASE_ANON_KEY and ALLOWED_EMAIL")
        return JSONResponse({"detail": "Login is not configured on the server"}, status_code=503)
    if not ok:
        if path.startswith("/api/"):
            response = _unauthorized()
        else:
            response = RedirectResponse("/login", status_code=303)
        auth.clear_session_cookies(response)
        return response
    response = await call_next(request)
    if refreshed:
        auth.set_session_cookies(response, refreshed, request.url.hostname)
    return response

async def run_extraction(bid: int, url: str, timeout: float | None = None):
    # Sets the bookmark ready or failed; must never raise.
    try:
        try:
            data = await asyncio.wait_for(fetch_page(url), timeout)
        except TimeoutError:
            db.set_failed(bid, f"Extraction timed out after {timeout:g}s")
        except Exception as exc:
            db.set_failed(bid, str(exc) or type(exc).__name__)
        else:
            db.update_extracted(bid, {**data, "platform": detect_platform(url)})
            # categorize() is a blocking API call; shelve() never raises.
            await anyio.to_thread.run_sync(shelves.shelve, bid)
    except Exception:
        log.exception("Extraction bookkeeping failed for bookmark %s", bid)

@app.get("/")
def hello():
    return FileResponse(STATIC / "index.html")

@app.get("/library")
def library_page():
    return FileResponse(STATIC / "library.html")

@app.get("/study")
def study_list_page():
    return FileResponse(STATIC / "study.html")

@app.get("/study/{nid}")
def study_note_page(nid: int):
    return FileResponse(STATIC / "study.html")

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/login")
def login_page():
    return FileResponse(STATIC / "login.html")

@app.post("/login")
async def login(inp: LoginIn, request: Request):
    try:
        session = await auth.sign_in(inp.email, inp.password)
    except auth.NotConfigured:
        raise HTTPException(status_code=503, detail="Login is not configured on the server")
    except auth.AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    response = JSONResponse({"ok": True})
    auth.set_session_cookies(response, session, request.url.hostname)
    return response

@app.post("/logout")
def logout():
    response = JSONResponse({"ok": True})
    auth.clear_session_cookies(response)
    return response

@app.post("/api/bookmarks")
def save(inp: SaveIn, background: BackgroundTasks):
    url = normalize_url(inp.url.strip())
    if not url.startswith(("https://", "http://")):
        raise HTTPException(status_code=400, detail="URL must start with http:// or https://")
    saved_at = inp.saved_at.isoformat() if inp.saved_at else None
    bid, _ = _add_bookmark(url, inp.note, background, inp.title, saved_at)
    return db.get(bid)

def _add_bookmark(url: str, note: str | None, background: BackgroundTasks,
                  title: str | None = None, created_at: str | None = None,
                  extract: bool = True) -> tuple[int, bool]:
    """Insert a normalized URL as pending and, if it's new and `extract`, extract it."""
    bid, created = db.insert(url, note, detect_platform(url), title, created_at)
    if created and extract:
        _extract(bid, url, background)
    return bid, created

def _extract(bid: int, url: str, background: BackgroundTasks):
    """Locally: after the response. On Vercel, work after the response isn't guaranteed to
    run, so extract now (from this sync route's worker thread) with a timeout."""
    if db.on_vercel():
        anyio.from_thread.run(run_extraction, bid, url, EXTRACT_TIMEOUT)
    else:
        background.add_task(run_extraction, bid, url)

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
        # On Vercel, imports stay pending for the process-pending cron.
        _, created = _add_bookmark(url, None, background, item["title"], item["created_at"],
                                   extract=not db.on_vercel())
        added += created
    return {"found": len(items), "added": added, "skipped": len(items) - added}

@app.get("/api/bookmarks")
def list_bookmarks(q: str | None = None, platform: str | None = None, category_id: int | None = None,
                   kind: Literal["paper", "post", "article"] | None = None,
                   sort: Literal["newest", "oldest", "title"] | None = None):
    if not (q or platform or category_id is not None or kind or sort):
        return db.list_all()    # newest saved first, for the home page
    return db.list_bookmarks(q=q, platform=platform, category_id=category_id, kind=kind, sort=sort or "newest")

@app.get("/api/bookmarks/{bid}")
def get_bookmark(bid: int):
    """One bookmark, including its AI summary (lists only say has_summary)."""
    row = db.get(bid)
    if not row:
        raise HTTPException(status_code=404, detail="Bookmark not found")
    return row

@app.post("/api/bookmarks/{bid}/summarize")
def summarize_bookmark(bid: int, inp: SummarizeIn | None = None):
    """The saved summary, or a new one (also when {"regenerate": true})."""
    try:
        row = summaries.summarize_item(bid, regenerate=bool(inp and inp.regenerate))
    except summaries.RateLimited as exc:
        raise HTTPException(status_code=429, detail=str(exc), headers={"Retry-After": str(exc.retry_after)})
    except summaries.SummaryError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))
    if row is None:
        raise HTTPException(status_code=404, detail="Bookmark not found")
    return row

@app.patch("/api/bookmarks/{bid}")
def update_bookmark(bid: int, inp: BookmarkPatch):
    if not db.get(bid):
        raise HTTPException(status_code=404, detail="Bookmark not found")
    try:
        db.set_category(bid, inp.category_id, "user")
    except db.ShelfError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return db.get(bid)

# --- study notes -----------------------------------------------------------------

def _study_call(fn, *args):
    """Run a study_notes call, mapping its errors to HTTP and None to 404."""
    try:
        result = fn(*args)
    except study_notes.RateLimited as exc:
        raise HTTPException(status_code=429, detail=str(exc), headers={"Retry-After": str(exc.retry_after)})
    except study_notes.StudyError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))
    if result is None:
        raise HTTPException(status_code=404, detail="Study note not found")
    return result

@app.get("/api/study")
def list_study_notes():
    return {"notes": study_notes.list_notes()}

@app.post("/api/study", status_code=201)
def create_study_note(inp: StudyNoteIn):
    nid = _study_call(study_notes.create_note, inp.topic, inp.bookmark_ids, inp.summarize_first)
    return study_notes.view(db.get_study_note(nid))

@app.get("/api/study/{nid}")
def get_study_note(nid: int):
    note = db.get_study_note(nid)
    if not note:
        raise HTTPException(status_code=404, detail="Study note not found")
    return study_notes.view(note)

@app.post("/api/study/{nid}/sources-ready")
def study_sources_ready(nid: int):
    """The page finished (or skipped) summarizing the note's sources; step 1 can start."""
    if not db.mark_study_sources_ready(nid):
        raise HTTPException(status_code=404, detail="Study note not found")
    return study_notes.view(db.get_study_note(nid))

@app.post("/api/study/bulk-delete")
def bulk_delete_study_notes(inp: StudyBulkDeleteIn):
    """Delete several notes; each in its own transaction, so one failure never stops the rest.
    Returns which were deleted, which didn't exist, and which failed (with why)."""
    deleted, not_found, failed = [], [], []
    for nid in dict.fromkeys(inp.ids):
        try:
            (deleted if db.delete_study_note(nid) else not_found).append(nid)
        except Exception as exc:
            log.warning("Deleting study note %s failed: %s: %s", nid, type(exc).__name__, exc)
            failed.append({"id": nid, "detail": "Couldn't delete it; try again."})
    return {"deleted": deleted, "not_found": not_found, "failed": failed}

@app.delete("/api/study/{nid}")
def delete_study_note(nid: int):
    if not db.delete_study_note(nid):
        raise HTTPException(status_code=404, detail="Study note not found")
    return {"ok": True}

@app.post("/api/study/{nid}/steps/{n}")
def run_study_step(nid: int, n: Annotated[int, Field(ge=1, le=3)], inp: StudyStepIn | None = None):
    """Steps 1-3 (mental models, debates, questions). Returns the saved result unless regenerate."""
    return _study_call(study_notes.run_step, nid, n, bool(inp and inp.regenerate))

@app.put("/api/study/{nid}/drafts/{q}")
def save_study_draft(nid: int, q: Annotated[int, Field(ge=1)], inp: StudyDraftIn):
    """Save an unsent answer draft for question q (blank deletes it). Not an attempt; no AI call."""
    return _study_call(study_notes.save_draft, nid, q, inp.text)

@app.post("/api/study/{nid}/answers/{q}")
def answer_study_question(nid: int, q: Annotated[int, Field(ge=1)], inp: StudyAnswerIn):
    """Step 4: check an answer to question q and save the feedback."""
    return _study_call(study_notes.answer, nid, q, inp.answer)

# --- shelves ------------------------------------------------------------------

def _shelf_error(exc: Exception) -> HTTPException:
    if isinstance(exc, db.ShelfNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, db.DuplicateShelf):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))

def _shelf_out(shelf: dict) -> dict:
    return {k: shelf.get(k) for k in ("id", "name", "description", "count", "is_fallback")}

@app.get("/api/categories")
def list_categories():
    totals = db.item_totals()
    return {"all": totals["all"], "unsorted": totals["unsorted"],
            "categories": [_shelf_out(s) for s in db.list_categories()]}

def _category(cid: int) -> dict:
    """A shelf as listed (with its count), for responses."""
    return _shelf_out(next(s for s in db.list_categories() if s["id"] == cid))

@app.post("/api/categories", status_code=201)
def create_category(inp: CategoryIn):
    try:
        shelf = db.create_category(inp.name, inp.description)
    except (db.ShelfError, db.ShelfNotFound) as exc:
        raise _shelf_error(exc)
    return _category(shelf["id"])

@app.patch("/api/categories/{cid}")
def update_category(cid: int, inp: CategoryPatch):
    if inp.name is None and inp.description is None and inp.position is None:
        raise HTTPException(status_code=422, detail="Nothing to change: send name, description or position")
    try:
        db.update_category(cid, inp.name, inp.description, inp.position)
    except (db.ShelfError, db.ShelfNotFound) as exc:
        raise _shelf_error(exc)
    return _category(cid)

@app.delete("/api/categories/{cid}")
def delete_category(cid: int, move_to: int | None = None):
    try:
        result = db.delete_category(cid, move_to)
    except (db.ShelfError, db.ShelfNotFound) as exc:
        raise _shelf_error(exc)
    return {"ok": True, "moved": result["moved"], "moved_to": _category(result["moved_to"]["id"])}

@app.post("/api/categories/{cid}/resort")
def resort_category(cid: int):
    """Re-categorize up to 25 items the AI left on "Other", now that shelf `cid` exists."""
    if not db.get_category(cid):
        raise HTTPException(status_code=404, detail=f"No shelf with id {cid}")
    result = shelves.resort_other()
    return {"checked": result["checked"], "moved": result["moved"],
            "moved_here": result["by_shelf"].get(cid, 0)}

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
    _extract(bid, row["url"], background)
    return db.get(bid)

@app.delete("/api/bookmarks/{bid}")
def delete_bookmark(bid: int):
    db.delete(bid)
    return {"ok": True}

# --- cron (Vercel calls these with Authorization: Bearer <CRON_SECRET>) ----------

@app.get("/api/cron/process-pending")
async def cron_process_pending():
    rows = db.list_pending(PENDING_BATCH)
    limit = asyncio.Semaphore(PENDING_CONCURRENCY)

    async def one(row):
        async with limit:
            await run_extraction(row["id"], row["url"], EXTRACT_TIMEOUT)

    await asyncio.gather(*(one(r) for r in rows))
    statuses = [(db.get(r["id"]) or {}).get("status") for r in rows]
    result = {"processed": len(rows), "ready": statuses.count("ready"), "failed": statuses.count("failed")}
    log.info("process-pending: %s", result)
    return result

@app.get("/api/cron/weekly-digest")
def cron_weekly_digest():
    from .jobs import weekly_digest
    code = weekly_digest.main([])
    if code != 0:
        raise HTTPException(status_code=500, detail=f"Weekly digest failed (exit code {code}); see the function logs")
    return {"ok": True}
