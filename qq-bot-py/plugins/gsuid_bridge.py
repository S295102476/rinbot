"""
gsuid_bridge.py
将游戏指令透传给 gsuid_core，支持：
  鸣潮 (前缀: ww)
  终末地 (前缀: end / zmd)
"""

import asyncio
import json
import re
import uuid
from typing import Dict, List, Optional

import websockets
import websockets.exceptions
from nonebot import get_bots, get_driver, on_message
from nonebot.adapters.onebot.v11 import (
    Bot,
    Message,
    MessageEvent,
    MessageSegment,
    GroupMessageEvent,
)
from nonebot.exception import StopPropagation
from nonebot.log import logger

from .gsuid_commands import (
    _is_core_command,
    _is_direct_only_core_command,
    _is_game_command,
    _normalize_core_command_text,
)
from .group_mode import CHAT_ONLY_GROUPS

# ──────────────────────────────────────────────
# 配置
# ──────────────────────────────────────────────

from runtime_config import feature_enabled, load_config
_bridge_config = load_config()
_gsuid_config = _bridge_config.get("gsuid") or {}
GSUID_ENABLED = feature_enabled(_bridge_config, "gsuid") and bool(_gsuid_config.get("enabled", False))
GSUID_WS_URL = str(_gsuid_config.get("ws_url") or "")
RESPONSE_TIMEOUT = 30   # 等待 gsuid_core 首次响应的超时（秒）
DRAIN_TIMEOUT = 2       # 收到首条响应后，继续等待后续消息的超时（秒）

# ──────────────────────────────────────────────
# 全局状态
# ──────────────────────────────────────────────

_ws: Optional[websockets.WebSocketClientProtocol] = None
# msg_id -> asyncio.Queue，收到的响应放入对应队列
_pending: Dict[str, asyncio.Queue] = {}
# 连接就绪事件，建立连接时 set，断开时 clear
_connected: Optional[asyncio.Event] = None


# ──────────────────────────────────────────────
# WebSocket 连接与后台监听
# ──────────────────────────────────────────────

async def _ws_listener():
    global _ws
    while True:
        try:
            logger.info("[GsuidBridge] 正在连接 gsuid_core WebSocket...")
            _ws = await websockets.connect(
                GSUID_WS_URL, max_size=2**25, open_timeout=30
            )
            if _connected is not None:
                _connected.set()
            logger.success("[GsuidBridge] 已连接至 gsuid_core！")
            async for raw in _ws:
                try:
                    data = json.loads(raw)
                    msg_id = data.get("msg_id", "")
                    content = data.get("content") or []
                    logger.debug(
                        f"[GsuidBridge] 收到响应: matched={msg_id in _pending} segments={len(content)}"
                    )
                    if msg_id and msg_id in _pending:
                        await _pending[msg_id].put(content)
                    else:
                        # ``target_send`` is also used by GsCore for主动推送
                        # (抽卡登录完成、插件更新通知、定时任务等). 这些消息
                        # 没有对应的入站 msg_id，不能只丢进 warning，否则
                        # GsCore 看似发送成功，QQ 用户却永远收不到。
                        if await _send_unsolicited(data):
                            logger.info(
                                f"[GsuidBridge] 主动推送已处理: segments={len(content)}"
                            )
                        else:
                            logger.warning(
                                f"[GsuidBridge] 未匹配响应（当前等待数量={len(_pending)}）"
                            )
                except Exception as e:
                    logger.warning(f"[GsuidBridge] 消息解析异常: {type(e).__name__}")
        except Exception as e:
            logger.warning(f"[GsuidBridge] 连接断开: {type(e).__name__}，5秒后重连...")
            if _connected is not None:
                _connected.clear()
            _ws = None
            await asyncio.sleep(5)


driver = get_driver()


@driver.on_startup
async def start_bridge():
    global _connected
    if not GSUID_ENABLED:
        return
    if not GSUID_WS_URL.startswith(("ws://", "wss://")):
        raise ValueError("Configure gsuid.ws_url before enabling Core.")
    _connected = asyncio.Event()
    asyncio.create_task(_ws_listener())
    logger.info("[GsuidBridge] 后台 WebSocket 监听器已启动")


# ──────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────

def _get_user_pm(event: MessageEvent) -> int:
    """根据群成员角色返回权限等级（越小越高）"""
    if isinstance(event, GroupMessageEvent):
        role = getattr(event.sender, "role", "member") or "member"
        return {"owner": 1, "admin": 2}.get(role, 3)
    return 3


