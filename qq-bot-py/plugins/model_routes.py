"""Purpose-based model routing. Public settings never contain credentials.

No imports of the chat plugin: offline tools and workers use exactly the same
resolver. Wire protocol is explicit, independent of the model's spelling.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = {
    "group_chat": ("antigravity", "openai", "gemini-3.8-flash-high", 90, 4000, 2),
    "memory": ("antigravity", "openai", "gemini-3.8-flash-high", 180, 8000, 1),
    "emotion": ("antigravity", "openai", "gemini-3.8-flash-low", 15, 128, 2),
    "meme": ("antigravity", "openai", "gemini-3.8-flash-low", 30, 256, 2),
    "expert": ("openai_responses", "responses", "gpt-5.6-sol", 180, 6000, 2),
    "development": ("openai_responses", "responses", "gpt-5.6-sol", 180, 12000, 1),
    "embedding": ("google", "embedding", "gemini-embedding-2", 30, 768, 2),
}
HARD_TIMEOUTS = {"group_chat": 90, "emotion": 15, "meme": 30, "embedding": 30}
ALIASES = {"chat": "group_chat", "summary": "memory", "meme_classification": "meme", "dev": "development"}
_CONFIG: dict | None = None
_OVERRIDES: dict[str, dict] = {}
_VERSIONS: dict[str, int] = {}
_SLOTS: dict[tuple, asyncio.Semaphore] = {}
_UPDATE_LOCK = asyncio.Lock()


class RouteError(RuntimeError):
    """Sanitized, actionable provider failure (never an upstream response body)."""


@dataclass(frozen=True)
class Route:
    role: str
    provider: str
    protocol: str
    model: str
    timeout: float
    max_output_tokens: int
    concurrency: int
    enabled: bool = True
    daily_limit: int = 20
    queue_timeout: float = 15
    base_url: str = field(default="", repr=False)
    api_key: str = field(default="", repr=False)
    proxy: str = field(default="", repr=False)
    source: str = "default"
    version: int = 0


def config() -> dict:
    global _CONFIG
    if _CONFIG is None:
        path = Path("config.yaml")
        if not path.exists():
            path = ROOT / "config.yaml"
        _CONFIG = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    return _CONFIG or {}


def provider_config(provider: str, cfg: dict | None = None) -> dict:
    cfg = config() if cfg is None else cfg
    ai = cfg.get("ai") or {}
    if provider == "google":
        raw = dict((cfg.get("meme") or {}).get("gemini") or {})
        raw.update(ai.get("google") or {})
        raw.setdefault("base_url", "https://generativelanguage.googleapis.com/v1beta")
    elif provider == "primary":
        raw = dict(ai)
    else:
        raw = dict(ai.get(provider) or {})
    env_name = raw.get("api_key_env")
    if provider == "openai_responses":
        from .responses_api import _api_key
        raw["api_key"] = _api_key(raw)
    elif env_name:
        raw["api_key"] = os.getenv(str(env_name), "")
        if not raw["api_key"]:
            from dotenv import dotenv_values
            raw["api_key"] = str(dotenv_values(ROOT / ".env").get(str(env_name)) or "")
    return raw


def resolve(role: str, group_id: int = 0) -> Route:
    role = ALIASES.get(role, role)
    if role not in DEFAULTS:
        raise ValueError("Unknown model purpose")
    provider, protocol, model, timeout, output, concurrency = DEFAULTS[role]
    agent = config().get("agent") or {}
    configured = (agent.get("model_routes") or {}).get(role) or {}
    values = dict(configured)
    source = "yaml" if configured else "default"
    # Only explicitly persisted legacy console model values are migrated, never
    # a console default for an unrelated provider. Other purposes stay independent.
    if role in _OVERRIDES:
        values.update(_OVERRIDES[role])
        source = "console"
    provider = values.get("provider", provider)
    raw = provider_config(provider)
    credentials = configured if provider == configured.get("provider", DEFAULTS[role][0]) else {}
    # The existing AG main model is retained for a configuration-only migration.
    if role == "group_chat" and not configured and role not in _OVERRIDES:
        model = raw.get("model") or model
    # Public deployments explicitly share the primary endpoint across purposes.
    # An omitted/empty purpose model inherits ai.model, never a private AG default.
    # Explicit AG high/low models and purpose-specific console overrides stay intact.
    effective_model = values.get("model")
    if not effective_model:
        inherit_provider_model = provider == "primary" or (
            provider == "openai_responses" and values.get("provider") == provider
        )
        effective_model = (raw.get("model") or "") if inherit_provider_model else model
    role_settings = agent.get("expert" if role == "expert" else "dev" if role == "development" else role) or {}
    return Route(
        role=role, provider=provider, protocol=values.get("protocol", protocol),
        model=effective_model, timeout=max(1, min(HARD_TIMEOUTS.get(role, 180), float(values.get("timeout", timeout)))),
        max_output_tokens=max(1, min(32000, int(values.get("max_output_tokens", output)))),
        concurrency=max(1, min(16, int(values.get("concurrency", concurrency)))),
        enabled=bool(values.get("enabled", role_settings.get("enabled", True))),
        daily_limit=max(0, min(1000, int(values.get("daily_limit", role_settings.get("daily_limit", 20))))),
        queue_timeout=max(1, min(15, float(values.get("queue_timeout", 15)))),
        base_url=str(credentials.get("base_url") or raw.get("responses_url") or raw.get("api_url") or raw.get("base_url") or ""),
        api_key=str(credentials.get("api_key") or os.getenv(str(credentials.get("api_key_env") or ""), "") or raw.get("api_key") or ""),
        proxy=str(credentials.get("proxy") or raw.get("proxy") or ""),
        source=source, version=_VERSIONS.get(role, 0),
    )


def public_route(role: str) -> dict:
    route = resolve(role)
    value = asdict(route)
    for key in ("api_key", "base_url", "proxy"):
        value.pop(key)
    value["configured"] = bool(route.api_key and route.base_url)
    return value


async def stage_legacy_model(session, model, actor: str):
    """Join a legacy console update's transaction; do not publish before commit."""
    from sqlalchemy import select
    from .console_state import ConsoleSetting, _add_audit
    row = (await session.execute(select(ConsoleSetting).where(ConsoleSetting.key == "route:group_chat").with_for_update())).scalar_one_or_none()
    value = json.loads(row.values_json) if row else {}
    if model is None:
        value.pop("model", None)
    else:
        validate_patch({"model": model})
        value["model"] = model
    version = (row.version if row else 0) + 1
    if row:
        row.values_json, row.version = json.dumps(value), version
    else:
        session.add(ConsoleSetting(key="route:group_chat", values_json=json.dumps(value), version=version))
    _add_audit(session, "route.update", "route:group_chat", {"fields": ["model"], "version": version}, actor)
    return value, version


