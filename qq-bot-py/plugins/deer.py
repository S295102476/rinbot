"""🦌 鹿历插件

指令：
  🦌          → 今日打勾（🦌了）
  不🦌         → 今日打叉（不🦌）
  补🦌 12      → 补签本月12日为勾（每月最多3次）
  补不🦌 12    → 补签本月12日为叉（与补🦌共用次数；可改正已有补签）
  🦌不🦌       → 随机抽1次，多则勾，少则叉，平局不写
  N🦌不🦌      → 随机抽N次（N≤99），结果规则同上

生成：以 deer.jpg 为格子的个人月历图，已标记日期叠加大红勾/叉。
数据：持久化到 MySQL deer_mark 表，重启不丢失。
"""

import calendar
import asyncio
import io
import os
import random
import re
from datetime import date, datetime

import yaml
from nonebot import on_fullmatch, on_message, get_driver
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageSegment
from nonebot.log import logger
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import select, func, text
from sqlalchemy.dialects.mysql import insert as mysql_insert

from .db import engine, get_session, DeerMark, Base

# ---------- 配置 ----------
with open("config.yaml", "r", encoding="utf-8") as f:
    _cfg = yaml.safe_load(f)

_deer_cfg = _cfg.get("deer", {})
DEER_IMAGE_PATH: str = _deer_cfg.get("image_path", "data/deer/deer.jpg")
ALLOWED_GROUPS: set[int] = set(_deer_cfg.get("allowed_groups", []))
BACKFILL_LIMIT_PER_MONTH: int = int(_deer_cfg.get("backfill_limit_per_month", 3))

# ---------- 字体（复用 sign_in 的候选列表）----------
_FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSerifCJKsc-Bold.otf",
    "/usr/share/fonts/truetype/noto/NotoSerifCJK-Bold.ttc",
    "/usr/share/fonts/noto-cjk/NotoSerifCJKsc-Bold.otf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
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


# 预加载字体
_FONT_TITLE = _load_font(28)
_FONT_WEEK = _load_font(18)
_FONT_DAY = _load_font(16)
_FONT_TAG = _load_font(15)

# ---------- 建表 ----------
@get_driver().on_startup
async def _create_deer_table():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # 旧表没有稳定的补签标记。先补列，再按历史的时间判定迁移一次：
        # 创建日期晚于目标日期的记录即旧版补签记录。
        has_column = (await conn.execute(
            text("SHOW COLUMNS FROM deer_mark LIKE 'is_backfill'")
        )).first()
        if not has_column:
            await conn.execute(text(
                "ALTER TABLE deer_mark "
                "ADD COLUMN is_backfill TINYINT(1) NOT NULL DEFAULT 0 "
                "AFTER mark"
            ))
            logger.info("[deer] 已添加 is_backfill 补签标记列")

        migrated = await conn.execute(text("""
            UPDATE deer_mark
            SET is_backfill = 1
            WHERE is_backfill = 0
              AND DATE(created_at) > STR_TO_DATE(
                  CONCAT(year, '-', LPAD(month, 2, '0'), '-', LPAD(day, 2, '0')),
                  '%Y-%m-%d'
              )
        """))
        if migrated.rowcount:
            logger.info(f"[deer] 已迁移 {migrated.rowcount} 条旧补签记录")
    logger.info("[deer] 数据表已就绪")


# ---------- DB 工具 ----------
async def _get_month_marks(user_id: int, group_id: int, year: int, month: int) -> dict[int, str]:
    """读取某用户当月所有 mark，返回 {day: 'check'/'cross'}"""
    session = await get_session()
    try:
        rows = (await session.execute(
            select(DeerMark.day, DeerMark.mark).where(
                DeerMark.user_id == user_id,
                DeerMark.group_id == group_id,
                DeerMark.year == year,
                DeerMark.month == month,
            )
        )).all()
        return {row.day: row.mark for row in rows}
    finally:
        await session.close()


async def _get_group_month_marks(group_id: int, year: int, month: int) -> dict[int, dict[int, str]]:
    """读取某群当月所有 mark，返回 {user_id: {day: 'check'/'cross'}}。"""
    session = await get_session()
    try:
        rows = (await session.execute(
            select(DeerMark.user_id, DeerMark.day, DeerMark.mark).where(
                DeerMark.group_id == group_id,
                DeerMark.year == year,
                DeerMark.month == month,
            )
        )).all()
        out: dict[int, dict[int, str]] = {}
        for row in rows:
            out.setdefault(int(row.user_id), {})[int(row.day)] = str(row.mark)
        return out
    finally:
        await session.close()


