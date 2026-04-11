"""记忆管理插件 — MySQL 存储"""

import yaml
import redis as redis_lib
from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot.log import logger
from sqlalchemy import select, delete

from .db import get_session, ChatHistory, UserMemory

with open("config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

rds = redis_lib.Redis(host=config["redis"]["host"], port=config["redis"]["port"], decode_responses=True)

memory_cmd = on_command("#我的记忆", priority=5, block=True)
clear_memory_cmd = on_command("#清除记忆", priority=5, block=True)
clear_history_cmd = on_command("#清除历史", priority=5, block=True)
clear_search_cmd = on_command("#清除搜索", priority=5, block=True)


@memory_cmd.handle()
async def handle_memory(bot: Bot, event: GroupMessageEvent):
    user_id = event.user_id
    session = await get_session()
    try:
        rows = (await session.execute(
            select(UserMemory)
            .where(UserMemory.user_id == user_id)
            .order_by(UserMemory.id.desc())
            .limit(50)
        )).scalars().all()
    finally:
        await session.close()

    if not rows:
        await memory_cmd.send("🧠 暂无记忆记录")
        return

    messages = []
    for i, row in enumerate(rows):
        content = f"{i + 1}. {row.content}（{row.created_at.strftime('%Y-%m-%d %H:%M')}）"
        messages.append({
            "type": "node",
            "data": {
                "name": "记忆系统",
                "uin": str(bot.self_id),
                "content": content,
            }
        })

    try:
        await bot.call_api("send_group_forward_msg", group_id=event.group_id, messages=messages)
    except Exception:
        text = "\n".join(f"{i+1}. {r.content}" for i, r in enumerate(rows[:10]))
        await memory_cmd.send(f"🧠 最近记忆:\n{text}")


@clear_memory_cmd.handle()
async def handle_clear_memory(event: GroupMessageEvent):
    session = await get_session()
    try:
        await session.execute(delete(UserMemory).where(UserMemory.user_id == event.user_id))
        await session.commit()
    finally:
        await session.close()
    await clear_memory_cmd.send("已清除你的所有记忆！")


@clear_history_cmd.handle()
async def handle_clear_history(event: GroupMessageEvent):
    session = await get_session()
    try:
        await session.execute(delete(ChatHistory).where(ChatHistory.user_id == event.user_id))
        await session.commit()
    finally:
        await session.close()
    await clear_history_cmd.send("已清除你的对话记录！")


@clear_search_cmd.handle()
async def handle_clear_search(event: GroupMessageEvent):
    rds.delete(f"ai:search:{event.user_id}")
    await clear_search_cmd.send("已清除你的搜索记录！")
