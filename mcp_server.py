"""Read-only MCP server for bookmark-agent.

Talks to the running web app over HTTP only (it does not import app/).
Run with:  .venv/bin/python mcp_server.py   (stdio transport)
"""
import os

import httpx
from mcp.server.mcpserver import MCPServer  # mcp 2.x name for FastMCP
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

BASE_URL = os.environ.get("BOOKMARKS_URL", "http://127.0.0.1:8000").rstrip("/")
TIMEOUT = 10.0
SEARCH_LIMIT = 10
TEXT_PREVIEW = 1500

mcp = MCPServer("bookmarks")
READ_ONLY = ToolAnnotations(readOnlyHint=True)


async def _fetch_bookmarks(params: dict | None = None) -> list[dict]:
    try:
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=TIMEOUT) as client:
            r = await client.get("/api/bookmarks", params=params)
            r.raise_for_status()
    # ToolError messages reach the model; other exceptions are hidden behind a generic error.
    except httpx.ConnectError as exc:
        raise ToolError(f"Can't reach the bookmarks app at {BASE_URL}; is it running?") from exc
    except httpx.TimeoutException as exc:
        raise ToolError(f"The bookmarks app at {BASE_URL} timed out") from exc
    except httpx.HTTPStatusError as exc:
        raise ToolError(f"Bookmarks API returned HTTP {exc.response.status_code}") from exc
    try:
        rows = r.json()
    except ValueError:
        rows = None
    if not isinstance(rows, list):
        raise ToolError("Unexpected response from /api/bookmarks (expected a JSON list)")
    return rows


def _newest_first(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda b: (b.get("created_at") or "", b.get("id") or 0), reverse=True)


def _matches(b: dict, terms: list[str]) -> bool:
    haystack = " ".join(
        str(b.get(k) or "") for k in ("title", "url", "note", "author", "summary", "text")
    ).lower()
    return all(t in haystack for t in terms)


def _line(b: dict) -> str:
    title = b.get("title") or "(untitled)"
    platform = b.get("platform") or "unknown"
    date = (b.get("created_at") or "")[:10]
    status = b.get("status")
    suffix = f" [{status}]" if status and status != "ready" else ""
    return f"#{b.get('id')} {title} — {platform}, {date}{suffix}\n    {b.get('url')}"


@mcp.tool(annotations=READ_ONLY)
async def search_bookmarks(query: str, platform: str | None = None) -> str:
    """Search the user's saved bookmarks by keyword.

    Use this when the user asks about something they saved, read, or bookmarked
    (e.g. "that article about tokenizers", "my substack saves on pricing").
    Every word in `query` must appear in the bookmark's title, URL, note, author,
    or extracted text (case-insensitive). Optionally restrict to one `platform`:
    one of "x", "instagram", "facebook", "substack", or "web".
    Returns up to 10 matches, newest first, each with its id; call get_bookmark
    with an id to read the full details and text.
    """
    params = {"q": query}
    if platform is not None:
        params["platform"] = platform
    rows = await _fetch_bookmarks(params)

    # The API may ignore q/platform, so filter here as well.
    terms = query.lower().split()
    want = platform.lower().strip() if platform else None
    hits = [
        b for b in _newest_first(rows)
        if _matches(b, terms) and (want is None or (b.get("platform") or "").lower() == want)
    ][:SEARCH_LIMIT]

    if not hits:
        where = f" on {platform}" if platform else ""
        return f"No bookmarks match {query!r}{where}."
    return f"{len(hits)} result(s) for {query!r}:\n" + "\n".join(_line(b) for b in hits)


@mcp.tool(annotations=READ_ONLY)
async def get_bookmark(id: int) -> str:
    """Get the full details of one bookmark by its numeric id.

    Use this after search_bookmarks or list_bookmarks to read a specific item:
    returns its url, title, platform, saved date, summary, the user's note, and
    the first 1500 characters of the page's extracted text.
    """
    rows = await _fetch_bookmarks()
    b = next((r for r in rows if r.get("id") == id), None)
    if b is None:
        return f"No bookmark with id {id}."

    text = b.get("text") or ""
    if len(text) > TEXT_PREVIEW:
        text = text[:TEXT_PREVIEW] + "…"
    fields = [
        ("url", b.get("url")),
        ("title", b.get("title")),
        ("platform", b.get("platform")),
        ("date", b.get("created_at")),
        ("status", b.get("status")),
        ("summary", b.get("summary")),
        ("note", b.get("note")),
    ]
    out = [f"Bookmark #{id}"] + [f"{k}: {v or '(none)'}" for k, v in fields]
    if b.get("status") == "failed" and b.get("error"):
        out.append(f"extraction error: {b['error']}")
    out.append("text:\n" + (text or "(none)"))
    return "\n".join(out)


@mcp.tool(annotations=READ_ONLY)
async def list_bookmarks(limit: int = 20) -> str:
    """List the user's most recently saved bookmarks, newest first.

    Use this to browse or answer "what did I save recently?" when there's no
    specific topic to search for (use search_bookmarks for that). Returns up to
    `limit` items (default 20), each with its id for use with get_bookmark.
    """
    rows = _newest_first(await _fetch_bookmarks())[: max(limit, 0)]
    if not rows:
        return "No bookmarks saved yet."
    return f"{len(rows)} most recent bookmark(s):\n" + "\n".join(_line(b) for b in rows)


if __name__ == "__main__":
    mcp.run()  # stdio
