import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import app.db as db
import app.main as web
from app import digest_rank, sources
from app.digest_score import CANDIDATE_LIMIT, WEIGHTS, prescore, top_candidates
from app.jobs import weekly_digest as job
from app.sources import arxiv, hf_papers

FIXTURES = Path(__file__).parent / "fixtures"
_real_rank_papers = digest_rank.rank_papers
NOW = datetime(2026, 10, 15, 8, 0, tzinfo=timezone.utc)
KEYWORDS = ["tokenizer", "multilingual", "low-resource", "language model"]


def fixture(name):
    return (FIXTURES / name).read_text()


class FakeClock:
    def __init__(self):
        self.t, self.sleeps = 100.0, []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def mock_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": sources.USER_AGENT})


# --- arXiv --------------------------------------------------------------------

def test_build_query():
    q = arxiv.build_query(["cs.CL", "cs.LG"], ["tokenizer", 'language "model"'],
                          datetime(2026, 10, 8, tzinfo=timezone.utc), NOW)
    assert q == ('(cat:cs.CL OR cat:cs.LG) AND (ti:"tokenizer" OR abs:"tokenizer" OR '
                 'ti:"language model" OR abs:"language model") AND submittedDate:[202610080000 TO 202610150800]')
    assert "AND (" not in arxiv.build_query(["cs.CL"], [], NOW, NOW).split(") AND ", 1)[1]


def test_parse_feed():
    papers, total = arxiv.parse_feed(fixture("arxiv_page1.xml"))
    assert total == 3
    first = papers[0]
    assert first == {
        "arxiv_id": "2610.10001",
        "title": "Tokenizer Choice Shapes Multilingual Fairness",
        "abstract": "We study how the tokenizer affects low-resource languages.",
        "authors": ["Ana Silva", "Bao Tran", "Chen Wei", "Dara Okafor"],
        "published": "2026-10-14T12:00:00Z",
    }


def test_parse_feed_raises_on_api_error_entry():
    with pytest.raises(RuntimeError, match="incorrect id format"):
        arxiv.parse_feed(fixture("arxiv_error.xml"))


def test_recent_papers_pages_paces_and_sends_user_agent():
    requests = []

    def handler(request):
        requests.append(request)
        start = int(request.url.params["start"])
        return httpx.Response(200, text=fixture("arxiv_page1.xml" if start == 0 else "arxiv_page2.xml"))

    clock = FakeClock()
    pacer = arxiv.Pacer(clock=clock, sleep=clock.sleep)
    papers = arxiv.recent_papers(["cs.CL"], ["tokenizer"], now=NOW, client=mock_client(handler), pacer=pacer)

    assert [p["arxiv_id"] for p in papers] == ["2610.10001", "2610.10002", "2610.10003"]
    assert [int(r.url.params["start"]) for r in requests] == [0, 2]
    assert requests[0].url.params["sortBy"] == "submittedDate"
    assert "submittedDate:[202610080800 TO 202610150800]" in requests[0].url.params["search_query"]
    assert requests[0].headers["user-agent"].startswith("bookmark-agent/")
    assert clock.sleeps == [arxiv.MIN_INTERVAL]  # one wait, before the second request


def test_recent_papers_retries_server_errors_with_pacing():
    responses = iter([httpx.Response(503), httpx.Response(200, text=fixture("arxiv_page2.xml"))])
    clock = FakeClock()
    papers = arxiv.recent_papers(["cs.CL"], [], now=NOW, client=mock_client(lambda r: next(responses)),
                                 pacer=arxiv.Pacer(clock=clock, sleep=clock.sleep), max_results=1)
    assert [p["arxiv_id"] for p in papers] == ["2610.10003"]
    assert clock.sleeps == [arxiv.MIN_INTERVAL]


