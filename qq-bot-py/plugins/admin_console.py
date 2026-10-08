"""Same-origin FastAPI console, mounted on the existing NoneBot application."""

from __future__ import annotations

import asyncio
import time
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from fastapi.routing import APIRoute

from .console_auth import AdminIdentity, ConsoleAuth
from .console_services import ConsoleServices

_AUTH: ConsoleAuth | None = None
_SERVICES: ConsoleServices | None = None
_GROUP_NAMES: dict[int, str] = {}
_GROUP_NAMES_AT = 0.0


async def _bot_groups() -> tuple[dict[int, str], bool]:
    global _GROUP_NAMES, _GROUP_NAMES_AT
    try:
        from nonebot import get_bots
        bots = list(get_bots().values())
    except ValueError:
        bots = []
    if bots and time.monotonic() - _GROUP_NAMES_AT > 60:
        try:
            rows = await asyncio.wait_for(bots[0].get_group_list(), 5)
            _GROUP_NAMES = {int(row["group_id"]): str(row.get("group_name") or "") for row in rows}
            _GROUP_NAMES_AT = time.monotonic()
        except Exception:
            pass
    return dict(_GROUP_NAMES), bool(bots)


async def _group_rows(settings, quota, services) -> tuple[list[dict], bool]:
    names, online = await _bot_groups()
    values = settings.global_values()
    mode_config = services.config.get("group_mode") or {}
    rows = []
    for gid in sorted(set(names) | settings.known_groups()):
        row = settings.group(gid)
        restrictions = []
        if not values["agent_enabled"]:
            restrictions.append("Agent 全局关闭")
        if values["development_mode"] == "test" and gid not in values["test_groups"]:
            restrictions.append("开发监听：非测试群")
        if values["development_mode"] == "at":
            restrictions.append("开发监听：仅 @")
        if gid in mode_config.get("chat_only_groups", []):
            restrictions.append("chat-only：仅允许白名单功能指令")
        if gid in mode_config.get("local_disabled_groups", []):
            restrictions.append("本地功能禁用")
        rows.append({**row, "name": names.get(gid) or None, "restrictions": restrictions,
                     "quota": await quota.status(gid)})
    return rows, online


def get_services() -> ConsoleServices:
    global _SERVICES
    if _SERVICES is None:
        _SERVICES = ConsoleServices()
    return _SERVICES


def get_auth() -> ConsoleAuth:
    global _AUTH
    if _AUTH is None:
        _AUTH = ConsoleAuth.from_environment(get_services().config.get("redis") or {})
    return _AUTH


async def require_admin(request: Request, write: bool | None = None) -> AdminIdentity:
    """Use for legacy admin URLs too; query tokens never authorize access."""
    return await get_auth().require(request, write=request.method not in {"GET", "HEAD", "OPTIONS"} if write is None else write)


class SafeConsoleRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe(request: Request):
            if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
                length = request.headers.get("content-length", "0")
                if not length.isdecimal() or int(length) > 524288:
                    raise HTTPException(413, "Request body too large")
                chunks, body_size = [], 0
                async for chunk in request.stream():
                    body_size += len(chunk)
                    if body_size > 524288:
                        raise HTTPException(413, "Request body too large")
                    chunks.append(chunk)
                request._body = b"".join(chunks)
            try:
                response = await handler(request)
                response.headers["Cache-Control"] = "no-store"
                response.headers["X-Content-Type-Options"] = "nosniff"
                return response
            except HTTPException:
                raise
            except Exception as error:
                from .console_state import VersionConflict
                if isinstance(error, VersionConflict):
                    raise HTTPException(409, "Settings changed; reload before saving") from None
                if isinstance(error, ValueError):
                    raise HTTPException(422, "Invalid request values") from None
                from nonebot.log import logger
                logger.warning(f"[console] request failed type={type(error).__name__}")
                raise HTTPException(503, "Console service temporarily unavailable") from None
        return safe


