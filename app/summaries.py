"""AI summaries of saved items, written by app.summarize from the best text available."""
import logging
import math
import os
from datetime import datetime, timedelta, timezone

import anthropic

from . import db
from . import summarize as summarizer
from .sources import arxiv_fulltext

log = logging.getLogger(__name__)
RATE_LIMIT = 60     # summary model calls per hour, across the whole app (a whole shelf can need many)


class SummaryError(Exception):
    """A summary that can't be made; the message is meant for the user."""
    status = 502


class NotEnoughText(SummaryError):
    status = 422


class RateLimited(SummaryError):
    status = 429

    def __init__(self, message: str, retry_after: int):
        super().__init__(message)
        self.retry_after = retry_after


class Unavailable(SummaryError):
    status = 503


TRY_AGAIN = "Couldn't summarize, try again."
TOO_LITTLE = "There isn't enough text here to summarize. Try opening the original."


def source_text(item: dict) -> tuple[str, str, bool]:
    """(text, source_kind, full_text_fetch_failed) for summarizing `item`.

    arXiv papers use their full text, fetched once and stored; if that fails, the abstract.
    Everything else uses the page text saved at extraction.
    """
    if item.get("platform") == "arxiv":
        full = db.get_full_text(item["id"])
        if not full:
            arxiv_id = arxiv_fulltext.arxiv_id_from_url(item.get("url"))
            try:
                if not arxiv_id:
                    raise arxiv_fulltext.FullTextError(f"no arXiv id in {item.get('url')!r}")
                full = arxiv_fulltext.fetch_full_text(arxiv_id)
                db.save_full_text(item["id"], full)
            except Exception as exc:
                log.warning("Full text for bookmark %s unavailable (%s); using the abstract", item["id"], exc)
                return item.get("text") or "", "paper abstract only", True
        return full, "full paper", False
    kind = "social post" if item.get("kind") == "post" else "web article"
    return item.get("text") or "", kind, False


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
        f"You've made {RATE_LIMIT} summaries in the last hour, the limit. "
        f"Try again in {minutes} minute{'s' if minutes != 1 else ''}.", retry_after=minutes * 60)


def summarize_item(bid: int, regenerate: bool = False) -> dict | None:
    """The bookmark with its summary, made now if it has none (or if regenerate).

    Returns None if the bookmark doesn't exist; raises a SummaryError subclass otherwise.
    """
    item = db.get(bid)
    if item is None:
        return None
    if item["summary"] and not regenerate:
        return item
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise Unavailable("Summaries are unavailable: ANTHROPIC_API_KEY is not set on the server.")
    used, oldest = db.summary_runs_last_hour()
    if used >= RATE_LIMIT:
        raise _rate_limited(oldest)

    text, kind, fetch_failed = source_text(item)
    if len(text.strip()) < summarizer.MIN_CHARS:
        # Without the paper itself, a short abstract may be fine next time.
        raise SummaryError(TRY_AGAIN) if fetch_failed else NotEnoughText(TOO_LITTLE)
    if not db.record_summary_run(bid, RATE_LIMIT):
        raise _rate_limited(db.summary_runs_last_hour()[1])
    try:
        summary = summarizer.summarize(item.get("title") or item.get("url") or "", text, kind)
    except anthropic.AuthenticationError as exc:
        raise Unavailable("Summaries are unavailable: the Anthropic API key was rejected.") from exc
    except Exception as exc:
        log.warning("Summarizing bookmark %s failed: %s: %s", bid, type(exc).__name__, exc)
        raise SummaryError(TRY_AGAIN) from exc
    db.save_summary(bid, summary, kind, summarizer.MODEL)
    return db.get(bid)
