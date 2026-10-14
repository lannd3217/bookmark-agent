import json
from datetime import datetime, timezone
from pathlib import Path

from app.platforms import normalize_url


def parse_facebook_saves(path) -> list[dict]:
    """Parse a Facebook saved_items.json export into [{url, title, created_at}]."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Facebook export not found: {path}")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict) or "saves_v2" not in doc:
        raise ValueError(f"{path} has no top-level 'saves_v2' key; is this saved_items.json?")
    if not isinstance(doc["saves_v2"], list):
        raise ValueError(f"'saves_v2' in {path} should be a list")

    items, seen = [], set()
    for entry in doc["saves_v2"]:
        if not isinstance(entry, dict):
            continue
        for obj in entry.get("data") or []:
            uri = obj.get("uri") if isinstance(obj, dict) else None
            if not uri:
                continue
            url = normalize_url(uri.strip())
            if url in seen:
                continue
            seen.add(url)
            items.append({
                "url": url,
                "title": obj.get("title"),
                "created_at": _iso(entry.get("timestamp")),
            })
    return items


def _iso(ts) -> str | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError) as exc:
        raise ValueError(f"Invalid timestamp in Facebook export: {ts!r}") from exc
