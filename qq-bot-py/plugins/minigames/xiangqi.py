"""Xiangqi rules and notation, backed by the pinned pyffish rule library.

Coordinates use Fairy-Stockfish UCI ranks 1..10, not UCCI ranks 0..9.
The public flat board starts at A1 (red's lower-left corner), with ``.``
for empty squares, uppercase red pieces and lowercase black pieces.
No AI search or network access happens in this module.
"""
from __future__ import annotations

from functools import lru_cache
import importlib
import re
import threading
import unicodedata

START_FEN = "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w - - 0 1"
_VARIANT = "xiangqi"
_VERSION = (0, 0, 90)
_LOCK = threading.RLock()
_library = None
_UCI = re.compile(r"([a-i])(10|[1-9])([a-i])(10|[1-9])\Z")
_COORDINATE = re.compile(r"([A-I])(10|[1-9])\s*(?:-|→|—)?\s*([A-I])(10|[1-9])\Z", re.I)
_NUMBER = {str(i): i for i in range(1, 10)}
_NUMBER.update({char: i for i, char in enumerate("一二三四五六七八九", 1)})
_PIECES = {
    **dict.fromkeys("车車俥", "r"), **dict.fromkeys("马馬傌", "n"),
    **dict.fromkeys("相象", "b"), **dict.fromkeys("仕士", "a"),
    **dict.fromkeys("帅帥将將", "k"), **dict.fromkeys("炮砲", "c"),
    **dict.fromkeys("兵卒", "p"),
}


class XiangqiUnavailable(ValueError):
    """The optional rule dependency is missing or cannot be loaded."""


def ensure_available() -> None:
    """Load the rule library lazily so other games work without pyffish."""
    global _library
    with _LOCK:
        if _library is not None:
            return
        try:
            library = importlib.import_module("pyffish")
            if tuple(library.version()) != _VERSION or _VARIANT not in library.variants():
                raise RuntimeError("unsupported rule library")
        except Exception as exc:
            raise XiangqiUnavailable("象棋规则库不可用，请安装 pyffish==0.0.90；其他小游戏仍可使用") from exc
        _library = library


def _board(fen: str) -> tuple[tuple[str, ...], int]:
    # Validate Python-side before passing stored/user-influenced data to native
    # code. The C extension is not a safe validator for arbitrary move strings.
    if not isinstance(fen, str) or len(fen) > 256:
        raise ValueError("象棋 FEN 数据无效")
    fields = fen.split()
    if (len(fields) != 6 or fields[1] not in ("w", "b")
            or fields[2:4] != ["-", "-"]
            or not re.fullmatch(r"[0-9]{1,5}", fields[4])
            or not re.fullmatch(r"[0-9]{1,5}", fields[5])
            or int(fields[5]) < 1):
        raise ValueError("象棋 FEN 数据无效")
    ranks = fields[0].removesuffix("[]").split("/")
    if len(ranks) != 10:
        raise ValueError("象棋棋盘必须是 9 列、10 行")
    board: list[str] = []
    for rank in reversed(ranks):
        row: list[str] = []
        for piece in rank:
            if piece in "123456789":
                row.extend("." * int(piece))
            elif piece in "rnbakcpRNBAKCP":
                row.append(piece)
            else:
                raise ValueError("象棋棋盘包含无效棋子")
        if len(row) != 9:
            raise ValueError("象棋棋盘必须是 9 列、10 行")
        board.extend(row)
    if board.count("K") != 1 or board.count("k") != 1:
        raise ValueError("象棋棋盘必须各有一枚将帅")
    return tuple(board), 1 if fields[1] == "w" else 2


def _history(moves: list[str], initial_fen: str) -> tuple[str, ...]:
    _board(initial_fen)
    if not isinstance(moves, (list, tuple)) or len(moves) > 4096:
        raise ValueError("象棋着法历史无效或过长")
    if any(not isinstance(move, str) or not _UCI.fullmatch(move) for move in moves):
        raise ValueError("象棋着法历史必须使用 a1 至 i10 的 UCI 坐标")
    return tuple(moves)


def _winner(value: int, turn: int) -> int:
    return -1 if value == 0 else turn if value > 0 else 3 - turn


@lru_cache(maxsize=128)
def _position(initial_fen: str, moves: tuple[str, ...]) -> tuple:
    """Immutable cached replay; every adjudication receives full history."""
    # Called under _LOCK: pyffish has process-global native state.
    ff = _library
    if not moves:
        if ff.validate_fen(initial_fen, _VARIANT) != ff.FEN_OK:
            raise ValueError("象棋初始局面不合法")
        fen = initial_fen
    else:
        previous = _position(initial_fen, moves[:-1])
        if previous[3]:
            raise ValueError("象棋历史包含终局后的着法")
        if moves[-1] not in previous[6]:
            raise ValueError("象棋历史包含非法着法")
        # FEN construction and all end checks deliberately receive the original
        # initial position + full moves. Repetition/check/chase evidence survives.
        fen = ff.get_fen(_VARIANT, initial_fen, list(moves))
    board, turn = _board(fen)
    move_list = list(moves)
    legal = tuple(ff.legal_moves(_VARIANT, initial_fen, move_list))
    check = bool(ff.gives_check(_VARIANT, initial_fen, move_list))
    immediate, result = ff.is_immediate_game_end(_VARIANT, initial_fen, move_list)
    winner, reason = 0, ""
    if immediate:
        winner, reason = _winner(result, turn), "象棋规则终局"
    elif not legal:
        winner = _winner(ff.game_result(_VARIANT, initial_fen, move_list), turn)
        reason = "将死" if check else "困毙"
    else:
        optional, result = ff.is_optional_game_end(_VARIANT, initial_fen, move_list)
        if optional:
            winner, reason = _winner(result, turn), "循环或自然限着（规则库裁定）"
        elif all(ff.has_insufficient_material(_VARIANT, initial_fen, move_list)):
            winner, reason = -1, "双方无获胜子力（规则库裁定）"
    return board, fen, turn, winner, reason, check, legal