async def _forward(bot: Bot, event: MessageEvent, msg_id: str) -> bool:
    """将消息发送给 gsuid_core，返回是否发送成功"""
    # 若连接尚未就绪（bot 刚启动），等待最多 10 秒
    if _connected is not None and not _connected.is_set():
        try:
            await asyncio.wait_for(_connected.wait(), timeout=10)
        except asyncio.TimeoutError:
            logger.warning("[GsuidBridge] 等待连接超时，跳过转发")
            return False
    if not _ws:
        logger.warning("[GsuidBridge] WebSocket 未连接，跳过转发")
        return False

    # 提取文本和图片内容
    content = []
    for seg in event.message:
        if seg.type == "text":
            t = seg.data.get("text", "").strip()
            if t:
                t = _normalize_core_command_text(t)
                content.append({"type": "text", "data": t})
        elif seg.type == "image":
            url = seg.data.get("url") or seg.data.get("file", "")
            if url:
                content.append({"type": "image", "data": url})

    if not content:
        return False

    if isinstance(event, GroupMessageEvent):
        user_type = "group"
        group_id = str(event.group_id)
    else:
        user_type = "direct"
        group_id = str(event.user_id)

    msg = {
        "bot_id": "Nonebot",
        "bot_self_id": str(bot.self_id),
        "msg_id": msg_id,
        "user_type": user_type,
        "group_id": group_id,
        "user_id": str(event.user_id),
        "sender": {
            "nickname": getattr(event.sender, "nickname", "") or "",
            "card": getattr(event.sender, "card", "") or "",
        },
        "user_pm": _get_user_pm(event),
        "content": content,
    }

    try:
        # gsuid_core 使用 receive_bytes()，必须发送二进制帧
        await _ws.send(json.dumps(msg, ensure_ascii=False).encode("utf-8"))
        return True
    except Exception as e:
        logger.warning(f"[GsuidBridge] 发送消息失败: {type(e).__name__}")
        return False


async def _collect_responses(msg_id: str, q: asyncio.Queue = None) -> List[List]:
    """
    等待 gsuid_core 的所有回复。
    先等待第一条（超时 RESPONSE_TIMEOUT 秒），
    之后在 DRAIN_TIMEOUT 内持续收集后续消息（用于图片+文字分批发送的情况）。
    q: 可传入已注册的队列（避免竞态），为 None 时自动创建。
    """
    if q is None:
        q = asyncio.Queue()
        _pending[msg_id] = q
    results = []
    try:
        first = await asyncio.wait_for(q.get(), timeout=RESPONSE_TIMEOUT)
        results.append(first)
        while True:
            try:
                more = await asyncio.wait_for(q.get(), timeout=DRAIN_TIMEOUT)
                results.append(more)
            except asyncio.TimeoutError:
                break
    except asyncio.TimeoutError:
        pass
    finally:
        _pending.pop(msg_id, None)
    return results


# 技术性错误关键词，命中时只记录日志不发送到群
_ERROR_KEYWORDS = (
    "渲染失败", "执行失败", "Playwright", "BrowserType",
    "doesn't exist", "playwright install", "HTML渲染",
    "Traceback", "Exception",
    "请求错误", "请求失败", "连接失败", "错误码",
    "unauthorized", "forbidden", "authentication failed",
)


def _is_tech_error(content: list) -> bool:
    """判断一条响应是否为技术性错误（不应转发给用户）"""
    for seg in content:
        if not isinstance(seg, dict):
            continue
        if seg.get("type") == "node" and isinstance(seg.get("data"), list):
            if _is_tech_error(seg["data"]):
                return True
        if seg.get("type") == "text":
            text = str(seg.get("data", ""))
            if (any(kw.casefold() in text.casefold() for kw in _ERROR_KEYWORDS)
                    or re.search(r"\berror\b|\bHTTP\s*[45]\d\d\b", text, re.IGNORECASE)):
                return True
    return False


def _extract_segments(segs: list) -> List[MessageSegment]:
    """将 gsuid_core 的 content 列表递归展开为 NoneBot MessageSegment 列表"""
    parts: List[MessageSegment] = []
    for seg in segs:
        seg_type = seg.get("type")
        seg_data = seg.get("data")
        if not seg_data:
            continue
        if seg_type == "text":
            parts.append(MessageSegment.text(str(seg_data)))
        elif seg_type == "image":
            data = str(seg_data)
            if data.startswith("base64://") or data.startswith("http"):
                parts.append(MessageSegment.image(data))
            else:
                parts.append(MessageSegment.image(f"base64://{data}"))
        elif seg_type == "node":
            # 合并转发：递归展开内层消息
            if isinstance(seg_data, list):
                parts.extend(_extract_segments(seg_data))
    return parts


