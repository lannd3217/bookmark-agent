import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.db as db
import app.main as web
from app import auth
from app.jobs import weekly_digest

CRON_SECRET = "cron-secret-" + "c" * 20
API_TOKEN = "api-token-" + "a" * 32
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Login on (fake Supabase where nothing is a valid session), secrets set, SQLite in tmp_path."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "bookmarks.db")
    db.init()
    monkeypatch.delenv("AUTH_DISABLED")
    monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon")
    monkeypatch.setenv("ALLOWED_EMAIL", "me@example.com")
    monkeypatch.setenv("CRON_SECRET", CRON_SECRET)
    monkeypatch.setenv("API_TOKEN", API_TOKEN)

    async def no_session(token):
        return "me@example.com" if token == "good-token" else None

    async def no_refresh(token):
        return None

    monkeypatch.setattr(auth, "verify", no_session)
    monkeypatch.setattr(auth, "refresh", no_refresh)
    return TestClient(web.app, base_url="https://testserver", follow_redirects=False)


@pytest.fixture
def fetches(monkeypatch):
    """Fake fetch_page that records URLs; set .delay to make it slow."""
    calls = []

    async def fake_fetch_page(url):
        calls.append(url)
        await asyncio.sleep(fake_fetch_page.delay)
        return {"title": "Fetched", "author": None, "text": "page text", "image": None}

    fake_fetch_page.delay = 0
    fake_fetch_page.calls = calls
    monkeypatch.setattr(web, "fetch_page", fake_fetch_page)
    return fake_fetch_page


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


API = bearer(API_TOKEN)
CRON = bearer(CRON_SECRET)


# --- cron secret ------------------------------------------------------------------

CRON_PATHS = ["/api/cron/process-pending", "/api/cron/weekly-digest"]


@pytest.mark.parametrize("path", CRON_PATHS)
@pytest.mark.parametrize("headers", [
    {},
    bearer("wrong-secret"),
    {"Authorization": CRON_SECRET},             # missing "Bearer "
    bearer(CRON_SECRET + "x"),
    API,                                         # the API token is not a cron secret
])
def test_cron_rejects_missing_or_wrong_secret(env, monkeypatch, path, headers):
    ran = []
    monkeypatch.setattr(weekly_digest, "main", lambda argv: ran.append(argv) or 0)
    monkeypatch.setattr(db, "list_pending", lambda limit: ran.append(limit) or [])
    r = env.get(path, headers=headers)
    assert r.status_code == 401
    assert ran == []


@pytest.mark.parametrize("path", CRON_PATHS)
def test_cron_rejects_logged_in_session_without_secret(env, path):
    env.cookies.set(auth.ACCESS_COOKIE, "good-token", domain="testserver.local")
    assert env.get("/api/bookmarks").status_code == 200
    assert env.get(path).status_code == 401


@pytest.mark.parametrize("path", CRON_PATHS)
def test_cron_rejects_everything_when_secret_unset(env, monkeypatch, path):
    monkeypatch.setenv("CRON_SECRET", "")
    assert env.get(path, headers=bearer("")).status_code == 401
    assert env.get(path, headers={"Authorization": "Bearer "}).status_code == 401


def test_cron_requires_secret_even_with_auth_disabled(env, monkeypatch):
    monkeypatch.setenv("AUTH_DISABLED", "1")
    assert env.get("/api/cron/process-pending").status_code == 401


def test_weekly_digest_cron_runs_the_job(env, monkeypatch):
    calls = []
    monkeypatch.setattr(weekly_digest, "main", lambda argv: calls.append(argv) or 0)
    r = env.get("/api/cron/weekly-digest", headers=CRON)
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert calls == [[]]


def test_weekly_digest_cron_reports_failure(env, monkeypatch):
    monkeypatch.setattr(weekly_digest, "main", lambda argv: 1)
    assert env.get("/api/cron/weekly-digest", headers=CRON).status_code == 500


# --- API token ----------------------------------------------------------------------

def test_api_token_accepted(env):
    db.insert("https://example.com/a", None, "web")
    r = env.get("/api/bookmarks", headers=API)
    assert r.status_code == 200
    assert [b["url"] for b in r.json()] == ["https://example.com/a"]


@pytest.mark.parametrize("headers", [
    bearer("wrong-token-" + "w" * 32),
    bearer(API_TOKEN[:-1]),
    {"Authorization": API_TOKEN},
    {"Authorization": f"Basic {API_TOKEN}"},
    bearer(CRON_SECRET),                         # the cron secret is not an API token
])
def test_wrong_api_token_rejected(env, headers):
    bid, _ = db.insert("https://example.com/keep", None, "web")
    assert env.get("/api/bookmarks", headers=headers).status_code == 401
    assert env.delete(f"/api/bookmarks/{bid}", headers=headers).status_code == 401
    assert db.get(bid) is not None


