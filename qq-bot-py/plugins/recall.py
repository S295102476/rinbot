"""引用撤回插件。

用法：引用一条消息，@机器人并发送“撤回”。
机器人自己的消息可直接撤回；其他消息由 OneBot 按机器人当前群权限处理。
"""

from __future__ import annotations

import re

from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot.log import logger
from nonebot.rule import Rule

from .chat_coordination import resolve_reply_context


_RAW_AT_RE = re.compile(
    r"\[(?:CQ:)?at[:,]qq=([^,\]]+)(?:,[^\]]*)?\]",
    flags=re.IGNORECASE,
)


def _mentions_bot(event: GroupMessageEvent, bot: Bot) -> bool:
    bot_id = str(bot.self_id)
    if any(
        segment.type == "at" and str(segment.data.get("qq", "")) == bot_id
        for segment in event.message
    ):
        return True

    raw = getattr(event, "raw_message", "") or ""
    return any(match.group(1).strip() == bot_id for match in _RAW_AT_RE.finditer(raw))


def _recall_rule() -> Rule:
    async def _rule(bot: Bot, event: GroupMessageEvent) -> bool:
        if not _mentions_bot(event, bot):
            return False
        # get_plaintext() 不包含 at/reply 段，只保留用户当前输入。
        return (event.get_plaintext() or "").strip() == "撤回"

    return Rule(_rule)


# Run before the Agent group's priority-0 listeners.  A matched recall request
# must be consumed before it can be queued as an Agent conversation turn.
recall_cmd = on_message(rule=_recall_rule(), priority=-1, block=True)


@recall_cmd.handle()
async def handle_recall(bot: Bot, event: GroupMessageEvent):
    context = await resolve_reply_context(bot, event)
    message_id = context.message_id if context else ""
    if not message_id:
        # 只在无法取得引用 ID 时静默结束，避免误删当前“撤回”消息。
        logger.debug("[recall] 未找到被引用消息 ID，忽略")
        return

    try:
        await bot.call_api("delete_msg", message_id=int(message_id))
        sender = context.sender_name if context else "未知用户"
        logger.info(f"[recall] 已撤回消息 id={message_id} sender={sender}")
    except Exception as exc:
        # 对方消息需要机器人拥有群管理权限；无权限、消息过期等情况均静默。
        logger.debug(
            f"[recall] 撤回失败，保持静默 id={message_id} "
            f"error={type(exc).__name__}: {exc}"
        )
