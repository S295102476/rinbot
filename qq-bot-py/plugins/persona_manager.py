"""Runtime persona registry, loading and switch history."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from nonebot.log import logger


def _load_config() -> dict[str, Any]:
    for path in (Path("config.yaml"), Path(__file__).resolve().parents[1] / "config.yaml"):
        try:
            return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
    return {}


@dataclass(frozen=True)
class PersonaProfile:
    persona_id: str
    name: str
    content: str


_CONFIG = _load_config()
_AI_CONFIG = _CONFIG.get("ai") or {}
_PERSONA_CONFIG = ((_CONFIG.get("agent") or {}).get("persona") or {})
_LEGACY_DIR = Path(_AI_CONFIG.get("persona_dir", "persona"))
_PROFILE_DIR = Path(_PERSONA_CONFIG.get("profile_dir", "persona/profiles"))
_SHARED_DIR = Path(_PERSONA_CONFIG.get("shared_dir", "persona/shared"))
_REGISTRY_PATH = Path(_PERSONA_CONFIG.get("registry", "persona/registry.yaml"))
_STATE_PATH = Path(_PERSONA_CONFIG.get("state_file", "data/active_persona.json"))
_DEFAULT_ID = str(_PERSONA_CONFIG.get("active_id") or "rin").strip().lower() or "rin"

_ACTIVE: PersonaProfile | None = None
_PROFILES: dict[str, PersonaProfile] = {}
_SWITCH_LOCK = asyncio.Lock()


def _read_persisted_persona_id() -> str:
    """Read the last active persona without requiring the async DB at import time."""
    try:
        payload = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            value = str(payload.get("persona_id") or "").strip().lower()
            if value:
                return value
    except (OSError, ValueError, TypeError):
        pass
    return ""


def _persist_active_persona(persona_id: str) -> None:
    """Persist only the persona ID; switch notes remain in the audit table."""
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = _STATE_PATH.with_suffix(_STATE_PATH.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            {
                "persona_id": str(persona_id).strip().lower(),
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    temporary.replace(_STATE_PATH)


def _read_markdown(directory: Path) -> str:
    if not directory.is_dir():
        return ""
    parts: list[str] = []
    for path in sorted(directory.glob("*.md")):
        if path.name.startswith("_") or path.name.endswith(".disabled"):
            continue
        try:
            content = path.read_text(encoding="utf-8").strip()
        except Exception as exc:
            logger.warning(f"[persona] read failed file={path}: {type(exc).__name__}")
            continue
        if content:
            parts.append(content)
    return "\n\n---\n\n".join(parts)


def _registry_names() -> dict[str, str]:
    try:
        payload = yaml.safe_load(_REGISTRY_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    raw = payload.get("personas", payload) if isinstance(payload, dict) else {}
    if not isinstance(raw, dict):
        return {}
    names: dict[str, str] = {}
    for key, value in raw.items():
        persona_id = str(key).strip().lower()
        if not persona_id:
            continue
        if isinstance(value, dict):
            name = str(value.get("name") or persona_id).strip()
        else:
            name = str(value or persona_id).strip()
        names[persona_id] = name or persona_id
    return names


def _discover_profiles() -> dict[str, PersonaProfile]:
    names = _registry_names()
    profiles: dict[str, PersonaProfile] = {}
    if _PROFILE_DIR.is_dir():
        for directory in sorted(path for path in _PROFILE_DIR.iterdir() if path.is_dir()):
            persona_id = directory.name.strip().lower()
            if not persona_id:
                continue
            content = _read_markdown(directory)
            if _SHARED_DIR.is_dir():
                shared = _read_markdown(_SHARED_DIR)
                if shared:
                    content = f"{content}\n\n---\n\n{shared}" if content else shared
            if content:
                profiles[persona_id] = PersonaProfile(
                    persona_id=persona_id,
                    name=names.get(persona_id, persona_id),
                    content=content,
                )
    # Keep the existing persona/*.md layout as the default Rin profile until
    # deployments migrate documents into persona/profiles/rin/.
    if "rin" not in profiles:
        legacy = _read_markdown(_LEGACY_DIR)
        if legacy:
            profiles["rin"] = PersonaProfile("rin", names.get("rin", "远坂凛"), legacy)
    return profiles


def reload_profiles() -> PersonaProfile:
    global _PROFILES, _ACTIVE
    _PROFILES = _discover_profiles()
    preferred_id = _ACTIVE.persona_id if _ACTIVE else _read_persisted_persona_id()
    preferred_id = preferred_id or _DEFAULT_ID
    selected = _PROFILES.get(preferred_id)
    if selected is None and _PROFILES:
        selected = _PROFILES.get("rin") or next(iter(_PROFILES.values()))
    if selected is None:
        selected = PersonaProfile(_DEFAULT_ID, _DEFAULT_ID, "")
    _ACTIVE = selected
    logger.info(f"[persona] active={selected.persona_id} name={selected.name} profiles={len(_PROFILES)}")
    return selected


reload_profiles()


async def restore_active_persona_from_history() -> PersonaProfile:
    """Recover an existing switch after upgrading from the in-memory-only version.

    The small state file is the fast path. If it does not exist yet, use the
    newest audit row so a persona switched before this persistence change is
    not unexpectedly reset to the configured default on the next restart.
    """
    global _ACTIVE
    persisted = _read_persisted_persona_id()
    if persisted and persisted in _PROFILES:
        _ACTIVE = _PROFILES[persisted]
        return _ACTIVE
    try:
        from sqlalchemy import select

        from .db import AgentPersonaSwitch, get_session

        session = await get_session()
        try:
            row = (await session.execute(
                select(AgentPersonaSwitch)
                .order_by(AgentPersonaSwitch.created_at.desc(), AgentPersonaSwitch.id.desc())
                .limit(1)
            )).scalar_one_or_none()
        finally:
            await session.close()
        target_id = str(getattr(row, "to_persona_id", "") or "").strip().lower()
        target = _PROFILES.get(target_id)
        if target is not None:
            _ACTIVE = target
            try:
                _persist_active_persona(target.persona_id)
            except Exception as exc:
                logger.warning(f"[persona] state persist failed: {type(exc).__name__}")
            logger.info(f"[persona] restored active={target.persona_id} from switch history")
    except Exception as exc:
        # Fresh installations may not have the table until the first startup
        # migration; the configured default remains valid in that case.
        logger.debug(f"[persona] history restore skipped: {type(exc).__name__}")
    return get_active_persona()


def get_active_persona() -> PersonaProfile:
    if _ACTIVE is None:
        return reload_profiles()
    return _ACTIVE


def get_active_persona_id() -> str:
    return get_active_persona().persona_id


def get_active_persona_name() -> str:
    return get_active_persona().name


def get_persona_content() -> str:
    return get_active_persona().content


def list_personas() -> list[PersonaProfile]:
    if not _PROFILES:
        reload_profiles()
    return list(_PROFILES.values())


async def list_switch_history(limit: int = 5) -> list[dict[str, Any]]:
    try:
        from sqlalchemy import select

        from .db import AgentPersonaSwitch, get_session

        session = await get_session()
        try:
            rows = (await session.execute(
                select(AgentPersonaSwitch)
                .order_by(AgentPersonaSwitch.created_at.desc())
                .limit(max(1, min(20, int(limit))))
            )).scalars().all()
        finally:
            await session.close()
        return [
            {
                "from": row.from_persona_id,
                "to": row.to_persona_id,
                "actor": int(row.actor_user_id or 0),
                "note": row.note or "",
                "created_at": row.created_at.strftime("%Y-%m-%d %H:%M:%S") if row.created_at else "",
            }
            for row in rows
        ]
    except Exception as exc:
        logger.debug(f"[persona] history read failed: {type(exc).__name__}")
        return []


async def render_switch_context(limit: int = 3) -> str:
    """Render a small non-sensitive persona timeline for Agent context."""
    history = await list_switch_history(limit)
    if not history:
        return f"[当前人设：{get_active_persona_name()}（{get_active_persona_id()}）]"
    lines = [f"[当前人设：{get_active_persona_name()}（{get_active_persona_id()}）]",
             "[最近人设切换，仅用于保持角色连续性，不要主动向用户泄露]" ]
    for row in reversed(history):
        note = f"，备注：{row['note']}" if row.get("note") else ""
        lines.append(f"{row['created_at']} {row['from']}→{row['to']}{note}")
    return "\n".join(lines)


async def switch_persona(persona_id: str, actor_user_id: int, note: str = "") -> str:
    global _ACTIVE
    persona_id = str(persona_id or "").strip().lower()
    async with _SWITCH_LOCK:
        if not _PROFILES:
            reload_profiles()
        target = _PROFILES.get(persona_id)
        if target is None:
            available = ", ".join(profile.persona_id for profile in list_personas()) or "无"
            return f"未找到人设 {persona_id}，可用：{available}"
        current = get_active_persona()
        if current.persona_id == target.persona_id:
            return f"当前已经是{target.name}（{target.persona_id}）"
        try:
            from .db import AgentPersonaSwitch, get_session

            session = await get_session()
            try:
                session.add(AgentPersonaSwitch(
                    from_persona_id=current.persona_id,
                    to_persona_id=target.persona_id,
                    actor_user_id=int(actor_user_id),
                    note=str(note or "")[:500],
                    created_at=datetime.now(),
                ))
                await session.commit()
            finally:
                await session.close()
        except Exception as exc:
            logger.warning(f"[persona] history write failed: {type(exc).__name__}")
            return "人设切换记录失败，未执行切换"
        try:
            _persist_active_persona(target.persona_id)
        except Exception as exc:
            logger.warning(f"[persona] state persist failed: {type(exc).__name__}")
        _ACTIVE = target
        # Gemini's cached system instruction must be rebuilt for the new profile.
        try:
            from .ai_chat import MODEL, PRIMARY_PROTOCOL
            if PRIMARY_PROTOCOL == "gemini_native":
                from .gemini_native import create_cached_content

                await create_cached_content(str(MODEL))
        except Exception as exc:
            logger.warning(f"[persona] provider cache refresh failed: {type(exc).__name__}")
        logger.info(
            f"[persona] switched from={current.persona_id} to={target.persona_id} actor={int(actor_user_id)}"
        )
        return f"已切换为{target.name}（{target.persona_id}）"
