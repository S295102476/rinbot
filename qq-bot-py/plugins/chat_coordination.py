"""Shared helpers for group reply context and ordered batch delivery."""

from __future__ import annotations

import asyncio
import random
import re
import time
from dataclasses import dataclass

from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageSegment


# A reply is considered part of the same burst when it starts within this
# window.  The value is deliberately longer than the three-second split delay
# so a second answer remains anchored even while the first one is being sent.
REPLY_BURST_WINDOW_SECONDS = 12.0
REPLY_PART_DELAY_SECONDS = 3.0

_group_reply_locks: dict[int, asyncio.Lock] = {}
_last_group_batch_at: dict[int, float] = {}
_last_group_trigger_at: dict[int, float] = {}

_RAW_REPLY_RE = re.compile(
    r"\[(?:CQ:)?reply(?:[:,][^\]]*)?\]",
    flags=re.IGNORECASE,
)
_RAW_REPLY_ID_RE = re.compile(
    r"\[(?:CQ:)?reply[:,][^\]]*?(?:id|message_id)=([0-9]+)[^\]]*\]",
    flags=re.IGNORECASE,
)
_RAW_REPLY_AT_RE = re.compile(
    r"^\s*\[(?:CQ:)?reply(?:[:,][^\]]*)?\]\s*"
    r"\[(?:CQ:)?at[:,]qq=([^,\]]+)(?:,[^\]]*)?\]",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class ReplyContext:
    """The message referenced by a OneBot reply segment."""

    sender_id: str = ""
    sender_name: str = "未知用户"
    message_id: str = ""
    text: str = ""
    image_refs: tuple[dict, ...] = ()

    @property
    def has_image(self) -> bool:
        return bool(self.image_refs)


def _sender_value(sender: object, key: str) -> str:
    if isinstance(sender, dict):
        return str(sender.get(key, "") or "")
    return str(getattr(sender, key, "") or "")


def _reply_message_id(event: GroupMessageEvent) -> str:
    reply = getattr(event, "reply", None)
    value = str(getattr(reply, "message_id", "") or "") if reply else ""
    if value:
        return value
    raw = getattr(event, "raw_message", "") or ""
    match = _RAW_REPLY_ID_RE.search(raw)
    return match.group(1) if match else ""


def _reply_sender_id_from_raw(event: GroupMessageEvent) -> str:
    raw = getattr(event, "raw_message", "") or ""
    match = _RAW_REPLY_AT_RE.search(raw)
    return match.group(1).strip() if match else ""


def _message_from_api(data: object) -> Message | None:
    if not isinstance(data, dict):
        return None
    raw_message = data.get("message")
    if isinstance(raw_message, Message):
        return raw_message
    if isinstance(raw_message, list):
        try:
            segments: list[MessageSegment] = []
            for item in raw_message:
                if isinstance(item, MessageSegment):
                    segments.append(item)
                    continue
                if not isinstance(item, dict):
                    continue
                segment_type = str(item.get("type") or "").strip()
                segment_data = item.get("data") or {}
                if segment_type and isinstance(segment_data, dict):
                    segments.append(MessageSegment(segment_type, segment_data))
            return Message(segments)
        except Exception:
            return None
    if isinstance(raw_message, str):
        return Message(raw_message)
    return None


async def resolve_reply_context(bot: Bot, event: GroupMessageEvent) -> ReplyContext | None:
    """Resolve quoted sender/content, including NapCat's raw reply+at form."""
    reply = getattr(event, "reply", None)
    raw = getattr(event, "raw_message", "") or ""
    if not reply and not _RAW_REPLY_RE.search(raw):
        return None

    message_id = _reply_message_id(event)
    reply_message = getattr(reply, "message", None) if reply else None
    sender = getattr(reply, "sender", None) if reply else None
    sender_id = _sender_value(sender, "user_id")
    sender_name = (
        _sender_value(sender, "card")
        or _sender_value(sender, "nickname")
        or _sender_value(sender, "name")
    )

    # Some NapCat payloads expose only [reply:id][at:qq] in raw_message.  The
    # get_msg fallback fills in the sender/content when the event omitted it.
    if message_id and (not sender_id or reply_message is None):
        try:
            fetched = await bot.call_api("get_msg", message_id=int(message_id))
            fetched_sender = fetched.get("sender") if isinstance(fetched, dict) else None
            sender_id = sender_id or _sender_value(fetched_sender, "user_id")
            sender_name = (
                sender_name
                or _sender_value(fetched_sender, "card")
                or _sender_value(fetched_sender, "nickname")
                or _sender_value(fetched_sender, "name")
            )
            reply_message = reply_message or _message_from_api(fetched)
        except Exception:
            pass

    sender_id = sender_id or _reply_sender_id_from_raw(event)
    if sender_id and sender_id == str(bot.self_id):
        sender_name = "凛(我)"
    elif not sender_name and sender_id:
        try:
            member = await bot.get_group_member_info(
                group_id=event.group_id,
                user_id=int(sender_id),
            )
            sender_name = member.get("card") or member.get("nickname") or sender_id
        except Exception:
            sender_name = sender_id
    sender_name = sender_name or sender_id or "未知用户"

    text = ""
    image_refs: list[dict] = []
    if reply_message is not None:
        try:
            text = reply_message.extract_plain_text().strip()
        except Exception:
            text = ""
        for segment in reply_message:
            if segment.type == "image":
                url = segment.data.get("url") or segment.data.get("file", "")
                if url:
                    image_refs.append({
                        "url": url,
                        "label": f"引用的图片，发送者：{sender_name}",
                    })
            elif segment.type == "file":
                filename = segment.data.get("file_name", "").lower()
                if not filename.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")):
                    continue
                file_id = segment.data.get("file_id", "")
                if not file_id:
                    continue
                try:
                    file_info = await bot.call_api("get_file", file_id=file_id)
                    url = file_info.get("url") or file_info.get("file_url") or ""
                    if url:
                        image_refs.append({
                            "url": url,
                            "label": f"引用的文件图片，发送者：{sender_name}",
                        })
                except Exception:
                    pass

    return ReplyContext(
        sender_id=sender_id,
        sender_name=sender_name,
        message_id=message_id,
        text=text,
        image_refs=tuple(image_refs),
    )


def format_reply_context(context: ReplyContext | None, text: str) -> str:
    """Keep the quoted author's identity in text stored/injected for the AI."""
    if context is None:
        return (text or "").strip()
    if context.text:
        quote = f"「回复 {context.sender_name}: {context.text}」"
    elif context.has_image:
        quote = f"「回复 {context.sender_name} 的图片」"
    else:
        quote = f"「回复 {context.sender_name} 的消息」"
    return f"{quote} {(text or '').strip()}".strip()


def mark_group_trigger(group_id: int, window: float = REPLY_BURST_WINDOW_SECONDS) -> bool:
    """Register a response request and report whether it is a rapid burst."""
    group_id = int(group_id)
    now = time.monotonic()
    previous = _last_group_trigger_at.get(group_id, 0.0)
    _last_group_trigger_at[group_id] = now
    return bool(previous and now - previous <= window)


def _group_reply_lock(group_id: int) -> asyncio.Lock:
    group_id = int(group_id)
    lock = _group_reply_locks.get(group_id)
    if lock is None:
        lock = asyncio.Lock()
        _group_reply_locks[group_id] = lock
    return lock


async def send_group_reply_parts(
    bot: Bot,
    group_id: int,
    parts: list[str],
    *,
    reply_message_id: int | None = None,
    mention_user_ids: list[int] | tuple[int, ...] | None = None,
    quote_rate: float = 0.0,
    force_quote: bool = False,
) -> None:
    """Send one complete response batch before another batch can start."""
    parts = [str(part).strip() for part in parts if str(part).strip()]
    if not parts:
        return

    lock = _group_reply_lock(group_id)
    was_contended = lock.locked()
    async with lock:
        last_batch_at = _last_group_batch_at.get(int(group_id), 0.0)
        recent_batch = bool(
            last_batch_at
            and time.monotonic() - last_batch_at <= REPLY_BURST_WINDOW_SECONDS
        )
        should_quote = bool(reply_message_id is not None) and (
            force_quote
            or was_contended
            or recent_batch
            or random.random() < max(0.0, min(1.0, float(quote_rate)))
        )
        mention_ids: list[int] = []
        for value in mention_user_ids or []:
            try:
                user_id = int(value)
            except (TypeError, ValueError):
                continue
            if user_id > 0 and user_id not in mention_ids:
                mention_ids.append(user_id)

        first_message = Message()
        if should_quote:
            first_message += MessageSegment.reply(int(reply_message_id))
        for user_id in mention_ids:
            first_message += MessageSegment.at(user_id)
            first_message += " "
        first_message += parts[0]
        first: Message | str = first_message if should_quote or mention_ids else parts[0]
        try:
            await bot.send_group_msg(group_id=int(group_id), message=first)
            for part in parts[1:]:
                await asyncio.sleep(REPLY_PART_DELAY_SECONDS)
                await bot.send_group_msg(group_id=int(group_id), message=part)
        finally:
            _last_group_batch_at[int(group_id)] = time.monotonic()
