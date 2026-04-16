"""娶群友插件 — #娶群友 每日随机抽一位群友"""

import random
from datetime import date

import httpx
import yaml
from nonebot import on_command, get_driver
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageSegment, Message
from nonebot.log import logger
from sqlalchemy import select

from .db import Base, engine, get_session, DailyWife

# ---------- 配置 ----------
with open("config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

# ---------- 建表 ----------
@get_driver().on_startup
async def _create_tables():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("[marry] 数据表已就绪")


# ---------- Handler ----------
marry_cmd = on_command("#娶群友", priority=5, block=True)


@marry_cmd.handle()
async def handle_marry(bot: Bot, event: GroupMessageEvent):
    user_id = event.user_id
    group_id = event.group_id
    today = date.today()

    session = await get_session()
    try:
        # 查今天是否已娶
        existing = (await session.execute(
            select(DailyWife).where(
                DailyWife.user_id == user_id,
                DailyWife.group_id == group_id,
                DailyWife.date == today,
            )
        )).scalar_one_or_none()

        if existing:
            # 已娶过，展示旧结果
            avatar_url = f"http://q1.qlogo.cn/g?b=qq&nk={existing.wife_id}&s=640"
            msg = (
                Message(MessageSegment.reply(event.message_id))
                + f"你今天已经娶过啦，你的老婆是 {existing.wife_nickname} 哦~\n"
                + MessageSegment.image(avatar_url)
            )
            await marry_cmd.send(msg)
            return

        # 获取群成员列表
        members = await bot.get_group_member_list(group_id=group_id)
        # 排除 bot 自身和发送者
        candidates = [
            m for m in members
            if str(m["user_id"]) != str(bot.self_id)
            and m["user_id"] != user_id
        ]

        if not candidates:
            await marry_cmd.send("群里没有可以娶的人啦……")
            return

        # 随机抽取
        chosen = random.choice(candidates)
        wife_id = chosen["user_id"]
        wife_nickname = chosen.get("card") or chosen.get("nickname") or str(wife_id)

        # 存入 DB
        session.add(DailyWife(
            user_id=user_id,
            group_id=group_id,
            wife_id=wife_id,
            wife_nickname=wife_nickname,
            date=today,
        ))
        await session.commit()

        # 发送结果
        avatar_url = f"http://q1.qlogo.cn/g?b=qq&nk={wife_id}&s=640"
        msg = (
            Message(MessageSegment.reply(event.message_id))
            + f"{wife_nickname} 成为了你的新老婆哦~\n"
            + MessageSegment.image(avatar_url)
        )
        await marry_cmd.send(msg)

    except Exception as e:
        logger.error(f"[marry] 娶群友失败: {e}")
        await marry_cmd.send("娶群友出了点问题……")
    finally:
        await session.close()