def test_pacer_waits_only_the_remainder():
    clock = FakeClock()
    pacer = arxiv.Pacer(interval=3.0, clock=clock, sleep=clock.sleep)
    pacer.wait()
    clock.t += 1.0
    pacer.wait()
    clock.t += 5.0
    pacer.wait()
    assert clock.sleeps == [2.0]


def test_user_agent_has_no_contact_by_default():
    assert sources.USER_AGENT == "bookmark-agent/0.1 (personal weekly paper digest)"


# --- Hugging Face -------------------------------------------------------------

def test_hf_upvotes_tolerate_bad_days():
    def handler(request):
        date = request.url.params["date"]
        if date == "2026-10-15":
            return httpx.Response(200, text=fixture("hf_daily_papers.json"))
        if date == "2026-10-14":
            return httpx.Response(500)
        if date == "2026-10-13":
            return httpx.Response(200, text="not json")
        if date == "2026-10-12":
            return httpx.Response(200, json=[{"paper": {"id": "2610.10001", "upvotes": 50}}])
        return httpx.Response(200, json=[])

    votes = hf_papers.recent_upvotes(now=NOW, client=mock_client(handler), sleep=lambda s: None)
    assert votes == {"2610.10001": 50, "2610.99999": 7}


def test_hf_total_failure_means_zero_upvotes():
    def handler(request):
        raise httpx.ConnectError("down")

    assert hf_papers.recent_upvotes(now=NOW, client=mock_client(handler), sleep=lambda s: None) == {}


# --- pre-score ----------------------------------------------------------------

def paper(aid, title="", abstract="", published="2026-10-14T08:00:00Z", upvotes=0):
    return {"arxiv_id": aid, "title": title, "abstract": abstract, "published": published, "upvotes": upvotes}


def test_prescore_parts():
    base = prescore(paper("a"), KEYWORDS, NOW)
    assert base == pytest.approx(WEIGHTS["recency"] * (1 - 1 / 7))
    assert prescore(paper("a", title="A Tokenizer study"), KEYWORDS, NOW) == pytest.approx(base + WEIGHTS["title_keyword"])
    assert prescore(paper("a", abstract="multilingual, low-resource"), KEYWORDS, NOW) == pytest.approx(
        base + 2 * WEIGHTS["abstract_keyword"])
    assert prescore(paper("a", upvotes=9), KEYWORDS, NOW) == pytest.approx(base + WEIGHTS["upvotes"] * 2.302585, rel=1e-4)
    assert prescore(paper("a", title="tokenizers"), KEYWORDS, NOW) == pytest.approx(base)  # whole words only
    assert prescore(paper("a", published="2026-10-01T00:00:00Z"), KEYWORDS, NOW) == 0


def test_top_candidates_orders_and_limits():
    papers = [paper(f"p{i}") for i in range(40)] + [paper("hot", upvotes=200), paper("kw", title="Tokenizer")]
    top = top_candidates(papers, KEYWORDS, now=NOW)
    assert len(top) == CANDIDATE_LIMIT
    assert [p["arxiv_id"] for p in top[:2]] == ["hot", "kw"]
    assert all("prescore" in p for p in top)


# --- job ----------------------------------------------------------------------

@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "bookmarks.db")
    db.init()
    config = tmp_path / "watchlists.json"
    config.write_text(json.dumps([
        {"name": "llm-language", "domain": "LLMs and language", "arxiv_categories": ["cs.CL"],
         "keywords": KEYWORDS, "top_n": 2},
        {"name": "other", "domain": "Other things", "arxiv_categories": ["cs.LG"], "keywords": [], "top_n": 1},
    ]))
    monkeypatch.setattr(job, "CONFIG_PATH", config)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(digest_rank, "USE_AI", False)

    papers = arxiv.parse_feed(fixture("arxiv_page1.xml"))[0] + arxiv.parse_feed(fixture("arxiv_page2.xml"))[0]
    monkeypatch.setattr(arxiv, "recent_papers", lambda cats, kws: [dict(p) for p in papers])
    monkeypatch.setattr(hf_papers, "recent_upvotes", lambda: {"2610.10001": 42})

    calls = []

    def fake_rank(domain, candidates, top_n=10):
        calls.append({"domain": domain, "ids": [c["arxiv_id"] for c in candidates], "top_n": top_n,
                      "upvotes": {c["arxiv_id"]: c["upvotes"] for c in candidates}})
        return [
            {"arxiv_id": "2610.10003", "why": "First Yoruba LM at this scale."},
            {"arxiv_id": "2610.10003", "why": "duplicate"},
            {"arxiv_id": "2610.10001", "why": "Shows tokenizers drive fairness gaps."},
        ][:top_n + 1]

    monkeypatch.setattr(digest_rank, "rank_papers", fake_rank)
    return calls


