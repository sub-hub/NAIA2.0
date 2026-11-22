"""Image lock: keep chosen parts of one image and let random rows supply the rest.

The user locks an image (result context menu > "이 그림으로 고정") and picks, per tag category,
where that category comes from on every generation:

- ``keep``: the locked image's tags (the chips the user selected),
- ``random``: the drawn random row's tags of that category,
- ``none``: nothing.

Examples: keep the look and let everything else roll (the old appearance lock); keep the look and
clothes and roll only pose; keep a whole scene and roll the character.

Where tags go (NAI):

- When features are kept, the kept identity (person, name, features, kept clothes) is sent as C1
  instead of the character module's slots, via ``sketchbook_character_prompts``. The module is
  never modified, so clearing the lock sends the real slots again.
- When features roll, the request carries no character prompt at all
  (``IMAGE_LOCK_NO_CHARACTERS``), so the row's character stays in the main prompt as in a
  normal random generation.
- Every other kept tag goes to the main prompt. Row tags of non-random categories are removed at
  the P.Eng consumption point (``apply_to_row``), and the P.Eng removal options mapped to lock
  categories are forced to match the lock (``forced_options``); stored presets stay unchanged.

Rows: only rows with the locked person's count tag (``1girl``) are drawn, and only ``solo`` rows
unless the partner toggle is on. In partner mode a row may add people of other genders but not a
second person of the locked gender, since C1 describes one person. Rows come from the lock pool of
the current SFW/NSFW mode when a search preset is assigned (``core.image_lock_pool``), otherwise
from the main pool.

NAI only; other modes ignore the lock. State lives in the session and is lost on restart;
the SFW/NSFW presets and the NSFW clothes default persist in user data.
"""
from __future__ import annotations

import json
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable

# Marks request params built under the lock, so Auto Generate drops the baked values and the
# lock dialog knows the image was drawn under a lock.
IMAGE_LOCK_MARKER = "_image_lock"
# Request flag: send no character prompts (features roll, the row's character stays in main).
IMAGE_LOCK_NO_CHARACTERS = "_image_lock_no_characters"
# Keys Auto Generate drops from baked overrides so edits or unlocking apply to the next image.
LIVE_REFETCH_KEYS = ("sketchbook_character_prompts", IMAGE_LOCK_NO_CHARACTERS, IMAGE_LOCK_MARKER)

KEEP, RANDOM, NONE = "keep", "random", "none"
MODES = (KEEP, RANDOM, NONE)
# Dialog order. ``person`` and ``meta`` are not lock rows: person tags always come from the row
# (the row filter makes them match), and meta tags are left to P.Eng.
CATEGORIES = (
    "names", "copyright", "features", "clothes",
    "expression", "pose", "composition", "location", "object", "other",
)
# Categories whose kept tags go to C1 while features are kept.
C1_CATEGORIES = ("names", "features", "clothes")
# Rows that cannot be "none": dropping the look or the whole category makes no sense.
NO_NONE = frozenset({"features"})

PRESETS: dict[str, dict[str, str]] = {
    # The former appearance lock.
    "look": {
        "names": KEEP, "copyright": NONE, "features": KEEP, "clothes": RANDOM,
        "expression": RANDOM, "pose": RANDOM, "composition": RANDOM,
        "location": RANDOM, "object": RANDOM, "other": RANDOM,
    },
    "character": {
        "names": KEEP, "copyright": KEEP, "features": KEEP, "clothes": KEEP,
        "expression": RANDOM, "pose": RANDOM, "composition": RANDOM,
        "location": RANDOM, "object": RANDOM, "other": RANDOM,
    },
    "scene": {
        "names": KEEP, "copyright": KEEP, "features": KEEP, "clothes": KEEP,
        "expression": RANDOM, "pose": RANDOM, "composition": KEEP,
        "location": KEEP, "object": KEEP, "other": KEEP,
    },
    "scene_new_character": {
        "names": RANDOM, "copyright": RANDOM, "features": RANDOM, "clothes": KEEP,
        "expression": KEEP, "pose": KEEP, "composition": KEEP,
        "location": KEEP, "object": KEEP, "other": KEEP,
    },
}
DEFAULT_PRESET = "look"