async def _get_mark(user_id: int, group_id: int, year: int, month: int, day: int) -> tuple[str, bool] | None:
    """读取某天标记及其是否为补签，未标记返回 None。"""
    session = await get_session()
    try:
        row = (await session.execute(
            select(DeerMark.mark, DeerMark.is_backfill, DeerMark.created_at).where(
                DeerMark.user_id == user_id,
                DeerMark.group_id == group_id,
                DeerMark.year == year,
                DeerMark.month == month,
                DeerMark.day == day,
            )
        )).one_or_none()
        if row is None:
            return None

        # 部署新字段前的历史数据仍以旧版时间规则兜底识别。
        is_backfill = bool(row.is_backfill)
        if not is_backfill and row.created_at:
            is_backfill = row.created_at.date() > date(year, month, day)
        return str(row.mark), is_backfill
    finally:
        await session.close()


def _days_in_scope(year: int, month: int) -> int:
    """结算月份统计整月。"""
    return calendar.monthrange(year, month)[1]


def _is_settled_month(year: int, month: int) -> bool:
    """只有已结束月份才发放标签。"""
    today = date.today()
    return (year, month) < (today.year, today.month)


def _previous_month(day: date) -> tuple[int, int]:
    """返回指定日期的上一个自然月。"""
    if day.month == 1:
        return day.year - 1, 12
    return day.year, day.month - 1


def _build_month_tags(
    user_id: int,
    year: int,
    month: int,
    group_marks: dict[int, dict[int, str]],
) -> list[str]:
    """根据群内月度记录计算当前用户标签。"""
    limit_day = _days_in_scope(year, month)
    user_marks = group_marks.get(user_id, {})

    def _count(marks: dict[int, str], mark: str) -> int:
        return sum(1 for day, value in marks.items() if 1 <= int(day) <= limit_day and value == mark)

    marked_days = {
        int(day)
        for day, value in user_marks.items()
        if 1 <= int(day) <= limit_day and value in {"check", "cross"}
    }
    check_count = _count(user_marks, "check")
    cross_count = _count(user_marks, "cross")
    full_attendance = limit_day > 0 and len(marked_days) == limit_day

    group_check_counts = [_count(marks, "check") for marks in group_marks.values()]
    group_cross_counts = [_count(marks, "cross") for marks in group_marks.values()]
    max_check = max(group_check_counts, default=0)
    max_cross = max(group_cross_counts, default=0)

    prefix = f"{month}月"
    tags: list[str] = []
    if full_attendance:
        tags.append(f"{prefix}全勤")
        if check_count == limit_day:
            tags.append(f"{prefix}全鹿奖")
        if cross_count == limit_day:
            tags.append(f"{prefix}不鹿奖")
    if max_check > 0 and check_count == max_check:
        tags.append(f"{prefix}鹿王")
    if max_cross > 0 and cross_count == max_cross:
        tags.append(f"{prefix}戒王")
    return tags


async def _get_month_backfill_count(user_id: int, group_id: int, year: int, month: int) -> int:
    """统计本月补签次数。"""
    session = await get_session()
    try:
        count = (await session.execute(
            select(func.count()).select_from(DeerMark).where(
                DeerMark.user_id == user_id,
                DeerMark.group_id == group_id,
                DeerMark.year == year,
                DeerMark.month == month,
                DeerMark.is_backfill.is_(True),
            )
        )).scalar_one()
        return int(count or 0)
    finally:
        await session.close()


async def _upsert_mark(
    user_id: int,
    group_id: int,
    year: int,
    month: int,
    day: int,
    mark: str,
    *,
    is_backfill: bool,
):
    """写入/覆盖某天的 mark"""
    session = await get_session()
    try:
        stmt = mysql_insert(DeerMark).values(
            user_id=user_id,
            group_id=group_id,
            year=year,
            month=month,
            day=day,
            mark=mark,
            is_backfill=is_backfill,
            created_at=datetime.now(),
        )
        stmt = stmt.on_duplicate_key_update(mark=mark, is_backfill=is_backfill)
        await session.execute(stmt)
        await session.commit()
    finally:
        await session.close()