def publish_legacy_model(candidate):
    _OVERRIDES["group_chat"], _VERSIONS["group_chat"] = candidate


def validate_patch(patch: dict) -> dict:
    allowed = {"provider", "protocol", "model", "timeout", "max_output_tokens", "concurrency", "enabled", "daily_limit", "queue_timeout"}
    if not isinstance(patch, dict) or set(patch) - allowed:
        raise ValueError("Only public routing fields may be edited")
    for key, value in patch.items():
        if value is None:
            continue
        if key == "provider" and value not in {"antigravity", "primary", "fallback", "google", "openai_responses"}:
            raise ValueError("Unknown provider")
        if key == "protocol" and value not in {"openai", "responses", "google", "embedding"}:
            raise ValueError("Unknown explicit protocol")
        if key == "model" and (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:/+\-]{1,160}", value)):
            raise ValueError("Invalid model identifier")
        if key == "enabled" and not isinstance(value, bool):
            raise ValueError("enabled must be boolean")
        limits = {"timeout": (1, 180), "max_output_tokens": (1, 32000), "concurrency": (1, 16), "daily_limit": (0, 1000), "queue_timeout": (1, 15)}
        if key in limits:
            low, high = limits[key]
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"{key} must be an integer between {low} and {high}")
    if patch.get("provider") is not None and patch.get("protocol") is not None:
        expected = {"google": {"google", "embedding"}, "openai_responses": {"responses"}, "antigravity": {"openai"}, "primary": {"openai"}, "fallback": {"openai"}}
        if patch["protocol"] not in expected.get(patch["provider"], set()):
            raise ValueError("Provider and protocol do not match")
    return dict(patch)


async def load() -> None:
    from sqlalchemy import select
    from .console_state import ConsoleSetting
    from .db import get_session
    async with await get_session() as session:
        rows = (await session.execute(select(ConsoleSetting))).scalars().all()
    _OVERRIDES.clear()
    _VERSIONS.clear()
    legacy = None
    for row in rows:
        if row.key.startswith("route:"):
            role = row.key.split(":", 1)[1]
            if role in DEFAULTS:
                _OVERRIDES[role] = validate_patch(json.loads(row.values_json))
                _VERSIONS[role] = row.version
        elif row.key == "global":
            legacy = json.loads(row.values_json).get("model")
    if legacy and "group_chat" not in _OVERRIDES:
        await update("group_chat", {"model": legacy}, actor="migration", version=0)


