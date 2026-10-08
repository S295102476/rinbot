"""Fail-closed admin authentication without import-time network access."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, Response

COOKIE_NAME = "agent_admin_session"
SESSION_SECONDS = 8 * 60 * 60
LOGIN_WINDOW = 15 * 60
LOGIN_ATTEMPTS = 8


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("Password must contain at least 12 characters")
    if len(password) > 1024:
        raise ValueError("Password is too long")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1, dklen=32)
    encode = lambda value: base64.urlsafe_b64encode(value).decode("ascii")
    return f"scrypt$v1$16384$8$1${encode(salt)}${encode(digest)}"


def valid_password_hash(encoded: str) -> bool:
    try:
        algorithm, version, n, r, p, salt, digest = encoded.split("$")
        return (
            (algorithm, version, n, r, p) == ("scrypt", "v1", "16384", "8", "1")
            and len(base64.b64decode(salt, altchars=b"-_", validate=True)) == 16
            and len(base64.b64decode(digest, altchars=b"-_", validate=True)) == 32
        )
    except (ValueError, TypeError):
        return False


def verify_password(password: str, encoded: str) -> bool:
    if not valid_password_hash(encoded) or len(password) > 1024:
        return False
    _, _, _, _, _, salt, expected = encoded.split("$")
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=base64.urlsafe_b64decode(salt),
        n=16384, r=8, p=1, dklen=32,
    )
    return hmac.compare_digest(digest, base64.urlsafe_b64decode(expected))


class MemorySessionStore:
    """Explicit test/local-only store. Production never falls back to this."""

    def __init__(self):
        self.values: dict[str, tuple[float, str]] = {}
        self.attempts: dict[str, tuple[float, int]] = {}

    async def get(self, key: str) -> str | None:
        expires, value = self.values.get(key, (0, ""))
        if expires <= time.time():
            self.values.pop(key, None)
            return None
        return value

    async def set(self, key: str, value: str, ttl: int) -> None:
        self.values[key] = (time.time() + ttl, value)

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)

    async def attempt(self, key: str) -> int:
        now = time.time()
        # Bound even the local development store under arbitrary client input.
        self.attempts = {key: value for key, value in self.attempts.items() if value[0] > now}
        expires, count = self.attempts.get(key, (now + LOGIN_WINDOW, 0))
        count += 1
        self.attempts[key] = (expires, count)
        return count


class RedisSessionStore:
    def __init__(self, client: Any):
        self.client = client

    async def get(self, key: str) -> str | None:
        return await asyncio.to_thread(self.client.get, key)

    async def set(self, key: str, value: str, ttl: int) -> None:
        await asyncio.to_thread(self.client.setex, key, ttl, value)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self.client.delete, key)

    async def attempt(self, key: str) -> int:
        # Counter expiry is atomic, including simultaneous initial attempts.
        script = "local n=redis.call('INCR',KEYS[1]); if n==1 then redis.call('EXPIRE',KEYS[1],ARGV[1]) end; return n"
        return int(await asyncio.to_thread(self.client.eval, script, 1, key, LOGIN_WINDOW))


@dataclass(frozen=True)
class AdminIdentity:
    username: str
    csrf_token: str


class ConsoleAuth:
    def __init__(self, username: str = "", password_hash: str = "", store: Any = None,
                 allowed_origins: tuple[str, ...] = (), local_only: bool = False):
        self.username = username
        self.password_hash = password_hash
        self.store = store
        self.allowed_origins = allowed_origins
        self.local_only = local_only

    @property
    def configured(self) -> bool:
        return bool(self.username and valid_password_hash(self.password_hash) and self.store)

    @classmethod
    def from_environment(cls, redis_config: dict[str, Any] | None = None) -> "ConsoleAuth":
        def setting(name: str, default: str = "") -> str:
            value = os.getenv(name)
            if value is not None:
                return value
            # NoneBot loads its .env into driver config, not into os.environ.
            try:
                from nonebot import get_driver
                return str(getattr(get_driver().config, name.lower(), default))
            except (ValueError, AttributeError):
                return default
        username = setting("AGENT_CONSOLE_USERNAME").strip()
        password_hash = setting("AGENT_CONSOLE_PASSWORD_HASH").strip()
        origins = tuple(origin.strip().rstrip("/") for origin in setting("AGENT_CONSOLE_ORIGINS").split(",") if origin.strip())
        if not username or not valid_password_hash(password_hash):
            return cls()
        backend = setting("AGENT_CONSOLE_SESSION_BACKEND", "redis").lower()
        if backend == "memory-local":
            return cls(username, password_hash, MemorySessionStore(), origins, local_only=True)
        if backend != "redis":
            return cls()
        import redis
        cfg = dict(redis_config or {})
        url = setting("AGENT_CONSOLE_REDIS_URL")
        options = {"decode_responses": True, "socket_timeout": 3, "socket_connect_timeout": 3}
        if url:
            client = redis.Redis.from_url(url, **options)
        else:
            client = redis.Redis(
                host=cfg.get("host", "127.0.0.1"), port=int(cfg.get("port", 6379)),
                db=int(cfg.get("db", 0)), password=cfg.get("password") or None, **options,
            )
        return cls(username, password_hash, RedisSessionStore(client), origins)

    def _ensure_ready(self, request: Request) -> None:
        if not self.configured:
            raise HTTPException(503, "Console authentication is not configured")
        if self.local_only and (not request.client or request.client.host not in {"127.0.0.1", "::1", "testclient"}):
            raise HTTPException(403, "Local sessions are restricted to loopback clients")

    def check_origin(self, request: Request) -> None:
        origin = request.headers.get("origin", "").rstrip("/")
        current = f"{request.url.scheme}://{request.url.netloc}"
        allowed = self.allowed_origins or (current,)
        if not origin or origin == "null" or origin not in allowed:
            raise HTTPException(403, "Origin check failed")
        parsed = urlsplit(origin)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise HTTPException(403, "Origin check failed")

    @staticmethod
    def _session_key(token: str) -> str:
        return "agent:console:session:" + hashlib.sha256(token.encode("ascii", errors="ignore")).hexdigest()

    async def login(self, request: Request, response: Response, username: str, password: str) -> dict[str, Any]:
        self._ensure_ready(request)
        self.check_origin(request)
        ip = request.client.host if request.client else "unknown"
        key = "agent:console:login:" + hashlib.sha256(ip.encode()).hexdigest()
        try:
            count = await self.store.attempt(key)
        except Exception:
            raise HTTPException(503, "Authentication service unavailable") from None
        if count > LOGIN_ATTEMPTS:
            raise HTTPException(429, "Too many login attempts", headers={"Retry-After": str(LOGIN_WINDOW)})
        matched = await asyncio.to_thread(verify_password, password, self.password_hash)
        if not hmac.compare_digest(username.encode(), self.username.encode()) or not matched:
            raise HTTPException(401, "Invalid credentials")
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        try:
            old = request.cookies.get(COOKIE_NAME)
            if old:
                await self.store.delete(self._session_key(old))
            await self.store.set(self._session_key(token), json.dumps({"username": self.username, "csrf_token": csrf}), SESSION_SECONDS)
        except Exception:
            raise HTTPException(503, "Authentication service unavailable") from None
        secure = request.url.scheme == "https" or request.url.hostname not in {"localhost", "127.0.0.1", "::1", "testserver"}
        response.set_cookie(COOKIE_NAME, token, max_age=SESSION_SECONDS, httponly=True,
                            secure=secure, samesite="strict", path="/")
        response.headers["Cache-Control"] = "no-store"
        return {"username": self.username, "csrf_token": csrf}

    async def require(self, request: Request, write: bool = False) -> AdminIdentity:
        self._ensure_ready(request)
        token = request.cookies.get(COOKIE_NAME, "")
        if not token or len(token) > 128:
            raise HTTPException(401, "Authentication required")
        try:
            encoded = await self.store.get(self._session_key(token))
            data = json.loads(encoded) if encoded else {}
        except Exception:
            raise HTTPException(503, "Authentication service unavailable") from None
        if not data or data.get("username") != self.username:
            raise HTTPException(401, "Session expired")
        if write:
            self.check_origin(request)
            supplied = request.headers.get("x-csrf-token", "")
            if not supplied or not hmac.compare_digest(supplied, str(data.get("csrf_token", ""))):
                raise HTTPException(403, "CSRF check failed")
        return AdminIdentity(data["username"], data["csrf_token"])

    async def logout(self, request: Request, response: Response) -> dict[str, bool]:
        await self.require(request, write=True)
        try:
            await self.store.delete(self._session_key(request.cookies[COOKIE_NAME]))
        except Exception:
            raise HTTPException(503, "Authentication service unavailable") from None
        response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="strict")
        return {"ok": True}
