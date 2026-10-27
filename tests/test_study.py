import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.db as db
import app.main as web
import app.study as study
from app import study_notes

STUDY_HTML = (Path(__file__).parent.parent / "static" / "study.html").read_text()
LIBRARY_HTML = (Path(__file__).parent.parent / "static" / "library.html").read_text()


def models_data(tag="v1"):
    return {"models": [{"name": f"Model {i} {tag}", "idea": "An idea [1].", "why_experts_use_it": "Because.",
                        "example": "For example.", "sources": [1]} for i in range(1, 6)]}


def debates_data():
    side = {"position": "A view", "strongest_argument": "An argument [1].", "sources": [1]}
    return {"debates": [{"question": f"Debate {i}", "side_a": side, "side_b": side, "what_would_settle_it": "A test."}
                        for i in range(1, 4)], "coverage_note": "Sources cover this well."}


def questions_data():
    return {"questions": [{"number": i, "question": f"Question {i}?", "type": "explain",
                           "deep_answer_includes": [f"secret point {i}"], "shallow_answer": f"secret shallow {i}",
                           "sources": [1]} for i in range(1, 11)]}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """SQLite in tmp_path, an API key set, and fake study functions that record their inputs."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "bookmarks.db")
    db.init()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    calls = SimpleNamespace(models=[], debates=[], questions=[], feedback=[], fail=None, tag="v1")

    def maybe_fail():
        if calls.fail:
            raise calls.fail

    def fake_models(topic, sources):
        calls.models.append((topic, sources)); maybe_fail()
        return models_data(calls.tag)

    def fake_debates(topic, sources, models):
        calls.debates.append(models); maybe_fail()
        return debates_data()

    def fake_questions(topic, sources, models, debates):
        calls.questions.append((models, debates)); maybe_fail()
        return questions_data()

    def fake_feedback(topic, sources, question, answer):
        calls.feedback.append((question, answer)); maybe_fail()
        return {"verdict": "partial", "what_you_got_right": ["One thing."], "what_is_wrong": [],
                "what_you_are_missing": ["Another thing."], "model_answer": "A model answer.", "follow_up": "Why?"}

    monkeypatch.setattr(study, "mental_models", fake_models)
    monkeypatch.setattr(study, "disagreements", fake_debates)
    monkeypatch.setattr(study, "study_questions", fake_questions)
    monkeypatch.setattr(study, "feedback", fake_feedback)
    return SimpleNamespace(client=TestClient(web.app), calls=calls)


def add(url, title="A source", text="Some text about sparse features. " * 10):
    bid, _ = db.insert(url, None, "web", title)
    db.update_extracted(bid, {"title": title, "text": text})
    return bid


def new_note(env, n=2, topic="Interpretability"):
    ids = [add(f"https://example.com/{i}", f"Source {i}") for i in range(n)]
    r = env.client.post("/api/study", json={"topic": topic, "bookmark_ids": ids})
    assert r.status_code == 201, r.text
    return r.json()["id"], ids


def step(env, nid, n, **body):
    return env.client.post(f"/api/study/{nid}/steps/{n}", json=body or None)


def run_through(env, nid, last=3):
    for n in range(1, last + 1):
        assert step(env, nid, n).status_code == 200


# --- creating notes -------------------------------------------------------------

def test_create_note_and_read_it_back(env):
    nid, ids = new_note(env)
    note = env.client.get(f"/api/study/{nid}").json()
    assert note["topic"] == "Interpretability"
    assert [s["id"] for s in note["sources"]] == ids
    assert note["steps"] == {"models": None, "debates": None, "questions": None}
    assert note["progress"] == {"steps_done": 0, "answered": 0, "questions": 0}


@pytest.mark.parametrize("body,status", [
    ({"topic": "   ", "bookmark_ids": [1]}, 422),
    ({"topic": "x" * 81, "bookmark_ids": [1]}, 422),
    ({"topic": "T", "bookmark_ids": []}, 422),
])
def test_create_note_validation(env, body, status):
    add("https://example.com/1")
    assert env.client.post("/api/study", json=body).status_code == status


def test_create_note_rejects_unknown_and_too_many_sources(env):
    a = add("https://example.com/a")
    r = env.client.post("/api/study", json={"topic": "T", "bookmark_ids": [a, 9999]})
    assert r.status_code == 400 and "9999" in r.json()["detail"]
    ids = [add(f"https://example.com/{i}") for i in range(26)]
    r = env.client.post("/api/study", json={"topic": "T", "bookmark_ids": ids})
    assert r.status_code == 400 and "up to 25" in r.json()["detail"]


# --- steps ----------------------------------------------------------------------

def test_steps_must_run_in_order(env):
    nid, _ = new_note(env)
    r = step(env, nid, 2)
    assert r.status_code == 409 and r.json()["detail"] == "Finish step 1 first."
    assert step(env, nid, 1).status_code == 200
    assert step(env, nid, 3).status_code == 409
    assert env.client.post(f"/api/study/{nid}/answers/1", json={"answer": "An answer"}).status_code == 409
    assert step(env, nid, 4).status_code == 422
    assert step(env, 9999, 1).status_code == 404


def test_step_uses_the_notes_sources_and_saves_its_result(env):
    nid, ids = new_note(env)
    note = step(env, nid, 1).json()
    topic, sources = env.calls.models[0]
    assert topic == "Interpretability"
    assert f"[{ids[0]}] Source 0" in sources and f"[{ids[1]}] Source 1" in sources
    assert note["steps"]["models"]["models"][0]["name"] == "Model 1 v1"
    assert note["progress"]["steps_done"] == 1


def test_saved_step_is_returned_without_calling_the_model_again(env):
    nid, _ = new_note(env)
    step(env, nid, 1)
    again = step(env, nid, 1)
    assert again.status_code == 200 and again.json()["steps"]["models"]["models"][0]["name"] == "Model 1 v1"
    assert len(env.calls.models) == 1


def test_regenerating_a_step_clears_later_steps_and_answers(env):
    nid, _ = new_note(env)
    run_through(env, nid)
    env.client.post(f"/api/study/{nid}/answers/1", json={"answer": "My answer"})
    env.calls.tag = "v2"
    note = step(env, nid, 1, regenerate=True).json()
    assert len(env.calls.models) == 2
    assert note["steps"]["models"]["models"][0]["name"] == "Model 1 v2"
    assert note["steps"]["debates"] is None and note["steps"]["questions"] is None
    assert note["answers"] == {}
    assert note["progress"] == {"steps_done": 1, "answered": 0, "questions": 0}
    # Step 2 now builds on the new models.
    step(env, nid, 2)
    assert env.calls.debates[-1]["models"][0]["name"] == "Model 1 v2"


def test_regenerating_questions_clears_answers_only(env):
    nid, _ = new_note(env)
    run_through(env, nid)
    env.client.post(f"/api/study/{nid}/answers/1", json={"answer": "My answer"})
    note = step(env, nid, 3, regenerate=True).json()
    assert note["steps"]["models"] and note["steps"]["debates"] and note["steps"]["questions"]
    assert note["answers"] == {}


def test_model_failure_returns_502_and_keeps_the_old_result(env):
    nid, _ = new_note(env)
    step(env, nid, 1)
    env.calls.fail = ValueError("Study step returned an incomplete 'models' list")
    r = step(env, nid, 1, regenerate=True)
    assert r.status_code == 502 and r.json()["detail"] == "Couldn't finish this step. Try again."
    assert db.get_study_note(nid)["models"]["models"][0]["name"] == "Model 1 v1"


def test_no_api_key_returns_503_but_saved_steps_still_load(env, monkeypatch):
    nid, _ = new_note(env)
    step(env, nid, 1)
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert step(env, nid, 1).status_code == 200
    r = step(env, nid, 2)
    assert r.status_code == 503 and "ANTHROPIC_API_KEY" in r.json()["detail"]


def test_deleted_sources_are_skipped(env):
    nid, ids = new_note(env)
    db.delete(ids[0])
    note = env.client.get(f"/api/study/{nid}").json()
    assert [s["id"] for s in note["sources"]] == [ids[1]]
    assert step(env, nid, 1).status_code == 200
    assert f"[{ids[0]}]" not in env.calls.models[0][1]


# --- step 4: answers and feedback ------------------------------------------------

def test_hidden_question_fields_appear_only_after_answering(env):
    nid, _ = new_note(env)
    note = None
    for n in (1, 2, 3):
        note = step(env, nid, n).json()
    for q in note["steps"]["questions"]["questions"]:
        assert "deep_answer_includes" not in q and "shallow_answer" not in q
    assert "secret" not in env.client.get(f"/api/study/{nid}").text

    note = env.client.post(f"/api/study/{nid}/answers/2", json={"answer": "  Because features overlap.  "}).json()
    a = note["answers"]["2"]
    assert a["answer"] == "Because features overlap."
    assert a["verdict"] == "partial" and a["feedback"]["model_answer"] == "A model answer."
    assert a["deep_answer_includes"] == ["secret point 2"] and a["shallow_answer"] == "secret shallow 2"
    assert "secret point 1" not in env.client.get(f"/api/study/{nid}").text     # still hidden for question 1
    question, answer = env.calls.feedback[0]
    assert question["number"] == 2 and question["deep_answer_includes"] == ["secret point 2"]


def test_try_again_overwrites_and_counts_attempts(env):
    nid, _ = new_note(env)
    run_through(env, nid)
    env.client.post(f"/api/study/{nid}/answers/1", json={"answer": "First try"})
    note = env.client.post(f"/api/study/{nid}/answers/1", json={"answer": "Second try"}).json()
    assert note["answers"]["1"]["answer"] == "Second try" and note["answers"]["1"]["attempts"] == 2
    assert note["progress"]["answered"] == 1


def test_answer_validation(env):
    nid, _ = new_note(env)
    run_through(env, nid)
    assert env.client.post(f"/api/study/{nid}/answers/11", json={"answer": "x"}).status_code == 400
    assert env.client.post(f"/api/study/{nid}/answers/0", json={"answer": "x"}).status_code == 422
    assert env.client.post(f"/api/study/{nid}/answers/1", json={"answer": "   "}).status_code == 422
    assert env.client.post(f"/api/study/{nid}/answers/1", json={"answer": "x" * 4001}).status_code == 422


def test_feedback_failure_returns_502_and_saves_nothing(env):
    nid, _ = new_note(env)
    run_through(env, nid)
    env.calls.fail = ValueError("Feedback was incomplete")
    r = env.client.post(f"/api/study/{nid}/answers/1", json={"answer": "My answer"})
    assert r.status_code == 502 and r.json()["detail"] == "Couldn't check your answer. Try again."
    assert env.client.get(f"/api/study/{nid}").json()["answers"] == {}


def test_answering_everything_completes_step_4(env):
    nid, _ = new_note(env)
    run_through(env, nid)
    for q in range(1, 11):
        note = env.client.post(f"/api/study/{nid}/answers/{q}", json={"answer": f"Answer {q}"}).json()
    assert note["progress"] == {"steps_done": 4, "answered": 10, "questions": 10}
    row = env.client.get("/api/study").json()["notes"][0]
    assert row["steps_done"] == 4 and row["answered"] == 10


# --- rate limit -------------------------------------------------------------------

def test_study_rate_limit_returns_429(env, monkeypatch):
    monkeypatch.setattr(study_notes, "RATE_LIMIT", 2)
    nid, _ = new_note(env)
    assert step(env, nid, 1).status_code == 200
    assert step(env, nid, 2).status_code == 200
    r = step(env, nid, 3)
    assert r.status_code == 429
    assert "study limit of 2 AI requests" in r.json()["detail"] and "Try again in" in r.json()["detail"]
    assert 0 < int(r.headers["Retry-After"]) <= 3600
    assert len(env.calls.questions) == 0
    assert step(env, nid, 1).status_code == 200          # saved results still load while limited
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute("UPDATE study_runs SET created_at = datetime('now', '-61 minutes')")
    assert step(env, nid, 3).status_code == 200


def test_study_limit_is_60_and_separate_from_summaries(env):
    assert study_notes.RATE_LIMIT == 60
    bid = add("https://example.com/s")
    for _ in range(5):
        db.record_summary_run(bid, 100)
    assert db.study_runs_last_hour()[0] == 0          # summaries don't use up the study limit
    nid, _ = new_note(env)
    step(env, nid, 1)
    assert db.summary_runs_last_hour()[0] == 5        # and study requests don't use up the summary limit
    assert db.study_runs_last_hour()[0] == 1


# --- list and delete --------------------------------------------------------------

def test_list_notes_with_progress(env):
    a, _ = new_note(env, topic="First")
    b, _ = new_note(env, n=3, topic="Second")
    with sqlite3.connect(db.DB_PATH) as conn:      # timestamps have one-second resolution
        conn.execute("UPDATE study_notes SET updated_at = datetime('now', '-1 hour')")
    run_through(env, a, last=2)
    notes = env.client.get("/api/study").json()["notes"]
    by_id = {n["id"]: n for n in notes}
    assert by_id[a]["steps_done"] == 2 and by_id[a]["source_count"] == 2 and by_id[a]["topic"] == "First"
    assert by_id[b]["steps_done"] == 0 and by_id[b]["source_count"] == 3
    assert notes[0]["id"] == a          # most recently changed first


def test_delete_note(env):
    nid, _ = new_note(env)
    run_through(env, nid)
    env.client.post(f"/api/study/{nid}/answers/1", json={"answer": "My answer"})
    assert env.client.delete(f"/api/study/{nid}").status_code == 200
    assert env.client.get(f"/api/study/{nid}").status_code == 404
    assert env.client.delete(f"/api/study/{nid}").status_code == 404
    with sqlite3.connect(db.DB_PATH) as conn:
        assert conn.execute("SELECT COUNT(*) FROM study_answers").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM study_note_sources").fetchone()[0] == 0


# --- pages ----------------------------------------------------------------------

def test_study_pages_are_served(env):
    for path in ("/study", "/study/1"):
        r = env.client.get(path)
        assert r.status_code == 200 and "<title>Study · Personal Knowledge Collection</title>" in r.text


def test_ai_text_with_html_is_kept_as_plain_text(env, monkeypatch):
    hostile = '<img src=x onerror="alert(1)"> <b>bold</b>'
    monkeypatch.setattr(study, "mental_models", lambda topic, sources: {"models": [
        {"name": hostile, "idea": hostile, "why_experts_use_it": "x", "example": "x", "sources": []}] * 5})
    nid, _ = new_note(env, topic=hostile)
    note = step(env, nid, 1).json()
    assert note["topic"] == hostile and note["steps"]["models"]["models"][0]["name"] == hostile
    # The pages render server data with textContent only; they never parse HTML.
    for page in (STUDY_HTML, LIBRARY_HTML):
        script = page[page.index("<script>"):]
        for sink in (".innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "DOMParser"):
            assert sink not in script


# --- regression: "Check my answer" stayed disabled for short answers ---------------

def test_short_answers_can_be_checked_and_page_uses_the_servers_minimum(env):
    """The page required 15 characters while the API accepted 1, so a short answer like
    "I'm not sure" left the button disabled with no explanation."""
    nid, _ = new_note(env)
    run_through(env, nid)
    note = env.client.get(f"/api/study/{nid}").json()
    assert note["limits"] == {"answer_min": study_notes.ANSWER_MIN, "answer_max": study_notes.ANSWER_MAX}
    assert study_notes.ANSWER_MIN == 1
    r = env.client.post(f"/api/study/{nid}/answers/10", json={"answer": "No"})
    assert r.status_code == 200 and r.json()["answers"]["10"]["answer"] == "No"
    # The page decides "can check" from the server's limit, not its own number.
    script = STUDY_HTML[STUDY_HTML.index("<script>"):]
    assert "const canCheck = text => str(text).trim().length >= answerMin();" in script
    assert "disabled: !canCheck(ta.value)" in script and "check.disabled = !canCheck(ta.value)" in script
    import re
    assert not re.search(r"\.length\s*<\s*\d{2}", script), "a hard-coded minimum answer length is back in study.html"


# --- drafts -----------------------------------------------------------------------

def put_draft(env, nid, q, text):
    return env.client.put(f"/api/study/{nid}/drafts/{q}", json={"text": text})


def test_draft_save_load_overwrite_and_clear(env):
    nid, _ = new_note(env)
    run_through(env, nid)
    r = put_draft(env, nid, 2, "Half an idea about superposition")
    assert r.status_code == 200
    body = r.json()
    assert body["q"] == 2 and body["text"] == "Half an idea about superposition" and body["saved_at"]
    drafts = env.client.get(f"/api/study/{nid}").json()["drafts"]
    assert drafts == {"2": {"text": "Half an idea about superposition", "saved_at": body["saved_at"]}}
    put_draft(env, nid, 2, "A fuller idea")
    assert env.client.get(f"/api/study/{nid}").json()["drafts"]["2"]["text"] == "A fuller idea"
    r = put_draft(env, nid, 2, "   ")                 # clearing the box deletes the draft
    assert r.json() == {"q": 2, "text": "", "saved_at": None}
    assert env.client.get(f"/api/study/{nid}").json()["drafts"] == {}


def test_drafts_are_not_attempts_and_never_reveal_the_answer_key(env):
    nid, _ = new_note(env)
    run_through(env, nid)
    runs_before = db.study_runs_last_hour()[0]
    put_draft(env, nid, 1, "My draft")
    note = env.client.get(f"/api/study/{nid}").json()
    assert note["answers"] == {} and note["progress"]["answered"] == 0
    assert "secret" not in env.client.get(f"/api/study/{nid}").text
    assert env.calls.feedback == []                                  # no AI call
    assert db.study_runs_last_hour()[0] == runs_before               # not counted against the study limit
    note = env.client.post(f"/api/study/{nid}/answers/1", json={"answer": "Checked"}).json()
    assert note["answers"]["1"]["attempts"] == 1                     # the draft didn't count as an attempt


def test_checking_an_answer_clears_only_its_draft(env):
    nid, _ = new_note(env)
    run_through(env, nid)
    put_draft(env, nid, 1, "Draft one")
    put_draft(env, nid, 2, "Draft two")
    note = env.client.post(f"/api/study/{nid}/answers/1", json={"answer": "Draft one, checked"}).json()
    assert set(note["drafts"]) == {"2"}


def test_regenerating_clears_drafts(env):
    nid, _ = new_note(env)
    run_through(env, nid)
    put_draft(env, nid, 3, "A draft")
    note = step(env, nid, 3, regenerate=True).json()
    assert note["drafts"] == {}


def test_draft_validation(env):
    nid, _ = new_note(env)
    run_through(env, nid, last=2)
    r = put_draft(env, nid, 1, "Too early")
    assert r.status_code == 409 and r.json()["detail"] == "Finish step 3 first."
    step(env, nid, 3)
    assert put_draft(env, nid, 11, "x").status_code == 400
    assert put_draft(env, nid, 0, "x").status_code == 422
    assert put_draft(env, nid, 1, "x" * 4001).status_code == 422
    assert put_draft(env, nid, 1, "x" * 4000).status_code == 200
    assert put_draft(env, 9999, 1, "x").status_code == 404


def test_list_shows_progress_and_drafts(env):
    nid, _ = new_note(env)
    run_through(env, nid)
    env.client.post(f"/api/study/{nid}/answers/1", json={"answer": "One"})
    put_draft(env, nid, 2, "Two, in progress")
    row = env.client.get("/api/study").json()["notes"][0]
    assert {k: row[k] for k in ("steps_done", "answered", "has_questions", "drafts")} == \
        {"steps_done": 3, "answered": 1, "has_questions": True, "drafts": 1}


def test_drafts_table_is_added_without_touching_existing_study_tables(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "old.db")
    db.init()
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute("DROP TABLE study_drafts")          # a database from before this change
        before = {t: conn.execute(f"PRAGMA table_info({t})").fetchall()
                  for t in ("study_notes", "study_note_sources", "study_answers", "study_runs")}
    db.init()
    with sqlite3.connect(db.DB_PATH) as conn:
        after = {t: conn.execute(f"PRAGMA table_info({t})").fetchall() for t in before}
        cols = [r[1] for r in conn.execute("PRAGMA table_info(study_drafts)")]
    assert after == before
    assert cols == ["note_id", "q_number", "text", "saved_at"]


def test_draft_sql_is_postgres_compatible(monkeypatch):
    """No Postgres here, so record the SQL the draft functions send in Postgres mode."""
    sent = []

    class Rows(list):
        def fetchone(self):
            return self[0] if self else None

    class RecordingConn:
        def execute(self, sql, params=()):
            sent.append((sql, params))
            if "RETURNING saved_at" in sql:
                return Rows([{"saved_at": "2026-10-26 12:00:00"}])
            return Rows([])

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(db, "is_postgres", lambda: True)
    monkeypatch.setattr(db, "conn", lambda: RecordingConn())
    assert db.save_study_draft(1, 2, "text") == "2026-10-26 12:00:00"
    db.save_study_draft(1, 2, "  ")
    sql = " ".join(s for s, _ in sent)
    assert "datetime(" not in sql                                    # no SQLite-only functions
    assert "now() AT TIME ZONE 'utc'" in sql
    assert "ON CONFLICT(note_id, q_number) DO UPDATE SET text = excluded.text" in sql
    upsert = next(s for s, _ in sent if "INSERT INTO study_drafts" in s)
    pg = upsert.replace("%", "%%").replace("?", "%s")                 # what _PgConn sends to psycopg
    assert pg.count("%s") == 3 and "?" not in pg
    ddl = next(s for s in db.PG_SCHEMA if "study_drafts" in s)
    assert "CREATE TABLE IF NOT EXISTS study_drafts" in ddl and "TIMESTAMP(0)" in ddl
    # Add-only: changes to existing study tables may only add columns, never alter or drop.
    study_alters = [s for s in db.PG_SCHEMA if s.startswith("ALTER TABLE study_")]
    assert all(" ADD COLUMN IF NOT EXISTS " in s for s in study_alters)
    assert not any("DROP " in s or "ALTER COLUMN" in s for s in db.PG_SCHEMA)


# --- deleting a note ------------------------------------------------------------------

def test_delete_removes_every_row_of_the_note_and_keeps_bookmarks(env):
    keep, keep_ids = new_note(env, topic="Keep me")
    run_through(env, keep)
    env.client.post(f"/api/study/{keep}/answers/1", json={"answer": "Kept"})
    env.client.put(f"/api/study/{keep}/drafts/2", json={"text": "Kept draft"})
    gone, gone_ids = new_note(env, topic="Delete me")
    run_through(env, gone)
    env.client.post(f"/api/study/{gone}/answers/1", json={"answer": "An answer"})
    env.client.put(f"/api/study/{gone}/drafts/2", json={"text": "A draft"})
    bookmarks_before = db.list_all()

    assert env.client.delete(f"/api/study/{gone}").status_code == 200
    with sqlite3.connect(db.DB_PATH) as conn:
        for table in db.STUDY_NOTE_TABLES:
            assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE note_id = ?", (gone,)).fetchone()[0] == 0, table
            assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE note_id = ?", (keep,)).fetchone()[0] > 0, table
        assert conn.execute("SELECT COUNT(*) FROM study_notes WHERE id = ?", (gone,)).fetchone()[0] == 0
    # The saved items themselves are untouched, including the ones the deleted note used.
    assert db.list_all() == bookmarks_before
    assert all(db.get(b) for b in gone_ids + keep_ids)
    assert env.client.get(f"/api/study/{gone}").status_code == 404
    assert env.client.get(f"/api/study/{keep}").json()["drafts"]["2"]["text"] == "Kept draft"


def test_every_table_with_note_rows_is_cleared_on_delete(env):
    """A new per-note table must be added to STUDY_NOTE_TABLES, or deleting a note would leave rows behind."""
    tables_with_note_id = set()
    with sqlite3.connect(db.DB_PATH) as conn:
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):
            if "note_id" in [r[1] for r in conn.execute(f"PRAGMA table_info({name})")]:
                tables_with_note_id.add(name)
    assert tables_with_note_id - {"study_runs"} == set(db.STUDY_NOTE_TABLES)


def test_delete_missing_note_returns_404(env):
    assert env.client.delete("/api/study/9999").status_code == 404


# --- summarize first --------------------------------------------------------------------

def test_summary_limit_is_60():
    from app import summaries
    assert summaries.RATE_LIMIT == 60


def test_summarize_first_choice_is_stored(env):
    ids = [add(f"https://example.com/s{i}") for i in range(2)]
    on = env.client.post("/api/study", json={"topic": "On", "bookmark_ids": ids, "summarize_first": True}).json()
    off = env.client.post("/api/study", json={"topic": "Off", "bookmark_ids": ids}).json()
    assert on["summarize_first"] is True and on["sources_ready"] is False
    assert off["summarize_first"] is False and off["sources_ready"] is False
    assert env.client.get(f"/api/study/{on['id']}").json()["summarize_first"] is True


def test_sources_ready_is_recorded_once(env):
    ids = [add("https://example.com/r")]
    nid = env.client.post("/api/study", json={"topic": "T", "bookmark_ids": ids, "summarize_first": True}).json()["id"]
    r = env.client.post(f"/api/study/{nid}/sources-ready")
    assert r.status_code == 200 and r.json()["sources_ready"] is True
    first = db.get_study_note(nid)["sources_ready_at"]
    env.client.post(f"/api/study/{nid}/sources-ready")
    assert db.get_study_note(nid)["sources_ready_at"] == first
    assert env.client.post("/api/study/9999/sources-ready").status_code == 404


def test_new_note_columns_are_added_to_an_older_database(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:      # study_notes as it was before these columns
        conn.execute("""CREATE TABLE study_notes(id INTEGER PRIMARY KEY, topic TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')), updated_at TEXT DEFAULT (datetime('now')),
            models_json TEXT, models_at TEXT, debates_json TEXT, debates_at TEXT,
            questions_json TEXT, questions_at TEXT, study_model TEXT)""")
        conn.execute("INSERT INTO study_notes(topic, models_json) VALUES ('Old note', '{}')")
        before = conn.execute("PRAGMA table_info(study_notes)").fetchall()
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init()
    with sqlite3.connect(path) as conn:
        after = conn.execute("PRAGMA table_info(study_notes)").fetchall()
        row = conn.execute("SELECT topic, models_json, summarize_first, sources_ready_at FROM study_notes").fetchone()
    assert after[:len(before)] == before                      # existing columns unchanged
    assert [c[1] for c in after[len(before):]] == ["summarize_first", "sources_ready_at"]
    assert row == ("Old note", "{}", None, None)              # old notes: not chosen, not prepared
    view = study_notes.view(db.get_study_note(1))
    assert view["summarize_first"] is False and view["sources_ready"] is False


# --- bulk delete --------------------------------------------------------------------

def test_bulk_delete_removes_the_chosen_notes_only(env):
    a, a_ids = new_note(env, topic="A")
    b, _ = new_note(env, topic="B")
    c, _ = new_note(env, topic="C")
    run_through(env, a)
    env.client.put(f"/api/study/{a}/drafts/1", json={"text": "draft"})
    bookmarks_before = db.list_all()
    r = env.client.post("/api/study/bulk-delete", json={"ids": [a, b, a]})      # duplicates are ignored
    assert r.status_code == 200
    assert r.json() == {"deleted": [a, b], "not_found": [], "failed": []}
    remaining = [n["id"] for n in env.client.get("/api/study").json()["notes"]]
    assert remaining == [c]
    with sqlite3.connect(db.DB_PATH) as conn:
        for table in db.STUDY_NOTE_TABLES:
            assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE note_id IN (?, ?)", (a, b)).fetchone()[0] == 0
    assert db.list_all() == bookmarks_before and all(db.get(i) for i in a_ids)


def test_bulk_delete_reports_missing_and_failed_without_stopping(env, monkeypatch):
    a, _ = new_note(env, topic="A")
    b, _ = new_note(env, topic="B")
    c, _ = new_note(env, topic="C")
    real = db.delete_study_note

    def flaky(nid):
        if nid == b:
            raise RuntimeError("database hiccup")
        return real(nid)

    monkeypatch.setattr(db, "delete_study_note", flaky)
    r = env.client.post("/api/study/bulk-delete", json={"ids": [a, b, 9999, c]})
    assert r.status_code == 200
    assert r.json() == {"deleted": [a, c], "not_found": [9999],
                        "failed": [{"id": b, "detail": "Couldn't delete it; try again."}]}
    assert [n["id"] for n in env.client.get("/api/study").json()["notes"]] == [b]


def test_bulk_delete_validation(env):
    assert env.client.post("/api/study/bulk-delete", json={"ids": []}).status_code == 422
    assert env.client.post("/api/study/bulk-delete", json={"ids": list(range(1, 102))}).status_code == 422
    assert env.client.post("/api/study/bulk-delete", json={}).status_code == 422