def test_wrong_api_token_does_not_fall_back_to_session(env):
    env.cookies.set(auth.ACCESS_COOKIE, "good-token", domain="testserver.local")
    assert env.get("/api/bookmarks", headers=bearer("wrong")).status_code == 401


def test_short_api_token_disables_token_access(env, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "short-token")
    assert auth.api_token_problem()
    assert env.get("/api/bookmarks", headers=bearer("short-token")).status_code == 401


def test_api_token_only_for_api_routes(env):
    r = env.get("/", headers=API)
    assert r.status_code == 303 and r.headers["location"] == "/login"


# --- extraction on Vercel -----------------------------------------------------------

def test_vercel_save_extracts_inline(env, fetches, monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    r = env.post("/api/bookmarks", json={"url": "https://example.com/page"}, headers=API)
    assert r.status_code == 200
    assert r.json()["status"] == "ready"
    assert r.json()["title"] == "Fetched"


def test_vercel_inline_extraction_timeout_sets_failed(env, fetches, monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setattr(web, "EXTRACT_TIMEOUT", 0.05)
    fetches.delay = 5
    r = env.post("/api/bookmarks", json={"url": "https://example.com/slow"}, headers=API)
    assert r.status_code == 200
    row = r.json()
    assert row["status"] == "failed"
    assert row["error"] == "Extraction timed out after 0.05s"
    assert db.get(row["id"])["status"] == "failed"


def test_vercel_retry_extracts_inline(env, fetches, monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    bid, _ = db.insert("https://example.com/r", None, "web")
    db.set_failed(bid, "boom")
    r = env.post(f"/api/bookmarks/{bid}/retry", headers=API)
    assert r.status_code == 200 and r.json()["status"] == "ready"


def test_vercel_import_saves_pending_without_extracting(env, fetches, monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    r = env.post("/api/import/facebook", json={"path": str(FIXTURES / "facebook_saves.json")}, headers=API)
    assert r.status_code == 200 and r.json()["added"] > 0
    assert fetches.calls == []
    assert {b["status"] for b in db.list_all()} == {"pending"}


def test_process_pending_handles_at_most_20(env, fetches):
    ids = [db.insert(f"https://example.com/{i}", None, "web")[0] for i in range(25)]
    r = env.get("/api/cron/process-pending", headers=CRON)
    assert r.status_code == 200
    assert r.json() == {"processed": 20, "ready": 20, "failed": 0}
    assert len(fetches.calls) == 20
    assert [b["id"] for b in db.list_pending(100)] == ids[20:]     # oldest first

    r = env.get("/api/cron/process-pending", headers=CRON)
    assert r.json() == {"processed": 5, "ready": 5, "failed": 0}
    assert env.get("/api/cron/process-pending", headers=CRON).json()["processed"] == 0


def test_process_pending_marks_timeouts_failed(env, fetches, monkeypatch):
    monkeypatch.setattr(web, "EXTRACT_TIMEOUT", 0.05)
    fetches.delay = 5
    bid, _ = db.insert("https://example.com/slow", None, "web")
    r = env.get("/api/cron/process-pending", headers=CRON)
    assert r.json() == {"processed": 1, "ready": 0, "failed": 1}
    assert db.get(bid)["error"] == "Extraction timed out after 0.05s"


# --- Vercel startup and cookies -----------------------------------------------------

def test_vercel_refuses_to_start_without_database_url(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    with pytest.raises(RuntimeError, match="DATABASE_URL is not set"):
        db.require_database_on_vercel()
    monkeypatch.setenv("DATABASE_URL", "postgresql://example/db")
    db.require_database_on_vercel()


def test_auth_disabled_ignored_on_vercel(monkeypatch):
    monkeypatch.setenv("AUTH_DISABLED", "1")
    monkeypatch.setenv("VERCEL", "1")
    assert not auth.auth_disabled()


@pytest.mark.parametrize("host,vercel,secure", [
    ("localhost", False, False),
    ("127.0.0.1", False, False),
    ("bookmarks.example.com", False, True),
    ("testserver", False, True),
    ("localhost", True, True),
])
def test_session_cookies_secure_except_localhost(monkeypatch, host, vercel, secure):
    if vercel:
        monkeypatch.setenv("VERCEL", "1")
    from fastapi.responses import JSONResponse
    response = JSONResponse({})
    auth.set_session_cookies(response, {"access_token": "a", "refresh_token": "r"}, host)
    cookies = response.headers.getlist("set-cookie")
    assert len(cookies) == 2
    assert all(("; secure" in c.lower()) == secure for c in cookies)