# P.Eng removal options mapped to the lock categories they remove.
OPTION_CATEGORIES: dict[str, tuple[str, ...]] = {
    "remove_character_name": ("names",),
    "remove_work_title": ("copyright",),
    "remove_character_features": ("features",),
    "remove_clothes": ("clothes",),
    "remove_clothing_event": ("clothes",),
    "remove_expression": ("expression",),
    # P.Eng's pose set also holds the composition tags (cowboy shot, from above).
    "remove_pose_action": ("pose", "composition"),
    "remove_location_and_background_color": ("location",),
    "remove_object_tags": ("object",),
}

PREFS_FILE = "remote_web_image_lock_prefs.json"
_PERSON_COUNT_RE = re.compile(r"^(\d+\+?)(girl|boy|other)s?$")
_MULTIPLE_RE = re.compile(r"^multiple (girl|boy|other)s$")
_PERSON_EXTRA = frozenset({"solo", "solo focus"})
# ``nahida (genshin impact)``: the qualifier names the work. General tags use the same shape
# (``sword (weapon)``), so the qualifier must be a known work (see ``_is_character_name``).
_NAME_QUALIFIER_RE = re.compile(r"^.+ \(([^()]+)\)$")

_LOCK = threading.RLock()
_PREFS_LOCK = threading.Lock()
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


def _units(value: Any) -> list[str]:
    """Comma units of a prompt text, keeping weighted groups whole."""
    from core.inset_feature_extract import _prompt_units

    return [unit.strip() for unit in _prompt_units(str(value or "")) if unit.strip()]


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        text = str(item or "").strip()
        key = _clean(text)
        if text and key not in seen:
            seen.add(key)
            out.append(text)
    return out


# -- person tags and the row filter ---------------------------------------------

def is_person_tag(tag: Any) -> bool:
    cleaned = _clean(tag)
    return bool(_PERSON_COUNT_RE.match(cleaned) or _MULTIPLE_RE.match(cleaned) or cleaned in _PERSON_EXTRA)


def main_person_tag(person_tags: Iterable[Any]) -> str:
    """The single-person count tag of the locked image (``1girl``), or ``""``."""
    for tag in person_tags:
        match = _PERSON_COUNT_RE.match(_clean(tag))
        if match and match.group(1) == "1":
            return _clean(tag)
    return ""


def partner_active(lock: dict[str, Any]) -> bool:
    """The partner toggle applies to NSFW only; it stays set so switching back to NSFW keeps it."""
    return bool(lock.get("partner")) and bool(lock.get("nsfw"))


def row_rule_text(lock: dict[str, Any]) -> str:
    """Which rows the lock draws, for the badge and messages (``solo 인 1girl 줄``)."""
    who = lock.get("main_person") or ""
    if partner_active(lock):
        return f"{who or '인물'} 이 한 명인 줄 (상대 인물 허용)"
    return f"solo 인 {who} 줄" if who else "solo 줄"


def row_filter(lock: dict[str, Any] | None) -> Callable[[set[str]], bool] | None:
    """Predicate over a row's tag set, or None for no restriction."""
    if not lock:
        return None
    main = str(lock.get("main_person") or "")
    main_match = _PERSON_COUNT_RE.match(main)
    gender = main_match.group(2) if main_match else ""
    partner = partner_active(lock)

    def accept(tags: set[str]) -> bool:
        if main and main not in tags:
            return False
        if not partner:
            return "solo" in tags
        if not gender:
            return True
        for tag in tags:
            match = _PERSON_COUNT_RE.match(tag)
            if match and match.group(2) == gender and match.group(1) != "1":
                return False
            multiple = _MULTIPLE_RE.match(tag)
            if multiple and multiple.group(1) == gender:
                return False
        return True

    return accept


# -- tag classification -----------------------------------------------------------

_dictionary_cache: dict[str, dict[str, str]] = {}


