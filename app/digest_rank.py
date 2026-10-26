import json, os

from .llm import response_text

USE_AI = os.getenv("DIGEST_USE_AI", "0") == "1"
MODEL = os.getenv("RANK_MODEL", "claude-haiku-5-5")

SYSTEM = """You curate a weekly reading list for someone who follows a research area closely.
From the candidate papers, pick the most important ones: new methods, strong or surprising
results, or work many people in the field will discuss. Upvotes show popularity, not quality.
Judge only from the titles and abstracts given. Reply with JSON only:
{"picks": [{"arxiv_id": "...", "why": "one sentence on why it matters"}]}"""

def first_sentence(text: str) -> str:
    text = " ".join((text or "").split())
    end = text.find(". ")
    return text[: end + 1] if end != -1 else text[:200]

def rank_without_ai(candidates: list[dict], top_n: int) -> list[dict]:
    # Candidates arrive sorted by pre-score, best first.
    return [{"arxiv_id": c["arxiv_id"], "why": first_sentence(c["abstract"])}
            for c in candidates[:top_n]]

def rank_with_ai(domain: str, candidates: list[dict], top_n: int) -> list[dict]:
    import anthropic  # only needed when AI ranking is on
    client = anthropic.Anthropic()
    listing = "\n\n".join(
        f"[{c['arxiv_id']}] {c['title']} (upvotes: {c.get('upvotes', 0)})\n{c['abstract'][:1200]}"
        for c in candidates
    )
    msg = client.messages.create(
        model=MODEL, max_tokens=2000, system=SYSTEM,
        messages=[{"role": "user", "content": f"Domain: {domain}\nPick up to {top_n}.\n\nCandidates:\n{listing}"}],
    )
    raw = response_text(msg)
    picks = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])["picks"]
    valid = {c["arxiv_id"] for c in candidates}  # grounding: drop anything not in the list
    return [p for p in picks if p.get("arxiv_id") in valid][:top_n]

def rank_papers(domain: str, candidates: list[dict], top_n: int = 10, use_ai: bool | None = None) -> list[dict]:
    if use_ai is None:
        use_ai = USE_AI
    if not use_ai:
        return rank_without_ai(candidates, top_n)
    try:
        return rank_with_ai(domain, candidates, top_n)
    except Exception as exc:
        print(f"AI ranking failed ({exc}); using pre-score order instead")
        return rank_without_ai(candidates, top_n)