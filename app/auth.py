"""Single-user login with Supabase Auth (email + password).

The session is the Supabase access and refresh tokens, kept in HttpOnly cookies. Every
request's access token is checked with Supabase (cached briefly) and must belong to
ALLOWED_EMAIL. Env: SUPABASE_URL, SUPABASE_ANON_KEY, ALLOWED_EMAIL; AUTH_DISABLED=1
turns login off for local development, but only on SQLite (DATABASE_URL unset), never on Vercel.

Two bearer secrets bypass the session: CRON_SECRET for /api/cron/* (and nothing else), and
API_TOKEN (at least 32 characters) for the other /api/* routes, e.g. for Claude Desktop.
"""
import hashlib
import os
import secrets
import time

import httpx

from . import db

ACCESS_COOKIE = "ba_access"
REFRESH_COOKIE = "ba_refresh"
COOKIE_MAX_AGE = 30 * 24 * 3600     # the refresh token decides when the session really ends
VERIFY_CACHE_SECONDS = 60
TIMEOUT = 10.0
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]"}
MIN_API_TOKEN_LENGTH = 32

_verified: dict[str, tuple[str, float]] = {}    # sha256(access token) -> (email, checked at)


class AuthError(Exception):
    """Login failed; the message is safe to show."""


class NotConfigured(Exception):
    pass


def auth_disabled() -> bool:
    return os.environ.get("AUTH_DISABLED") == "1" and not db.is_postgres() and not db.on_vercel()


def _bearer_matches(header: str | None, secret: str) -> bool:
    if not secret:
        return False
    return secrets.compare_digest((header or "").encode(), f"Bearer {secret}".encode())


def cron_authorized(header: str | None) -> bool:
    """Authorization header is `Bearer <CRON_SECRET>`; always False if CRON_SECRET is unset."""
    return _bearer_matches(header, os.environ.get("CRON_SECRET", "").strip())


def api_token_problem() -> str | None:
    token = os.environ.get("API_TOKEN", "").strip()
    if token and len(token) < MIN_API_TOKEN_LENGTH:
        return f"API_TOKEN is shorter than {MIN_API_TOKEN_LENGTH} characters, so token access is off"
    return None


def api_token_authorized(header: str | None) -> bool:
    """Authorization header is `Bearer <API_TOKEN>`; always False if API_TOKEN is unset or too short."""
    token = os.environ.get("API_TOKEN", "").strip()
    return len(token) >= MIN_API_TOKEN_LENGTH and _bearer_matches(header, token)


def _config() -> tuple[str, str, str]:
    url, key, email = (os.environ.get(k, "").strip()
                       for k in ("SUPABASE_URL", "SUPABASE_ANON_KEY", "ALLOWED_EMAIL"))
    if not (url and key and email):
        raise NotConfigured("SUPABASE_URL, SUPABASE_ANON_KEY and ALLOWED_EMAIL must be set")
    return url.rstrip("/"), key, email


def is_allowed(email: str | None) -> bool:
    return bool(email) and email.strip().casefold() == _config()[2].casefold()


async def _auth_request(method: str, path: str, **kwargs) -> httpx.Response:
    url, key, _ = _config()
    headers = {"apikey": key, **kwargs.pop("headers", {})}
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        return await client.request(method, f"{url}/auth/v1{path}", headers=headers, **kwargs)


def _session(data: dict) -> dict:
    return {"access_token": data["access_token"], "refresh_token": data["refresh_token"],
            "email": (data.get("user") or {}).get("email")}


async def sign_in(email: str, password: str) -> dict:
    """Return {access_token, refresh_token, email} or raise AuthError."""
    if not is_allowed(email):
        raise AuthError("Invalid email or password")
    try:
        r = await _auth_request("POST", "/token", params={"grant_type": "password"},
                                json={"email": email.strip(), "password": password})
    except httpx.HTTPError as exc:
        raise AuthError("Can't reach the login service; try again") from exc
    if r.status_code != 200:
        raise AuthError("Invalid email or password")
    session = _session(r.json())
    if not is_allowed(session["email"]):
        raise AuthError("Invalid email or password")
    return session


async def refresh(refresh_token: str) -> dict | None:
    """Exchange a refresh token for a new session, or None if it's no longer valid."""
    try:
        r = await _auth_request("POST", "/token", params={"grant_type": "refresh_token"},
                                json={"refresh_token": refresh_token})
    except httpx.HTTPError:
        return None
    return _session(r.json()) if r.status_code == 200 else None


async def verify(access_token: str) -> str | None:
    """The email the access token belongs to, or None if Supabase rejects it."""
    key = hashlib.sha256(access_token.encode()).hexdigest()
    now = time.monotonic()
    hit = _verified.get(key)
    if hit and now - hit[1] < VERIFY_CACHE_SECONDS:
        return hit[0]
    try:
        r = await _auth_request("GET", "/user", headers={"Authorization": f"Bearer {access_token}"})
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        _verified.pop(key, None)
        return None
    email = r.json().get("email")
    if len(_verified) > 100:
        _verified.clear()
    _verified[key] = (email, now)
    return email


async def check_session(cookies: dict) -> tuple[bool, dict | None]:
    """(logged in?, new session to store if the tokens were refreshed)."""
    access = cookies.get(ACCESS_COOKIE)
    if access and is_allowed(await verify(access)):
        return True, None
    refresh_token = cookies.get(REFRESH_COOKIE)
    if refresh_token:
        session = await refresh(refresh_token)
        if session and is_allowed(session["email"]):
            return True, session
    return False, None


def set_session_cookies(response, session: dict, host: str) -> None:
    secure = db.on_vercel() or host not in LOCAL_HOSTS
    for name, value in ((ACCESS_COOKIE, session["access_token"]), (REFRESH_COOKIE, session["refresh_token"])):
        response.set_cookie(name, value, max_age=COOKIE_MAX_AGE, httponly=True,
                            secure=secure, samesite="lax", path="/")


def clear_session_cookies(response) -> None:
    for name in (ACCESS_COOKIE, REFRESH_COOKIE):
        response.delete_cookie(name, path="/")
