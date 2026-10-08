"""Real pyffish rules, notation, and optional-dependency isolation tests."""
import importlib
import sys
import types
from pathlib import Path

import pytest

package = types.ModuleType("_minigame_xiangqi_tests")
package.__path__ = [str(Path(__file__).parents[1] / "plugins/minigames")]
sys.modules[package.__name__] = package
xq = importlib.import_module(package.__name__ + ".xiangqi")


@pytest.fixture
def native():
    pytest.importorskip("pyffish")
    xq.ensure_available()
    return xq


def fen(pieces, side="w", halfmoves=0):
    board = [["."] * 9 for _ in range(10)]
    for square, piece in pieces.items():
        board[int(square[1:]) - 1][ord(square[0].lower()) - 97] = piece
    rows = []
    for row in reversed(board):
        encoded, empty = "", 0
        for piece in row:
            if piece == ".":
                empty += 1
            else:
                encoded += (str(empty) if empty else "") + piece
                empty = 0
        rows.append(encoded + (str(empty) if empty else ""))
    return "/".join(rows) + f" {side} - - {halfmoves} 1"


def test_missing_dependency_is_lazy_and_distinct_from_bad_state(monkeypatch):
    monkeypatch.setattr(xq, "_library", None)
    def unavailable(name):
        raise ImportError(name)
    monkeypatch.setattr(xq.importlib, "import_module", unavailable)
    # Formatting does not import the optional C extension.
    assert xq.format_move("h3e3") == "H3-E3"
    with pytest.raises(xq.XiangqiUnavailable, match="pyffish==0.0.90"):
        xq.ensure_available()
    with pytest.raises(ValueError) as error:
        xq.replay(["not a move"])
    assert not isinstance(error.value, xq.XiangqiUnavailable)


@pytest.mark.parametrize("library", [
    types.SimpleNamespace(version=lambda: (0, 0, 89), variants=lambda: ["xiangqi"]),
    types.SimpleNamespace(version=lambda: (0, 0, 90), variants=lambda: ["chess"]),
])
def test_wrong_rule_version_or_variant_is_not_silently_accepted(monkeypatch, library):
    monkeypatch.setattr(xq, "_library", None)
    monkeypatch.setattr(xq.importlib, "import_module", lambda name: library)
    with pytest.raises(xq.XiangqiUnavailable):
        xq.ensure_available()


def test_start_board_and_replay_are_bottom_up_and_defensive_copies(native):
    state = native.replay([])
    assert len(state["board"]) == 90
    assert state["board"][:9] == list("RNBAKABNR")
    assert state["board"][-9:] == list("rnbakabnr")
    assert state["board"][9:18] == ["."] * 9
    assert (state["turn"], state["winner"], state["in_check"]) == (1, 0, False)
    state["board"][0] = "."
    assert native.replay([])["board"][0] == "R"
    moves = ["h3e3", "h10g8"]
    result = native.replay(moves)
    assert result["turn"] == 1 and result["winner"] == 0
    assert result["board"][2 * 9 + 4] == "C"
    assert result["board"][7 * 9 + 6] == "n"
    assert moves == ["h3e3", "h10g8"]


@pytest.mark.parametrize("text", ["H3-E3", "h3e3", "H3 → E3", "Ｈ３－Ｅ３", " 炮二平五 ", "砲２平５"])
def test_coordinates_and_chinese_opening_are_equivalent(native, text):
    assert native.parse_move(text, []) == "h3e3"


@pytest.mark.parametrize("text, expected", [
    ("马二进三", "h1g3"), ("馬二進三", "h1g3"),
    ("相七进五", "c1e3"), ("象七进五", "c1e3"),
    ("仕六进五", "d1e2"), ("士六进五", "d1e2"),
    ("车九进二", "a1a3"), ("俥九進二", "a1a3"),
    ("兵九进一", "a4a5"), ("卒九进一", "a4a5"),
])
def test_chinese_piece_aliases_and_destination_meaning(native, text, expected):
    assert native.parse_move(text, []) == expected


