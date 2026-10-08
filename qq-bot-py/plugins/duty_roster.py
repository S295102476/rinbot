"""Global persona duty roster and 02:00 automatic switching."""

from __future__ import annotations

import asyncio
import importlib.util
import io
import random
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from nonebot import get_driver, on_message, require
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageSegment
from nonebot.log import logger
from nonebot.rule import Rule
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import select

from .db import Base, DutyRosterEntry, engine, get_session


def _load_config() -> dict[str, Any]:
    try:
        return yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


_CONFIG = _load_config()
_ROSTER_CONFIG = _CONFIG.get("duty_roster") or {}
ENABLED = bool(_ROSTER_CONFIG.get("enabled", True))
try:
    TIMEZONE = ZoneInfo(str(_ROSTER_CONFIG.get("timezone", "Asia/Shanghai")))
except (ZoneInfoNotFoundError, ValueError):
    # Minimal deployments (notably Windows virtualenvs) may not ship tzdata.
    # Asia/Shanghai has no DST, so a fixed UTC+8 fallback is correct here.
    TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")
AUTO_SWITCH_HOUR = max(0, min(23, int(_ROSTER_CONFIG.get("auto_switch_hour", 2))))
AVATAR_DIR = Path(_ROSTER_CONFIG.get("avatar_dir", "data/dutyroster"))
ADMIN_USERS = {
    int(value)
    for value in ((_CONFIG.get("agent") or {}).get("dev") or {}).get("admin_users", [])
    if str(value).lstrip("-").isdigit()
}

PERSONA_ALIASES = {
    "rin": "rin",
    "凛": "rin",
    "远坂凛": "rin",
    "eres": "eres",
    "ereshkigal": "eres",
    "艾蕾": "eres",
    "埃列什基伽勒": "eres",
    "ishtar": "ishtar",
    "伊什塔尔": "ishtar",
}
PERSONA_AVATARS = {
    "rin": "rin.jpg",
    "eres": "eres.jpg",
    "ishtar": "ishtar.jpg",
}
WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
_AUTO_LOCK = asyncio.Lock()


def _local_now() -> datetime:
    return datetime.now(TIMEZONE)


def _week_start(value: date) -> date:
    return value - timedelta(days=value.weekday())


def _normalise_command(text: str) -> str:
    text = (text or "").strip().replace("＃", "#", 1)
    return re.sub(r"^#\s*", "#", text)


def _is_roster_command(text: str) -> bool:
    return bool(re.match(r"^#(?:值班表|随机排班|手动排班)(?:\s|$)", _normalise_command(text)))


def _roster_rule() -> Rule:
    async def _rule(_bot: Bot, event: GroupMessageEvent) -> bool:
        return isinstance(event, GroupMessageEvent) and _is_roster_command(event.get_plaintext())

    return Rule(_rule)


roster_cmd = on_message(rule=_roster_rule(), priority=4, block=True)


def _parse_view_scope(argument: str, today: date) -> date:
    value = (argument or "").strip().lower()
    if not value or value in {"本周", "this", "current"}:
        return _week_start(today)
    if value in {"下周", "next"}:
        return _week_start(today) + timedelta(days=7)
    raise ValueError("只支持本周或下周")


def _parse_manual_assignment(argument: str, today: date) -> tuple[date, str]:
    match = re.match(
        r"^(?:(?P<year>\d{4})[-/.])?(?P<month>\d{1,2})[-/.](?P<day>\d{1,2})\s+(?P<persona>.+?)$",
        (argument or "").strip(),
        re.IGNORECASE,
    )
    if not match:
        raise ValueError("用法：#手动排班 M-D rin")
    year = int(match.group("year") or today.year)
    try:
        duty_date = date(year, int(match.group("month")), int(match.group("day")))
    except ValueError as exc:
        raise ValueError("日期无效") from exc
    if duty_date < today:
        raise ValueError("不能修改已经过去的日期")
    persona_key = re.sub(r"\s+", "", match.group("persona")).lower()
    persona_id = PERSONA_ALIASES.get(persona_key)
    if not persona_id:
        raise ValueError("人设只支持 rin/凛、eres/艾蕾、ishtar/伊什塔尔")
    return duty_date, persona_id


async def _get_entries(start: date) -> dict[date, DutyRosterEntry]:
    end = start + timedelta(days=7)
    session = await get_session()
    try:
        rows = (await session.execute(
            select(DutyRosterEntry)
            .where(DutyRosterEntry.duty_date >= start, DutyRosterEntry.duty_date < end)
            .order_by(DutyRosterEntry.duty_date.asc())
        )).scalars().all()
        return {row.duty_date: row for row in rows}
    finally:
        await session.close()


