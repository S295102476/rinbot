"""Authenticated developer task API; permissions never come from request fields."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from .console_auth import AdminIdentity, ConsoleAuth
from .developer_tasks import DeveloperTasks, TaskConflict, get_developer_service


def create_developer_router(auth: ConsoleAuth | None = None, service: DeveloperTasks | None = None) -> APIRouter:
    from .admin_console import SafeConsoleRoute, get_auth

    router = APIRouter(prefix="/api/admin/developer", route_class=SafeConsoleRoute)

    async def identity(request: Request) -> AdminIdentity:
        return await (auth or get_auth()).require(request, write=request.method != "GET")

    def tasks() -> DeveloperTasks:
        return service or get_developer_service()

    async def invoke(call):
        try:
            return await call
        except TaskConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from None
        except LookupError:
            raise HTTPException(404, "Development task not found") from None

    @router.get("/tasks")
    async def list_tasks(admin: AdminIdentity = Depends(identity)):
        return await tasks().list()

    @router.post("/tasks")
    async def create_task(payload: dict, admin: AdminIdentity = Depends(identity)):
        if set(payload) != {"request"} or not isinstance(payload["request"], str):
            raise HTTPException(422, "Only the development request is accepted")
        item = await invoke(tasks().create(payload["request"], f"console:{admin.username}"))
        tasks().start(item["id"])
        return tasks()._public(item)

    @router.get("/tasks/{task_id}")
    async def get_task(task_id: str, admin: AdminIdentity = Depends(identity)):
        return await invoke(tasks().get(task_id))

    @router.post("/tasks/{task_id}/{action}")
    async def decide_task(task_id: str, action: str, payload: dict, admin: AdminIdentity = Depends(identity)):
        if set(payload) != {"version"} or not isinstance(payload["version"], int) or isinstance(payload["version"], bool):
            raise HTTPException(422, "Current task version is required")
        return await invoke(tasks().decide(task_id, f"console:{admin.username}", payload["version"], action))

    return router
