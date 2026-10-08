"""记忆管理插件 — MySQL 存储"""

import yaml
import redis as redis_lib
from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot.log import logger
from sqlalchemy import select, delete

from .db import AgentGlobalPersonFact, AgentPersonFact, ChatHistory, UserMemory, get_session

with open("config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

rds = redis_lib.Redis(host=config["redis"]["host"], port=config["redis"]["port"], password=config["redis"].get("password") or None, db=int(config["redis"].get("db", 0)), decode_responses=True)
_ADMINS: set[int] = {
    int(user_id) for user_id in config.get("setu", {}).get("admin_users", [])
}


def _target_user_id(event: GroupMessageEvent) -> int:
    for segment in event.message:
        if segment.type == "at":
            raw = str(segment.data.get("qq", ""))
            if raw.isdigit():
                return int(raw)
    for token in event.message.extract_plain_text().split():
        if token.isdigit() and 5 <= len(token) <= 12:
            return int(token)
    return int(event.user_id)

memory_cmd = on_command("#我的记忆", priority=5, block=True)
clear_memory_cmd = on_command("#清除记忆", priority=5, block=True)
clear_history_cmd = on_command("#清除历史", priority=5, block=True)
clear_search_cmd = on_command("#清除搜索", priority=5, block=True)


@memory_cmd.handle()
async def handle_memory(bot: Bot, event: GroupMessageEvent):
    if int(event.user_id) not in _ADMINS:
        await memory_cmd.finish("记忆查看仅对管理员开放。")
    user_id = _target_user_id(event)
    session = await get_session()
    try:
        rows = (await session.execute(
            select(UserMemory)
            .where(UserMemory.user_id == user_id)
            .order_by(UserMemory.id.desc())
            .limit(50)
        )).scalars().all()
        facts = (await session.execute(
            select(AgentPersonFact)
            .where(
                AgentPersonFact.user_id == user_id,
                AgentPersonFact.status == "active",
            )
            .order_by(AgentPersonFact.importance.desc(), AgentPersonFact.updated_at.desc())
            .limit(50)
        )).scalars().all()
        global_facts = (await session.execute(
            select(AgentGlobalPersonFact)
            .where(
                AgentGlobalPersonFact.user_id == user_id,
                AgentGlobalPersonFact.status == "active",
            )
            .order_by(AgentGlobalPersonFact.importance.desc(), AgentGlobalPersonFact.updated_at.desc())
            .limit(50)
        )).scalars().all()
    finally:
        await session.close()

    if not rows and not facts and not global_facts:
        await memory_cmd.send("🧠 暂无记忆记录")
        return

    messages = []
    index = 1
    for row in facts:
        content = (
            f"{index}. 【长期事实 / 群{row.group_id} / {row.category}】{row.fact}"
            f"（来源消息:{row.source_message_id}，更新:{row.updated_at.strftime('%Y-%m-%d %H:%M')}）"
        )
        messages.append({
            "type": "node",
            "data": {
                "name": "记忆系统",
                "uin": str(bot.self_id),
                "content": content,
            }
        })
        index += 1
    for row in global_facts:
        content = (
            f"{index}. 【全局长期事实 / {row.category}】{row.fact}"
            f"（来源群:{row.source_group_id} 消息:{row.source_message_id}，更新:{row.updated_at.strftime('%Y-%m-%d %H:%M')}）"
        )
        messages.append({
            "type": "node",
            "data": {
                "name": "记忆系统",
                "uin": str(bot.self_id),
                "content": content,
            }
        })
        index += 1
    for row in rows:
        content = f"{index}. 【旧对话摘要】{row.content}（{row.created_at.strftime('%Y-%m-%d %H:%M')}）"
        messages.append({
            "type": "node",
            "data": {
                "name": "记忆系统",
                "uin": str(bot.self_id),
                "content": content,
            }
        })
        index += 1

    try:
        await bot.call_api("send_group_forward_msg", group_id=event.group_id, messages=messages)
    except Exception:
        preview = messages[:10]
        text = "\n".join(
            str(node.get("data", {}).get("content", ""))
            for node in preview
        )
        await memory_cmd.send(f"🧠 最近记忆:\n{text}")


@clear_memory_cmd.handle()
async def handle_clear_memory(event: GroupMessageEvent):
    if int(event.user_id) not in _ADMINS:
        await clear_memory_cmd.finish("记忆清理仅对管理员开放。")
    target_user_id = _target_user_id(event)
    session = await get_session()
    try:
        await session.execute(delete(UserMemory).where(UserMemory.user_id == target_user_id))
        await session.execute(delete(AgentPersonFact).where(AgentPersonFact.user_id == target_user_id))
        await session.execute(delete(AgentGlobalPersonFact).where(AgentGlobalPersonFact.user_id == target_user_id))
        await session.commit()
    finally:
        await session.close()
    await clear_memory_cmd.send(
        f"已清除 QQ {target_user_id} 的个人摘要和长期事实；群聊原始记录与群公共摘要不受影响。"
    )


@clear_history_cmd.handle()
async def handle_clear_history(event: GroupMessageEvent):
    if int(event.user_id) not in _ADMINS:
        return

    target_user_id: int | None = None
    for segment in event.message:
        if segment.type == "at":
            raw_qq = str(segment.data.get("qq", ""))
            if raw_qq.isdigit():
                target_user_id = int(raw_qq)
                break

    if target_user_id is None:
        for token in event.message.extract_plain_text().split():
            if token.isdigit() and 5 <= len(token) <= 12:
                target_user_id = int(token)
                break

    target_user_id = target_user_id or int(event.user_id)
    session = await get_session()
    try:
        await session.execute(delete(ChatHistory).where(ChatHistory.user_id == target_user_id))
        await session.commit()
    finally:
        await session.close()
    if target_user_id == int(event.user_id):
        await clear_history_cmd.send("已清除你的对话记录！")
    else:
        await clear_history_cmd.send(f"已清除 QQ {target_user_id} 的对话记录！")


@clear_search_cmd.handle()
async def handle_clear_search(event: GroupMessageEvent):
    rds.delete(f"ai:search:{event.user_id}")
    await clear_search_cmd.send("已清除你的搜索记录！")
