import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

import app.db as db
import app.main as web
import app.summarize as summarizer
from app import summaries
from app.sources import USER_AGENT, arxiv_fulltext
from app.sources.arxiv import Pacer

REAL_CLIENT = arxiv_fulltext._client     # before conftest swaps it for an offline one
LIBRARY_HTML = (Path(__file__).parent.parent / "static" / "library.html").read_text()
LONG = "This piece explains a result in plain words. " * 20      # ~900 chars
SUMMARY = {"tldr": "", "why_it_matters": "It matters.", "key_points": ["One.", "Two.", "Three."],
           "terms": [{"term": "SAE", "meaning": "A small network."}], "caveats": []}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """SQLite in tmp_path, an API key set, a fake summarize, and a fake arxiv.org."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "bookmarks.db")
    db.init()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    calls = []

    def fake_summarize(title, text, source_kind):
        calls.append(SimpleNamespace(title=title, text=text, kind=source_kind))
        if fake_summarize.error:
            raise fake_summarize.error
        return {**SUMMARY, "tldr": fake_summarize.tldr or f"Summary {len(calls)}"}

    fake_summarize.error = None
    fake_summarize.tldr = None
    monkeypatch.setattr(summarizer, "summarize", fake_summarize)

    arxiv = SimpleNamespace(requests=[], routes={})

    def handler(request):
        arxiv.requests.append(request)
        return arxiv.routes.get(request.url.path, httpx.Response(404, text="Not found"))

    monkeypatch.setattr(arxiv_fulltext, "_pacer", Pacer(0))
    monkeypatch.setattr(arxiv_fulltext, "_client", lambda: httpx.Client(
        transport=httpx.MockTransport(handler), follow_redirects=True, headers={"User-Agent": USER_AGENT}))
    return SimpleNamespace(client=TestClient(web.app), calls=calls, fake=fake_summarize, arxiv=arxiv)


def add(url="https://blog.example/post", platform="web", title="A post", text=LONG):
    bid, _ = db.insert(url, None, platform, title)
    db.update_extracted(bid, {"title": title, "text": text})
    return bid


def summarize(env, bid, **body):
    return env.client.post(f"/api/bookmarks/{bid}/summarize", json=body or None)


def paper_html(body_chars=3000):
    para = "<p>We study sparse features and find <math alttext=\"x^{2}\"><mi>x</mi></math> scales well. </p>"
    return (f"<html><body><nav>arXiv menu</nav><article class=\"ltx_document\"><h1>Title</h1>"
            f"{para * (body_chars // 60)}<section class=\"ltx_bibliography\">REFERENCES LIST</section>"
            f"</article></body></html>")


def make_pdf(lines: list[str]) -> bytes:
    """A small valid one-page PDF with real text (Helvetica), built by hand."""
    content = "BT /F1 9 Tf 40 770 Td 11 TL " + " ".join(f"({line}) '" for line in lines) + " ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{obj}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


# --- saved summaries and regenerating -----------------------------------------------

def test_saved_summary_is_returned_without_calling_the_model_again(env):
    bid = add()
    r = summarize(env, bid)
    assert r.status_code == 200
    body = r.json()
    assert body["has_summary"] is True
    assert body["summary"]["tldr"] == "Summary 1"
    assert body["summary_source"] == "web article"
    assert body["summary_model"] == summarizer.MODEL
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", body["summarized_at"])
    assert env.calls[0].title == "A post" and env.calls[0].text == LONG

    again = summarize(env, bid)
    assert again.json()["summary"]["tldr"] == "Summary 1"
    assert len(env.calls) == 1


def test_regenerate_calls_the_model_again(env):
    bid = add()
    summarize(env, bid)
    r = summarize(env, bid, regenerate=True)
    assert r.status_code == 200 and r.json()["summary"]["tldr"] == "Summary 2"
    assert len(env.calls) == 2


def test_lists_flag_summaries_and_single_item_includes_it(env):
    bid, other = add(), add("https://blog.example/other")
    summarize(env, bid)
    rows = {r["id"]: r for r in env.client.get("/api/bookmarks").json()}
    assert rows[bid]["has_summary"] is True and rows[other]["has_summary"] is False
    for row in rows.values():
        assert not {"summary", "summary_json", "full_text"} & row.keys()
    one = env.client.get(f"/api/bookmarks/{bid}").json()
    assert one["summary"]["key_points"] == SUMMARY["key_points"]
    assert env.client.get(f"/api/bookmarks/{other}").json()["summary"] is None
    assert env.client.get("/api/bookmarks/9999").status_code == 404


def test_posts_are_summarized_as_social_posts(env):
    bid = add("https://x.com/u/status/1", platform="x", title="A thread")
    assert summarize(env, bid).json()["summary_source"] == "social post"
    assert env.calls[0].kind == "social post"


# --- errors ---------------------------------------------------------------------------

def test_short_text_returns_422(env):
    bid = add(text="Too short to say anything.")
    r = summarize(env, bid)
    assert r.status_code == 422
    assert r.json()["detail"] == "There isn't enough text here to summarize. Try opening the original."
    assert env.calls == []
    assert db.summary_runs_last_hour()[0] == 0     # nothing counted against the limit


def test_no_api_key_returns_503_but_saved_summaries_still_load(env, monkeypatch):
    bid, other = add(), add("https://blog.example/other")
    summarize(env, bid)
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert summarize(env, bid).status_code == 200
    r = summarize(env, other)
    assert r.status_code == 503 and "ANTHROPIC_API_KEY" in r.json()["detail"]


@pytest.mark.parametrize("error", [RuntimeError("API overloaded"), ValueError("Summary was missing its main parts")])
def test_model_failure_returns_502(env, error):
    env.fake.error = error
    bid = add()
    r = summarize(env, bid)
    assert r.status_code == 502 and r.json()["detail"] == "Couldn't summarize, try again."
    assert db.get(bid)["has_summary"] is False


def test_failed_regenerate_keeps_the_old_summary(env):
    bid = add()
    summarize(env, bid)
    env.fake.error = RuntimeError("API overloaded")
    assert summarize(env, bid, regenerate=True).status_code == 502
    assert db.get(bid)["summary"]["tldr"] == "Summary 1"


def test_unknown_bookmark_returns_404(env):
    assert summarize(env, 9999).status_code == 404


def test_rate_limit_returns_429(env, monkeypatch):
    monkeypatch.setattr(summaries, "RATE_LIMIT", 2)
    a, b, c = add(), add("https://blog.example/2"), add("https://blog.example/3")
    assert summarize(env, a).status_code == 200
    assert summarize(env, a, regenerate=True).status_code == 200     # regenerating counts too
    r = summarize(env, b)
    assert r.status_code == 429
    assert "2 summaries in the last hour" in r.json()["detail"] and "Try again in" in r.json()["detail"]
    assert 0 < int(r.headers["Retry-After"]) <= 3600
    assert len(env.calls) == 2
    # A saved summary is still returned while limited.
    assert summarize(env, a).status_code == 200
    # Runs older than an hour don't count.
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute("UPDATE summary_runs SET created_at = datetime('now', '-61 minutes')")
    assert summarize(env, c).status_code == 200


def test_summary_text_with_html_is_kept_as_plain_text(env):
    hostile = '<img src=x onerror="alert(1)"> <b>bold</b>'
    env.fake.tldr = hostile
    bid = add()
    assert summarize(env, bid).json()["summary"]["tldr"] == hostile        # stored and returned as text
    assert env.client.get(f"/api/bookmarks/{bid}").json()["summary"]["tldr"] == hostile
    # The page renders every summary field with textContent; it never parses HTML.
    script = LIBRARY_HTML[LIBRARY_HTML.index("<script>"):]
    for sink in (".innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "DOMParser"):
        assert sink not in script
    card = script[script.index("function tldrCard"):script.index("async function loadSummary")]
    assert "textContent: String(s.tldr" in card and "textContent: String(s.why_it_matters)" in card


# --- arXiv full text --------------------------------------------------------------------

ABSTRACT = "We show that sparse features explain refusal across model scales. " * 8   # ~540 chars


def test_arxiv_html_full_text_is_used_and_saved_once(env):
    env.arxiv.routes["/html/2610.12345"] = httpx.Response(200, text=paper_html())
    bid = add("https://arxiv.org/abs/2610.12345v2", "arxiv", "A paper", ABSTRACT)
    r = summarize(env, bid)
    assert r.status_code == 200 and r.json()["summary_source"] == "full paper"
    sent = env.calls[0]
    assert sent.kind == "full paper"
    assert "x^{2}" in sent.text and "REFERENCES LIST" not in sent.text and "arXiv menu" not in sent.text
    assert db.get_full_text(bid) == sent.text
    assert [req.url.path for req in env.arxiv.requests] == ["/html/2610.12345"]
    assert env.arxiv.requests[0].headers["User-Agent"] == USER_AGENT

    summarize(env, bid, regenerate=True)
    assert len(env.arxiv.requests) == 1          # stored text reused, no second fetch
    assert env.calls[1].text == sent.text


def test_arxiv_falls_back_to_the_pdf(env):
    lines = [f"Line {i}: the method trains sparse autoencoders on residual activations." for i in range(60)]
    env.arxiv.routes["/pdf/2610.12345"] = httpx.Response(200, content=make_pdf(lines))
    bid = add("https://arxiv.org/abs/2610.12345", "arxiv", "A paper", ABSTRACT)
    assert summarize(env, bid).json()["summary_source"] == "full paper"
    assert [req.url.path for req in env.arxiv.requests] == ["/html/2610.12345", "/pdf/2610.12345"]
    assert "Line 59: the method trains sparse autoencoders" in env.calls[0].text


def test_arxiv_fetch_failure_falls_back_to_the_abstract(env):
    env.arxiv.routes["/pdf/2610.12345"] = httpx.Response(503)
    bid = add("https://arxiv.org/abs/2610.12345", "arxiv", "A paper", ABSTRACT)
    r = summarize(env, bid)
    assert r.status_code == 200 and r.json()["summary_source"] == "paper abstract only"
    assert env.calls[0].kind == "paper abstract only" and env.calls[0].text == ABSTRACT
    assert db.get_full_text(bid) is None          # nothing saved, so it's tried again next time


def test_arxiv_garbage_pdf_falls_back_to_the_abstract(env):
    env.arxiv.routes["/pdf/2610.12345"] = httpx.Response(200, content=b"<html>not a pdf</html>")
    bid = add("https://arxiv.org/abs/2610.12345", "arxiv", "A paper", ABSTRACT)
    assert summarize(env, bid).json()["summary_source"] == "paper abstract only"


def test_arxiv_fetch_failure_with_a_short_abstract_returns_502(env):
    bid = add("https://arxiv.org/abs/2610.12345", "arxiv", "A paper", "Short abstract.")
    r = summarize(env, bid)
    assert r.status_code == 502 and r.json()["detail"] == "Couldn't summarize, try again."
    assert env.calls == []


@pytest.mark.parametrize("url,expected", [
    ("https://arxiv.org/abs/2610.12345", "2610.12345"),
    ("https://arxiv.org/abs/2610.12345v3", "2610.12345"),
    ("http://arxiv.org/pdf/2610.1234v1.pdf", "2610.1234"),
    ("https://arxiv.org/html/2610.12345v2", "2610.12345"),
    ("https://arxiv.org/abs/hep-th/9901001v2", "hep-th/9901001"),
    ("https://example.com/2610.12345", None),
    (None, None),
])
def test_arxiv_id_from_url(url, expected):
    assert arxiv_fulltext.arxiv_id_from_url(url) == expected


def test_real_client_identifies_itself():
    with REAL_CLIENT() as client:
        assert client.headers["User-Agent"] == USER_AGENT
    assert arxiv_fulltext._pacer.interval >= 15     # arxiv.org robots.txt crawl delay


# --- migration -------------------------------------------------------------------------

def test_existing_database_gets_summary_columns(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE bookmarks(id INTEGER PRIMARY KEY, url TEXT UNIQUE NOT NULL, note TEXT, "
                     "created_at TEXT DEFAULT (datetime('now')))")
        conn.execute("INSERT INTO bookmarks(url) VALUES ('https://example.com/old')")
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init()
    with sqlite3.connect(path) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(bookmarks)")}
    assert {"summary_json", "summary_source", "summary_model", "summarized_at", "full_text"} <= cols
    row = db.get(1)
    assert row["summary"] is None and row["has_summary"] is False
