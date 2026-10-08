import importlib
import sys
import types
from pathlib import Path
import threading
import time

import pytest

package = types.ModuleType("_minigame_rules_tests")
package.__path__ = [str(Path(__file__).parents[1] / "plugins/minigames")]
sys.modules[package.__name__] = package
rules = importlib.import_module(package.__name__+".rules")
ai = importlib.import_module(package.__name__+".ai")


@pytest.mark.parametrize("start,stride", [(0,1),(0,15),(0,16),(14,14)])
def test_gomoku_wins_all_axes_and_long_connections(start,stride):
    board = rules.new_board("gomoku")
    for i in range(6):
        board[start+i*stride] = 1
    winner,line = rules.outcome("gomoku",board)
    assert winner == 1 and len(line) == 6


def test_no_row_wrapping_and_validation():
    board = rules.new_board("gomoku")
    for i in range(13,18):
        board[i] = 2
    assert rules.outcome("gomoku",board)[0] == 0
    for move in ["A0","P1","A16","H8 text","8","H08"]:
        with pytest.raises(ValueError):
            rules.parse_move("gomoku",move)
    assert rules.parse_move("gomoku","h8") == 112
    assert rules.format_move("gomoku",224) == "O15"
    assert rules.parse_move("tictactoe","C3") == rules.parse_move("tictactoe","9") == 8
    with pytest.raises(ValueError):
        rules.play_move("gomoku",board,True,1)
    with pytest.raises(ValueError):
        rules.play_move("gomoku",board,14,1)
    original = list(board)
    changed = rules.play_move("gomoku",board,0,1)
    assert changed[0] == 1 and board == original


def test_tic_draw_and_win():
    assert rules.outcome("tictactoe",[1,2,1,1,2,2,2,1,1])[0] == -1
    assert rules.outcome("tictactoe",[1,2,0,2,1,0,0,0,1]) == (1,[0,4,8])


@pytest.mark.parametrize("game,difficulty", [(g,d) for g in ("gomoku","tictactoe") for d in ("easy","normal")])
def test_ai_wins_and_blocks_without_mutating(game,difficulty):
    board = rules.new_board(game)
    count = 4 if game == "gomoku" else 2
    for i in range(count):
        board[i] = 1
    original=list(board)
    assert ai.choose_move(game,board,1,difficulty) == count
    assert ai.choose_move(game,board,2,difficulty) == count
    assert board == original


def test_normal_tic_never_loses_against_any_human_moves():
    visited=set()
    def visit(board,turn,bot):
        key=(tuple(board),turn,bot)
        if key in visited:
            return
        visited.add(key)
        winner,_=rules.outcome("tictactoe",board)
        if winner:
            assert winner in (-1,bot)
            return
        moves=[ai.choose_move("tictactoe",board,bot)] if turn==bot else [i for i,v in enumerate(board) if not v]
        for move in moves:
            visit(rules.play_move("tictactoe",board,move,turn),3-turn,bot)
    visit([0]*9,1,1)
    visit([0]*9,1,2)


def test_gomoku_deadline_and_cancel():
    board=rules.new_board("gomoku")
    for index,side in [(112,1),(113,2),(127,1),(97,2),(98,1),(128,2)]:
        board[index]=side
    started=time.monotonic()
    choice=ai.choose_move("gomoku",board,1,budget_seconds=.03)
    assert not board[choice] and time.monotonic()-started < .5
    assert not board[ai.choose_move("gomoku",board,1,budget_seconds=0)]
    cancelled=threading.Event()
    cancelled.set()
    with pytest.raises(ai.SearchCancelled):
        ai.choose_move("gomoku",board,1,cancel_event=cancelled)


def test_finished_game_refuses_ai_move():
    with pytest.raises(ValueError):
        ai.choose_move("tictactoe",[1,1,1,2,2,0,0,0,0],2)


def test_new_difficulty_aliases_preserve_legacy_and_require_engine(monkeypatch):
    calls = []
    monkeypatch.setattr(ai.random, "choice", lambda moves: calls.append(list(moves)) or moves[-1])
    assert ai.choose_move("tictactoe", [0]*9, 1, "casual") == 8
    assert calls
    calls.clear()
    assert ai.choose_move("tictactoe", [0]*9, 1, "serious") == ai.choose_move("tictactoe", [0]*9, 1, "normal")
    assert not calls
    assert ai.choose_move("gomoku", [0]*225, 1, "casual", .03) == 112
    assert not calls
    with pytest.raises(ValueError, match="Rapfi"):
        ai.choose_move("gomoku", [0]*225, 1, "serious")
    # Historical easy Gomoku still uses its former random local policy.
    ai.choose_move("gomoku", [0]*225, 1, "easy")
    assert calls


@pytest.mark.parametrize("difficulty", ["casual", "serious"])
def test_new_tic_difficulties_take_wins_before_blocks(difficulty):
    assert ai.choose_move("tictactoe", [1,1,0,2,2,0,0,0,0], 1, difficulty) == 2
    assert ai.choose_move("tictactoe", [1,1,0,0,2,0,0,0,0], 2, difficulty) == 2
