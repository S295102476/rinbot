"""Visual semantics for the two strategy games; no engine or network needed."""
import copy
import importlib.util
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image


spec = importlib.util.spec_from_file_location(
    "phase3_render", Path(__file__).parents[1] / "plugins/minigames/render.py")
render = importlib.util.module_from_spec(spec)
spec.loader.exec_module(render)


def state(game="xiangqi", size=9, persona="rin"):
    value = {"schema": 4, "game": game, "status": "active", "phase": "play",
             "mode": "bot", "difficulty": "serious", "persona_id": persona,
             "players": {"1": {"id": 11, "name": "旅行者"},
                         "2": {"id": 900, "name": render.THEMES[persona]["name"]}},
             "bot_id": 900, "persona_avatar": "missing-avatar.png", "turn": 2,
             "moves": [], "winner": 0}
    if game == "xiangqi":
        # Rows are stored from red's home rank upwards.
        value["board"] = list("RNBAKABNR" + "........." + ".C.....C." + "P.P.P.P.P" +
                              "........." * 2 + "p.p.p.p.p" + ".c.....c." + "........." + "rnbakabnr")
        value["board"][25], value["board"][22] = ".", "C"
        value["moves"] = [{"move": "h3e3", "side": 1, "user_id": 11, "notation": "炮二平五"}]
        value["in_check"] = False
    else:
        value.update(board_size=size, board=[0] * (size * size), komi=7.5, captures={"1": 2, "2": 1})
        for row, column, piece in ((2, 2, 1), (2, 3, 1), (3, 2, 1), (3, 3, 2),
                                   (3, 4, 2), (4, 2, 1), (4, 4, 2), (5, 3, 2),
                                   (size - 3, size - 3, 1), (size - 4, size - 3, 2)):
            value["board"][row * size + column] = piece
        value["moves"] = [{"move": (size - 4) * size + size - 3, "side": 2, "user_id": 900}]
        value["turn"] = 1
    return value


def scoring_state(size=19, persona="eres"):
    value = state("go", size, persona)
    value.update(phase="scoring", mode="pvp")
    value["players"]["2"] = {"id": 22, "name": "另一位棋友"}
    value["scoring"] = {"dead": [3 * size + 3], "confirmed": [11],
                        "suggested": [3 * size + 3, 3 * size + 4], "source": "engine", "revision": 2,
                        "score": {"black": 12, "white": 18.5, "winner": 2, "margin": 6.5,
                                  "black_territory": [size + 2, size + 3],
                                  "white_territory": [5 * size + 4, 5 * size + 5],
                                  "neutral": [4 * size + 3]}}
    return value


def collect_text(monkeypatch):
    captured = []
    for name in ("_text", "_center", "_paragraph"):
        original = getattr(render, name)

        def record(draw, xy, text, *args, _original=original, **kwargs):
            captured.append(str(text))
            return _original(draw, xy, text, *args, **kwargs)

        monkeypatch.setattr(render, name, record)
    return captured


@pytest.mark.parametrize("persona", ["rin", "eres", "ishtar"])
@pytest.mark.parametrize("game,size", [("xiangqi", 9), ("go", 9), ("go", 13), ("go", 19)])
def test_new_boards_are_readonly_and_use_persona(persona, game, size):
    value = state(game, size, persona)
    before = copy.deepcopy(value)
    with Image.open(BytesIO(render.render_board(value))) as image:
        assert image.format == "PNG"
        assert image.width == (1200 if game == "go" and size == 19 else 960)
        assert 1400 < image.height < 2200
        assert image.getpixel((0, 0)) == render.THEMES[persona]["accent"]
    assert value == before


