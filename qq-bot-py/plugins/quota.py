"""群调用次数限制插件

功能：
- 按群配置每日调用上限（群总量 + 每人上限），每天 0 点自动重置
- 不填 / ~ = 不限
- 超限时：当天对该用户第一次回复提示，后续静默忽略
- 豁免（不计次数）：#签到  #娶群友  🦌  不🦌  补🦌  补不🦌  N🦌不🦌
- 计数范围：# 开头的所有指令 + @机器人

config.yaml 示例：
  quota:
    default:
      group_limit: ~     # 不限
      user_limit: 10     # 每人每天 10 次
    groups:
      123456:
        group_limit: ~   # 该群不限群总量
        user_limit: 20   # 该群每人每天 20 次
"""

import asyncio
import re
from datetime import datetime

import redis as redis_lib
import yaml
from nonebot.exception import IgnoredException
from nonebot.log import logger
from nonebot.message import event_preprocessor
from nonebot.adapters.onebot.v11 import Bot, Event, GroupMessageEvent

# ---------- 配置 ----------
with open("config.yaml", "r", encoding="utf-8") as _f:
    _config = yaml.safe_load(_f)

_quota_cfg = _config.get("quota", {})
_default_cfg = _quota_cfg.get("default", {})
_groups_cfg: dict = _quota_cfg.get("groups", {})

# 豁免：完全匹配
_EXEMPT_EXACT: set[str] = {"🦌", "不🦌"}
# 豁免：前缀匹配
_EXEMPT_PREFIXES: tuple[str, ...] = ("#签到", "#娶群友", "补🦌", "补不🦌")# 豁免：正则（N🦌不🦌）
_EXEMPT_REGEX = re.compile(r"^(\d{1,2})?🦌不🦌$")
_SEARCH_TRIGGER_RE = re.compile(
    r"^\s*凛[\s,，。.!！?？~～、：:;；\-—_]*"
    r"(?P<prefix>.{0,12}?)"
    r"(?P<verb>搜索|搜|查找|查)"
    r"(?P<suffix>一下|一哈|看看|下|找|找一下)?"
    r"[\s,，。.!！?？~～、：:;；\-—_]*"
    r"(?P<query>.*)$",
    re.DOTALL,
)

# ---------- Redis（同步客户端，与项目其他插件保持一致）----------
rds = redis_lib.Redis(
    host=_config["redis"]["host"],
    port=_config["redis"]["port"], password=_config["redis"].get("password") or None, db=int(_config["redis"].get("db", 0)),
    decode_responses=True,
)


# ---------- 工具函数 ----------

def _today() -> str:
    return datetime.now().strftime("%Y%m%d")


def _ttl_to_eod() -> int:
    """距今天 23:59:59 还剩多少秒（用于 Redis key 过期）"""
    now = datetime.now()
    eod = now.replace(hour=23, minute=59, second=59, microsecond=0)
    return max(1, int((eod - now).total_seconds()))


def _get_limits(group_id: int) -> tuple[int | None, int | None]:
    """返回 (group_limit, user_limit)，None 表示不限"""
    grp = _groups_cfg.get(group_id) or _groups_cfg.get(str(group_id)) or {}

    def _val(key: str) -> int | None:
        v = grp.get(key)
        if v is None:
            v = _default_cfg.get(key)
        if v is None:
            return None
        return int(v)

    return _val("group_limit"), _val("user_limit")


def _is_valid_call(event: GroupMessageEvent, bot_self_id: str) -> bool:
    """判断该消息是否应该计入次数"""
    text = event.get_plaintext().strip()

    # 豁免：精确匹配（🦌 / 不🦌）
    if text in _EXEMPT_EXACT:
        return False

    # 豁免：前缀匹配（#签到 / #娶群友）
    for p in _EXEMPT_PREFIXES:
        if text.startswith(p):
            return False

    # 豁免：N🦌不🦌 正则
    if _EXEMPT_REGEX.match(text):
        return False

    # 有效调用：# 开头的指令
    if text.startswith("#"):
        return True

    # 有效调用：凛……搜/搜索/查/查找 系列
    search_match = _SEARCH_TRIGGER_RE.match(text)
    if search_match and (
        not search_match.group("prefix")
        or re.search(r"(帮|帮我|帮忙|麻烦|请|你|给我|替我|来|去|能不能|可以)", search_match.group("prefix") or "")
    ):
        return True

    # 有效调用：NoneBot 标记为 to_me 的消息（昵称触发、回复 bot 等）
    if event.to_me:
        return True

    # 有效调用：@机器人
    for seg in event.message:
        if seg.type == "at" and str(seg.data.get("qq")) == bot_self_id:
            return True

    return False