async def update(role: str, patch: dict, *, actor: str, version: int) -> dict:
    from sqlalchemy import select, update as sql_update
    from sqlalchemy.exc import IntegrityError
    from .console_state import ConsoleSetting, VersionConflict, _add_audit, local_now
    from .db import get_session
    role = ALIASES.get(role, role)
    if role not in DEFAULTS:
        raise ValueError("Unknown model purpose")
    patch = validate_patch(patch)
    if role == "group_chat" and (patch.get("timeout") or 0) > 90:
        raise ValueError("Main chat calls must finish within 90 seconds")
    async with _UPDATE_LOCK:
        async with await get_session() as session:
            key = f"route:{role}"
            row = (await session.execute(select(ConsoleSetting).where(ConsoleSetting.key == key).with_for_update())).scalar_one_or_none()
            actual = row.version if row else 0
            if isinstance(version, bool) or version != actual:
                raise VersionConflict("Routing changed; reload before saving")
            value = json.loads(row.values_json) if row else {}
            for name, item in patch.items():
                if item is None:
                    value.pop(name, None)
                else:
                    value[name] = item
            configured = ((config().get("agent") or {}).get("model_routes") or {}).get(role) or {}
            effective_protocol = value.get("protocol", configured.get("protocol", DEFAULTS[role][1]))
            effective_provider = value.get("provider", configured.get("provider", DEFAULTS[role][0]))
            validate_patch({"provider": effective_provider, "protocol": effective_protocol})
            if (role == "embedding") != (effective_protocol == "embedding"):
                raise ValueError("Embedding protocol is only valid for the vector route")
            if role == "embedding" and value.get("max_output_tokens", configured.get("max_output_tokens", 768)) != 768:
                raise ValueError("The current memory index requires 768 dimensions")
            values = dict(values_json=json.dumps(value), version=actual + 1, updated_at=local_now())
            if row:
                result = await session.execute(sql_update(ConsoleSetting).where(ConsoleSetting.key == key, ConsoleSetting.version == actual).values(**values))
                if result.rowcount != 1:
                    raise VersionConflict("Routing changed; reload before saving")
            else:
                session.add(ConsoleSetting(key=key, **values))
            _add_audit(session, "route.update", key, {"fields": sorted(patch), "version": actual + 1}, actor)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                raise VersionConflict("Routing changed; reload before saving") from None
            _OVERRIDES[role], _VERSIONS[role] = value, actual + 1
    return public_route(role)


def _openai_result(data: dict) -> str:
    msg = (data.get("choices") or [{}])[0].get("message") or {}
    calls = msg.get("tool_calls") or []
    if calls:
        tools = []
        for call in calls:
            function = call.get("function") or {}
            args = function.get("arguments") or "{}"
            tools.append({"name": function.get("name"), "arguments": json.loads(args) if isinstance(args, str) else args})
        return json.dumps({"action": "tool", "tool_calls": tools}, ensure_ascii=False)
    content = msg.get("content")
    if isinstance(content, list):
        content = "\n".join(str(p.get("text") or "") for p in content if isinstance(p, dict))
    if not isinstance(content, str) or not content.strip():
        raise RouteError("Provider returned no text")
    return content.strip()


def _text_size(messages: list[dict]) -> int:
    total = 0
    for message in messages:
        content = message.get("content", "")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            total += sum(len(str(part.get("text", ""))) for part in content if isinstance(part, dict) and part.get("type") == "text")
    return total


def _google_payload(messages: list[dict], route: Route) -> dict:
    contents, system = [], []
    for message in messages:
        content = message.get("content", "")
        if message.get("role") in {"system", "developer"}:
            system.append(str(content))
            continue
        parts = []
        for p in ([{"type": "text", "text": content}] if isinstance(content, str) else content):
            if p.get("type") == "text":
                parts.append({"text": p["text"]})
            elif p.get("type") == "image_url":
                url = (p.get("image_url") or {}).get("url", "")
                match = re.fullmatch(r"data:([^;]+);base64,(.+)", url, re.S)
                if match:
                    parts.append({"inlineData": {"mimeType": match[1], "data": match[2]}})
        if parts:
            contents.append({"role": "model" if message.get("role") == "assistant" else "user", "parts": parts})
    result = {"contents": contents, "generationConfig": {"maxOutputTokens": route.max_output_tokens}}
    if system:
        result["systemInstruction"] = {"parts": [{"text": "\n".join(system)}]}
    return result


