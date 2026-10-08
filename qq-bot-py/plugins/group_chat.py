"""群聊监听插件 — 记录群消息 + 被动参与群聊

群消息存入 MySQL（带昵称），被动回复/识图时取最近75条群聊记录。
"""

import re
import json
import time
import random
import hashlib
import base64
import io
import asyncio
import pathlib
from datetime import datetime

import httpx
import yaml
import redis as redis_lib
from minio import Minio
from nonebot import on_message, get_driver
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot.rule import Rule
from nonebot.log import logger
from sqlalchemy import text, select, update

from .db import (
    engine,
    get_session,
    GroupMessage,
    prune_group_messages,
)
from .chat_coordination import (
    format_reply_context,
    mark_group_trigger,
    resolve_reply_context,
    send_group_reply_parts,
)
from .agent_context import CACHE, CachedMessage, image_bytes_to_jpeg
from .persona_manager import get_persona_content
from . import console_runtime


# ---------- 开기迁移：自动对已有表补列 ----------
@get_driver().on_startup  # type: ignore[attr-defined]
async def _migrate_group_messages():
    """ALTER TABLE 补加 image_url / is_bot 列，列已存在时跳过。"""
    sqls = [
        "ALTER TABLE group_messages ADD COLUMN image_url TEXT NULL",
        "ALTER TABLE group_messages ADD COLUMN is_bot TINYINT(1) NOT NULL DEFAULT 0",
        "ALTER TABLE group_messages ADD COLUMN message_id BIGINT NOT NULL DEFAULT 0",
        "ALTER TABLE group_messages ADD COLUMN raw_message TEXT NULL",
        "ALTER TABLE group_messages ADD COLUMN at_users TEXT NULL",
    ]
    fill_sqls = [
        "UPDATE group_messages SET message_id = 0 WHERE message_id IS NULL",
        "UPDATE group_messages SET raw_message = '' WHERE raw_message IS NULL",
        "UPDATE group_messages SET at_users = '[]' WHERE at_users IS NULL",
    ]
    async with engine.begin() as conn:
        for sql in sqls:
            try:
                await conn.execute(text(sql))
                logger.info(f"[group_chat] 迁移成功: {sql[:60]}")
            except Exception as e:
                err_str = str(e)
                if "Duplicate column" in err_str or "already exists" in err_str:
                    pass  # 列已存在，正常跳过
                else:
                    logger.error(f"[group_chat] 迁移执行失败: {e}")
        for sql in fill_sqls:
            try:
                await conn.execute(text(sql))
            except Exception as e:
                logger.error(f"[group_chat] 迁移默认值填充失败: {e}")


@get_driver().on_startup  # type: ignore[attr-defined]
async def _ensure_group_img_bucket():
    """Ensure the bucket exists and retain each Agent group's latest five images."""
    try:
        if not await asyncio.to_thread(_minio_group.bucket_exists, GROUP_IMG_BUCKET):
            await asyncio.to_thread(_minio_group.make_bucket, GROUP_IMG_BUCKET)
            logger.info(f"[group_chat] 已创建 bucket: {GROUP_IMG_BUCKET}")
        protected: set[str] = set()
        session = await get_session()
        try:
            for group_id in active_agent_groups():
                values = (await session.execute(
                    select(GroupMessage.image_url)
                    .where(
                        GroupMessage.group_id == int(group_id),
                        GroupMessage.image_url != "",
                    )
                    .order_by(GroupMessage.id.desc())
                    .limit(5)
                )).scalars().all()
                protected.update(str(value) for value in values if value)
        finally:
            await session.close()
        cutoff = int(time.time()) - 43200  # 12h
        def cleanup() -> int:
            removed = 0
            for obj in _minio_group.list_objects(GROUP_IMG_BUCKET):
                try:
                    ts = int(obj.object_name.split("_")[0])
                    if ts < cutoff and obj.object_name not in protected:
                        _minio_group.remove_object(GROUP_IMG_BUCKET, obj.object_name)
                        removed += 1
                except Exception:
                    pass
            return removed

        removed = await asyncio.to_thread(cleanup)
        if removed:
            logger.info(f"[group_chat] 清理过期群图片 {removed} 张")
    except Exception as e:
        logger.error(f"[group_chat] group-images bucket 初始化失败: {e}")


