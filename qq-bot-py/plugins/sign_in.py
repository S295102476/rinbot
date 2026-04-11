"""每日签到插件 — #签到 生成图片卡片"""

import random
import textwrap
from datetime import date, datetime
from io import BytesIO

import httpx
import yaml
from io import BytesIO
from minio import Minio
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageSegment
from nonebot.log import logger
from sqlalchemy import BigInteger, Integer, Date, select, func
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, engine, get_session

# ---------- 配置 ----------
with open("config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)
sign_cfg = config.get("sign_in", {})
AFF_LO, AFF_HI = sign_cfg.get("affection_range", [1, 10])
CONT_BONUS = sign_cfg.get("continuous_bonus", 5)
BG_API = sign_cfg.get("bg_api", "https://www.dmoe.cc/random.php")
HITOKOTO_API = sign_cfg.get("hitokoto_api", "https://v1.hitokoto.cn")

# ---------- MinIO 背景图 ----------
minio_cfg = config["meme"]["minio"]
_minio = Minio(
    minio_cfg["endpoint"],
    access_key=minio_cfg["access_key"],
    secret_key=minio_cfg["secret_key"],
    secure=minio_cfg.get("secure", False),
)
BG_BUCKET = "sign-bg"

try:
    if not _minio.bucket_exists(BG_BUCKET):
        _minio.make_bucket(BG_BUCKET)
except Exception as e:
    logger.warning(f"[sign_in] MinIO bucket检查失败: {e}")

# ---------- 字体 ----------
# 优先使用衬线/粗体字体以获得艺术感
_FONT_CANDIDATES = [
    # Serif CJK Bold — 艺术感更强
    "/usr/share/fonts/opentype/noto/NotoSerifCJKsc-Bold.otf",
    "/usr/share/fonts/truetype/noto/NotoSerifCJK-Bold.ttc",
    "/usr/share/fonts/noto-cjk/NotoSerifCJKsc-Bold.otf",
    "/usr/share/fonts/google-noto-serif-cjk/NotoSerifCJK-Bold.ttc",
    # Sans CJK Bold
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
    # Sans CJK Regular (fallback)
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    # Windows
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
]


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


FONT_L = _load_font(32)
FONT_M = _load_font(24)
FONT_S = _load_font(18)
FONT_DATE = _load_font(40)

# ---------- ORM ----------


class SignIn(Base):
    __tablename__ = "sign_in"
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    group_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    affection: Mapped[int] = mapped_column(Integer, default=0)
    total_days: Mapped[int] = mapped_column(Integer, default=0)
    continuous: Mapped[int] = mapped_column(Integer, default=0)
    last_date: Mapped[date | None] = mapped_column(Date, default=None)


# ---------- 启动时建表 ----------
from nonebot import get_driver


@get_driver().on_startup
async def _create_tables():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("[sign_in] 数据表已就绪")


# ---------- Handler ----------
sign_cmd = on_command("#签到", priority=5, block=True)


@sign_cmd.handle()
async def handle_sign(bot: Bot, event: GroupMessageEvent):
    user_id = event.user_id
    group_id = event.group_id
    today = date.today()

    session = await get_session()
    try:
        row = (await session.execute(
            select(SignIn).where(SignIn.user_id == user_id, SignIn.group_id == group_id)
        )).scalar_one_or_none()

        if row and row.last_date == today:
            await sign_cmd.send("你今天已经签到过了哦~")
            return

        # 计算好感度增量
        add = random.randint(AFF_LO, AFF_HI)
        if row is None:
            row = SignIn(user_id=user_id, group_id=group_id, affection=0, total_days=0, continuous=0)
            session.add(row)

        # 连续签到判定
        from datetime import timedelta
        yesterday = today - timedelta(days=1)
        if row.last_date == yesterday:
            row.continuous += 1
            add += CONT_BONUS
        else:
            row.continuous = 1

        row.affection += add
        row.total_days += 1
        row.last_date = today
        await session.commit()

        # 查排名
        rank_result = await session.execute(
            select(func.count()).select_from(SignIn).where(
                SignIn.group_id == group_id,
                SignIn.affection > row.affection
            )
        )
        rank = rank_result.scalar() + 1

        affection = row.affection
        continuous = row.continuous
        total_days = row.total_days
    finally:
        await session.close()

    # 获取昵称
    try:
        member = await bot.get_group_member_info(group_id=group_id, user_id=user_id)
        nickname = member.get("card") or member.get("nickname") or str(user_id)
    except Exception:
        nickname = str(user_id)

    # 获取背景图、头像、一言
    bg_bytes = avatar_bytes = hitokoto_text = None

    # 背景图：在线下载直接用，同时存MinIO；失败则从MinIO随机取
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as bg_client:
        try:
            bg_resp = await bg_client.get(BG_API)
            content_type = bg_resp.headers.get("content-type", "")
            raw = bg_resp.content
            logger.info(f"[sign_in] 背景图在线: status={bg_resp.status_code}, size={len(raw)}, type={content_type}")
            if "image" in content_type and len(raw) >= 5000:
                bg_bytes = raw
                _save_bg_to_minio(raw, content_type)
            else:
                logger.warning("[sign_in] 在线背景图内容异常, 回退MinIO")
        except Exception as e:
            logger.warning(f"[sign_in] 在线背景图失败: {e}, 回退MinIO")

    if not bg_bytes:
        bg_bytes = _random_bg_from_minio()

    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:

        try:
            avatar_resp = await client.get(f"http://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640")
            avatar_bytes = avatar_resp.content
            logger.info(f"[sign_in] 头像: status={avatar_resp.status_code}, size={len(avatar_bytes)}")
        except Exception as e:
            logger.error(f"[sign_in] 头像获取失败: {e}")
        try:
            hk_resp = await client.get(HITOKOTO_API, params={"encode": "json"})
            hk = hk_resp.json()
            hitokoto_text = hk.get("hitokoto", "")
            logger.info(f"[sign_in] 一言: {hitokoto_text}")
        except Exception as e:
            logger.error(f"[sign_in] 一言获取失败: {e}")
            hitokoto_text = "所谓宿命，其实都是最好的安排。"

    # 渲染图片
    img_bytes = _render_card(
        bg_bytes=bg_bytes,
        avatar_bytes=avatar_bytes,
        nickname=nickname,
        add=add,
        affection=affection,
        rank=rank,
        continuous=continuous,
        total_days=total_days,
        hitokoto=hitokoto_text,
    )
    await sign_cmd.send(MessageSegment.image(img_bytes))


# ---------- 图片渲染 ----------
CARD_W, CARD_H = 900, 500


def _circle_avatar(avatar_bytes: bytes | None, size: int = 100) -> Image.Image | None:
    if not avatar_bytes:
        return None
    try:
        av = Image.open(BytesIO(avatar_bytes)).convert("RGBA").resize((size, size))
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
        av.putalpha(mask)
        return av
    except Exception:
        return None


def _greeting() -> str:
    h = datetime.now().hour
    if h < 6:
        return "凌晨好！"
    elif h < 12:
        return "上午好！"
    elif h < 14:
        return "中午好！"
    elif h < 18:
        return "下午好！"
    elif h < 22:
        return "晚上好！"
    else:
        return "夜深了！"


def _render_card(
    bg_bytes: bytes | None,
    avatar_bytes: bytes | None,
    nickname: str,
    add: int,
    affection: int,
    rank: int,
    continuous: int,
    total_days: int,
    hitokoto: str,
) -> BytesIO:
    W, H = CARD_W, CARD_H
    MASK_W = 420  # 左侧信息区宽度

    # --- 1. 背景：原图铺满整张卡 ---
    if bg_bytes:
        try:
            bg = Image.open(BytesIO(bg_bytes)).convert("RGBA").resize((W, H), Image.LANCZOS)
        except Exception:
            bg = Image.new("RGBA", (W, H), (60, 40, 80, 255))
    else:
        bg = Image.new("RGBA", (W, H), (60, 40, 80, 255))

    # --- 2. 左侧半透明遮罩 (高透明度，能看到背景) ---
    overlay = Image.new("RGBA", (MASK_W, H), (0, 0, 0, 60))
    bg.alpha_composite(overlay, (0, 0))

    draw = ImageDraw.Draw(bg)

    # --- 3. 日期 (右上角) ---
    now = datetime.now()
    date_str = f"{now.year}/{now.month}/{now.day}"
    draw.text((W - 210, 15), date_str, font=FONT_DATE, fill=(255, 255, 255, 230))

    # --- 4. 头像 + 问候 ---
    avatar_img = _circle_avatar(avatar_bytes, 80)
    y = 40
    if avatar_img:
        bg.paste(avatar_img, (30, y), avatar_img)

    greeting = _greeting()
    draw.text((125, y + 10), greeting, font=FONT_L, fill="white")
    draw.text((125, y + 48), nickname, font=FONT_S, fill=(200, 200, 200))

    # --- 5. 好感度信息 ---
    y = 160
    draw.text((30, y), f"好感度+{add}", font=FONT_M, fill=(255, 220, 100))
    y += 40
    draw.text((30, y), f"当前好感度：{affection}", font=FONT_M, fill="white")
    y += 40
    draw.text((30, y), f"当前群排名：第{rank}位", font=FONT_M, fill="white")
    y += 50
    draw.text((30, y), f"连续签到：{continuous}天", font=FONT_M, fill=(180, 220, 255))
    y += 40
    draw.text((30, y), f"累计签到：{total_days}天", font=FONT_M, fill=(180, 220, 255))

    # --- 6. 今日一言 ---
    y += 50
    draw.text((30, y), "今日一言：", font=FONT_S, fill=(200, 200, 200))
    y += 28
    wrapped = textwrap.fill(hitokoto, width=18)
    for line in wrapped.split("\n"):
        draw.text((30, y), line, font=FONT_S, fill=(220, 220, 220))
        y += 25

    # --- 7. 底部水印 ---
    draw.text((20, H - 30), "Create By 寒bot", font=FONT_S, fill=(255, 255, 255, 80))

    # 导出
    buf = BytesIO()
    bg.convert("RGB").save(buf, format="JPEG", quality=92)
    buf.seek(0)
    return buf


# ---------- MinIO 背景图存取 ----------

def _save_bg_to_minio(img_bytes: bytes, content_type: str = "image/jpeg"):
    """把在线获取的背景图存入 MinIO"""
    import hashlib
    ext = "jpg"
    if "png" in content_type:
        ext = "png"
    elif "webp" in content_type:
        ext = "webp"
    name = hashlib.md5(img_bytes).hexdigest() + f".{ext}"
    try:
        _minio.put_object(BG_BUCKET, name, BytesIO(img_bytes), len(img_bytes), content_type=content_type)
        logger.info(f"[sign_in] 背景图已存入MinIO: {name}")
    except Exception as e:
        logger.warning(f"[sign_in] 背景图存入MinIO失败: {e}")


def _random_bg_from_minio() -> bytes | None:
    """从 MinIO 随机取一张背景图"""
    try:
        objects = list(_minio.list_objects(BG_BUCKET))
        if not objects:
            logger.info("[sign_in] MinIO背景图库为空")
            return None
        obj = random.choice(objects)
        resp = _minio.get_object(BG_BUCKET, obj.object_name)
        data = resp.read()
        resp.close()
        resp.release_conn()
        logger.info(f"[sign_in] MinIO背景图: {obj.object_name}, size={len(data)}")
        return data
    except Exception as e:
        logger.error(f"[sign_in] MinIO背景图获取失败: {e}")
        return None


# ---------- #下载图片 指令 ----------
ADMIN_USERS = {int(uid) for uid in sign_cfg.get("admin_users", [])}
BG_APIS = [
    "https://www.dmoe.cc/random.php",
    "https://cdn.seovx.com/d/?mom=302",
    "https://www.loliapi.com/acg/pc",
]

download_bg_cmd = on_command("#下载图片", priority=5, block=True)


@download_bg_cmd.handle()
async def handle_download_bg(bot: Bot, event: GroupMessageEvent):
    if event.user_id not in ADMIN_USERS:
        return

    await download_bg_cmd.send(f"开始从 {len(BG_APIS)} 个源下载背景图...")

    success = 0
    fail = 0
    details = []

    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        for api in BG_APIS:
            try:
                logger.info(f"[下载图片] 请求: {api}")
                resp = await client.get(api)
                content_type = resp.headers.get("content-type", "")
                size = len(resp.content)
                logger.info(f"[下载图片] {api} → status={resp.status_code}, type={content_type}, size={size}")

                if "image" not in content_type or size < 5000:
                    msg = f"❌ {api}\n  非图片或太小 (type={content_type}, size={size})"
                    details.append(msg)
                    logger.warning(f"[下载图片] 跳过: {msg}")
                    fail += 1
                    continue

                _save_bg_to_minio(resp.content, content_type)
                success += 1
                details.append(f"✅ {api}\n  size={size}, type={content_type}")
            except Exception as e:
                fail += 1
                details.append(f"❌ {api}\n  {e}")
                logger.error(f"[下载图片] {api} 失败: {e}")

    # 统计当前库存
    try:
        total = len(list(_minio.list_objects(BG_BUCKET)))
    except Exception:
        total = "?"

    report = f"下载完成: ✅{success} ❌{fail}\nMinIO库存: {total}张\n\n" + "\n".join(details)
    await download_bg_cmd.send(report)