async def _upsert_entry(
    duty_date: date,
    persona_id: str,
    source: str,
    actor_user_id: int,
    session=None,
) -> None:
    owns_session = session is None
    active_session = session or await get_session()
    try:
        row = await active_session.get(DutyRosterEntry, duty_date)
        now = datetime.now()
        if row is None:
            active_session.add(DutyRosterEntry(
                duty_date=duty_date,
                persona_id=persona_id,
                source=source,
                actor_user_id=int(actor_user_id),
                applied_at=None,
                created_at=now,
                updated_at=now,
            ))
        else:
            row.persona_id = persona_id
            row.source = source
            row.actor_user_id = int(actor_user_id)
            row.applied_at = None
            row.updated_at = now
        if owns_session:
            await active_session.commit()
    finally:
        if owns_session:
            await active_session.close()


def _font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    candidates = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
        "C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _cover_avatar(path: Path, size: int) -> Image.Image | None:
    try:
        source = Image.open(path).convert("RGB")
        side = min(source.width, source.height)
        left = (source.width - side) // 2
        top = (source.height - side) // 2
        source = source.crop((left, top, left + side, top + side)).resize((size, size), Image.LANCZOS)
        avatar = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=max(8, size // 8), fill=255)
        avatar.paste(source.convert("RGBA"), (0, 0), mask)
        return avatar
    except (OSError, ValueError):
        return None


def _render_roster(start: date, entries: dict[date, str]) -> bytes:
    width, height = 1480, 470
    margin = 24
    title_h = 82
    cell_gap = 10
    cell_width = (width - margin * 2 - cell_gap * 6) // 7
    cell_height = height - title_h - margin
    image = Image.new("RGB", (width, height), (247, 249, 252))
    draw = ImageDraw.Draw(image)
    title_font = _font(38, bold=True)
    week_font = _font(25, bold=True)
    date_font = _font(22, bold=True)
    empty_font = _font(20)
    end = start + timedelta(days=6)
    title = f"{start.year}/{start.month}/{start.day}-{end.year}/{end.month}/{end.day}"
    title_box = draw.textbbox((0, 0), title, font=title_font)
    draw.text(((width - (title_box[2] - title_box[0])) // 2, 20), title, font=title_font, fill=(38, 49, 66))

    palette = ((224, 235, 255), (232, 246, 238), (255, 239, 218), (241, 234, 255), (255, 231, 235), (228, 245, 246), (239, 241, 246))
    for index in range(7):
        duty_date = start + timedelta(days=index)
        x = margin + index * (cell_width + cell_gap)
        y = title_h
        draw.rounded_rectangle((x, y, x + cell_width, y + cell_height), radius=16, fill=palette[index], outline=(211, 220, 232), width=2)
        weekday_box = draw.textbbox((0, 0), WEEKDAYS[index], font=week_font)
        draw.text((x + (cell_width - (weekday_box[2] - weekday_box[0])) // 2, y + 14), WEEKDAYS[index], font=week_font, fill=(57, 70, 88))
        date_text = f"{duty_date.month}/{duty_date.day}"
        date_box = draw.textbbox((0, 0), date_text, font=date_font)
        draw.text((x + (cell_width - (date_box[2] - date_box[0])) // 2, y + 51), date_text, font=date_font, fill=(90, 103, 120))

        avatar_size = min(cell_width - 34, cell_height - 112)
        avatar = None
        persona_id = entries.get(duty_date)
        if persona_id:
            avatar = _cover_avatar(AVATAR_DIR / PERSONA_AVATARS[persona_id], avatar_size)
        avatar_x = x + (cell_width - avatar_size) // 2
        avatar_y = y + 94
        if avatar is not None:
            image.paste(avatar, (avatar_x, avatar_y), avatar)
        else:
            draw.ellipse((avatar_x + avatar_size // 4, avatar_y + avatar_size // 4, avatar_x + avatar_size * 3 // 4, avatar_y + avatar_size * 3 // 4), fill=(218, 225, 235))
            empty = "—"
            empty_box = draw.textbbox((0, 0), empty, font=empty_font)
            draw.text((x + (cell_width - (empty_box[2] - empty_box[0])) // 2, avatar_y + avatar_size // 2 - 10), empty, font=empty_font, fill=(132, 145, 162))

    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


async def _roster_image(start: date) -> bytes:
    rows = await _get_entries(start)
    entries = {duty_date: row.persona_id for duty_date, row in rows.items() if row.persona_id in PERSONA_AVATARS}
    return await asyncio.to_thread(_render_roster, start, entries)


@roster_cmd.handle()
async def handle_roster(bot: Bot, event: GroupMessageEvent):
    text = _normalise_command(event.get_plaintext())
    today = _local_now().date()
    if text.startswith("#值班表"):
        argument = text[len("#值班表"):].strip()
        try:
            start = _parse_view_scope(argument, today)
            image = await _roster_image(start)
            await bot.send(event, MessageSegment.image(image))
        except ValueError as exc:
            await bot.send(event, str(exc))
        return

    if event.user_id not in ADMIN_USERS:
        await bot.send(event, "值班排班只对管理员开放")
        return

    if text == "#随机排班":
        start = _week_start(today) + timedelta(days=7)
        session = await get_session()
        try:
            for offset in range(7):
                persona_id = ("rin", "eres", "ishtar")[random.randint(1, 3) - 1]
                await _upsert_entry(start + timedelta(days=offset), persona_id, "random", int(event.user_id), session)
            await session.commit()
        finally:
            await session.close()
        image = await _roster_image(start)
        await bot.send(event, MessageSegment.image(image))
        logger.info(f"[duty_roster] random week={start.isoformat()} actor={event.user_id}")
        return

    if text.startswith("#手动排班"):
        argument = text[len("#手动排班"):].strip()
        try:
            duty_date, persona_id = _parse_manual_assignment(argument, today)
            await _upsert_entry(duty_date, persona_id, "manual", int(event.user_id))
            image = await _roster_image(_week_start(duty_date))
            await bot.send(event, MessageSegment.image(image))
            logger.info(
                f"[duty_roster] manual date={duty_date.isoformat()} persona={persona_id} actor={event.user_id}"
            )
        except ValueError as exc:
            await bot.send(event, str(exc))


async def _refresh_persona_documents() -> None:
    from .persona_manager import reload_profiles

    reload_profiles()


async def _apply_today_if_due(now: datetime | None = None) -> None:
    if not ENABLED:
        return
    local_now = now.astimezone(TIMEZONE) if now is not None and now.tzinfo else (now or _local_now())
    if local_now.hour < AUTO_SWITCH_HOUR:
        return
    duty_date = local_now.date()
    async with _AUTO_LOCK:
        session = await get_session()
        try:
            row = await session.get(DutyRosterEntry, duty_date)
            if row is None or row.applied_at is not None:
                return
            if row.persona_id not in PERSONA_AVATARS:
                logger.warning(f"[duty_roster] invalid persona date={duty_date} persona={row.persona_id}")
                return
            await _refresh_persona_documents()
            from .persona_manager import switch_persona

            result = await switch_persona(row.persona_id, 0, f"值班表自动切换 {duty_date.isoformat()}")
            if result.startswith(("未找到", "人设切换记录失败")):
                raise RuntimeError(result)
            row.applied_at = local_now.replace(tzinfo=None)
            row.updated_at = datetime.now()
            await session.commit()
            logger.info(f"[duty_roster] auto_switch date={duty_date} persona={row.persona_id}")
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


@get_driver().on_startup
async def _startup() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    if ENABLED:
        try:
            await _apply_today_if_due()
        except Exception as exc:
            logger.warning(f"[duty_roster] startup auto switch failed: {type(exc).__name__}")
        logger.info(
            f"[duty_roster] enabled timezone={getattr(TIMEZONE, 'key', 'Asia/Shanghai')} "
            f"auto_switch={AUTO_SWITCH_HOUR:02d}:00"
        )


try:
    if importlib.util.find_spec("nonebot_plugin_apscheduler") is None:
        raise ImportError("nonebot_plugin_apscheduler is not installed")
    require("nonebot_plugin_apscheduler")
    from nonebot_plugin_apscheduler import scheduler

    @scheduler.scheduled_job("interval", seconds=60, id="duty_roster_auto_switch")
    async def _scheduled_auto_switch() -> None:
        try:
            await _apply_today_if_due()
        except Exception as exc:
            logger.warning(f"[duty_roster] scheduled auto switch failed: {type(exc).__name__}")
except Exception:
    logger.warning("[duty_roster] APScheduler unavailable; automatic persona switching disabled")