def _coerce_target_id(value):
    """保留非数字 ID，同时把 OneBot 常见的数字字符串转为 int。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return int(text) if text.isdigit() else text


def _select_bot(data: dict):
    """选择 GsCore 主动推送对应的 NoneBot 实例。"""
    bots = get_bots()
    if not bots:
        return None

    wanted_self_id = str(data.get("bot_self_id") or "").strip()
    if wanted_self_id:
        for bot in bots.values():
            if str(getattr(bot, "self_id", "")) == wanted_self_id:
                return bot

    # 早柚旧版本的 MessageSend 可能只带 bot_id，不带 bot_self_id。
    wanted_bot_id = str(data.get("bot_id") or "").strip()
    if wanted_bot_id:
        for bot in bots.values():
            if str(getattr(bot, "type", "")) == wanted_bot_id:
                return bot

    # 当前部署只有一个 NoneBot 账号时，直接使用唯一实例。
    if len(bots) == 1:
        return next(iter(bots.values()))
    return None


async def _send_unsolicited(data: dict) -> bool:
    """把 GsCore 的无请求主动消息投递到 OneBot 目标会话。"""
    target_type = str(data.get("target_type") or "").lower()
    target_id = _coerce_target_id(data.get("target_id"))
    if target_type not in {"direct", "private", "friend", "group"} or target_id is None:
        # 日志帧（target_type/target_id 为空）不应当当成 QQ 消息发送。
        return False

    if target_type == "group" and target_id in CHAT_ONLY_GROUPS:
        logger.debug(
            f"[GsuidBridge] 已抑制纯聊天群主动推送: group={target_id}"
        )
        # 该帧已被有意处理，避免监听器把它记成“未匹配响应”。
        return True

    content = data.get("content") or []
    if _is_tech_error(content):
        logger.warning("[GsuidBridge] 已屏蔽主动推送中的技术性错误")
        return True
    parts = _extract_segments(content)
    if not parts:
        return False

    bot = _select_bot(data)
    if bot is None:
        logger.warning("[GsuidBridge] 主动推送找不到对应的 NoneBot 实例")
        return False

    message = Message(parts)
    try:
        if target_type in {"direct", "private", "friend"}:
            await bot.send_private_msg(user_id=target_id, message=message)
        else:
            await bot.send_group_msg(group_id=target_id, message=message)
        return True
    except Exception as e:
        logger.warning(
            f"[GsuidBridge] 主动推送发送失败: {type(e).__name__}"
        )
        return False


async def _send_results(bot: Bot, event: MessageEvent, results: List[List]):
    """将 gsuid_core 返回的内容逐条发送给 QQ"""
    if isinstance(event, GroupMessageEvent) and int(event.group_id) in CHAT_ONLY_GROUPS:
        logger.debug(
            f"[GsuidBridge] 已抑制纯聊天群响应: group={int(event.group_id)}"
        )
        return
    for content in results:
        if _is_tech_error(content):
            logger.warning("[GsuidBridge] 已屏蔽技术性错误消息")
            continue
        parts = _extract_segments(content)
        if parts:
            try:
                await bot.send(event, Message(parts))
            except Exception as exc:
                logger.warning(f"[GsuidBridge] 响应发送失败: {type(exc).__name__}")


# ──────────────────────────────────────────────
# NoneBot 消息处理器
# ──────────────────────────────────────────────

# priority=3：高于 ai_chat(5) 和 group_chat(10)，保证游戏指令优先处理
# block=False：默认不阻断，仅在 gsuid_core 有实际响应时才 StopPropagation
game_handler = on_message(priority=3, block=False)


@game_handler.handle()
async def handle_game(bot: Bot, event: MessageEvent):
    if not GSUID_ENABLED:
        return
    if isinstance(event, GroupMessageEvent) and int(event.group_id) in CHAT_ONLY_GROUPS:
        return

    plain = event.get_plaintext().strip()

    # 非游戏指令，直接跳过，交由 ai_chat / group_chat 处理
    if not _is_game_command(plain):
        return

    is_core_command = _is_core_command(plain)

    if isinstance(event, GroupMessageEvent) and _is_direct_only_core_command(plain):
        await bot.send(event, "绑定设备仅支持私聊，请直接私聊机器人后再发送设备信息。")
        raise StopPropagation

    msg_id = str(uuid.uuid4())

    # 先注册队列再发送，避免响应先于队列建立而被丢失
    q: asyncio.Queue = asyncio.Queue()
    _pending[msg_id] = q

    sent = await _forward(bot, event, msg_id)
    if not sent:
        _pending.pop(msg_id, None)
        if is_core_command:
            await bot.send(
                event,
                "GsCore 当前未连接，命令未执行。请先启动 GsCore（8765 端口）后重试。",
            )
            raise StopPropagation
        return

    results = await _collect_responses(msg_id, q)
    if not results:
        # gsuid_core 无响应（命令不存在），允许后续处理器继续
        if is_core_command:
            await bot.send(
                event,
                "GsCore 未返回结果，命令可能未加载。请重启 GsCore 后重试。",
            )
            raise StopPropagation
        return

    await _send_results(bot, event, results)
    # 有游戏响应 → 阻止 ai_chat / group_chat 再次回复同一条消息
    raise StopPropagation