@pytest.mark.parametrize("persona", ["rin", "eres", "ishtar"])
@pytest.mark.parametrize("game", ["xiangqi", "go"])
def test_new_tutorials_are_complete_and_themed(persona, game, monkeypatch):
    texts = collect_text(monkeypatch)
    with Image.open(BytesIO(render.render_menu({"id": persona}, game))) as image:
        assert image.width == 960
        assert 1800 < image.height < 3700
        assert image.getpixel((0, 0)) == render.THEMES[persona]["accent"]
    combined = "\n".join(texts)
    assert "三子连线" not in combined and "1～9，直接发送数字" not in combined
    if game == "xiangqi":
        assert all(label in combined for label in ("炮二平五", "#求和", "#同意和棋", "困毙", "长捉", "A～I"))
    else:
        assert all(label in combined for label in ("19路", "#标死 D4", "#取消死子 D4", "#确认数目", "#继续对局", "#停一手", "不另加分"))


def test_catalogue_exposes_six_games(monkeypatch):
    texts = collect_text(monkeypatch)
    render.render_menu()
    combined = "\n".join(texts)
    assert "6 款" in combined
    assert all(f"#{entry.name}" in combined for entry in render.GAME_REGISTRY)


def test_new_menu_switches_hide_cards_and_explain_disabled_tutorial(monkeypatch):
    texts = collect_text(monkeypatch)
    render.render_menu(xiangqi_enabled=False, go_enabled=False)
    combined = "\n".join(texts)
    assert "4 款" in combined and "#象棋" not in combined and "#围棋" not in combined
    texts.clear()
    render.render_menu(game="go", go_enabled=False)
    assert "本群暂未启用围棋" in "\n".join(texts)


def test_go_coordinates_are_bottom_up_skip_i_and_mark_last_move(monkeypatch):
    texts = collect_text(monkeypatch)
    value = state("go", 19)
    value["board"] = [0] * 361
    value["board"][8] = 1
    value["moves"] = [{"move": 8, "side": 1}]
    with Image.open(BytesIO(render.render_board(value))) as image:
        assert image.getpixel(render._strategy_points("go", 19)[8]) == render.LAST_MOVE
    assert "J" in texts and "T" in texts and "I" not in texts
    assert "黑方上步：J1" in texts
    points = render._strategy_points("go", 19)
    assert points[0][1] > points[342][1]
    assert points[1][0] - points[0][0] >= 50


def test_xiangqi_origin_target_river_and_palace(monkeypatch):
    texts = collect_text(monkeypatch)
    value = state()
    value["in_check"] = True
    with Image.open(BytesIO(render.render_board(value))) as image:
        origin = render._strategy_points("xiangqi")[25]
        target = render._strategy_points("xiangqi")[22]
        assert image.getpixel((origin[0], origin[1] - 35)) == render.LAST_MOVE
        assert image.getpixel((target[0], target[1] - 41)) == render.LAST_MOVE
        # The river interrupts inner vertical lines, while its banks remain.
        assert image.getpixel((210, 875)) == render.BOARD_WOOD
        assert image.getpixel((120, 875)) == render.BOARD_LINE
        assert image.getpixel((435, 1145)) == render.BOARD_LINE
    combined = "\n".join(texts)
    assert "红方上步：H3 → E3 · 炮二平五" in texts
    assert "楚 河" in texts and "汉 界" in texts
    assert "将军！请应将" in combined


def test_scoring_marks_confirmations_and_not_final_result(monkeypatch):
    texts = collect_text(monkeypatch)
    value = scoring_state()
    before = copy.deepcopy(value)
    with Image.open(BytesIO(render.render_board(value))) as image:
        points = render._strategy_points("go", 19)
        assert image.getpixel(points[60]) == render.DEAD_MARK
        assert image.getpixel(points[21]) == (37, 42, 48)
        assert image.getpixel(points[99]) == (255, 253, 243)
    combined = "\n".join(texts)
    assert "当前试算：黑 12 · 白 18.5" in combined
    assert "黑方已确认 · 白方待确认" in combined
    assert "#继续对局" in combined and "#确认数目" in combined
    assert "获胜" not in combined and "最终计分" not in combined
    assert value == before