def _parse_backfill_date(arg: str, today: date) -> tuple[date | None, str | None]:
    """解析补签日期，仅允许当前月份内的过去日期。"""
    text = (arg or "").strip()
    if not text:
        return None, "请在后面写要补的日期，例如：补🦌 12"

    text = text.replace("号", "").replace("日", "").strip()
    target: date | None = None

    try:
        if re.fullmatch(r"\d{1,2}", text):
            target = date(today.year, today.month, int(text))
        else:
            m = re.fullmatch(r"(?:(\d{4})[-/.])?(\d{1,2})[-/.](\d{1,2})", text)
            if not m:
                return None, "日期格式不对哦，例如：补🦌 12 或 补不🦌 6-12"
            year = int(m.group(1)) if m.group(1) else today.year
            month = int(m.group(2))
            day = int(m.group(3))
            target = date(year, month, day)
    except ValueError:
        return None, "这个日期不存在哦。"

    if target.year != today.year or target.month != today.month:
        return None, "目前只能补本月的鹿历。"
    if target >= today:
        return None, "只能补今天以前漏掉的日期。"
    return target, None


async def _handle_backfill(
    bot: Bot,
    event: GroupMessageEvent,
    day_arg: str,
    mark_val: str,
):
    if ALLOWED_GROUPS and event.group_id not in ALLOWED_GROUPS:
        return

    user_id = event.user_id
    group_id = event.group_id
    today = date.today()
    target, err = _parse_backfill_date(day_arg, today)
    if err:
        await bot.send(event, err)
        return
    assert target is not None

    existing = await _get_mark(user_id, group_id, target.year, target.month, target.day)
    if existing is not None:
        existing_mark, is_backfill = existing
        if not is_backfill:
            await bot.send(event, f"{target.month}月{target.day}日已经有正常记录了，不能用补签修改。")
            return
        if existing_mark == mark_val:
            await bot.send(event, f"{target.month}月{target.day}日已经是{'🦌' if mark_val == 'check' else '不🦌'}了。")
            return

        prefix = (
            f"已修改补签：{target.month}月{target.day}日改为"
            f"{'🦌' if mark_val == 'check' else '不🦌'}\n"
            "本次修改不额外消耗补签次数。"
        )
        await _send_calendar(
            bot, event, target.day, mark_val, prefix, is_backfill=True
        )
        return

    used = await _get_month_backfill_count(user_id, group_id, today.year, today.month)
    if used >= BACKFILL_LIMIT_PER_MONTH:
        await bot.send(event, f"本月补🦌次数已经用完了（{BACKFILL_LIMIT_PER_MONTH}/{BACKFILL_LIMIT_PER_MONTH}）。")
        return

    prefix = (
        f"已补{target.month}月{target.day}日："
        f"{'🦌' if mark_val == 'check' else '不🦌'}\n"
        f"本月补签次数：{used + 1}/{BACKFILL_LIMIT_PER_MONTH}"
    )
    await _send_calendar(bot, event, target.day, mark_val, prefix, is_backfill=True)


# ---------- 渲染日历图 ----------
CELL = 90          # 格子宽高（px）
PAD = 16           # 边距
HEADER_H = 118     # 顶部标题区高度
WEEK_H = 28        # 星期行高度
_WEEKS = ["一", "二", "三", "四", "五", "六", "日"]


