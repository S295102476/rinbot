"""Small, side-effect-free catalogue shared by routing, parsing and cards."""
from dataclasses import dataclass


@dataclass(frozen=True)
class GameSpec:
    id: str
    name: str
    short: str
    description: str
    kind: str


GAME_REGISTRY = (
    GameSpec("gomoku", "五子棋", "五", "15×15 自由规则 · 人机 / 群友对战", "board"),
    GameSpec("tictactoe", "井字棋", "井", "3×3 九宫格 · 三子连线 · 快速上手", "board"),
    GameSpec("xiangqi", "象棋", "象", "中国象棋 · 中文棋谱 / 坐标 · 红先黑后", "board"),
    GameSpec("go", "围棋", "围", "9 / 13 / 19 路 · 提子围地 · 默认九路", "board"),
    GameSpec("idiom", "成语填空", "文", "单人十题计时 · 全群抢答争先十分", "quiz"),
    GameSpec("number", "猜数字", "AB", "四位不重数字 · 1A2B 推理 · 单人 / 抢答", "quiz"),
)
GAME_BY_ID = {game.id: game for game in GAME_REGISTRY}
GAME_NAMES = {game.id: game.name for game in GAME_REGISTRY}
GAME_COMMANDS = {game.name: game.id for game in GAME_REGISTRY}
SESSION_COMMANDS = {
    "小游戏": "menu", "加入游戏": "join", "落子": "move", "棋盘": "view",
    "悔棋": "undo", "同意悔棋": "accept_undo", "拒绝悔棋": "reject_undo",
    "认输": "resign", "结束游戏": "cancel",
    "作答": "answer", "题目": "question", "跳过": "skip", "猜": "guess",
    "走棋": "move", "停一手": "pass", "求和": "offer_draw",
    "同意和棋": "accept_draw", "拒绝和棋": "reject_draw",
    "标死": "mark_dead", "取消死子": "unmark_dead",
    "确认数目": "confirm_score", "继续对局": "resume",
}
COMMAND_NAMES = tuple(GAME_COMMANDS) + tuple(SESSION_COMMANDS)