def _tag_dictionary(repo_root: str | Path | None) -> dict[str, str]:
    """``tag -> category`` from data/KR_tags.parquet (``구성 > 샷``, ``저작권 > 게임`` ...)."""
    root = str(Path(repo_root) if repo_root else Path(__file__).resolve().parents[1])
    cached = _dictionary_cache.get(root)
    if cached is not None:
        return cached
    table: dict[str, str] = {}
    try:
        import pandas as pd

        frame = pd.read_parquet(Path(root) / "data" / "KR_tags.parquet", columns=["tag", "category"])
        for tag, category in zip(frame["tag"].astype(str), frame["category"].astype(str)):
            table[_clean(tag)] = category.strip()
    except Exception:      # noqa: BLE001 - without the dictionary composition, names and copyright fall to other
        table = {}
    _dictionary_cache[root] = table
    return table


def _is_composition(category: str) -> bool:
    top = category.split(">", 1)[0].strip()
    return top in {"구성", "시점"} or "구도" in category or "프레이밍" in category


def _is_character_name(tag: str, dictionary: dict[str, str]) -> bool:
    """A character in the tag dictionary, or ``name (work)`` whose qualifier is a known work."""
    if dictionary.get(tag, "").startswith("캐릭터"):
        return True
    match = _NAME_QUALIFIER_RE.match(tag)
    return bool(match) and dictionary.get(match.group(1).strip(), "").startswith("저작권")


def classify_tags(
    tags: Iterable[Any],
    filter_manager: Any = None,
    repo_root: str | Path | None = None,
) -> dict[str, list[str]]:
    """Split plain tags into lock categories plus ``person`` and ``meta``.

    Features and clothes use the character outfit catalog first (it knows wings, breast size and
    head accessories that P.Eng's feature list misses), then P.Eng's category sets. Composition
    comes from the tag dictionary. Unknown tags that the dictionary marks as a character or a
    work become names or copyright (``name (work)`` counts as a name when the work is known).
    """
    from core.character_outfit_filter import _catalog_entry, _catalog_verdict, _vocabulary, is_accessory_or_headwear
    from core.prompt_category_annotation import _category_sets, classify

    groups: dict[str, list[str]] = {key: [] for key in (*CATEGORIES, "person", "meta")}
    vocabulary = _vocabulary(repo_root)
    sets = _category_sets(filter_manager) if filter_manager is not None else {}
    dictionary = _tag_dictionary(repo_root)
    seen: set[str] = set()
    for raw in tags:
        tag = _clean(raw)
        if not tag or tag in seen:
            continue
        seen.add(tag)
        if is_person_tag(tag):
            groups["person"].append(tag)
            continue
        entry = _catalog_entry(tag, vocabulary["catalog"])
        if entry is not None:
            groups["features" if _catalog_verdict(tag, entry) else "clothes"].append(tag)
            continue
        if tag in vocabulary["traits"] or is_accessory_or_headwear(tag, repo_root):
            groups["features"].append(tag)
            continue
        category = classify(tag, sets) if sets else "extra"
        known = dictionary.get(tag, "")
        if category == "features":
            groups["features"].append(tag)
        elif category in ("clothes", "clothing_event"):
            groups["clothes"].append(tag)
        elif category == "expression":
            groups["expression"].append(tag)
        elif category in ("pose_action", "meta", "extra") and known and _is_composition(known):
            groups["composition"].append(tag)
        elif category == "pose_action":
            groups["pose"].append(tag)
        elif category == "location":
            groups["location"].append(tag)
        elif category == "object":
            groups["object"].append(tag)
        elif category == "meta":
            groups["meta"].append(tag)
        elif _is_character_name(tag, dictionary):
            groups["names"].append(tag)
        elif known.startswith("저작권"):
            groups["copyright"].append(tag)
        else:
            groups["other"].append(tag)
    return groups


# -- state ------------------------------------------------------------------------

def _stored(context: Any) -> dict[str, Any] | None:
    value = getattr(context, "image_lock", None)
    return value if isinstance(value, dict) else None


def _bump(context: Any) -> None:
    context.image_lock_seq = int(getattr(context, "image_lock_seq", 0) or 0) + 1