def _draw_check(draw: ImageDraw.ImageDraw, x: int, y: int, size: int, color: tuple):
    """在格子上绘制大红勾（✓）"""
    lw = max(7, size // 9)
    p1 = (x + int(size * 0.10), y + int(size * 0.52))
    p2 = (x + int(size * 0.38), y + int(size * 0.82))
    p3 = (x + int(size * 0.88), y + int(size * 0.18))
    draw.line([p1, p2], fill=color, width=lw)
    draw.line([p2, p3], fill=color, width=lw)


def _draw_cross(draw: ImageDraw.ImageDraw, x: int, y: int, size: int, color: tuple):
    """在格子上绘制大叉（✗）"""
    lw = max(5, size // 14)
    m = int(size * 0.18)
    draw.line([(x + m, y + m), (x + size - m, y + size - m)], fill=color, width=lw)
    draw.line([(x + size - m, y + m), (x + m, y + size - m)], fill=color, width=lw)


def _text_size(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def _draw_tags(draw: ImageDraw.ImageDraw, tags: list[str], x: int, y: int, max_x: int) -> int:
    """绘制标签，返回绘制后的下一行 y。"""
    if not tags:
        return y

    cur_x = x
    cur_y = y
    pad_x = 9
    tag_h = 25
    gap = 6
    bg = (236, 226, 204)
    outline = (176, 139, 88)
    fg = (85, 52, 24)

    for tag in tags:
        text_w, _ = _text_size(draw, tag, _FONT_TAG)
        tag_w = text_w + pad_x * 2
        if cur_x + tag_w > max_x and cur_x > PAD:
            cur_x = PAD
            cur_y += tag_h + gap
        draw.rounded_rectangle(
            [cur_x, cur_y, cur_x + tag_w, cur_y + tag_h],
            radius=6,
            fill=bg,
            outline=outline,
            width=1,
        )
        draw.text((cur_x + pad_x, cur_y + 3), tag, font=_FONT_TAG, fill=fg)
        cur_x += tag_w + gap

    return cur_y + tag_h


def _render_calendar(year: int, month: int, marks: dict[int, str], nickname: str, tags: list[str] | None = None) -> bytes:
    """渲染月历图，返回 PNG bytes"""
    # 加载/拼格子底图
    deer_cell: Image.Image | None = None
    if os.path.exists(DEER_IMAGE_PATH):
        try:
            deer_cell = Image.open(DEER_IMAGE_PATH).convert("RGB").resize((CELL, CELL), Image.LANCZOS)
        except Exception as e:
            logger.warning(f"[deer] 加载鹿图失败: {e}")

    weeks = calendar.monthcalendar(year, month)  # 每行7个，0表示非本月
    rows = len(weeks)

    img_w = PAD * 2 + CELL * 7
    img_h = PAD + HEADER_H + WEEK_H + CELL * rows + PAD
    img = Image.new("RGB", (img_w, img_h), (245, 240, 230))
    draw = ImageDraw.Draw(img)

    # ── 标题行 ──
    title = f"{year}-{month:02d}"
    draw.text((PAD, PAD), title, font=_FONT_TITLE, fill=(60, 40, 20))
    draw.text((PAD, PAD + 36), nickname, font=_FONT_WEEK, fill=(120, 80, 40))
    nick_w, _ = _text_size(draw, nickname, _FONT_WEEK)
    tag_x = PAD + nick_w + 12
    tag_y = PAD + 37
    if tag_x > img_w - PAD - 80:
        tag_x = PAD
        tag_y = PAD + 63
    _draw_tags(draw, tags or [], tag_x, tag_y, img_w - PAD)

    # ── 星期头 ──
    week_y = PAD + HEADER_H
    for i, w in enumerate(_WEEKS):
        wx = PAD + i * CELL + (CELL - 20) // 2
        draw.text((wx, week_y + 5), w, font=_FONT_WEEK, fill=(100, 60, 30))

    # ── 格子 ──
    CHECK_COLOR = (220, 30, 30)   # 大红
    CROSS_COLOR = (220, 30, 30)

    for row_i, week in enumerate(weeks):
        for col_i, day in enumerate(week):
            cx = PAD + col_i * CELL
            cy = PAD + HEADER_H + WEEK_H + row_i * CELL

            if day == 0:
                # 非本月格：浅灰
                draw.rectangle([cx, cy, cx + CELL - 1, cy + CELL - 1], fill=(210, 205, 195))
                continue

            # 贴鹿底图或浅橙色背景
            if deer_cell:
                img.paste(deer_cell, (cx, cy))
            else:
                draw.rectangle([cx, cy, cx + CELL - 1, cy + CELL - 1], fill=(255, 240, 200))

            # 格子边框（细线）
            draw.rectangle([cx, cy, cx + CELL - 1, cy + CELL - 1], outline=(180, 160, 130), width=1)

            # 日期数字（白色描边 + 黑色）
            day_str = str(day)
            tx = cx + CELL - 18
            ty = cy + 3
            # 描边
            for dx, dy in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                draw.text((tx + dx, ty + dy), day_str, font=_FONT_DAY, fill=(0, 0, 0))
            draw.text((tx, ty), day_str, font=_FONT_DAY, fill=(255, 255, 255))

            # 标记
            mark = marks.get(day)
            if mark == "check":
                _draw_check(draw, cx, cy, CELL, CHECK_COLOR)
            elif mark == "cross":
                _draw_cross(draw, cx, cy, CELL, CROSS_COLOR)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------- 公共发送逻辑 ----------
async def _send_calendar(
    bot: Bot,
    event: GroupMessageEvent,
    mark_day: int | None,
    mark_val: str | None,
    prefix: str = "",
    *,
    is_backfill: bool = False,
):
    """读DB → 可选写入 → 渲染 → 发送"""
    user_id = event.user_id
    group_id = event.group_id
    today = date.today()
    year, month = today.year, today.month

    if mark_day is not None and mark_val is not None:
        await _upsert_mark(
            user_id,
            group_id,
            year,
            month,
            mark_day,
            mark_val,
            is_backfill=is_backfill,
        )

    marks = await _get_month_marks(user_id, group_id, year, month)
    award_year, award_month = _previous_month(today)
    group_marks = await _get_group_month_marks(group_id, award_year, award_month)
    tags = (
        _build_month_tags(user_id, award_year, award_month, group_marks)
        if _is_settled_month(award_year, award_month)
        else []
    )

    try:
        member = await bot.get_group_member_info(group_id=group_id, user_id=user_id)
        nickname = member.get("card") or member.get("nickname") or str(user_id)
    except Exception:
        nickname = str(user_id)

    img_bytes = await asyncio.get_event_loop().run_in_executor(None, _render_calendar, year, month, marks, nickname, tags)

    msgs = []
    if prefix:
        msgs.append(prefix)
    msgs.append(MessageSegment.image(img_bytes))
    for m in msgs:
        await bot.send(event, m)


# ---------- 指令注册 ----------

from nonebot import on_regex
from nonebot.params import RegexGroup


# 标签样式测试：不写入数据库
deer_tag_test_cmd = on_regex(r"^#(?:🦌|鹿)\s*测试$", priority=5, block=True)

@deer_tag_test_cmd.handle()
async def handle_deer_tag_test(bot: Bot, event: GroupMessageEvent):
    if ALLOWED_GROUPS and event.group_id not in ALLOWED_GROUPS:
        return

    today = date.today()
    year, month = today.year, today.month
    limit_day = _days_in_scope(year, month)
    marks = {
        day: ("check" if day % 2 else "cross")
        for day in range(1, limit_day + 1)
    }
    tags = [
        f"{month}月全勤",
        f"{month}月全鹿奖",
        f"{month}月不鹿奖",
        f"{month}月鹿王",
        f"{month}月戒王",
    ]

    try:
        member = await bot.get_group_member_info(group_id=event.group_id, user_id=event.user_id)
        nickname = member.get("card") or member.get("nickname") or str(event.user_id)
    except Exception:
        nickname = str(event.user_id)

    img_bytes = await asyncio.get_event_loop().run_in_executor(
        None,
        _render_calendar,
        year,
        month,
        marks,
        nickname,
        tags,
    )
    await bot.send(event, "鹿历标签样式测试（不写入数据）")
    await bot.send(event, MessageSegment.image(img_bytes))


# 补🦌 / 补不🦌（本月最多3次，共用额度）
deer_backfill_cmd = on_regex(r"^补(不)?🦌\s+(.+)$", priority=5, block=True)

@deer_backfill_cmd.handle()
async def handle_deer_backfill(bot: Bot, event: GroupMessageEvent, matched=RegexGroup()):
    is_cross, day_arg = matched
    mark_val = "cross" if is_cross else "check"
    await _handle_backfill(bot, event, day_arg, mark_val)


# 🦌（今日打勾）
deer_check_cmd = on_fullmatch("🦌", priority=5, block=True)

@deer_check_cmd.handle()
async def handle_deer_check(bot: Bot, event: GroupMessageEvent):
    if ALLOWED_GROUPS and event.group_id not in ALLOWED_GROUPS:
        return
    today = date.today()
    await _send_calendar(bot, event, today.day, "check")


# 不🦌（今日打叉）
deer_cross_cmd = on_fullmatch("不🦌", priority=5, block=True)

@deer_cross_cmd.handle()
async def handle_deer_cross(bot: Bot, event: GroupMessageEvent):
    if ALLOWED_GROUPS and event.group_id not in ALLOWED_GROUPS:
        return
    today = date.today()
    await _send_calendar(bot, event, today.day, "cross")


# 🦌不🦌 / N🦌不🦌（随机抽取）

deer_random_cmd = on_regex(r"^(\d{1,2})?🦌不🦌$", priority=5, block=True)

@deer_random_cmd.handle()
async def handle_deer_random(bot: Bot, event: GroupMessageEvent, matched=RegexGroup()):
    if ALLOWED_GROUPS and event.group_id not in ALLOWED_GROUPS:
        return

    n_str = matched[0]
    n = int(n_str) if n_str else 1
    if n < 1:
        n = 1
    if n > 99:
        await bot.send(event, "最多抽取99次哦～")
        return

    results = random.choices(["🦌", "不🦌"], k=n)
    count_check = results.count("🦌")
    count_cross = results.count("不🦌")

    prefix = f"抽取结果：{count_check}个🦌，{count_cross}个不🦌"

    today = date.today()
    if count_check > count_cross:
        mark_val = "check"
        prefix += "\n结果：🦌了！"
    elif count_cross > count_check:
        mark_val = "cross"
        prefix += "\n结果：不🦌…"
    else:
        mark_val = None  # 平局不写入
        prefix += "\n结果：势均力敌，不做标记"

    await _send_calendar(bot, event, today.day if mark_val else None, mark_val, prefix)