def create_router(auth: ConsoleAuth | None = None, services: ConsoleServices | None = None,
                  settings: Any = None, stats: Any = None, quota: Any = None) -> APIRouter:
    router = APIRouter(prefix="/api/admin", route_class=SafeConsoleRoute)

    def authentication() -> ConsoleAuth:
        return auth or get_auth()

    def service() -> ConsoleServices:
        return services or get_services()

    def state():
        from .console_state import SETTINGS, STATS, QUOTA
        return settings or SETTINGS, stats or STATS, quota or QUOTA

    async def identity(request: Request) -> AdminIdentity:
        return await authentication().require(request, write=request.method not in {"GET", "HEAD", "OPTIONS"})

    @router.post("/auth/login")
    async def login(request: Request, response: Response, payload: dict = Body(...)):
        if set(payload) != {"username", "password"} or not all(isinstance(payload[key], str) for key in payload):
            raise HTTPException(422, "Username and password are required")
        if len(payload["username"]) > 100 or len(payload["password"]) > 1024:
            raise HTTPException(422, "Invalid credentials")
        return await authentication().login(request, response, **payload)

    @router.get("/auth/me")
    async def me(admin: AdminIdentity = Depends(identity)):
        return {"username": admin.username, "csrf_token": admin.csrf_token}

    @router.post("/auth/logout")
    async def logout(request: Request, response: Response):
        return await authentication().logout(request, response)

    @router.get("/overview")
    async def overview(date_from: date | None = None, date_to: date | None = None, admin: AdminIdentity = Depends(identity)):
        current_settings, current_stats, current_quota = state()
        data = await current_stats.summary(date_from=date_from, date_to=date_to)
        from .console_runtime import telemetry_health
        groups, online = await _group_rows(current_settings, current_quota, service())
        return {**data, "status": {"agent_enabled": current_settings.global_values()["agent_enabled"],
                                  "bot_online": online,
                                  "model": current_settings.global_values()["model"],
                                  "active_persona": service()._personas().get_active_persona_id(),
                                  "active_groups": len(current_settings.active_groups())},
                "health": {**await current_stats.health(), **telemetry_health()}, "quotas": groups}

    @router.get("/settings")
    async def get_settings(admin: AdminIdentity = Depends(identity)):
        store = state()[0]
        return {**store.global_values(), "defaults": getattr(store, "_defaults", {}),
                "overrides": getattr(store, "_global", {}), "loaded": getattr(store, "loaded", False)}

    @router.patch("/settings")
    async def patch_settings(payload: dict, admin: AdminIdentity = Depends(identity)):
        patch, version = _patch(payload)
        return await state()[0].update_global(patch, actor=admin.username, version=version)

    @router.post("/settings/reset")
    async def reset_settings(payload: dict, admin: AdminIdentity = Depends(identity)):
        return await state()[0].reset_global(actor=admin.username, version=_version(payload))

    @router.get("/groups")
    async def groups(admin: AdminIdentity = Depends(identity)):
        current_settings, _, current_quota = state()
        items, online = await _group_rows(current_settings, current_quota, service())
        return {"items": items, "total": len(items), "bot_online": online}

    @router.patch("/groups/bulk")
    async def bulk_groups(payload: dict, admin: AdminIdentity = Depends(identity)):
        if set(payload) - {"group_ids", "patch", "versions"} or not isinstance(payload.get("group_ids"), list):
            raise HTTPException(422, "Invalid group batch")
        ids = payload["group_ids"]
        if not 1 <= len(ids) <= 200 or any(isinstance(gid, bool) or not isinstance(gid, int) or gid <= 0 for gid in ids):
            raise HTTPException(422, "Provide 1 to 200 group IDs")
        patch = payload.get("patch")
        versions = payload.get("versions", {})
        if not isinstance(patch, dict) or not isinstance(versions, dict):
            raise HTTPException(422, "Invalid group patch")
        # Validate the shared patch before any row is committed.
        from .console_state import GROUP_KEYS, _validate_patch, VersionConflict
        _validate_patch(patch, GROUP_KEYS)
        items, failed = [], []
        for gid in dict.fromkeys(ids):
            version = versions.get(str(gid))
            if version is not None and (isinstance(version, bool) or not isinstance(version, int) or version < 0):
                raise HTTPException(422, "Invalid version")
            try:
                items.append(await state()[0].update_group(gid, patch, actor=admin.username, version=version))
            except VersionConflict:
                failed.append({"group_id": gid, "status": 409, "detail": "Group changed; reload before saving"})
        return {"ok": not failed, "items": items, "failed": failed}

    @router.patch("/groups/{group_id}")
    async def patch_group(group_id: int, payload: dict, admin: AdminIdentity = Depends(identity)):
        patch, version = _patch(payload)
        return await state()[0].update_group(group_id, patch, actor=admin.username, version=version)

    @router.post("/groups/{group_id}/reset")
    async def reset_group(group_id: int, payload: dict, admin: AdminIdentity = Depends(identity)):
        return await state()[0].reset_group(group_id, actor=admin.username, version=_version(payload))

    @router.get("/stats/groups")
    async def group_stats(date_from: date | None = None, date_to: date | None = None,
                          group_id: int | None = None, admin: AdminIdentity = Depends(identity)):
        return await state()[1].summary(date_from=date_from, date_to=date_to, group_id=group_id)

    @router.get("/stats/users")
    async def user_stats(date_from: date | None = None, date_to: date | None = None, group_id: int | None = None,
                         offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500), admin: AdminIdentity = Depends(identity)):
        return await state()[1].activity(date_from=date_from, date_to=date_to, group_id=group_id, offset=offset, limit=limit)

    @router.get("/requests")
    async def requests(date_from: date | None = None, date_to: date | None = None, group_id: int | None = None, status: str = "", source: str = "",
                        offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=500), admin: AdminIdentity = Depends(identity)):
        return await state()[1].requests(date_from=date_from, date_to=date_to, group_id=group_id, status=status, source=source, offset=offset, limit=limit)

    @router.get("/audits")
    async def audits(date_from: date | None = None, date_to: date | None = None, action: str = "",
                     offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=500), admin: AdminIdentity = Depends(identity)):
        return await state()[1].audits(date_from=date_from, date_to=date_to, action=action, offset=offset, limit=limit)

    @router.get("/memes")
    async def memes(emotion: str = "", status: str = "active", q: str = "", offset: int = Query(0, ge=0),
                    limit: int = Query(24, ge=1, le=100), admin: AdminIdentity = Depends(identity)):
        return await service().memes(emotion, status, q, offset, limit)

    @router.post("/memes/batch")
    async def batch_memes(payload: dict, admin: AdminIdentity = Depends(identity)):
        return await service().batch_memes(payload, admin.username)

    @router.patch("/memes/{item_id}")
    async def patch_meme(item_id: int, payload: dict, admin: AdminIdentity = Depends(identity)):
        return await service().update_meme(item_id, payload, admin.username)

    @router.get("/memes/{item_id}/image")
    async def meme_image(item_id: int, admin: AdminIdentity = Depends(identity)):
        data, mime = await service().meme_image(item_id)
        return Response(content=data, media_type=mime, headers={
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Cross-Origin-Resource-Policy": "same-origin",
            "Content-Disposition": "inline",
        })

    @router.delete("/memes/{item_id}")
    async def delete_meme(item_id: int, hard: bool = False, admin: AdminIdentity = Depends(identity)):
        return await service().update_meme(item_id, {"status": "deleted"}, admin.username, hard=hard)

    @router.get("/personas")
    async def personas(admin: AdminIdentity = Depends(identity)):
        return await service().personas()

    @router.get("/personas/history")
    async def persona_history(limit: int = Query(20, ge=1, le=100), admin: AdminIdentity = Depends(identity)):
        from sqlalchemy import select, func
        from .db import AgentPersonaSwitch
        from .console_services import _row
        async with await service().session_factory() as session:
            total = (await session.execute(select(func.count()).select_from(AgentPersonaSwitch))).scalar() or 0
            rows = (await session.execute(select(AgentPersonaSwitch).order_by(AgentPersonaSwitch.created_at.desc()).limit(limit))).scalars().all()
            return {"items": [_row(row) for row in rows], "total": total}

    @router.post("/personas/switch")
    async def switch_persona(payload: dict, admin: AdminIdentity = Depends(identity)):
        if set(payload) != {"persona_id", "reason"}:
            raise HTTPException(422, "Persona and reason are required")
        return await service().switch_persona(payload["persona_id"], payload["reason"], admin.username)

    @router.get("/personas/documents/{document_id}")
    async def document(document_id: str, admin: AdminIdentity = Depends(identity)):
        return await service().document(document_id)

    @router.put("/personas/documents/{document_id}")
    async def save_document(document_id: str, payload: dict, admin: AdminIdentity = Depends(identity)):
        return await service().save_document(document_id, payload, admin.username)

    @router.get("/personas/documents/{document_id}/revisions")
    async def revisions(document_id: str, admin: AdminIdentity = Depends(identity)):
        return await service().revisions(document_id)

    @router.post("/personas/documents/{document_id}/restore")
    async def restore_document(document_id: str, payload: dict, admin: AdminIdentity = Depends(identity)):
        if set(payload) != {"revision_id", "version"} or not all(isinstance(value, str) for value in payload.values()):
            raise HTTPException(422, "Revision and current version are required")
        return await service().restore_document(document_id, payload["revision_id"], payload["version"], admin.username)

    @router.get("/memory/{kind}")
    async def memories(kind: str, scope: str = "group", group_id: int = Query(0, ge=0), user_id: int = Query(0, ge=0),
                       persona_id: str = "", q: str = "", offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
                       sort_by: str = "updated_at", sort_order: str = "desc",
                       admin: AdminIdentity = Depends(identity)):
        return await service().memories(kind, scope, group_id, user_id, persona_id, q, offset, limit,
                                        sort_by=sort_by, sort_order=sort_order)

    @router.post("/memory/{kind}")
    async def create_memory(kind: str, payload: dict, scope: str = "group", admin: AdminIdentity = Depends(identity)):
        return await service().mutate_memory(kind, scope, payload, admin.username)

    @router.patch("/memory/{kind}/{item_id}")
    async def update_memory(kind: str, item_id: int, payload: dict, scope: str = "group", admin: AdminIdentity = Depends(identity)):
        return await service().mutate_memory(kind, scope, payload, admin.username, item_id=item_id)

    @router.delete("/memory/{kind}/{item_id}")
    async def delete_memory(kind: str, item_id: int, payload: dict = Body(...), scope: str = "group", admin: AdminIdentity = Depends(identity)):
        return await service().mutate_memory(kind, scope, payload, admin.username, item_id=item_id, deleting=True)

    @router.put("/affinities/{persona_id}/{user_id}")
    async def set_affinity(persona_id: str, user_id: int, payload: dict, admin: AdminIdentity = Depends(identity)):
        return await service().set_affinity(persona_id, user_id, payload, admin.username)

    @router.get("/roster")
    async def roster(start: date | None = None, admin: AdminIdentity = Depends(identity)):
        return await service().roster(start or date.today())

    @router.patch("/roster")
    async def update_roster(payload: dict, admin: AdminIdentity = Depends(identity)):
        return await service().update_roster(payload, admin.username)

    return router


