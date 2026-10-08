"""用户黑名单插件

功能：
- 被拉黑的 QQ 号发的所有消息对 bot 完全不可见（@、群聊、私聊均屏蔽）
- 黑名单存储于 MySQL blacklist 表，内存缓存加速判断
- 管理员指令（仅 config.yaml 中 admin_users 可用）：
    #黑名单 <QQ号>        — 手动拉黑
    #移除黑名单 <QQ号>    — 解除拉黑
- 自动触发：5分钟内 @bot 超过 30 次 → 自动拉黑
"""

import time
from collections import defaultdict

import yaml
from nonebot import on_command, get_driver
from nonebot.message import event_preprocessor
from nonebot.exception import IgnoredException
from nonebot.adapters.onebot.v11 import Bot, Event, GroupMessageEvent, Message
from nonebot.log import logger
from sqlalchemy import select, delete

from .db import Base, engine, get_session, Blacklist

# ---------- 配置 ----------
with open("config.yaml", "r", encoding="utf-8") as _f:
    _config = yaml.safe_load(_f)

# 管理员列表（从 setu.admin_users 读取，和其他插件保持一致）
_ADMINS: set[int] = {int(u) for u in _config.get("setu", {}).get("admin_users", [])}
_AGENT_GROUPS: set[int] = {
    int(value) for value in ((_config.get("agent") or {}).get("active_groups") or [])
}

# ---------- 内存缓存 ----------
_blocked: set[int] = set()  # 启动时从 DB 加载，运行时同步更新

# ---------- 频率统计（内存，重启清零） ----------
# {user_id: [timestamp, timestamp, ...]}  只保留最近 5 分钟的时间戳
_AT_WINDOW = 300        # 5 分钟窗口（秒）
_AT_THRESHOLD = 30      # 窗口内 @bot 次数阈值
_at_times: dict[int, list[float]] = defaultdict(list)


# ---------- 建表 + 加载 ----------
@get_driver().on_startup  # type: ignore[attr-defined]
async def _init_blacklist():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session = await get_session()
    try:
        rows = (await session.execute(select(Blacklist.user_id))).scalars().all()
        _blocked.update(rows)
        logger.info(f"[blocklist] 已加载黑名单 {len(_blocked)} 条")
    finally:
        await session.close()


# ---------- 全局拦截 ----------
@event_preprocessor
async def _block_check(bot: Bot, event: Event):
    from .console_runtime import observe_incoming
    observe_incoming(bot, event)
    uid = getattr(event, "user_id", None)
    if uid is None:
        return
    uid = int(uid)

    # 已在黑名单 → 直接丢弃
    if uid in _blocked:
        raise IgnoredException("blocked user")

    # 统计 @bot 频率（仅群消息）
    if isinstance(event, GroupMessageEvent) and int(event.group_id) in _AGENT_GROUPS:
        for seg in event.message:
            if seg.type == "at" and str(seg.data.get("qq")) == str(bot.self_id):
                now = time.time()
                times = _at_times[uid]
                times.append(now)
                # 清理窗口外的记录
                _at_times[uid] = [t for t in times if now - t <= _AT_WINDOW]
                if len(_at_times[uid]) >= _AT_THRESHOLD:
                    member_info = {}
                    try:
                        member_info = await bot.get_group_member_info(group_id=event.group_id, user_id=uid)
                    except Exception:
                        pass
                    nick = member_info.get("card") or member_info.get("nickname") or str(uid)
                    await _add_to_blacklist(uid, "auto_spam", nickname=nick, group_id=event.group_id)
                    logger.warning(f"[blocklist] 自动拉黑 {uid}({nick})：{_AT_WINDOW}s 内 @bot {len(_at_times[uid])} 次")
                    try:
                        await bot.send_group_msg(
                            group_id=event.group_id,
                            message=f"[CQ:at,qq={uid}] 你被自动加入黑名单（频繁刷屏）。"
                        )
                    except Exception:
                        pass
                    raise IgnoredException("auto banned")
                break


# ---------- 数据库操作 ----------
async def _add_to_blacklist(
    user_id: int,
    reason: str = "manual",
    nickname: str = "",
    group_id: int | None = None,
):
    _blocked.add(user_id)
    session = await get_session()
    try:
        exists = (await session.execute(
            select(Blacklist).where(Blacklist.user_id == user_id)
        )).scalars().first()
        if not exists:
            session.add(Blacklist(user_id=user_id, reason=reason, nickname=nickname, group_id=group_id))
            await session.commit()
    finally:
        await session.close()


async def _remove_from_blacklist(user_id: int):
    _blocked.discard(user_id)
    _at_times.pop(user_id, None)
    session = await get_session()
    try:
        await session.execute(delete(Blacklist).where(Blacklist.user_id == user_id))
        await session.commit()
    finally:
        await session.close()


# ---------- 管理员指令 ----------
bl_add = on_command("#黑名单", priority=1, block=True)
bl_rm  = on_command("#移除黑名单", priority=1, block=True)
bl_ls  = on_command("#拉黑名单", priority=1, block=True)


@bl_add.handle()
async def handle_bl_add(bot: Bot, event: GroupMessageEvent):
    if event.user_id not in _ADMINS:
        return
    arg = event.get_plaintext().removeprefix("#黑名单").strip()
    if not arg.isdigit():
        await bl_add.send("用法：#黑名单 <QQ号>")
        return
    uid = int(arg)
    member_info = {}
    try:
        member_info = await bot.get_group_member_info(group_id=event.group_id, user_id=uid)
    except Exception:
        pass
    nick = member_info.get("card") or member_info.get("nickname") or str(uid)
    await _add_to_blacklist(uid, reason="manual", nickname=nick, group_id=event.group_id)
    await bl_add.send(f"已将 {nick}({uid}) 加入黑名单。")


@bl_rm.handle()
async def handle_bl_rm(bot: Bot, event: GroupMessageEvent):
    if event.user_id not in _ADMINS:
        return
    arg = event.get_plaintext().removeprefix("#移除黑名单").strip()
    if not arg.isdigit():
        await bl_rm.send("用法：#移除黑名单 <QQ号>")
        return
    uid = int(arg)
    await _remove_from_blacklist(uid)
    await bl_rm.send(f"已将 {uid} 移出黑名单。")


@bl_ls.handle()
async def handle_bl_ls(bot: Bot, event: GroupMessageEvent):
    if event.user_id not in _ADMINS:
        return
    session = await get_session()
    try:
        rows = (await session.execute(
            select(Blacklist).order_by(Blacklist.created_at.desc())
        )).scalars().all()
    finally:
        await session.close()
    if not rows:
        await bl_ls.send("黑名单为空。")
        return
    lines = [f"共 {len(rows)} 人："]
    for r in rows:
        ts = r.created_at.strftime("%m-%d %H:%M")
        nick = r.nickname or "未知"
        reason_map = {"manual": "手动", "auto_spam": "自动(刷屏)"}
        reason_str = reason_map.get(r.reason, r.reason)
        group_str = f"群{r.group_id}" if r.group_id else "未知群"
        lines.append(f"  [{ts}] {nick}({r.user_id})  原因:{reason_str}  来源:{group_str}")
    await bl_ls.send("\n".join(lines))
