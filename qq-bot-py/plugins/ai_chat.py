"""AI 聊天插件 — @机器人 触发对话

对话历史存储于 MySQL，按 user_id 隔离上下文。
被@时注入最近100条群聊记录（含图片URL、bot自己的发言） + 用户50条对话历史。
支持处理引用消息、合并转发、上下文内图片等多模态内容。
"""

import asyncio
import base64
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
PRIMARY_TIMEOUT = ai_cfg.get("primary_timeout", 15)
MAX_HISTORY = ai_cfg.get("max_history", 100)  # 用户对话保留100条
USER_CONTEXT_LIMIT = 50   # @回复时取用户最近50条
GROUP_CONTEXT_LIMIT = 100  # @回复时取群聊最近100条

# 兑底配置
_fallback_cfg = ai_cfg.get("fallback", {})
FALLBACK_URL = _fallback_cfg.get("api_url", "")
FALLBACK_KEY = _fallback_cfg.get("api_key", "")
FALLBACK_MODEL = _fallback_cfg.get("model", "deepseek-v4-flash")


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
@get_driver().on_startup  # type: ignore[attr-defined]
async def _create_chat_tables():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("[ai_chat] 数据表已就绪")


async def record_bot_reply(group_id: int, content: str) -> None:
    """将 bot 自己的回复写入群消息历史，让 AI 下次能感知自己说过的话。"""
    try:
        session = await get_session()
        try:
            session.add(GroupMessage(
                group_id=group_id,
                user_id=0,
                nickname="凛",
                content=content,
                has_image=False,
                image_url="",
                is_bot=True,
            ))
            await session.commit()
        finally:
            await session.close()
    except Exception as e:
        logger.debug(f"[ai_chat] record_bot_reply 失败: {e}")


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

    # ---- C: 处理引用/回复消息 ----
    # 提取被引用消息的文字和图片，拼成「引用X: ...」前缀让 AI 知道对话背景
    # 注意：引用图片也加入 image_urls，让视觉模型能看到
    image_urls = [
        seg.data.get("url") or seg.data.get("file", "")
        for seg in event.message
        if seg.type == "image"
        and seg.data.get("sub_type", 0) != 1
        and (seg.data.get("url") or seg.data.get("file", ""))
    ]
    if event.reply:
        reply_sender = (
            getattr(event.reply.sender, "card", None)
            or getattr(event.reply.sender, "nickname", None)
            or str(getattr(event.reply.sender, "user_id", "?"))
        )
        reply_text = event.reply.message.extract_plain_text().strip()
        reply_imgs = [
            seg.data.get("url") or seg.data.get("file", "")
            for seg in event.reply.message
            if seg.type == "image"
            and seg.data.get("sub_type", 0) != 1
            and (seg.data.get("url") or seg.data.get("file", ""))
        ]
        if reply_text:
            quote_prefix = f"「引用 {reply_sender}: {reply_text}」"
        elif reply_imgs:
            quote_prefix = f"「引用 {reply_sender} 的图片」"
        else:
            quote_prefix = ""
        if quote_prefix:
            message = (quote_prefix + " " + message).strip()
        # 引用消息里的图片优先拒在当前消息图片前面
        image_urls = reply_imgs + image_urls

    # ---- D: 处理合并转发（仅第一层） ----
    for seg in event.message:
        if seg.type != "forward":
            continue
        fwd_id = seg.data.get("id", "")
        if not fwd_id:
            continue
        try:
            fwd_data = await bot.call_api("get_forward_msg", message_id=fwd_id)
            fwd_messages = fwd_data.get("messages") or fwd_data.get("msg", [])
            lines = []
            for fwd_item in fwd_messages:
                sender_info = fwd_item.get("sender") or {}
                fwd_name = sender_info.get("card") or sender_info.get("nickname") or "某人"
                # 展开消息内容：文字 + 标注是否含图
                fwd_msg = fwd_item.get("message", [])
                if isinstance(fwd_msg, str):
                    fwd_text = fwd_msg.strip()
                    fwd_has_img = False
                else:
                    fwd_text = "".join(
                        s.get("data", {}).get("text", "")
                        for s in fwd_msg if s.get("type") == "text"
                    ).strip()
                    fwd_has_img = any(s.get("type") == "image" for s in fwd_msg)
                line = f"{fwd_name}: {fwd_text}" if fwd_text else f"{fwd_name}:"
                if fwd_has_img:
                    line += "（含图片）"
                lines.append(line)
            if lines:
                fwd_summary = "[合并转发内容]\n" + "\n".join(lines)
                message = (message + "\n" + fwd_summary).strip() if message else fwd_summary
                logger.info(f"[ai_chat] 展开转发 id={fwd_id}，共 {len(lines)} 条")
        except Exception as fwd_err:
            logger.warning(f"[ai_chat] 获取合并转发内容失败: {fwd_err}")
        break  # 只处理第一个 forward segment

    if not message and not image_urls:
        return

    # 白名单群注入群上下文
    enabled_groups = ai_cfg["group_chat"].get("enabled_groups", [])
    context_group_id = group_id if group_id in enabled_groups else None

    if image_urls:
        reply = await chat_with_vision(user_id, nickname, message, image_urls, context_group_id)
    else:
        reply = await chat(user_id, nickname, message, context_group_id)

    if not reply:
        return

    await ai_chat.send(Message(MessageSegment.reply(event.message_id)) + reply)

    # B: 将 bot 回复写入群消息历史，让 AI 下次能感知自己说过的话
    asyncio.create_task(record_bot_reply(group_id, reply))

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

