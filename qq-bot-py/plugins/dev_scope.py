"""Development-time group event routing.

The live switch supports three modes: all Agent groups, configured test groups,
or direct-mention-only. Hash commands remain reachable in every mode. Private
messages remain available for the administrator development Agent unless
explicitly disabled.
"""

from __future__ import annotations

from pathlib import Path
import re

import yaml
from nonebot import on_message
from nonebot.adapters.onebot.v11 import (
    Bot,
    Event,
    GroupMessageEvent,
    MessageEvent,
    PokeNotifyEvent,
    PrivateMessageEvent,
)
from nonebot.exception import IgnoredException
from nonebot.log import logger
from nonebot.message import event_preprocessor
from nonebot.rule import Rule

from . import console_runtime


def _load_config() -> dict:
    path = Path("config.yaml")
    if not path.exists():
        path = Path(__file__).resolve().parents[1] / "config.yaml"
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


_config = _load_config()
_scope = _config.get("dev_scope") or {}
ENABLED = bool(_scope.get("enabled", False))
SCOPE_ENABLED = ENABLED
AT_ONLY_ENABLED = bool(_scope.get("at_only", False))
ALLOWED_GROUPS: frozenset[int] = frozenset(
    int(group_id) for group_id in (_scope.get("allowed_groups") or [])
)
ALLOW_PRIVATE = bool(_scope.get("allow_private", True))
_agent_config = _config.get("agent") or {}
AGENT_GROUPS: frozenset[int] = frozenset(
    int(group_id) for group_id in (_agent_config.get("active_groups") or [])
)
_dev_config = _agent_config.get("dev") or {}
ADMIN_USERS: frozenset[int] = frozenset(
    int(user_id) for user_id in (_dev_config.get("admin_users") or [])
)


def _scope_allows_group(
    group_id: int,
    text: str,
    *,
    mentions_bot: bool = False,
) -> bool:
    """Return whether a group event may reach matchers under the live scope switch."""
    normalized_text = str(text or "").lstrip().replace("＃", "#", 1)
    if normalized_text.startswith("#"):
        return True
    if console_runtime.ready():
        return console_runtime.scope_allows(int(group_id), direct_at=mentions_bot)
    if AT_ONLY_ENABLED:
        # Do not expose the legacy @ chat matcher in unrelated groups.  The
        # direct-mention entry point is for configured Agent groups only.
        return bool(mentions_bot and int(group_id) in AGENT_GROUPS)
    if not SCOPE_ENABLED:
        return True
    return int(group_id) in ALLOWED_GROUPS


def _message_mentions_bot(bot: Bot, event: GroupMessageEvent) -> bool:
    """Recognize only a real QQ @ segment, with raw OneBot data as fallback."""
    bot_id = str(bot.self_id)
    for segment in event.message:
        if segment.type == "at" and str(segment.data.get("qq", "")) == bot_id:
            return True

    raw = (getattr(event, "raw_message", "") or "") + "\n" + str(event.message)
    pattern = rf"\[(?:CQ:)?at[:,]qq={re.escape(bot_id)}(?:[,]?[^\]]*)\]"
    return bool(re.search(pattern, raw, flags=re.IGNORECASE))


def _message_has_parseable_link(event: GroupMessageEvent) -> bool:
    """Keep the video-link parser reachable without turning the message into Agent chat."""
    try:
        from .link_parser import _is_parseable

        return bool(_is_parseable(event))
    except Exception:
        # A parser plugin may be optional during startup/tests.  Its own
        # matcher remains the source of truth when it is available.
        return False


def _is_local_command_event(event: GroupMessageEvent) -> bool:
    """Commands owned by local plugins must survive the Agent-only gate."""
    text = (event.get_plaintext() or "").strip()
    if not text:
        return False

    # 鹿历 commands intentionally do not use a hash prefix.
    if (
        text in {"🦌", "不🦌", "🦌不🦌"}
        or re.fullmatch(r"\d{1,2}🦌不🦌", text)
        or re.fullmatch(r"补(?:不)?🦌\s+.+", text)
        or re.fullmatch(r"#(?:🦌|鹿)\s*测试", text)
        or re.fullmatch(r"[Jj][Mm]\d+", text)
    ):
        return True

    # CoC uses dot-prefixed commands (.r7d3, .st, .sc, ...).
    if text.startswith("."):
        return True

    # GsCore game/core commands have their own command recognizer and are
    # handled before Agent chat by plugins.gsuid_bridge.
    try:
        from .gsuid_commands import _is_game_command

        if _is_game_command(text):
            return True
    except Exception:
        pass
    return False


