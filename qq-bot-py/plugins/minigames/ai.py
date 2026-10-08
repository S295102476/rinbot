"""Budgeted local search. No model, network, process, or thread creation."""
import random
import time
from .rules import validate_board, outcome, line_at, DIRECTIONS


class SearchCancelled(Exception):
    pass


class _Deadline(Exception):
    pass


def choose_move(game, board, side, difficulty="normal", budget_seconds=1.0, cancel_event=None):
    n = validate_board(game, board)
    if type(side) is not int or side not in (1, 2) or difficulty not in {"normal", "easy", "casual", "serious"}:
        raise ValueError("Invalid AI settings")
    # Persisted easy/normal sessions keep their exact legacy policy. New
    # sessions use casual/serious, never silently weaken an external engine.
    if difficulty == "serious" and game == "gomoku":
        raise ValueError("五子棋认真档必须使用 Rapfi 引擎，不能降级为本地娱乐算法")
    if difficulty == "casual":
        difficulty = "normal" if game == "gomoku" else "easy"
    elif difficulty == "serious":
        difficulty = "normal"
    board = list(board)
    end = time.monotonic()+max(0., min(float(budget_seconds), 5.))
    def check():
        if cancel_event is not None and cancel_event.is_set():
            raise SearchCancelled()
        if time.monotonic() >= end:
            raise _Deadline()
    if cancel_event is not None and cancel_event.is_set():
        raise SearchCancelled()
    if outcome(game, board)[0]:
        raise ValueError("Cannot move in a finished game")
    target = 5 if n == 15 else 3
    legal = [i for i, value in enumerate(board) if not value]
    if not legal:
        raise ValueError("No legal move")
    center = n//2
    def distance(i):
        return abs(i//n-center)+abs(i%n-center), i
    legal.sort(key=distance)
    best = legal[0]
    try:
        # Scan all empty points so isolated threats are never omitted by the
        # proximity heuristic. Always win before choosing a defensive block.
        blocks = []
        for who in (side, 3-side):
            for i in legal:
                check()
                board[i] = who
                line = line_at(board, n, i, target)
                board[i] = 0
                if line:
                    if who == side:
                        return i
                    blocks.append(i)
        if blocks:
            return blocks[0]
        if n == 3:
            if difficulty == "easy":
                return random.choice(legal)
            memo = {}
            def minimax(turn, depth):
                check()
                winner, _ = outcome(game, board)
                if winner:
                    return 0 if winner == -1 else (10-depth if winner == side else depth-10)
                key = (tuple(board), turn)
                if key in memo:
                    return memo[key]
                values = []
                for i, piece in enumerate(board):
                    if not piece:
                        board[i] = turn
                        try:
                            value = minimax(3-turn, depth+1)
                        finally:
                            board[i] = 0
                        values.append(value)
                result = max(values) if turn == side else min(values)
                memo[key] = result
                return result
            score = -100
            for i in legal:
                check()
                board[i] = side
                try:
                    value = minimax(3-side, 1)
                finally:
                    board[i] = 0
                if value > score:
                    best, score = i, value
            return best

        def candidates():
            occupied = [i for i, v in enumerate(board) if v]
            if not occupied:
                return [center*n+center]
            cells = set()
            for i in occupied:
                check()
                r, c = divmod(i, n)
                for rr in range(max(0, r-2), min(n, r+3)):
                    for cc in range(max(0, c-2), min(n, c+3)):
                        j = rr*n+cc
                        if not board[j]:
                            cells.add(j)
            return sorted(cells, key=distance)

        options = candidates()
        if difficulty == "easy":
            return random.choice(options)

        def pattern(i, who):
            r, c = divmod(i, n)
            total = 0
            for dr, dc in DIRECTIONS:
                count, opens = 1, 0
                for direction in (-1, 1):
                    rr, cc = r+dr*direction, c+dc*direction
                    while 0 <= rr < n and 0 <= cc < n and board[rr*n+cc] == who:
                        count += 1
                        rr, cc = rr+dr*direction, cc+dc*direction
                    opens += int(0 <= rr < n and 0 <= cc < n and board[rr*n+cc] == 0)
                if count >= 5:
                    total += 10000000
                elif opens:
                    total += {4: 50000 if opens == 2 else 7000,
                              3: 2500 if opens == 2 else 180,
                              2: 100 if opens == 2 else 15, 1: 2}[count]
            return total

        def ranked(turn, width=12):
            scores = []
            for i in candidates():
                check()
                scores.append((pattern(i, turn)+pattern(i, 3-turn)*1.05, i))
            scores.sort(key=lambda pair: (-pair[0], distance(pair[1])))
            return [i for _, i in scores[:width]]

        def evaluate():
            own, other = [], []
            for i in candidates():
                check()
                own.append(pattern(i, side))
                other.append(pattern(i, 3-side))
            own.sort(reverse=True)
            other.sort(reverse=True)
            return sum(own[:3])-sum(other[:3])*1.1

        roots = ranked(side)
        best = roots[0]
        def search(turn, depth, alpha, beta, last):
            check()
            if line_at(board, n, last, 5):
                return (100000000+depth) if board[last] == side else (-100000000-depth)
            if depth == 0:
                return evaluate()
            moves = ranked(turn, 8)
            if not moves:
                return 0
            value = -float("inf") if turn == side else float("inf")
            for i in moves:
                check()
                board[i] = turn
                try:
                    child = search(3-turn, depth-1, alpha, beta, i)
                finally:
                    board[i] = 0
                if turn == side:
                    value, alpha = max(value, child), max(alpha, child)
                else:
                    value, beta = min(value, child), min(beta, child)
                if alpha >= beta:
                    break
            return value
        # Only promote a fully evaluated depth. Deadline never picks an
        # unfinished line over a known safe heuristic fallback.
        for depth in (1, 2, 3):
            depth_best, score = best, -float("inf")
            for i in roots:
                check()
                board[i] = side
                try:
                    value = search(3-side, depth-1, -float("inf"), float("inf"), i)
                finally:
                    board[i] = 0
                if value > score:
                    depth_best, score = i, value
            best = depth_best
        return best
    except _Deadline:
        return best