_FATAL_CODES = {"service_unavailable", "rate_limit_exceeded", "insufficient_quota"}


async def _call_primary(messages: list) -> str:
    """调用主模型，受 PRIMARY_TIMEOUT 限制。
    - 超时 / 服务不可用 / 限流 → 直接抛出，触发兜底，不重试
    - 其他错误（如负载均衡坏节点）→ 重试一次
    """
    last_err = None
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=PRIMARY_TIMEOUT) as client:
                logger.info(f"[ai_chat] 调用主模型 {MODEL} (attempt {attempt + 1})")
                resp = await client.post(
                    API_URL,
                    headers={"Authorization": f"Bearer {API_KEY}"},
                    json={"model": MODEL, "messages": messages},
                )
                data = resp.json()
                if "error" in data:
                    err_code = data["error"].get("code", "")
                    if err_code in _FATAL_CODES:
                        raise RuntimeError(f"API error: {data['error']}")  # 不重试
                    raise ValueError(f"API error: {data['error']}")  # 可重试
                return data["choices"][0]["message"]["content"].strip()
        except (httpx.TimeoutException, RuntimeError):
            raise  # 直接触发兜底
        except Exception as e:
            last_err = e
            if attempt == 0:
                logger.debug(f"[ai_chat] 主模型第1次失败，重试: {e}")
            continue
    raise last_err


async def _call_fallback(messages: list) -> str:
    """调用兜底模型（DeepSeek V4 Flash），使用完整超时"""
    async with httpx.AsyncClient(timeout=60) as client:
        logger.info(f"[ai_chat] 调用兜底模型 {FALLBACK_MODEL}")
        resp = await client.post(
            FALLBACK_URL,
            headers={"Authorization": f"Bearer {FALLBACK_KEY}"},
            json={"model": FALLBACK_MODEL, "messages": messages},
        )
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"Fallback API error: {data['error']}")
        return data["choices"][0]["message"]["content"].strip()


async def chat(user_id: int, nickname: str, user_message: str, group_id: int = None,
               context_images: list[str] | None = None) -> str:
    """主对话 — 用户50条历史 + 群聊100条上下文（含图片）"""
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

    # 注入人设知识库（单独一条，与主 prompt 分开避免过长）
    if PERSONA_CONTENT:
        messages.append({"role": "system", "content": PERSONA_CONTENT})

    # 3. 注入群聊上下文（最近100条，含图片）
    if group_id is not None:
        group_context_text, group_context_imgs = await _get_group_context(group_id, GROUP_CONTEXT_LIMIT)
        if group_context_text:
            all_ctx_imgs = (context_images or []) + group_context_imgs
            all_ctx_imgs = all_ctx_imgs[:10]
            if all_ctx_imgs:
                ctx_content: list = [{"type": "text", "text": group_context_text}]
                for img_url in all_ctx_imgs:
                    ctx_content.append({"type": "image_url", "image_url": {"url": img_url}})
                messages.append({"role": "user", "content": ctx_content})
            else:
                messages.append({"role": "user", "content": group_context_text})
            messages.append({"role": "assistant", "content": "好的，我了解了群里最近的动态。"})

    # 4. 用户历史 + 当前消息 (带昵称标签)
    messages.extend(history)
    tagged_msg = f"[{nickname}]: {user_message}"
    messages.append({"role": "user", "content": tagged_msg})

    # 5. 调用 API（主模型超时后自动兑底 DeepSeek）
    try:
        reply = await _call_primary(messages)
    except (httpx.TimeoutException, asyncio.TimeoutError):
        logger.warning(f"[ai_chat] 主模型超时（>{PRIMARY_TIMEOUT}s），切换兑底 {FALLBACK_MODEL}")
        try:
            reply = await _call_fallback(messages)
        except Exception as fe:
            _log_err("ai_chat/fallback", f"兑底调用失败: {fe}")
            return None
    except Exception as e:
        _log_err("ai_chat/chat", f"API 调用异常: {e}")
        # 非超时异常（400/429等）也尝试兑底
        if FALLBACK_URL and FALLBACK_KEY:
            logger.warning(f"[ai_chat] 主模型异常，尝试兑底 {FALLBACK_MODEL}")
            try:
                reply = await _call_fallback(messages)
            except Exception as fe:
                _log_err("ai_chat/fallback", f"兑底调用失败: {fe}")
                return None
        else:
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


