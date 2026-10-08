"""Return recent group context around the latest message that mentioned the user."""

import json
import re

import yaml
from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot.log import logger
from nonebot.rule import Rule
from sqlalchemy import select

from .db import get_session, GroupMessage

with open("config.yaml", "r", encoding="utf-8") as f:
    _config = yaml.safe_load(f)

_enabled_groups: set[int] = {
    int(g) for g in _config.get("ai", {}).get("group_chat", {}).get("enabled_groups", [])
}
_local_disabled_groups: set[int] = {
    int(g) for g in _config.get("group_mode", {}).get("local_disabled_groups", [])
}

_TRIGGER_RE = re.compile(r"^\s*谁\s*(?:@|艾特|at)\s*我\s*[?？!！。~～]*\s*$", re.IGNORECASE)
_CQ_AT_RE = re.compile(r"\[CQ:at,qq=([^,\]]+)[^\]]*\]")
_CQ_IMAGE_RE = re.compile(r"\[CQ:image[^\]]*\]")
_CQ_REPLY_RE = re.compile(r"\[CQ:reply[^\]]*\]")
_CQ_FACE_RE = re.compile(r"\[CQ:face[^\]]*\]")
_CQ_ANY_RE = re.compile(r"\[CQ:[^\]]+\]")

_HISTORY_LIMIT = 2000
_SIDE_SIZE = 20


def _at_context_rule() -> Rule:
    async def _rule(event: GroupMessageEvent) -> bool:
        group_id = int(event.group_id)
        if group_id in _local_disabled_groups:
            return False
        if group_id not in _enabled_groups:
            return False
        return bool(_TRIGGER_RE.fullmatch(event.get_plaintext().strip()))

    return Rule(_rule)


at_context = on_message(rule=_at_context_rule(), priority=4, block=True)


def _load_at_users(raw: str | None) -> set[int]:
    if not raw:
        return set()
    try:
        data = json.loads(raw)
    except Exception:
        return set()
    if not isinstance(data, list):
        return set()
    users: set[int] = set()
    for item in data:
        try:
            users.add(int(item))
        except (TypeError, ValueError):
            continue
    return users


def _query_nickname(event: GroupMessageEvent) -> str:
    sender = event.sender
    return sender.card or sender.nickname or str(event.user_id)


def _render_raw_message(raw: str, target_user_id: int, target_nickname: str) -> str:
    def replace_at(match: re.Match[str]) -> str:
        qq = match.group(1)
        if qq.lower() == "all":
            return "@全体成员"
        if qq == str(target_user_id):
            return f"@{target_nickname}"
        return f"@{qq}"

    text = raw or ""
    text = _CQ_REPLY_RE.sub("[引用]", text)
    text = _CQ_IMAGE_RE.sub("[图片]", text)
    text = _CQ_FACE_RE.sub("[表情]", text)
    text = _CQ_AT_RE.sub(replace_at, text)
    text = _CQ_ANY_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def _format_node_content(row: GroupMessage, target_user_id: int, target_nickname: str) -> str:
    text = _render_raw_message(row.raw_message or "", target_user_id, target_nickname)
    if not text:
        text = (row.content or "").strip()
    if row.has_image and "[图片]" not in text:
        text = f"[图片] {text}".strip()
    return text or "[消息]"


def _build_forward_nodes(
    rows: list[GroupMessage],
    bot_self_id: str,
    target_user_id: int,
    target_nickname: str,
) -> list[dict]:
    nodes: list[dict] = [
        {
            "type": "node",
            "data": {
                "name": "rin",
                "uin": str(bot_self_id),
                "content": "群聊的聊天记录",
            },
        }
    ]
    for row in rows:
        nickname = row.nickname or str(row.user_id)
        uin = str(bot_self_id) if row.is_bot or int(row.user_id) == 0 else str(row.user_id)
        nodes.append({
            "type": "node",
            "data": {
                "name": nickname,
                "uin": uin,
                "content": _format_node_content(row, target_user_id, target_nickname),
            },
        })
    return nodes


def _slice_context(rows: list[GroupMessage], target_index: int) -> list[GroupMessage]:
    start = target_index - _SIDE_SIZE
    end = target_index + _SIDE_SIZE + 1
    if start < 0:
        end += -start
        start = 0
    if end > len(rows):
        start -= end - len(rows)
        end = len(rows)
    return rows[max(0, start):min(len(rows), end)]


@at_context.handle()
async def handle_at_context(bot: Bot, event: GroupMessageEvent):
    group_id = int(event.group_id)
    user_id = int(event.user_id)
    target_nickname = _query_nickname(event)

    session = await get_session()
    try:
        rows_desc = (await session.execute(
            select(GroupMessage)
            .where(GroupMessage.group_id == group_id)
            .order_by(GroupMessage.id.desc())
            .limit(_HISTORY_LIMIT)
        )).scalars().all()
    finally:
        await session.close()

    if not rows_desc:
        return

    target_id: int | None = None
    for row in rows_desc:
        if user_id in _load_at_users(row.at_users):
            target_id = row.id
            break

    if target_id is None:
        return

    rows = list(reversed(rows_desc))
    target_index = next((i for i, row in enumerate(rows) if row.id == target_id), -1)
    if target_index < 0:
        return

    context_rows = _slice_context(rows, target_index)
    nodes = _build_forward_nodes(context_rows, str(bot.self_id), user_id, target_nickname)
    try:
        await bot.call_api(
            "send_group_forward_msg",
            group_id=group_id,
            messages=nodes,
            _timeout=60,
        )
    except Exception as e:
        logger.warning(f"[at_context] 合并转发发送失败: {type(e).__name__}: {e}")