async def _dispatch(route: Route, messages: list[dict], timeout: float, backend_search: bool) -> tuple[str, dict]:
    if route.protocol == "responses":
        from .responses_api import call_responses
        raw = provider_config(route.provider)
        raw.update(model=route.model, api_key=route.api_key, base_url=route.base_url, responses_url=route.base_url, api_backend="responses")
        result = await call_responses({"ai": {"openai_responses": raw}}, messages, timeout=timeout, max_output_tokens=route.max_output_tokens, enable_web_search=False)
        return result, {}
    if route.protocol not in {"openai", "google"}:
        raise RouteError("Use the embedding worker for vector requests")
    base = route.base_url.rstrip("/")
    if route.protocol == "google":
        url = f"{base}/models/{route.model}:generateContent"
        payload = _google_payload(messages, route)
        headers = {"x-goog-api-key": route.api_key}
    else:
        url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
        payload = {"model": route.model, "messages": messages, "stream": False, "max_tokens": route.max_output_tokens}
        if backend_search and route.provider == "antigravity":
            payload["tools"] = [{"googleSearch": {}}]
        headers = {"Authorization": f"Bearer {route.api_key}"}
    async with httpx.AsyncClient(timeout=timeout, proxy=route.proxy or None, trust_env=False) as client:
        response = await client.post(url, headers=headers, json=payload)
        # A relay may not support Google's native search extension. Retry only
        # this capability rejection, with the same enclosing call deadline.
        if response.status_code in {400, 422} and "tools" in payload:
            payload.pop("tools")
            response = await client.post(url, headers=headers, json=payload)
        if not response.is_success:
            raise RouteError(f"{route.role}: HTTP {response.status_code}; check provider configuration")
        data = response.json()
        if route.protocol == "openai":
            return _openai_result(data), data.get("usage") or {}
        parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
        result = "\n".join(p.get("text", "") for p in parts if not p.get("thought"))
        if not result.strip():
            raise RouteError("Provider returned no text")
        return result, data.get("usageMetadata") or {}


async def complete(role: str, messages: list[dict[str, Any]], *, group_id: int = 0, purpose: str | None = None, timeout: float | None = None, **_kwargs) -> str:
    route = resolve(role, group_id)
    if not route.enabled:
        raise RouteError(f"{route.role} is disabled")
    if not route.api_key or not route.base_url or not route.model:
        provider_path = "ai" if route.provider == "primary" else f"ai.{route.provider}"
        raise RouteError(f"{route.role}: configure {provider_path} credentials, endpoint and model")
    hard_max = HARD_TIMEOUTS.get(route.role, 180)
    limit = min(float(timeout or route.timeout), route.timeout, hard_max)
    key = (id(asyncio.get_running_loop()), route.role, route.concurrency)
    slot = _SLOTS.setdefault(key, asyncio.Semaphore(route.concurrency))
    started, status, usage = time.monotonic(), "failed", {}
    from .agent_requests import REQUEST_CONTEXT
    old = REQUEST_CONTEXT.get() or {}
    metadata = {**old, "model": route.model, "timeout": limit, "purpose": purpose or old.get("purpose") or route.role, "group_id": group_id or old.get("group_id", 0), "request_id": old.get("request_id") or uuid4().hex[:12]}
    token = REQUEST_CONTEXT.set(metadata)
    acquired = False
    try:
        await asyncio.wait_for(slot.acquire(), min(route.queue_timeout, limit))
        acquired = True
        result, usage = await asyncio.wait_for(_dispatch(route, messages, limit, bool(old.get("backend_search", False))), limit)
        status = "success"
        return result
    except (TimeoutError, httpx.TimeoutException):
        status = "timeout"
        raise TimeoutError(f"{route.role} timed out") from None
    except RouteError:
        raise
    except Exception as exc:
        raise RouteError(f"{route.role}: {type(exc).__name__}; check provider connectivity") from None
    finally:
        if acquired:
            slot.release()
        REQUEST_CONTEXT.reset(token)
        try:
            from . import console_runtime
            console_runtime.record("model_request", int(metadata["group_id"]), 0, metadata["request_id"], request_id=metadata["request_id"], attempt=1, source=metadata["purpose"], provider=route.provider, model=route.model, status=status, latency_ms=int((time.monotonic()-started)*1000), input_chars=_text_size(messages), input_tokens=usage.get("prompt_tokens", usage.get("promptTokenCount")), output_tokens=usage.get("completion_tokens", usage.get("candidatesTokenCount")))
        except Exception:
            pass
