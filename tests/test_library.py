import importlib.util
import json
import logging
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.categorize as categorizer
import app.db as db
import app.main as web
from app.jobs import weekly_digest

ROOT = Path(__file__).parent.parent
SEED_NAMES = [c["name"] for c in json.loads((ROOT / "config" / "categories.json").read_text())]


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Fresh SQLite library and a fake categorize: picks the shelf whose name appears in the
    item's title or text, else "Other". Records the shelf names each call was offered."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "bookmarks.db")
    db.init()
    calls = []

    def fake_categorize(item, categories=None):
        calls.append([c["name"] for c in categories])
        if fake_categorize.fail:
            raise RuntimeError("model unavailable")
        text = f"{item.get('title') or ''} {item.get('text') or ''}".lower()
        return next((c["name"] for c in categories if c["name"].lower() in text), "Other")

    fake_categorize.fail = False
    monkeypatch.setattr(categorizer, "categorize", fake_categorize)
    return SimpleNamespace(client=TestClient(web.app), calls=calls, fake=fake_categorize)


def shelf(name):
    return db.category_by_name(name)["id"]


def add(url, title="Untitled", platform="web", created="2026-10-01 12:00:00", on=None, by=None, text=None):
    bid, _ = db.insert(url, None, platform, title, created)
    db.update_extracted(bid, {"title": title, "text": text})
    if on:
        db.set_category(bid, shelf(on), by)
    return bid


def names():
    return [c["name"] for c in db.list_categories()]


# --- seeding and migration ------------------------------------------------------

def test_seeding_runs_once(env, monkeypatch, tmp_path):
    assert names() == SEED_NAMES
    assert names()[-1] == "Other"
    db.delete_category(shelf("Learning & careers"))
    # The file is never read again once the table has rows.
    monkeypatch.setattr(db, "CATEGORIES_SEED", tmp_path / "missing.json")
    db.init()
    assert names() == [n for n in SEED_NAMES if n != "Learning & careers"]


def test_seed_without_other_still_gets_other_last(tmp_path, monkeypatch):
    seed = tmp_path / "cats.json"
    seed.write_text(json.dumps([{"name": "Cooking", "description": "Food"}]))
    monkeypatch.setattr(db, "CATEGORIES_SEED", seed)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "b.db")
    db.init()
    assert names() == ["Cooking", "Other"]


