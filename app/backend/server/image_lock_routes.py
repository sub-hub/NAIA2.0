"""REST routes for the image lock: pick candidates, lock, edit, toggle SFW/NSFW and clear.

The rules live in ``core.image_lock``; these routes parse requests, build lock pools from
search presets (the tag archive search lives in this layer) and broadcast the resulting state.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.backend.server.result_display_routes import history_item_from_viewer_path
from core import image_lock, image_lock_pool
from core.web_session_context import WebSessionContext

AsyncRunner = Callable[..., Awaitable[Any]]


async def _payload(req: Request) -> dict[str, Any]:
    try:
        data = await req.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _executed_c1(params: dict[str, Any]) -> str:
    """C1 text that was actually sent for a history item, if any."""
    executed = params.get("_executed_characters")
    if isinstance(executed, list) and executed:
        return str(executed[0] or "")
    for entry in params.get("sketchbook_character_prompts") or []:
        if isinstance(entry, dict):
            return str(entry.get("prompt") or "")
        if isinstance(entry, (list, tuple)) and entry:
            return str(entry[0] or "")
    return ""


def _candidate_source(item: Any, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Return ``(source, candidate kwargs)`` for the image being locked.

    - ``body``: the request kept the random prompt's main tags (P.Eng prefix and suffix left
      out). An image drawn under a lock already shows the kept tags there.
    - ``row``: an older request without them, but the random row is still in memory.
    - ``prompt``: a saved or imported file: the saved prompt and its C1.
    In every case the C1 that was sent with the image is offered as well.
    """
    params = getattr(item, "generation_params", None) if item is not None else None
    params = params if isinstance(params, dict) else {}
    c1 = _executed_c1(params).strip()
    body = params.get("_prompt_body_tags")
    if isinstance(body, list) and body:
        names = params.get("_prompt_source_names") if isinstance(params.get("_prompt_source_names"), dict) else {}
        return "body", {
            "body_tags_in": body,
            "names": image_lock.split_csv(names.get("character")),
            "copyright_tags": image_lock.split_csv(names.get("copyright")),
            "character_prompt": c1,
        }
    row = getattr(item, "source_row", None) if item is not None else None
    getter = getattr(row, "get", None)
    if callable(getter) and not params.get(image_lock.IMAGE_LOCK_MARKER):
        general = image_lock.split_csv(getter("general"))
        if general:
            return "row", {
                "body_tags_in": general,
                "names": image_lock.split_csv(getter("character")),
                "copyright_tags": image_lock.split_csv(getter("copyright")),
                "character_prompt": c1,
            }
    saved = payload.get("characters")
    saved_c1 = str(saved[0] or "").strip() if isinstance(saved, list) and saved else ""
    general = image_lock.tags_from_prompt(payload.get("prompt"))
    return ("prompt" if general or saved_c1 or c1 else ""), {
        "body_tags_in": general,
        "character_prompt": saved_c1 or c1,
    }


def _candidates(context: WebSessionContext, payload: dict[str, Any]) -> dict[str, Any]:
    rel_path = str(payload.get("path") or "").strip()
    item = history_item_from_viewer_path(context, rel_path) if rel_path else None
    source, kwargs = _candidate_source(item, payload)
    if not source:
        raise ValueError("이 그림에서 고를 태그를 찾지 못했습니다.")
    groups = image_lock.candidates(
        filter_manager=getattr(context, "filter_data_manager", None),
        repo_root=getattr(context, "repo_root", None),
        **kwargs,
    )
    label = str(payload.get("label") or getattr(item, "filename", "") or rel_path.rsplit("/", 1)[-1])
    return {
        "ok": True,
        "source": source,
        "source_label": label,
        "groups": groups,
        "presets": image_lock.PRESETS,
        "prefs": image_lock.load_prefs(context),
        "search_presets": _search_preset_names(context),
        "search_presets_blocking_partner": _presets_blocking_partner(context),
    }


def _search_preset_names(context: WebSessionContext) -> list[str]:
    from core import search_presets

    return [item["name"] for item in search_presets.load_presets(context)]


def _presets_blocking_partner(context: WebSessionContext) -> list[str]:
    """Presets whose exclude, or the shared permanent exclude, drops ``1boy`` rows (no partner can appear)."""
    from core import search_presets

    permanent = search_presets.global_permanent_exclude(context)
    return [item["name"] for item in search_presets.load_presets(context)
            if search_presets.excludes_tag(item, "1boy", permanent)]