async def _get_group_context(group_id: int, limit: int = 100) -> tuple[str | None, list[str]]:
    """从数据库获取最近群聊记录。
    返回 (text, image_urls):
    - text: 文字化群聊上下文，bot 自己的发言标注「凛(我): 」
    - image_urls: 最新10张图片 URL（按时间最新顺序）
    """
    session = await get_session()
    try:
        rows = (await session.execute(
            select(GroupMessage)
            .where(GroupMessage.group_id == group_id)
            .order_by(GroupMessage.id.desc())
            .limit(limit)
        )).scalars().all()
        if not rows:
            return None, []
        rows.reverse()
        lines = []
        img_urls: list[str] = []
        for r in rows:
            # B: bot 自己的发言用「凛(我):」前缀
            if getattr(r, "is_bot", False):
                lines.append(f"凛(我): {r.content}")
                continue
            if r.has_image and r.content:
                lines.append(f"{r.nickname}: [图片] {r.content}")
            elif r.has_image:
                lines.append(f"{r.nickname}: [发了图片]")
            else:
                lines.append(f"{r.nickname}: {r.content}")
            # A: 收集图片 URL
            img_url = getattr(r, "image_url", "") or ""
            if img_url:
                img_urls.append(img_url)
        # 只保留最新10张
        recent_imgs = img_urls[-10:]
        return "[最近的群聊记录，仅供参考]\n" + "\n".join(lines), recent_imgs
    finally:
        await session.close()


# ---------- 图片识别对话 ----------

async def chat_with_vision(user_id: int, nickname: str, user_message: str,
                           image_urls: list[str], group_id: int = None) -> str:
    """调用视觉模型识图，先下载图片转 base64 再发送。
    同时注入群聊文字上下文（历史图片已在当前 image_urls 里，此处只加文字）。
    """
    vision_cfg = ai_cfg.get("vision", {})
    api_url = vision_cfg.get("api_url", API_URL)
    api_key = vision_cfg.get("api_key", API_KEY)
    model = vision_cfg.get("model", "qwen3-vl-plus")

    content: list = []
    async with httpx.AsyncClient(timeout=30) as dl_client:
        for url in image_urls:
            try:
                img_resp = await dl_client.get(url)
                img_resp.raise_for_status()
                mime = img_resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
                b64 = base64.b64encode(img_resp.content).decode()
                data_url = f"data:{mime};base64,{b64}"
                content.append({"type": "image_url", "image_url": {"url": data_url}})
                logger.debug(f"[ai_chat/vision] 图片已下载转 base64 ({len(img_resp.content)//1024}KB)")
            except Exception as e:
                logger.warning(f"[ai_chat/vision] 图片下载失败，跳过: {e}")
    if not content:
        # 所有图片下载失败，降级为普通文字对话
        return await chat(user_id, nickname, user_message or "你看到我发图了吗", group_id)

    prompt = user_message if user_message else "描述一下这张图，然后用你一贯的风格评论一句"
    content.append({"type": "text", "text": f"[{nickname}]: {prompt}"})

    special_users = ai_cfg.get("special_users", {})
    system_prompt = ai_cfg["system_prompt"]
    if user_id in special_users:
        system_prompt += "\n" + special_users[user_id]
    system_prompt += f"\n\n[当前对话用户昵称: {nickname}]"

    messages = [{"role": "system", "content": system_prompt}]
    if PERSONA_CONTENT:
        messages.append({"role": "system", "content": PERSONA_CONTENT})

    # 注入群聊文字上下文（视觉调用时只追加文字，图片已在 content 里）
    if group_id is not None:
        group_context_text, _ = await _get_group_context(group_id, GROUP_CONTEXT_LIMIT)
        if group_context_text:
            messages.append({"role": "user", "content": group_context_text})
            messages.append({"role": "assistant", "content": "好的，我了解了群里最近的动态。"})

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
