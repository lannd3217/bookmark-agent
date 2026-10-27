"""Study notes: the four guided steps around app.study, with storage and limits."""
import logging
import math
import os
from datetime import datetime, timedelta, timezone

import anthropic

from . import db
from . import study

log = logging.getLogger(__name__)
# Study AI requests per hour (steps and answer feedback, across all notes), counted in study_runs.
# Separate from TL;DR summaries' limit; the Ask box isn't limited here.
RATE_LIMIT = 60
MAX_SOURCES = 25
# Answer and draft lengths, enforced by the API and sent to the page so the two can't disagree.
ANSWER_MIN = 1
ANSWER_MAX = 4000
STEP_NAMES = {1: "models", 2: "debates", 3: "questions"}
# Question fields the learner sees only after answering.
HIDDEN_UNTIL_ANSWERED = ("deep_answer_includes", "shallow_answer")


class StudyError(Exception):
    """A study request that can't be done; the message is meant for the user."""
    status = 502


class NotReady(StudyError):
    status = 409


class Unavailable(StudyError):
    status = 503


class RateLimited(StudyError):
    status = 429

    def __init__(self, message: str, retry_after: int):
        super().__init__(message)
        self.retry_after = retry_after


class Invalid(StudyError):
    status = 400


def _retry_minutes(oldest: str | None) -> int:
    try:
        start = datetime.strptime(oldest or "", "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return 60
    left = start + timedelta(hours=1) - datetime.now(timezone.utc)
    return max(1, math.ceil(left.total_seconds() / 60))


def _rate_limited(oldest: str | None) -> RateLimited:
    minutes = _retry_minutes(oldest)
    return RateLimited(
        f"You've used the study limit of {RATE_LIMIT} AI requests in the last hour. "
        f"Try again in {minutes} minute{'s' if minutes != 1 else ''}.", retry_after=minutes * 60)


def _before_model_call(nid: int):
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise Unavailable("Study notes are unavailable: ANTHROPIC_API_KEY is not set on the server.")
    used, oldest = db.study_runs_last_hour()
    if used >= RATE_LIMIT:
        raise _rate_limited(oldest)
    if not db.record_study_run(nid, RATE_LIMIT):
        raise _rate_limited(db.study_runs_last_hour()[1])


def _sources(note: dict) -> tuple[list[dict], str]:
    items = [it for it in (db.get(bid) for bid in note["source_ids"]) if it]
    if not items:
        raise Invalid("None of this note's sources are in your library any more.")
    return items, study.format_sources(items)


def create_note(topic: str, bookmark_ids: list[int], summarize_first: bool = False) -> int:
    ids = list(dict.fromkeys(bookmark_ids))       # unique, order kept
    if not ids:
        raise Invalid("Pick at least one source.")
    if len(ids) > MAX_SOURCES:
        raise Invalid(f"A study note can use up to {MAX_SOURCES} sources; you picked {len(ids)}.")
    missing = [i for i in ids if i not in db.existing_bookmark_ids(ids)]
    if missing:
        raise Invalid(f"These items aren't in your library: {', '.join(map(str, missing))}.")
    return db.create_study_note(topic, ids, summarize_first)


def run_step(nid: int, n: int, regenerate: bool = False) -> dict | None:
    """Run (or return the saved) step n of 1-3. Returns the note view, or None if there's no note."""
    note = db.get_study_note(nid)
    if note is None:
        return None
    name = STEP_NAMES[n]
    if n > 1 and not note[STEP_NAMES[n - 1]]:
        raise NotReady(f"Finish step {n - 1} first.")
    if note[name] and not regenerate:
        return view(note)
    items, sources = _sources(note)
    _before_model_call(nid)
    try:
        if n == 1:
            data = study.mental_models(note["topic"], sources)
        elif n == 2:
            data = study.disagreements(note["topic"], sources, note["models"])
        else:
            data = study.study_questions(note["topic"], sources, note["models"], note["debates"])
    except anthropic.AuthenticationError as exc:
        raise Unavailable("Study notes are unavailable: the Anthropic API key was rejected.") from exc
    except Exception as exc:
        log.warning("Study step %s for note %s failed: %s: %s", n, nid, type(exc).__name__, exc)
        raise StudyError("Couldn't finish this step. Try again.") from exc
    db.save_study_step(nid, name, data, study.MODEL)
    return view(db.get_study_note(nid))


def answer(nid: int, q_number: int, text: str) -> dict | None:
    """Check an answer to question q_number (1-based) and save the feedback. Returns the note view."""
    note = db.get_study_note(nid)
    if note is None:
        return None
    questions = (note["questions"] or {}).get("questions") or []
    if not questions:
        raise NotReady("Finish step 3 first.")
    if not 1 <= q_number <= len(questions):
        raise Invalid(f"There's no question {q_number}.")
    items, sources = _sources(note)
    question = {**questions[q_number - 1], "number": q_number}
    _before_model_call(nid)
    try:
        fb = study.feedback(note["topic"], sources, question, text)
    except anthropic.AuthenticationError as exc:
        raise Unavailable("Study notes are unavailable: the Anthropic API key was rejected.") from exc
    except Exception as exc:
        log.warning("Feedback for note %s question %s failed: %s: %s", nid, q_number, type(exc).__name__, exc)
        raise StudyError("Couldn't check your answer. Try again.") from exc
    db.save_study_answer(nid, q_number, text, fb)
    return view(db.get_study_note(nid))


def save_draft(nid: int, q_number: int, text: str) -> dict | None:
    """Save (or, if blank, delete) an unsent draft for one question. No model call, no attempt
    counted, and the question's hidden fields stay hidden. Returns None if there's no note."""
    note = db.get_study_note(nid)
    if note is None:
        return None
    questions = (note["questions"] or {}).get("questions") or []
    if not questions:
        raise NotReady("Finish step 3 first.")
    if not 1 <= q_number <= len(questions):
        raise Invalid(f"There's no question {q_number}.")
    saved_at = db.save_study_draft(nid, q_number, text)
    return {"q": q_number, "text": text if saved_at else "", "saved_at": saved_at}


def view(note: dict) -> dict:
    """The note as the page sees it: sources that still exist, steps, answers, and progress.

    Each question's hidden fields come back only with that question's answer.
    """
    sources = []
    for bid in note["source_ids"]:
        it = db.get(bid)
        if it:
            sources.append({k: it.get(k) for k in ("id", "title", "url", "platform", "kind", "has_summary")})
    questions = (note["questions"] or {}).get("questions") or []
    public_q = None
    if note["questions"]:
        public_q = {**note["questions"], "questions": [
            {k: v for k, v in q.items() if k not in HIDDEN_UNTIL_ANSWERED} | {"number": i + 1}
            for i, q in enumerate(questions)]}
    answers = {}
    for number, a in note["answers"].items():
        q = questions[number - 1] if 0 < number <= len(questions) else {}
        answers[number] = {
            "answer": a["answer"], "feedback": a["feedback"], "verdict": a["verdict"],
            "attempts": a["attempts"], "answered_at": a["answered_at"],
            **{k: q.get(k) for k in HIDDEN_UNTIL_ANSWERED},
        }
    answered = sum(1 for a in answers.values() if a["feedback"])
    steps_done = sum(1 for name in STEP_NAMES.values() if note[name])
    if questions and answered >= len(questions):
        steps_done += 1
    return {
        "id": note["id"], "topic": note["topic"], "created_at": note["created_at"], "updated_at": note["updated_at"],
        "summarize_first": bool(note.get("summarize_first")),
        "sources_ready": bool(note.get("sources_ready_at")),
        "sources": sources,
        "steps": {"models": note["models"], "debates": note["debates"], "questions": public_q},
        "step_times": {name: note[f"{name}_at"] for name in STEP_NAMES.values()},
        "answers": answers,
        "drafts": note.get("drafts", {}),
        "progress": {"steps_done": steps_done, "answered": answered, "questions": len(questions)},
        "limits": {"answer_min": ANSWER_MIN, "answer_max": ANSWER_MAX},
    }


def list_notes() -> list[dict]:
    out = []
    for n in db.list_study_notes():
        steps = n["has_models"] + n["has_debates"] + n["has_questions"]
        if n["has_questions"] and n["answered"] >= 10:
            steps += 1
        out.append({"id": n["id"], "topic": n["topic"], "created_at": n["created_at"], "updated_at": n["updated_at"],
                    "source_count": n["source_count"], "steps_done": steps, "answered": n["answered"],
                    "has_questions": bool(n["has_questions"]), "drafts": n["drafts"]})
    return out
