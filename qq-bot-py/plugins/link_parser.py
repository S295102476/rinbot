"""link_parser.py
社交媒体链接解析白名单与误触发门卫。

nonebot-plugin-parser 默认对所有群开放（priority=5）。本插件在
priority=1 对已识别的平台链接执行白名单控制，并拦截已知的 B 站解析
误触发输入，避免普通文本（如 ``bmpt``）被当作 BV 号。

支持平台：B站 / 抖音 / Twitter(X) / YouTube
"""
import re
from pathlib import Path

import yaml
from nonebot import on_message
from nonebot.adapters.onebot.v11 import GroupMessageEvent
from nonebot.matcher import Matcher

# ── 读取 config.yaml 中的白名单配置 ──────────────────────────────
_config_path = Path("config.yaml")
if not _config_path.exists():
    _config_path = Path(__file__).parent.parent / "config.yaml"
_cfg = yaml.safe_load(_config_path.read_text(encoding="utf-8"))
_lp_cfg = _cfg.get("link_parser", {})

# 白名单群号集合；空列表 = 不启用白名单（所有群均可解析）
ALLOWED_GROUPS: set[int] = set(_lp_cfg.get("allowed_groups", []))

# ── 可解析链接的检测正则 ──────────────────────────────────────────
# 与 nonebot-plugin-parser 支持的平台关键词保持一致（仅开启的三个平台）
_URL_PAT = re.compile(
    r"b23\.tv/"                    # B站短链
    r"|bilibili\.com/"             # B站完整链接
    r"|BV[A-Za-z0-9]{10}"          # BV号（纯文本）
    r"|av\d+"                      # av号（纯文本）
    r"|v\.douyin\.com/"            # 抖音短链
    r"|douyin\.com/video/"         # 抖音完整链接（PC端）
    r"|twitter\.com/\w+/status/"   # Twitter
    r"|x\.com/\w+/status/"         # X.com
    r"|(?:www\.)?youtube\.com/(?:watch\?v=|shorts/|live/)"  # YouTube
    r"|youtu\.be/",                # YouTube short link
    re.IGNORECASE,
)

# Some versions of nonebot-plugin-parser treat a bare ASCII ``b`` prefix as a
# Bilibili candidate before validating its BV number.  Keep that protection
# deliberately narrow: valid b23/bilibili/BV forms are never blocked here.
_INVALID_BILIBILI_PREFIX = re.compile(
    r"^\s*b(?!"
    r"23\.tv/"
    r"|ilibili\.com/"
    r"|v[a-z0-9]{10}(?![a-z0-9])"
    r")",
    re.IGNORECASE,
)


def _is_parseable(event: GroupMessageEvent) -> bool:
    """检查消息是否包含可被解析插件处理的内容。"""
    # 1. 纯文本链接 / BV号 / av号
    if _URL_PAT.search(event.get_plaintext()):
        return True
    # 2. JSON/APP 消息段（B站小程序卡片、分享卡片等）
    for seg in event.message:
        if seg.type in ("json", "app"):
            raw = str(seg.data)
            lowered = raw.lower()
            if (
                "bilibili" in lowered
                or "b23.tv" in lowered
                or "youtube.com" in lowered
                or "youtu.be" in lowered
            ):
                return True
    return False


def _looks_like_invalid_bilibili_candidate(event: GroupMessageEvent) -> bool:
    """Detect the parser's broad BV false-positive shape without touching URLs."""
    if _is_parseable(event):
        return False
    return bool(_INVALID_BILIBILI_PREFIX.search(event.get_plaintext() or ""))


# ── 门卫 Matcher：priority=1，block=False ────────────────────────
# block=False：允许正常消息继续传播到其他插件
# stop_propagation()：仅在需要拦截时才阻止低优先级 Matcher（包括 nonebot-plugin-parser priority=5）
_gate = on_message(priority=1, block=False)


@_gate.handle()
async def link_parser_gate(matcher: Matcher, event: GroupMessageEvent) -> None:
    """Only let verified, whitelisted platform links reach the parser plugin."""
    if _looks_like_invalid_bilibili_candidate(event):
        matcher.stop_propagation()
        await matcher.finish()

    if not _is_parseable(event):
        return

    # Empty whitelist means every group may parse recognized links.
    if not ALLOWED_GROUPS or event.group_id in ALLOWED_GROUPS:
        return

    matcher.stop_propagation()
    await matcher.finish()
