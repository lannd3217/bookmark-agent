import asyncio
import json
import pathlib
import tempfile

import httpx
import pytest
from starlette.testclient import TestClient

import app.db as db

# app.main runs db.init() on import; point it at a throwaway file first.
db.DB_PATH = pathlib.Path(tempfile.mkdtemp()) / "import.db"

import app.main as web  # noqa: E402
import mcp_http_server as srv  # noqa: E402

TOKEN = "test-token-" + "x" * 32


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "bookmarks.db")
    db.init()

    async def fake_fetch_page(url):
        return {"title": None, "author": None, "text": "page text", "image": None}

    monkeypatch.setattr(web, "fetch_page", fake_fetch_page)
    monkeypatch.setattr(
        srv, "_client",
        lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app), base_url="http://web"),
    )
    monkeypatch.setattr(srv, "limiter", srv.RateLimiter())
    monkeypatch.setattr(srv, "_add_lock", asyncio.Lock())
    with TestClient(srv.create_app(TOKEN), base_url="http://127.0.0.1:8001") as c:
        yield c


def rpc(c, name, args, token=TOKEN):
    headers = {"Accept": "application/json, text/event-stream"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return c.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": name, "arguments": args}},
        headers=headers,
    )


def call(c, name, args):
    """Call a tool and return (is_error, structured result or text)."""
    r = rpc(c, name, args)
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    if result.get("isError"):
        return True, result["content"][0]["text"]
    if result.get("structuredContent") is not None:
        return False, result["structuredContent"]
    return False, json.loads(result["content"][0]["text"])


# --- auth ---------------------------------------------------------------------

def test_no_token_is_401(client):
    r = rpc(client, "library_list", {}, token=None)
    assert r.status_code == 401
    assert r.text == "Unauthorized"


def test_wrong_token_is_401_with_same_response(client):
    wrong = rpc(client, "library_list", {}, token="y" * len(TOKEN))
    missing = rpc(client, "library_list", {}, token=None)
    assert wrong.status_code == 401
    assert (wrong.text, wrong.headers.get("www-authenticate")) == (
        missing.text, missing.headers.get("www-authenticate"))


def test_main_refuses_short_token(monkeypatch, capsys):
    monkeypatch.setenv("MCP_TOKEN", "too-short-token")
    with pytest.raises(SystemExit):
        srv.main()
    err = capsys.readouterr().err
    assert "secrets.token_urlsafe" in err
    assert "too-short-token" not in err


# --- adding -------------------------------------------------------------------

def test_valid_add(client):
    is_error, res = call(client, "library_add_item", {
        "url": "https://Example.com/post?utm_source=agent&id=1",
        "title": "A post", "note": "read later", "saved_at": "2024-05-01T10:00:00+02:00",
    })
    assert not is_error, res
    assert res["status"] == "added"
    item = res["item"]
    assert item["url"] == "https://example.com/post?id=1"
    assert item["title"] == "A post"
    assert item["note"] == "read later"
    assert item["saved_at"] == "2024-05-01 08:00:00"
    assert db.get(item["id"])["text"] == "page text"  # background extraction ran


def test_duplicate_add_returns_exists_and_keeps_note(client):
    _, first = call(client, "library_add_item", {"url": "https://example.com/a", "note": "mine"})
    is_error, again = call(client, "library_add_item",
                           {"url": "https://example.com/a#frag", "note": "agent's note"})
    assert not is_error, again
    assert again["status"] == "exists"
    assert again["item"]["id"] == first["item"]["id"]
    assert again["note_applied"] is False
    assert db.get(first["item"]["id"])["note"] == "mine"


@pytest.mark.parametrize("bad", [
    {"url": "ftp://example.com/file"},
    {"url": "javascript:alert(1)"},
    {"url": "https://example.com/" + "a" * 2048},
    {"url": "https://example.com/", "title": "t" * 1001},
    {"url": "https://example.com/", "saved_at": "yesterday"},
])
def test_invalid_item_rejected(client, bad):
    is_error, msg = call(client, "library_add_item", bad)
    assert is_error
    assert "Invalid item" in msg
    assert db.list_all() == []


def test_batch_of_51_rejected(client):
    items = [{"url": f"https://example.com/{i}"} for i in range(51)]
    is_error, msg = call(client, "library_add_items", {"items": items})
    assert is_error
    assert "at most 50" in msg
    assert db.list_all() == []


def test_batch_with_one_bad_item_adds_the_rest(client):
    call(client, "library_add_item", {"url": "https://example.com/existing"})
    is_error, res = call(client, "library_add_items", {"items": [
        {"url": "https://example.com/1", "title": "One"},
        {"url": "not a url"},
        {"url": "https://example.com/existing"},
        {"url": "https://example.com/2", "note": "two"},
    ]})
    assert not is_error, res
    assert [r["status"] for r in res["results"]] == ["added", "invalid", "exists", "added"]
    assert res["totals"] == {"added": 2, "exists": 1, "invalid": 1}
    assert "http" in res["results"][1]["error"]
    assert len(db.list_all()) == 3


def test_rate_limit(client, monkeypatch):
    monkeypatch.setattr(srv, "limiter", srv.RateLimiter(limit=3))
    is_error, _ = call(client, "library_add_items",
                       {"items": [{"url": f"https://example.com/{i}"} for i in range(3)]})
    assert not is_error
    is_error, msg = call(client, "library_add_item", {"url": "https://example.com/more"})
    assert is_error
    assert "Rate limit exceeded" in msg


# --- read-only ----------------------------------------------------------------

def test_search_and_list(client):
    call(client, "library_add_item", {"url": "https://example.com/tok", "title": "Tokenizer tax"})
    call(client, "library_add_item", {"url": "https://example.com/other", "title": "Other"})
    r = rpc(client, "library_search", {"query": "tokenizer"}).json()["result"]
    assert "Tokenizer tax" in r["content"][0]["text"] and "Other" not in r["content"][0]["text"]
    r = rpc(client, "library_list", {"limit": 1}).json()["result"]
    assert r["content"][0]["text"].startswith("1 most recent")
