import json
from .study import _ask

RELATIONS = ["introduces", "uses", "supports", "challenges", "extends",
             "example of", "part of", "depends on", "related to"]
MAX_CONCEPTS = 20


def build_map(topic: str, sources: str, items: list[dict], models: dict) -> dict:
    """items: the note's items (dicts with id, title, url). models: step 1's output."""
    model_names = [m.get("name", "") for m in models.get("models", [])]
    task = f"""Topic: {topic}

Sources:
{sources}

The 5 core mental models, to use as the "model" nodes with ids m1 to m5, in this order:
{json.dumps(model_names)}

Build a concept map that shows the big picture: how the sources connect to the core ideas
and to each other.
- "model" nodes: the 5 mental models above.
- "concept" nodes: up to {MAX_CONCEPTS} important ideas, methods, or findings from the sources.
- "source" nodes: one per source, with its [id] as item_id.
- Edges: connect sources to the concepts they introduce, use, support, or challenge;
  connect concepts to the mental models they belong to; connect concepts to each other
  where it matters. Prefer fewer, meaningful edges over many weak ones.
Edge labels must be one of: {", ".join(RELATIONS)}.
For each source, also give its contribution in one plain sentence, 2-4 key ideas, and
2-5 terms a newcomer would need, each with a plain-language meaning.
Reply as:
{{"nodes": [
  {{"id": "m1", "type": "model", "label": "max 4 words", "summary": "1-2 plain sentences"}},
  {{"id": "c1", "type": "concept", "label": "max 4 words", "summary": "1-2 plain sentences"}},
  {{"id": "s3", "type": "source", "item_id": 3, "label": "short name, max 5 words",
    "contribution": "one sentence", "key_ideas": ["..."],
    "terms": [{{"term": "...", "meaning": "..."}}]}}],
 "edges": [{{"from": "s3", "to": "c1", "label": "introduces"}}]}}"""
    return _clean_map(_ask(task, max_tokens=8000), items)


def _txt(x, limit):
    return str(x or "").strip()[:limit]


def _clean_map(data: dict, items: list[dict]) -> dict:
    allowed = {it["id"]: it for it in items}
    nodes, seen_items, concepts = {}, set(), 0

    for n in data.get("nodes") or []:
        if not isinstance(n, dict):
            continue
        nid, kind = _txt(n.get("id"), 20), n.get("type")
        if not nid or nid in nodes or kind not in {"model", "concept", "source"}:
            continue
        node = {"id": nid, "type": kind, "label": _txt(n.get("label"), 48)}
        if kind == "source":
            try:
                item_id = int(n.get("item_id"))
            except (TypeError, ValueError):
                continue
            if item_id not in allowed or item_id in seen_items:
                continue  # grounding: only the note's own items, once each
            seen_items.add(item_id)
            node.update(
                item_id=item_id,
                contribution=_txt(n.get("contribution"), 300),
                key_ideas=[_txt(k, 200) for k in (n.get("key_ideas") or [])][:4],
                terms=[{"term": _txt(t.get("term"), 60), "meaning": _txt(t.get("meaning"), 300)}
                       for t in (n.get("terms") or [])
                       if isinstance(t, dict) and t.get("term")][:5],
            )
        else:
            if kind == "concept":
                concepts += 1
                if concepts > MAX_CONCEPTS:
                    continue
            node["summary"] = _txt(n.get("summary"), 300)
        if node["label"]:
            nodes[nid] = node

    # Every selected item appears on the map, even if the model skipped it.
    for item_id, it in allowed.items():
        if item_id not in seen_items:
            nid = f"s{item_id}"
            while nid in nodes:
                nid += "_"
            nodes[nid] = {"id": nid, "type": "source", "item_id": item_id,
                          "label": _txt(it.get("title") or it.get("url"), 48),
                          "contribution": "", "key_ideas": [], "terms": []}

    edges, seen_edges = [], set()
    for e in data.get("edges") or []:
        if not isinstance(e, dict):
            continue
        a, b = e.get("from"), e.get("to")
        if a not in nodes or b not in nodes or a == b:
            continue
        label = e.get("label") if e.get("label") in RELATIONS else "related to"
        if (a, b, label) not in seen_edges:
            seen_edges.add((a, b, label))
            edges.append({"from": a, "to": b, "label": label})
    edges = edges[:80]

    # Glossary: every source's terms, merged and deduplicated.
    glossary = {}
    for n in nodes.values():
        for t in n.get("terms", []):
            g = glossary.setdefault(t["term"].lower(),
                                    {"term": t["term"], "meaning": t["meaning"], "item_ids": []})
            g["item_ids"].append(n["item_id"])

    if not any(n["type"] == "concept" for n in nodes.values()) or not edges:
        raise ValueError("Concept map was incomplete")
    return {"nodes": list(nodes.values()), "edges": edges,
            "glossary": sorted(glossary.values(), key=lambda g: g["term"].lower())}