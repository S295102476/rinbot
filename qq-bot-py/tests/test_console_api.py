from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from plugins.admin_console import create_router, install
from plugins.console_auth import ConsoleAuth, MemorySessionStore, hash_password


class FakeSettings:
    def global_values(self):
        return {"agent_enabled": True, "daily_reply_limit": 200, "version": 2}

    async def update_global(self, patch, actor, version):
        from plugins.console_state import VersionConflict
        if version != 2:
            raise VersionConflict("sensitive internal detail")
        return {**self.global_values(), **patch, "version": 3}


def test_authenticated_settings_csrf_version_and_secrets():
    app = FastAPI()
    auth = ConsoleAuth("admin", hash_password("long-enough-password"), MemorySessionStore())
    app.include_router(create_router(auth=auth, settings=FakeSettings()))
    with TestClient(app) as client:
        response = client.post("/api/admin/auth/login", json={"username": "admin", "password": "long-enough-password"}, headers={"origin": "http://testserver"})
        csrf = response.json()["csrf_token"]
        headers = {"origin": "http://testserver", "x-csrf-token": csrf}
        response = client.get("/api/admin/settings")
        assert response.status_code == 200
        assert "password" not in response.text
        assert response.headers["cache-control"] == "no-store"
        assert client.patch("/api/admin/settings", json={"daily_reply_limit": 10, "version": 2}).status_code == 403
        assert client.patch("/api/admin/settings", json={"daily_reply_limit": 10}, headers=headers).status_code == 422
        response = client.patch("/api/admin/settings", json={"daily_reply_limit": 10, "version": 1}, headers=headers)
        assert response.status_code == 409
        assert "sensitive" not in response.text
        assert client.patch("/api/admin/settings", json={"daily_reply_limit": 10, "version": 2}, headers=headers).json()["version"] == 3


def test_spa_only_serves_built_root_and_missing_build_fails(tmp_path):
    app = FastAPI()
    install(app, tmp_path / "dist")
    with TestClient(app) as client:
        assert client.get("/admin").status_code == 503
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>Console</title>", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("do not serve", encoding="utf-8")
    with TestClient(app) as client:
        response = client.get("/admin")
        assert response.status_code == 200
        assert response.headers["x-frame-options"] == "DENY"
        assert client.get("/admin/%2e%2e/secret.txt").status_code == 404
        assert client.get("/admin/missing.js").status_code == 404


def test_oversized_body_is_rejected_without_logging_content():
    app = FastAPI()
    app.include_router(create_router(auth=ConsoleAuth()))
    with TestClient(app) as client:
        response = client.post("/api/admin/auth/login", content="x" * 524289)
        assert response.status_code == 413


def test_legacy_meme_api_uses_console_sessions_and_csrf(monkeypatch):
    from plugins import admin_console, meme_admin

    class FakeMemes:
        calls = []

        async def memes(self, *args):
            return {"items": [], "total": 0}

        async def update_meme(self, item_id, payload, actor, hard=False):
            self.calls.append((item_id, payload, actor, hard))
            return {"ok": True, "cache_synced": True}

        async def batch_memes(self, payload, actor):
            self.calls.append((payload, actor))
            return {"ok": True, "deleted_ids": payload["ids"], "failed": []}

    auth = ConsoleAuth("admin", hash_password("long-enough-password"), MemorySessionStore())
    service = FakeMemes()
    monkeypatch.setattr(admin_console, "_AUTH", auth)
    monkeypatch.setattr(meme_admin, "get_services", lambda: service)
    app = FastAPI()
    app.include_router(create_router(auth=auth))
    app.include_router(meme_admin.create_router())
    with TestClient(app) as client:
        response = client.get("/meme-admin?token=old-secret", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/#memes"
        assert client.get("/api/memes?token=old-secret").status_code == 401
        assert client.delete("/api/memes/1?hard=true&token=old-secret").status_code == 401
        login = client.post("/api/admin/auth/login", json={"username": "admin", "password": "long-enough-password"}, headers={"origin": "http://testserver"})
        assert client.get("/api/memes").status_code == 200
        assert client.patch("/api/memes/1", json={"status": "disabled"}).status_code == 403
        csrf = login.json()["csrf_token"]
        assert client.delete("/api/memes/1", headers={"origin": "https://evil.example", "x-csrf-token": csrf}).status_code == 403
        headers = {"origin": "http://testserver", "x-csrf-token": csrf}
        assert client.delete("/api/memes/1?hard=true", headers=headers).status_code == 200
        assert service.calls[0] == (1, {"status": "deleted"}, "admin", True)
        response = client.post("/api/memes/batch-delete", json={"ids": [2, 3], "hard": False}, headers=headers)
        assert response.json()["deleted_ids"] == [2, 3]


def test_legacy_memes_fail_closed_without_credentials(monkeypatch):
    from plugins import admin_console, meme_admin
    monkeypatch.setattr(admin_console, "_AUTH", ConsoleAuth())
    app = FastAPI()
    app.include_router(meme_admin.create_router())
    with TestClient(app) as client:
        assert client.get("/api/memes?token=old-secret").status_code == 503
        assert client.post("/api/memes/batch-delete", json={"ids": [1]}).status_code == 503


def test_meme_image_requires_session_and_memory_sort_is_forwarded():
    class FakeServices:
        async def meme_image(self, item_id):
            assert item_id == 7
            return b"image fixture", "image/png"
        async def memories(self, *args, **kwargs):
            return {"items": [], "total": 0, **kwargs}

    auth = ConsoleAuth("admin", hash_password("long-enough-password"), MemorySessionStore())
    app = FastAPI()
    app.include_router(create_router(auth=auth, services=FakeServices()))
    with TestClient(app) as client:
        assert client.get("/api/admin/memes/7/image?token=old-secret").status_code == 401
        login = client.post("/api/admin/auth/login", json={"username": "admin", "password": "long-enough-password"}, headers={"origin": "http://testserver"})
        assert login.status_code == 200
        response = client.get("/api/admin/memes/7/image")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["cross-origin-resource-policy"] == "same-origin"
        assert response.headers["cache-control"] == "no-store"
        response = client.get("/api/admin/memory/relationships?sort_by=message_count&sort_order=asc")
        assert response.json()["sort_by"] == "message_count"
        assert response.json()["sort_order"] == "asc"