@pytest.mark.parametrize("status", ["waiting", "ended"])
@pytest.mark.parametrize("game", ["xiangqi", "go"])
def test_waiting_ended_and_long_names(status, game, monkeypatch):
    texts = collect_text(monkeypatch)
    value = state(game)
    value.update(status=status, winner=2)
    value["players"]["1"]["name"] = "长昵称" * 100
    assert render.render_board(value).startswith(b"\x89PNG")
    combined = "\n".join(texts)
    assert ("等待群友加入" if status == "waiting" else "获胜") in combined


def test_both_new_board_avatars_use_circle_crop(tmp_path):
    value = state()
    user = render._png(Image.new("RGB", (80, 80), (250, 0, 0)))
    avatar = tmp_path / "bot.png"
    Image.new("RGB", (80, 80), (0, 250, 0)).save(avatar)
    value["persona_avatar"] = str(avatar)
    with Image.open(BytesIO(render.render_board(value, {"11": user}))) as image:
        assert image.getpixel((84, 171)) == (250, 0, 0)
        assert image.getpixel((536, 171)) == (0, 250, 0)
        assert image.getpixel((50, 137)) == (255, 255, 255)
        assert image.getpixel((502, 137)) == (255, 255, 255)


def test_board_mascot_and_missing_assets_are_safe(monkeypatch, tmp_path):
    requested = []
    original = render._local_image

    def record(source):
        requested.append(str(source))
        return original(source)

    monkeypatch.setattr(render, "_local_image", record)
    monkeypatch.setattr(render, "GAME_ASSET_DIR", tmp_path)
    assert render.render_board(state("go", persona="ishtar")).startswith(b"\x89PNG")
    assert str(tmp_path / "ishtar.png") in requested


def test_pass_pending_scoring_and_final_result(monkeypatch):
    texts = collect_text(monkeypatch)
    value = state("go")
    value["moves"] = [{"move": "pass", "side": 2}]
    render.render_board(value)
    assert "白方上步：停一手" in texts
    value.update(phase="scoring", scoring={"source": "pending", "dead": [], "confirmed": []})
    render.render_board(value)
    assert "正在准备死子建议" in "\n".join(texts)
    value = scoring_state()
    value.update(status="ended", winner=2, end_reason="scored")
    render.render_board(value)
    assert "最终计分：黑 12 · 白 18.5" in "\n".join(texts)


def test_cancelled_scoring_does_not_publish_a_final_result(monkeypatch):
    texts = collect_text(monkeypatch)
    value = scoring_state()
    value.update(status="ended", end_reason="cancelled", winner=0)
    render.render_board(value)
    combined = "\n".join(texts)
    assert "未结算" in combined and "最终计分" not in combined


@pytest.mark.parametrize("game,board,size", [("xiangqi", ["."] * 89, 9),
                                                ("xiangqi", ["?"] * 90, 9),
                                                ("go", [0] * 100, 10),
                                                ("go", [3] * 81, 9)])
def test_invalid_boards_rejected(game, board, size):
    value = state(game)
    value.update(board=board, board_size=size)
    with pytest.raises(ValueError):
        render.render_board(value)


def write_previews(directory):
    """Local inspection helper, invoked explicitly outside pytest."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for persona in render.THEMES:
        for game in ("xiangqi", "go"):
            with Image.open(BytesIO(render.render_menu({"id": persona}, game))) as image:
                image.save(directory / f"{persona}-{game}-tutorial.png")
            value = state(game, 19 if game == "go" else 9, persona)
            with Image.open(BytesIO(render.render_board(value))) as image:
                image.save(directory / f"{persona}-{game}-board.png")
    with Image.open(BytesIO(render.render_board(scoring_state()))) as image:
        image.save(directory / "go-scoring.png")
    with Image.open(BytesIO(render.render_menu())) as image:
        image.save(directory / "menu.png")
