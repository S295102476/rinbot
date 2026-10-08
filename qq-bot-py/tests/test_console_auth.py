import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from plugins.admin_console import create_router
from plugins.console_auth import COOKIE_NAME, ConsoleAuth, MemorySessionStore, hash_password, verify_password


def client_for(auth):
    app = FastAPI()
    app.include_router(create_router(auth=auth))
    return TestClient(app)


def configured_auth():
    return ConsoleAuth("admin", hash_password("long-enough-test-password"), MemorySessionStore())


def login(client):
    return client.post("/api/admin/auth/login", json={"username": "admin", "password": "long-enough-test-password"},
                       headers={"origin": "http://testserver"})


def test_password_hash_is_salted_and_bounded():
    first = hash_password("long-enough-test-password")
    second = hash_password("long-enough-test-password")
    assert first != second
    assert verify_password("long-enough-test-password", first)
    assert not verify_password("wrong", first)
    assert not verify_password("long-enough-test-password", first.replace("16384", "1073741824"))


def test_unconfigured_auth_fails_closed_and_query_token_does_not_help():
    with client_for(ConsoleAuth()) as client:
        assert client.get("/api/admin/settings?token=anything").status_code == 503
        assert login(client).status_code == 503
    with client_for(configured_auth()) as client:
        assert client.get("/api/admin/settings?token=anything").status_code == 401


def test_cookie_csrf_origin_logout_and_server_side_revocation():
    auth = configured_auth()
    with client_for(auth) as client:
        response = login(client)
        assert response.status_code == 200
        assert "HttpOnly" in response.headers["set-cookie"]
        assert "SameSite=strict" in response.headers["set-cookie"]
        csrf = response.json()["csrf_token"]
        cookie = client.cookies[COOKIE_NAME]
        assert client.get("/api/admin/auth/me").json()["username"] == "admin"
        assert client.post("/api/admin/auth/logout", headers={"origin": "http://testserver"}).status_code == 403
        assert client.post("/api/admin/auth/logout", headers={"origin": "https://evil.example", "x-csrf-token": csrf}).status_code == 403
        assert client.post("/api/admin/auth/logout", headers={"origin": "http://testserver", "x-csrf-token": csrf}).status_code == 200
        client.cookies.set(COOKIE_NAME, cookie)
        assert client.get("/api/admin/auth/me").status_code == 401


def test_login_origin_and_brute_force_limit():
    with client_for(configured_auth()) as client:
        assert client.post("/api/admin/auth/login", json={"username": "admin", "password": "wrong"}).status_code == 403
        for _ in range(8):
            assert client.post("/api/admin/auth/login", json={"username": "admin", "password": "wrong"},
                               headers={"origin": "http://testserver"}).status_code == 401
        assert login(client).status_code == 429


def test_redis_failure_does_not_fall_back_or_expose_error():
    class BrokenStore(MemorySessionStore):
        async def attempt(self, key):
            raise RuntimeError("password=secret host=private")

    auth = configured_auth()
    auth.store = BrokenStore()
    with client_for(auth) as client:
        response = login(client)
        assert response.status_code == 503
        assert "secret" not in response.text


def test_environment_hash_must_be_valid(monkeypatch):
    monkeypatch.setenv("AGENT_CONSOLE_USERNAME", "admin")
    monkeypatch.setenv("AGENT_CONSOLE_PASSWORD_HASH", "not-a-hash")
    monkeypatch.setenv("AGENT_CONSOLE_SESSION_BACKEND", "memory-local")
    assert not ConsoleAuth.from_environment().configured
