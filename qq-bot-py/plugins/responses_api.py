"""Small OpenAI Responses-compatible client used by the bot's non-AG paths.

The relay URL and model are configuration, while the API key is read from an
environment variable.  Keeping this client provider-neutral lets chat
fallbacks, search, translation and the development Agent share one wire
implementation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Iterable

import httpx


class ResponsesAPIError(RuntimeError):
    """Raised when a Responses endpoint rejects a request or returns no text."""


@dataclass(frozen=True)
class ResponsesConfig:
    name: str
    model: str
    base_url: str
    api_key: str
    api_backend: str = "responses"
    supports_backend_search: bool = False
    timeout: float = 120.0
    reasoning_effort: str = ""
    reasoning_summary: str = ""
    store: bool = False

    @property
    def responses_url(self) -> str:
        base = self.base_url.rstrip("/")
        if base.endswith("/responses"):
            return base
        return f"{base}/responses"


def _api_key(raw: dict[str, Any]) -> str:
    direct = str(raw.get("api_key") or "").strip()
    if direct:
        return direct
    env_name = str(raw.get("api_key_env") or "OPENAI_RESPONSES_API_KEY").strip()
    value = os.getenv(env_name, "").strip()
    if value:
        return value
    # NoneBot reads .env into its settings, not necessarily into os.environ.
    # Read the same file as a fallback so plugin imports see the configured key.
    try:
        from dotenv import dotenv_values

        return str(dotenv_values(".env").get(env_name) or "").strip()
    except Exception:
        return ""


def load_responses_config(config: dict[str, Any], section: str = "openai_responses") -> ResponsesConfig:
    raw = (config.get("ai") or {}).get(section) or {}
    cfg = ResponsesConfig(
        name=str(raw.get("name") or "OpenAI Responses"),
        model=str(raw.get("model") or ""),
        base_url=str(raw.get("responses_url") or raw.get("base_url") or ""),
        api_key=_api_key(raw),
        api_backend=str(raw.get("api_backend") or "responses").lower(),
        supports_backend_search=bool(raw.get("supports_backend_search", False)),
        timeout=float(raw.get("timeout") or 120),
        reasoning_effort=str(raw.get("reasoning_effort") or "").strip(),
        reasoning_summary=str(raw.get("reasoning_summary") or "").strip(),
        store=bool(raw.get("store", False)),
    )
    if cfg.api_backend != "responses":
        raise ResponsesAPIError(f"Unsupported Responses backend: {cfg.api_backend}")
    if not cfg.model or not cfg.base_url or not cfg.api_key:
        raise ResponsesAPIError("OpenAI Responses configuration is incomplete")
    return cfg


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content or "")
    texts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") in {"text", "input_text", "output_text"}:
            text = part.get("text")
            if text:
                texts.append(str(text))
    return "\n".join(texts)


def messages_to_responses_input(
    messages: Iterable[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    instructions: list[str] = []
    input_items: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "user").lower()
        content = message.get("content")
        text = _content_to_text(content)
        if not text:
            continue
        if role in {"system", "developer"}:
            instructions.append(text)
            continue
        if role not in {"user", "assistant"}:
            role = "user"
        input_items.append({"role": role, "content": text})
    return "\n\n".join(instructions).strip(), input_items


def build_responses_payload(
    cfg: ResponsesConfig,
    messages: Iterable[dict[str, Any]],
    *,
    enable_web_search: bool = False,
    max_output_tokens: int | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    include_reasoning: bool = True,
) -> dict[str, Any]:
    instructions, input_items = messages_to_responses_input(messages)
    payload: dict[str, Any] = {
        "model": cfg.model,
        "input": input_items,
        "store": cfg.store,
    }
    if instructions:
        payload["instructions"] = instructions
    output_limit = max_output_tokens if max_output_tokens is not None else max_tokens
    if output_limit is not None:
        payload["max_output_tokens"] = int(output_limit)
    if temperature is not None:
        payload["temperature"] = float(temperature)
    if include_reasoning and (cfg.reasoning_effort or cfg.reasoning_summary):
        reasoning: dict[str, str] = {}
        if cfg.reasoning_effort:
            reasoning["effort"] = cfg.reasoning_effort
        if cfg.reasoning_summary:
            reasoning["summary"] = cfg.reasoning_summary
        payload["reasoning"] = reasoning
    if enable_web_search:
        if not cfg.supports_backend_search:
            raise ResponsesAPIError("Responses backend search is disabled in configuration")
        payload["tools"] = [{"type": "web_search"}]
    return payload


def parse_responses_text(data: dict[str, Any]) -> str:
    top_level = data.get("output_text")
    if isinstance(top_level, str) and top_level.strip():
        return top_level.strip()
    texts: list[str] = []
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        item_text = item.get("output_text")
        if isinstance(item_text, str) and item_text.strip():
            texts.append(item_text.strip())
        content = item.get("content")
        if isinstance(content, str) and content.strip():
            texts.append(content.strip())
            continue
        for part in content or []:
            if not isinstance(part, dict):
                continue
            part_text = part.get("text") or part.get("output_text")
            if isinstance(part_text, str) and part_text.strip():
                texts.append(part_text.strip())
    return "\n".join(texts).strip()


def _error_detail(data: dict[str, Any], status_code: int) -> str:
    detail = data.get("error") or data.get("message") or f"HTTP {status_code}"
    if isinstance(detail, dict):
        detail = detail.get("message") or detail.get("code") or str(detail)
    return str(detail)[:500]


async def request_responses(
    config: dict[str, Any],
    messages: Iterable[dict[str, Any]],
    *,
    section: str = "openai_responses",
    enable_web_search: bool = False,
    max_output_tokens: int | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    timeout: float | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[ResponsesConfig, dict[str, Any], httpx.Response]:
    cfg = load_responses_config(config, section=section)
    payload = build_responses_payload(
        cfg,
        messages,
        enable_web_search=enable_web_search,
        max_output_tokens=max_output_tokens,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    async with httpx.AsyncClient(timeout=timeout or cfg.timeout, transport=transport) as client:
        response = await client.post(
            cfg.responses_url,
            headers={
                "Authorization": f"Bearer {cfg.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
    if not response.content:
        raise ResponsesAPIError(f"Responses returned an empty body (HTTP {response.status_code})")
    try:
        data = response.json()
    except ValueError as exc:
        raise ResponsesAPIError(
            f"Responses returned non-JSON data (HTTP {response.status_code})"
        ) from exc
    if not isinstance(data, dict):
        raise ResponsesAPIError(f"Responses returned invalid JSON body (HTTP {response.status_code})")
    if response.status_code < 200 or response.status_code >= 300:
        raise ResponsesAPIError(
            f"Responses API error (HTTP {response.status_code}): {_error_detail(data, response.status_code)}"
        )
    if data.get("error"):
        raise ResponsesAPIError(f"Responses API error: {data['error']}")
    return cfg, data, response


async def call_responses(
    config: dict[str, Any],
    messages: Iterable[dict[str, Any]],
    *,
    section: str = "openai_responses",
    enable_web_search: bool = False,
    max_output_tokens: int | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    timeout: float | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    try:
        cfg, data, _ = await request_responses(
            config,
            messages,
            section=section,
            enable_web_search=enable_web_search,
            max_output_tokens=max_output_tokens,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
            transport=transport,
        )
    except ResponsesAPIError as exc:
        # Some relays expose Responses but reject optional reasoning summaries.
        # Retry once without the optional block so ordinary chat remains usable.
        if "reasoning" not in str(exc).lower():
            raise
        cfg = load_responses_config(config, section=section)
        instructions, input_items = messages_to_responses_input(messages)
        payload: dict[str, Any] = {"model": cfg.model, "input": input_items, "store": cfg.store}
        if instructions:
            payload["instructions"] = instructions
        if max_output_tokens is not None or max_tokens is not None:
            payload["max_output_tokens"] = int(
                max_output_tokens if max_output_tokens is not None else max_tokens
            )
        if enable_web_search:
            if not cfg.supports_backend_search:
                raise
            payload["tools"] = [{"type": "web_search"}]
        async with httpx.AsyncClient(timeout=timeout or cfg.timeout, transport=transport) as client:
            response = await client.post(
                cfg.responses_url,
                headers={"Authorization": f"Bearer {cfg.api_key}", "Content-Type": "application/json"},
                json=payload,
            )
        if response.status_code < 200 or response.status_code >= 300:
            raise
        data = response.json()
    text = parse_responses_text(data)
    if not text:
        raise ResponsesAPIError(f"Responses returned no text (status={data.get('status', 'unknown')})")
    return text