def _build_pool_frame(context: WebSessionContext, preset: dict[str, Any], permanent: str):
    """Search the tag archive with a search preset and the shared permanent exclude (same rules as Search)."""
    from app.backend.server.search_runtime import (
        _dedup_by_id,
        join_exclude_terms,
        search_tag_archive_frame,
        tag_archive_parquet_sources,
    )

    sources = tag_archive_parquet_sources(context)
    if not sources:
        raise ValueError("태그 데이터가 없어 프리셋으로 검색할 수 없습니다.")
    if preset.get("bucket_start") is not None or preset.get("bucket_end") is not None:
        from core.tag_bucket_dates import clamp_bucket_range

        start, end = clamp_bucket_range(preset.get("bucket_start"), preset.get("bucket_end"), len(sources))
        sources = sources[start:end + 1]
    frame = search_tag_archive_frame(
        sources,
        query=str(preset.get("query") or ""),
        exclude=join_exclude_terms(preset.get("exclude"), permanent),
        ratings=set(preset.get("ratings") or "gsqe"),
    )
    return _dedup_by_id(frame)


def _prepare_pool(context: WebSessionContext) -> dict[str, Any]:
    """Search the current mode's preset now, so the first generation does not wait for it."""
    lock = image_lock.active_lock(context)
    name = image_lock.current_preset_name(lock, image_lock.load_prefs(context))
    if not name:
        return {"preset": "", "built": False, "remaining": 0}
    pool = image_lock_pool.pool_for(context, name)
    if pool is None:
        return {"preset": name, "built": False, "remaining": 0, "missing": True}
    return {"preset": name, **image_lock_pool.status(context, name)}


def _state_with_pool(context: WebSessionContext) -> dict[str, Any]:
    state = image_lock.public_state(context)
    state["search_presets"] = _search_preset_names(context)
    state["search_presets_blocking_partner"] = _presets_blocking_partner(context)
    state["presets"] = image_lock.PRESETS
    return state


def register_image_lock_routes(
    app: FastAPI,
    context: WebSessionContext,
    *,
    run_in_thread: AsyncRunner,
    clients: set[Any],
    broadcast_json: Callable[..., Awaitable[Any]],
) -> None:
    context.image_lock_pool_builder = lambda preset, permanent: _build_pool_frame(context, preset, permanent)

    async def announce() -> dict[str, Any]:
        state = await run_in_thread(_state_with_pool, context)
        # Other clients must update the badge, the quick panel and P.Eng's forced options too.
        await broadcast_json(clients, state)
        try:
            await broadcast_json(clients, context._prompt_engineering_module_state())
        except Exception as exc:      # noqa: BLE001 - the lock itself has already changed
            print(f"[ImageLock] P.Eng state broadcast failed: {exc!r}")
        return state

    async def prepare_and_announce() -> dict[str, Any]:
        try:
            pool = await run_in_thread(_prepare_pool, context)
        except Exception as exc:      # noqa: BLE001 - the lock still works with the main pool
            pool = {"error": str(exc)}
        state = await announce()
        state["pool_result"] = pool
        return state

    def error(exc: Exception) -> JSONResponse:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    @app.get("/api/image-lock/state")
    async def api_image_lock_state():
        return await run_in_thread(_state_with_pool, context)

    @app.post("/api/image-lock/candidates")
    async def api_image_lock_candidates(req: Request):
        payload = await _payload(req)
        try:
            return await run_in_thread(_candidates, context, payload)
        except ValueError as exc:
            return error(exc)

    @app.post("/api/image-lock/set")
    async def api_image_lock_set(req: Request):
        payload = await _payload(req)
        try:
            image_lock.set_lock(
                context,
                groups=payload.get("groups"),
                kept=payload.get("kept"),
                modes=payload.get("modes"),
                partner=payload.get("partner"),
                nsfw=payload.get("nsfw"),
                uc=payload.get("uc"),
                source_label=payload.get("source_label"),
                sfw_clothes_mode=payload.get("sfw_clothes_mode"),
            )
        except ValueError as exc:
            return error(exc)
        return await prepare_and_announce()

    @app.post("/api/image-lock/update")
    async def api_image_lock_update(req: Request):
        payload = await _payload(req)
        try:
            image_lock.update_lock(
                context,
                modes=payload.get("modes"),
                kept=payload.get("kept"),
                prompt=payload.get("prompt"),
                uc=payload.get("uc"),
                partner=payload.get("partner"),
                nsfw=payload.get("nsfw"),
                sfw_clothes_mode=payload.get("sfw_clothes_mode"),
            )
        except ValueError as exc:
            return error(exc)
        if "nsfw" in payload:
            return await prepare_and_announce()
        return await announce()

    @app.post("/api/image-lock/prefs")
    async def api_image_lock_prefs(req: Request):
        payload = await _payload(req)
        await run_in_thread(image_lock.save_prefs, context, payload)
        return await prepare_and_announce()

    @app.post("/api/image-lock/clear")
    async def api_image_lock_clear():
        image_lock.clear_lock(context)
        return await announce()