def agent_at_only_enabled(group_id: int | None = None) -> bool:
    """Whether Agent group chat is currently restricted to real @ mentions."""
    if console_runtime.ready():
        if group_id is not None:
            return console_runtime.group_at_only(int(group_id))
        return console_runtime.global_value("development_mode", "all") == "at"
    return AT_ONLY_ENABLED


def _normalize_fullwidth_hash(event: GroupMessageEvent) -> None:
    for segment in event.message:
        if segment.type != "text":
            continue
        value = str(segment.data.get("text") or "")
        stripped = value.lstrip()
        if stripped.startswith("＃"):
            prefix_length = len(value) - len(stripped)
            segment.data["text"] = value[:prefix_length] + "#" + stripped[1:]
            try:
                event.raw_message = str(event.raw_message).replace("＃", "#", 1)
            except Exception:
                pass
        return


@event_preprocessor
async def _development_scope_gate(_bot: Bot, event: Event):
    from .minigame_gate import capture, allows
    game_arrival = capture(int(_bot.self_id), event)
    console_runtime.observe_incoming(_bot, event)
    if ALLOW_PRIVATE and isinstance(event, PrivateMessageEvent):
        return
    group_id = getattr(event, "group_id", None)
    if group_id is None:
        return
    try:
        group_id = int(group_id)
    except (TypeError, ValueError):
        raise IgnoredException("invalid group scope")
    if isinstance(event, PokeNotifyEvent):
        # A poke is meaningful here only when the bot itself is the target.
        # When development scope is disabled, _scope_allows_group returns
        # True for every group; poke_reaction still limits delivery to the
        # configured Agent groups.
        if (
            int(event.target_id) == int(_bot.self_id)
            and _scope_allows_group(group_id, "", mentions_bot=False)
        ):
            logger.debug(f"[router] group={group_id} route=agent_batch poke=1")
            return
    if isinstance(event, GroupMessageEvent):
        _normalize_fullwidth_hash(event)
        if game_arrival is not None and allows(group_id):
            logger.debug(f"[router] group={group_id} route=minigame")
            return
        text = event.get_plaintext().strip()
        mentions_bot = _message_mentions_bot(_bot, event)
        has_parseable_link = _message_has_parseable_link(event)
        local_command = _is_local_command_event(event)
        if has_parseable_link:
            # Link parsing is an independent feature. Let its whitelist gate
            # decide whether to handle the URL, while group_chat still ignores
            # it in at-only mode because it has no real @ trigger.
            logger.debug(f"[router] group={group_id} route=link_parser")
            return
        if local_command:
            logger.debug(f"[router] group={group_id} route=command")
            return
        if _scope_allows_group(group_id, text, mentions_bot=mentions_bot):
            if text.startswith("#"):
                logger.debug(f"[router] group={group_id} route=command")
            elif agent_at_only_enabled(group_id):
                logger.debug(
                    f"[router] group={group_id} route=agent_batch trigger=direct_at"
                )
            elif group_id in ALLOWED_GROUPS:
                logger.debug(f"[router] group={group_id} route=agent_batch")
            return
    reason = "at_only_non_mention" if agent_at_only_enabled(group_id) else "inactive_non_command"
    logger.debug(f"[router] group={group_id} route=ignored reason={reason}")
    raise IgnoredException("group event is outside the active Agent scope")


def _is_scope_command(event: MessageEvent) -> bool:
    try:
        user_id = int(getattr(event, "user_id", 0))
    except (TypeError, ValueError):
        return False
    return (
        user_id in ADMIN_USERS
        and event.get_plaintext().strip().startswith("#开发监听")
    )


