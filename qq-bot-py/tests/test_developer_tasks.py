"""Checks run against synthetic source and SQLite; never production or model APIs."""
import asyncio
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from plugins import db
from plugins.console_state import ConsoleAudit
from plugins.console_auth import ConsoleAuth, MemorySessionStore, hash_password
from plugins.developer_tasks import DeveloperTask, DeveloperTasks, TaskConflict, apply_unified_patch, digest, execute_developer_tool, path_name


class AsyncAdapter:
    def __init__(self, engine):
        self.session = Session(engine, expire_on_commit=False)
    async def __aenter__(self): return self
    async def __aexit__(self, *_): self.session.close()
    async def get(self, *args): return self.session.get(*args)
    async def execute(self, *args): return self.session.execute(*args)
    async def commit(self): self.session.commit()
    def add(self, row): self.session.add(row)


@asynccontextmanager
async def service_context(tmp_path, check_ok=True):
    source = tmp_path / "source"
    (source / "plugins").mkdir(parents=True)
    (source / "plugins" / "example.py").write_text("answer = 1\n", encoding="utf-8")
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    db.Base.metadata.create_all(engine, tables=[DeveloperTask.__table__, ConsoleAudit.__table__])
    async def factory(): return AsyncAdapter(engine)
    async def check(root, name, targets):
        assert root != source
        return {"ok": check_ok, "exit_code": int(not check_ok), "output": "synthetic check"}
    service = DeveloperTasks(source, tmp_path / "tasks", factory, config={"model": {"api_key": "fixture-private-credential"}}, check_runner=check)
    try: yield service
    finally:
        for worker in service.workers.values():
            worker.cancel()
        await asyncio.gather(*service.workers.values(), return_exceptions=True)
        engine.dispose()


PATCH = "--- a/plugins/example.py\n+++ b/plugins/example.py\n@@ -1 +1 @@\n-answer = 1\n+answer = 2\n"


async def prepare(service):
    item = await service.create("Change the answer", "console:admin")
    replies = iter([json.dumps({"tool_calls": [{"name": "propose_patch", "arguments": {"patch": PATCH, "summary": "Update answer"}}]}), '{"reply":"Prepared","tool_calls":[]}'])
    async def model(messages): return next(replies)
    await service.run(item["id"], model)
    return await service.get(item["id"])


@pytest.mark.parametrize("value", ["../config.yaml", "plugins/../../config.yaml", "config.yaml", ".env", "console/.env", "plugins/a.pem", "data/private.py", "plugins/special_users.py", "/etc/passwd", "plugins\\secret.py", "C:/secret.py", "plugins/.hidden.py", "console/package-lock.json"])
def test_private_and_traversal_paths_are_rejected(value):
    with pytest.raises((ValueError, PermissionError)):
        path_name(value)


def test_snapshot_excludes_secrets_and_data(tmp_path):
    async def run():
        async with service_context(tmp_path) as service:
            (service.source / "config.yaml").write_text("password: private")
            (service.source / "plugins" / "private.py").write_text('api_key = "fixture-private-credential"\n')
            item = await service.create("Do work", "console:admin")
            snapshot = service.directory(item["id"]) / "workspace"
            assert not (snapshot / "config.yaml").exists()
            assert not (snapshot / "plugins" / "private.py").exists()
            assert (snapshot / "plugins" / "example.py").exists()
            assert "fixture-private-credential" not in json.dumps(await service.get(item["id"]))
    asyncio.run(run())


def test_patch_validates_both_targets_add_delete_and_rename(tmp_path):
    (tmp_path / "plugins").mkdir()
    (tmp_path / "plugins" / "old.py").write_text("old\n")
    patch = "--- a/plugins/old.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-old\n--- /dev/null\n+++ b/plugins/new.py\n@@ -0,0 +1 @@\n+new\n"
    assert set(apply_unified_patch(tmp_path, patch)) == {"plugins/old.py", "plugins/new.py"}
    assert not (tmp_path / "plugins" / "old.py").exists()
    rename = "diff --git a/plugins/new.py b/plugins/renamed.py\nsimilarity index 100%\nrename from plugins/new.py\nrename to plugins/renamed.py\n"
    apply_unified_patch(tmp_path, rename)
    assert (tmp_path / "plugins" / "renamed.py").read_text() == "new\n"
    with pytest.raises((PermissionError, ValueError)):
        apply_unified_patch(tmp_path, "--- a/config.yaml\n+++ b/plugins/okay.py\n@@ -1 +1 @@\n-old\n+new\n")