@pytest.mark.parametrize("text, expected", [
    ("马８进７", "h10g8"), ("馬八進七", "h10g8"),
    ("车九平八", "i10h10"), ("炮八平五", "h8e8"),
    ("卒一进一", "a7a6"),
])
def test_black_notation_uses_black_view_not_red_view(native, text, expected):
    history = ["h3e3"]
    if expected == "i10h10":
        history += ["h10g8", "h1g3"]
    assert native.parse_move(text, history) == expected


def test_front_back_and_middle(native):
    position = fen({"E1": "K", "F10": "k", "A3": "R", "A5": "R"})
    assert native.parse_move("前车平八", [], position) == "a5b5"
    assert native.parse_move("后車平八", [], position) == "a3b3"
    with pytest.raises(ValueError, match="歧义"):
        native.parse_move("车九平八", [], position)
    position = fen({"E1": "K", "F10": "k", "A5": "P", "A6": "P", "A7": "P"})
    assert native.parse_move("中兵平八", [], position) == "a6b6"
    assert native.parse_move("前兵平八", [], position) == "a7b7"
    black = fen({"E1": "K", "F10": "k", "A7": "r", "A9": "r"}, "b")
    assert native.parse_move("前车平二", [], black) == "a7b7"
    assert native.parse_move("後車平二", [], black) == "a9b9"


def test_multiple_tandem_files_do_not_guess_front_piece(native):
    position = fen({"E1": "K", "F10": "k",
                    "A6": "P", "A7": "P", "C6": "P", "C7": "P"})
    with pytest.raises(ValueError, match="歧义"):
        native.parse_move("前兵进一", [], position)
    assert native.parse_move("A7-A8", [], position) == "a7a8"


@pytest.mark.parametrize("text", ["我走炮二平五", "炮 二平五", "H0-H1", "A01-A2",
                                      "J1-I1", "A11-A10", "H3-E3 please",
                                      "兵九退一", "帅五进九", "炮二平十", "炮二上五",
                                      "中车平八", "前马进三", "", 3, None])
def test_invalid_or_illegal_notation_is_rejected(native, text):
    with pytest.raises(ValueError):
        native.parse_move(text, [])


def test_horse_leg_elephant_eye_river_and_cannon_screen(native):
    legal = native.legal_moves([])
    assert "h1g3" in legal and "h1f2" not in legal
    assert "c1e3" in legal
    position = fen({"E1": "K", "F10": "k", "C1": "B", "D2": "P", "A4": "P"})
    assert "c1e3" not in native.legal_moves([], position)
    position = fen({"E1": "K", "F10": "k", "C5": "B", "A4": "P"})
    assert "c5e7" not in native.legal_moves([], position)
    assert "c5a3" in native.legal_moves([], position)
    assert "h3h10" in legal
    assert "h3h8" not in legal and "h3h9" not in legal
    position = fen({"E1": "K", "F10": "k", "H3": "C",
                    "H6": "p", "H8": "p", "H10": "r"})
    assert "h3h10" not in native.legal_moves([], position)


def test_capture_is_saved_and_own_piece_cannot_be_captured(native):
    result = native.replay(["h3h10"])
    assert result["board"][9 * 9 + 7] == "C"
    assert result["board"][2 * 9 + 7] == "."
    assert sum(piece != "." for piece in result["board"]) == 31
    assert "h3b3" not in native.legal_moves([])
    assert "i10h10" in native.legal_moves(["h3h10"])


def test_soldier_palace_facing_kings_and_leaving_check(native):
    legal = native.legal_moves([])
    assert "a4a5" in legal and "a4b4" not in legal and "a4a3" not in legal
    position = fen({"E1": "K", "F10": "k", "A6": "P"})
    assert "a6b6" in native.legal_moves([], position)
    assert "e1d1" in native.legal_moves([], position)
    assert "e1d2" not in native.legal_moves([], position)
    position = fen({"D1": "K", "F10": "k", "A6": "P"})
    assert "d1c1" not in native.legal_moves([], position)
    position = fen({"E1": "K", "E10": "k", "E5": "R"})
    assert "e5f5" not in native.legal_moves([], position)
    assert "e5e6" in native.legal_moves([], position)
    position = fen({"E1": "K", "D10": "k", "E5": "r", "A5": "R"})
    assert native.replay([], position)["in_check"]
    assert "a5a6" not in native.legal_moves([], position)
    assert "a5e5" in native.legal_moves([], position)