def _scope_command_rule() -> Rule:
    async def _rule(_bot: Bot, event: MessageEvent) -> bool:
        return _is_scope_command(event)

    return Rule(_rule)


scope_command = on_message(rule=_scope_command_rule(), priority=0, block=True)


@scope_command.handle()
async def handle_scope_command(bot: Bot, event: MessageEvent):
    try:
        user_id = int(getattr(event, "user_id", 0))
    except (TypeError, ValueError):
        return
    if user_id not in ADMIN_USERS:
        await bot.send(event, "开发监听开关只对 Agent 管理员开放。")
        return

    global SCOPE_ENABLED, AT_ONLY_ENABLED
    action = event.get_plaintext().strip()[len("#开发监听"):].strip().lower()
    # Accept both the short form ``#开发监听 at`` and the explicit form
    # ``#开发监听 agent at`` so the switch is self-explanatory in chat.
    action_parts = action.split()
    if len(action_parts) >= 2 and action_parts[0] in {"agent", "聊天", "chat"}:
        action = action_parts[1]
    live = console_runtime.ready()
    test_groups = console_runtime.global_value("test_groups", sorted(ALLOWED_GROUPS))
    if action in {"on", "开启", "打开", "1"}:
        if live:
            try:
                await console_runtime.SETTINGS.update_global({"development_mode": "test"}, actor=f"qq:{user_id}")
            except Exception as exc:
                logger.warning(f"[router] scope save failed: {type(exc).__name__}")
                await bot.send(event, "开发监听设置保存失败，当前模式未改变。")
                return
        SCOPE_ENABLED = True
        AT_ONLY_ENABLED = False
        logger.info(
            f"[router] development_scope=test groups={sorted(test_groups)} user={user_id}"
        )
        await bot.send(event, f"开发监听已开启，仅处理群：{', '.join(map(str, sorted(test_groups)))}")
    elif action in {"off", "关闭", "停用", "0"}:
        if live:
            try:
                await console_runtime.SETTINGS.update_global({"development_mode": "all"}, actor=f"qq:{user_id}")
            except Exception as exc:
                logger.warning(f"[router] scope save failed: {type(exc).__name__}")
                await bot.send(event, "开发监听设置保存失败，当前模式未改变。")
                return
        SCOPE_ENABLED = False
        AT_ONLY_ENABLED = False
        logger.info(f"[router] development_scope=all user={user_id}")
        await bot.send(event, "开发监听已关闭，恢复接收所有群事件。")
    elif action in {
        "at", "@", "at-only", "mention", "mention-only", "only-at",
        "仅@", "只@", "仅at", "仅提及",
    }:
        if live:
            try:
                await console_runtime.SETTINGS.update_global({"development_mode": "at"}, actor=f"qq:{user_id}")
            except Exception as exc:
                logger.warning(f"[router] scope save failed: {type(exc).__name__}")
                await bot.send(event, "开发监听设置保存失败，当前模式未改变。")
                return
        SCOPE_ENABLED = False
        AT_ONLY_ENABLED = True
        logger.info(f"[router] development_scope=at_only user={user_id}")
        await bot.send(
            event,
            "已切换为仅 @ 模式：所有群仅真实 @机器人 时触发 Agent，#指令保持可用。",
        )
    else:
        if agent_at_only_enabled():
            mode_label = "仅 @"
        elif console_runtime.global_value("development_mode", "test" if SCOPE_ENABLED else "all") == "test":
            mode_label = "仅测试群"
        else:
            mode_label = "全部 Agent 群"
        await bot.send(
            event,
            f"开发监听模式：{mode_label}；测试群：{', '.join(map(str, sorted(test_groups)))}\n"
            "用法：#开发监听 on / off / at / status",
        )


if AT_ONLY_ENABLED:
    logger.info(
        "[router] development_scope=at_only；所有群仅真实@机器人触发Agent；"
        "#指令保持可用"
    )
elif ENABLED:
    logger.info(
        f"[router] development_scope=on groups={sorted(ALLOWED_GROUPS)}；"
        f"其他群仅放行#指令；私聊={'允许' if ALLOW_PRIVATE else '禁用'}"
    )
