"""群聊监听插件 — 记录群消息 + 被动参与群聊

群消息存入 MySQL（带昵称），被动回复/识图时取最近50条群聊记录。
"""

import re
import time
import random

import httpx
import yaml
import redis as redis_lib
from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot.rule import Rule
from nonebot.log import logger
from sqlalchemy import select, func, delete

from .db import get_session, GroupMessage

with open("config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

ai_cfg = config["ai"]
gc_cfg = ai_cfg["group_chat"]
rds = redis_lib.Redis(host=config["redis"]["host"], port=config["redis"]["port"], decode_responses=True)

GROUP_CONTEXT_LIMIT = 50  # 自动回复取群聊最近50条

_last_reply_time: dict[int, float] = {}


def is_group_enabled(group_id: int) -> bool:
    return group_id in gc_cfg.get("enabled_groups", [])


def not_at_bot_rule() -> Rule:
    async def _rule(bot: Bot, event: GroupMessageEvent) -> bool:
        return f"[CQ:at,qq={bot.self_id}]" not in event.raw_message
    return Rule(_rule)


group_listener = on_message(rule=not_at_bot_rule(), priority=10, block=False)


@group_listener.handle()
async def handle_group_msg(bot: Bot, event: GroupMessageEvent):
    group_id = event.group_id
    if not is_group_enabled(group_id):
        return

    plain = event.get_plaintext().strip()
    user_id = event.user_id

    # 获取昵称
    try:
        member = await bot.get_group_member_info(group_id=group_id, user_id=user_id)
        nickname = member.get("card") or member.get("nickname") or str(user_id)
    except Exception:
        nickname = str(user_id)

    has_image = any(seg.type == "image" for seg in event.message)

    # 存入 MySQL（非指令消息）
    if (plain or has_image) and not plain.startswith("#"):
        try:
            session = await get_session()
            try:
                session.add(GroupMessage(
                    group_id=group_id,
                    user_id=user_id,
                    nickname=nickname,
                    content=plain,
                    has_image=has_image,
                ))
                await session.commit()
                # 5% 概率清理旧记录，每群保留最新500条
                if random.random() < 0.05:
                    count = (await session.execute(
                        select(func.count()).select_from(GroupMessage)
                        .where(GroupMessage.group_id == group_id)
                    )).scalar()
                    if count > 500:
                        old_ids = (await session.execute(
                            select(GroupMessage.id)
                            .where(GroupMessage.group_id == group_id)
                            .order_by(GroupMessage.id.asc())
                            .limit(count - 500)
                        )).scalars().all()
                        if old_ids:
                            await session.execute(
                                delete(GroupMessage).where(GroupMessage.id.in_(old_ids))
                            )
                            await session.commit()
            finally:
                await session.close()
        except Exception as e:
            logger.warning(f"[group_chat] 存储群消息失败: {e}")

    # 提取图片URL
    image_urls = [
        seg.data.get("url") or seg.data.get("file", "")
        for seg in event.message
        if seg.type == "image" and (seg.data.get("url") or seg.data.get("file", ""))
    ]

    # 群内图片概率识图回复
    vision_rate = gc_cfg.get("vision_image_rate", 0.0)
    if image_urls and vision_rate > 0 and not _is_cooling(group_id) and random.random() < vision_rate:
        reply = await vision_comment(group_id, image_urls, plain)
        if reply:
            _last_reply_time[group_id] = time.time()
            await group_listener.send(reply)
            rds.setex(f"meme:cd:{group_id}", config.get("meme", {}).get("cooldown", 60), "1")
        return

    # 常规被动文字回复
    if not plain or plain.startswith("#"):
        return

    if not should_trigger(group_id, plain):
        return

    reply = await passive_reply(group_id)
    if reply:
        await group_listener.send(reply)


def should_trigger(group_id: int, message: str) -> bool:
    cooldown = gc_cfg.get("cooldown_seconds", 10)
    last = _last_reply_time.get(group_id, 0)
    if time.time() - last < cooldown:
        return False

    rate = gc_cfg.get("passive_rate", 0.005)
    for kw in gc_cfg.get("keywords", []):
        if kw in message:
            rate = gc_cfg.get("keyword_rate", 0.1)
            break

    return random.random() < rate


def _is_cooling(group_id: int) -> bool:
    cooldown = gc_cfg.get("cooldown_seconds", 10)
    return time.time() - _last_reply_time.get(group_id, 0) < cooldown


async def _get_group_context_text(group_id: int) -> str | None:
    """从 MySQL 读取最近50条群聊记录，附带昵称"""
    session = await get_session()
    try:
        rows = (await session.execute(
            select(GroupMessage)
            .where(GroupMessage.group_id == group_id)
            .order_by(GroupMessage.id.desc())
            .limit(GROUP_CONTEXT_LIMIT)
        )).scalars().all()
        if not rows:
            return None
        rows.reverse()
        lines = []
        for r in rows:
            if r.has_image and r.content:
                lines.append(f"{r.nickname}: [图片] {r.content}")
            elif r.has_image:
                lines.append(f"{r.nickname}: [发了图片]")
            else:
                lines.append(f"{r.nickname}: {r.content}")
        return "[最近的群聊记录]\n" + "\n".join(lines)
    finally:
        await session.close()


async def passive_reply(group_id: int) -> str | None:
    """被动回复 — 从 MySQL 读取群聊上下文（带昵称）"""
    context_text = await _get_group_context_text(group_id)
    if not context_text:
        return None

    system_content = (
        ai_cfg["system_prompt"]
        + "\n\n【群聊模式】以下是群聊中大家的对话记录，你可以自然地参与讨论，保持简短自然。"
    )
    messages = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": context_text},
    ]

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                ai_cfg["api_url"],
                headers={"Authorization": f"Bearer {ai_cfg['api_key']}"},
                json={"model": ai_cfg["model"], "messages": messages},
            )
            reply = resp.json()["choices"][0]["message"]["content"].strip()
            reply = re.sub(r"<think>.*?</think>", "", reply, flags=re.DOTALL).strip()
            _last_reply_time[group_id] = time.time()
            return reply if reply else None
    except Exception:
        return None


