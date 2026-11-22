"""Appearance lock: keep a random character's look while pose, scene, rating and art style keep rolling.

In random generation a "character" is usually a bundle of appearance tags (hair, eyes, body,
wings, ...) rather than a single name tag. While a lock is active:

1. The locked tags are sent as the NAI C1 character prompt instead of the character module's
   slots (a bypass, like the reference inset's virtual characters, via
   ``sketchbook_character_prompts``). The character module itself is never modified, so
   clearing the lock sends the real C1 again.
2. Character name, copyright and appearance tags (plus clothes when ``include_clothes``) are
   stripped from the random row regardless of the preset's P.Eng options. The stored options
   are left alone; the override is applied only where the options are consumed
   (``prompt_engineering_runtime``), so switching presets cannot undo it and clearing the lock
   needs nothing restored.
3. Only rows tagged ``solo`` are picked, because a single locked character does not fit rows
   such as ``2girls`` or ``hug``.

NAI only (character prompts exist only there); other modes ignore the lock. The state lives in
the session and is lost on restart.
"""
from __future__ import annotations

import threading
import uuid
from pathlib import Path
from typing import Any, Iterable

# Marks request params whose C1 came from the lock, so Auto Generate can drop the baked value
# and re-read the live lock for the next image.
APPEARANCE_LOCK_MARKER = "_appearance_lock"
# Preprocessing options forced on while locked; clothes are added only with ``include_clothes``.
FORCED_OPTIONS = ("remove_character_name", "remove_work_title", "remove_character_features")
CLOTHES_OPTION = "remove_clothes"
SOLO_TAG = "solo"
# Group label used by ``split_character_features`` for clothing tags.
CLOTHES_GROUP = "의상"
# Keys Auto Generate drops from baked overrides so edits or unlocking apply to the next image.
LIVE_REFETCH_KEYS = ("sketchbook_character_prompts", APPEARANCE_LOCK_MARKER)

_LOCK = threading.RLock()
# Identifies this server process. ``seq`` restarts at 0 after a restart, so clients only
# discard older states that carry the same token.
_SYNC_SESSION = uuid.uuid4().hex[:12]


def _clean(tag: Any) -> str:
    return str(tag or "").replace("_", " ").strip().lower()


def _api_mode(context: Any) -> str:
    getter = getattr(context, "get_api_mode", None)
    try:
        mode = getter() if callable(getter) else getattr(context, "api_mode", "")
    except Exception:      # noqa: BLE001 - an unreadable mode means "not locked"
        mode = ""
    return str(mode or "").upper()


def _stored(context: Any) -> dict[str, Any] | None:
    value = getattr(context, "appearance_lock", None)
    return value if isinstance(value, dict) and str(value.get("prompt") or "").strip() else None


def _bump(context: Any) -> None:
    context.appearance_lock_seq = int(getattr(context, "appearance_lock_seq", 0) or 0) + 1


def _normalize_prompt(value: Any) -> str:
    return str(value or "").strip().strip(",").strip()


# -- state ---------------------------------------------------------------------

def active_lock(context: Any, api_mode: str | None = None) -> dict[str, Any] | None:
    """Return a copy of the lock that applies to generation now, or None (no lock, or not NAI)."""
    mode = str(api_mode or "").upper() or _api_mode(context)
    if mode != "NAI":
        return None
    with _LOCK:
        lock = _stored(context)
        return dict(lock) if lock else None


def forced_options(lock: dict[str, Any] | None) -> list[str]:
    if not lock:
        return []
    keys = list(FORCED_OPTIONS)
    if lock.get("include_clothes"):
        keys.append(CLOTHES_OPTION)
    return keys


