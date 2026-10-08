"""Pure free-style Gomoku and tic-tac-toe rules, using flat boards."""
import re

SIZES = {"gomoku": 15, "tictactoe": 3}
DIRECTIONS = ((0, 1), (1, 0), (1, 1), (1, -1))


def size(game):
    if game not in SIZES:
        raise ValueError("未知游戏")
    return SIZES[game]


def new_board(game):
    return [0] * (size(game)**2)


def validate_board(game, board):
    n = size(game)
    if len(board) != n*n or any(type(x) is not int or x not in (0, 1, 2) for x in board):
        raise ValueError("棋盘数据无效")
    return n


def parse_move(game, text):
    n = size(game)
    value = str(text).strip().upper()
    if game == "tictactoe" and re.fullmatch(r"[1-9]", value):
        return int(value)-1
    match = re.fullmatch(r"([A-Z])([1-9][0-9]?)", value)
    if match:
        col, row = ord(match[1])-ord('A'), int(match[2])-1
        if 0 <= row < n and 0 <= col < n:
            return row*n+col
    raise ValueError("请输入 A1～O15，例如 H8" if n == 15 else "请输入 1～9 或 A1～C3")


def format_move(game, index):
    n = size(game)
    if type(index) is not int or not 0 <= index < n*n:
        raise ValueError("落子越界")
    return f"{chr(65+index%n)}{index//n+1}"


def play_move(game, board, index, side):
    n = validate_board(game, board)
    if type(side) is not int or side not in (1, 2):
        raise ValueError("棋子颜色无效")
    if type(index) is not int or not 0 <= index < n*n:
        raise ValueError("落子越界")
    if board[index]:
        raise ValueError("这个位置已经有棋子了，请换一个位置")
    result = list(board)
    result[index] = side
    return result


def line_at(board, n, index, target):
    side = board[index]
    if not side:
        return []
    row, col = divmod(index, n)
    for dr, dc in DIRECTIONS:
        before, after = [], []
        for direction, found in ((-1, before), (1, after)):
            r, c = row+dr*direction, col+dc*direction
            while 0 <= r < n and 0 <= c < n and board[r*n+c] == side:
                found.append(r*n+c)
                r, c = r+dr*direction, c+dc*direction
        line = list(reversed(before))+[index]+after
        if len(line) >= target:
            return line
    return []


def outcome(game, board):
    n = validate_board(game, board)
    target = 5 if n == 15 else 3
    for index, side in enumerate(board):
        if side:
            line = line_at(board, n, index, target)
            if line:
                return side, line
    return (-1, []) if all(board) else (0, [])