@pytest.mark.parametrize("position, reason", [
    ("3k5/4R4/3R5/9/9/9/9/9/9/4K4 b - - 0 1", "将死"),
    ("4k4/3R1R3/9/9/9/9/9/9/9/3K5 b - - 0 1", "困毙"),
])
def test_checkmate_and_stalemate_are_both_losses(native, position, reason):
    result = native.replay([], position)
    assert result["winner"] == 1 and result["end_reason"] == reason
    assert native.legal_moves([], position) == []
    with pytest.raises(ValueError, match="结束"):
        native.parse_move("E10-E9", [], position)


def test_rule_library_chase_result_uses_full_history_not_plain_threefold(native):
    # Regression position from Fairy-Stockfish's upstream pyffish rule tests.
    # https://github.com/fairy-stockfish/Fairy-Stockfish/blob/master/test.py
    start = "2bakabnr/9/r1n1c4/2p1p1p1p/PP7/9/4P1P1P/2C3NC1/9/1NBAKAB1R w - - 0 1"
    history = "c3a3 a8b8 a3b3 b8a8 b3a3 a8b8 a3b3 b8a8 b3a3".split()
    result = native.replay(history, start)
    assert result["turn"] == 2 and result["winner"] == 2
    assert "规则库" in result["end_reason"]
    # A FEN alone cannot reproduce perpetual-chase adjudication.
    assert native.replay([], result["fen"])["winner"] == 0
    assert native.legal_moves(history, start) == []
    with pytest.raises(ValueError, match="终局后"):
        native.replay(history + ["a8b8"], start)


def test_perpetual_check_sign_and_neutral_repetition(native):
    start = "5k3/9/9/5C3/5c3/5C3/9/9/5p3/4K4 w - - 0 1"
    result = native.replay(2 * ["f5d5", "f6d6", "d5f5", "d6f6"], start)
    assert result["turn"] == 1 and result["winner"] == 2
    history = 2 * ["h1g3", "h10g8", "g3h1", "g8h10"]
    result = native.replay(history)
    assert result["winner"] == -1


def test_natural_move_limit_is_not_lost_on_restore(native):
    start = native.START_FEN.replace("0 1", "120 61")
    assert native.replay([], start)["winner"] == -1


@pytest.mark.parametrize("moves", [["a1a10"], ["h3e3", "h3e3"], ["h0e0"],
                                       ["a1a2;exit"], [None], [1], "h3e3", None])
def test_bad_history_rejected_before_native_execution(native, moves):
    with pytest.raises(ValueError):
        native.replay(moves)


@pytest.mark.parametrize("initial", ["", "not fen", None, "9/9/9/9/9/9/9/9/9/9 w - - 0 1",
                                         xq.START_FEN.replace(" w ", " q "),
                                         xq.START_FEN.replace("0 1", "0 -1"),
                                         xq.START_FEN.replace("1C5C1", "1Q5C1")])
def test_bad_fen_is_rejected(native, initial):
    with pytest.raises(ValueError):
        native.replay([], initial)


@pytest.mark.parametrize("move, expected", [("a1i10", "A1-I10"), ("i10a1", "I10-A1"), ("h3e3", "H3-E3")])
def test_format_coordinate_does_not_shift_to_ucci_ranks(move, expected):
    assert xq.format_move(move) == expected


@pytest.mark.parametrize("move", ["a0a1", "a1a11", "j1i1", "a1a2x", True, None])
def test_format_rejects_outside_board(move):
    with pytest.raises(ValueError):
        xq.format_move(move)
