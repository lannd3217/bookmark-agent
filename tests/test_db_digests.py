import pytest
from fastapi.testclient import TestClient

import app.db as db
import app.main as web


def pick(n, rank, why=None):
    return {"url": f"https://arxiv.org/abs/2610.{n:05d}", "platform": "arxiv", "title": f"Paper {n}",
            "author": None, "text": "abstract", "note": why, "rank": rank, "why": why or f"why {n}"}


@pytest.fixture
def digests(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "bookmarks.db")
    db.init()


def test_empty_when_no_digests(digests):
    assert db.latest_digest_items() == []
    assert db.latest_digest_items("alignment") == []


def test_latest_digest_per_watchlist_ordered_by_watchlist_then_rank(digests):
    db.save_digest("robustness-evals", [pick(1, 1)])                      # older, superseded
    db.save_digest("alignment", [pick(2, 2), pick(3, 1)])
    db.save_digest("robustness-evals", [pick(4, 1), pick(1, 2, "again")])  # newest for this list

    rows = db.latest_digest_items()
    assert [(r["watchlist"], r["rank"], r["title"]) for r in rows] == [
        ("alignment", 1, "Paper 3"),
        ("alignment", 2, "Paper 2"),
        ("robustness-evals", 1, "Paper 4"),
        ("robustness-evals", 2, "Paper 1"),
    ]
    first = rows[0]
    assert set(first) == {"id", "title", "url", "watchlist", "rank", "why", "run_at"}
    assert first["id"] == db.search("2610.00003")[0]["id"]  # bookmark id, not digest id
    assert first["url"] == "https://arxiv.org/abs/2610.00003" and first["why"] == "why 3"
    assert rows[3]["why"] == "again" and rows[3]["run_at"]


def test_filter_by_watchlist(digests):
    db.save_digest("alignment", [pick(1, 1)])
    db.save_digest("interpretability", [pick(2, 1)])
    assert [r["title"] for r in db.latest_digest_items("interpretability")] == ["Paper 2"]
    assert db.latest_digest_items("no-such-list") == []


def test_matches_api_digests_latest(digests):
    db.save_digest("alignment", [pick(1, 1), pick(2, 2)])
    db.save_digest("interpretability", [pick(3, 1)])
    api = TestClient(web.app).get("/api/digests/latest").json()["digests"]
    from_api = sorted((d["watchlist"], i["rank"], i["id"]) for d in api for i in d["items"])
    assert [(r["watchlist"], r["rank"], r["id"]) for r in db.latest_digest_items()] == from_api
