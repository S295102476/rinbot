"""Thin integration between live bot events and the administration services."""

from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from collections import OrderedDict
from contextlib import contextmanager, asynccontextmanager
from contextvars import ContextVar
from typing import Any

from nonebot.log import logger

from .console_state import SETTINGS, STATS, QUOTA, initialize_console


SEND_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar("console_send_context", default=None)
_SEEN: OrderedDict[str, float] = OrderedDict()
_QUEUE: asyncio.Queue | None = None
_WORKER: asyncio.Task | None = None
_ERROR = ""
_DROPPED = 0
_MAINTENANCE: asyncio.Task | None = None
_INITIALIZATION_FAILED = False


class ChatBlocked(PermissionError):
    pass


def ready() -> bool:
    return SETTINGS.loaded


def global_value(name: str, default: Any = None) -> Any:
    return SETTINGS.global_values().get(name, default) if ready() else default


def group_enabled(group_id: int, fallback: bool = False) -> bool:
    if _INITIALIZATION_FAILED:
        return False
    if not ready():
        return fallback
    return bool(global_value("agent_enabled", False) and SETTINGS.group(group_id)["enabled"])


def group_at_only(group_id: int) -> bool:
    return ready() and (
        global_value("development_mode") == "at" or SETTINGS.group(group_id)["mode"] == "at"
    )


def scope_allows(group_id: int, *, direct_at: bool = False, fallback: bool = True) -> bool:
    if not ready():
        return fallback
    if not group_enabled(group_id):
        return False
    mode = global_value("development_mode", "all")
    if mode == "test" and group_id not in global_value("test_groups", []):
        return False
    return not group_at_only(group_id) or direct_at


def real_at(bot: Any, event: Any) -> bool:
    bot_id = str(getattr(bot, "self_id", "") or "")
    if not bot_id:
        return False
    for segment in getattr(event, "message", ()):
        if getattr(segment, "type", "") == "at" and str(segment.data.get("qq", "")) == bot_id:
            return True
    return bool(re.search(rf"\[(?:CQ:)?at[:,]qq={re.escape(bot_id)}(?:[,\]])", str(getattr(event, "raw_message", ""))))


def explicit_interaction(bot: Any, event: Any) -> bool:
    if real_at(bot, event) or bool(getattr(event, "to_me", False)):
        return True
    sender = getattr(getattr(event, "reply", None), "sender", None)
    reply_user = sender.get("user_id", 0) if isinstance(sender, dict) else getattr(sender, "user_id", 0)
    if reply_user and str(reply_user) == str(getattr(bot, "self_id", "")):
        return True
    from .persona_manager import get_active_persona_name

    names = ["rin", "凛", "远坂凛", "艾蕾", "伊什塔尔", get_active_persona_name()]
    getter = getattr(event, "get_plaintext", None)
    text = getter() if callable(getter) else str(getattr(event, "raw_message", ""))
    return bool(re.match(r"^(?:" + "|".join(re.escape(name) for name in names) + r")(?:\s|[，,。！？!?：:]|$)", text.strip(), re.I))


def record(kind: str, group_id: int, user_id: int = 0, event_key: str | None = None, **metadata) -> None:
    """Bounded, non-blocking telemetry. Never carry message bodies in this queue."""
    global _QUEUE, _WORKER, _DROPPED, _ERROR
    if not ready():
        return
    if _QUEUE is None:
        _QUEUE = asyncio.Queue(maxsize=5000)
    try:
        _QUEUE.put_nowait((kind, int(group_id), int(user_id), event_key, metadata))
        if _WORKER is None or _WORKER.done():
            _WORKER = asyncio.create_task(_drain())
    except asyncio.QueueFull:
        _DROPPED += 1
        _ERROR = "telemetry_queue_full"
        logger.warning("[console_stats] queue full; statistics are incomplete")


async def _drain() -> None:
    global _ERROR, _DROPPED
    while _QUEUE is not None and not _QUEUE.empty():
        kind, group_id, user_id, event_key, metadata = await _QUEUE.get()
        try:
            for attempt in range(3):
                try:
                    await asyncio.wait_for(STATS.record_event(kind, group_id, user_id, event_key, **metadata), 10)
                    break
                except Exception as exc:
                    _ERROR = type(exc).__name__
                    if attempt == 2:
                        _DROPPED += 1
                        logger.warning(f"[console_stats] write failed kind={kind} error={_ERROR}")
                    else:
                        await asyncio.sleep(attempt + 1)
        finally:
            _QUEUE.task_done()


