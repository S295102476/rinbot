"""Typed Agent game tools bound to real QQ events and ingress snapshots."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from . import minigame_gate as gate

is_natural_request = gate.is_natural_request


class ToolArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_message_id: int = Field(gt=0)


class StartArguments(ToolArguments):
    game: Literal["gomoku", "tictactoe", "xiangqi", "go", "idiom", "number"]
    mode: Literal["bot", "pvp", "solo", "race"] | None = None
    difficulty: Literal["casual", "serious"] = "casual"
    first: bool = True
    board_size: Literal[9, 13, 19] = 9


class ActionArguments(ToolArguments):
    action: Literal["move", "join", "undo", "accept_undo", "reject_undo", "resign", "cancel",
                    "answer", "guess", "skip", "pass", "mark_dead", "unmark_dead", "confirm_score", "resume",
                    "offer_draw", "accept_draw", "reject_draw"]
    argument: str = Field(default="", max_length=40)


ARGUMENT_MODELS = {"minigame_start": StartArguments, "minigame_action": ActionArguments,
                   "minigame_show": ToolArguments}


def capture_snapshots(bot, events):
    if bot is None or not str(getattr(bot, "self_id", "")).isdigit():
        return {}
    return {int(event.message_id): value for event in events
            if (value := gate.capture_agent(int(bot.self_id), event)) is not None}


def source_event(args, ctx):
    bot, group_id = ctx.get("bot"), int(ctx.get("group_id") or 0)
    if bot is None or group_id <= 0:
        raise PermissionError("游戏工具只能在真实群消息中使用")
    events = ctx.get("batch_events") or ([ctx["event"]] if ctx.get("event") is not None else [])
    matches = [event for event in events if int(getattr(event, "message_id", 0)) == args["source_message_id"]]
    if len(matches) != 1:
        raise PermissionError("游戏请求必须来自当前批次的真实消息")
    event = matches[0]
    if (int(getattr(event, "group_id", 0)) != group_id or int(event.self_id) != int(bot.self_id)
            or int(event.user_id) <= 0 or int(event.user_id) == int(bot.self_id)):
        raise PermissionError("游戏请求身份不匹配")
    if any(segment.type not in {"text", "at", "reply"} for segment in event.message):
        raise ValueError("请单独发送游戏操作，不要附带图片")
    return bot, event


def _snapshot(ctx, event, bot):
    snapshots = ctx.get("game_snapshots") or {}
    value = snapshots.get(int(event.message_id), snapshots.get(str(event.message_id)))
    if (not isinstance(value, dict) or value.get("message_id") != int(event.message_id)
            or value.get("user_id") != int(event.user_id) or value.get("group_id") != int(event.group_id)
            or value.get("bot_id") != int(bot.self_id)):
        raise PermissionError("缺少模型调用前的棋局快照，请重新发送操作")
    return dict(value, strict_version=True)


def _command(name, args, bot, event):
    from .minigames.commands import Command
    intent = gate.natural_intent(event.get_plaintext())
    if not intent:
        raise ValueError("游戏工具需要当前消息明确提出游戏操作")
    if name == "minigame_show":
        if intent["action"] != "view":
            raise ValueError("当前消息未请求查看棋局或题目")
        return Command("view")
    if name == "minigame_action":
        if intent["action"] != args["action"] or str(intent.get("argument", "")).upper() != args.get("argument", "").upper():
            raise ValueError("工具操作与当前群友的游戏请求不一致")
        return Command(args["action"], argument=args.get("argument", ""))
    if intent["action"] != "start" or intent["game"] != args["game"]:
        raise ValueError("当前消息没有请求开始这种游戏")
    targets = []
    for segment in event.message:
        if segment.type != "at" or str(segment.data.get("qq")) == str(bot.self_id):
            continue
        value = str(segment.data.get("qq"))
        if not value.isdigit() or int(value) <= 0:
            raise ValueError("请只 @ 一位真实群友")
        targets.append(int(value))
    if len(targets) > 1 or (targets and targets[0] == int(event.user_id)):
        raise ValueError("请只邀请一位其他群友")
    text = intent["text"]
    game = args["game"]
    quiz = game in {"idiom", "number"}
    mode = ("race" if "抢答" in text else "solo") if quiz else ("pvp" if targets or "双人" in text else "bot")
    difficulty = "serious" if "认真" in text and mode == "bot" else "casual"
    first = "后手" not in text
    size = 19 if "19路" in text else 13 if "13路" in text else 9
    if (args.get("mode") not in {None, mode} or args["difficulty"] != difficulty or args["first"] != first
            or args["board_size"] != size):
        raise ValueError("模式、难度或先后手必须与真实消息一致")
    if quiz and targets:
        raise ValueError("答题游戏请用单人或抢答，不需要邀请对手")
    return Command("start", game, mode=mode, difficulty=difficulty, first=first,
                   invitee=targets[0] if targets else 0, board_size=size)


async def execute_game_tool(name, args, ctx):
    from . import minigames as runtime
    from .minigames.commands import Command

    args = ARGUMENT_MODELS[name].model_validate(args).model_dump()
    bot, event = source_event(args, ctx)
    if not gate.agent_allows(event.group_id):
        raise PermissionError("本群未启用小游戏自然语言工具，可以使用原有游戏指令")
    command = _command(name, args, bot, event)
    arrival = _snapshot(ctx, event, bot)
    key = (int(bot.self_id), int(event.group_id))
    state = runtime.SERVICE.states.get(key)
    if (command.action == "start" and state and state["status"] in {"waiting", "active"}
            and state["expires_at"] > runtime.SERVICE.clock()):
        # Existing games are never cancelled or replaced by a conversational start.
        command = Command("view")
    task_claims = ctx.setdefault("game_tools_used", set())
    claim = int(event.message_id)
    if claim in task_claims:
        return {"status": "duplicate", "terminal": True}
    task_claims.add(claim)
    try:
        return await runtime.apply_game_command(bot, event, command, arrival)
    except Exception:
        # A state may already be durable when delivery fails. The game service's
        # message id dedupe protects retries across separate Agent tasks as well.
        task_claims.discard(claim)
        raise


async def start(args, ctx):
    return await execute_game_tool("minigame_start", args, ctx)


async def action(args, ctx):
    return await execute_game_tool("minigame_action", args, ctx)


async def show(args, ctx):
    return await execute_game_tool("minigame_show", args, ctx)
