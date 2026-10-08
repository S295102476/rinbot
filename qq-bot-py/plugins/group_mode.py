"""群功能模式：
- local_disabled_groups: 仅早柚/GsHub 模式
- gb_frame_only_groups: 仅 #GB 帧数查询模式
"""

import re
import yaml
from nonebot.adapters.onebot.v11 import Bot, Event, GroupMessageEvent
from nonebot.exception import IgnoredException
from nonebot.log import logger
from nonebot.message import event_preprocessor

from .gsuid_commands import _is_game_command

with open("config.yaml", "r", encoding="utf-8") as _f:
    _config = yaml.safe_load(_f)

_mode_cfg = _config.get("group_mode", {})
LOCAL_DISABLED_GROUPS: set[int] = {
    int(g) for g in _mode_cfg.get("local_disabled_groups", [])
}
GB_FRAME_ONLY_GROUPS: set[int] = {
    int(g) for g in _mode_cfg.get("gb_frame_only_groups", [])
}
# These are still normal Agent chat groups. Manual commands and every GsCore
# entry point are blocked; Agent tools call their Python services directly and
# remain usable. Global duty-roster commands are exempted below.
CHAT_ONLY_GROUPS: set[int] = {
    int(g) for g in _mode_cfg.get("chat_only_groups", [])
}
# Global roster commands are also allowed in chat-only groups.  The roster
# plugin performs the administrator check for mutating commands; keeping the
# gate here means those commands are not discarded before the matcher sees them.
CHAT_ONLY_ALLOWED_COMMANDS = frozenset({"搜本子", "值班表", "随机排班", "手动排班"})


def _is_duty_roster_command(text: str) -> bool:
    normalized = (text or "").lstrip().replace("＃", "#", 1)
    return bool(re.match(r"^#\s*(?:值班表|随机排班|手动排班)(?:\s|$)", normalized))


def _is_gsuid_command(text: str) -> bool:
    return _is_game_command(text)


def _is_gb_frame_command(text: str) -> bool:
    text = (text or "").strip().replace("＃", "#")
    text = re.sub(r"^#\s+", "#", text)
    text = text.lower()
    return text == "#gb" or text.startswith("#gb ")


def _is_hash_command(text: str) -> bool:
    return (text or "").lstrip().replace("＃", "#", 1).startswith("#")


def _is_chat_only_allowed_command(text: str) -> bool:
    normalized = (text or "").lstrip().replace("＃", "#", 1)
    match = re.match(r"^#\s*([^\s]+)", normalized)
    return bool(match and match.group(1) in CHAT_ONLY_ALLOWED_COMMANDS)


def _is_chat_only_blocked(text: str) -> bool:
    """Block every GsCore command and local command except configured exceptions."""
    if _is_gsuid_command(text):
        return True
    return _is_hash_command(text) and not _is_chat_only_allowed_command(text)


@event_preprocessor
async def _local_feature_gate(bot: Bot, event: Event):
    group_id = int(getattr(event, "group_id", 0) or 0)
    if group_id and group_id not in {int(g) for g in _config.get("allowed_groups", [])}:
        raise IgnoredException("group is not in allowed_groups")
    if not isinstance(event, GroupMessageEvent):
        return
    from .console_runtime import observe_incoming
    observe_incoming(bot, event)
    text = event.get_plaintext().strip()

    # The roster is global, so its query and admin maintenance commands must
    # remain reachable even in groups that disable other local features.
    if _is_duty_roster_command(text):
        return

    if group_id in CHAT_ONLY_GROUPS and _is_chat_only_blocked(text):
        reason = "chat_only_gsuid" if _is_gsuid_command(text) else "chat_only_command"
        logger.debug(f"[router] group={group_id} route=ignored reason={reason}")
        raise IgnoredException("commands are disabled in this chat-only group")

    if group_id in GB_FRAME_ONLY_GROUPS:
        if _is_gb_frame_command(text):
            return
        raise IgnoredException("only gbvsr frame lookup enabled in this group")

    if group_id not in LOCAL_DISABLED_GROUPS:
        return

    if _is_gsuid_command(text):
        return

    raise IgnoredException("local qqbot features disabled in this group")
