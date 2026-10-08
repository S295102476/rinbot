"""Public command contract without loading the bot or connecting to services."""
import importlib.util
from pathlib import Path
import sys

import pytest


_spec = importlib.util.spec_from_file_location(
    "_minigames_commands_test",
    Path(__file__).parents[1] / "plugins/minigames/commands.py",
)
_commands = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _commands
_spec.loader.exec_module(_commands)
parse_command = _commands.parse_command


def test_menu_does_not_start_a_session():
    assert parse_command("#小游戏").action == "menu"
    assert parse_command(" ＃小游戏 " ).action == "menu"


@pytest.mark.parametrize("text,game", [
    ("#五子棋", "gomoku"), ("#井字棋", "tictactoe"),
    ("#小游戏 五子棋", "gomoku"), ("＃小游戏 井字棋", "tictactoe"),
    ("#成语填空", "idiom"), ("#小游戏 成语填空", "idiom"),
    ("#猜数字", "number"), ("#小游戏 猜数字", "number"),
])
def test_game_names_open_read_only_tutorials(text, game):
    command = parse_command(text)
    assert (command.action, command.game) == ("menu", game)


@pytest.mark.parametrize("text,game", [
    ("#五子棋 对战", "gomoku"), ("#井字棋 对战", "tictactoe"),
    ("#小游戏 五子棋 对战", "gomoku"), ("＃小游戏 井字棋 对战", "tictactoe"),
])
def test_explicit_start_is_casual_human_first(text, game):
    command = parse_command(text)
    assert (command.action, command.game, command.mode) == ("start", game, "bot")
    assert command.difficulty == "casual"
    assert command.first is True


def test_difficulty_and_first_side_apply_to_both_games():
    for text in ("#五子棋 简单 后手", "#小游戏 井字棋 简单 后手",
                 "#五子棋 对战 简单 后手", "#井字棋 简单 对战 后手"):
        command = parse_command(text)
        assert command.difficulty == "casual"
        assert command.first is False


def test_open_and_targeted_opponents():
    assert parse_command("#五子棋 双人").mode == "pvp"
    command = parse_command("#井字棋 后手", [22], bot_id=99, user_id=11)
    assert command.mode == "pvp"
    assert command.invitee == 22
    assert not command.first
    assert parse_command("#五子棋", [99], bot_id=99, user_id=11).action == "menu"
    assert parse_command("#五子棋", [22], bot_id=99, user_id=11).action == "start"


@pytest.mark.parametrize("mentions", [[11], [22, 33], ["all"], [-1], ["bad"]])
def test_invalid_invites_are_rejected(mentions):
    with pytest.raises(ValueError):
        parse_command("#五子棋", mentions, bot_id=99, user_id=11)


@pytest.mark.parametrize("text", [
    "#五子棋 简单 普通", "#井字棋 先手 后手", "#五子棋 简单 简单",
    "#五子棋 地狱", "#小游戏 国际象棋", "#棋盘 多余", "#落子",
    "#悔棋 多余", "#结束游戏 多余", "#对战", "#五子棋 对战 对战",
])
def test_unknown_or_conflicting_arguments_fail_closed(text):
    with pytest.raises(ValueError):
        parse_command(text)


@pytest.mark.parametrize("text,action", [
    ("#加入游戏", "join"), ("#棋盘", "view"), ("#悔棋", "undo"),
    ("#同意悔棋", "accept_undo"), ("#拒绝悔棋", "reject_undo"),
    ("#认输", "resign"), ("#结束游戏", "cancel"),
])
def test_session_actions(text, action):
    assert parse_command(text).action == action


def test_moves_keep_the_current_message_argument_only():
    assert parse_command("#落子 H8").argument == "H8"
    assert parse_command("5").argument == "5"
    assert parse_command("h8").argument == "h8"


def test_non_start_commands_reject_an_extra_target():
    with pytest.raises(ValueError):
        parse_command("#棋盘", [22], bot_id=99, user_id=11)


@pytest.mark.parametrize("name", ["五子棋", "井字棋"])
@pytest.mark.parametrize("difficulty,expected", [("娱乐", "casual"), ("认真", "serious"), ("普通", "casual"), ("简单", "casual")])
def test_new_difficulty_and_legacy_aliases(name, difficulty, expected):
    assert parse_command(f"#{name} 对战 {difficulty}").difficulty == expected


def test_pvp_ignores_difficulty():
    command = parse_command("#五子棋 双人 认真 娱乐 后手")
    assert command.mode == "pvp" and command.difficulty == "casual" and not command.first


@pytest.mark.parametrize("mode,expected", [("单人", "solo"), ("抢答", "race")])
def test_idiom_start_modes(mode, expected):
    command = parse_command(f"＃成语填空 {mode}")
    assert (command.action, command.game, command.mode) == ("start", "idiom", expected)
    assert parse_command(f"#小游戏 成语填空 {mode}").mode == expected


def test_idiom_actions_preserve_answer_for_rules_to_normalize():
    assert parse_command("#作答 春暖花开！").argument == "春暖花开！"
    assert parse_command("#作答 春暖花开").action == "answer"
    assert parse_command("#题目").action == "question"
    assert parse_command("#跳过").action == "skip"


@pytest.mark.parametrize("text", ["#成语填空 对战", "#成语填空 单人 抢答", "#作答", "#题目 1", "#跳过 1", "#游戏名", "#五子棋 娱乐 认真"])
def test_idiom_wrong_arguments_and_placeholder_rejected(text):
    with pytest.raises(ValueError):
        parse_command(text)


def test_idiom_never_accepts_an_invitee():
    for text in ("#成语填空", "#成语填空 单人", "#成语填空 抢答", "#作答 春暖花开"):
        with pytest.raises(ValueError):
            parse_command(text, [22], bot_id=99, user_id=11)


@pytest.mark.parametrize("mode,expected", [("单人", "solo"), ("抢答", "race")])
def test_number_start_modes(mode, expected):
    command = parse_command(f"＃猜数字 {mode}")
    assert (command.action, command.game, command.mode) == ("start", "number", expected)
    assert parse_command(f"#小游戏 猜数字 {mode}").mode == expected


@pytest.mark.parametrize("text,action", [("#猜 1234", "guess"), ("#猜1234", "guess"), ("#作答1234", "answer"), ("#作答 1234", "answer")])
def test_number_guess_commands(text, action):
    command = parse_command(text)
    assert command.action == action and command.argument == "1234"


@pytest.mark.parametrize("text", ["#猜数字 对战", "#猜数字 单人 抢答", "#猜", "#猜12", "#作答12345", "#猜abcd"])
def test_number_wrong_arguments_rejected(text):
    with pytest.raises(ValueError):
        parse_command(text)


def test_number_never_accepts_an_invitee():
    for text in ("#猜数字", "#猜数字 单人", "#猜数字 抢答", "#猜1234"):
        with pytest.raises(ValueError):
            parse_command(text, [22], bot_id=99, user_id=11)