def test_full_review_checked_approval_owner_and_rollback(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "plugins.agent_tools", SimpleNamespace(tool_schemas=lambda scope: []))
    async def run():
        async with service_context(tmp_path) as service:
            task = await prepare(service)
            assert task["status"] == "pending_approval"
            assert task["iterations"] == 2
            assert task["diff"] == PATCH
            assert (service.source / "plugins" / "example.py").read_text() == "answer = 1\n"
            with pytest.raises(PermissionError):
                await service.decide(task["id"], "console:other", task["version"], "approve")
            with pytest.raises(TaskConflict):
                await service.decide(task["id"], task["actor"], task["version"] - 1, "approve")
            applied = await service.decide(task["id"], task["actor"], task["version"], "approve")
            assert applied["status"] == "applied"
            assert (service.source / "plugins" / "example.py").read_text() == "answer = 2\n"
            restored = await service.decide(task["id"], task["actor"], applied["version"], "rollback")
            assert restored["status"] == "rolled_back"
            assert (service.source / "plugins" / "example.py").read_text() == "answer = 1\n"
    asyncio.run(run())


@pytest.mark.parametrize("change", ["edit", "new", "delete", "snapshot"])
def test_stale_tree_or_snapshot_never_applies(tmp_path, monkeypatch, change):
    monkeypatch.setitem(sys.modules, "plugins.agent_tools", SimpleNamespace(tool_schemas=lambda scope: []))
    async def run():
        async with service_context(tmp_path) as service:
            task = await prepare(service)
            target = service.source / "plugins" / "example.py"
            if change == "edit": target.write_text("changed outside\n")
            if change == "new": (service.source / "plugins" / "another.py").write_text("new\n")
            if change == "delete": target.unlink()
            if change == "snapshot": (service.directory(task["id"]) / "workspace" / "plugins" / "example.py").write_text("tampered\n")
            with pytest.raises(TaskConflict):
                await service.decide(task["id"], task["actor"], task["version"], "approve")
            assert not target.exists() or target.read_text() != "answer = 2\n"
    asyncio.run(run())


def test_expired_and_failed_checks_cannot_be_approved(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "plugins.agent_tools", SimpleNamespace(tool_schemas=lambda scope: []))
    async def run():
        async with service_context(tmp_path, check_ok=False) as service:
            task = await prepare(service)
            assert task["status"] == "checks_failed"
            with pytest.raises(TaskConflict):
                await service.decide(task["id"], task["actor"], task["version"], "approve")
    asyncio.run(run())


def test_development_tools_need_host_task_owner_not_group_context(tmp_path):
    async def run():
        async with service_context(tmp_path) as service:
            task = await service.create("Read source", "qq:123")
            with pytest.raises(PermissionError):
                await execute_developer_tool("read_project_file", {"path": "plugins/example.py"}, {"is_admin": True, "user_id": 123})
            with service.context(task["id"], "qq:123"):
                for context in ({"is_admin": True, "user_id": 456}, {"is_admin": True, "user_id": 123, "group_id": 77}, {"is_admin": False, "user_id": 123}):
                    with pytest.raises(PermissionError):
                        await execute_developer_tool("read_project_file", {"path": "plugins/example.py"}, context)
                result = await execute_developer_tool("read_project_file", {"path": "plugins/example.py"}, {"is_admin": True, "user_id": 123})
                assert "answer = 1" in result
    asyncio.run(run())


def test_docker_checks_have_no_host_mount_network_or_shell(tmp_path):
    async def run():
        async with service_context(tmp_path) as service:
            task = await service.create("Check source", "console:admin")
            service.check_runner = None
            commands = []
            async def fake_process(argv, timeout=300):
                commands.append(argv)
                return 0, "passed"
            service._process = fake_process
            result = await service.check(task["id"], "pytest", ["plugins/example.py"])
            assert result["ok"]
            create = commands[3]
            for flag in ["--network=none", "--read-only", "--memory=512m", "--cpus=1", "--cap-drop=ALL", "--pull=never"]:
                assert flag in create
            assert not any("mount" in arg or "docker.sock" in arg or arg == "sh" for arg in create)
            assert commands[1][1] == "cp"
            assert commands[2][1] == "commit"
            assert commands[4][1:3] == ["start", "--attach"]
            assert commands[0][3] not in commands[4]
            assert commands[-2][1:3] == ["rm", "--force"]
            assert commands[-1][1:3] == ["image", "rm"]
    asyncio.run(run())


def test_restart_keeps_review_and_resumes_queued_tasks(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "plugins.agent_tools", SimpleNamespace(tool_schemas=lambda scope: []))
    async def run():
        async with service_context(tmp_path) as service:
            approved = await prepare(service)
            queued = await service.create("Explain source", "console:admin")
            restarted = DeveloperTasks(service.source, service.storage, service.session_factory, check_runner=service.check_runner)
            async def model(messages): return '{"reply":"Source explained","tool_calls":[]}'
            await restarted.recover(model)
            await asyncio.gather(*restarted.workers.values())
            assert (await restarted.get(queued["id"]))["status"] == "completed"
            assert (await restarted.get(approved["id"]))["status"] == "pending_approval"
    asyncio.run(run())


