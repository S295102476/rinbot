import hashlib
import random
import io
import yaml
import httpx
import redis as redis_lib
from minio import Minio
from nonebot import on_message, get_driver
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageSegment
from nonebot.rule import Rule
from nonebot.log import logger

# ── 配置 ──────────────────────────────────────────────
with open("config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

meme_cfg = config["meme"]
minio_cfg = meme_cfg["minio"]
gemini_cfg = meme_cfg.get("gemini", {})

rds = redis_lib.Redis(
    host=config["redis"]["host"],
    port=config["redis"]["port"],
    decode_responses=True,
)

COLLECT_RATE = meme_cfg.get("collect_rate", 0.10)
SEND_ON_IMAGE_RATE = meme_cfg.get("send_on_image_rate", 0.15)
SEND_ON_TEXT_RATE = meme_cfg.get("send_on_text_rate", 0.05)
COOLDOWN = meme_cfg.get("cooldown", 60)
MAX_POOL = meme_cfg.get("max_pool_size", 1000)
MIN_SIZE = meme_cfg.get("min_size_kb", 10) * 1024       # → bytes
MAX_SIZE = meme_cfg.get("max_size_mb", 3) * 1024 * 1024  # → bytes
BUCKET = minio_cfg["bucket"]

# 情绪相关配置
GEMINI_API_KEY = gemini_cfg.get("api_key", "")
GEMINI_BASE_URL = gemini_cfg.get("base_url", "https://openclawroot.com/v1")
DETECTOR_MODEL = gemini_cfg.get("detector_model", "gemini-2.0-flash")
CONTEXT_SIZE = gemini_cfg.get("context_size", 8)          # 用于情绪判断的消息条数
EMOTION_SEND_RATE = gemini_cfg.get("emotion_send_rate", 0.6)  # 使用情绪选图的概率

POOL_KEY = "meme:pool"           # Redis SET，存所有 object name
CHAT_KEY_PREFIX = "meme:chat:"   # Redis LIST，存群聊上下文
TAG_PREFIX = "meme:tags:"        # Redis STRING，存单个表情的情绪标签
EMOTION_PREFIX = "meme:emotion:" # Redis SET，按情绪分组的表情池

EMOTIONS = ["happy", "sad", "angry", "surprised", "funny", "cool", "disgusted", "confused", "neutral"]

# ── MinIO 客户端 ──────────────────────────────────────
minio_client = Minio(
    minio_cfg["endpoint"],
    access_key=minio_cfg["access_key"],
    secret_key=minio_cfg["secret_key"],
    secure=minio_cfg.get("secure", False),
)


@get_driver().on_startup
async def _ensure_bucket():
    """启动时确保 bucket 存在，并同步 MinIO 中已有对象到 Redis"""
    try:
        if not minio_client.bucket_exists(BUCKET):
            minio_client.make_bucket(BUCKET)
            logger.info(f"[meme] 已创建 MinIO bucket: {BUCKET}")
        else:
            logger.info(f"[meme] MinIO bucket 已就绪: {BUCKET}")

        # 同步 MinIO 对象列表到 Redis
        objects = list(minio_client.list_objects(BUCKET))
        if objects:
            names = [obj.object_name for obj in objects]
            rds.delete(POOL_KEY)
            rds.sadd(POOL_KEY, *names)
            logger.info(f"[meme] 已同步 {len(names)} 个对象到表情池")
        else:
            logger.info("[meme] MinIO bucket 为空，表情池清零")
            rds.delete(POOL_KEY)
    except Exception as e:
        logger.error(f"[meme] MinIO 连接/同步失败: {e}")


# ── 工具函数 ──────────────────────────────────────────

def _cd_key(group_id: int) -> str:
    return f"meme:cd:{group_id}"


def _is_cooling(group_id: int) -> bool:
    return rds.exists(_cd_key(group_id)) == 1


def _set_cooldown(group_id: int):
    rds.setex(_cd_key(group_id), COOLDOWN, "1")


def _presigned_url(object_name: str) -> str:
    """生成 1 小时有效的 presigned GET URL"""
    from datetime import timedelta
    return minio_client.presigned_get_object(BUCKET, object_name, expires=timedelta(hours=1))


async def _download_image(url: str) -> bytes | None:
    """下载图片并返回 bytes，若超出大小限制返回 None"""
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return None
            data = resp.content
            if len(data) < MIN_SIZE or len(data) > MAX_SIZE:
                return None
            return data
    except Exception:
        return None


def _upload_to_minio(data: bytes, ext: str) -> str | None:
    """上传图片到 MinIO，返回 object name，重复则跳过"""
    md5 = hashlib.md5(data).hexdigest()
    object_name = f"{md5}.{ext}"

    # 检查 Redis 是否已有（比查 MinIO 快）
    if rds.sismember(POOL_KEY, object_name):
        return None  # 重复，不再存

    try:
        minio_client.put_object(
            BUCKET,
            object_name,
            io.BytesIO(data),
            length=len(data),
            content_type=f"image/{ext}",
        )
    except Exception as e:
        logger.error(f"[meme] MinIO 上传失败: {e}")
        return None

    rds.sadd(POOL_KEY, object_name)

    # 超容量时随机淘汰
    pool_size = rds.scard(POOL_KEY)
    if pool_size > MAX_POOL:
        to_remove = rds.srandmember(POOL_KEY)
        if to_remove:
            rds.srem(POOL_KEY, to_remove)
            try:
                minio_client.remove_object(BUCKET, to_remove)
            except Exception:
                pass

    logger.info(f"[meme] 已收集表情包: {object_name} (pool: {min(pool_size, MAX_POOL)})")
    return object_name


def _get_random_meme_url() -> str | None:
    """从表情池随机取一张，返回 presigned URL"""
    obj = rds.srandmember(POOL_KEY)
    if not obj:
        return None
    return _presigned_url(obj)


# ── 情绪感知工具函数 ──────────────────────────────────

def _chat_key(group_id: int) -> str:
    return f"{CHAT_KEY_PREFIX}{group_id}"


def _push_chat_message(group_id: int, text: str):
    """将文本消息追加到群聊上下文列表（最多保留 CONTEXT_SIZE*2 条）"""
    key = _chat_key(group_id)
    rds.rpush(key, text)
    rds.ltrim(key, -(CONTEXT_SIZE * 2), -1)
    rds.expire(key, 3600)  # 1 小时后自动过期


def _get_recent_messages(group_id: int) -> list[str]:
    """取最近 CONTEXT_SIZE 条消息"""
    key = _chat_key(group_id)
    messages = rds.lrange(key, -CONTEXT_SIZE, -1)
    return messages or []


async def _detect_emotion(messages: list[str]) -> str:
    """
    调用 Gemini Flash，根据最近聊天内容判断当前群聊情绪。
    返回 EMOTIONS 中的一个标签，失败时返回 "neutral"。
    """
    if not GEMINI_API_KEY or not messages:
        return "neutral"

    chat_text = "\n".join(f"- {m}" for m in messages)
    prompt = (
        f"以下是一个群聊的最近几条消息：\n{chat_text}\n\n"
        f"请判断当前群聊的整体情绪氛围，从以下选项中选一个：\n"
        f"{', '.join(EMOTIONS)}\n"
        f"只回复一个英文单词，不要解释。"
    )

    payload = {
        "model": DETECTOR_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 10,
        "temperature": 0,
    }
    headers = {
        "Authorization": f"Bearer {GEMINI_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{GEMINI_BASE_URL}/chat/completions",
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
            label = resp.json()["choices"][0]["message"]["content"].strip().lower()
            label = label.split()[0].rstrip(".,!?;:")
            if label not in EMOTIONS:
                logger.debug(f"[meme] 情绪检测返回未知标签 '{label}'，使用 neutral")
                return "neutral"
            return label
    except Exception as e:
        logger.debug(f"[meme] 情绪检测失败: {e}")
        return "neutral"


def _get_emotion_meme_url(emotion: str) -> str | None:
    """
    从该情绪对应的 Redis SET 中随机取一张表情包的 presigned URL。
    若该情绪没有可用表情，回退到完全随机。
    """
    emotion_key = f"{EMOTION_PREFIX}{emotion}"
    obj = rds.srandmember(emotion_key)
    if not obj:
        logger.debug(f"[meme] 情绪 '{emotion}' 下无表情包，回退到随机")
        return _get_random_meme_url()
    # 确认该对象仍在总池中（避免被淘汰后残留）
    if not rds.sismember(POOL_KEY, obj):
        rds.srem(emotion_key, obj)
        return _get_random_meme_url()
    return _presigned_url(obj)


async def get_reply_meme_url(reply_text: str) -> str | None:
    """
    供 ai_chat 调用：根据 AI 回复文本推断情绪，返回匹配表情包的 presigned URL。
    表情池为空或 API key 未配置时返回 None，异常时回退到完全随机。
    """
    if rds.scard(POOL_KEY) == 0:
        return None
    tagged_total = sum(rds.scard(f"{EMOTION_PREFIX}{em}") for em in EMOTIONS)
    if tagged_total > 0 and reply_text:
        try:
            emotion = await _detect_emotion([reply_text])
            logger.debug(f"[meme] AI回复情绪: {emotion}")
            return _get_emotion_meme_url(emotion)
        except Exception as e:
            logger.debug(f"[meme] AI回复情绪检测失败，回退随机: {e}")
    return _get_random_meme_url()


# ── 不响应 bot 自己 ───────────────────────────────────

def _not_self_rule() -> Rule:
    async def _rule(bot: Bot, event: GroupMessageEvent) -> bool:
        return str(event.user_id) != str(bot.self_id)
    return Rule(_rule)


# ── Handler ───────────────────────────────────────────

meme_handler = on_message(rule=_not_self_rule(), priority=11, block=False)


def _is_meme(seg) -> bool:
    """判断图片 segment 是否为表情包（双重检查 sub_type 和 summary）"""
    sub_type = seg.data.get("sub_type")
    summary = seg.data.get("summary", "")
    # sub_type=1 表示表情包/贴纸；summary 含"动画表情"也是表情包
    return str(sub_type) == "1" or "动画表情" in summary


@meme_handler.handle()
async def handle_meme(bot: Bot, event: GroupMessageEvent):
    # 提取所有图片 segment
    image_segs = [seg for seg in event.message if seg.type == "image"]
    has_image = len(image_segs) > 0

    # 收集只针对表情包（sub_type=1 或 summary 含"动画表情"）
    meme_segs = [seg for seg in image_segs if _is_meme(seg)]

    # ── 追踪群聊文本（用于情绪推断）──
    group_id = event.group_id
    text_parts = [seg.data.get("text", "").strip()
                  for seg in event.message if seg.type == "text"]
    plain_text = " ".join(p for p in text_parts if p)
    if plain_text:
        _push_chat_message(group_id, plain_text)

    # ── 收集逻辑 ──
    if meme_segs:
        roll = random.random()
        logger.debug(f"[meme] 收集骰子: {roll:.2f}, 阈值: {COLLECT_RATE}")
        if roll < COLLECT_RATE:
            for seg in meme_segs:
                url = seg.data.get("url") or seg.data.get("file", "")
                logger.debug(f"[meme] 图片URL: {url[:80]}...")
                if not url:
                    logger.debug("[meme] URL为空，跳过")
                    continue
                data = await _download_image(url)
                if data is None:
                    logger.warning(f"[meme] 下载失败或图片大小不符合要求")
                    continue
                logger.debug(f"[meme] 下载完成, 大小: {len(data)} bytes")
                # 从 URL 猜后缀，默认 jpg
                ext = "jpg"
                if "png" in url.lower():
                    ext = "png"
                elif "gif" in url.lower():
                    ext = "gif"
                result = _upload_to_minio(data, ext)
                if result:
                    logger.info(f"[meme] 收集成功: {result}")
                else:
                    logger.debug("[meme] 上传跳过（重复或失败）")

    # ── 发送逻辑 ──
    if _is_cooling(group_id):
        logger.debug(f"[meme] 群 {group_id} 冷却中，跳过发送")
        return

    # 表情池为空则跳过
    pool_size = rds.scard(POOL_KEY)
    if pool_size == 0:
        logger.debug("[meme] 表情池为空，跳过发送")
        return

    # 根据消息类型确定触发概率
    rate = SEND_ON_IMAGE_RATE if has_image else SEND_ON_TEXT_RATE
    roll = random.random()
    logger.debug(f"[meme] 发送骰子: {roll:.2f}, 阈值: {rate}, pool: {pool_size}")
    if roll >= rate:
        return

    # ── 情绪感知选图 ──
    meme_url: str | None = None
    tagged_total = sum(rds.scard(f"{EMOTION_PREFIX}{em}") for em in EMOTIONS)

    if tagged_total > 0 and random.random() < EMOTION_SEND_RATE:
        # 已有分类数据，且命中情绪发送概率
        recent = _get_recent_messages(group_id)
        if recent:
            emotion = await _detect_emotion(recent)
            logger.info(f"[meme] 检测到群 {group_id} 情绪: {emotion}，尝试情绪选图")
            meme_url = _get_emotion_meme_url(emotion)
        else:
            meme_url = _get_random_meme_url()
    else:
        meme_url = _get_random_meme_url()

    if not meme_url:
        logger.warning("[meme] 获取 presigned URL 失败")
        return

    logger.info(f"[meme] 准备发送表情包到群 {group_id}, URL: {meme_url[:80]}...")
    try:
        await bot.send_group_msg(
            group_id=group_id,
            message=MessageSegment.image(meme_url),
        )
        _set_cooldown(group_id)
        logger.info(f"[meme] 已在群 {group_id} 发送表情包")
    except Exception as e:
        logger.error(f"[meme] 发送表情包失败: {e}")
