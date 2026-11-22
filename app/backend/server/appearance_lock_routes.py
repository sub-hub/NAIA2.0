"""REST routes for the appearance lock: pick candidates, lock, edit and clear.

The rules live in ``core.appearance_lock``; these routes only parse requests and broadcast
the resulting state.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.backend.server.result_display_routes import history_item_from_viewer_path
from core import appearance_lock
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


def _candidate_tags(item: Any, payload: dict[str, Any]) -> tuple[str, list[str], list[str], str]:
    """Return ``(source, character name tags, general tags, C1 text)`` for the drawn character.

    - Drawn under a lock: only the locked C1 counts. The in-memory random row still holds the
      row's original character, whose look was stripped before sending.
    - In-memory random row: the row's tags plus the C1 that was sent with it.
    - Otherwise (an imported file, or after a restart): the saved base prompt plus the saved C1.
    """
    params = getattr(item, "generation_params", None) if item is not None else None
    params = params if isinstance(params, dict) else {}
    c1 = _executed_c1(params).strip()
    if params.get(appearance_lock.APPEARANCE_LOCK_MARKER) and c1:
        return "lock", [], [], c1
    row = getattr(item, "source_row", None) if item is not None else None
    if row is not None:
        names, general = appearance_lock.tags_from_source_row(row)
        if general:
            return "row", names, general, c1
    saved = payload.get("characters")
    saved_c1 = str(saved[0] or "").strip() if isinstance(saved, list) and saved else ""
    general = appearance_lock.tags_from_prompt(payload.get("prompt"))
    return ("prompt" if general or saved_c1 else ""), [], general, saved_c1


def _candidates(context: WebSessionContext, payload: dict[str, Any]) -> dict[str, Any]:
    rel_path = str(payload.get("path") or "").strip()
    item = history_item_from_viewer_path(context, rel_path) if rel_path else None
    source, names, general, c1 = _candidate_tags(item, payload)
    if not general and not c1:
        raise ValueError("이 그림에서 고를 태그를 찾지 못했습니다.")
    groups = appearance_lock.candidates(
        general,
        names,
        getattr(context, "filter_data_manager", None),
        getattr(context, "repo_root", None),
        character_prompt=c1,
    )
    label = str(payload.get("label") or getattr(item, "filename", "") or rel_path.rsplit("/", 1)[-1])
    return {"ok": True, "source": source, "source_label": label, "groups": groups}


def register_appearance_lock_routes(
    app: FastAPI,
    context: WebSessionContext,
    *,
    run_in_thread: AsyncRunner,
    clients: set[Any],
    broadcast_json: Callable[..., Awaitable[Any]],
) -> None:
    async def announce(state: dict[str, Any]) -> None:
        # Other clients must update the badge, the quick panel and P.Eng's forced options too.
        await broadcast_json(clients, state)
        try:
            await broadcast_json(clients, context._prompt_engineering_module_state())
        except Exception as exc:      # noqa: BLE001 - the lock itself has already changed
            print(f"[AppearanceLock] P.Eng state broadcast failed: {exc!r}")

    @app.get("/api/appearance-lock/state")
    async def api_appearance_lock_state():
        return appearance_lock.public_state(context)

    @app.post("/api/appearance-lock/candidates")
    async def api_appearance_lock_candidates(req: Request):
        payload = await _payload(req)
        try:
            return await run_in_thread(_candidates, context, payload)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    @app.post("/api/appearance-lock/set")
    async def api_appearance_lock_set(req: Request):
        payload = await _payload(req)
        try:
            state = appearance_lock.set_lock(
                context,
                prompt=payload.get("prompt"),
                uc=payload.get("uc"),
                include_clothes=payload.get("include_clothes"),
                source_label=payload.get("source_label"),
            )
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        await announce(state)
        return state

    @app.post("/api/appearance-lock/update")
    async def api_appearance_lock_update(req: Request):
        payload = await _payload(req)
        try:
            state = appearance_lock.update_lock(context, prompt=payload.get("prompt"), uc=payload.get("uc"))
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        await announce(state)
        return state

    @app.post("/api/appearance-lock/clear")
    async def api_appearance_lock_clear():
        state = appearance_lock.clear_lock(context)
        await announce(state)
        return state