async def vision_comment(group_id: int, image_urls: list[str], user_text: str) -> str | None:
    """Qwen-VL 识图，结合群聊上下文评论"""
    vision_cfg = ai_cfg.get("vision", {})
    api_url = vision_cfg.get("api_url", ai_cfg["api_url"])
    api_key = vision_cfg.get("api_key", ai_cfg["api_key"])
    model = vision_cfg.get("model", "qwen3-vl-plus")

    # 读取群聊上下文，让 AI 知道大家在聊什么
    context_text = await _get_group_context_text(group_id)

    content: list = []
    for url in image_urls:
        content.append({"type": "image_url", "image_url": {"url": url}})

    prompt_parts = []
    if context_text:
        prompt_parts.append(context_text)
    if user_text:
        prompt_parts.append(f"发图者说: {user_text}")
    prompt_parts.append("群里有人发了这张图，结合上面的群聊内容，用你的风格自然地评论一下，要短小自然")
    content.append({"type": "text", "text": "\n".join(prompt_parts)})

    system_prompt = (
        ai_cfg["system_prompt"]
        + "\n\n[群聊模式]你看到群里有人发了一张图，结合群内讨论的话题自然地发表评论，严格控制在30字以内，最多两句话。"
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": content},
    ]

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                api_url,
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": model, "messages": messages},
            )
            reply = resp.json()["choices"][0]["message"]["content"].strip()
        reply = re.sub(r"<think>.*?</think>", "", reply, flags=re.DOTALL).strip()
        return reply if reply else None
    except Exception:
        return None
