"""AI 聊天插件 — @机器人 触发对话

对话历史存储于 MySQL，按 user_id 隔离上下文。
被@时注入最近50条群聊记录 + 用户50条对话历史。
"""

import asyncio
import re
import random
import pathlib

import httpx
import yaml
from datetime import datetime
from nonebot import on_message, get_driver
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageSegment, Message
from nonebot.rule import Rule
from nonebot.log import logger
from sqlalchemy import select, func, delete

from .db import Base, engine, get_session, ChatHistory, UserMemory, GroupMessage
from .meme_collector import get_reply_meme_url

# ---------- 配置 ----------
with open("config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

ai_cfg = config["ai"]
API_URL = ai_cfg["api_url"]
API_KEY = ai_cfg["api_key"]
MODEL = ai_cfg["model"]
MAX_HISTORY = ai_cfg.get("max_history", 100)  # 用户对话保留100条
USER_CONTEXT_LIMIT = 50   # @回复时取用户最近50条
GROUP_CONTEXT_LIMIT = 50  # @回复时取群聊最近50条


# ---------- 人设知识库加载 ----------
def _load_persona() -> str:
    """
    加载 persona/ 目录下所有 .md 文件，按文件名排序后合并。
    character.md（设定）排在 prompt.md（规则）之前（字母序 c < p）。
    """
    persona_dir = ai_cfg.get("persona_dir", "persona")
    base = pathlib.Path(persona_dir)
    if not base.is_dir():
        logger.warning(f"[ai_chat] persona 目录不存在: {persona_dir}，人设知识库未加载")
        return ""
    parts = []
    files = sorted(base.glob("*.md"))
    for f in files:
        try:
            text = f.read_text(encoding="utf-8").strip()
            if text:
                parts.append(text)
        except Exception as e:
            logger.warning(f"[ai_chat] 读取人设文件失败 {f.name}: {e}")
    if parts:
        total_chars = sum(len(p) for p in parts)
        logger.info(f"[ai_chat] 已加载人设文档 {len(parts)} 个（{', '.join(f.name for f in files)}），共 {total_chars} 字")
        return "\n\n---\n\n".join(parts)
    return ""


PERSONA_CONTENT: str = _load_persona()


# ---------- 错误日志 ----------
_log_dir = pathlib.Path("logs")
_log_dir.mkdir(exist_ok=True)
_err_log_path = _log_dir / "ai_errors.log"

def _log_err(tag: str, detail: str):
    """将错误明细写入 logs/ai_errors.log，不向用户暴露任何技术信息。"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(_err_log_path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] [{tag}] {detail}\n")
    except Exception:
        pass
    logger.debug(f"[{tag}] {detail}")


# ---------- 建表 ----------
@get_driver().on_startup
async def _create_chat_tables():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("[ai_chat] 数据表已就绪")


# ---------- 规则 ----------
def _at_bot_rule() -> Rule:
    """检测消息中任意位置是否有 @bot"""
    async def _rule(bot: Bot, event: GroupMessageEvent) -> bool:
        for seg in event.message:
            if seg.type == "at" and str(seg.data.get("qq")) == str(bot.self_id):
                return True
        return event.to_me
    return Rule(_rule)


ai_chat = on_message(rule=_at_bot_rule(), priority=5, block=True)


@ai_chat.handle()
async def handle_chat(bot: Bot, event: GroupMessageEvent):
    user_id = event.user_id
    group_id = event.group_id

    # 获取用户昵称
    try:
        member = await bot.get_group_member_info(group_id=group_id, user_id=user_id)
        nickname = member.get("card") or member.get("nickname") or str(user_id)
    except Exception:
        nickname = str(user_id)

    # 构建消息文本，将 @某人 解析为真实昵称，避免 AI 收到空白或无法识别的 QQ 号
    message_parts = []
    for seg in event.message:
        if seg.type == "text":
            text = seg.data.get("text", "").strip()
            if text:
                message_parts.append(text)
        elif seg.type == "at":
            at_qq = str(seg.data.get("qq", ""))
            if at_qq == str(bot.self_id):
                continue  # 跳过 @机器人 本身
            try:
                at_member = await bot.get_group_member_info(group_id=group_id, user_id=int(at_qq))
                at_name = at_member.get("card") or at_member.get("nickname") or at_qq
            except Exception:
                at_name = at_qq
            message_parts.append(f"@{at_name}")
    message = " ".join(message_parts).strip()

    # 提取图片URL
    image_urls = [
        seg.data.get("url") or seg.data.get("file", "")
        for seg in event.message
        if seg.type == "image" and (seg.data.get("url") or seg.data.get("file", ""))
    ]
    if not image_urls and event.reply:
        for seg in event.reply.message:
            if seg.type == "image":
                url = seg.data.get("url") or seg.data.get("file", "")
                if url:
                    image_urls.append(url)

    if not message and not image_urls:
        return

    # 白名单群注入群上下文
    enabled_groups = ai_cfg["group_chat"].get("enabled_groups", [])
    context_group_id = group_id if group_id in enabled_groups else None

    if image_urls:
        reply = await chat_with_vision(user_id, nickname, message, image_urls)
    else:
        reply = await chat(user_id, nickname, message, context_group_id)

    if not reply:
        return

    await ai_chat.send(Message(MessageSegment.reply(event.message_id)) + reply)

    # 概率附带情绪表情包（独立消息，稍后发出）
    reply_meme_rate = ai_cfg.get("group_chat", {}).get("reply_meme_rate", 0.0)
    if reply and reply_meme_rate > 0 and random.random() < reply_meme_rate:
        try:
            meme_url = await get_reply_meme_url(reply)
            if meme_url:
                await asyncio.sleep(0.4)
                await bot.send_group_msg(group_id=group_id, message=MessageSegment.image(meme_url))
        except Exception as e:
            logger.debug(f"[ai_chat] 发送表情包失败（忽略）: {e}")


# ---------- 文字对话 (MySQL 存储) ----------

async def chat(user_id: int, nickname: str, user_message: str, group_id: int = None) -> str:
    """主对话 — 用户50条历史 + 群聊50条上下文"""
    # 1. 从 MySQL 加载用户近期对话 (最近50条)
    session = await get_session()
    try:
        rows = (await session.execute(
            select(ChatHistory)
            .where(ChatHistory.user_id == user_id)
            .order_by(ChatHistory.id.desc())
            .limit(USER_CONTEXT_LIMIT)
        )).scalars().all()
        rows.reverse()
        history = [{"role": r.role, "content": r.content} for r in rows]
    finally:
        await session.close()

    # 2. 构建 system prompt
    special_users = ai_cfg.get("special_users", {})
    prompt = ai_cfg["system_prompt"]
    if user_id in special_users:
        prompt += "\n" + special_users[user_id]
    prompt += f"\n\n[当前对话用户昵称: {nickname}]"

    messages = [{"role": "system", "content": prompt}]

    # 注入人设知识库（设定 + 规则）
    if PERSONA_CONTENT:
        messages.append({"role": "system", "content": PERSONA_CONTENT})

    # 3. 注入群聊上下文 (最近50条)
    if group_id is not None:
        group_context = await _get_group_context(group_id, GROUP_CONTEXT_LIMIT)
        if group_context:
            messages.append({"role": "user", "content": group_context})
            messages.append({"role": "assistant", "content": "好的，我了解了群里最近的动态。"})

    # 4. 用户历史 + 当前消息 (带昵称标签)
    messages.extend(history)
    tagged_msg = f"[{nickname}]: {user_message}"
    messages.append({"role": "user", "content": tagged_msg})

    # 5. 调用 API
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                API_URL,
                headers={"Authorization": f"Bearer {API_KEY}"},
                json={"model": MODEL, "messages": messages},
            )
            data = resp.json()
            if "error" in data:
                _log_err("ai_chat/chat", f"API error: {data['error']}")
                return None
            reply = data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        _log_err("ai_chat/chat", f"API 调用异常: {e}")
        return None

    reply = re.sub(r"<think>.*?</think>", "", reply, flags=re.DOTALL).strip()

    # 6. 存入 MySQL (保留100条)
    session = await get_session()
    try:
        session.add(ChatHistory(user_id=user_id, role="user", content=tagged_msg))
        session.add(ChatHistory(user_id=user_id, role="assistant", content=reply))
        await session.commit()
        # 裁剪: 保留最新 MAX_HISTORY 条
        count = (await session.execute(
            select(func.count()).select_from(ChatHistory).where(ChatHistory.user_id == user_id)
        )).scalar()
        if count > MAX_HISTORY:
            old_ids = (await session.execute(
                select(ChatHistory.id)
                .where(ChatHistory.user_id == user_id)
                .order_by(ChatHistory.id.asc())
                .limit(count - MAX_HISTORY)
            )).scalars().all()
            if old_ids:
                await session.execute(delete(ChatHistory).where(ChatHistory.id.in_(old_ids)))
                await session.commit()
    except Exception as e:
        logger.warning(f"[ai_chat] 历史存储失败: {e}")
    finally:
        await session.close()

    # 7. 异步提取记忆
    asyncio.create_task(_extract_memory(user_id, user_message))

    return reply


async def _get_group_context(group_id: int, limit: int = 50) -> str | None:
    """从数据库获取最近的群聊记录，附带昵称"""
    session = await get_session()
    try:
        rows = (await session.execute(
            select(GroupMessage)
            .where(GroupMessage.group_id == group_id)
            .order_by(GroupMessage.id.desc())
            .limit(limit)
        )).scalars().all()
        if not rows:
            return None
        rows.reverse()
        lines = []
        for r in rows:
            if r.has_image and r.content:
                lines.append(f"{r.nickname}: [发了图片] {r.content}")
            elif r.has_image:
                lines.append(f"{r.nickname}: [发了图片]")
            else:
                lines.append(f"{r.nickname}: {r.content}")
        return "[最近的群聊记录，仅供参考]\n" + "\n".join(lines)
    finally:
        await session.close()


# ---------- 图片识别对话 ----------

async def chat_with_vision(user_id: int, nickname: str, user_message: str, image_urls: list[str]) -> str:
    """调用 Qwen-VL 识别图片"""
    vision_cfg = ai_cfg.get("vision", {})
    api_url = vision_cfg.get("api_url", API_URL)
    api_key = vision_cfg.get("api_key", API_KEY)
    model = vision_cfg.get("model", "qwen3-vl-plus")

    content: list = []
    for url in image_urls:
        content.append({"type": "image_url", "image_url": {"url": url}})
    prompt = user_message if user_message else "描述一下这张图，然后用你一贯的风格评论一句"
    content.append({"type": "text", "text": f"[{nickname}]: {prompt}"})

    special_users = ai_cfg.get("special_users", {})
    system_prompt = ai_cfg["system_prompt"]
    if user_id in special_users:
        system_prompt += "\n" + special_users[user_id]
    system_prompt += f"\n\n[当前对话用户昵称: {nickname}]"

    messages = [
        {"role": "system", "content": system_prompt},
    ]
    # 注入人设知识库（设定 + 规则）
    if PERSONA_CONTENT:
        messages.append({"role": "system", "content": PERSONA_CONTENT})
    messages.append({"role": "user", "content": content})

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                api_url,
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": model, "messages": messages},
            )
            resp_json = resp.json()
            if "error" in resp_json:
                _log_err("ai_chat/vision", f"API 报错: {resp_json['error']}")
                return None
            reply = resp_json["choices"][0]["message"]["content"].strip()
        reply = re.sub(r"<think>.*?</think>", "", reply, flags=re.DOTALL).strip()
        return reply
    except Exception as e:
        _log_err("ai_chat/vision", f"调用异常: {e}")
        return None


# ---------- 记忆提取 ----------

async def _extract_memory(user_id: int, user_message: str):
    """异步提取用户发言记忆存入 MySQL"""
    try:
        prompt = (
            "请把以下用户说的话总结成一句简短的第三人称陈述句（15字以内），"
            "只输出这句话，不要加任何多余内容。\n"
            f"用户说：{user_message}"
        )
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                API_URL,
                headers={"Authorization": f"Bearer {API_KEY}"},
                json={"model": MODEL, "messages": [{"role": "user", "content": prompt}]},
            )
            resp_data = resp.json()
            if "error" in resp_data or "choices" not in resp_data:
                return
            summary = resp_data["choices"][0]["message"]["content"].strip()
            summary = re.sub(r"<think>.*?</think>", "", summary, flags=re.DOTALL).strip()

        session = await get_session()
        try:
            session.add(UserMemory(user_id=user_id, content=summary))
            await session.commit()
            count = (await session.execute(
                select(func.count()).select_from(UserMemory).where(UserMemory.user_id == user_id)
            )).scalar()
            if count > 100:
                old_ids = (await session.execute(
                    select(UserMemory.id)
                    .where(UserMemory.user_id == user_id)
                    .order_by(UserMemory.id.asc())
                    .limit(count - 100)
                )).scalars().all()
                if old_ids:
                    await session.execute(delete(UserMemory).where(UserMemory.id.in_(old_ids)))
                    await session.commit()
        finally:
            await session.close()
    except Exception as e:
        logger.debug(f"[ai_chat] 记忆提取失败: {e}")