def _version(payload: dict) -> int:
    value = payload.get("version")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise HTTPException(422, "Current version is required")
    return value


def _patch(payload: dict) -> tuple[dict, int]:
    # Direct fields + version keep existing settings forms straightforward.
    version = _version(payload)
    if "patch" in payload:
        if set(payload) != {"patch", "version"} or not isinstance(payload["patch"], dict):
            raise HTTPException(422, "Invalid patch")
        return payload["patch"], version
    return {key: value for key, value in payload.items() if key != "version"}, version


def install(app: FastAPI, dist_root: Path | None = None) -> None:
    if getattr(app.state, "agent_console_installed", False):
        return
    app.state.agent_console_installed = True
    app.include_router(create_router())
    root = (dist_root or Path(__file__).resolve().parents[1] / "console" / "dist").resolve()

    @app.get("/admin", include_in_schema=False)
    @app.get("/admin/{asset_path:path}", include_in_schema=False)
    async def admin_page(asset_path: str = ""):
        target = (root / asset_path).resolve() if asset_path else root / "index.html"
        if not target.is_relative_to(root):
            raise HTTPException(404, "Not found")
        if not target.is_file():
            if Path(asset_path).suffix:
                raise HTTPException(404, "Not found")
            target = root / "index.html"
        if not target.is_file():
            raise HTTPException(503, "Console frontend has not been built")
        return FileResponse(target, headers={
            "Cache-Control": "no-store" if target.name == "index.html" else "public, max-age=3600",
            "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
            "Referrer-Policy": "same-origin",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: http: https:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
        })


try:
    from nonebot import get_driver
    _app = get_driver().server_app
except (ValueError, RuntimeError, AttributeError):
    _app = None
if _app is not None:
    install(_app)