def _checked(moves: list[str], initial_fen: str) -> tuple:
    history = _history(moves, initial_fen)
    ensure_available()
    with _LOCK:
        try:
            # Warm consecutive prefixes iteratively, avoiding recursion-depth
            # failures when restoring a long game after a fresh process start.
            for length in range(len(history) + 1):
                _position(initial_fen, history[:length])
            return _position(initial_fen, history)
        except (ValueError, XiangqiUnavailable):
            raise
        except Exception as exc:
            raise ValueError("象棋局面读取失败，请检查保存的着法历史") from exc


def replay(moves: list[str], initial_fen: str = START_FEN) -> dict:
    board, fen, turn, winner, reason, check, _ = _checked(moves, initial_fen)
    return {
        "board": list(board), "fen": fen, "turn": turn,
        "winner": winner, "end_reason": reason, "in_check": check,
    }


def legal_moves(moves: list[str], initial_fen: str = START_FEN) -> list[str]:
    position = _checked(moves, initial_fen)
    return [] if position[3] else list(position[6])


def _squares(uci: str) -> tuple[int, int, int, int]:
    match = _UCI.fullmatch(uci) if isinstance(uci, str) else None
    if not match:
        raise ValueError("请输入象棋坐标，例如 H3-E3")
    a, b, c, d = match.groups()
    return ord(a) - 97, int(b) - 1, ord(c) - 97, int(d) - 1


def format_move(uci: str) -> str:
    col, row, to_col, to_row = _squares(uci)
    return f"{chr(65 + col)}{row + 1}-{chr(65 + to_col)}{to_row + 1}"


def _file(col: int, turn: int) -> int:
    return 9 - col if turn == 1 else col + 1


def _matches_chinese(text: str, move: str, board: tuple[str, ...], turn: int) -> bool:
    col, row, dest_col, dest_row = _squares(move)
    piece = board[row * 9 + col]
    direction = 1 if turn == 1 else -1
    if text[0] in ("前", "后", "中"):
        if _PIECES.get(text[1]) != piece.lower():
            return False
        # Disambiguate only among same-side, same-file pieces. Multiple tandem
        # files may still match; the caller then requires unambiguous coordinates.
        peers = [r for r in range(10) if board[r * 9 + col] == piece]
        peers.sort(reverse=turn == 1)
        if len(peers) < 2:
            return False
        index = {"前": 0, "后": len(peers) - 1, "中": len(peers) // 2}[text[0]]
        if text[0] == "中" and (len(peers) < 3 or len(peers) % 2 != 1):
            return False
        if peers[index] != row:
            return False
    else:
        if _PIECES.get(text[0]) != piece.lower() or _NUMBER.get(text[1]) != _file(col, turn):
            return False
    action, last = text[2], _NUMBER.get(text[3])
    if last is None:
        return False
    if action == "平":
        return row == dest_row and last == _file(dest_col, turn)
    if action not in ("进", "退"):
        return False
    if (dest_row - row) * direction * (1 if action == "进" else -1) <= 0:
        return False
    # Horse/elephant/advisor notation gives the destination file, while other
    # pieces give the number of ranks advanced or retreated.
    target = _file(dest_col, turn) if piece.lower() in "nba" else abs(dest_row - row)
    return target == last


def parse_move(text: str, moves: list[str], initial_fen: str = START_FEN) -> str:
    if not isinstance(text, str) or len(text) > 64:
        raise ValueError("请输入 H3-E3 或 炮二平五 这样的完整着法")
    value = unicodedata.normalize("NFKC", text).strip().replace("進", "进").replace("後", "后")
    position = _checked(moves, initial_fen)
    board, _, turn, winner, _, _, legal = position
    if winner:
        raise ValueError("本局象棋已经结束")
    coordinate = _COORDINATE.fullmatch(value)
    if coordinate:
        move = "".join(coordinate.groups()).lower()
        if move in legal:
            return move
        raise ValueError("这一步不合法，请检查回合、棋子走法或是否仍被将军")
    if len(value) != 4:
        raise ValueError("请输入 H3-E3 或 炮二平五 这样的完整着法")
    matches = [move for move in legal if _matches_chinese(value, move, board, turn)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError("这条中文棋谱有歧义，请改用坐标，例如 H3-E3")
    raise ValueError("这条棋谱没有对应的合法着法，请检查记谱或改用坐标")
