"""Persistent, low-cost counters for messages actually sent by the Agent."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

import yaml

from nonebot.log import logger


def _load_config() -> dict:
    for path in (Path("config.yaml"), Path(__file__).resolve().parents[1] / "config.yaml"):
        try:
            return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
    return {}


_CONFIG = _load_config()
_LOCAL: defaultdict[int, deque[float]] = defaultdict(deque)
_LOCAL_LOCK = asyncio.Lock()

try:
    import redis as redis_lib

    _REDIS_CONFIG = _CONFIG.get("redis") or {}
    _REDIS = redis_lib.Redis(
        host=_REDIS_CONFIG.get("host", "127.0.0.1"),
        port=int(_REDIS_CONFIG.get("port", 6379)),
        decode_responses=True,
    )
except Exception:
    _REDIS = None


def _key(group_id: int) -> str:
    return f"agent:sent:{int(group_id)}"


def _trim_local(values: deque[float], now: float) -> None:
    cutoff = now - 86400
    while values and values[0] < cutoff:
        values.popleft()


async def record_agent_messages(group_id: int, count: int = 1) -> None:
    """Record successful outgoing Agent messages, not model decisions."""
    count = max(0, int(count))
    if count <= 0:
        return
    now = time.time()
    if _REDIS is not None:
        try:
            mapping = {f"live:{now:.6f}:{uuid.uuid4().hex}:{index}": now for index in range(count)}
            await asyncio.to_thread(_REDIS.zadd, _key(group_id), mapping)
            await asyncio.to_thread(_REDIS.zremrangebyscore, _key(group_id), 0, now - 86400)
            await asyncio.to_thread(_REDIS.expire, _key(group_id), 3 * 86400)
            return
        except Exception as exc:
            logger.debug(f"[agent_metrics] Redis record failed: {type(exc).__name__}")
    async with _LOCAL_LOCK:
        values = _LOCAL[int(group_id)]
        _trim_local(values, now)
        for _ in range(count):
            values.append(now)


async def get_agent_message_counts(group_id: int) -> tuple[int, int]:
    """Return (rolling 60-minute, rolling 24-hour) outgoing counts."""
    now = time.time()
    hour_start = now - 3600
    day_start = now - 86400
    if _REDIS is not None:
        try:
            key = _key(group_id)
            await asyncio.to_thread(_REDIS.zremrangebyscore, key, 0, day_start)
            hour_count = await asyncio.to_thread(_REDIS.zcount, key, hour_start, "+inf")
            day_count = await asyncio.to_thread(_REDIS.zcount, key, day_start, "+inf")
            return int(hour_count), int(day_count)
        except Exception as exc:
            logger.debug(f"[agent_metrics] Redis count failed: {type(exc).__name__}")
    async with _LOCAL_LOCK:
        values = _LOCAL[int(group_id)]
        _trim_local(values, now)
        return sum(value >= hour_start for value in values), len(values)


async def warm_agent_message_stats(group_ids: Iterable[int]) -> None:
    """Seed Redis on first deployment from persisted bot rows.

    Existing Redis data is left untouched so restarts do not double-count rows.
    """
    if _REDIS is None:
        return
    from sqlalchemy import select

    from .db import GroupMessage, get_session

    session = await get_session()
    try:
        cutoff = datetime.now() - timedelta(days=1)
        rows = (await session.execute(
            select(GroupMessage.id, GroupMessage.group_id, GroupMessage.created_at)
            .where(
                GroupMessage.is_bot == True,
                GroupMessage.group_id.in_([int(value) for value in group_ids]),
                GroupMessage.created_at >= cutoff,
            )
        )).all()
    finally:
        await session.close()
    grouped: defaultdict[int, list[tuple[str, float]]] = defaultdict(list)
    for row_id, group_id, created_at in rows:
        if created_at is None:
            continue
        grouped[int(group_id)].append((f"db:{row_id}", created_at.timestamp()))
    for group_id, entries in grouped.items():
        key = _key(group_id)
        try:
            existing = await asyncio.to_thread(_REDIS.zcard, key)
            if int(existing) == 0:
                await asyncio.to_thread(_REDIS.zadd, key, dict(entries))
                await asyncio.to_thread(_REDIS.expire, key, 3 * 86400)
        except Exception as exc:
            logger.debug(f"[agent_metrics] Redis warm failed group={group_id}: {type(exc).__name__}")
