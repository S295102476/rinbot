"""Pure Go tests: coordinates, captures, superko and Chinese area scoring."""
import importlib
from pathlib import Path
import sys
import types

import pytest

package = types.ModuleType("_minigame_go_rule_tests")
package.__path__ = [str(Path(__file__).parents[1] / "plugins/minigames")]
sys.modules[package.__name__] = package
go = importlib.import_module(package.__name__ + ".go_rules")


def board_at(black=(), white=(), size=9):
    board = [0] * (size * size)
    for color, points in ((1, black), (2, white)):
        for point in points:
            board[go.parse_move(point, size)] = color
    return board


@pytest.mark.parametrize("size,last", [(9, "J9"), (13, "N13"), (19, "T19")])
def test_coordinate_roundtrip_skips_i_and_uses_bottom_origin(size, last):
    assert go.parse_move("A1", size) == 0
    assert go.parse_move(last.lower(), size) == size * size - 1
    assert go.parse_move("J1", size) == 8
    for index in range(size * size):
        assert go.parse_move(go.format_move(index, size), size) == index
    assert go.format_move("pass", size) == "pass"


@pytest.mark.parametrize("text,size", [("I1", 19), ("T20", 19), ("K1", 9), ("N14", 13),
                                          ("A0", 9), ("A01", 9), ("A1 hi", 9),
                                          ("pass", 9), ("5", 9)])
def test_invalid_coordinate(text, size):
    with pytest.raises(ValueError):
        go.parse_move(text, size)


@pytest.mark.parametrize("size", [3, 8, 10, 15, 20, True, 9.0, "9", None])
def test_only_explicit_supported_integer_sizes(size):
    with pytest.raises(ValueError):
        go.check_size(size)


def test_neighbors_do_not_wrap_rows_and_group_has_unique_liberties():
    assert set(go.neighbors(0, 9)) == {1, 9}
    assert set(go.neighbors(8, 9)) == {7, 17}
    board = board_at(["A1", "B1", "B2"])
    stones, liberties = go.group(board, 0, 9)
    assert stones == {0, 1, 10}
    assert liberties == {2, 9, 11, 19}
    with pytest.raises(ValueError):
        go.group(board, 3, 9)


def test_capture_multiple_neighbor_groups_is_atomic_and_nonmutating():
    board = board_at(["B4", "C3", "C5", "F4", "E3", "E5"], ["C4", "E4"])
    before = list(board)
    move = go.parse_move("D4", 9)
    changed, captured = go.play(board, move, 1, 9, {bytes(board)})
    assert board == before and changed[move] == 1
    assert captured == sorted([go.parse_move("C4", 9), go.parse_move("E4", 9)])
    assert all(changed[index] == 0 for index in captured)


def ko_board():
    return board_at(["A2", "C2", "B1"], ["B2", "A3", "C3", "B4"])


def test_capture_precedes_suicide_and_immediate_recapture_is_ko():
    board = ko_board()
    changed, captured = go.play(board, go.parse_move("B3", 9), 1, 9, {bytes(board)})
    assert captured == [go.parse_move("B2", 9)]
    assert go.group(changed, go.parse_move("B3", 9), 9)[1] == {go.parse_move("B2", 9)}
    with pytest.raises(ValueError, match="全局同形"):
        go.play(changed, go.parse_move("B2", 9), 2, 9, {bytes(board), bytes(changed)})
    # Removing the old position permits the recapture: rejection comes from
    # persisted superko history rather than a permanently forbidden point.
    restored, removed = go.play(changed, go.parse_move("B2", 9), 2, 9, {bytes(changed)})
    assert restored == board and removed == [go.parse_move("B3", 9)]


def test_positional_superko_checks_all_prior_positions_and_pass_is_exempt():
    board = [0] * 81
    changed = list(board)
    changed[0] = 1
    unrelated = [0] * 81
    unrelated[40] = 2
    with pytest.raises(ValueError, match="全局同形"):
        go.play(board, 0, 1, 9, {bytes(board), bytes(changed), bytes(unrelated)})
    passed, captured = go.play(board, "pass", 1, 9, {bytes(board)})
    assert passed == board and passed is not board and captured == []


