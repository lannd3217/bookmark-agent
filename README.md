# bookmark-agent

A personal bookmark library: FastAPI + SQLite web app (`app/`), with MCP servers for agents.

```sh
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --port 8000      # web app at http://127.0.0.1:8000
.venv/bin/python -m pytest
```

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