def telemetry_health() -> dict:
    return {"ready": ready(), "pending": _QUEUE.qsize() if _QUEUE else 0, "dropped": _DROPPED, "error": _ERROR or None}


def settings_changed() -> None:
    """Invalidate queued chat only. Independent matchers are deliberately untouched."""
    module = sys.modules.get("plugins.agent_runtime")
    runtime = getattr(module, "RUNTIME", None)
    if runtime is None:
        return
    for group_id, items in list(runtime._batch_queues.items()):
        runtime._batch_queues[group_id] = [item for item in items
            if scope_allows(group_id, direct_at=real_at(item[0], item[1]))]
        if not scope_allows(group_id, direct_at=True):
            task = runtime._batch_tasks.get(group_id)
            if task is not None and not task.done():
                task.cancel()
    for group_id, task in list(runtime._summary_tasks.items()):
        if not group_enabled(group_id) and not task.done():
            task.cancel()


def observe_incoming(bot: Any, event: Any) -> None:
    """Called synchronously before every local rejecting preprocessor can await."""
    from nonebot.adapters.onebot.v11 import GroupMessageEvent

    if not ready() or not isinstance(event, GroupMessageEvent):
        return
    gid, uid = int(event.group_id), int(event.user_id)
    if not group_enabled(gid) or str(uid) == str(bot.self_id):
        return
    key = f"{bot.self_id}:{gid}:{event.message_id}"
    if key in _SEEN:
        return
    _SEEN[key] = time.monotonic()
    while len(_SEEN) > 20000:
        _SEEN.popitem(last=False)
    metadata = {"bot_id": int(bot.self_id), "message_id": int(event.message_id)}
    record("incoming", gid, uid, key, **metadata)
    direct = real_at(bot, event)
    explicit = explicit_interaction(bot, event)
    if direct:
        record("direct_at", gid, uid, key, **metadata)
    if explicit and not direct:
        record("interaction", gid, uid, key, **metadata)


@contextmanager
def sending(group_id: int, user_id: int = 0, source: str = "chat", explicit: bool = False):
    state = {"group_id": int(group_id), "user_id": int(user_id), "source": source, "explicit": explicit, "sent": 0, "unknown": False}
    token = SEND_CONTEXT.set(state)
    try:
        yield state
    finally:
        SEND_CONTEXT.reset(token)


async def quota_status(group_id: int) -> dict:
    if not ready():
        return {"enforced": False, "stage": "normal", "count": 0, "hour_count": 0, "limit": 0, "hard_limit": 0}
    return await QUOTA.status(group_id)


async def check_agent_action(group_id: int, *, direct_at: bool = False, proactive: bool = False) -> None:
    if _INITIALIZATION_FAILED:
        raise ChatBlocked("Console persistence unavailable")
    if not ready():
        return
    if not scope_allows(group_id, direct_at=direct_at):
        raise ChatBlocked("Agent disabled for this group or trigger")
    status = await quota_status(group_id)
    if status["enforced"] and (status["stage"] == "hard_limit" or (proactive and status["stage"] == "at_only")):
        raise ChatBlocked("Daily Agent quota reached")


@asynccontextmanager
async def chat_turn(bot: Any, group_id: int, user_id: int, source_message_id: int = 0,
                    *, explicit: bool = False, direct_at: bool = False, source: str = "chat"):
    """Reserve just before sending; count partial success once, even on cancellation."""
    await check_agent_action(group_id, direct_at=direct_at, proactive=not explicit)
    claim = await QUOTA.reserve(group_id, explicit) if ready() else None
    if ready() and claim is None:
        raise ChatBlocked("Daily reply quota reached")
    with sending(group_id, user_id, source, explicit) as state:
        state["direct_at"] = direct_at
        state["claim"] = claim
        try:
            yield state
        finally:
            if claim:
                if state["unknown"] and not state["sent"]:
                    await asyncio.shield(QUOTA.uncertain(claim))
                else:
                    await asyncio.shield(QUOTA.finish(
                        claim, state["sent"] > 0, user_id, source_message_id, state["sent"],
                    ))
    try:
        await notify_quota(bot, group_id)
    except Exception as exc:
        logger.warning(f"[console_quota] notice failed group={group_id} error={type(exc).__name__}")