def test_dry_run_prints_picks_and_saves_nothing(env, capsys):
    assert job.main(["--watchlist", "llm-language", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "1. A Language Model for Yoruba" in out
    assert "2. Tokenizer Choice Shapes Multilingual Fairness" in out
    assert "https://arxiv.org/abs/2610.10001  (upvotes: 42)" in out
    assert "dry run" in out
    assert db.list_all() == [] and db.latest_digests() == []
    assert env[0]["domain"] == "LLMs and language" and env[0]["top_n"] == 2
    assert env[0]["upvotes"]["2610.10001"] == 42


def test_run_saves_bookmarks_and_digest(env):
    assert job.main(["--watchlist", "llm-language"]) == 0
    rows = {r["url"]: r for r in db.list_all()}
    assert set(rows) == {"https://arxiv.org/abs/2610.10003", "https://arxiv.org/abs/2610.10001"}
    tok = rows["https://arxiv.org/abs/2610.10001"]
    assert tok["platform"] == "arxiv" and tok["status"] == "ready"
    assert tok["title"] == "Tokenizer Choice Shapes Multilingual Fairness"
    assert tok["author"] == "Ana Silva, Bao Tran, Chen Wei et al."
    assert tok["text"] == "We study how the tokenizer affects low-resource languages."
    assert tok["note"] == "Shows tokenizers drive fairness gaps."
    assert rows["https://arxiv.org/abs/2610.10003"]["author"] == "Femi Ade, Grace Obi"

    [digest] = db.latest_digests()
    assert digest["watchlist"] == "llm-language"
    assert [(i["rank"], i["url"], i["why"]) for i in digest["items"]] == [
        (1, "https://arxiv.org/abs/2610.10003", "First Yoruba LM at this scale."),
        (2, "https://arxiv.org/abs/2610.10001", "Shows tokenizers drive fairness gaps."),
    ]


def test_existing_bookmark_is_reused_and_keeps_its_note(env):
    bid, _ = db.insert("https://arxiv.org/abs/2610.10001", "my own note", "web")
    job.main(["--watchlist", "llm-language"])
    assert len(db.list_all()) == 2
    assert db.get(bid)["note"] == "my own note"
    items = db.latest_digests("llm-language")[0]["items"]
    assert items[1]["id"] == bid and items[1]["why"] == "Shows tokenizers drive fairness gaps."


def test_latest_digest_per_watchlist_and_api(env):
    job.main([])
    job.main(["--watchlist", "llm-language"])
    digests = db.latest_digests()
    assert sorted(d["watchlist"] for d in digests) == ["llm-language", "other"]
    assert len(db.list_all()) == 2  # second run reused the same bookmarks

    body = TestClient(web.app).get("/api/digests/latest", params={"watchlist": "other"}).json()
    [d] = body["digests"]
    assert d["watchlist"] == "other" and [i["rank"] for i in d["items"]] == [1]


def test_no_candidates_skips_rank(env, monkeypatch, capsys):
    monkeypatch.setattr(arxiv, "recent_papers", lambda cats, kws: [])
    assert job.main(["--watchlist", "other"]) == 0
    assert env == [] and "No candidates" in capsys.readouterr().out


def test_failure_in_one_watchlist_does_not_stop_others(env, monkeypatch):
    real = arxiv.parse_feed(fixture("arxiv_page2.xml"))[0]

    def flaky(cats, kws):
        if cats == ["cs.CL"]:
            raise RuntimeError("arXiv down")
        return [dict(p) for p in real]

    monkeypatch.setattr(arxiv, "recent_papers", flaky)
    assert job.main([]) == 1
    assert [d["watchlist"] for d in db.latest_digests()] == ["other"]


def test_ai_without_key_warns_and_still_runs(env, monkeypatch, caplog):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.setattr(digest_rank, "USE_AI", True)
    assert job.main(["--watchlist", "other", "--dry-run"]) == 0
    assert "ANTHROPIC_API_KEY is not set" in caplog.text
    assert len(env) == 1


def test_real_rank_papers_without_ai_works_with_job(env, monkeypatch, capsys):
    # The default (non-AI) path of the real rank_papers, with no network or API calls.
    monkeypatch.setattr(digest_rank, "rank_papers", _real_rank_papers)
    monkeypatch.setattr(digest_rank, "USE_AI", False)
    assert job.main(["--watchlist", "llm-language", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "1. Tokenizer Choice Shapes Multilingual Fairness" in out
    assert "We study how the tokenizer affects low-resource languages." in out


@pytest.mark.parametrize("config, message", [
    ("[", "not valid JSON"),
    ("[]", "non-empty JSON list"),
    ('[{"name": "a", "domain": "d", "arxiv_categories": []}]', "arxiv_categories"),
    ('[{"name": "a", "domain": "d", "arxiv_categories": ["cs.CL"], "top_n": 0}]', "top_n"),
    ('[{"name": "a", "domain": "d", "arxiv_categories": ["cs.CL"]},'
     ' {"name": "a", "domain": "d", "arxiv_categories": ["cs.CL"]}]', "duplicate"),
])
def test_config_validation(tmp_path, config, message):
    path = tmp_path / "w.json"
    path.write_text(config)
    with pytest.raises(ValueError, match=message):
        job.load_watchlists(path)


def test_shipped_config_is_valid():
    watchlists = job.load_watchlists()
    assert watchlists
    assert all(w["arxiv_categories"] and 1 <= w["top_n"] <= CANDIDATE_LIMIT for w in watchlists)


# --- ranking method label -----------------------------------------------------

def test_ranked_by_prescore_when_ai_is_off(env):
    job.main(["--watchlist", "llm-language"])
    assert db.latest_digests()[0]["ranked_by"] == "prescore"


def test_ranked_by_ai(env, monkeypatch):
    monkeypatch.setattr(digest_rank, "USE_AI", True)
    job.main(["--watchlist", "llm-language"])
    body = TestClient(web.app).get("/api/digests/latest").json()
    assert body["digests"][0]["ranked_by"] == "ai"


def test_ranked_by_detects_silent_ai_fallback(env, monkeypatch):
    monkeypatch.setattr(digest_rank, "USE_AI", True)
    monkeypatch.setattr(digest_rank, "rank_papers",
                        lambda domain, candidates, top_n=10: digest_rank.rank_without_ai(candidates, top_n))
    job.main(["--watchlist", "llm-language"])
    assert db.latest_digests()[0]["ranked_by"] == "prescore_fallback"


def test_old_digests_table_gets_ranked_by_column(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "old.db")
    with db.conn() as c:
        c.execute("CREATE TABLE digests(id INTEGER PRIMARY KEY, watchlist TEXT NOT NULL, "
                  "run_at TEXT NOT NULL DEFAULT (datetime('now')))")
        c.execute("INSERT INTO digests(watchlist) VALUES ('old')")
    db.init()
    [d] = db.latest_digests()
    assert d["watchlist"] == "old" and d["ranked_by"] is None
