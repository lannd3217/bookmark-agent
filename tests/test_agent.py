import json
import logging
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.db as db
import app.main as web
from app import agent
from app.claude_log import instrument


# --- fake Anthropic client ----------------------------------------------------

def text(t):
    return SimpleNamespace(type="text", text=t)


def tool_use(id, name, input):
    return SimpleNamespace(type="tool_use", id=id, name=name, input=input)


def response(stop_reason, *blocks):
    return SimpleNamespace(
        stop_reason=stop_reason, content=list(blocks), model="fake-model",
        usage=SimpleNamespace(input_tokens=12, output_tokens=3),
    )


class FakeClient:
    """Replays scripted responses; `script` is a list, or a function of the call number."""

    def __init__(self, script):
        self.script = script
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        # Copy: agent.ask keeps appending to the same list after the call.
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        n = len(self.calls) - 1
        return self.script(n) if callable(self.script) else self.script[n]


@pytest.fixture
def library(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "bookmarks.db")
    db.init()
    ids = {}
    for key, url, title, created in [
        ("tok", "https://example.com/tokenizer", "The tokenizer tax", "2024-01-02"),
        ("probe", "https://arxiv.org/abs/1", "Probe generalisation", "2024-03-04"),
    ]:
        ids[key], _ = db.insert(url, None, "web", title, created)
        db.update_extracted(ids[key], {"title": title, "text": f"Body of {title}"})
    return ids


def use_fake(monkeypatch, script):
    fake = FakeClient(script)
    monkeypatch.setattr(agent, "client", fake)
    return fake


def tool_results(call):
    """The tool_result blocks the agent sent in this call's last message."""
    return call["messages"][-1]["content"]


# --- agent loop ---------------------------------------------------------------

def test_direct_answer(library, monkeypatch):
    fake = use_fake(monkeypatch, [response("end_turn", text("Hello! "), text("Nothing to look up."))])
    result = agent.ask("hi")
    assert result == {"answer": "Hello! Nothing to look up.", "steps": 1, "item_ids": []}
    assert len(fake.calls) == 1
    assert fake.calls[0]["messages"] == [{"role": "user", "content": "hi"}]
    assert {t["name"] for t in fake.calls[0]["tools"]} == {"search_library", "get_item", "get_weekly_digest"}


def test_one_tool_call_then_answer(library, monkeypatch):
    fake = use_fake(monkeypatch, [
        response("tool_use", text("Let me search."), tool_use("tu_1", "search_library", {"query": "tokenizer"})),
        response("end_turn", text(f"See [{library['tok']}] The tokenizer tax.")),
    ])
    result = agent.ask("what did I save about tokenizers?")

    assert result["steps"] == 2
    assert result["item_ids"] == [library["tok"]]
    assert result["answer"].startswith(f"See [{library['tok']}]")

    second = fake.calls[1]["messages"]
    assert second[1]["role"] == "assistant"
    [res] = tool_results(fake.calls[1])
    assert res["type"] == "tool_result" and res["tool_use_id"] == "tu_1"
    found = json.loads(res["content"])
    assert [r["id"] for r in found] == [library["tok"]]
    assert set(found[0]) == {"id", "title", "url", "platform", "created_at"}


def test_unknown_tool_returns_error_and_continues(library, monkeypatch):
    fake = use_fake(monkeypatch, [
        response("tool_use", tool_use("tu_1", "delete_everything", {})),
        response("end_turn", text("I can't do that.")),
    ])
    result = agent.ask("delete my library")
    [res] = tool_results(fake.calls[1])
    assert json.loads(res["content"]) == {"error": "Unknown tool: delete_everything"}
    assert result == {"answer": "I can't do that.", "steps": 2, "item_ids": []}
    assert len(db.list_all()) == 2


def test_step_limit(library, monkeypatch):
    fake = use_fake(monkeypatch, lambda n: response(
        "tool_use", tool_use(f"tu_{n}", "get_item", {"id": library["probe"]})))
    result = agent.ask("loop forever")
    assert len(fake.calls) == agent.MAX_STEPS
    assert result["steps"] == agent.MAX_STEPS
    assert "step limit" in result["answer"]
    assert result["item_ids"] == [library["probe"]]