def normalize_modes(raw: Any, available: dict[str, list[str]] | None = None) -> dict[str, str]:
    """Fill missing categories from the default preset and drop invalid values.

    ``keep`` on a category the locked image has no tags for becomes ``none`` (``random`` for
    the categories in ``NO_NONE``), and ``none`` on those becomes ``random``.
    """
    base = dict(PRESETS[DEFAULT_PRESET])
    if isinstance(raw, dict):
        for key in CATEGORIES:
            value = str(raw.get(key) or "").strip().lower()
            if value in MODES:
                base[key] = value
    for key in NO_NONE:
        if base[key] == NONE:
            base[key] = RANDOM
    if available is not None:
        for key in CATEGORIES:
            if base[key] == KEEP and not available.get(key):
                base[key] = NONE if key not in NO_NONE else RANDOM
    return base


def c1_mode(lock: dict[str, Any]) -> bool:
    """True when the kept look is sent as C1 (features kept)."""
    return lock.get("modes", {}).get("features") == KEEP and bool(lock.get("kept", {}).get("features"))


def compose_c1(lock: dict[str, Any]) -> str:
    modes = lock.get("modes", {})
    kept = lock.get("kept", {})
    units = [str(lock.get("person_label") or "")]
    for key in C1_CATEGORIES:
        if modes.get(key) == KEEP:
            units.extend(kept.get(key) or [])
    return ", ".join(_dedupe(units))


def body_tags(lock: dict[str, Any]) -> list[str]:
    """Kept tags that go to the main prompt."""
    modes = lock.get("modes", {})
    kept = lock.get("kept", {})
    in_c1 = set(C1_CATEGORIES) if c1_mode(lock) else set()
    out: list[str] = []
    for key in CATEGORIES:
        if key not in in_c1 and modes.get(key) == KEEP:
            out.extend(kept.get(key) or [])
    return _dedupe(out)


def active_lock(context: Any, api_mode: str | None = None) -> dict[str, Any] | None:
    """Return a copy of the lock that applies to generation now, or None (no lock, or not NAI)."""
    mode = str(api_mode or "").upper() or _api_mode(context)
    if mode != "NAI":
        return None
    with _LOCK:
        lock = _stored(context)
        return json.loads(json.dumps(lock)) if lock else None


def current_preset_name(lock: dict[str, Any] | None, prefs: dict[str, Any]) -> str:
    if not lock:
        return ""
    return str(prefs.get("nsfw_preset" if lock.get("nsfw") else "sfw_preset") or "")


def public_state(context: Any) -> dict[str, Any]:
    """State sent to clients. ``active`` means the lock applies to generation (a lock exists and the mode is NAI)."""
    with _LOCK:
        lock = _stored(context)
        seq = int(getattr(context, "image_lock_seq", 0) or 0)
        stored = json.loads(json.dumps(lock)) if lock else None
    prefs = load_prefs(context)
    state: dict[str, Any] = {
        "type": "image_lock_state",
        "active": bool(stored) and _api_mode(context) == "NAI",
        "seq": seq,
        "sync_session": _SYNC_SESSION,
        "prefs": prefs,
    }
    if stored:
        state.update({
            "modes": stored["modes"],
            "kept": stored["kept"],
            "groups": stored.get("groups") or {},
            "nsfw": bool(stored.get("nsfw")),
            "partner": bool(stored.get("partner")),
            "main_person": stored.get("main_person") or "",
            "sfw_clothes_mode": stored.get("sfw_clothes_mode") or "",
            "c1_mode": c1_mode(stored),
            "prompt": compose_c1(stored) if c1_mode(stored) else "",
            "row_rule": row_rule_text(stored),
            "uc": str(stored.get("uc") or ""),
            "source_label": str(stored.get("source_label") or ""),
            "pool_preset": current_preset_name(stored, prefs),
        })
    return state


