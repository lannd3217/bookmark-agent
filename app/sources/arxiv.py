"""Recent papers from the arXiv API (https://info.arxiv.org/help/api/).

arXiv's terms for the legacy API: at most one request every three seconds and a
single connection at a time. One shared client and a pacer enforce both.
"""
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import httpx

from . import USER_AGENT

API_URL = "https://export.arxiv.org/api/query"
MIN_INTERVAL = 3.0      # seconds between requests (arXiv API terms)
PAGE_SIZE = 100
MAX_RESULTS = 400       # per watchlist; pre-scoring keeps far fewer
TIMEOUT = 30.0
RETRIES = 2

NS = {"a": "http://www.w3.org/2005/Atom", "os": "http://a9.com/-/spec/opensearch/1.1/"}
log = logging.getLogger(__name__)


class Pacer:
    """Blocks so that calls to wait() are at least `interval` seconds apart."""

    def __init__(self, interval: float = MIN_INTERVAL, clock=time.monotonic, sleep=time.sleep):
        self.interval, self.clock, self.sleep = interval, clock, sleep
        self._last = None

    def wait(self):
        if self._last is not None:
            remaining = self.interval - (self.clock() - self._last)
            if remaining > 0:
                self.sleep(remaining)
        self._last = self.clock()


_pacer = Pacer()  # module-wide, so separate watchlists in one run share the limit


def build_query(categories: list[str], keywords: list[str], start: datetime, end: datetime) -> str:
    parts = ["(" + " OR ".join(f"cat:{c}" for c in categories) + ")"]
    if keywords:
        terms = []
        for kw in keywords:
            phrase = kw.replace('"', "").strip()
            terms += [f'ti:"{phrase}"', f'abs:"{phrase}"']
        parts.append("(" + " OR ".join(terms) + ")")
    parts.append(f"submittedDate:[{start:%Y%m%d%H%M} TO {end:%Y%m%d%H%M}]")
    return " AND ".join(parts)


def _text(el, path: str) -> str:
    found = el.find(path, NS)
    return re.sub(r"\s+", " ", found.text).strip() if found is not None and found.text else ""


def _arxiv_id(abs_url: str) -> str:
    # http://arxiv.org/abs/2610.20570v1 -> 2610.20570 (old-style ids keep their archive prefix)
    ident = abs_url.split("/abs/", 1)[-1]
    return re.sub(r"v\d+$", "", ident)


def parse_feed(xml_text: str) -> tuple[list[dict], int]:
    """Return (papers, totalResults) from an arXiv Atom response."""
    root = ET.fromstring(xml_text)
    total = int(_text(root, "os:totalResults") or 0)
    papers = []
    for entry in root.findall("a:entry", NS):
        ident = _text(entry, "a:id")
        if "/abs/" not in ident:  # arXiv reports query errors as an entry
            raise RuntimeError(f"arXiv API error: {_text(entry, 'a:summary') or 'unknown'}")
        papers.append({
            "arxiv_id": _arxiv_id(ident),
            "title": _text(entry, "a:title"),
            "abstract": _text(entry, "a:summary"),
            "authors": [_text(a, "a:name") for a in entry.findall("a:author", NS)],
            "published": _text(entry, "a:published"),
        })
    return papers, total


def _get(client: httpx.Client, params: dict, pacer: Pacer) -> str:
    for attempt in range(RETRIES + 1):
        pacer.wait()
        try:
            r = client.get(API_URL, params=params)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            if attempt == RETRIES:
                raise
            log.warning("arXiv request failed (%s); retrying", type(exc).__name__)
            continue
        if r.status_code >= 500 and attempt < RETRIES:
            log.warning("arXiv returned HTTP %s; retrying", r.status_code)
            continue
        r.raise_for_status()
        return r.text
    raise AssertionError("unreachable")


def recent_papers(categories: list[str], keywords: list[str], days: int = 7,
                  now: datetime | None = None, client: httpx.Client | None = None,
                  pacer: Pacer | None = None, max_results: int = MAX_RESULTS) -> list[dict]:
    """Papers first submitted in the last `days` days matching the categories and keywords."""
    end = now or datetime.now(timezone.utc)
    query = build_query(categories, keywords, end - timedelta(days=days), end)
    pacer = pacer or _pacer
    own_client = client is None
    client = client or httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    papers: dict[str, dict] = {}
    try:
        start = 0
        while start < max_results:
            params = {"search_query": query, "sortBy": "submittedDate", "sortOrder": "descending",
                      "start": start, "max_results": min(PAGE_SIZE, max_results - start)}
            page, total = parse_feed(_get(client, params, pacer))
            for p in page:
                papers.setdefault(p["arxiv_id"], p)
            start += len(page)
            if not page or start >= total:
                break
    finally:
        if own_client:
            client.close()
    return list(papers.values())
