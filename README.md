# bookmark-agent

A personal bookmark library: FastAPI + SQLite web app (`app/`), with MCP servers for agents.

```sh
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --port 8000      # web app at http://127.0.0.1:8000
.venv/bin/python -m pytest
```

## Database and login

The app uses Postgres (e.g. Supabase) when `DATABASE_URL` is set, and the local SQLite file
`bookmarks.db` when it isn't. Tests always use SQLite and never read `DATABASE_URL` from `.env`.
Tables are created on startup on either backend.

Every page and API endpoint needs a login, except `/login` and `/health`. Login is Supabase Auth
with email and password, and only `ALLOWED_EMAIL` can sign in. Create that user in the Supabase
dashboard under Authentication → Users, and turn off public sign-ups. For local development
without a network, set `AUTH_DISABLED=1`. It is ignored whenever `DATABASE_URL` is set, so a
Postgres-backed app always requires login.

To copy an existing `bookmarks.db` into Postgres (ids and dates are kept, and rows already there
are skipped):

```sh
.venv/bin/python scripts/migrate_sqlite_to_postgres.py --dry-run   # counts only, writes nothing
.venv/bin/python scripts/migrate_sqlite_to_postgres.py
```

### Environment variables

Set these in the environment or in `.env` (git-ignored).

| Variable | Needed | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | for Postgres | Postgres connection string. On Supabase, use the pooler URL (port 6543). Unset means SQLite. |
| `SUPABASE_URL` | yes, unless auth is disabled | Project URL, e.g. `https://<ref>.supabase.co` |
| `SUPABASE_ANON_KEY` | yes, unless auth is disabled | The project's anon (public) key |
| `ALLOWED_EMAIL` | yes, unless auth is disabled | The one email allowed to log in |
| `AUTH_DISABLED` | no | `1` turns login off, only when `DATABASE_URL` is unset |
| `ANTHROPIC_API_KEY` | for Ask and AI ranking | Anthropic API key |
| `ASK_MODEL` | no | Model for the Ask box (default `claude-sonnet-5-5`) |
| `DIGEST_USE_AI` | no | `1` ranks weekly digest picks with Claude |
| `RANK_MODEL` | no | Model for digest ranking (default `claude-haiku-5-5`) |
| `BOOKMARK_AGENT_CONTACT` | no | Contact info added to the paper sources' User-Agent |
| `MCP_TOKEN` | for `mcp_http_server.py` | Bearer token, at least 32 characters (see below) |
| `MCP_HTTP_PORT` | no | Remote MCP server port (default `8001`) |
| `MCP_ALLOWED_HOSTS` | no | Tunnel hostnames the remote MCP server accepts |
| `BOOKMARKS_URL` | no | Web app URL for the MCP servers (default `http://127.0.0.1:8000`) |

> **Note:** the MCP servers call the web app's API, which now requires a login. They work
> against a local app with `AUTH_DISABLED=1`, but not yet against a deployed one.

## Remote MCP access

`mcp_http_server.py` is a token-protected MCP server over Streamable HTTP that lets an
outside agent add links to the library and search or list it. It has no delete or edit
tools. It runs as its own process on `127.0.0.1:8001` and talks to the web app over HTTP.
(`mcp_server.py` is the separate, read-only stdio server for local clients.)

1. Generate a token and put it in `.env`. That file is git-ignored; never commit the token:

   ```sh
   echo "MCP_TOKEN=$(python -c 'import secrets; print(secrets.token_urlsafe(32))')" >> .env
   ```

   The server refuses to start if `MCP_TOKEN` is missing or shorter than 32 characters.

2. Start both servers, each in its own terminal:

   ```sh
   .venv/bin/uvicorn app.main:app --port 8000
   .venv/bin/python mcp_http_server.py
   ```

   The endpoint is `http://127.0.0.1:8001/mcp`. Clients must send
   `Authorization: Bearer <MCP_TOKEN>` on every request.

   Optional settings (environment or `.env`): `MCP_HTTP_PORT` (default `8001`),
   `BOOKMARKS_URL` (default `http://127.0.0.1:8000`), and `MCP_ALLOWED_HOSTS`, a
   comma-separated list of your tunnel's public hostnames (e.g. `my-tunnel.example.com`).
   The server rejects requests whose `Host` header isn't localhost or listed there, so
   set this when you use a tunnel.

Adds are limited to 120 items per minute and 50 per `library_add_items` call. The log
records the time, the tool and counts only, never notes, URLs, queries or the token.

> **Warning:** the server binds to `127.0.0.1` only. Expose it to the internet only
> through an authenticated tunnel (e.g. one that requires your own login in front of the
> bearer token). Anyone with the URL and token can add items to your library, so rotate
> `MCP_TOKEN` if it leaks.
