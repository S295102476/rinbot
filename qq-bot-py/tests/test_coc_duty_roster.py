import io
from datetime import date

import nonebot
import pytest
from PIL import Image


try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(driver="~fastapi")

from plugins.coc_dice import DiceExpressionError, _eval_dice_expr, _parse_command  # noqa: E402
from plugins.duty_roster import (  # noqa: E402
    _parse_manual_assignment,
    _parse_view_scope,
    _render_roster,
)


def test_compact_coc_commands_are_normalized():
    assert _parse_command(".r7d3") == (".r", "7d3")
    assert _parse_command(".r#7 d3") == (".r", "#7 d3")
    assert _parse_command(".r 7d3+2") == (".r", "7d3+2")
    assert _parse_command(".rd6") == (".rd", "d6")
    assert _parse_command(".rc 侦查 60") == (".rc", "侦查 60")


def test_dice_expression_supports_multiple_dice_and_keep(monkeypatch):
    monkeypatch.setattr("plugins.coc_dice._roll", lambda count, _sides: list(range(1, count + 1)))
    total, detail = _eval_dice_expr("7d3")
    assert total == 28
    assert detail.startswith("[1+2+3+4+5+6+7]")

    total, detail = _eval_dice_expr("4d6kh3")
    assert total == 9
    assert "→4+3+2" in detail


def test_dice_expression_rejects_unsafe_input():
    with pytest.raises(DiceExpressionError):
        _eval_dice_expr("__import__('os').system('whoami')")


def test_roster_date_and_manual_aliases():
    today = date(2026, 9, 4)
    assert _parse_view_scope("本周", today) == date(2026, 8, 31)
    assert _parse_view_scope("下周", today) == date(2026, 9, 7)
    assert _parse_manual_assignment("9-7 艾蕾", today) == (date(2026, 9, 7), "eres")
    assert _parse_manual_assignment("2026-9-8 ishtar", today) == (date(2026, 9, 8), "ishtar")
    with pytest.raises(ValueError):
        _parse_manual_assignment("9-3 rin", today)


def test_roster_render_is_a_seven_day_png():
    payload = _render_roster(
        date(2026, 8, 31),
        {
            date(2026, 8, 31): "rin",
            date(2026, 9, 1): "eres",
            date(2026, 9, 2): "ishtar",
        },
    )
    with Image.open(io.BytesIO(payload)) as image:
        assert image.format == "PNG"
        assert image.size == (1480, 470)