def public_state(context: Any) -> dict[str, Any]:
    """State sent to clients. ``active`` means the lock applies to generation (a lock exists and the mode is NAI)."""
    with _LOCK:
        lock = _stored(context)
        seq = int(getattr(context, "appearance_lock_seq", 0) or 0)
        stored = dict(lock) if lock else None
    return {
        "type": "appearance_lock_state",
        "active": bool(stored) and _api_mode(context) == "NAI",
        "prompt": str(stored.get("prompt") or "") if stored else "",
        "uc": str(stored.get("uc") or "") if stored else "",
        "include_clothes": bool(stored.get("include_clothes")) if stored else False,
        "source_label": str(stored.get("source_label") or "") if stored else "",
        "seq": seq,
        "sync_session": _SYNC_SESSION,
    }


def set_lock(
    context: Any,
    *,
    prompt: Any,
    uc: Any = "",
    include_clothes: Any = False,
    source_label: Any = "",
) -> dict[str, Any]:
    text = _normalize_prompt(prompt)
    if not text:
        raise ValueError("고정할 외형 태그가 없습니다.")
    with _LOCK:
        context.appearance_lock = {
            "prompt": text,
            "uc": _normalize_prompt(uc),
            "include_clothes": bool(include_clothes),
            "source_label": str(source_label or "")[:200],
        }
        _bump(context)
    return public_state(context)


def update_lock(context: Any, *, prompt: Any = None, uc: Any = None) -> dict[str, Any]:
    """Apply an edit from the quick panel. An empty prompt is rejected; unlocking is explicit."""
    with _LOCK:
        lock = _stored(context)
        if not lock:
            raise ValueError("외형 고정이 꺼져 있습니다.")
        lock = dict(lock)
        if prompt is not None:
            text = _normalize_prompt(prompt)
            if not text:
                raise ValueError("외형을 비울 수 없습니다. 풀려면 외형 고정 해제를 누르세요.")
            lock["prompt"] = text
        if uc is not None:
            lock["uc"] = _normalize_prompt(uc)
        context.appearance_lock = lock
        _bump(context)
    return public_state(context)


def clear_lock(context: Any) -> dict[str, Any]:
    with _LOCK:
        had = _stored(context) is not None
        context.appearance_lock = None
        if had:
            _bump(context)
    return public_state(context)


# -- generation ----------------------------------------------------------------

def generation_params(context: Any, api_mode: str | None = None) -> dict[str, Any]:
    """Params that replace C1 for a plain NAI request; empty when no lock applies."""
    lock = active_lock(context, api_mode)
    if not lock:
        return {}
    return {
        "sketchbook_character_prompts": [{"prompt": lock["prompt"], "uc": str(lock.get("uc") or "")}],
        APPEARANCE_LOCK_MARKER: True,
    }


def pop_random_row(context: Any, search_results: Any, active_ratings: Any) -> Any:
    """Pop the next random row: ``solo`` rows only while locked, otherwise unchanged."""
    if active_lock(context) is None:
        return search_results.pop_random_row(active_ratings)
    return search_results.pop_random_row_with_tag(active_ratings, SOLO_TAG)


def strip_row_tags(
    main_tags: list[str],
    removed_tags: list[str],
    lock: dict[str, Any],
    filter_manager: Any = None,
    repo_root: str | Path | None = None,
) -> list[str]:
    """Remove appearance (and optionally clothing) tags from a random row in place.

    Uses the same classifier as the character asset feature extraction. It covers more than
    P.Eng's ``characteristic_list`` (wings, breast size and head accessories are missing there).
    Person-count tags such as ``1girl`` and tags the classifier does not know are kept.
    Returns the removed tags, which are also appended to ``removed_tags``.
    """
    from core.inset_feature_extract import PERSON_RENAME, split_character_features

    if not main_tags:
        return []
    split = split_character_features([], list(main_tags), filter_manager, repo_root)
    drop = set(split.get("keep") or [])
    if lock.get("include_clothes"):
        drop.update(item["tag"] for item in split.get("dropped") or [] if item.get("group") == CLOTHES_GROUP)
    removed: list[str] = []
    for tag in list(main_tags):
        cleaned = _clean(tag)
        if cleaned in PERSON_RENAME or cleaned not in drop:
            continue
        main_tags.remove(tag)
        removed_tags.append(tag)
        removed.append(tag)
    return removed


