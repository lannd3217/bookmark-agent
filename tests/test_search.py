import pytest
from fastapi.testclient import TestClient

import app.db as db
import app.main as web


@pytest.fixture
def rows(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "bookmarks.db")
    db.init()
    ids = {}
    for key, url, platform, title, created, extra in [
        ("old", "https://example.com/old", "web", "Pricing Strategy", "2023-01-01", {"author": "Ana"}),
        ("new", "https://foo.substack.com/p/pricing", "substack", "On pricing", "2024-06-01", {}),
        ("x", "https://x.com/u/status/1", "x", None, "2024-01-01", {"text": "Thread about LLM pricing tiers"}),
        ("pct", "https://example.com/pct", "web", "Save 100% today", "2022-01-01", {}),
    ]:
        ids[key], _ = db.insert(url, "my note" if key == "old" else None, platform, title, created)
        db.update_extracted(ids[key], {"title": title, **extra})
    return ids


def ids_of(results):
    return [r["id"] for r in results]


def test_every_word_must_match_some_field(rows):
    assert ids_of(db.search("pricing ana")) == [rows["old"]]      # title + author
    assert ids_of(db.search("llm tiers")) == [rows["x"]]          # text only
    assert ids_of(db.search("pricing nope")) == []


def test_case_insensitive_and_newest_first(rows):
    assert ids_of(db.search("PRICING")) == [rows["new"], rows["x"], rows["old"]]


def test_matches_url_and_note(rows):
    assert ids_of(db.search("substack")) == [rows["new"]]
    assert ids_of(db.search("note")) == [rows["old"]]


def test_platform_filter_and_limit(rows):
    assert ids_of(db.search("pricing", platform="x")) == [rows["x"]]
    assert ids_of(db.search("pricing", limit=1)) == [rows["new"]]
    assert ids_of(db.search("", platform="web")) == [rows["old"], rows["pct"]]


def test_like_wildcards_are_literal(rows):
    assert ids_of(db.search("100%")) == [rows["pct"]]
    assert ids_of(db.search("%")) == [rows["pct"]]
    assert db.search("_") == []


def test_quotes_are_just_text(rows):
    assert db.search("' OR 1=1 --") == []
    assert len(db.list_all()) == 4


def test_get_bookmarks_uses_search(rows):
    c = TestClient(web.app)
    assert ids_of(c.get("/api/bookmarks", params={"q": "pricing", "platform": "substack"}).json()) == [rows["new"]]
    assert len(c.get("/api/bookmarks").json()) == 4
    assert ids_of(c.get("/api/bookmarks", params={"platform": "x"}).json()) == [rows["x"]]