def set_lock(
    context: Any,
    *,
    groups: Any,
    kept: Any,
    modes: Any,
    partner: Any = False,
    nsfw: Any = False,
    uc: Any = "",
    source_label: Any = "",
    sfw_clothes_mode: Any = None,
) -> dict[str, Any]:
    """Create the lock from the dialog's choice.

    ``groups`` are the candidate tags of the locked image per category (kept for reopening the
    dialog from the badge), ``kept`` the selected chips per category.
    """
    clean_groups = _clean_groups(groups)
    clean_kept = {key: _dedupe((kept or {}).get(key) or []) if isinstance(kept, dict) else [] for key in CATEGORIES}
    clean_modes = normalize_modes(modes, clean_kept)
    if clean_modes["features"] == KEEP and not clean_kept["features"]:
        raise ValueError("고정할 외형 태그를 하나 이상 골라 주세요. 외형을 바꾸려면 외형 칸을 랜덤으로 두세요.")
    if not any(mode == KEEP for mode in clean_modes.values()):
        raise ValueError("원본으로 둘 칸이 하나도 없습니다.")
    person = clean_groups.get("person") or []
    main = main_person_tag(person)
    from core.inset_feature_extract import PERSON_RENAME

    with _LOCK:
        context.image_lock = {
            "groups": clean_groups,
            "kept": clean_kept,
            "modes": clean_modes,
            "main_person": main,
            "person_label": PERSON_RENAME.get(main, ""),
            "partner": bool(partner),
            "nsfw": bool(nsfw),
            "uc": _normalize_prompt(uc),
            "source_label": str(source_label or "")[:200],
            # Clothes mode to restore when NSFW is turned off again.
            "sfw_clothes_mode": sfw_clothes_mode if sfw_clothes_mode in MODES else clean_modes["clothes"],
        }
        _bump(context)
    return public_state(context)


def _normalize_prompt(value: Any) -> str:
    return str(value or "").strip().strip(",").strip()


def _clean_groups(groups: Any) -> dict[str, list[str]]:
    if not isinstance(groups, dict):
        return {key: [] for key in (*CATEGORIES, "person")}
    return {key: _dedupe(groups.get(key) or []) for key in (*CATEGORIES, "person")}


def update_lock(
    context: Any,
    *,
    modes: Any = None,
    kept: Any = None,
    prompt: Any = None,
    uc: Any = None,
    partner: Any = None,
    nsfw: Any = None,
    sfw_clothes_mode: Any = None,
) -> dict[str, Any]:
    """Change an active lock: dialog edits, the quick panel's C1 text, the badge toggles.

    A SFW/NSFW switch is applied before ``modes``, so modes sent with it (the dialog) win over
    the NSFW clothes default; the badge sends the switch alone and gets the default.
    """
    with _LOCK:
        lock = _stored(context)
        if not lock:
            raise ValueError("고정이 꺼져 있습니다.")
        lock = json.loads(json.dumps(lock))
        if kept is not None and isinstance(kept, dict):
            lock["kept"] = {key: _dedupe(kept.get(key) or []) for key in CATEGORIES}
        if nsfw is not None and bool(nsfw) != bool(lock.get("nsfw")):
            lock["nsfw"] = bool(nsfw)
            if lock["nsfw"]:
                lock["sfw_clothes_mode"] = lock["modes"]["clothes"]
                lock["modes"]["clothes"] = load_prefs(context)["nsfw_clothes_mode"]
            else:
                lock["modes"]["clothes"] = lock.get("sfw_clothes_mode") or lock["modes"]["clothes"]
            lock["modes"] = normalize_modes(lock["modes"], lock["kept"])
        if modes is not None:
            lock["modes"] = normalize_modes(modes, lock["kept"])
            if not lock.get("nsfw"):
                lock["sfw_clothes_mode"] = lock["modes"]["clothes"]
        if sfw_clothes_mode in MODES:
            lock["sfw_clothes_mode"] = sfw_clothes_mode
        if prompt is not None:
            if not c1_mode(lock):
                raise ValueError("외형을 C1 으로 보내지 않는 고정입니다.")
            _apply_c1_text(lock, prompt)
        if uc is not None:
            lock["uc"] = _normalize_prompt(uc)
        if partner is not None:
            lock["partner"] = bool(partner)
        context.image_lock = lock
        _bump(context)
    return public_state(context)


