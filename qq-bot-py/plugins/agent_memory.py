"""Bounded long-term memory for the group Agent.

Raw group messages stay in ``GroupMessage`` and the hot context cache.  This
module stores only compact, source-backed facts, episode summaries and
deterministic interaction state so a restart never turns the Agent into a
blank slate.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Any, Iterable, Sequence

import yaml
from nonebot.log import logger
from sqlalchemy import delete, or_, select

from .db import (
    AgentPersonaAffinity,
    AgentGlobalPersonFact,
    AgentMemoryEpisode,
    AgentPersonFact,
    AgentPersonaRelationshipState,
    AgentRelationshipState,
    UserAffinity,
    get_session,
)
from .affinity import relationship_stage
from .console_services import MEMORY_WRITE_LOCK, is_memory_protected


_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.DOTALL)
_SPACE_RE = re.compile(r"\s+")
_FACT_CATEGORIES = {"identity", "preference", "habit", "project", "relationship", "general"}
_GLOBAL_FACT_CATEGORIES = {"identity", "preference", "habit", "project", "general"}


def _load_memory_config() -> dict[str, Any]:
    for path in (Path("config.yaml"), Path(__file__).resolve().parents[1] / "config.yaml"):
        try:
            config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            return ((config.get("agent") or {}).get("memory") or {})
        except Exception:
            continue
    return {}


_CONFIG = _load_memory_config()
_ENABLED = bool(_CONFIG.get("enabled", True))
_EPISODE_LIMIT = max(10, min(500, int(_CONFIG.get("episode_limit_per_group", 120))))
_EPISODE_RECALL_LIMIT = max(0, min(4, int(_CONFIG.get("episode_recall_limit", 2))))
_FACT_LIMIT_PER_USER = max(5, min(200, int(_CONFIG.get("fact_limit_per_user", 40))))
_FACT_RECALL_LIMIT = max(1, min(12, int(_CONFIG.get("fact_recall_limit", 6))))
_MAX_FACTS_PER_WRITEBACK = max(1, min(12, int(_CONFIG.get("max_facts_per_writeback", 8))))


def _current_persona_id() -> str:
    try:
        from .persona_manager import get_active_persona_id

        return get_active_persona_id()
    except Exception:
        return "rin"


def _clean_text(value: object, limit: int) -> str:
    return _SPACE_RE.sub(" ", str(value or "")).strip()[:limit]


def _fingerprint(value: str) -> str:
    normalized = re.sub(r"[\s\W_]+", "", value.casefold())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _unique_recalled_facts(rows: Iterable[Any], limit: int) -> list[Any]:
    """Deduplicate prompt entries without changing stored facts or meaning."""
    result: list[Any] = []
    seen: set[tuple[int, str, str]] = set()
    for row in rows:
        fact = _SPACE_RE.sub(" ", str(row.fact or "")).strip()
        key = (int(row.user_id), str(row.category), fact)
        if not fact or key in seen:
            continue
        seen.add(key)
        result.append(row)
        if len(result) >= limit:
            break
    return result


def parse_writeback(raw: object) -> tuple[str, list[dict[str, Any]]]:
    """Read the summary/fact JSON emitted by the asynchronous writeback call.

    A plain-text model response remains a usable summary.  Facts are rejected
    in that case instead of guessing at a malformed structure.
    """
    text = str(raw or "").strip()
    if not text:
        return "", []
    candidate = _JSON_FENCE_RE.sub("", text).strip()
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end <= start:
            return _clean_text(text, 5000), []
        try:
            payload = json.loads(candidate[start:end + 1])
        except json.JSONDecodeError:
            return _clean_text(text, 5000), []
    if not isinstance(payload, dict):
        return _clean_text(text, 5000), []
    summary = _clean_text(payload.get("summary"), 5000)
    facts = payload.get("facts")
    return summary, facts if isinstance(facts, list) else []


def _message_values(messages: Sequence[Any]) -> tuple[dict[int, int], set[int]]:
    """Return source-message ownership and valid non-bot speakers."""
    source_owners: dict[int, int] = {}
    speakers: set[int] = set()
    for item in messages:
        if bool(getattr(item, "is_bot", False)):
            continue
        try:
            user_id = int(getattr(item, "user_id", 0) or 0)
            message_id = int(getattr(item, "message_id", 0) or 0)
        except (TypeError, ValueError):
            continue
        if user_id <= 0:
            continue
        speakers.add(user_id)
        if message_id > 0:
            source_owners[message_id] = user_id
    return source_owners, speakers


def _validated_fact(
    raw: object,
    source_owners: dict[int, int],
) -> tuple[int, str, str, int, float, int, int] | None:
    if not isinstance(raw, dict):
        return None
    try:
        user_id = int(raw.get("user_id") or 0)
        source_message_id = int(raw.get("source_message_id") or 0)
        importance = max(1, min(5, int(raw.get("importance") or 1)))
        confidence = max(0.0, min(1.0, float(raw.get("confidence") or 0.5)))
        expires_days = max(0, min(365, int(raw.get("expires_days") or 0)))
    except (TypeError, ValueError):
        return None
    fact = _clean_text(raw.get("fact"), 280)
    category = _clean_text(raw.get("category"), 32).lower() or "general"
    if category not in _FACT_CATEGORIES:
        category = "general"
    if not fact or user_id <= 0 or source_message_id <= 0:
        return None
    # The writer can only save a fact for the author of a concrete source
    # message in this episode.  This makes the record auditable and prevents
    # cross-user attribution hallucinations.
    if source_owners.get(source_message_id) != user_id:
        return None
    return user_id, fact, category, importance, confidence, source_message_id, expires_days


def _serialized_memory_write(method):
    @wraps(method)
    async def serialized(*args, **kwargs):
        async with MEMORY_WRITE_LOCK:
            return await method(*args, **kwargs)
    return serialized


async def _delete_unprotected(session, model, rows, kind: str, scope: str) -> None:
    ids = []
    for row in rows:
        if not await is_memory_protected(
            session, kind, scope, group_id=int(getattr(row, "group_id", 0) or 0),
            user_id=int(getattr(row, "user_id", 0) or 0),
            identity=str(row.end_message_id if kind == "episodes" else row.fingerprint)
        ):
            ids.append(row.id)
    if ids:
        await session.execute(delete(model).where(model.id.in_(ids)))


class AgentMemoryStore:
    @_serialized_memory_write
    async def promote_legacy_relationships(self) -> int:
        """Copy pre-persona relation counters into the default Rin namespace."""
        if not _ENABLED:
            return 0
        session = await get_session()
        moved = 0
        try:
            rows = (await session.execute(select(AgentRelationshipState))).scalars().all()
            existing_affinity_users = set((await session.execute(
                select(AgentPersonaAffinity.user_id).where(
                    AgentPersonaAffinity.persona_id == "rin"
                )
            )).scalars().all())
            migrated_affinities = 0
            legacy_affinities = (await session.execute(select(UserAffinity))).scalars().all()
            for legacy in legacy_affinities:
                user_id = int(legacy.user_id)
                if await is_memory_protected(session, "affinities", "global", user_id=user_id,
                                             persona_id="rin"):
                    continue
                if user_id in existing_affinity_users:
                    continue
                session.add(AgentPersonaAffinity(
                    persona_id="rin",
                    user_id=user_id,
                    affinity_score=float(legacy.affinity_score or 0.0),
                    last_delta=float(legacy.last_delta or 0.0),
                    last_reason=legacy.last_reason or "",
                    updated_at=legacy.updated_at,
                ))
                existing_affinity_users.add(user_id)
                migrated_affinities += 1
            for row in rows:
                if await is_memory_protected(
                    session, "relationships", "group", group_id=int(row.group_id),
                    user_id=int(row.user_id), persona_id="rin"
                ):
                    continue
                existing = (await session.execute(
                    select(AgentPersonaRelationshipState).where(
                        AgentPersonaRelationshipState.persona_id == "rin",
                        AgentPersonaRelationshipState.group_id == int(row.group_id),
                        AgentPersonaRelationshipState.user_id == int(row.user_id),
                    )
                )).scalar_one_or_none()
                if existing is not None:
                    continue
                session.add(AgentPersonaRelationshipState(
                    persona_id="rin",
                    group_id=int(row.group_id),
                    user_id=int(row.user_id),
                    message_count=int(row.message_count or 0),
                    explicit_interaction_count=int(row.explicit_interaction_count or 0),
                    affinity_score=float(row.affinity_score or 0.0),
                    last_delta=float(row.last_delta or 0.0),
                    last_reason=row.last_reason or "",
                    last_message_id=int(row.last_message_id or 0),
                    last_seen_at=row.last_seen_at,
                    last_interaction_at=row.last_interaction_at,
                    updated_at=row.updated_at,
                ))
                moved += 1
            # Some upgraded deployments have only the old per-group mirror.
            # Use it as a fallback, never overwriting the authoritative old
            # global score or an already-persona-scoped value.
            for row in sorted(
                rows,
                key=lambda value: value.updated_at or datetime.min,
                reverse=True,
            ):
                user_id = int(row.user_id)
                if user_id in existing_affinity_users:
                    continue
                if await is_memory_protected(session, "affinities", "global", user_id=user_id,
                                             persona_id="rin"):
                    continue
                session.add(AgentPersonaAffinity(
                    persona_id="rin",
                    user_id=user_id,
                    affinity_score=float(row.affinity_score or 0.0),
                    last_delta=float(row.last_delta or 0.0),
                    last_reason=row.last_reason or "",
                    updated_at=row.updated_at or datetime.now(),
                ))
                existing_affinity_users.add(user_id)
                migrated_affinities += 1
            await session.commit()
            if moved or migrated_affinities:
                logger.info(
                    f"[agent_memory] promoted legacy relationships={moved} "
                    f"affinities={migrated_affinities}"
                )
            return moved + migrated_affinities
        except Exception as exc:
            await session.rollback()
            logger.warning(f"[agent_memory] relationship promotion failed: {type(exc).__name__}")
            return 0
        finally:
            await session.close()

    @_serialized_memory_write
    async def promote_stable_facts(self) -> int:
        """Backfill old group-scoped stable facts into the global fact table."""
        if not _ENABLED:
            return 0
        session = await get_session()
        moved = 0
        try:
            rows = (await session.execute(
                select(AgentPersonFact).where(
                    AgentPersonFact.category.in_(_GLOBAL_FACT_CATEGORIES),
                    AgentPersonFact.status == "active",
                )
            )).scalars().all()
            for row in rows:
                if await is_memory_protected(
                    session, "facts", "group", group_id=int(row.group_id),
                    user_id=int(row.user_id), identity=str(row.fingerprint)
                ) or await is_memory_protected(
                    session, "facts", "global", user_id=int(row.user_id),
                    identity=str(row.fingerprint)
                ):
                    continue
                existing = (await session.execute(
                    select(AgentGlobalPersonFact).where(
                        AgentGlobalPersonFact.user_id == int(row.user_id),
                        AgentGlobalPersonFact.fingerprint == row.fingerprint,
                    )
                )).scalar_one_or_none()
                if existing is None:
                    session.add(AgentGlobalPersonFact(
                        user_id=int(row.user_id),
                        fingerprint=row.fingerprint,
                        category=row.category,
                        fact=row.fact,
                        importance=int(row.importance or 1),
                        confidence=float(row.confidence or 0.5),
                        source_group_id=int(row.group_id),
                        source_message_id=int(row.source_message_id or 0),
                        expires_at=row.expires_at,
                        status=row.status,
                        created_at=row.created_at,
                        updated_at=row.updated_at,
                    ))
                    moved += 1
                elif float(row.confidence or 0.0) > float(existing.confidence or 0.0):
                    existing.confidence = float(row.confidence or 0.0)
                    existing.updated_at = max(existing.updated_at, row.updated_at)
            await session.commit()
            if moved:
                logger.info(f"[agent_memory] promoted stable facts={moved}")
            return moved
        except Exception as exc:
            await session.rollback()
            logger.warning(f"[agent_memory] fact promotion failed: {type(exc).__name__}")
            return 0
        finally:
            await session.close()

    @_serialized_memory_write
    async def persist_writeback(
        self,
        group_id: int,
        messages: Sequence[Any],
        summary: str,
        raw_facts: Iterable[object],
    ) -> None:
        """Persist a bounded episode and source-backed person facts."""
        if not _ENABLED:
            return
        group_id = int(group_id)
        summary = _clean_text(summary, 5000)
        if not summary or not messages:
            return
        source_owners, speakers = _message_values(messages)
        try:
            start_message_id = int(getattr(messages[0], "message_id", 0) or 0)
            end_message_id = int(getattr(messages[-1], "message_id", 0) or 0)
        except (IndexError, TypeError, ValueError):
            return
        if end_message_id <= 0:
            return

        accepted = [
            item
            for raw in list(raw_facts)[:_MAX_FACTS_PER_WRITEBACK]
            if (item := _validated_fact(raw, source_owners)) is not None
        ]
        now = datetime.now()
        session = await get_session()
        try:
            expired = (await session.execute(
                select(AgentPersonFact).where(
                    AgentPersonFact.group_id == group_id,
                    AgentPersonFact.expires_at.is_not(None),
                    AgentPersonFact.expires_at <= now,
                )
            )).scalars().all()
            await _delete_unprotected(session, AgentPersonFact, expired, "facts", "group")
            expired_global = (await session.execute(
                select(AgentGlobalPersonFact).where(
                    AgentGlobalPersonFact.user_id.in_(speakers),
                    AgentGlobalPersonFact.expires_at.is_not(None),
                    AgentGlobalPersonFact.expires_at <= now,
                )
            )).scalars().all()
            await _delete_unprotected(session, AgentGlobalPersonFact, expired_global, "facts", "global")
            episode = (await session.execute(
                select(AgentMemoryEpisode).where(
                    AgentMemoryEpisode.group_id == group_id,
                    AgentMemoryEpisode.end_message_id == end_message_id,
                )
            )).scalar_one_or_none()
            episode_protected = await is_memory_protected(
                session, "episodes", "group", group_id=group_id,
                identity=str(end_message_id)
            )
            if episode is None and not episode_protected:
                episode = AgentMemoryEpisode(
                    group_id=group_id,
                    start_message_id=max(0, start_message_id),
                    end_message_id=end_message_id,
                )
                session.add(episode)
            if episode is not None and not episode_protected:
                episode.participant_ids = json.dumps(sorted(speakers), ensure_ascii=False)
                episode.summary = summary
                episode.updated_at = now

            touched_users: set[int] = set()
            for user_id, fact, category, importance, confidence, source_message_id, expires_days in accepted:
                touched_users.add(user_id)
                fingerprint = _fingerprint(fact)
                if await is_memory_protected(
                    session, "facts", "global", user_id=user_id, identity=fingerprint
                ) or await is_memory_protected(
                    session, "facts", "group", group_id=group_id,
                    user_id=user_id, identity=fingerprint
                ):
                    continue
                if category in _GLOBAL_FACT_CATEGORIES:
                    row = (await session.execute(
                        select(AgentGlobalPersonFact).where(
                            AgentGlobalPersonFact.user_id == user_id,
                            AgentGlobalPersonFact.fingerprint == fingerprint,
                        )
                    )).scalar_one_or_none()
                    expires_at = now + timedelta(days=expires_days) if expires_days else None
                    if row is None:
                        session.add(AgentGlobalPersonFact(
                            user_id=user_id,
                            fingerprint=fingerprint,
                            category=category,
                            fact=fact,
                            importance=importance,
                            confidence=confidence,
                            source_group_id=group_id,
                            source_message_id=source_message_id,
                            expires_at=expires_at,
                            status="active",
                        ))
                    else:
                        row.category = category
                        row.fact = fact
                        row.importance = max(int(row.importance or 1), importance)
                        row.confidence = max(float(row.confidence or 0.0), confidence)
                        row.source_group_id = group_id
                        row.source_message_id = source_message_id
                        row.expires_at = expires_at
                        row.status = "active"
                        row.updated_at = now
                    continue
                row = (await session.execute(
                    select(AgentPersonFact).where(
                        AgentPersonFact.group_id == group_id,
                        AgentPersonFact.user_id == user_id,
                        AgentPersonFact.fingerprint == fingerprint,
                    )
                )).scalar_one_or_none()
                expires_at = now + timedelta(days=expires_days) if expires_days else None
                if row is None:
                    session.add(AgentPersonFact(
                        group_id=group_id,
                        user_id=user_id,
                        fingerprint=fingerprint,
                        category=category,
                        fact=fact,
                        importance=importance,
                        confidence=confidence,
                        source_message_id=source_message_id,
                        expires_at=expires_at,
                        status="active",
                    ))
                    continue
                row.category = category
                row.fact = fact
                row.importance = max(int(row.importance or 1), importance)
                row.confidence = max(float(row.confidence or 0.0), confidence)
                row.source_message_id = source_message_id
                row.expires_at = expires_at
                row.status = "active"
                row.updated_at = now

            episodes = (await session.execute(
                select(AgentMemoryEpisode)
                .where(AgentMemoryEpisode.group_id == group_id)
                .order_by(AgentMemoryEpisode.id.desc())
            )).scalars().all()
            if len(episodes) > _EPISODE_LIMIT:
                await _delete_unprotected(session, AgentMemoryEpisode, episodes[_EPISODE_LIMIT:], "episodes", "group")

            for user_id in touched_users:
                facts = (await session.execute(
                    select(AgentPersonFact)
                    .where(
                        AgentPersonFact.group_id == group_id,
                        AgentPersonFact.user_id == user_id,
                    )
                    .order_by(
                        AgentPersonFact.importance.asc(),
                        AgentPersonFact.updated_at.asc(),
                    )
                )).scalars().all()
                if len(facts) > _FACT_LIMIT_PER_USER:
                    await _delete_unprotected(session, AgentPersonFact, facts[:-_FACT_LIMIT_PER_USER], "facts", "group")
                globals_rows = (await session.execute(
                    select(AgentGlobalPersonFact)
                    .where(AgentGlobalPersonFact.user_id == user_id)
                    .order_by(
                        AgentGlobalPersonFact.importance.asc(),
                        AgentGlobalPersonFact.updated_at.asc(),
                    )
                )).scalars().all()
                if len(globals_rows) > _FACT_LIMIT_PER_USER:
                    await _delete_unprotected(session, AgentGlobalPersonFact, globals_rows[:-_FACT_LIMIT_PER_USER], "facts", "global")
            await session.commit()
            logger.info(
                f"[agent_memory] writeback group={group_id} episode_end={end_message_id} "
                f"facts={len(accepted)}"
            )
        except Exception as exc:
            await session.rollback()
            logger.warning(f"[agent_memory] writeback failed group={group_id}: {type(exc).__name__}")
        finally:
            await session.close()

    @_serialized_memory_write
    async def record_batch_activity(
        self,
        group_id: int,
        items: Sequence[tuple[Any, Any, bool]],
        persona_id: str = "rin",
    ) -> None:
        """Record deterministic per-group interaction counters for a batch."""
        if not _ENABLED or not items:
            return
        group_id = int(group_id)
        events: dict[int, tuple[int, bool]] = {}
        for _bot, event, explicit in items:
            try:
                user_id = int(getattr(event, "user_id", 0) or 0)
                message_id = int(getattr(event, "message_id", 0) or 0)
            except (TypeError, ValueError):
                continue
            if user_id > 0 and message_id > 0:
                events[message_id] = (user_id, bool(explicit))
        if not events:
            return
        now = datetime.now()
        session = await get_session()
        try:
            user_ids = {user_id for user_id, _explicit in events.values()}
            existing = (await session.execute(
                select(AgentPersonaRelationshipState).where(
                    AgentPersonaRelationshipState.persona_id == str(persona_id),
                    AgentPersonaRelationshipState.group_id == group_id,
                    AgentPersonaRelationshipState.user_id.in_(user_ids),
                )
            )).scalars().all()
            states = {int(row.user_id): row for row in existing}
            for message_id, (user_id, explicit) in events.items():
                row = states.get(user_id)
                if row is None and await is_memory_protected(
                    session, "relationships", "group", group_id=group_id,
                    user_id=user_id, persona_id=str(persona_id)
                ):
                    continue
                if row is None:
                    row = AgentPersonaRelationshipState(
                        persona_id=str(persona_id), group_id=group_id, user_id=user_id
                    )
                    states[user_id] = row
                    session.add(row)
                if int(row.last_message_id or 0) == message_id:
                    continue
                row.message_count = int(row.message_count or 0) + 1
                row.last_message_id = message_id
                row.last_seen_at = now
                row.updated_at = now
                if explicit:
                    row.explicit_interaction_count = int(row.explicit_interaction_count or 0) + 1
                    row.last_interaction_at = now
            await session.commit()
        except Exception as exc:
            await session.rollback()
            logger.debug(f"[agent_memory] activity failed group={group_id}: {type(exc).__name__}")
        finally:
            await session.close()

    @_serialized_memory_write
    async def record_affinity_update(
        self,
        group_id: int,
        user_id: int,
        score: float,
        delta: float,
        reason: str,
        message_id: int,
        persona_id: str = "rin",
    ) -> None:
        """Mirror the active persona affinity result into the group relation state."""
        if not _ENABLED:
            return
        now = datetime.now()
        session = await get_session()
        try:
            row = (await session.execute(
                select(AgentPersonaRelationshipState).where(
                    AgentPersonaRelationshipState.persona_id == str(persona_id),
                    AgentPersonaRelationshipState.group_id == int(group_id),
                    AgentPersonaRelationshipState.user_id == int(user_id),
                )
            )).scalar_one_or_none()
            if row is None and await is_memory_protected(
                session, "relationships", "group", group_id=int(group_id),
                user_id=int(user_id), persona_id=str(persona_id)
            ):
                return
            if row is None:
                row = AgentPersonaRelationshipState(
                    persona_id=str(persona_id), group_id=int(group_id), user_id=int(user_id)
                )
                session.add(row)
            authoritative = (await session.execute(select(AgentPersonaAffinity).where(
                AgentPersonaAffinity.persona_id == str(persona_id), AgentPersonaAffinity.user_id == int(user_id)
            ))).scalar_one_or_none()
            if authoritative is not None:
                score, delta, reason = authoritative.affinity_score, authoritative.last_delta, authoritative.last_reason
            row.affinity_score = max(-100.0, min(100.0, float(score)))
            row.last_delta = max(-2.0, min(2.0, float(delta)))
            row.last_reason = _clean_text(reason, 300)
            row.last_message_id = max(int(row.last_message_id or 0), int(message_id or 0))
            row.last_interaction_at = now
            row.updated_at = now
            await session.commit()
        except Exception as exc:
            await session.rollback()
            logger.debug(f"[agent_memory] affinity state failed group={group_id}: {type(exc).__name__}")
        finally:
            await session.close()

    async def render_recall(
        self,
        group_id: int,
        events: Sequence[Any],
        *,
        include_episodes: bool,
    ) -> str:
        """Return a tiny, relevant memory block for the active batch only."""
        if not _ENABLED:
            return ""
        user_ids: list[int] = []
        for event in events:
            try:
                user_id = int(getattr(event, "user_id", 0) or 0)
            except (TypeError, ValueError):
                continue
            if user_id > 0 and user_id not in user_ids:
                user_ids.append(user_id)
        if not user_ids:
            return ""
        now = datetime.now()
        session = await get_session()
        try:
            facts = (await session.execute(
                select(AgentPersonFact)
                .where(
                    AgentPersonFact.group_id == int(group_id),
                    AgentPersonFact.user_id.in_(user_ids),
                    AgentPersonFact.status == "active",
                    or_(
                        AgentPersonFact.expires_at.is_(None),
                        AgentPersonFact.expires_at > now,
                    ),
                )
                .order_by(AgentPersonFact.importance.desc(), AgentPersonFact.updated_at.desc())
                .limit(_FACT_RECALL_LIMIT)
            )).scalars().all()
            facts = [row for row in facts if not await is_memory_protected(
                session, "facts", "global", user_id=int(row.user_id), identity=str(row.fingerprint)
            )]
            global_facts = (await session.execute(
                select(AgentGlobalPersonFact)
                .where(
                    AgentGlobalPersonFact.user_id.in_(user_ids),
                    AgentGlobalPersonFact.status == "active",
                    or_(
                        AgentGlobalPersonFact.expires_at.is_(None),
                        AgentGlobalPersonFact.expires_at > now,
                    ),
                )
                .order_by(
                    AgentGlobalPersonFact.importance.desc(),
                    AgentGlobalPersonFact.updated_at.desc(),
                )
                .limit(_FACT_RECALL_LIMIT)
            )).scalars().all()
            persona_id = _current_persona_id()
            relations = (await session.execute(
                select(AgentPersonaRelationshipState).where(
                    AgentPersonaRelationshipState.persona_id == persona_id,
                    AgentPersonaRelationshipState.group_id == int(group_id),
                    AgentPersonaRelationshipState.user_id.in_(user_ids),
                )
            )).scalars().all()
            affinity_rows = (await session.execute(
                select(AgentPersonaAffinity).where(
                    AgentPersonaAffinity.persona_id == persona_id,
                    AgentPersonaAffinity.user_id.in_(user_ids),
                )
            )).scalars().all()
            affinity_by_user = {
                int(row.user_id): float(row.affinity_score or 0.0)
                for row in affinity_rows
            }
            if persona_id == "rin":
                legacy_affinities = (await session.execute(
                    select(UserAffinity).where(UserAffinity.user_id.in_(user_ids))
                )).scalars().all()
                for row in legacy_affinities:
                    affinity_by_user.setdefault(int(row.user_id), float(row.affinity_score or 0.0))
            if not relations and persona_id == "rin":
                relations = (await session.execute(
                    select(AgentRelationshipState).where(
                        AgentRelationshipState.group_id == int(group_id),
                        AgentRelationshipState.user_id.in_(user_ids),
                    )
                )).scalars().all()
                relations = [row for row in relations if not await is_memory_protected(
                    session, "relationships", "group", group_id=int(group_id),
                    user_id=int(row.user_id), persona_id="rin"
                )]
            episodes: list[AgentMemoryEpisode] = []
            if include_episodes and _EPISODE_RECALL_LIMIT:
                episodes = (await session.execute(
                    select(AgentMemoryEpisode)
                    .where(AgentMemoryEpisode.group_id == int(group_id))
                    .order_by(AgentMemoryEpisode.updated_at.desc())
                    .limit(_EPISODE_RECALL_LIMIT)
                )).scalars().all()
        except Exception as exc:
            logger.debug(f"[agent_memory] recall failed group={group_id}: {type(exc).__name__}")
            return ""
        finally:
            await session.close()

        lines: list[str] = []
        recalled_facts = _unique_recalled_facts([*global_facts, *facts], _FACT_RECALL_LIMIT)
        if recalled_facts:
            lines.append("[与当前发言者相关的长期事实，仅在确有帮助时使用，不要生硬复述]")
            for row in recalled_facts:
                scope = "global:" if isinstance(row, AgentGlobalPersonFact) else ""
                lines.append(f"QQ:{int(row.user_id)} [{scope}{row.category}] {row.fact}")
        if relations:
            lines.append("[当前群互动状态，仅作语气与连续性参考，不要向用户公开这些数值]")
            for row in relations:
                details = f"QQ:{int(row.user_id)} 已记录发言{int(row.message_count or 0)}次"
                if int(row.explicit_interaction_count or 0):
                    details += f"，明确互动{int(row.explicit_interaction_count)}次"
                score = affinity_by_user.get(int(row.user_id), float(row.affinity_score or 0.0))
                details += f"，关系阶段{relationship_stage(score)}（好感{score:.1f}）"
                if float(row.last_delta or 0):
                    details += f"，最近好感变化{float(row.last_delta):+.1f}"
                lines.append(details)
        if episodes:
            lines.append("[较早群聊事件摘要，仅在当前话题涉及过去内容时参考]")
            for row in reversed(episodes):
                timestamp = row.updated_at.strftime("%Y-%m-%d %H:%M") if row.updated_at else "未知时间"
                lines.append(f"[{timestamp}] {_clean_text(row.summary, 700)}")
        return "\n".join(lines)


MEMORY = AgentMemoryStore()