async def notify_quota(bot: Any, group_id: int) -> None:
    if not ready() or not scope_allows(group_id, direct_at=True):
        return
    notice = await QUOTA.claim_notice(group_id)
    if not notice:
        return
    text = (
        f"本群今日已回复 {notice['count']} 轮；{notice['limit']} 轮后停止主动发言，"
        f"{notice['hard_limit']} 轮后暂停聊天，功能指令不受影响。"
    )
    try:
        with sending(group_id, source="notice", explicit=True):
            await bot.send_group_msg(group_id=group_id, message=text)
    except Exception:
        await QUOTA.finish_notice(notice, False)
        raise
    else:
        await QUOTA.finish_notice(notice, True)


async def flush() -> None:
    if _MAINTENANCE is not None:
        _MAINTENANCE.cancel()
    if _QUEUE is not None:
        await asyncio.wait_for(_QUEUE.join(), 5)


async def _startup() -> None:
    global _MAINTENANCE, _ERROR, _INITIALIZATION_FAILED
    from nonebot import get_driver
    enabled = os.getenv("AGENT_CONSOLE_RUNTIME_ENABLED", str(getattr(get_driver().config, "agent_console_runtime_enabled", "true")))
    if enabled.lower() in {"false", "0", "off"}:
        logger.warning("[console] runtime integration disabled by server configuration")
        return
    try:
        await initialize_console()
    except Exception as exc:
        _INITIALIZATION_FAILED = True
        _ERROR = f"startup:{type(exc).__name__}"
        logger.error(f"[console] initialization failed: {type(exc).__name__}; console unavailable")
        return
    async def maintain():
        global _ERROR
        ticks = 0
        while True:
            await asyncio.sleep(60)
            try:
                await STATS.health(heartbeat=True, error=_ERROR, dropped=_DROPPED)
                ticks += 1
                if ticks % 60 == 0:
                    await STATS.prune()
            except Exception as exc:
                _ERROR = f"maintenance:{type(exc).__name__}"
                logger.warning(f"[console] maintenance failed: {type(exc).__name__}")
    _MAINTENANCE = asyncio.create_task(maintain())


def _install_hooks() -> None:
    from nonebot import get_driver
    from nonebot.adapters.onebot.v11 import Bot
    from nonebot.message import event_preprocessor
    from nonebot.exception import MockApiException

    @event_preprocessor
    async def _observe(bot, event):
        observe_incoming(bot, event)

    @Bot.on_calling_api
    async def _before_send(bot, api, data):
        context = SEND_CONTEXT.get()
        if not context or context["source"] == "notice" or api not in {"send_group_msg", "send_msg", "send_group_forward_msg", "set_group_ban"}:
            return
        if int(data.get("group_id") or 0) != context["group_id"]:
            return
        try:
            await check_agent_action(context["group_id"], direct_at=context.get("direct_at", False),
                                     proactive=context["source"] in {"chat", "followup"} and not context["explicit"])
        except Exception:
            context["blocked"] = True
            raise MockApiException(result={"_console_blocked": True})

    @Bot.on_called_api
    async def _after_send(bot, exception, api, data, result):
        context = SEND_CONTEXT.get()
        if api not in {"send_group_msg", "send_msg", "send_group_forward_msg"}:
            return
        if context is None:
            from nonebot.matcher import current_event
            event = current_event.get(None)
            gid = int(data.get("group_id") or 0)
            if not gid or not group_enabled(gid) or exception is not None:
                return
            message_id = result.get("message_id") if isinstance(result, dict) else None
            if message_id is not None:
                record("outbound", gid, int(getattr(event, "user_id", 0)), str(message_id), source="command", bot_id=int(bot.self_id))
            return
        if int(data.get("group_id") or 0) != context["group_id"]:
            return
        if isinstance(result, dict) and result.get("_console_blocked"):
            return
        if exception is not None:
            context["unknown"] |= type(exception).__name__ not in {"ActionFailed", "MockApiException"}
            return
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if message_id is None:
            context["unknown"] = True
            return
        context["sent"] += 1
        record("outbound", context["group_id"], context["user_id"],
               str(message_id) if message_id is not None else None,
               source=context["source"], bot_id=int(bot.self_id))

    driver = get_driver()
    driver.on_startup(_startup)
    driver.on_shutdown(flush)


try:
    _install_hooks()
except ValueError:
    pass
