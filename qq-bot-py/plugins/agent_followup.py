"""Persisted, rate-limited follow-ups for questions asked by the group Agent."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from typing import Any

import yaml
from nonebot import get_driver, require
from nonebot.adapters.onebot.v11 import Bot, Message, MessageSegment
from nonebot.log import logger
from sqlalchemy import select

from .db import AgentFollowup, Base, engine, get_session
from . import console_runtime as CONSOLE

require("nonebot_plugin_apscheduler")
from nonebot_plugin_apscheduler import scheduler


def _load_config() -> dict[str, Any]:
    try:
        with open("config.yaml", "r", encoding="utf-8") as handle:
            return yaml.safe_load(handle) or {}
    except Exception:
        return {}


_CONFIG = _load_config()
_AGENT_CONFIG = _CONFIG.get("agent") or {}
_GROUP_CONFIG = _AGENT_CONFIG.get("group") or {}
_FOLLOWUP_CONFIG = _GROUP_CONFIG.get("follow_up") or {}
ENABLED = bool(_FOLLOWUP_CONFIG.get("enabled", False))
OFFSETS = tuple(
    max(30, int(value))
    for value in (_FOLLOWUP_CONFIG.get("schedule_seconds") or [120, 600, 1800])
)
MAX_PER_HOUR = max(1, int(_FOLLOWUP_CONFIG.get("max_per_group_per_hour", 6)))
_SEND_TIMES: dict[int, list[float]] = {}
_TICK_LOCK = asyncio.Lock()


def _active_group(group_id: int) -> bool:
    if CONSOLE.ready():
        return ENABLED and CONSOLE.scope_allows(group_id)
    if not ENABLED or not bool(_AGENT_CONFIG.get("enabled", False)):
        return False
    return int(group_id) in {
        int(value) for value in (_AGENT_CONFIG.get("active_groups") or [])
    }


def _within_group_rate(group_id: int) -> bool:
    now = time.monotonic()
    history = [stamp for stamp in _SEND_TIMES.get(int(group_id), []) if now - stamp < 3600]
    if len(history) >= MAX_PER_HOUR:
        _SEND_TIMES[int(group_id)] = history
        return False
    history.append(now)
    _SEND_TIMES[int(group_id)] = history
    return True


async def schedule_followup(
    *,
    group_id: int,
    user_id: int,
    source_message_id: int,
    question: str,
) -> None:
    """Create or replace one pending question for a user in a group."""
    if not _active_group(group_id) or not question.strip() or not OFFSETS:
        return
    now = datetime.now()
    session = await get_session()
    try:
        existing = (await session.execute(
            select(AgentFollowup).where(
                AgentFollowup.group_id == int(group_id),
                AgentFollowup.user_id == int(user_id),
                AgentFollowup.status == "pending",
            )
        )).scalars().first()
        if existing is not None:
            existing.status = "replaced"
            existing.updated_at = now
        session.add(AgentFollowup(
            group_id=int(group_id),
            user_id=int(user_id),
            source_message_id=int(source_message_id),
            question=question.strip()[:500],
            status="pending",
            stage=0,
            next_due_at=now + timedelta(seconds=OFFSETS[0]),
            created_at=now,
            updated_at=now,
        ))
        await session.commit()
        logger.info(
            f"[agent_followup] created group={group_id} user={user_id} "
            f"schedule={list(OFFSETS)}"
        )
    finally:
        await session.close()


async def _check_one(bot: Bot, item: AgentFollowup) -> None:
    from .agent_runtime import RUNTIME, _recent_group_context

    try:
        await CONSOLE.check_agent_action(item.group_id, proactive=True)
        context = await _recent_group_context(item.group_id, RUNTIME.policy.max_context_messages)
        request = (
            "这是一次延迟跟进检查。你之前向用户提出了下面的问题：\n"
            f"{item.question}\n\n"
            f"目标用户 QQ：{item.user_id}\n"
            "请根据最新群聊上下文判断目标用户是否已经回答。"
            "如果已经回答，输出 action=ignore，并附 follow_up_status=answered；"
            "如果没有回答且仍值得提醒，输出 action=reply，只给出一条简短中文提醒，"
            "不要自行添加@，系统会自动@目标用户；"
            "如果不应继续打扰，输出 action=ignore。"
        )
        parts, decision = await RUNTIME._decide(
            scope="group",
            request=request,
            context=context,
            force_reply=False,
            user_id=item.user_id,
            group_id=item.group_id,
            bot=bot,
        )
    except Exception as exc:
        logger.warning(
            f"[agent_followup] decision failed group={item.group_id} user={item.user_id}: "
            f"{type(exc).__name__}"
        )
        return

    now = datetime.now()
    session = await get_session()
    try:
        current = await session.get(AgentFollowup, item.id)
        if current is None or current.status != "pending":
            return
        status_hint = str(decision.get("follow_up_status") or "").lower()
        if status_hint == "answered" or not parts:
            current.status = "resolved" if status_hint == "answered" else "expired"
            current.last_checked_at = now
            current.updated_at = now
            await session.commit()
            return
        if not _within_group_rate(current.group_id):
            current.status = "skipped"
            current.updated_at = now
            await session.commit()
            logger.warning(f"[agent_followup] rate limited group={current.group_id}")
            return

        text = str(parts[0]).strip()[:240]
        async with CONSOLE.chat_turn(bot, current.group_id, current.user_id, current.source_message_id, source="followup"):
            await bot.send_group_msg(
                group_id=int(current.group_id),
                message=Message(MessageSegment.at(int(current.user_id))) + f" {text}",
            )
        try:
            from .agent_metrics import record_agent_messages

            await record_agent_messages(current.group_id, 1)
        except Exception as exc:
            logger.debug(f"[agent_metrics] followup message record failed: {type(exc).__name__}")
        try:
            from .ai_chat import record_bot_reply

            asyncio.create_task(record_bot_reply(current.group_id, text))
        except Exception:
            pass
        current.stage += 1
        current.last_checked_at = now
        current.updated_at = now
        if current.stage >= len(OFFSETS):
            current.status = "expired"
        else:
            current.next_due_at = current.created_at + timedelta(seconds=OFFSETS[current.stage])
        await session.commit()
        logger.info(
            f"[agent_followup] sent group={current.group_id} user={current.user_id} "
            f"stage={current.stage} status={current.status}"
        )
    finally:
        await session.close()


async def _tick(bot: Bot) -> None:
    if not ENABLED:
        return
    async with _TICK_LOCK:
        session = await get_session()
        try:
            rows = (await session.execute(
                select(AgentFollowup)
                .where(
                    AgentFollowup.status == "pending",
                    AgentFollowup.next_due_at <= datetime.now(),
                )
                .order_by(AgentFollowup.next_due_at.asc())
                .limit(20)
            )).scalars().all()
        finally:
            await session.close()
        for item in rows:
            if _active_group(item.group_id):
                await _check_one(bot, item)
            else:
                session = await get_session()
                try:
                    current = await session.get(AgentFollowup, item.id)
                    if current is not None and current.status == "pending":
                        current.status = "skipped"
                        current.updated_at = datetime.now()
                        await session.commit()
                finally:
                    await session.close()


@get_driver().on_startup
async def _startup() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    if ENABLED:
        logger.info(
            f"[agent_followup] enabled schedule={list(OFFSETS)} "
            f"max_per_group_per_hour={MAX_PER_HOUR}"
        )


@scheduler.scheduled_job("interval", seconds=30, id="agent_followup_tick")
async def _scheduled_followup() -> None:
    if not ENABLED:
        return
    from nonebot import get_bot

    try:
        bot = get_bot()
    except Exception:
        return
    await _tick(bot)