with open("config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

ai_cfg = config["ai"]
gc_cfg = ai_cfg["group_chat"]
rds = redis_lib.Redis(host=config["redis"]["host"], port=config["redis"]["port"], password=config["redis"].get("password") or None, db=int(config["redis"].get("db", 0)), decode_responses=True)
AGENT_GROUPS: set[int] = {
    int(g) for g in ((config.get("agent") or {}).get("active_groups", []) or [])
}
ENABLED_GROUPS: set[int] = set(AGENT_GROUPS)
LOCAL_DISABLED_GROUPS: set[int] = {
    int(g) for g in config.get("group_mode", {}).get("local_disabled_groups", [])
}

GROUP_CONTEXT_LIMIT = 500  # Agent 群持久化/热缓存窗口
REPEAT_ENABLED = bool(gc_cfg.get("repeat_enabled", False))
REPEAT_THRESHOLD = max(2, int(gc_cfg.get("repeat_threshold", 4)))
REPEAT_WINDOW_SECONDS = max(10, int(gc_cfg.get("repeat_window_seconds", 120)))
REPEAT_MAX_CHARS = max(1, int(gc_cfg.get("repeat_max_chars", 120)))
REPEAT_MIN_USERS = 2


def _load_group_persona() -> str:
    """启动时读取 persona/*.md，供被动群聊使用（OpenAI compat 路径，无 Context Cache）。"""
    base = pathlib.Path(ai_cfg.get("persona_dir", "persona"))
    if not base.is_dir():
        return ""
    parts = []
    for f in sorted(base.glob("*.md")):
        if not f.name.startswith("_"):
            try:
                t = f.read_text(encoding="utf-8").strip()
                if t:
                    parts.append(t)
            except Exception:
                pass
    return "\n\n---\n\n".join(parts)


_GROUP_PERSONA_TEXT = _load_group_persona()

# ── 群图片 MinIO 客户端（24h 归档）───────────────────────
_minio_cfg_g = config["meme"]["minio"]
_minio_group = Minio(
    _minio_cfg_g["endpoint"],
    access_key=_minio_cfg_g["access_key"],
    secret_key=_minio_cfg_g["secret_key"],
    secure=_minio_cfg_g.get("secure", False),
)
GROUP_IMG_BUCKET = _minio_cfg_g.get("group_img_bucket", "group-images")


async def _minio_upload_group_img(url: str) -> tuple[str, bytes] | None:
    """下载 QQ CDN 图片并上传到 MinIO，返回对象名和原始字节。"""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return None
            data = resp.content
        ts = int(time.time())
        md5 = hashlib.md5(data).hexdigest()[:8]
        ext = "png" if data[:4] == b'\x89PNG' else "gif" if data[:3] == b'GIF' else "jpg"
        obj_name = f"{ts}_{md5}.{ext}"
        await asyncio.to_thread(
            _minio_group.put_object,
            GROUP_IMG_BUCKET,
            obj_name,
            io.BytesIO(data),
            len(data),
            content_type=f"image/{ext}",
        )
        return obj_name, data
    except Exception as e:
        logger.debug(f"[group_chat] 群图片上传MinIO失败: {e}")
        return None


async def _upload_group_img_bg(group_id: int, message_id: int, row_id: int, qq_url: str) -> None:
    """后台上传并预处理图片，之后 Agent 请求直接命中内存缩略图。"""
    uploaded = await _minio_upload_group_img(qq_url)
    if not uploaded:
        return
    obj_name, raw_data = uploaded
    try:
        session = await get_session()
        try:
            await session.execute(
                update(GroupMessage).where(GroupMessage.id == row_id).values(image_url=obj_name)
            )
            await session.commit()
        finally:
            await session.close()
        CACHE.attach_image(group_id, message_id, obj_name)
        thumbnail = image_bytes_to_jpeg(raw_data)
        if thumbnail:
            CACHE.put_image_payload(group_id, obj_name, "image/jpeg", thumbnail)
        logger.info(
            f"[context_cache] image_prefetch group={group_id} message={message_id} "
            f"images={CACHE.image_count(group_id)}"
        )
    except Exception as e:
        logger.debug(f"[group_chat] 更新群图片MinIO路径失败: {e}")

_last_reply_time: dict[int, float] = {}


def _repeat_state_key(group_id: int) -> str:
    return f"group_chat:repeat:{int(group_id)}"


def reset_repeat_state(group_id: int) -> None:
    if not REPEAT_ENABLED:
        return
    try:
        rds.delete(_repeat_state_key(group_id))
    except Exception as e:
        logger.debug(f"[group_chat] 重置复读状态失败: {e}")


def _normalize_repeat_text(message: str) -> str:
    return re.sub(r"\s+", " ", (message or "").strip())


def _track_repeat_message(
    group_id: int,
    user_id: int,
    message: str,
    *,
    has_image: bool,
    at_users: list[int],
) -> bool:
    """记录连续纯文字；达到阈值且至少两人参与时返回 True。"""
    if not REPEAT_ENABLED:
        return False

    normalized = _normalize_repeat_text(message)
    if (
        not normalized
        or normalized.startswith("#")
        or has_image
        or at_users
        or len(normalized) > REPEAT_MAX_CHARS
    ):
        reset_repeat_state(group_id)
        return False

    key = _repeat_state_key(group_id)
    now = time.time()
    try:
        state = rds.hgetall(key)
        same_streak = (
            state.get("text") == normalized
            and now - float(state.get("last_at", 0) or 0) <= REPEAT_WINDOW_SECONDS
        )
        if same_streak:
            count = int(state.get("count", 0) or 0) + 1
            try:
                users = {int(item) for item in json.loads(state.get("users", "[]"))}
            except (TypeError, ValueError, json.JSONDecodeError):
                users = set()
            echoed = state.get("echoed") == "1"
        else:
            count = 1
            users = set()
            echoed = False

        users.add(int(user_id))
        should_echo = (
            count >= REPEAT_THRESHOLD
            and len(users) >= REPEAT_MIN_USERS
            and not echoed
        )
        rds.hset(key, mapping={
            "text": normalized,
            "count": count,
            "users": json.dumps(sorted(users)),
            "last_at": now,
            "echoed": "1" if echoed or should_echo else "0",
        })
        rds.expire(key, max(300, REPEAT_WINDOW_SECONDS * 2))
        return should_echo
    except Exception as e:
        logger.debug(f"[group_chat] 复读状态更新失败: {e}")
        return False


async def _send_repeat_message(bot: Bot, group_id: int, message: str) -> None:
    """Send the repeated text after a repeat streak without invoking the Agent."""
    _last_reply_time[group_id] = time.time()
    try:
        async with console_runtime.chat_turn(bot, group_id, 0, source="chat"):
            await bot.send_group_msg(group_id=group_id, message=message)
    except console_runtime.ChatBlocked:
        return
    asyncio.create_task(record_bot_reply(group_id, message))
    logger.info(
        f"[group_chat] 跟随复读 group={group_id} "
        f"threshold={REPEAT_THRESHOLD} text={message[:40]!r}"
    )


def _build_group_system_prompt() -> str:
    """为被动群聊构建 system prompt（读取 persona/*.md 文件）。"""
    return get_persona_content()


def is_group_enabled(group_id: int) -> bool:
    group_id = int(group_id)
    return console_runtime.group_enabled(group_id, fallback=group_id in ENABLED_GROUPS) and group_id not in LOCAL_DISABLED_GROUPS


def active_agent_groups() -> set[int]:
    if console_runtime.ready():
        return {gid for gid in console_runtime.SETTINGS.active_groups() if is_group_enabled(gid)}
    return {gid for gid in ENABLED_GROUPS if is_group_enabled(gid)}


def _agent_scope_allows_event(bot: Bot, event: GroupMessageEvent) -> bool:
    if str(event.user_id) == str(bot.self_id):
        return False
    direct_at = int(bot.self_id) in _extract_at_users(event)
    if console_runtime.ready():
        return console_runtime.scope_allows(int(event.group_id), direct_at=direct_at)
    from .dev_scope import _scope_allows_group

    return _scope_allows_group(int(event.group_id), "", mentions_bot=direct_at)


def _is_hash_command(text: str) -> bool:
    return str(text or "").lstrip().startswith(("#", "＃"))


def _is_explicit_agent_trigger(bot: Bot, event: GroupMessageEvent) -> bool:
    if bool(getattr(event, "to_me", False)):
        return True
    raw = str(getattr(event, "raw_message", "") or "").strip()
    raw = re.sub(
        r"^(?:\[(?:CQ:)?(?:reply|at)(?:[:,][^\]]*)?\]\s*)+",
        "",
        raw,
        flags=re.IGNORECASE,
    )
    try:
        from .persona_manager import get_active_persona_name

        persona_name = re.escape(get_active_persona_name())
    except Exception:
        persona_name = "远坂凛"
    if re.match(
        rf"^(?:远坂凛|凛|rin|{persona_name})(?:\s|[，,。！？!?：:]|$)",
        raw,
        flags=re.IGNORECASE,
    ):
        return True
    reply = getattr(event, "reply", None)
    sender = getattr(reply, "sender", None)
    try:
        reply_user_id = int(getattr(sender, "user_id", 0) or sender.get("user_id", 0))
    except (AttributeError, TypeError, ValueError):
        reply_user_id = 0
    return reply_user_id == int(bot.self_id)


def _extract_at_users(event: GroupMessageEvent) -> list[int]:
    users: list[int] = []
    for seg in event.message:
        if seg.type != "at":
            continue
        qq = str(seg.data.get("qq", "")).strip()
        if not qq or qq.lower() == "all":
            continue
        try:
            user_id = int(qq)
        except ValueError:
            continue
        if user_id not in users:
            users.append(user_id)

    raw = (getattr(event, "raw_message", "") or "") + "\n" + str(event.message)
    for qq in re.findall(r"\[(?:CQ:)?at[:,]qq=(\d+)(?:[,\]])", raw):
        try:
            user_id = int(qq)
        except ValueError:
            continue
        if user_id not in users:
            users.append(user_id)
    return users


def not_at_bot_rule() -> Rule:
    async def _rule(bot: Bot, event: GroupMessageEvent) -> bool:
        console_runtime.observe_incoming(bot, event)
        return is_group_enabled(event.group_id) and _agent_scope_allows_event(bot, event) and int(bot.self_id) not in _extract_at_users(event)
    return Rule(_rule)


def at_bot_rule() -> Rule:
    async def _rule(bot: Bot, event: GroupMessageEvent) -> bool:
        console_runtime.observe_incoming(bot, event)
        group_id = event.group_id
        if not is_group_enabled(group_id) or not _agent_scope_allows_event(bot, event):
            return False
        return int(bot.self_id) in _extract_at_users(event)

    return Rule(_rule)


async def _content_with_reply_author(bot: Bot, event: GroupMessageEvent, plain: str) -> str:
    """Store reply attribution in the text so later group context keeps it."""
    context = await resolve_reply_context(bot, event)
    return format_reply_context(context, plain)


# Run before command matchers so Agent-group commands are still captured in the
# 500-message recovery window, then return without feeding them to the Agent.
group_at_listener = on_message(rule=at_bot_rule(), priority=0, block=False)
group_listener = on_message(rule=not_at_bot_rule(), priority=0, block=False)


@group_at_listener.handle()
async def handle_at_bot_group_msg(bot: Bot, event: GroupMessageEvent):
    console_runtime.observe_incoming(bot, event)
    if not is_group_enabled(event.group_id) or not _agent_scope_allows_event(bot, event):
        return
    plain = event.get_plaintext().strip()

    group_id = event.group_id
    reset_repeat_state(group_id)
    user_id = event.user_id
    at_users = _extract_at_users(event)
    has_image = any(seg.type == "image" for seg in event.message)
    if not (plain or has_image or at_users):
        return

    try:
        member = await bot.get_group_member_info(group_id=group_id, user_id=user_id)
        nickname = member.get("card") or member.get("nickname") or str(user_id)
    except Exception:
        nickname = str(user_id)
    stored_content = await _content_with_reply_author(bot, event, plain)

    first_image_url = next(
        (seg.data.get("url") or seg.data.get("file", "")
         for seg in event.message
         if seg.type == "image" and (seg.data.get("url") or seg.data.get("file", ""))),
        "",
    )

    try:
        session = await get_session()
        try:
            msg_obj = GroupMessage(
                group_id=group_id,
                message_id=event.message_id,
                user_id=user_id,
                nickname=nickname,
                content=stored_content,
                raw_message=event.raw_message,
                at_users=json.dumps(at_users, ensure_ascii=False),
                has_image=has_image,
                image_url="",
            )
            session.add(msg_obj)
            await session.flush()
            row_id = msg_obj.id
            await prune_group_messages(group_id, session=session)
            await session.commit()
            if has_image and first_image_url:
                asyncio.create_task(_upload_group_img_bg(group_id, event.message_id, row_id, first_image_url))
        finally:
            await session.close()
    except Exception as e:
        logger.warning(f"[group_chat] 存储@bot群消息失败: {e}")

    cached_record = CachedMessage(
        group_id=group_id,
        message_id=int(event.message_id),
        user_id=int(user_id),
        nickname=nickname,
        content=stored_content,
        raw_message=event.raw_message,
        at_users=tuple(at_users),
        has_image=has_image,
        created_at=datetime.now(),
    )
    CACHE.add(cached_record)
    CACHE.add_owner_message(cached_record)
    CACHE.prefetch_avatar(user_id)

    if _is_hash_command(plain):
        logger.debug(f"[router] group={group_id} route=command")
        return

    # Agent 群聊模式接管明确 @。不阻断后续表情包监听器，保留原来的
    # “收到消息后概率发一张表情包”行为；旧文字聊天 matcher 已在 Agent
    # 群自行跳过，不会产生第二次文字回复。
    try:
        from .agent_runtime import agent_group_enabled, enqueue_group_event

        if agent_group_enabled(group_id):
            await enqueue_group_event(bot, event, force_reply=True)
            return
    except Exception as e:
        logger.warning(f"[group_chat] Agent @入队失败: {type(e).__name__}: {e}")
        return


@group_listener.handle()
async def handle_group_msg(bot: Bot, event: GroupMessageEvent):
    console_runtime.observe_incoming(bot, event)
    group_id = event.group_id
    if not is_group_enabled(group_id) or not _agent_scope_allows_event(bot, event):
        return

    plain = event.get_plaintext().strip()
    user_id = event.user_id
    # 获取昵称
    try:
        member = await bot.get_group_member_info(group_id=group_id, user_id=user_id)
        nickname = member.get("card") or member.get("nickname") or str(user_id)
    except Exception:
        nickname = str(user_id)
    stored_content = await _content_with_reply_author(bot, event, plain)

    has_image = any(seg.type == "image" for seg in event.message)
    at_users = _extract_at_users(event)
    # 提取第一张图的 URL，存入 DB 供 AI 上下文使用
    first_image_url = next(
        (seg.data.get("url") or seg.data.get("file", "")
         for seg in event.message
         if seg.type == "image" and (seg.data.get("url") or seg.data.get("file", ""))),
        "",
    )

    # Agent 群消息全部持久化；非 Agent 群不会进入本监听器。
    if plain or has_image or at_users:
        try:
            session = await get_session()
            try:
                msg_obj = GroupMessage(
                    group_id=group_id,
                    message_id=event.message_id,
                    user_id=user_id,
                    nickname=nickname,
                    content=stored_content,
                    raw_message=event.raw_message,
                    at_users=json.dumps(at_users, ensure_ascii=False),
                    has_image=has_image,
                    image_url="",  # 由后台任务上传MinIO后更新
                )
                session.add(msg_obj)
                await session.flush()  # populate msg_obj.id
                row_id = msg_obj.id
                await prune_group_messages(group_id, session=session)
                await session.commit()
                if has_image and first_image_url:
                    asyncio.create_task(_upload_group_img_bg(group_id, event.message_id, row_id, first_image_url))
            finally:
                await session.close()
        except Exception as e:
            logger.warning(f"[group_chat] 存储群消息失败: {e}")

    cached_record = CachedMessage(
        group_id=group_id,
        message_id=int(event.message_id),
        user_id=int(user_id),
        nickname=nickname,
        content=stored_content,
        raw_message=event.raw_message,
        at_users=tuple(at_users),
        has_image=has_image,
        created_at=datetime.now(),
    )
    CACHE.add(cached_record)
    CACHE.add_owner_message(cached_record)
    CACHE.prefetch_avatar(user_id)

    if _is_hash_command(plain):
        logger.debug(f"[router] group={group_id} route=command")
        return

    # Agent 模式下不再按 passive_rate/keyword_rate 随机触发。
    try:
        from .agent_runtime import agent_group_enabled, enqueue_group_event

        if agent_group_enabled(group_id):
            if plain or has_image or at_users:
                if _track_repeat_message(
                    group_id,
                    user_id,
                    plain,
                    has_image=has_image,
                    at_users=at_users,
                ):
                    await _send_repeat_message(bot, group_id, plain)
                    return
                await enqueue_group_event(
                    bot,
                    event,
                    force_reply=_is_explicit_agent_trigger(bot, event),
                )
            return
    except Exception as e:
        logger.warning(f"[group_chat] Agent群聊入队失败: {type(e).__name__}: {e}")
        return

    # Agent 未接管时才运行旧的复读、识图和随机被动回复链路。
    # 关闭总开关后仍保留上面的消息入库，不再产生任何旧式自动回复。
    if not bool(gc_cfg.get("legacy_passive_enabled", False)):
        return

    # 提取图片URL
    image_urls = [
        seg.data.get("url") or seg.data.get("file", "")
        for seg in event.message
        if seg.type == "image" and (seg.data.get("url") or seg.data.get("file", ""))
    ]

    if _track_repeat_message(
        group_id,
        user_id,
        plain,
        has_image=has_image,
        at_users=at_users,
    ):
        await _send_repeat_message(bot, group_id, plain)
        return

    # 群内图片概率识图回复（支持按群覆盖，优先读 group_overrides[group_id]）
    group_overrides = gc_cfg.get("group_overrides", {}) or {}
    group_cfg = group_overrides.get(group_id) or group_overrides.get(str(group_id)) or {}
    vision_rate = group_cfg.get("vision_image_rate", gc_cfg.get("vision_image_rate", 0.0))
    if image_urls and vision_rate > 0 and not _is_cooling(group_id) and random.random() < vision_rate:
        rapid_chat_trigger = mark_group_trigger(group_id)
        reply = await vision_comment(group_id, image_urls, plain, nickname)
        if reply:
            _last_reply_time[group_id] = time.time()
            parts = [p for p in reply.splitlines() if p.strip()]
            await send_group_reply_parts(
                bot,
                group_id,
                parts or [reply],
                reply_message_id=event.message_id,
                force_quote=rapid_chat_trigger,
            )
            for part in parts:
                asyncio.create_task(record_bot_reply(group_id, part))
            rds.setex(f"meme:cd:{group_id}", config.get("meme", {}).get("cooldown", 60), "1")
        return

    # 常规被动文字回复
    if not plain or plain.startswith("#"):
        return

    if not should_trigger(group_id, plain):
        return

    rapid_chat_trigger = mark_group_trigger(group_id)
    reply = await passive_reply(group_id, trigger_nickname=nickname)
    if reply:
        parts = [p for p in reply.splitlines() if p.strip()]
        await send_group_reply_parts(
            bot,
            group_id,
            parts or [reply],
            reply_message_id=event.message_id,
            force_quote=rapid_chat_trigger,
        )
        for part in parts:
            asyncio.create_task(record_bot_reply(group_id, part))


async def record_bot_reply(group_id: int, content: str) -> None:
    """将 bot 自己的回复写入群消息历史，供 AI 上下文感知自己说过的话。"""
    if not is_group_enabled(group_id):
        return

    try:
        session = await get_session()
        try:
            session.add(GroupMessage(
                group_id=group_id,
                message_id=0,
                user_id=0,
                nickname="凛",
                content=content,
                raw_message=content,
                at_users="[]",
                has_image=False,
                image_url="",
                is_bot=True,
            ))
            await prune_group_messages(group_id, session=session)
            await session.commit()
        finally:
            await session.close()
    except Exception as e:
        logger.debug(f"[group_chat] record_bot_reply 失败: {e}")


def should_trigger(group_id: int, message: str) -> bool:
    if not bool(gc_cfg.get("legacy_passive_enabled", False)):
        return False
    cooldown = gc_cfg.get("cooldown_seconds", 10)
    last = _last_reply_time.get(group_id, 0)
    if time.time() - last < cooldown:
        return False

    _overrides = (gc_cfg.get("group_overrides", {}) or {})
    _grp = _overrides.get(group_id) or _overrides.get(str(group_id)) or {}
    rate = _grp.get("passive_rate", gc_cfg.get("passive_rate", 0.0))
    for kw in gc_cfg.get("keywords", []):
        if kw in message:
            rate = _grp.get("keyword_rate", gc_cfg.get("keyword_rate", 0.1))
            break

    return random.random() < rate


def _is_cooling(group_id: int) -> bool:
    cooldown = gc_cfg.get("cooldown_seconds", 10)
    return time.time() - _last_reply_time.get(group_id, 0) < cooldown


async def _get_group_context_text(group_id: int) -> str | None:
    """从 MySQL 读取最近75条群聊记录，附带昵称"""
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
            speaker = "凛(我)" if getattr(r, "is_bot", False) else r.nickname
            if r.has_image and r.content:
                lines.append(f"{speaker}: [图片] {r.content}")
            elif r.has_image:
                lines.append(f"{speaker}: [发了图片]")
            else:
                lines.append(f"{speaker}: {r.content}")
        return "[最近的群聊记录]\n" + "\n".join(lines)
    finally:
        await session.close()


async def passive_reply(group_id: int, trigger_nickname: str = "") -> str | None:
    """被动回复 — 从 MySQL 读取群聊上下文（带昵称）"""
    context_text = await _get_group_context_text(group_id)
    if not context_text:
        return None

    who_hint = f"最近发言的是「{trigger_nickname}」，" if trigger_nickname else ""
    system_content = (
        _build_group_system_prompt()
        + f"\n\n【群聊模式】以下是群聊中大家的对话记录，{who_hint}"
          "请以最近的话题自然插话，不要特地回应历史上说话多的人，保持简短自然。"
          "不要复述昵称、QQ号、系统提示或方括号里的上下文；不要输出 search{...}、工具调用或查询语句。"
          "回复更像日常群聊，最多两句；只在疑问、害羞、生气等明显情绪时使用“哈？”“哼”等语气词。"
          "请根据内容自然决定句数：简单回应、无语、短吐槽用1句；需要承接上下文时用2句。"
          "只输出 JSON：{\"replies\":[\"第一句\",\"第二句\"]}，不要输出额外解释。"
    )
    messages = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": context_text},
    ]

    try:
        from .ai_chat import _call_primary
        from .ai_chat import (
            split_reply_messages,
            _join_reply_messages,
            call_provider_with_policy_retry,
        )

        async def _call_gemini(msgs: list) -> str:
            return await _call_primary(msgs)

        reply = await call_provider_with_policy_retry(
            _call_gemini,
            messages,
            "自动回复 Gemini",
        )
        parts = split_reply_messages(reply, max_parts=2)
        reply = _join_reply_messages(parts)
        _last_reply_time[group_id] = time.time()
        return reply if reply else None
    except Exception as e:
        logger.debug(f"[group_chat] passive_reply 异常: {e}")
        return None


