"""Token-protected MCP server over Streamable HTTP for bookmark-agent.

Lets an outside agent add to and read the library. Talks to the running web app
over HTTP only (it does not import app/) and binds to 127.0.0.1; expose it only
through an authenticated tunnel.

Run with:  .venv/bin/python mcp_http_server.py
Env (also read from .env): MCP_TOKEN (required, >= 32 chars), MCP_HTTP_PORT (8001),
BOOKMARKS_URL (http://127.0.0.1:8000), MCP_ALLOWED_HOSTS (tunnel hostnames, comma-separated).
"""
import asyncio
import hashlib
import logging
import os
import secrets
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from mcp_server import _line, _matches, _newest_first, SEARCH_LIMIT

HOST = "127.0.0.1"
ROOT = Path(__file__).parent
MIN_TOKEN_LENGTH = 32
TOKEN_COMMAND = "python -c 'import secrets; print(secrets.token_urlsafe(32))'"
TIMEOUT = 10.0
MAX_URL_LENGTH = 2048
MAX_TEXT_LENGTH = 1000
MAX_BATCH = 50
RATE_LIMIT = 120        # items
RATE_WINDOW = 60.0      # seconds

log = logging.getLogger("mcp_http")

mcp = MCPServer("bookmarks-remote")
READ_ONLY = ToolAnnotations(readOnlyHint=True)
ADDS = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True)


def _base_url() -> str:
    return os.environ.get("BOOKMARKS_URL", "http://127.0.0.1:8000").rstrip("/")


def _client() -> httpx.AsyncClient:
    # Tests replace this to talk to the web app in-process.
    return httpx.AsyncClient(base_url=_base_url(), timeout=TIMEOUT)


# --- web app calls -----------------------------------------------------------

async def _request(method: str, path: str, **kwargs) -> httpx.Response:
    # ToolError messages reach the model; other exceptions are hidden behind a generic error.
    try:
        async with _client() as client:
            return await client.request(method, path, **kwargs)
    except httpx.ConnectError as exc:
        raise ToolError(f"Can't reach the bookmarks app at {_base_url()}; is it running?") from exc
    except httpx.TimeoutException as exc:
        raise ToolError(f"The bookmarks app at {_base_url()} timed out") from exc


async def _fetch_bookmarks(params: dict | None = None) -> list[dict]:
    r = await _request("GET", "/api/bookmarks", params=params)
    if r.status_code != 200:
        raise ToolError(f"Bookmarks API returned HTTP {r.status_code}")
    try:
        rows = r.json()
    except ValueError:
        rows = None
    if not isinstance(rows, list):
        raise ToolError("Unexpected response from /api/bookmarks (expected a JSON list)")
    return rows


async def _save(body: dict) -> dict:
    r = await _request("POST", "/api/bookmarks", json=body)
    if r.status_code in (400, 422):
        raise ValueError("Rejected by the bookmarks app")
    if r.status_code != 200:
        raise ToolError(f"Bookmarks API returned HTTP {r.status_code}")
    return r.json()


# --- validation --------------------------------------------------------------

def _validate(raw: Any) -> dict:
    """Return a clean {url, title, note, saved_at} or raise ValueError with a reason."""
    if not isinstance(raw, dict):
        raise ValueError("Each item must be an object with at least a 'url'")
    url = raw.get("url")
    if not isinstance(url, str) or not url.strip():
        raise ValueError("'url' is required")
    url = url.strip()
    if len(url) > MAX_URL_LENGTH:
        raise ValueError(f"'url' is longer than {MAX_URL_LENGTH} characters")
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise ValueError("'url' must be an http:// or https:// link")

    clean = {"url": url}
    for field in ("title", "note"):
        value = raw.get(field)
        if value is None:
            continue
        if not isinstance(value, str):
            raise ValueError(f"'{field}' must be a string")
        if len(value) > MAX_TEXT_LENGTH:
            raise ValueError(f"'{field}' is longer than {MAX_TEXT_LENGTH} characters")
        clean[field] = value

    saved_at = raw.get("saved_at")
    if saved_at is not None:
        if not isinstance(saved_at, str):
            raise ValueError("'saved_at' must be an ISO 8601 date string")
        try:
            dt = datetime.fromisoformat(saved_at.strip().replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("'saved_at' must be an ISO 8601 date, e.g. 2024-05-01 or 2024-05-01T10:00:00Z")
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        clean["saved_at"] = dt.isoformat()
    return clean


# --- rate limiting -----------------------------------------------------------

class RateLimiter:
    """Sliding-window item counter per key."""

    def __init__(self, limit: int = RATE_LIMIT, window: float = RATE_WINDOW):
        self.limit, self.window = limit, window
        self._hits: dict[str, deque] = {}

    def take(self, key: str, n: int) -> None:
        now = time.monotonic()
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] >= self.window:
            hits.popleft()
        if len(hits) + n > self.limit:
            retry = int(self.window - (now - hits[0])) + 1 if hits else int(self.window)
            raise ToolError(
                f"Rate limit exceeded: at most {self.limit} items per minute "
                f"({len(hits)} used, {n} requested). Try again in {retry} seconds."
            )
        hits.extend([now] * n)