def test_suicide_occupied_and_invalid_side_or_point():
    board = board_at(white=["A2", "B1"])
    with pytest.raises(ValueError, match="自杀"):
        go.play(board, 0, 1, 9, {bytes(board)})
    with pytest.raises(ValueError, match="已有棋子"):
        go.play(board, 1, 1, 9, {bytes(board)})
    for point in (-1, 81, True, 1.0, None):
        with pytest.raises(ValueError):
            go.play(board, point, 1, 9, set())
    for side in (0, 3, True, 1.0, "1"):
        with pytest.raises(ValueError):
            go.play(board, 2, side, 9, set())


def test_replay_tracks_turn_captures_and_requires_resume_after_two_passes():
    moves = [{"side": 1, "move": 0}, {"side": 2, "move": 1},
             {"side": 1, "move": "pass"}, {"side": 2, "move": 9}]
    state = go.replay(moves, 9)
    assert state["board"][0] == 0 and state["captures"] == {"1": 0, "2": 1}
    assert state["turn"] == 1 and state["passes"] == 0
    moves += [{"side": 1, "move": "pass"}, {"side": 2, "move": "pass"}]
    before_resume = go.replay(moves, 9)
    assert before_resume["passes"] == 2
    with pytest.raises(ValueError, match="先继续对局"):
        go.replay(moves + [{"side": 1, "move": 2}], 9)
    resumed = go.replay(moves + [{"side": 1, "move": 2}], 9, [6])
    assert resumed["passes"] == 0 and resumed["turn"] == 2
    assert before_resume["positions"] < resumed["positions"]
    with pytest.raises(ValueError, match="恢复落子"):
        go.replay(moves, 9, [3])


def test_replay_rejects_wrong_side_and_duplicate_move():
    with pytest.raises(ValueError, match="轮次"):
        go.replay([{"side": 2, "move": 0}], 9)
    with pytest.raises(ValueError, match="已有棋子"):
        go.replay([{"side": 1, "move": 0}, {"side": 2, "move": 0}], 9)
    with pytest.raises(ValueError):
        go.replay("not history", 9)


@pytest.mark.parametrize("size", [9, 13, 19])
def test_empty_board_counts_neutral_and_white_komi_not_fake_territory(size):
    result = go.score([0] * (size * size), size)
    assert result["black"] == 0 and result["white"] == 7.5
    assert result["neutral"] == list(range(size * size))
    assert result["black_territory"] == result["white_territory"] == []
    assert result["winner"] == 2 and result["margin"] == 7.5


def test_area_is_stones_plus_territory_and_shared_liberties_stay_neutral():
    board = board_at(["A2", "B1", "B3", "C2"], ["H9", "J8"])
    result = go.score(board, 9)
    b2 = go.parse_move("B2", 9)
    assert b2 in result["black_territory"]
    assert go.parse_move("E5", 9) in result["neutral"]
    assert go.parse_move("J9", 9) in result["white_territory"]
    assert result["black"] == board.count(1) + len(result["black_territory"])
    assert result["white"] == board.count(2) + len(result["white_territory"]) + 7.5


def test_dead_marking_requires_whole_group_and_does_not_count_prisoners_twice():
    board = board_at(["A2", "B1", "B3", "C1", "C3", "D2"], ["B2", "C2", "J9"])
    before = list(board)
    b2, c2 = go.parse_move("B2", 9), go.parse_move("C2", 9)
    with pytest.raises(ValueError, match="完整连通"):
        go.score(board, 9, [b2])
    result = go.score(board, 9, [b2, c2])
    assert {b2, c2} <= set(result["black_territory"])
    assert result["black"] == board.count(1) + len(result["black_territory"])
    assert board == before
    with pytest.raises(ValueError, match="棋子"):
        go.score(board, 9, [go.parse_move("E5", 9)])
