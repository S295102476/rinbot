"""Strict parsing of the public game commands, without plugin side effects."""
from dataclasses import dataclass
import re

from plugins.minigame_registry import GAME_COMMANDS, SESSION_COMMANDS


@dataclass(frozen=True)
class Command:
    action: str
    game: str = ""
    argument: str = ""
    mode: str = "bot"
    difficulty: str = "casual"
    first: bool = True
    invitee: int = 0
    board_size: int = 9


NAMES = {**SESSION_COMMANDS, **{name: "start" for name in GAME_COMMANDS}}
GAMES = GAME_COMMANDS


def parse_command(text: str, mentions=(), *, bot_id=0, user_id=0) -> Command:
    value = text.strip().replace("＃", "#", 1)
    if not value.startswith("#"):
        return Command("move", argument=value)
    compact_guess = re.fullmatch(r"#\s*(作答|猜)([0-9]{4})", value)
    if compact_guess:
        value = f"#{compact_guess[1]} {compact_guess[2]}"
    match = re.fullmatch(r"#\s*(\S+)(?:\s+(.*))?", value, re.S)
    if not match or match[1] not in NAMES:
        raise ValueError("请发送 #小游戏 查看玩法")
    name, argument = match[1], (match[2] or "").strip()
    action = NAMES[name]
    targets = []
    for target in mentions:
        if str(target) == "all":
            raise ValueError("不能邀请全体成员，请只 @ 一位群友")
        if not str(target).isdigit() or int(target) <= 0:
            raise ValueError("邀请对象无效")
        if int(target) != int(bot_id):
            targets.append(int(target))
    if action == "menu" and argument:
        tokens = argument.split(maxsplit=1)
        name = tokens[0]
        if name not in GAMES:
            raise ValueError("目前支持：" + " / ".join(f"#小游戏 {game}" for game in GAMES))
        argument = tokens[1] if len(tokens) > 1 else ""
        action = "start"
    if action != "start":
        if targets or (argument and action not in {"move", "answer", "guess", "mark_dead", "unmark_dead"}):
            raise ValueError("这条操作不需要额外参数或 @ 群友")
        if action == "move" and not argument:
            raise ValueError("用法：#落子 H8 / #落子 5 / #走棋 炮二平五 / #走棋 H3-E3")
        if action in {"mark_dead", "unmark_dead"} and not argument:
            raise ValueError("用法：#标死 D4 / #取消死子 D4（围棋结算阶段）")
        if action == "answer" and not argument:
            raise ValueError("用法：#作答 春暖花开，请填写完整四字成语")
        if action == "guess" and not argument:
            raise ValueError("用法：#猜 1234，请填写四位不重复数字，首位不能为 0")
        return Command(action, argument=argument)
    if len(targets) > 1 or (targets and targets[0] == int(user_id)):
        raise ValueError("请只邀请一位其他群友，不能邀请自己")
    tokens = argument.split()
    # Opening a help page is read-only. A real opponent mention or explicit
    # start parameters still retain the previous quick-start behavior.
    if not tokens and not targets:
        return Command("menu", game=GAMES[name])
    if GAMES[name] in {"idiom", "number"}:
        if targets or tokens not in (["单人"], ["抢答"]):
            raise ValueError(f"用法：#{name} 单人 / #{name} 抢答，不需要邀请对手")
        return Command("start", GAMES[name], mode="solo" if tokens == ["单人"] else "race")
    allowed = {"对战", "娱乐", "认真", "简单", "普通", "先手", "后手", "双人"}
    if GAMES[name] == "go":
        allowed |= {"9路", "13路", "19路"}
    if any(token not in allowed for token in tokens):
        raise ValueError(f"发送 #{name} 对战 开局；可加娱乐、认真、后手、双人或 @一位群友" + ("，围棋可加9路/13路/19路" if GAMES[name] == "go" else ""))
    pvp = bool(targets or "双人" in tokens)
    difficulties = set(tokens) & {"娱乐", "认真", "简单", "普通"}
    sizes = set(tokens) & {"9路", "13路", "19路"}
    if len(sizes) > 1:
        raise ValueError("请只选择一种围棋尺寸")
    if len(tokens) != len(set(tokens)) or (not pvp and len(difficulties) > 1) or {"先手", "后手"} <= set(tokens):
        raise ValueError("难度或先后手参数重复、冲突")
    return Command("start", GAMES[name], mode="pvp" if pvp else "bot",
                   difficulty="serious" if "认真" in tokens and not pvp else "casual", first="后手" not in tokens,
                   invitee=targets[0] if targets else 0,
                   board_size=int(next(iter(sizes))[:-1]) if sizes else 9)