def _redis_check_and_incr(
    group_id: int,
    user_id: int,
    group_limit: int | None,
    user_limit: int | None,
    today: str,
    ttl: int,
) -> tuple[bool, bool, str, int, int]:
    """
    用 pipeline 原子性地 INCR 计数器；超限则 DECR 回退。
    返回 (group_exceeded, user_exceeded, notify_key, group_count, user_count)
    """
    g_key = f"quota:g:{group_id}:{today}"
    u_key = f"quota:u:{group_id}:{user_id}:{today}"
    notify_key = f"quota:notified:{group_id}:{user_id}:{today}"

    pipe = rds.pipeline()
    pipe.incr(g_key)
    pipe.incr(u_key)
    g_count, u_count = pipe.execute()

    # 首次写入时设 TTL（INCR 返回 1 代表该 key 是新建的）
    if g_count == 1:
        rds.expire(g_key, ttl)
    if u_count == 1:
        rds.expire(u_key, ttl)

    group_exceeded = group_limit is not None and g_count > group_limit
    user_exceeded = user_limit is not None and u_count > user_limit

    if group_exceeded or user_exceeded:
        # 超限：回退 INCR，不消耗次数
        pipe2 = rds.pipeline()
        pipe2.decr(g_key)
        pipe2.decr(u_key)
        pipe2.execute()

    return group_exceeded, user_exceeded, notify_key, g_count, u_count


# ---------- preprocessor 拦截 ----------

@event_preprocessor
async def _quota_check(bot: Bot, event: Event):
    from .minigame_gate import capture, allows
    game_arrival = capture(int(bot.self_id), event)
    from .console_runtime import observe_incoming, ready, group_enabled
    observe_incoming(bot, event)
    if not isinstance(event, GroupMessageEvent):
        return
    if game_arrival is not None and allows(int(event.group_id)):
        return
    if ready() and group_enabled(int(event.group_id)) and not event.get_plaintext().lstrip().startswith(("#", "＃", ".")):
        # Agent replies use confirmed reply rounds; legacy command quotas stay separate.
        return

    if not _is_valid_call(event, str(bot.self_id)):
        return

    group_id = int(event.group_id)
    user_id = int(event.user_id)
    group_limit, user_limit = _get_limits(group_id)

    # 两个限制都为 None，直接放行
    if group_limit is None and user_limit is None:
        return

    today = _today()
    ttl = _ttl_to_eod()
    loop = asyncio.get_event_loop()

    group_exceeded, user_exceeded, notify_key, group_count, user_count = await loop.run_in_executor(
        None,
        _redis_check_and_incr,
        group_id,
        user_id,
        group_limit,
        user_limit,
        today,
        ttl,
    )

    logger.debug(
        f"[quota] 计数 | group={group_id} user={user_id} "
        f"group={group_count}/{group_limit or '∞'} user={user_count}/{user_limit or '∞'}"
    )

    if not group_exceeded and not user_exceeded:
        return

    # 今天对该用户是否已经提示过（nx=True 保证只写入一次）
    def _mark_notified() -> bool:
        return rds.set(notify_key, "1", ex=ttl, nx=True) is not None

    should_notify = await loop.run_in_executor(None, _mark_notified)

    if should_notify:
        if user_exceeded:
            msg = f"[CQ:at,qq={user_id}] 你今天的调用次数已用完（上限 {user_limit} 次），明天再来吧~"
        else:
            msg = f"[CQ:at,qq={user_id}] 本群今日调用次数已用完（上限 {group_limit} 次），明天再来吧~"
        try:
            await bot.send_group_msg(group_id=group_id, message=msg)
        except Exception as e:
            logger.warning(f"[quota] 发送超限提示失败: {e}")

    logger.debug(
        f"[quota] 拦截 | group={group_id} user={user_id} "
        f"group_exceeded={group_exceeded} user_exceeded={user_exceeded}"
    )
    raise IgnoredException("quota exceeded")