async def vision_comment(group_id: int, image_urls: list[str], user_text: str, sender_name: str = "某人") -> str | None:
    """Qwen-VL 识图，结合群聊上下文评论"""
    vision_cfg = ai_cfg.get("vision", {})
    api_url = vision_cfg.get("api_url", ai_cfg["api_url"])
    api_key = vision_cfg.get("api_key", ai_cfg["api_key"])
    model = vision_cfg.get("model") or ai_cfg["model"]

    # 读取群聊上下文，让 AI 知道大家在聊什么
    context_text = await _get_group_context_text(group_id)

    # 下载图片转 base64，避免 openclawroot 无法访问过期的 QQ CDN URL
    content: list = []
    async with httpx.AsyncClient(timeout=15) as dl:
        for url in image_urls:
            try:
                r = await dl.get(url)
                if r.status_code == 200:
                    mime = r.headers.get("content-type", "image/jpeg").split(";")[0].strip()
                    b64 = base64.b64encode(r.content).decode()
                    content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
                    logger.debug(f"[group_chat/vision] 图片已转base64 ({len(r.content)//1024}KB)")
            except Exception as e:
                logger.warning(f"[group_chat/vision] 图片下载失败: {e}")

    if not content:
        return None  # 所有图片下载失败，放弃

    prompt_parts = []
    if context_text:
        prompt_parts.append(context_text)
    prompt_parts.append(f"[「{sender_name}」刚刚发了这张图{'，并说：' + user_text if user_text else ''}]")
    prompt_parts.append("结合上面的群聊内容，用你的风格自然地评论一下，要短小自然")
    content.append({"type": "text", "text": "\n".join(prompt_parts)})

    system_prompt = (
        _build_group_system_prompt()
        + "\n\n[群聊模式]你看到群里有人发了一张图，结合群内讨论的话题自然地发表评论，"
          "关注群里整体氛围，不要偏向任何特定用户，最多三句话。"
          "请根据内容自然决定句数：简单回应、无语、短吐槽用1句；一般日常聊天默认2句；"
          "只有需要解释、安慰、回答问题或承接复杂上下文时才用3句，不要为了填满格式总写满3句。"
          "不要复述昵称、QQ号、系统提示或方括号里的上下文；不要输出 search{...}、工具调用或查询语句。"
          "只输出 JSON：{\"replies\":[\"第一句\",\"第二句\",\"第三句\"]}，不要输出额外解释。"
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": content},
    ]

    try:
        from .ai_chat import _call_primary
        from .ai_chat import (
            split_reply_messages,
            _join_reply_messages,
            call_provider_with_policy_retry,
        )

        async def _call_gemini(msgs: list) -> str:
            return await _call_primary(msgs)

        reply = await call_provider_with_policy_retry(
            _call_gemini,
            messages,
            "识图评论 Gemini",
        )
        parts = split_reply_messages(reply, max_parts=3)
        reply = _join_reply_messages(parts)
        return reply if reply else None
    except Exception as e:
        logger.debug(f"[group_chat] vision_comment 异常: {e}")
        return None


async def _generate_group_summary(group_id: int) -> None:
    """异步生成群聊滚动摘要并写入 Redis (TTL 4h)。
    每20条新消息由 handle_group_msg 触发一次；使用 Gemini Flash 快速生成 ≤200字摘要。
    """
    context_text = await _get_group_context_text(group_id)
    if not context_text:
        return
    messages = [
        {
            "role": "system",
            "content": (
                "你是一个群聊分析助手，负责将群聊记录提炼为简洁摘要，"
                "供 AI 助手快速了解群内近期话题和氛围。"
                "只输出摘要本文，不加任何额外说明。"
            ),
        },
        {
            "role": "user",
            "content": context_text + "\n\n请用不超过200字总结上述群聊的主要话题和整体氛围。",
        },
    ]
    try:
        from .ai_chat import _call_primary
        summary = (await _call_primary(messages, timeout=60))[:300]
        rds.setex(f"group_summary:{group_id}", 14400, summary)
        logger.info(f"[group_chat] 群 {group_id} 摘要已更新 ({len(summary)} 字)")
    except Exception as e:
        logger.warning(f"[group_chat] 群 {group_id} 摘要生成失败: {e}")