limiter = RateLimiter()
_token_key = ""          # hash of MCP_TOKEN, set in create_app; never the token itself
_add_lock = asyncio.Lock()


# --- adding ------------------------------------------------------------------

def _item_view(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "url": row.get("url"),
        "title": row.get("title"),
        "platform": row.get("platform"),
        "saved_at": row.get("created_at"),
        "note": row.get("note"),
        "extraction_status": row.get("status"),
    }


async def _add_one(item: dict, known_ids: set[int]) -> dict:
    # The note is only sent once we know the URL is new: re-saving an existing
    # URL would overwrite its note, and this server must not edit items.
    body = {k: item[k] for k in ("url", "title", "saved_at") if k in item}
    row = await _save(body)
    if row["id"] in known_ids:
        return {"status": "exists", "item": _item_view(row),
                **({"note_applied": False} if "note" in item else {})}
    known_ids.add(row["id"])
    if "note" in item:
        row = await _save({"url": row["url"], "note": item["note"]})
    return {"status": "added", "item": _item_view(row)}


async def _add_many(raw_items: list) -> list[dict]:
    validated: list[dict | Exception] = []
    for raw in raw_items:
        try:
            validated.append(_validate(raw))
        except ValueError as exc:
            validated.append(exc)

    results: list[dict] = []
    async with _add_lock:
        known_ids = {b["id"] for b in await _fetch_bookmarks()}
        for v in validated:
            if isinstance(v, Exception):
                results.append({"status": "invalid", "error": str(v)})
                continue
            try:
                results.append(await _add_one(v, known_ids))
            except ValueError as exc:
                results.append({"status": "invalid", "error": str(exc)})
    return results


def _totals(results: list[dict]) -> dict:
    return {s: sum(r["status"] == s for r in results) for s in ("added", "exists", "invalid")}


def _log_call(tool: str, **counts) -> None:
    log.info("tool=%s %s", tool, " ".join(f"{k}={v}" for k, v in counts.items()))


@mcp.tool(annotations=ADDS)
async def library_add_item(
    url: str, title: str | None = None, note: str | None = None, saved_at: str | None = None
) -> dict:
    """Save one link to the user's bookmark library.

    Use this when the user asks you to save, bookmark, or add a single link.
    `url` must be http(s), at most 2048 characters. `title` and `note` are
    optional, at most 1000 characters each. `saved_at` is an optional ISO 8601
    date or datetime (e.g. "2024-05-01" or "2024-05-01T10:00:00Z") for when it
    was originally saved; it defaults to now.
    Returns {"status": "added", "item": {...}} for a new link, or
    {"status": "exists", "item": {...}} if the library already has it (the
    existing item is left unchanged, including its note). The page's text is
    fetched in the background, so a new item starts with extraction_status
    "pending".
    """
    limiter.take(_token_key, 1)
    try:
        item = _validate({"url": url, "title": title, "note": note, "saved_at": saved_at})
    except ValueError as exc:
        _log_call("library_add_item", items=1, added=0, exists=0, invalid=1)
        raise ToolError(f"Invalid item: {exc}") from exc
    [result] = await _add_many([item])
    _log_call("library_add_item", items=1, **_totals([result]))
    if result["status"] == "invalid":
        raise ToolError(f"Invalid item: {result['error']}")
    return result


