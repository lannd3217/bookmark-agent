"""Upvote counts from Hugging Face Daily Papers.

Endpoint (checked 2026-10-15): GET https://huggingface.co/api/daily_papers?date=YYYY-MM-DD
returns a list of {"paper": {"id": <arXiv id>, "upvotes": n, ...}, ...}.
Any failure is logged and treated as "no upvotes", never raised.
"""
import logging
import time
from datetime import datetime, timedelta, timezone

import httpx

from . import USER_AGENT

API_URL = "https://huggingface.co/api/daily_papers"
TIMEOUT = 15.0
INTERVAL = 0.5  # be polite between the daily requests

log = logging.getLogger(__name__)


def parse_day(data) -> dict[str, int]:
    upvotes = {}
    for entry in data if isinstance(data, list) else []:
        paper = entry.get("paper") if isinstance(entry, dict) else None
        if isinstance(paper, dict) and paper.get("id"):
            try:
                upvotes[str(paper["id"])] = int(paper.get("upvotes") or 0)
            except (TypeError, ValueError):
                continue
    return upvotes


def recent_upvotes(days: int = 7, now: datetime | None = None,
                   client: httpx.Client | None = None, sleep=time.sleep) -> dict[str, int]:
    """Map arXiv id -> upvotes over the last `days` days. Missing papers count as 0."""
    today = (now or datetime.now(timezone.utc)).date()
    own_client = client is None
    client = client or httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    upvotes: dict[str, int] = {}
    failed = 0
    try:
        for i in range(days):
            if i:
                sleep(INTERVAL)
            day = today - timedelta(days=i)
            try:
                r = client.get(API_URL, params={"date": day.isoformat()})
                r.raise_for_status()
                day_votes = parse_day(r.json())
            except Exception as exc:
                failed += 1
                log.warning("HF daily papers for %s unavailable (%s); using 0 upvotes", day, type(exc).__name__)
                continue
            for pid, n in day_votes.items():
                upvotes[pid] = max(n, upvotes.get(pid, 0))
    finally:
        if own_client:
            client.close()
    if failed == days:
        log.warning("Hugging Face Daily Papers unavailable; all upvotes are 0")
    return upvotes
