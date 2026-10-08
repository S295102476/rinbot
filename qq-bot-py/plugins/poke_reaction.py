"""将“戳机器人”的 OneBot V11 群聊事件交给 Agent 判断。

只有 target_id 等于机器人自身、且群在 agent.active_groups 中时才会入队；
戳其他成员不会进入 Agent。dev_scope 只负责开发期间的群范围过滤。
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace

from nonebot import on_notice
from nonebot.adapters.onebot.v11 import (
    Bot,
    Message,
    MessageSegment,
    PokeNotifyEvent,
)
from nonebot.log import logger
from nonebot.rule import Rule


_POKE_SEQUENCE = itertools.count()


def _synthetic_message_id() -> int:
    """Poke notices have no message_id; create a positive, time-sortable ID."""
    return int(time.time_ns() // 1_000_000) * 100 + next(_POKE_SEQUENCE) % 100


@dataclass
class PokeAgentEvent:
    """Small GroupMessageEvent-compatible view consumed by AgentRuntime."""

    group_id: int
    user_id: int
    message_id: int
    nickname: str
    created_at: datetime
    message: Message
    raw_message: str
    sender: object
    reply: object = None
    to_me: bool = False
    is_poke: bool = True

    def get_plaintext(self) -> str:
        return "戳了戳你"


async def _poke_agent_rule(bot: Bot, event: PokeNotifyEvent) -> bool:
    if not isinstance(event, PokeNotifyEvent):
        return False
    if event.group_id is None or int(event.target_id) != int(bot.self_id):
        return False
    try:
        from .agent_runtime import agent_group_enabled

        return agent_group_enabled(int(event.group_id))
    except Exception:
        return False


poke_notice = on_notice(rule=Rule(_poke_agent_rule), priority=0, block=False)


@poke_notice.handle()
async def handle_poke(bot: Bot, event: PokeNotifyEvent):
    if not isinstance(event, PokeNotifyEvent):
        return
    group_id = event.group_id
    if group_id is None or int(event.target_id) != int(bot.self_id):
        return

    try:
        from .agent_runtime import agent_group_enabled, enqueue_group_event

        if not agent_group_enabled(int(group_id)):
            logger.debug(
                f"[router] group={int(group_id)} route=ignored reason=poke_non_agent"
            )
            return
    except Exception as exc:
        logger.debug(f"[poke] route check failed: {type(exc).__name__}")
        return

    user_id = int(event.user_id)
    try:
        member = await bot.get_group_member_info(group_id=int(group_id), user_id=user_id)
        nickname = member.get("card") or member.get("nickname") or str(user_id)
    except Exception:
        nickname = str(user_id)

    created_at = datetime.fromtimestamp(int(event.time or time.time()))
    poke_event = PokeAgentEvent(
        group_id=int(group_id),
        user_id=user_id,
        message_id=_synthetic_message_id(),
        nickname=str(nickname),
        created_at=created_at,
        message=Message([MessageSegment.text("戳了戳你")]),
        raw_message="[poke]",
        sender=SimpleNamespace(
            user_id=user_id,
            card=str(nickname),
            nickname=str(nickname),
        ),
    )

    # Pokes are lightweight context events: keep them in the same rolling
    # cache/DB as messages, but do not mark them as an explicit @ trigger.
    try:
        from .agent_context import CACHE, CachedMessage
        from .db import GroupMessage, get_session, prune_group_messages

        session = await get_session()
        try:
            row = GroupMessage(
                group_id=int(group_id),
                message_id=int(poke_event.message_id),
                user_id=user_id,
                nickname=str(nickname),
                content="[戳一戳] 用户戳了戳你（QQ问候）",
                raw_message="[poke]",
                at_users="[]",
                has_image=False,
                image_url="",
                is_bot=False,
                created_at=created_at,
            )
            session.add(row)
            await session.flush()
            await prune_group_messages(int(group_id), session=session)
            await session.commit()
        finally:
            await session.close()
        cached_record = CachedMessage(
            group_id=int(group_id),
            message_id=int(poke_event.message_id),
            user_id=user_id,
            nickname=str(nickname),
            content="[戳一戳] 用户戳了戳你（QQ问候）",
            raw_message="[poke]",
            at_users=(),
            has_image=False,
            created_at=created_at,
        )
        CACHE.add(cached_record)
        # A poke is not spoken cross-group content; keep the owner's
        # cross-group recall limited to actual messages.
        CACHE.prefetch_avatar(user_id)
    except Exception as exc:
        logger.warning(f"[poke] context persist failed group={int(group_id)}: {type(exc).__name__}")

    await enqueue_group_event(bot, poke_event, force_reply=False)
    logger.info(
        f"[router] group={int(group_id)} route=agent_batch poke=1 "
        f"user={user_id} force=False"
    )