@mcp.tool(annotations=ADDS)
async def library_add_items(items: list[dict[str, Any]]) -> dict:
    """Save several links to the user's bookmark library in one call (at most 50).

    Use this instead of repeated library_add_item calls when adding many links.
    Each item is an object {"url": ..., "title": ..., "note": ..., "saved_at": ...}
    with the same rules as library_add_item; only "url" is required.
    A bad item doesn't fail the batch: it is reported as "invalid" with an
    error, and the rest are still added. Returns {"results": [...] in input
    order, each with a "status" of "added", "exists", or "invalid", and
    "totals": {"added": n, "exists": n, "invalid": n}}.
    """
    if not isinstance(items, list) or not items:
        raise ToolError("'items' must be a non-empty list")
    if len(items) > MAX_BATCH:
        _log_call("library_add_items", items=len(items), rejected="too_many")
        raise ToolError(f"Too many items: {len(items)} given, at most {MAX_BATCH} per call")
    limiter.take(_token_key, len(items))
    results = await _add_many(items)
    totals = _totals(results)
    _log_call("library_add_items", items=len(items), **totals)
    return {"results": results, "totals": totals}


# --- read-only (same behavior as mcp_server.py) ------------------------------

@mcp.tool(annotations=READ_ONLY)
async def library_search(query: str) -> str:
    """Search the user's saved bookmarks by keyword.

    Use this to check what the user has already saved on a topic. Every word in
    `query` must appear in the bookmark's title, URL, note, author, or
    extracted text (case-insensitive). Returns up to 10 matches, newest first.
    """
    rows = await _fetch_bookmarks({"q": query})
    terms = query.lower().split()
    hits = [b for b in _newest_first(rows) if _matches(b, terms)][:SEARCH_LIMIT]
    _log_call("library_search", results=len(hits))
    if not hits:
        return f"No bookmarks match {query!r}."
    return f"{len(hits)} result(s) for {query!r}:\n" + "\n".join(_line(b) for b in hits)


@mcp.tool(annotations=READ_ONLY)
async def library_list(limit: int = 20) -> str:
    """List the user's most recently saved bookmarks, newest first.

    Returns up to `limit` items (default 20).
    """
    rows = _newest_first(await _fetch_bookmarks())[: max(limit, 0)]
    _log_call("library_list", results=len(rows))
    if not rows:
        return "No bookmarks saved yet."
    return f"{len(rows)} most recent bookmark(s):\n" + "\n".join(_line(b) for b in rows)


# --- HTTP app ----------------------------------------------------------------

class BearerAuth:
    """ASGI middleware: every HTTP request needs `Authorization: Bearer <token>`."""

    def __init__(self, app, token: str):
        self.app = app
        self._expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            given = dict(scope["headers"]).get(b"authorization", b"")
            if not secrets.compare_digest(given, self._expected):
                await send({
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"text/plain"), (b"www-authenticate", b"Bearer")],
                })
                await send({"type": "http.response.body", "body": b"Unauthorized"})
                return
        await self.app(scope, receive, send)


def _security_settings() -> TransportSecuritySettings:
    # Keep the SDK's DNS-rebinding protection, plus any tunnel hostnames.
    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    origins = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
    for h in filter(None, (h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(","))):
        hosts.append(h)
        origins.append(f"https://{h}")
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins
    )


def create_app(token: str):
    global _token_key
    _token_key = hashlib.sha256(token.encode()).hexdigest()[:16]
    app = mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        host=HOST,
        transport_security=_security_settings(),
    )
    return BearerAuth(app, token)


def main():
    from dotenv import load_dotenv
    import uvicorn

    load_dotenv(ROOT / ".env")
    # force: the SDK configures logging on import. httpx would log request URLs,
    # which include search queries, so keep it quiet.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    token = os.environ.get("MCP_TOKEN", "")
    if len(token) < MIN_TOKEN_LENGTH:
        problem = "is not set" if not token else f"is shorter than {MIN_TOKEN_LENGTH} characters"
        print(f"MCP_TOKEN {problem}. Generate one with:\n  {TOKEN_COMMAND}\n"
              "and put it in .env as MCP_TOKEN=...", file=sys.stderr)
        sys.exit(1)

    port = int(os.environ.get("MCP_HTTP_PORT", "8001"))
    log.info("Starting MCP HTTP server on http://%s:%d/mcp (web app at %s)", HOST, port, _base_url())
    uvicorn.run(create_app(token), host=HOST, port=port)


if __name__ == "__main__":
    main()
