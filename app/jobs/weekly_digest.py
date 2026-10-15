"""Weekly "top new papers" digest.

    python -m app.jobs.weekly_digest [--watchlist NAME] [--dry-run]

For each watchlist in config/watchlists.json: fetch last week's arXiv papers, add
Hugging Face upvotes, pre-score, let rank_papers pick (by pre-score, or with
Claude when DIGEST_USE_AI=1), then save the picks as bookmarks plus a digest.
--dry-run prints the picks without saving.
"""
import argparse
import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")  # before digest_rank and sources read their env vars

from app import db, digest_rank  # noqa: E402
from app.digest_score import CANDIDATE_LIMIT, top_candidates  # noqa: E402
from app.sources import arxiv, hf_papers  # noqa: E402

CONFIG_PATH = ROOT / "config" / "watchlists.json"
log = logging.getLogger("weekly_digest")


def load_watchlists(path: Path | None = None) -> list[dict]:
    path = path or CONFIG_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"Watchlist config not found: {path}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}")
    if not isinstance(data, list) or not data:
        raise ValueError(f"{path} must be a non-empty JSON list of watchlists")
    names = set()
    for i, w in enumerate(data):
        where = f"watchlist #{i + 1}"
        if not isinstance(w, dict):
            raise ValueError(f"{where} must be an object")
        for key in ("name", "domain"):
            if not isinstance(w.get(key), str) or not w[key].strip():
                raise ValueError(f"{where}: '{key}' must be a non-empty string")
        where = f"watchlist {w['name']!r}"
        if w["name"] in names:
            raise ValueError(f"{where}: duplicate name")
        names.add(w["name"])
        cats = w.get("arxiv_categories")
        if not isinstance(cats, list) or not cats or not all(isinstance(c, str) and c.strip() for c in cats):
            raise ValueError(f"{where}: 'arxiv_categories' must be a non-empty list of strings like \"cs.CL\"")
        kws = w.get("keywords", [])
        if not isinstance(kws, list) or not all(isinstance(k, str) for k in kws):
            raise ValueError(f"{where}: 'keywords' must be a list of strings")
        top_n = w.get("top_n", 10)
        if not isinstance(top_n, int) or isinstance(top_n, bool) or not 1 <= top_n <= CANDIDATE_LIMIT:
            raise ValueError(f"{where}: 'top_n' must be an integer from 1 to {CANDIDATE_LIMIT}")
        w["keywords"], w["top_n"] = kws, top_n
    return data


def author_line(authors: list[str]) -> str | None:
    names = [a for a in authors if a]
    if not names:
        return None
    return ", ".join(names[:3]) + (" et al." if len(names) > 3 else "")


def build_picks(ranked: list[dict], candidates: list[dict]) -> list[dict]:
    """Turn rank_papers output into rows to save: drop repeats, number ranks 1..n."""
    by_id = {c["arxiv_id"]: c for c in candidates}
    picks, seen = [], set()
    for r in ranked:
        aid = r.get("arxiv_id")
        if aid not in by_id or aid in seen:
            continue
        seen.add(aid)
        paper = by_id[aid]
        why = str(r.get("why") or "").strip() or None
        picks.append({
            "arxiv_id": aid,
            "rank": len(picks) + 1,
            "url": f"https://arxiv.org/abs/{aid}",
            "platform": "arxiv",
            "title": paper["title"],
            "author": author_line(paper.get("authors", [])),
            "text": paper["abstract"],
            "note": why,
            "why": why,
            "upvotes": paper.get("upvotes", 0),
        })
    return picks


def ranking_method(ranked: list[dict], candidates: list[dict], top_n: int) -> str:
    """How rank_papers ranked: "prescore", "ai", or "prescore_fallback" if AI ranking failed.

    rank_papers falls back to pre-score order without saying so; its fallback output is
    exactly rank_without_ai's, while Claude writes its own "why" sentences.
    """
    if not digest_rank.USE_AI:
        return "prescore"
    if ranked == digest_rank.rank_without_ai(candidates, top_n):
        return "prescore_fallback"
    return "ai"


def run_watchlist(w: dict, upvotes: dict[str, int], dry_run: bool) -> list[dict]:
    papers = arxiv.recent_papers(w["arxiv_categories"], w["keywords"])
    for p in papers:
        p["upvotes"] = upvotes.get(p["arxiv_id"], 0)
    candidates = top_candidates(papers, w["keywords"])
    print(f"\n== {w['name']}: {len(papers)} arXiv papers, {len(candidates)} candidates")
    if not candidates:
        print("   No candidates this week; skipping.")
        return []

    ranked = digest_rank.rank_papers(w["domain"], candidates, w["top_n"])
    ranked_by = ranking_method(ranked, candidates, w["top_n"])
    picks = build_picks(ranked, candidates)
    print(f"   Ranked by: {ranked_by}")
    for p in picks:
        print(f"{p['rank']:>3}. {p['title']}\n     {p['url']}  (upvotes: {p['upvotes']})\n     {p['why'] or ''}")
    if not picks:
        print("   rank_papers returned no valid picks.")
    if dry_run:
        print("   (dry run: not saved)")
    elif picks:
        digest_id, saved = db.save_digest(w["name"], picks, ranked_by)
        added = sum(created for _, created in saved)
        print(f"   Saved digest #{digest_id}: {added} new bookmarks, {len(saved) - added} already saved.")
    return picks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.jobs.weekly_digest", description=__doc__.splitlines()[0])
    parser.add_argument("--watchlist", metavar="NAME", help="run only this watchlist")
    parser.add_argument("--dry-run", action="store_true", help="print the picks without saving")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    try:
        watchlists = load_watchlists()
    except ValueError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    if args.watchlist:
        watchlists = [w for w in watchlists if w["name"] == args.watchlist]
        if not watchlists:
            print(f"No watchlist named {args.watchlist!r} in {CONFIG_PATH}", file=sys.stderr)
            return 2
    if digest_rank.USE_AI and not os.environ.get("ANTHROPIC_API_KEY"):
        log.warning("DIGEST_USE_AI=1 but ANTHROPIC_API_KEY is not set; ranking will fall back to pre-score order")

    db.init(recover_pending=False)
    log.info("Weekly digest started: %s%s", ", ".join(w["name"] for w in watchlists),
             " (dry run)" if args.dry_run else "")
    upvotes = hf_papers.recent_upvotes()

    failed = 0
    for w in watchlists:
        try:
            run_watchlist(w, upvotes, args.dry_run)
        except Exception as exc:
            failed += 1
            log.exception("Watchlist %r failed: %s", w["name"], exc)
    log.info("Weekly digest finished: %d ok, %d failed", len(watchlists) - failed, failed)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
