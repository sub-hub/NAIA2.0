"""Lock pools: random rows for the image lock, searched with a saved search preset.

The image lock borrows poses (or whatever categories roll) from a pool of its own, so the main
pool the user picks characters from stays untouched. The SFW and NSFW modes of the lock each
point to a search preset (``core.search_presets``); the pool for a preset is searched once and
kept in memory, with the shared permanent exclude applied. It is searched again when the
preset's condition or the permanent exclude changed, or the pool ran out.

Searching needs the tag archive, which lives in the server layer, so the server installs a
builder on the context (``context.image_lock_pool_builder``: (preset, permanent) -> DataFrame).
Without a builder, or without a preset, the lock draws from the main pool.
"""
from __future__ import annotations

import threading
from typing import Any

_LOCK = threading.RLock()


def _pools(context: Any) -> dict[str, dict[str, Any]]:
    pools = getattr(context, "image_lock_pools", None)
    if not isinstance(pools, dict):
        pools = {}
        context.image_lock_pools = pools
    return pools


def _build(context: Any, preset: dict[str, Any], permanent: str) -> Any:
    from core.search_result_model import SearchResultModel

    builder = getattr(context, "image_lock_pool_builder", None)
    if not callable(builder):
        return None
    frame = builder(preset, permanent)
    return SearchResultModel(frame)


def pool_for(context: Any, preset_name: str) -> Any:
    """The SearchResultModel for ``preset_name``, searching it first when needed.

    Returns None when no preset is set or it no longer exists (the caller then uses the main pool).
    """
    from core import search_presets

    name = str(preset_name or "").strip()
    if not name:
        return None
    preset = search_presets.find_preset(context, name)
    if preset is None:
        return None
    permanent = search_presets.global_permanent_exclude(context)
    key = search_presets.condition_key(preset, permanent)
    with _LOCK:
        entry = _pools(context).get(name.lower())
        if entry is not None and entry["key"] == key:
            return entry["model"]
        model = _build(context, preset, permanent)
        if model is None:
            return None
        _pools(context)[name.lower()] = {"key": key, "model": model}
        return model


def refill(context: Any, preset_name: str) -> bool:
    """Search the preset again after its pool ran out. Returns True when the new pool has rows."""
    name = str(preset_name or "").strip().lower()
    with _LOCK:
        _pools(context).pop(name, None)
    model = pool_for(context, preset_name)
    return model is not None and model.get_count() > 0


def status(context: Any, preset_name: str) -> dict[str, Any]:
    """Row count of a built pool; ``built`` is False before the first search."""
    name = str(preset_name or "").strip().lower()
    with _LOCK:
        entry = _pools(context).get(name)
    if entry is None:
        return {"built": False, "remaining": 0}
    return {"built": True, "remaining": int(entry["model"].get_count())}