@pytest.fixture
def digest(library):
    """Two watchlists' latest digests; returns the bookmark ids by watchlist."""
    db.save_digest("alignment", [
        {"url": "https://arxiv.org/abs/2610.00001", "platform": "arxiv", "title": "Reward hacking study",
         "author": None, "text": "abstract", "note": None, "rank": 1, "why": "Shows a new failure mode."},
    ])
    db.save_digest("interpretability", [
        {"url": "https://arxiv.org/abs/2610.00002", "platform": "arxiv", "title": "SAE features",
         "author": None, "text": "abstract", "note": None, "rank": 1, "why": "Better SAE evals."},
    ])
    return {r["watchlist"]: r["id"] for r in db.latest_digest_items()}


def test_get_weekly_digest_for_one_watchlist(digest, monkeypatch):
    fake = use_fake(monkeypatch, [
        response("tool_use", tool_use("tu_1", "get_weekly_digest", {"watchlist": "alignment"})),
        response("end_turn", text(f"This week: [{digest['alignment']}] Reward hacking study.")),
    ])
    result = agent.ask("what's new in alignment this week?")

    [res] = tool_results(fake.calls[1])
    rows = json.loads(res["content"])
    assert [(r["watchlist"], r["rank"], r["title"], r["why"]) for r in rows] == [
        ("alignment", 1, "Reward hacking study", "Shows a new failure mode."),
    ]
    assert result["item_ids"] == [digest["alignment"]]
    assert result["steps"] == 2
    assert "Today is " in fake.calls[0]["system"]


def test_get_weekly_digest_without_watchlist_returns_all(digest, monkeypatch):
    fake = use_fake(monkeypatch, [
        response("tool_use", tool_use("tu_1", "get_weekly_digest", {})),
        response("end_turn", text("Here is this week's digest.")),
    ])
    result = agent.ask("top papers this week?")
    rows = json.loads(tool_results(fake.calls[1])[0]["content"])
    assert [r["watchlist"] for r in rows] == ["alignment", "interpretability"]
    assert result["item_ids"] == sorted(digest.values())


def test_get_weekly_digest_with_no_digest_yet(library, monkeypatch):
    fake = use_fake(monkeypatch, [
        response("tool_use", tool_use("tu_1", "get_weekly_digest", {})),
        response("end_turn", text("There's no digest yet.")),
    ])
    result = agent.ask("this week's papers?")
    assert json.loads(tool_results(fake.calls[1])[0]["content"]) == []
    assert result["item_ids"] == []


# --- /api/ask -----------------------------------------------------------------

@pytest.fixture
def api(library):
    return TestClient(web.app)


def test_ask_without_api_key_is_503(api, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    fake = use_fake(monkeypatch, [])
    r = api.post("/api/ask", json={"question": "anything?"})
    assert r.status_code == 503
    assert "ANTHROPIC_API_KEY" in r.json()["detail"]
    assert fake.calls == []


@pytest.mark.parametrize("question", ["", "   ", "x" * 501])
def test_ask_rejects_bad_question(api, monkeypatch, question):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    fake = use_fake(monkeypatch, [])
    assert api.post("/api/ask", json={"question": question}).status_code == 422
    assert fake.calls == []


def test_ask_returns_answer_and_cited_items(api, library, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    use_fake(monkeypatch, [
        response("tool_use", tool_use("tu_1", "search_library", {"query": "probe"})),
        response("end_turn", text(f"[{library['probe']}] Probe generalisation <b>bold</b>")),
    ])
    r = api.post("/api/ask", json={"question": "probes?"})
    assert r.status_code == 200
    body = r.json()
    assert body["answer"].endswith("<b>bold</b>")  # escaping is the page's job (textContent)
    assert body["items"] == [{
        "id": library["probe"], "title": "Probe generalisation",
        "url": "https://arxiv.org/abs/1", "platform": "web",
    }]


# --- logging ------------------------------------------------------------------

def test_instrument_logs_model_tokens_and_latency(caplog):
    fake = instrument(FakeClient([response("end_turn", text("ok"))]))
    with caplog.at_level(logging.INFO, logger="claude"):
        fake.messages.create(model="fake-model", messages=[])
    [line] = [r.getMessage() for r in caplog.records if r.name == "claude"]
    assert "model=fake-model" in line
    assert "input_tokens=12" in line and "output_tokens=3" in line
    assert "latency_ms=" in line
    assert instrument(fake) is fake  # wrapping twice is a no-op
