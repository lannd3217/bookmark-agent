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

### Library shelves

Shelves live in the `categories` table. On first start it is seeded from
`config/categories.json`; after that, manage shelves in the Library (`/library`) and the file is
not read again. New bookmarks are shelved by `app/categorize.py` after extraction; weekly-digest
papers go on the shelf named by their watchlist's `category`. To shelve existing bookmarks:

```sh
.venv/bin/python scripts/categorize_backfill.py --dry-run   # show what would happen
.venv/bin/python scripts/categorize_backfill.py             # only items with no shelf
.venv/bin/python scripts/categorize_backfill.py --redo      # also redo the AI's choices (never yours or the digest's)
```

### Environment variables

Set these in the environment or in `.env` (git-ignored).

| Variable | Needed | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | for Postgres | Postgres connection string. On Supabase, use the pooler URL (port 6543). Unset means SQLite. |
| `SUPABASE_URL` | yes, unless auth is disabled | Project URL, e.g. `https://<ref>.supabase.co` |
| `SUPABASE_ANON_KEY` | yes, unless auth is disabled | The project's anon (public) key |
| `ALLOWED_EMAIL` | yes, unless auth is disabled | The one email allowed to log in |
| `AUTH_DISABLED` | no | `1` turns login off, only when `DATABASE_URL` is unset and not on Vercel |
| `CRON_SECRET` | for the cron routes | Bearer secret for `/api/cron/*`. If it's unset, those routes reject every request. |
| `API_TOKEN` | no | Bearer token for `/api/*` that works without logging in, at least 32 characters (for Claude Desktop) |
| `VERCEL` | set by Vercel | Turns on Vercel mode: Postgres required, extraction runs inline |
| `ANTHROPIC_API_KEY` | for Ask and AI ranking | Anthropic API key |
| `ASK_MODEL` | no | Model for the Ask box (default `claude-sonnet-5-5`) |
| `DIGEST_USE_AI` | no | `1` ranks weekly digest picks with Claude |
| `RANK_MODEL` | no | Model for digest ranking (default `claude-haiku-5-5`) |
| `BOOKMARK_AGENT_CONTACT` | no | Contact info added to the paper sources' User-Agent |
| `MCP_TOKEN` | for `mcp_http_server.py` | Bearer token, at least 32 characters (see below) |
| `MCP_HTTP_PORT` | no | Remote MCP server port (default `8001`) |
| `MCP_ALLOWED_HOSTS` | no | Tunnel hostnames the remote MCP server accepts |
| `BOOKMARKS_URL` | no | Web app URL for the MCP servers (default `http://127.0.0.1:8000`) |
| `BOOKMARKS_TOKEN` | for `mcp_server.py` with a deployed app | The app's `API_TOKEN`, sent as a bearer token |

> **Note:** `mcp_server.py` works with a deployed app when `BOOKMARKS_TOKEN` is set.
> `mcp_http_server.py` sends no token, so it only works against a local app with
> `AUTH_DISABLED=1`. It isn't part of the Vercel deployment.

## Deploy to Vercel

The app deploys as a single Python function. Vercel loads `index.py`, which imports
`app.main:app`. `vercel.json` adds `static/` and `config/` to the bundle, sets the function's
max duration to 300 s (the Hobby plan's maximum, which applies to every route, including the
crons), and schedules two cron jobs:

| Path | Schedule (UTC) | What it does |
| --- | --- | --- |
| `/api/cron/process-pending` | daily at 14:00 | Extracts up to 20 pending bookmarks |
| `/api/cron/weekly-digest` | Mondays at 15:00 | Runs the same job as `python -m app.jobs.weekly_digest` |

On Hobby, a cron job can start at any time within its scheduled hour. Vercel calls crons
only on the production deployment and sends `Authorization: Bearer <CRON_SECRET>`.

How Vercel mode differs from running locally:
- The filesystem is read-only, so the app refuses to start without `DATABASE_URL` and never
  writes local files.
- Saving or retrying a link extracts the page right away, with a 10-second limit, and the
  response comes back as `ready` or `failed`.
- Facebook imports are saved as `pending`, and the daily cron extracts 20 of them per run.
  The import reads a file from the server, and `data/` isn't deployed, so run large imports
  locally against the Supabase database instead.

Before the first deploy:
1. Copy the local data with `scripts/migrate_sqlite_to_postgres.py` (see above).
2. In Supabase, create your user under Authentication → Users and turn off new sign-ups.
3. In Vercel, under Project → Settings → Environment Variables, turn on **Enable access to
   System Environment Variables**, so that Vercel sets `VERCEL=1`. Then add these variables
   for Production:

| Variable | Required | What it is | Where to get it |
| --- | --- | --- | --- |
| `DATABASE_URL` | yes | Postgres connection string | Supabase → **Connect** → Transaction pooler URI (port 6543), with your database password filled in |
| `SUPABASE_URL` | yes | Supabase project URL | Supabase → Project Settings → API (`https://<ref>.supabase.co`) |
| `SUPABASE_ANON_KEY` | yes | Public key used to call Supabase Auth | Supabase → Project Settings → API Keys: the `anon` public key, or the publishable key |
| `ALLOWED_EMAIL` | yes | The only email allowed to log in | The user you created in Supabase Auth |
| `CRON_SECRET` | yes | Secret Vercel sends to the cron routes | Generate one: `python -c 'import secrets; print(secrets.token_urlsafe(32))'` |
| `API_TOKEN` | for Claude Desktop | Bearer token for `/api/*` without logging in, at least 32 characters | Generate one the same way, as a different value from `CRON_SECRET` |
| `ANTHROPIC_API_KEY` | for Ask and AI ranking | Anthropic API key | console.anthropic.com → API Keys |
| `DIGEST_USE_AI` | no | `1` ranks digest picks with Claude | Your choice |
| `ASK_MODEL` | no | Model for the Ask box | Defaults to `claude-sonnet-5-5` |
| `RANK_MODEL` | no | Model for digest ranking | Defaults to `claude-haiku-5-5` |
| `BOOKMARK_AGENT_CONTACT` | no | Contact info added to the arXiv and Hugging Face User-Agent | Your email or URL |

Don't set `AUTH_DISABLED` on Vercel; it's ignored there anyway. You don't need `MCP_TOKEN`,
`MCP_HTTP_PORT`, `MCP_ALLOWED_HOSTS` or `BOOKMARKS_URL` either.

To use the deployed library from Claude Desktop, run `mcp_server.py` locally and point it at
the deployment (replace the placeholders):

```json
{
  "mcpServers": {
    "bookmarks": {
      "command": "/path/to/bookmark-agent/.venv/bin/python",
      "args": ["/path/to/bookmark-agent/mcp_server.py"],
      "env": {
        "BOOKMARKS_URL": "https://<your-project>.vercel.app",
        "BOOKMARKS_TOKEN": "<your API_TOKEN>"
      }
    }
  }
}
```

To run a cron job by hand, for example after deploying:
`curl -H "Authorization: Bearer <CRON_SECRET>" https://<your-project>.vercel.app/api/cron/process-pending`

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
