import json
from .study import _ask

KINDS = {"flow", "cycle", "layers", "compare", "spectrum", "tree", "overlap"}
MAX_LABEL = 28


def visualize_models(topic: str, models: dict) -> dict:
    """models: step 1's output. Returns one diagram spec per mental model."""
    task = f"""Topic: {topic}

Mental models:
{json.dumps(models.get("models", []))}

For each mental model, design one simple diagram that makes the idea click at a glance.
Choose the kind that fits the idea best:
- flow: steps in order, A -> B -> C (3-5 steps)
- cycle: a loop that feeds back on itself (3-5 steps)
- layers: levels stacked from bottom to top (3-5 layers)
- compare: two things side by side (a left and a right, each with 1-3 points)
- spectrum: a range between two ends, with one marker on it
- tree: one idea that breaks into parts (a root and 2-4 branches)
- overlap: two ideas that share something (left, right, and what they share)
Labels are plain words, at most 4 words each. Add a one-sentence caption that says what the picture shows.
Number models from 1, in the order given.
Reply as:
{{"visuals": [{{"model": 1, "kind": "flow", "caption": "...", "parts": {{...}}}}]}}
Parts by kind:
- flow, cycle, layers: {{"items": ["...", "..."]}}
- compare: {{"left": {{"title": "...", "points": ["..."]}}, "right": {{"title": "...", "points": ["..."]}}}}
- spectrum: {{"left": "...", "right": "...", "marker": "...", "marker_position": 0.0 to 1.0}}
- tree: {{"root": "...", "branches": ["...", "..."]}}
- overlap: {{"left": "...", "right": "...", "shared": "..."}}"""
    data = _ask(task, max_tokens=6000)
    count = len(models.get("models", []))
    visuals = {}
    for v in data.get("visuals") or []:
        cleaned = _clean_visual(v, count)
        if cleaned and cleaned["model"] not in visuals:
            visuals[cleaned["model"]] = cleaned
    return {"visuals": [visuals[k] for k in sorted(visuals)]}


def _t(x):
    return str(x or "").strip()[:MAX_LABEL]


def _items(x, lo, hi):
    items = [_t(i) for i in (x or []) if _t(i)]
    return items[:hi] if len(items) >= lo else None


def _clean_visual(v, count):
    """Keep only a well-formed diagram for a real model; otherwise drop it."""
    if not isinstance(v, dict) or v.get("kind") not in KINDS:
        return None
    try:
        model = int(v.get("model"))
    except (TypeError, ValueError):
        return None
    if not 1 <= model <= count:
        return None
    kind, p = v["kind"], v.get("parts") or {}
    if not isinstance(p, dict):
        return None

    if kind in {"flow", "cycle", "layers"}:
        items = _items(p.get("items"), 3, 5)
        parts = {"items": items} if items else None
    elif kind == "compare":
        parts = {}
        for side in ("left", "right"):
            s = p.get(side)
            if not isinstance(s, dict):
                return None
            points = _items(s.get("points"), 1, 3)
            if not _t(s.get("title")) or not points:
                return None
            parts[side] = {"title": _t(s.get("title")), "points": points}
    elif kind == "spectrum":
        try:
            pos = min(1.0, max(0.0, float(p.get("marker_position", 0.5))))
        except (TypeError, ValueError):
            pos = 0.5
        parts = {"left": _t(p.get("left")), "right": _t(p.get("right")),
                 "marker": _t(p.get("marker")), "marker_position": pos}
        if not (parts["left"] and parts["right"] and parts["marker"]):
            return None
    elif kind == "tree":
        branches = _items(p.get("branches"), 2, 4)
        root = _t(p.get("root"))
        parts = {"root": root, "branches": branches} if root and branches else None
    else:  # overlap
        parts = {"left": _t(p.get("left")), "right": _t(p.get("right")),
                 "shared": _t(p.get("shared"))}
        if not all(parts.values()):
            return None

    if not parts:
        return None
    return {"model": model, "kind": kind,
            "caption": str(v.get("caption") or "").strip()[:200], "parts": parts}