"""Private admin entry points for the development Agent and approvals."""

from __future__ import annotations

import re

from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, MessageEvent, PrivateMessageEvent
from nonebot.log import logger
from nonebot.rule import Rule

from .agent_policy import AgentPolicy
from .agent_runtime import CONFIG, clear_dev_session, handle_dev_request
from .agent_tools import approve_patch, execute_tool, reject_patch
from .persona_manager import (
    get_active_persona,
    list_personas,
    list_switch_history,
    reload_profiles,
    switch_persona,
)


from runtime_config import feature_enabled
_POLICY = AgentPolicy.from_config(CONFIG)


def _private_agent_rule() -> Rule:
    async def _rule(_bot: Bot, event: MessageEvent) -> bool:
        if not isinstance(event, PrivateMessageEvent):
            return False
        if not _POLICY.user_is_admin(event.user_id):
            return False
        text = event.get_plaintext().strip()
        if not feature_enabled(CONFIG, "development_agent") and not re.match(r"^#(?:agent|\u4ee3\u7406)\s+persona\s+", text, re.I):
            return False
        return text.startswith("#开发") or bool(re.match(r"^#(?:agent|代理)\s+", text, re.IGNORECASE))

    return Rule(_rule)


private_agent = on_message(rule=_private_agent_rule(), priority=1, block=True)


@private_agent.handle()
async def handle_private_agent(bot: Bot, event: PrivateMessageEvent):
    text = event.get_plaintext().strip()
    if text.startswith("#开发"):
        request = text[len("#开发"):].strip()
        if request.lower() in {"清空会话", "重置会话", "reset", "clear"}:
            clear_dev_session(int(event.user_id))
            replies = ["开发会话已清空；项目文件没有被修改。"]
        else:
            replies = await handle_dev_request(int(event.user_id), request)
    else:
        persona_match = re.match(r"^#(?:agent|代理)\s+persona\s+(.+?)\s*$", text, re.IGNORECASE)
        if persona_match:
            args = persona_match.group(1).strip()
            subcommand, _, rest = args.partition(" ")
            subcommand = subcommand.lower()
            if subcommand == "reload":
                active = reload_profiles()
                try:
                    from .ai_chat import MODEL, PRIMARY_PROTOCOL
                    if PRIMARY_PROTOCOL == "gemini_native":
                        from .gemini_native import create_cached_content

                        await create_cached_content(str(MODEL))
                except Exception as exc:
                    logger.warning(f"[persona] reload cache failed: {type(exc).__name__}")
                replies = [f"人设文档已重新加载，当前为{active.name}（{active.persona_id}）"]
            elif subcommand == "list":
                profiles = list_personas()
                history = await list_switch_history(3)
                lines = [
                    f"当前人设：{get_active_persona().name}（{get_active_persona().persona_id}）",
                    "可用人设：" + ("、".join(f"{p.persona_id}={p.name}" for p in profiles) or "无"),
                ]
                if history:
                    lines.append("最近切换：" + "；".join(
                        f"{row['from']}→{row['to']} {row['created_at']}" for row in history
                    ))
                replies = ["\n".join(lines)]
            elif subcommand == "show":
                target = (rest.strip().lower() or get_active_persona().persona_id)
                profile = next((item for item in list_personas() if item.persona_id == target), None)
                replies = [
                    f"人设：{profile.name}（{profile.persona_id}）\n文档长度：{len(profile.content)}字"
                    if profile else f"未找到人设 {target}"
                ]
            elif subcommand in {"switch", "切换"}:
                target, _, note = rest.strip().partition(" ")
                replies = [await switch_persona(target, int(event.user_id), note)] if target else [
                    "用法：#agent persona switch <id> [备注]"
                ]
            else:
                replies = ["用法：#agent persona reload、list、show <id>、switch <id> [备注]"]
        else:
            match = re.match(r"^#(?:agent|代理)\s+approve\s+([A-Za-z0-9_-]+)\s*$", text, re.IGNORECASE)
            if match:
                replies = [await approve_patch(match.group(1), int(event.user_id))]
            elif match := re.match(
                r"^#(?:agent|代理)\s+reject\s+([A-Za-z0-9_-]+)\s*$",
                text,
                re.IGNORECASE,
            ):
                replies = [await reject_patch(match.group(1), int(event.user_id))]
            elif re.match(r"^#(?:agent|代理)\s+status\s*$", text, re.IGNORECASE):
                status = await execute_tool(
                    "git_status",
                    {"command": "status"},
                    "dev",
                    {"user_id": int(event.user_id), "is_admin": True},
                )
                replies = [str(status)]
            else:
                replies = [
                    "用法：#开发 <需求>、#开发 清空会话、#agent status、"
                    "#agent persona reload/list/show/switch、#agent approve <审批编号>、#agent reject <审批编号>。"
                ]
    for reply in replies:
        if reply.strip():
            await bot.send(event, reply)


logger.info("[agent] 私聊开发入口已加载（管理员白名单由 config.agent.dev.admin_users 控制）")