def _apply_c1_text(lock: dict[str, Any], prompt: Any) -> None:
    """Re-split an edited C1 text into the kept categories.

    Units already kept as names or clothes stay there; the person label is dropped (it is
    added back from the locked person); every other unit becomes a kept feature.
    """
    units = _units(prompt)
    label = _clean(lock.get("person_label"))
    names = {_clean(unit) for unit in lock["kept"].get("names") or []}
    clothes = {_clean(unit) for unit in lock["kept"].get("clothes") or []}
    kept = {"names": [], "features": [], "clothes": []}
    for unit in units:
        key = _clean(unit)
        if key == label:
            continue
        if key in names:
            kept["names"].append(unit)
        elif key in clothes:
            kept["clothes"].append(unit)
        else:
            kept["features"].append(unit)
    if not kept["features"]:
        raise ValueError("외형을 비울 수 없습니다. 풀려면 고정 해제를 누르세요.")
    for key, value in kept.items():
        if lock["modes"].get(key) == KEEP:
            lock["kept"][key] = value


def clear_lock(context: Any) -> dict[str, Any]:
    with _LOCK:
        had = _stored(context) is not None
        context.image_lock = None
        if had:
            _bump(context)
    return public_state(context)


# -- preferences (persist across restarts) ----------------------------------------

def _prefs_path(context: Any) -> Path | None:
    saver = getattr(context, "_save_path", None)
    return Path(saver(PREFS_FILE)) if callable(saver) else None


def load_prefs(context: Any) -> dict[str, Any]:
    prefs: dict[str, Any] = {"sfw_preset": "", "nsfw_preset": "", "nsfw_clothes_mode": RANDOM}
    path = _prefs_path(context)
    try:
        if path is not None and path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                prefs["sfw_preset"] = str(data.get("sfw_preset") or "")
                prefs["nsfw_preset"] = str(data.get("nsfw_preset") or "")
                if data.get("nsfw_clothes_mode") in MODES:
                    prefs["nsfw_clothes_mode"] = data["nsfw_clothes_mode"]
    except (OSError, ValueError):
        pass
    return prefs


def save_prefs(context: Any, updates: Any) -> dict[str, Any]:
    with _PREFS_LOCK:
        prefs = load_prefs(context)
        if isinstance(updates, dict):
            for key in ("sfw_preset", "nsfw_preset"):
                if key in updates:
                    prefs[key] = str(updates.get(key) or "").strip()
            if updates.get("nsfw_clothes_mode") in MODES:
                prefs["nsfw_clothes_mode"] = updates["nsfw_clothes_mode"]
        path = _prefs_path(context)
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(prefs, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, path)
    return prefs


# -- generation -------------------------------------------------------------------

def generation_params(context: Any, api_mode: str | None = None) -> dict[str, Any]:
    """Params that replace the character slots for a plain NAI request; empty when no lock applies."""
    lock = active_lock(context, api_mode)
    if not lock:
        return {}
    if c1_mode(lock):
        return {
            "sketchbook_character_prompts": [{"prompt": compose_c1(lock), "uc": str(lock.get("uc") or "")}],
            IMAGE_LOCK_MARKER: True,
        }
    return {IMAGE_LOCK_NO_CHARACTERS: True, IMAGE_LOCK_MARKER: True}


def forced_options(lock: dict[str, Any] | None) -> dict[str, bool]:
    """P.Eng removal options the lock decides: off when a mapped category rolls, else on."""
    if not lock:
        return {}
    modes = lock.get("modes", {})
    return {
        option: not any(modes.get(key) == RANDOM for key in keys)
        for option, keys in OPTION_CATEGORIES.items()
    }


def apply_to_row(
    main_tags: list[str],
    removed_tags: list[str],
    lock: dict[str, Any],
    filter_manager: Any = None,
    repo_root: str | Path | None = None,
) -> list[str]:
    """Shape a random row's main tags in place: keep only rolling categories, then add the kept tags.

    Person and meta tags of the row stay (P.Eng decides about meta). Returns the removed tags,
    which are also appended to ``removed_tags``.
    """
    modes = lock.get("modes", {})
    groups = classify_tags(main_tags, filter_manager, repo_root)
    drop = {
        tag
        for key in CATEGORIES
        if modes.get(key) != RANDOM
        for tag in groups.get(key) or []
    }
    removed: list[str] = []
    for tag in list(main_tags):
        if _clean(tag) in drop:
            main_tags.remove(tag)
            removed_tags.append(tag)
            removed.append(tag)
    present = {_clean(tag) for tag in main_tags}
    additions = [tag for tag in body_tags(lock) if _clean(tag) not in present]
    main_tags[0:0] = additions
    return removed


