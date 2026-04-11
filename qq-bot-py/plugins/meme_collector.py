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

POOL_KEY = "meme:pool"  # Redis SET，存所有 object name

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


# ── 不响应 bot 自己 ───────────────────────────────────

def _not_self_rule() -> Rule:
    async def _rule(bot: Bot, event: GroupMessageEvent) -> bool:
        return str(event.user_id) != str(bot.self_id)
    return Rule(_rule)


# ── Handler ───────────────────────────────────────────

meme_handler = on_message(rule=_not_self_rule(), priority=11, block=False)


@meme_handler.handle()
async def handle_meme(bot: Bot, event: GroupMessageEvent):
    # 提取所有图片 segment
    image_segs = [seg for seg in event.message if seg.type == "image"]
    has_image = len(image_segs) > 0

    # ── 收集逻辑 ──
    if has_image:
        roll = random.random()
        logger.debug(f"[meme] 收集骰子: {roll:.2f}, 阈值: {COLLECT_RATE}")
        if roll < COLLECT_RATE:
            for seg in image_segs:
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
    group_id = event.group_id

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