def test_existing_database_gets_category_columns(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE bookmarks(id INTEGER PRIMARY KEY, url TEXT UNIQUE NOT NULL, note TEXT, "
                  "created_at TEXT DEFAULT (datetime('now')))")
        c.execute("INSERT INTO bookmarks(url) VALUES ('https://example.com/old')")
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init()
    row = db.list_all()[0]
    assert row["category_id"] is None and row["category_set_by"] is None and row["category"] is None
    assert row["kind"] == "article"
    assert names()[-1] == "Other"


# --- shelf API ------------------------------------------------------------------

def test_list_categories_with_counts(env):
    add("https://a.example/1", on="Interpretability", by="ai")
    add("https://a.example/2", on="Interpretability", by="user")
    add("https://a.example/3")
    body = env.client.get("/api/categories").json()
    assert body["all"] == 3 and body["unsorted"] == 1
    assert [c["name"] for c in body["categories"]] == SEED_NAMES
    interp = body["categories"][0]
    assert interp["count"] == 2 and interp["is_fallback"] is False
    assert body["categories"][-1]["is_fallback"] is True


def test_other_cannot_be_renamed_deleted_or_moved(env):
    other = shelf("Other")
    r = env.client.patch(f"/api/categories/{other}", json={"name": "Misc"})
    assert r.status_code == 400 and "can't be renamed" in r.json()["detail"]
    r = env.client.delete(f"/api/categories/{other}")
    assert r.status_code == 400 and "can't be deleted" in r.json()["detail"]
    r = env.client.patch(f"/api/categories/{other}", json={"position": 0})
    assert r.status_code == 400 and "stays last" in r.json()["detail"]
    # Its description can change.
    r = env.client.patch(f"/api/categories/{other}", json={"description": "Everything else"})
    assert r.status_code == 200 and r.json()["description"] == "Everything else"
    assert names()[-1] == "Other"


def test_duplicate_and_invalid_names_rejected(env):
    r = env.client.post("/api/categories", json={"name": "  interpretability ", "description": ""})
    assert r.status_code == 409 and "already a shelf" in r.json()["detail"]
    r = env.client.post("/api/categories", json={"name": "other"})
    assert r.status_code == 409
    for bad in ({"name": "   "}, {"name": "x" * 41}, {"name": "Ok", "description": "d" * 201}):
        assert env.client.post("/api/categories", json=bad).status_code == 422
    r = env.client.patch(f"/api/categories/{shelf('Machine learning')}", json={"name": "ALIGNMENT & OVERSIGHT"})
    assert r.status_code == 409
    # Same name, different case of itself is a rename, not a duplicate.
    r = env.client.patch(f"/api/categories/{shelf('Machine learning')}", json={"name": "Machine Learning"})
    assert r.status_code == 200 and r.json()["name"] == "Machine Learning"


def test_create_goes_above_other_and_reorder_keeps_other_last(env):
    r = env.client.post("/api/categories", json={"name": " Neuroscience ", "description": " Brains "})
    assert r.status_code == 201
    created = r.json()
    assert created["name"] == "Neuroscience" and created["description"] == "Brains" and created["count"] == 0
    assert names()[-2:] == ["Neuroscience", "Other"]
    assert env.client.patch(f"/api/categories/{created['id']}", json={"position": 0}).status_code == 200
    assert names()[0] == "Neuroscience"
    env.client.patch(f"/api/categories/{created['id']}", json={"position": 999})
    assert names()[-2:] == ["Neuroscience", "Other"]
    assert env.client.patch(f"/api/categories/{created['id']}", json={}).status_code == 422
    assert env.client.patch("/api/categories/9999", json={"name": "Nope"}).status_code == 404


def test_delete_moves_items(env):
    a = add("https://a.example/1", on="Machine learning", by="ai")
    b = add("https://a.example/2", on="Machine learning", by="digest")
    target = shelf("AI tools & products")
    ml = shelf("Machine learning")
    assert env.client.delete(f"/api/categories/{ml}?move_to={ml}").status_code == 400
    assert env.client.delete(f"/api/categories/{ml}?move_to=9999").status_code == 400
    r = env.client.delete(f"/api/categories/{ml}?move_to={target}")
    assert r.status_code == 200 and r.json()["moved"] == 2 and r.json()["moved_to"]["id"] == target
    for bid in (a, b):
        row = db.get(bid)
        assert row["category_id"] == target and row["category_set_by"] == "user"
    assert "Machine learning" not in names()
    assert env.client.delete(f"/api/categories/{ml}").status_code == 404
    # Without move_to, items go to Other.
    c = add("https://a.example/3", on="Learning & careers", by="ai")
    env.client.delete(f"/api/categories/{shelf('Learning & careers')}")
    assert db.get(c)["category"] == "Other"


# --- categorizing ---------------------------------------------------------------

@pytest.fixture
def fake_fetch(monkeypatch):
    async def fetch_page(url):
        return {"title": f"Notes on Interpretability ({url})", "author": None, "text": "body", "image": None}
    monkeypatch.setattr(web, "fetch_page", fetch_page)


def test_save_categorizes_with_shelves_from_the_database(env, fake_fetch):
    env.client.patch(f"/api/categories/{shelf('Machine learning')}", json={"name": "ML research"})
    r = env.client.post("/api/bookmarks", json={"url": "https://blog.example/post"})
    assert r.status_code == 200
    row = db.get(r.json()["id"])
    assert row["category"] == "Interpretability" and row["category_set_by"] == "ai"
    assert "ML research" in env.calls[-1] and "Machine learning" not in env.calls[-1]


def test_categorize_failure_never_fails_the_save(env, fake_fetch):
    env.fake.fail = True
    r = env.client.post("/api/bookmarks", json={"url": "https://blog.example/post"})
    assert r.status_code == 200
    row = db.get(r.json()["id"])
    assert row["status"] == "ready"
    assert row["category"] == "Other" and row["category_set_by"] == "ai"


def test_resort_only_touches_ai_items_on_other(env):
    ai_other = add("https://n.example/1", "Neuroscience of attention", on="Other", by="ai")
    ai_other_miss = add("https://n.example/2", "Keyboards", on="Other", by="ai")
    ai_other_ml = add("https://n.example/3", "Machine learning tricks", on="Other", by="ai")
    user_other = add("https://n.example/4", "Neuroscience for me", on="Other", by="user")
    digest_other = add("https://n.example/5", "Neuroscience paper", on="Other", by="digest")
    ai_elsewhere = add("https://n.example/6", "Neuroscience again", on="Interpretability", by="ai")
    neuro = env.client.post("/api/categories", json={"name": "Neuroscience", "description": "Brains"}).json()

    r = env.client.post(f"/api/categories/{neuro['id']}/resort")
    assert r.status_code == 200
    assert r.json() == {"checked": 3, "moved": 2, "moved_here": 1}
    assert db.get(ai_other)["category"] == "Neuroscience"
    assert db.get(ai_other)["category_set_by"] == "ai"
    assert db.get(ai_other_ml)["category"] == "Machine learning"
    assert db.get(ai_other_miss)["category"] == "Other"
    for bid, shelf_name in ((user_other, "Other"), (digest_other, "Other"), (ai_elsewhere, "Interpretability")):
        assert db.get(bid)["category"] == shelf_name
    assert env.client.post("/api/categories/9999/resort").status_code == 404


def test_resort_checks_at_most_25(env):
    for i in range(30):
        add(f"https://n.example/{i}", "Nothing in particular", on="Other", by="ai")
    neuro = env.client.post("/api/categories", json={"name": "Neuroscience"}).json()
    assert env.client.post(f"/api/categories/{neuro['id']}/resort").json()["checked"] == 25


# --- item API -------------------------------------------------------------------

def test_patch_bookmark_sets_user(env):
    bid = add("https://a.example/1", on="Interpretability", by="ai")
    target = shelf("Alignment & oversight")
    r = env.client.patch(f"/api/bookmarks/{bid}", json={"category_id": target})
    assert r.status_code == 200
    assert r.json()["category_id"] == target and r.json()["category_set_by"] == "user"
    assert r.json()["category"] == "Alignment & oversight"
    r = env.client.patch(f"/api/bookmarks/{bid}", json={"category_id": 9999})
    assert r.status_code == 400 and "No shelf" in r.json()["detail"]
    assert db.get(bid)["category_id"] == target
    assert env.client.patch("/api/bookmarks/9999", json={"category_id": target}).status_code == 404
    assert env.client.patch(f"/api/bookmarks/{bid}", json={}).status_code == 422


def test_filters_and_sort(env):
    paper = add("https://arxiv.org/abs/1", "Banana paper", "arxiv", "2026-10-03 00:00:00", on="Interpretability", by="digest")
    tweet = add("https://x.com/u/status/1", "Apple thread", "x", "2026-10-01 00:00:00", on="Interpretability", by="ai")
    insta = add("https://instagram.com/p/1", "Cherry reel", "instagram", "2026-10-02 00:00:00", on="Other", by="ai")
    blog = add("https://blog.example/1", "Date blog", "web", "2026-10-04 00:00:00", on="Other", by="ai")
    sub = add("https://a.substack.com/p/1", "Elder essay", "substack", "2026-10-05 00:00:00")

    def ids(**params):
        r = env.client.get("/api/bookmarks", params=params)
        assert r.status_code == 200
        return [row["id"] for row in r.json()]

    assert ids(sort="newest") == [sub, blog, paper, insta, tweet]
    assert ids(sort="oldest") == [tweet, insta, paper, blog, sub]
    assert ids(sort="title") == [tweet, paper, insta, blog, sub]
    assert ids(kind="paper") == [paper]
    assert ids(kind="post") == [insta, tweet]
    assert ids(kind="article") == [sub, blog]
    assert ids(category_id=shelf("Interpretability")) == [paper, tweet]
    assert ids(category_id=shelf("Interpretability"), kind="post") == [tweet]
    assert ids(q="essay") == [sub]
    assert ids(q="reel", category_id=shelf("Other"), sort="title") == [insta]
    row = env.client.get("/api/bookmarks", params={"kind": "paper"}).json()[0]
    assert row["kind"] == "paper" and row["category"] == "Interpretability" and row["category_set_by"] == "digest"
    # No parameters: everything, newest saved first (the home page's order).
    assert ids() == [sub, blog, insta, tweet, paper]
    assert env.client.get("/api/bookmarks", params={"kind": "video"}).status_code == 422
    assert env.client.get("/api/bookmarks", params={"sort": "random"}).status_code == 422


def test_kind_is_derived_from_platform():
    assert [db.kind_of(p) for p in ("arxiv", "x", "instagram", "facebook", "substack", "web", None)] == \
        ["paper", "post", "post", "post", "article", "article", "article"]


# --- digest ---------------------------------------------------------------------

def pick(url, rank=1):
    return {"url": url, "platform": "arxiv", "title": "T", "author": None, "text": None,
            "note": None, "rank": rank, "why": None}


def test_digest_uses_the_watchlist_shelf(env):
    w = {"name": "alignment", "category": "Alignment & oversight"}
    cid = weekly_digest.shelf_for(w)
    assert cid == shelf("Alignment & oversight")
    mine = add("https://arxiv.org/abs/2", "Mine", "arxiv", on="Interpretability", by="user")
    _, saved = db.save_digest("alignment", [pick("https://arxiv.org/abs/1"), pick("https://arxiv.org/abs/2", 2)],
                              "prescore", category_id=cid)
    new = db.get(saved[0][0])
    assert new["category_id"] == cid and new["category_set_by"] == "digest"
    assert db.get(mine)["category"] == "Interpretability" and db.get(mine)["category_set_by"] == "user"
    assert env.calls == []      # no AI call for digest papers


def test_renamed_digest_shelf_falls_back_to_other(env, caplog):
    env.client.patch(f"/api/categories/{shelf('Alignment & oversight')}", json={"name": "Alignment"})
    with caplog.at_level(logging.WARNING, logger="weekly_digest"):
        cid = weekly_digest.shelf_for({"name": "alignment", "category": "Alignment & oversight"})
    assert cid == shelf("Other")
    assert "'alignment'" in caplog.text and "renamed or deleted" in caplog.text


def test_shipped_watchlists_name_seeded_shelves():
    for w in weekly_digest.load_watchlists():
        assert w["category"] in SEED_NAMES, w["name"]


def test_watchlist_category_must_be_a_name(tmp_path):
    path = tmp_path / "w.json"
    path.write_text(json.dumps([{"name": "a", "domain": "d", "arxiv_categories": ["cs.CL"], "category": ""}]))
    with pytest.raises(ValueError, match="'category'"):
        weekly_digest.load_watchlists(path)


# --- backfill script ------------------------------------------------------------

@pytest.fixture
def backfill():
    spec = importlib.util.spec_from_file_location("categorize_backfill", ROOT / "scripts" / "categorize_backfill.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mixed(env):
    ids = SimpleNamespace(
        none=add("https://b.example/1", "Robustness & evaluations of agents"),
        ai=add("https://b.example/2", "Machine learning basics", on="Other", by="ai"),
        user=add("https://b.example/3", "Machine learning, mine", on="Other", by="user"),
        digest=add("https://b.example/4", "Machine learning digest", on="Interpretability", by="digest"),
    )
    ids.pending, _ = db.insert("https://b.example/5", None, "web", "Machine learning pending")
    return ids


def test_backfill_fills_only_unshelved_by_default(env, backfill, mixed, capsys):
    assert backfill.main([]) == 0
    assert db.get(mixed.none)["category"] == "Robustness & evaluations"
    assert db.get(mixed.none)["category_set_by"] == "ai"
    assert db.get(mixed.ai)["category"] == "Other"
    assert db.get(mixed.pending)["category_id"] is None
    out = capsys.readouterr().out
    assert "1 item(s) to categorize" in out
    assert "Robustness & evaluations" in out and "Total" in out


def test_backfill_redo_never_touches_user_or_digest(env, backfill, mixed):
    assert backfill.main(["--redo"]) == 0
    assert db.get(mixed.ai)["category"] == "Machine learning"
    assert db.get(mixed.user)["category"] == "Other" and db.get(mixed.user)["category_set_by"] == "user"
    assert db.get(mixed.digest)["category"] == "Interpretability" and db.get(mixed.digest)["category_set_by"] == "digest"
    assert len(env.calls) == 2      # only the unshelved item and the AI-shelved one


def test_backfill_dry_run_and_limit(env, backfill, mixed, capsys):
    backfill.main(["--redo", "--dry-run"])
    assert db.get(mixed.none)["category_id"] is None
    assert db.get(mixed.ai)["category"] == "Other"
    assert "would be" in capsys.readouterr().out
    backfill.main(["--redo", "--limit", "1"])
    assert db.get(mixed.none)["category_id"] is not None
    assert db.get(mixed.ai)["category"] == "Other"


# --- page -----------------------------------------------------------------------

def test_library_page_is_served(env):
    r = env.client.get("/library")
    assert r.status_code == 200
    assert "<title>Library · Personal Knowledge Collection</title>" in r.text
