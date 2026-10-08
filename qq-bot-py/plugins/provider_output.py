"""Normalize model output before it reaches chat reply parsing."""

from __future__ import annotations

import json
import re


_SEARCH_ACTIVITY = re.compile(
    r"(?:[，,。；;]\s*)?🔍\s*(?:"
    r"已为(?:您|你)?搜索|已搜索|搜索(?:查询)?|searched(?:\s+for)?"
    r")\s*[：:]\s*[^\r\n]*(?:\r?\n|$)",
    flags=re.IGNORECASE,
)
_REPLIES_PREFIX = re.compile(
    r"^\s*\{\s*[\"']replies[\"']\s*:",
    flags=re.IGNORECASE | re.DOTALL,
)
_MALFORMED_EMPTY_FIRST_REPLY = re.compile(
    r"^\s*\{\s*\"replies\"\s*:\s*\"\s*,\s*\""
    r"(?P<reply>(?:\\.|[^\"\\])*)\"\s*\]\s*\}\s*$",
    flags=re.DOTALL,
)
_CITATION_PART = re.compile(r"^\s*\[\d+")
_INLINE_CITATION_MARKER = re.compile(
    r"\s*\[(?:\d+\.)+\d+\](?=\s*(?:[。！？!?；;，,、]|$))"
)
_THINKING_SUFFIX = re.compile(
    r"\s*\(\d+\s*[字charsa-zA-Z]+\)\s*[->\s\.A-Za-z]*$"
)


def strip_tool_activity(text: str | None) -> str:
    """Remove provider search-status text while preserving the actual answer."""
    cleaned, removed = _SEARCH_ACTIVITY.subn("", text or "")
    cleaned = cleaned.strip()
    if removed:
        cleaned = cleaned.rstrip("，,；;").strip()
    return cleaned


def strip_inline_citation_markers(text: str | None) -> str:
    """Remove leaked provider citations such as ``[1.1.2]`` near sentence ends."""
    return _INLINE_CITATION_MARKER.sub("", text or "").strip()


def extract_json_object(text: str | None) -> dict | None:
    raw = strip_tool_activity(text)
    if not raw:
        return None
    # Some compatible gateways emit a single-backtick ``json`` wrapper.
    # Accept it just like a regular code fence so structured Agent output
    # cannot fall through to the plain-text reply path.
    raw = re.sub(r"^`{1,3}\s*(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s*`{1,3}$", "", raw).strip()
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except Exception:
        pass

    start = raw.find("{")
    if start < 0:
        return None
    try:
        value, _end = json.JSONDecoder().raw_decode(raw[start:])
        return value if isinstance(value, dict) else None
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def looks_like_replies_payload(text: str | None) -> bool:
    raw = strip_tool_activity(text)
    raw = re.sub(r"^`{1,3}\s*(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    return bool(_REPLIES_PREFIX.match(raw))


def recover_malformed_replies(text: str | None) -> list[str]:
    """Recover useful text from common truncated/malformed replies JSON."""
    raw = strip_tool_activity(text)
    raw = re.sub(r"^`{1,3}\s*(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    prefix = _REPLIES_PREFIX.match(raw)
    if not prefix:
        return []

    # The outer object may be truncated while its value is still valid.
    value_text = re.sub(r"\}\s*$", "", raw[prefix.end():].strip())
    try:
        value = json.loads(value_text)
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [item for item in value if isinstance(item, str)]
    except Exception:
        pass

    # Antigravity has emitted :","answer"]} after a googleSearch call.  The
    # first item is empty and its opening list bracket is missing, but the
    # second item is still a complete JSON string and can be recovered safely.
    match = _MALFORMED_EMPTY_FIRST_REPLY.match(raw)
    if not match:
        return []
    try:
        return [json.loads(f'"{match.group("reply")}"')]
    except Exception:
        return []


def extract_gemini_text(data: dict, *, skip_citation_parts: bool = False) -> str:
    """Extract final Gemini text without thought or search activity parts."""
    chunks: list[str] = []
    for candidate in data.get("candidates", []):
        for part in candidate.get("content", {}).get("parts", []):
            if part.get("thought") is True:
                continue
            text = part.get("text", "")
            if not isinstance(text, str) or not text:
                continue
            if skip_citation_parts and _CITATION_PART.match(text):
                continue
            chunks.append(_THINKING_SUFFIX.sub("", text))
    return strip_tool_activity("".join(chunks))
