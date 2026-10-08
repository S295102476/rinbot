"""Deterministic Chinese-style area Go, positional superko, no suicide.

Rows start at the bottom, columns skip I, as in GTP. No engine or I/O.
"""
import re

COLUMNS = "ABCDEFGHJKLMNOPQRST"
SIZES = (9, 13, 19)
KOMI = 7.5


def check_size(size):
    if type(size) is not int or size not in SIZES:
        raise ValueError("围棋仅支持 9路、13路、19路")
    return size


def parse_move(text, size):
    check_size(size)
    value = str(text).strip().upper()
    match = re.fullmatch(r"([A-HJ-T])([1-9]|1[0-9])", value)
    if match and match[1] in COLUMNS[:size] and int(match[2]) <= size:
        return (int(match[2])-1)*size + COLUMNS.index(match[1])
    raise ValueError(f"请输入 A1～{COLUMNS[size-1]}{size}；列跳过 I，行从下往上")


def format_move(index, size):
    check_size(size)
    if index == "pass":
        return "pass"
    if type(index) is not int or not 0 <= index < size*size:
        raise ValueError("围棋坐标越界")
    return f"{COLUMNS[index%size]}{index//size+1}"


def neighbors(index, size):
    row, col = divmod(index, size)
    if col: yield index-1
    if col < size-1: yield index+1
    if row: yield index-size
    if row < size-1: yield index+size


def group(board, index, size):
    if type(index) is not int or not 0 <= index < size*size or board[index] not in (1, 2):
        raise ValueError("请选择棋子所在坐标")
    stones, liberties, todo = {index}, set(), [index]
    while todo:
        for other in neighbors(todo.pop(), size):
            if board[other] == 0:
                liberties.add(other)
            elif board[other] == board[index] and other not in stones:
                stones.add(other)
                todo.append(other)
    return stones, liberties


def play(board, move, side, size, seen):
    check_size(size)
    if (len(board) != size*size or any(type(x) is not int or x not in (0, 1, 2) for x in board)
            or type(side) is not int or side not in (1, 2)):
        raise ValueError("围棋盘面无效")
    if move == "pass":
        return list(board), []
    if type(move) is not int or not 0 <= move < size*size:
        raise ValueError("围棋落子越界")
    if board[move]:
        raise ValueError("这个位置已有棋子")
    changed = list(board)
    changed[move] = side
    captured = set()
    for other in neighbors(move, size):
        if changed[other] == 3-side:
            stones, liberties = group(changed, other, size)
            if not liberties:
                captured.update(stones)
    for index in captured:
        changed[index] = 0
    if not group(changed, move, size)[1]:
        raise ValueError("不能自杀落子，请换一个位置")
    if bytes(changed) in seen:
        raise ValueError("此步会造成全局同形重复（劫），请换一个位置")
    return changed, sorted(captured)


def replay(moves, size, resume_after=()):
    check_size(size)
    if not isinstance(moves, list) or len(moves) > 2048:
        raise ValueError("围棋历史无效或超过安全上限")
    board = [0]*(size*size)
    seen = {bytes(board)}
    captures, passes, turn = {"1": 0, "2": 0}, 0, 1
    resumes = set(resume_after)
    for ply, item in enumerate(moves, 1):
        if item.get("side") != turn:
            raise ValueError("围棋历史轮次错误")
        if passes >= 2:
            raise ValueError("结算中的棋局必须先继续对局")
        board, removed = play(board, item["move"], turn, size, seen)
        seen.add(bytes(board))
        captures[str(turn)] += len(removed)
        passes = passes+1 if item["move"] == "pass" else 0
        turn = 3-turn
        if ply in resumes:
            if passes != 2:
                raise ValueError("围棋恢复落子的历史无效")
            passes = 0
    return {"board": board, "turn": turn, "captures": captures,
            "passes": passes, "positions": seen}


def score(board, size, dead=(), komi=KOMI):
    check_size(size)
    if len(board) != size*size or any(type(x) is not int or x not in (0, 1, 2) for x in board):
        raise ValueError("围棋盘面无效")
    removed = set(dead)
    for point in removed:
        stones, _ = group(board, point, size)
        if not stones <= removed:
            raise ValueError("死子必须标记完整连通棋块")
    counted = [0 if i in removed else stone for i, stone in enumerate(board)]
    visited, regions = set(), {1: [], 2: [], 0: []}
    for point, stone in enumerate(counted):
        if stone or point in visited: continue
        space, border, todo = {point}, set(), [point]
        visited.add(point)
        while todo:
            for other in neighbors(todo.pop(), size):
                if counted[other]:
                    border.add(counted[other])
                elif other not in visited:
                    space.add(other)
                    visited.add(other)
                    todo.append(other)
        owner = next(iter(border)) if len(border) == 1 else 0
        regions[owner].extend(space)
    black = counted.count(1)+len(regions[1])
    white = counted.count(2)+len(regions[2])+komi
    return {"black": black, "white": white, "komi": komi,
            "black_territory": sorted(regions[1]), "white_territory": sorted(regions[2]),
            "neutral": sorted(regions[0]), "winner": 1 if black > white else 2 if white > black else -1,
            "margin": abs(black-white)}
