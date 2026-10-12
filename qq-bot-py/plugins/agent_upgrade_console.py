"""Authenticated routes, memory controls and queue observability."""
from fastapi import APIRouter, Depends, HTTPException, Query, Request


def create_upgrade_router(auth=None, memory=None):
    from .admin_console import SafeConsoleRoute, get_auth
    from .agent_memory_v2 import MEMORY_V2
    from . import model_routes
    memory = memory or MEMORY_V2
    router = APIRouter(prefix="/api/admin/agent", route_class=SafeConsoleRoute)

    async def identity(request: Request):
        return await (auth or get_auth()).require(request, write=request.method != "GET")

    async def audit(actor, action, target, fields):
        from .db import get_session
        from .console_state import _add_audit
        async with await get_session() as session:
            _add_audit(session, action, target, {"fields": fields}, actor)
            await session.commit()

    @router.get("/routes")
    async def routes(admin=Depends(identity)):
        return {"items": [model_routes.public_route(role) for role in model_routes.DEFAULTS]}

    @router.patch("/routes/{role}")
    async def edit_route(role: str, payload: dict, admin=Depends(identity)):
        if set(payload) != {"patch", "version"} or isinstance(payload["version"], bool) or not isinstance(payload["version"], int):
            raise HTTPException(422, "Current version and patch are required")
        return await model_routes.update(role, payload["patch"], actor=admin.username, version=payload["version"])

    @router.get("/memory")
    async def records(group_id: int | None = Query(None, gt=0), kind: str | None = None,
                      user_id: int | None = Query(None, gt=0), admin=Depends(identity)):
        if kind and kind not in {"fact", "episode", "style", "joke", "candidate"}:
            raise HTTPException(422, "Unknown memory type")
        return {"items": await memory.list_records(group_id, kind, user_id)}

    @router.patch("/memory/{record_id}")
    async def edit_memory(record_id: int, payload: dict, admin=Depends(identity)):
        if set(payload) - {"content", "delete", "version"} or isinstance(payload.get("version"), bool) or not isinstance(payload.get("version"), int):
            raise HTTPException(422, "Current memory version is required")
        if "content" in payload and (not isinstance(payload["content"], str) or not 1 <= len(payload["content"]) <= 1500):
            raise HTTPException(422, "Memory must be 1–1500 characters")
        if "delete" in payload and not isinstance(payload["delete"], bool):
            raise HTTPException(422, "delete must be boolean")
        if ("content" not in payload and not payload.get("delete")) or ("content" in payload and payload.get("delete")):
            raise HTTPException(422, "Choose a content correction or deletion")
        try:
            result = await memory.update_record(record_id, content=payload.get("content"),
                delete_record=payload.get("delete", False), expected_version=payload["version"], actor=admin.username)
        except ValueError as exc:
            if "已更新" in str(exc):
                raise HTTPException(409, str(exc)) from None
            raise
        return result

    @router.get("/privacy/{user_id}")
    async def privacy(user_id: int, group_id: int = 0, admin=Depends(identity)):
        if user_id <= 0 or group_id < 0:
            raise HTTPException(422, "Invalid identity")
        return await memory.privacy(user_id, group_id)

    @router.patch("/privacy/{user_id}")
    async def edit_privacy(user_id: int, payload: dict, admin=Depends(identity)):
        if set(payload) - {"group_id", "learning", "style_learning", "evaluation", "forget", "version"}:
            raise HTTPException(422, "Unknown privacy setting")
        version = payload.pop("version", None)
        if isinstance(version, bool) or not isinstance(version, int):
            raise HTTPException(422, "Current privacy version is required")
        group_id = payload.pop("group_id", 0)
        if isinstance(group_id, bool) or not isinstance(group_id, int) or group_id < 0 or user_id <= 0:
            raise HTTPException(422, "Invalid identity")
        if any(not isinstance(value, bool) for value in payload.values()):
            raise HTTPException(422, "Privacy switches must be boolean")
        if not payload:
            raise HTTPException(422, "At least one privacy switch is required")
        try:
            result = await memory.set_privacy(user_id, group_id, **payload, expected_version=version, actor=admin.username)
        except ValueError as exc:
            if "已更新" in str(exc):
                raise HTTPException(409, str(exc)) from None
            raise
        return result

    @router.get("/jobs")
    async def jobs(admin=Depends(identity)):
        return await memory.jobs_stats()

    @router.post("/jobs/retry")
    async def retry(admin=Depends(identity)):
        # Work remains in the durable queue. This endpoint only wakes it up.
        import asyncio
        task = getattr(memory, "_manual_retry", None)
        if task and not task.done():
            return {"queued": True}
        memory._manual_retry = asyncio.create_task(memory.process_pending(force=True))
        await audit(admin.username, "memory_v2.retry", "queue", [])
        return {"queued": True}

    @router.get("/reports")
    async def reports(group_id: int = Query(..., gt=0), user_id: int = Query(..., gt=0), admin=Depends(identity)):
        from sqlalchemy import select
        from .agent_memory_models import MemoryReport
        async with await memory.session() as session:
            row = (await session.execute(select(MemoryReport).where(MemoryReport.group_id == group_id, MemoryReport.user_id == user_id))).scalar_one_or_none()
            import json
            return json.loads(row.payload) if row else {"evidence": []}
    return router
