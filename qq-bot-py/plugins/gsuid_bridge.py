"""
gsuid_bridge.py
将游戏指令透传给 gsuid_core，支持：
  鸣潮 (前缀: ww)
  终末地 (前缀: end / zmd)
"""

import asyncio
import json
import uuid
from typing import Dict, List, Optional

import websockets
import websockets.exceptions
from nonebot import get_driver, on_message
from nonebot.adapters.onebot.v11 import (
    Bot,
    Message,
    MessageEvent,
    MessageSegment,
    GroupMessageEvent,
)
from nonebot.exception import StopPropagation
from nonebot.log import logger

# ──────────────────────────────────────────────
# 配置
# ──────────────────────────────────────────────

GSUID_WS_URL = "ws://localhost:8765/ws/Nonebot"
RESPONSE_TIMEOUT = 30   # 等待 gsuid_core 首次响应的超时（秒）
DRAIN_TIMEOUT = 2       # 收到首条响应后，继续等待后续消息的超时（秒）

# 游戏插件的触发前缀（小写匹配，含中文前缀）
GAME_PREFIXES = ("ww", "end", "zmd", "ark", "mrfz", "zzz", "绝区零", "lol", "sr")

# gsuid_core 核心指令（无游戏前缀，需要完整匹配或前缀匹配，全部转发给 gsuid_core）
# 包含绑定 Cookie / 扫码登录等账号管理指令
CORE_COMMANDS = (
    "扫码登陆", "扫码登录",  # 米游社扫码登录（prefix=False，无需 core 前缀）
    "core",                  # 所有 core* 管理指令（core添加<ck>、core刷新CK 等）
)

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
                    target = f"{data.get('target_type')}:{data.get('target_id')}"
                    logger.debug(
                        f"[GsuidBridge] 收到响应: msg_id={msg_id!r} "
                        f"target={target} segments={len(content)}"
                    )
                    if msg_id and msg_id in _pending:
                        await _pending[msg_id].put(content)
                    else:
                        logger.warning(
                            f"[GsuidBridge] 未匹配响应 msg_id={msg_id!r} "
                            f"target={target} （当前等待数量={len(_pending)}）"
                        )
                except Exception as e:
                    logger.warning(f"[GsuidBridge] 消息解析异常: {e}")
        except Exception as e:
            logger.warning(f"[GsuidBridge] 连接断开: {e}，5秒后重连...")
            if _connected is not None:
                _connected.clear()
            _ws = None
            await asyncio.sleep(5)


driver = get_driver()


@driver.on_startup
async def start_bridge():
    global _connected
    _connected = asyncio.Event()
    asyncio.create_task(_ws_listener())
    logger.info("[GsuidBridge] 后台 WebSocket 监听器已启动")


# ──────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────

def _is_game_command(text: str) -> bool:
    """判断是否是游戏指令或核心指令（需要转发给 gsuid_core）"""
    lower = text.lower().lstrip()
    # 游戏前缀（小写不区分大小写）
    if any(lower.startswith(p) for p in GAME_PREFIXES):
        return True
    # 核心指令（原文匹配，含中文，不 lower）
    stripped = text.strip()
    if any(stripped == cmd or stripped.startswith(cmd) for cmd in CORE_COMMANDS):
        return True
    return False


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
        logger.warning(f"[GsuidBridge] 发送消息失败: {e}")
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
)


def _is_tech_error(content: list) -> bool:
    """判断一条响应是否为技术性错误（不应转发给用户）"""
    for seg in content:
        if seg.get("type") == "text":
            text = str(seg.get("data", ""))
            if any(kw in text for kw in _ERROR_KEYWORDS):
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


async def _send_results(bot: Bot, event: MessageEvent, results: List[List]):
    """将 gsuid_core 返回的内容逐条发送给 QQ"""
    for content in results:
        if _is_tech_error(content):
            logger.warning(f"[GsuidBridge] 屏蔽技术性错误消息: {content}")
            continue
        parts = _extract_segments(content)
        if parts:
            await bot.send(event, Message(parts))


# ──────────────────────────────────────────────
# NoneBot 消息处理器
# ──────────────────────────────────────────────

# priority=3：高于 ai_chat(5) 和 group_chat(10)，保证游戏指令优先处理
# block=False：默认不阻断，仅在 gsuid_core 有实际响应时才 StopPropagation
game_handler = on_message(priority=3, block=False)


@game_handler.handle()
async def handle_game(bot: Bot, event: MessageEvent):
    plain = event.get_plaintext().strip()

    # 非游戏指令，直接跳过，交由 ai_chat / group_chat 处理
    if not _is_game_command(plain):
        return

    msg_id = str(uuid.uuid4())

    # 先注册队列再发送，避免响应先于队列建立而被丢失
    q: asyncio.Queue = asyncio.Queue()
    _pending[msg_id] = q

    sent = await _forward(bot, event, msg_id)
    if not sent:
        _pending.pop(msg_id, None)
        return

    results = await _collect_responses(msg_id, q)
    if not results:
        # gsuid_core 无响应（命令不存在），允许后续处理器继续
        return

    await _send_results(bot, event, results)
    # 有游戏响应 → 阻止 ai_chat / group_chat 再次回复同一条消息
    raise StopPropagation
