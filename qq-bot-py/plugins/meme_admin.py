"""Authenticated compatibility routes for the retired meme administration UI."""

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse

from .admin_console import SafeConsoleRoute, get_services, require_admin
from .console_auth import AdminIdentity


def create_router() -> APIRouter:
    router = APIRouter(route_class=SafeConsoleRoute)

    async def identity(request: Request) -> AdminIdentity:
        return await require_admin(request)

    @router.get("/meme-admin", include_in_schema=False)
    async def meme_admin_page():
        # Old query tokens are neither copied nor used for access.
        return RedirectResponse("/admin/#memes", status_code=303)

    @router.get("/api/memes", include_in_schema=False)
    async def list_memes(
        emotion: str = "", status: str = "active", q: str = "",
        limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
        admin: AdminIdentity = Depends(identity),
    ):
        return await get_services().memes(emotion, status, q, offset, limit)

    @router.post("/api/memes/batch-delete", include_in_schema=False)
    async def batch_delete_memes(payload: dict, admin: AdminIdentity = Depends(identity)):
        return await get_services().batch_memes(payload, admin.username)

    @router.patch("/api/memes/{item_id}", include_in_schema=False)
    async def update_meme(item_id: int, payload: dict, admin: AdminIdentity = Depends(identity)):
        return await get_services().update_meme(item_id, payload, admin.username)

    @router.delete("/api/memes/{item_id}", include_in_schema=False)
    async def delete_meme(item_id: int, hard: bool = False, admin: AdminIdentity = Depends(identity)):
        return await get_services().update_meme(item_id, {"status": "deleted"}, admin.username, hard=hard)

    return router


try:
    from nonebot import get_driver
    _app = get_driver().server_app
except (ValueError, RuntimeError, AttributeError):
    _app = None
if _app is not None and not getattr(_app.state, "legacy_meme_admin_installed", False):
    _app.state.legacy_meme_admin_installed = True
    _app.include_router(create_router())
