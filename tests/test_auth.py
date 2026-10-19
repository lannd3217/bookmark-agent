import pytest
from fastapi.testclient import TestClient

import app.db as db
import app.main as web
from app import auth

ALLOWED = "me@example.com"


@pytest.fixture
def login_on(tmp_path, monkeypatch):
    """Auth enabled with a fake Supabase: only the token 'good-token' is a live session for ALLOWED."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "bookmarks.db")
    db.init()
    monkeypatch.delenv("AUTH_DISABLED")
    monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon")
    monkeypatch.setenv("ALLOWED_EMAIL", ALLOWED)
    sessions = {"good-token": ALLOWED, "other-user-token": "someone@else.com"}
    refreshable = {"good-refresh": {"access_token": "good-token", "refresh_token": "next-refresh",
                                    "email": ALLOWED}}
    supabase_calls = []

    async def fake_verify(token):
        return sessions.get(token)

    async def fake_refresh(token):
        return refreshable.get(token)

    async def fake_auth_request(method, path, **kwargs):
        supabase_calls.append((method, path, kwargs))
        raise AssertionError("tests must not reach Supabase")

    monkeypatch.setattr(auth, "verify", fake_verify)
    monkeypatch.setattr(auth, "refresh", fake_refresh)
    monkeypatch.setattr(auth, "_auth_request", fake_auth_request)
    client = TestClient(web.app, follow_redirects=False)
    client.supabase_calls = supabase_calls
    return client


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/bookmarks"),
    ("POST", "/api/bookmarks"),
    ("DELETE", "/api/bookmarks/1"),
    ("POST", "/api/bookmarks/1/retry"),
    ("POST", "/api/import/facebook"),
    ("GET", "/api/digests/latest"),
    ("POST", "/api/ask"),
])
def test_api_rejects_unauthenticated(login_on, method, path):
    r = login_on.request(method, path, json={"url": "https://example.com", "question": "hi"})
    assert r.status_code == 401
    assert r.json() == {"detail": "Not logged in"}


@pytest.mark.parametrize("path", ["/", "/docs", "/openapi.json", "/no-such-page"])
def test_pages_redirect_to_login(login_on, path):
    r = login_on.get(path)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_unauthenticated_request_does_not_touch_data(login_on):
    bid, _ = db.insert("https://example.com/keep", None, "web")
    assert login_on.delete(f"/api/bookmarks/{bid}").status_code == 401
    assert db.get(bid) is not None


@pytest.mark.parametrize("cookies", [
    {auth.ACCESS_COOKIE: "forged-token"},
    {auth.ACCESS_COOKIE: "other-user-token"},     # a real session, but not ALLOWED_EMAIL
    {auth.ACCESS_COOKIE: "forged-token", auth.REFRESH_COOKIE: "revoked-refresh"},
])
def test_invalid_sessions_rejected(login_on, cookies):
    login_on.cookies.update(cookies)
    assert login_on.get("/api/bookmarks").status_code == 401
    assert login_on.get("/").status_code == 303


def test_login_page_and_health_are_public(login_on):
    r = login_on.get("/login")
    assert r.status_code == 200 and "<form" in r.text
    assert login_on.get("/health").json() == {"ok": True}


def test_valid_session_allowed(login_on):
    login_on.cookies.set(auth.ACCESS_COOKIE, "good-token")
    assert login_on.get("/api/bookmarks").status_code == 200
    assert login_on.get("/").status_code == 200


def test_expired_access_token_is_refreshed(login_on):
    login_on.cookies.update({auth.ACCESS_COOKIE: "expired-token", auth.REFRESH_COOKIE: "good-refresh"})
    r = login_on.get("/api/bookmarks")
    assert r.status_code == 200
    assert r.cookies[auth.ACCESS_COOKIE] == "good-token"
    assert r.cookies[auth.REFRESH_COOKIE] == "next-refresh"


def test_login_rejects_other_email_without_calling_supabase(login_on):
    r = login_on.post("/login", json={"email": "intruder@example.com", "password": "x"})
    assert r.status_code == 401
    assert r.json()["detail"] == "Invalid email or password"
    assert login_on.supabase_calls == []


@pytest.fixture
def fake_sign_in(monkeypatch):
    async def sign_in(email, password):
        assert (email, password) == (ALLOWED, "pw")
        return {"access_token": "good-token", "refresh_token": "good-refresh", "email": ALLOWED}

    monkeypatch.setattr(auth, "sign_in", sign_in)


def test_login_success_sets_httponly_cookies(login_on, fake_sign_in):
    r = login_on.post("/login", json={"email": ALLOWED, "password": "pw"})
    assert r.status_code == 200
    set_cookie = r.headers.get_list("set-cookie")
    assert all("HttpOnly" in c and "SameSite=lax" in c for c in set_cookie)
    assert login_on.get("/api/bookmarks").status_code == 200


def test_logout_clears_session(login_on, fake_sign_in):
    login_on.post("/login", json={"email": ALLOWED, "password": "pw"})
    assert login_on.get("/api/bookmarks").status_code == 200
    r = login_on.post("/logout")
    assert r.status_code == 200
    assert login_on.get("/api/bookmarks").status_code == 401


def test_missing_config_fails_closed(login_on, monkeypatch):
    monkeypatch.delenv("SUPABASE_ANON_KEY")
    login_on.cookies.set(auth.ACCESS_COOKIE, "good-token")
    assert login_on.get("/api/bookmarks").status_code == 503


def test_auth_disabled_only_without_database_url(monkeypatch):
    monkeypatch.setenv("AUTH_DISABLED", "1")
    assert auth.auth_disabled()
    monkeypatch.setenv("DATABASE_URL", "postgresql://example/db")
    assert not auth.auth_disabled()
