"""Cheap pre-score to pick which papers go to rank_papers. Tune the numbers here."""
import math
import re
from datetime import datetime, timezone

WEIGHTS = {
    "recency": 1.0,           # x (1 - age_days / window_days), so 0..1
    "upvotes": 1.0,           # x log(1 + upvotes); 10 upvotes ~ 2.4, 100 ~ 4.6
    "title_keyword": 1.5,     # per distinct watchlist keyword found in the title
    "abstract_keyword": 0.5,  # per distinct watchlist keyword found in the abstract
}
CANDIDATE_LIMIT = 30          # how many candidates rank_papers sees
WINDOW_DAYS = 7


def _keyword_hits(text: str, keywords: list[str]) -> int:
    return sum(
        1 for kw in keywords
        if kw.strip() and re.search(rf"(?<!\w){re.escape(kw.strip())}(?!\w)", text, re.IGNORECASE)
    )


def _age_days(published: str, now: datetime) -> float:
    try:
        when = datetime.fromisoformat(published.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return WINDOW_DAYS
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max((now - when).total_seconds() / 86400, 0.0)


def prescore(paper: dict, keywords: list[str], now: datetime, weights: dict = WEIGHTS) -> float:
    recency = max(0.0, 1 - _age_days(paper.get("published", ""), now) / WINDOW_DAYS)
    return (
        weights["recency"] * recency
        + weights["upvotes"] * math.log1p(max(paper.get("upvotes", 0), 0))
        + weights["title_keyword"] * _keyword_hits(paper.get("title", ""), keywords)
        + weights["abstract_keyword"] * _keyword_hits(paper.get("abstract", ""), keywords)
    )


def top_candidates(papers: list[dict], keywords: list[str], now: datetime | None = None,
                   limit: int = CANDIDATE_LIMIT, weights: dict = WEIGHTS) -> list[dict]:
    """Return the `limit` best papers, each with a "prescore" added. Ties keep input order."""
    now = now or datetime.now(timezone.utc)
    scored = [{**p, "prescore": round(prescore(p, keywords, now, weights), 3)} for p in papers]
    return sorted(scored, key=lambda p: -p["prescore"])[:limit]