def pop_random_row(context: Any, search_results: Any, active_ratings: Any) -> Any:
    """Pop the next random row; while locked, from the lock pool and through the person filter."""
    lock = active_lock(context)
    if lock is None:
        return search_results.pop_random_row(active_ratings)
    accept = row_filter(lock)
    from core import image_lock_pool

    preset = current_preset_name(lock, load_prefs(context))
    pool = image_lock_pool.pool_for(context, preset)
    if pool is None:
        return search_results.pop_random_row_where(active_ratings, accept)
    # The lock pool already holds only the preset's ratings.
    row = pool.pop_random_row_where(None, accept)
    if row is None and image_lock_pool.refill(context, preset):
        pool = image_lock_pool.pool_for(context, preset)
        row = pool.pop_random_row_where(None, accept) if pool is not None else None
    return row


def empty_pool_message(lock: dict[str, Any] | None, prefs: dict[str, Any]) -> str:
    if not lock:
        return ""
    preset = current_preset_name(lock, prefs)
    where = f"프리셋 \"{preset}\" 의 풀" if preset else "지금 풀"
    return f"고정 중에는 {row_rule_text(lock)}만 뽑습니다 - {where}에 남은 줄이 없습니다."


# -- candidates for the lock dialog -----------------------------------------------

def tags_from_prompt(prompt: Any) -> list[str]:
    """Extract plain tags from a saved prompt.

    Saved images do not keep P.Eng category annotations (they are stripped before sending), so
    the prompt is re-classified. Weighted groups (``1.0::artist:x ::``), brace groups, artist
    tags and comment lines are skipped.
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
            tags.append(text.replace("\\(", "(").replace("\\)", ")"))
    return tags


def split_csv(value: Any) -> list[str]:
    import pandas as pd

    if value is None:
        return []
    try:
        if pd.isna(value):
            return []
    except (TypeError, ValueError):     # pd.isna on a list-like is not a scalar answer
        pass
    return [part.strip() for part in str(value).split(",") if part.strip()]


def candidates(
    body_tags_in: Iterable[Any],
    *,
    names: Iterable[Any] = (),
    copyright_tags: Iterable[Any] = (),
    character_prompt: Any = "",
    filter_manager: Any = None,
    repo_root: str | Path | None = None,
) -> dict[str, list[str]]:
    """Group the locked image's tags for the dialog.

    ``body_tags_in`` are the main prompt tags without P.Eng's prefix and suffix when known.
    ``character_prompt`` is the C1 text that was sent. It was written by a person (or by an
    earlier lock), so it is kept as whole units, weights included; only clothing units are split
    off, and its person label (``girl``) is dropped.
    """
    from core.inset_feature_extract import PERSON_RENAME, split_character_prompt

    groups = classify_tags(body_tags_in, filter_manager, repo_root)
    labels = set(PERSON_RENAME.values())
    if str(character_prompt or "").strip():
        c1 = split_character_prompt(character_prompt, repo_root)
        c1_features = [unit for unit in c1.get("features") or [] if _clean(unit) not in labels]
        # A character name written into C1 (an earlier lock puts it there) is a name, not a feature.
        dictionary = _tag_dictionary(repo_root)
        c1_names = [unit for unit in c1_features if _is_character_name(_clean(unit), dictionary)]
        c1_features = [unit for unit in c1_features if unit not in c1_names]
        groups["names"] = _dedupe([*c1_names, *groups["names"]])
        groups["features"] = _dedupe([*c1_features, *groups["features"]])
        groups["clothes"] = _dedupe([*(c1.get("clothes") or []), *groups["clothes"]])
        person = [tag for tag, label in PERSON_RENAME.items()
                  if any(_clean(unit) == label for unit in c1.get("features") or [])]
        groups["person"] = _dedupe([*person, *groups["person"]])
    groups["names"] = _dedupe([*(_clean(tag) for tag in names), *groups["names"]])
    groups["copyright"] = _dedupe([*(_clean(tag) for tag in copyright_tags), *groups["copyright"]])
    return groups