# -- candidates for the lock dialog --------------------------------------------

def tags_from_prompt(prompt: Any) -> list[str]:
    """Extract plain tags from a saved prompt.

    Saved images do not keep P.Eng category annotations (they are stripped before sending), so
    the prompt is re-classified. Weighted groups (``1.0::artist:x ::``), brace groups, artist
    tags and comment lines never describe the character and are skipped.
    """
    from core.inset_feature_extract import _prompt_units

    tags: list[str] = []
    for line in str(prompt or "").splitlines():
        if line.strip().startswith("#"):
            continue
        for unit in _prompt_units(line):
            text = unit.strip()
            if not text or text.startswith("#"):
                continue
            if "::" in text or text[0] in "{[(<" or text.startswith("artist:"):
                continue
            tags.append(text)
    return tags


def _split_csv(value: Any) -> list[str]:
    import pandas as pd

    if value is None:
        return []
    try:
        if pd.isna(value):
            return []
    except (TypeError, ValueError):     # pd.isna on a list-like is not a scalar answer
        pass
    return [part.strip() for part in str(value).split(",") if part.strip()]


def tags_from_source_row(row: Any) -> tuple[list[str], list[str]]:
    """Return ``(character name tags, general tags)`` of an in-memory random row."""
    getter = getattr(row, "get", None)
    if not callable(getter):
        return [], []
    return _split_csv(getter("character")), _split_csv(getter("general"))


def candidates(
    general_tags: Iterable[Any],
    character_tags: Iterable[Any] = (),
    filter_manager: Any = None,
    repo_root: str | Path | None = None,
    character_prompt: Any = "",
) -> dict[str, list[str]]:
    """Group tags for the lock dialog.

    ``character_prompt`` is the C1 text that was sent with the image. It was written by a
    person (or by an earlier lock), so it is kept as whole units, weights included, and only
    clothing units are split off. Tags from the random row or base prompt go through the
    classifier. The dialog preselects person, C1, names, features and accessories; clothes and
    unknown tags start unselected. ``others`` (pose, expression, background, meta) are only counted.
    """
    from core.character_outfit_filter import is_accessory_or_headwear
    from core.inset_feature_extract import PERSON_RENAME, split_character_features, split_character_prompt

    c1 = split_character_prompt(character_prompt, repo_root) if str(character_prompt or "").strip() else {}
    c1_features = [unit.strip() for unit in c1.get("features") or [] if unit.strip()]
    c1_clothes = [unit.strip() for unit in c1.get("clothes") or [] if unit.strip()]
    in_c1 = {_clean(unit) for unit in c1_features + c1_clothes}

    names = [_clean(tag) for tag in character_tags if _clean(tag)]
    general = [tag for tag in general_tags if _clean(tag) not in in_c1]
    split = split_character_features(names, general, filter_manager, repo_root)
    person_values = set(PERSON_RENAME.values())
    name_set = set(names)
    person: list[str] = []
    features: list[str] = []
    accessories: list[str] = []
    for tag in split.get("keep") or []:
        if tag in name_set or tag in in_c1:
            continue
        if tag in person_values:
            person.append(tag)
        elif is_accessory_or_headwear(tag, repo_root):
            accessories.append(tag)
        else:
            features.append(tag)
    dropped = split.get("dropped") or []
    return {
        "person": person,
        "character": c1_features,
        "names": names,
        "features": features,
        "accessories": accessories,
        "clothes": c1_clothes + [item["tag"] for item in dropped if item.get("group") == CLOTHES_GROUP],
        "unknown": [tag for tag in split.get("unknown") or [] if tag not in in_c1],
        "others": [item["tag"] for item in dropped if item.get("group") != CLOTHES_GROUP],
    }
