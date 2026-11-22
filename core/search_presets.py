"""Search presets: named, complete search conditions the user can load again.

A preset holds the search panel fields that decide which archive rows a search returns:
query, exclude, search ratings and the date-cutoff bucket range. A saved Parquet is the
frozen result of such a condition; a preset is the condition itself, so it can be edited
and searched again.

The permanent exclude is not part of a preset. It is one shared list (the search panel's red
row, also read by the Event Map) that applies to every search, presets included; a tag only
one preset should drop goes into that preset's exclude.

The search panel loads a preset into its fields (the user then presses Search). The
image lock uses a preset to build its own pose pool (see core.image_lock_pool).

Storage: ``remote_web_search_presets.json`` in user data, next to the search history,
shared by every Remote Web client of one backend.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

FILE_NAME = "remote_web_search_presets.json"
MAX_NAME_LENGTH = 60
RATING_KEYS = "gsqe"
_LOCK = threading.Lock()


def presets_path(context: Any) -> Path:
    return Path(context._save_path(FILE_NAME))


def _bucket_index(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    return None


def normalize(raw: Any) -> dict[str, Any] | None:
    """Return a clean preset dict, or None when ``raw`` has no usable name."""
    if not isinstance(raw, dict):
        return None
    name = " ".join(str(raw.get("name") or "").split())[:MAX_NAME_LENGTH]
    if not name:
        return None
    ratings = raw.get("ratings")
    if isinstance(ratings, str):
        ratings = list(ratings)
    if not isinstance(ratings, (list, tuple, set)):
        ratings = []
    wanted = {str(item).strip().lower() for item in ratings}
    preset: dict[str, Any] = {
        "name": name,
        "query": str(raw.get("query") or "").strip(),
        "exclude": str(raw.get("exclude") or "").strip(),
        # An empty rating list would search nothing; fall back to every rating.
        "ratings": [key for key in RATING_KEYS if key in wanted] or list(RATING_KEYS),
        "bucket_start": _bucket_index(raw.get("bucket_start")),
        "bucket_end": _bucket_index(raw.get("bucket_end")),
        "period": str(raw.get("period") or "").strip(),
        "saved_at": str(raw.get("saved_at") or ""),
    }
    if (
        preset["bucket_start"] is not None
        and preset["bucket_end"] is not None
        and preset["bucket_start"] > preset["bucket_end"]
    ):
        preset["bucket_start"], preset["bucket_end"] = preset["bucket_end"], preset["bucket_start"]
    return preset


def load_presets(context: Any) -> list[dict[str, Any]]:
    try:
        path = presets_path(context)
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [item for item in (normalize(raw) for raw in data) if item]
    except (OSError, ValueError):
        pass
    return []


def find_preset(context: Any, name: Any) -> dict[str, Any] | None:
    target = str(name or "").strip().lower()
    if not target:
        return None
    return next((item for item in load_presets(context) if item["name"].lower() == target), None)


def _write(context: Any, items: list[dict[str, Any]]) -> None:
    path = presets_path(context)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def save_preset(context: Any, raw: Any) -> list[dict[str, Any]]:
    """Add or overwrite (same name, case-insensitive) a preset. Returns the sorted list."""
    if isinstance(raw, dict):
        raw = {**raw, "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    entry = normalize(raw)
    if entry is None:
        return load_presets(context)
    with _LOCK:
        items = [item for item in load_presets(context) if item["name"].lower() != entry["name"].lower()]
        items.append(entry)
        items.sort(key=lambda item: item["name"].lower())
        _write(context, items)
        return items


def delete_preset(context: Any, name: Any) -> list[dict[str, Any]]:
    target = str(name or "").strip().lower()
    with _LOCK:
        items = [item for item in load_presets(context) if item["name"].lower() != target]
        _write(context, items)
        return items


def excludes_tag(preset: dict[str, Any], tag: str, permanent: Any = "") -> bool:
    """Whether the preset's exclude or the shared ``permanent`` exclude would drop rows carrying ``tag``.

    Terms are read by the search engine's own parser (``SearchEngine._parse_query``) and matched
    like ``SearchEngine._apply_filters`` matches exclusions: ``~tag`` and ``*tag`` whole-tag, a
    plain term as a substring, ``{a|b}`` term by term.
    """
    from core.search_engine import SearchEngine

    wanted = " ".join(str(tag or "").replace("_", " ").lower().split())
    engine = SearchEngine()

    def hit(term: str, whole: bool) -> bool:
        term = term.strip().lower()
        return bool(term) and (term == wanted if whole else term in wanted)

    for text in (preset.get("exclude"), permanent):
        parsed = engine._parse_query(str(text or ""))
        if any(hit(term, False) for term in parsed["normal"]):
            return True
        if any(hit(term, True) for term in parsed["exact"] + parsed["not_exact"]):
            return True
        # Inside {a|b} the engine reads only `*` as whole-tag (SearchEngine or_term); `~` stays literal.
        for group in parsed.get("or") or []:
            if any(hit(term.lstrip("*"), term.startswith("*")) for term in group):
                return True
    return False


def global_permanent_exclude(context: Any) -> str:
    """The shared permanent exclude (search panel red row)."""
    normalize_state = getattr(context, "normalize_search_filter_state", None)
    state = getattr(context, "search_filter_state", None)
    if callable(normalize_state):
        state = normalize_state(state)
    return str((state or {}).get("exclude_permanent") or "").strip() if isinstance(state, dict) else ""


def condition_key(preset: dict[str, Any], permanent: Any = "") -> tuple:
    """Everything that decides the rows a search returns; a changed key means a stale pool."""
    return (
        preset.get("query", ""),
        preset.get("exclude", ""),
        str(permanent or "").strip(),
        tuple(preset.get("ratings") or ()),
        preset.get("bucket_start"),
        preset.get("bucket_end"),
    )