def test_console_auth_csrf_and_actor_binding(tmp_path):
    from plugins.admin_console import create_router
    from plugins.developer_console import create_developer_router
    async def run():
        async with service_context(tmp_path) as service:
            service.start = lambda *args: None
            app = FastAPI()
            auth = ConsoleAuth("admin", hash_password("long-enough-password"), MemorySessionStore())
            app.include_router(create_router(auth=auth))
            app.include_router(create_developer_router(auth, service))
            with TestClient(app) as client:
                assert client.get("/api/admin/developer/tasks").status_code == 401
                login = client.post("/api/admin/auth/login", json={"username": "admin", "password": "long-enough-password"}, headers={"origin": "http://testserver"})
                headers = {"origin": "http://testserver", "x-csrf-token": login.json()["csrf_token"]}
                assert client.post("/api/admin/developer/tasks", json={"request": "Review code"}).status_code == 403
                assert client.post("/api/admin/developer/tasks", json={"request": "Review", "actor": "qq:999"}, headers=headers).status_code == 422
                task = client.post("/api/admin/developer/tasks", json={"request": "Review code"}, headers=headers).json()
                assert task["actor"] == "console:admin"
                assert client.get(f"/api/admin/developer/tasks/{task['id']}").json()["request"] == "Review code"
                assert client.post(f"/api/admin/developer/tasks/{task['id']}/cancel", json={"version": 999}, headers=headers).status_code == 409
                response = client.post(f"/api/admin/developer/tasks/{task['id']}/cancel", json={"version": task["version"]}, headers=headers)
                assert response.json()["status"] == "cancelled"
    asyncio.run(run())


def test_approval_expires_and_single_console_admin_is_explicitly_bound(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "plugins.agent_tools", SimpleNamespace(tool_schemas=lambda scope: []))
    async def run():
        async with service_context(tmp_path) as service:
            service.config["agent"] = {"dev": {"admin_users": [123], "console_username": "admin"}}
            assert service.owner_key("console:admin") == "qq:123"
            assert service.owner_key("console:someone") == "console:someone"
            task = await prepare(service)
            task["expires_at"] = 0
            task = await service._save(task, "test_expiry")
            with pytest.raises(TaskConflict, match="expired"):
                await service.decide(task["id"], "qq:123", task["version"], "approve")
            assert (await service.get(task["id"]))["status"] == "expired"
    asyncio.run(run())


def test_failed_proposal_leaves_snapshot_unchanged(tmp_path):
    async def run():
        async with service_context(tmp_path) as service:
            item = await service.create("Code update", "console:admin")
            target = service.directory(item["id"]) / "workspace" / "plugins" / "example.py"
            malicious = PATCH.replace("+answer = 2", '+password = "production-secret-value"')
            with pytest.raises(PermissionError):
                await service.tool(item["id"], "propose_patch", {"patch": malicious})
            assert target.read_text() == "answer = 1\n"
    asyncio.run(run())


def test_interrupted_apply_rolls_back_without_overwriting_third_party(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "plugins.agent_tools", SimpleNamespace(tool_schemas=lambda scope: []))
    async def run():
        async with service_context(tmp_path) as service:
            task = await prepare(service)
            baseline = service._manifest(task["id"])
            _, proposed = service.changes(task["id"])
            root = service.directory(task["id"])
            backup = root / "rollback" / "plugins"
            backup.mkdir(parents=True)
            (backup / "example.py").write_bytes((root / "baseline" / "plugins" / "example.py").read_bytes())
            (service.source / "plugins" / "example.py").write_bytes((root / "workspace" / "plugins" / "example.py").read_bytes())
            saved = await service._load(task["id"])
            saved.update(status="applying", journal={"action": "approve", "before": baseline, "after": proposed, "touched": ["plugins/example.py"]})
            await service._save(saved, "crash_fixture")
            await service.recover()
            assert (service.source / "plugins" / "example.py").read_text() == "answer = 1\n"
            assert (await service.get(task["id"]))["status"] == "failed"
    asyncio.run(run())


def test_iteration_budget_survives_restart(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "plugins.agent_tools", SimpleNamespace(tool_schemas=lambda scope: []))
    async def run():
        async with service_context(tmp_path) as service:
            task = await service.create("Read code repeatedly", "console:admin")
            task.update(iterations=7, calls=[{"iteration": 7, "status": "interrupted", "seconds": 10}], elapsed_seconds=20)
            await service._save(task, "checkpoint_fixture")
            calls = []
            async def model(messages):
                calls.append(messages)
                return json.dumps({"tool_calls": [{"name": "list_project_files", "arguments": {}}]})
            await service.run(task["id"], model)
            assert len(calls) == 1
            assert (await service.get(task["id"]))["iterations"] == 8
    asyncio.run(run())


def test_disabled_development_blocks_creation_and_restart_but_preserves_task(tmp_path):
    async def run():
        async with service_context(tmp_path) as service:
            queued = await service.create("Read source", "console:admin")
            service.config["agent"] = {"dev": {"enabled": False}}
            with pytest.raises(PermissionError):
                await service.create("Another request", "console:admin")
            with pytest.raises(PermissionError):
                service.start(queued["id"])
            await service.recover()
            assert not service.workers
            assert (await service.get(queued["id"]))["status"] == "queued"
            result = await service.decide(queued["id"], "console:admin", (await service.get(queued["id"]))["version"], "cancel")
            assert result["status"] == "cancelled"
    asyncio.run(run())
