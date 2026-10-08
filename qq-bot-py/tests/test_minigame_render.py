import importlib.util
from io import BytesIO
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import pytest

spec=importlib.util.spec_from_file_location("minigame_render_test",Path(__file__).parents[1]/"plugins/minigames/render.py")
render=importlib.util.module_from_spec(spec)
spec.loader.exec_module(render)


def state(game):
    return {"game":game,"board":[0]*(225 if game=="gomoku" else 9),
        "players":{"1":{"id":11,"name":"玩家"},"2":{"id":900,"name":"机器人"}},
        "bot_id":900,"persona_avatar":"/nonexistent/avatar.jpg","mode":"bot",
        "difficulty":"normal","status":"active","turn":1,"moves":[],"winning_line":[]}


def test_menu_valid_png():
    with Image.open(BytesIO(render.render_menu())) as image:
        assert image.format=="PNG" and image.width==960 and image.height > 690


@pytest.mark.parametrize("game",["gomoku","tictactoe"])
def test_board_valid_png_and_last_move_marker(game):
    value=state(game)
    index=112 if game=="gomoku" else 4
    value["board"][index]=1
    value["moves"]=[{"index":index,"side":1}]
    with Image.open(BytesIO(render.render_board(value))) as image:
        assert image.format=="PNG" and image.size==(960,1100)
        if game=="gomoku":
            assert image.getpixel((480,622)) == (224,84,59)
        else:
            assert image.getpixel((475,597)) == render.ACCENT


@pytest.mark.parametrize("status",["waiting","ended"])
def test_waiting_ended_long_labels_and_missing_avatar(status):
    value=state("tictactoe")
    value["status"]=status
    value["players"]["1"]["name"]="长昵称"*100
    value.update(winner=1,board=[1,1,1,2,2,0,0,0,0],winning_line=[0,1,2])
    assert render.render_board(value).startswith(b"\x89PNG")


@pytest.mark.parametrize("persona", ["rin", "eres", "ishtar"])
@pytest.mark.parametrize("game", ["", "gomoku", "tictactoe", "idiom", "number"])
def test_persona_menus_use_theme_and_decodable_png(persona, game):
    with Image.open(BytesIO(render.render_menu({"id": persona}, game))) as image:
        assert image.width == 960
        assert image.getpixel((0, 0)) == render.THEMES[persona]["accent"]
        assert 900 <= image.height < 3000


def test_active_notice_grows_readonly_menu():
    active = state("gomoku")
    import copy
    before = copy.deepcopy(active)
    for game in ("", "gomoku", "tictactoe"):
        plain = Image.open(BytesIO(render.render_menu(game=game)))
        ongoing = Image.open(BytesIO(render.render_menu(game=game, active_game=active)))
        assert ongoing.height > plain.height
    assert active == before


def test_missing_standee_and_invalid_avatars_are_safe(monkeypatch, tmp_path):
    monkeypatch.setattr(render, "GAME_ASSET_DIR", tmp_path)
    assert render.render_menu({"id": "eres"}, "gomoku").startswith(b"\x89PNG")
    value = state("gomoku")
    assert render.render_board(value, {11: b"not an image"}).startswith(b"\x89PNG")


def test_both_player_avatars_are_circles_with_neutral_corners(tmp_path):
    red = render._png(Image.new("RGB", (100, 100), (250, 0, 0)))
    green_file = tmp_path / "bot.png"
    Image.new("RGB", (100, 100), (0, 250, 0)).save(green_file)
    value = state("gomoku")
    value["persona_avatar"] = str(green_file)
    with Image.open(BytesIO(render.render_board(value, {11: red}))) as image:
        # User at (41,86), bot at (493,86); uniform size 68.
        assert image.getpixel((75, 120)) == (250, 0, 0)
        assert image.getpixel((527, 120)) == (0, 250, 0)
        assert image.getpixel((41, 86)) == (255, 255, 255)
        assert image.getpixel((493, 86)) == (255, 255, 255)
        assert image.getpixel((75, 81)) == render.ACCENT
        assert image.getpixel((527, 81)) == (210, 216, 223)


def test_avatar_can_use_string_qq_and_neutral_placeholder():
    red = render._png(Image.new("RGB", (20, 20), (250, 0, 0)))
    value = state("tictactoe")
    with Image.open(BytesIO(render.render_board(value, {"11": red}))) as image:
        assert image.getpixel((75, 120)) == (250, 0, 0)
    with Image.open(BytesIO(render.render_board(value))) as image:
        assert image.getpixel((75, 110)) == (153, 165, 181)


def test_wrap_retains_long_tutorial_without_clipping():
    text = "这是一段不能被截断的说明，支持文字换行。" * 20
    draw = ImageDraw.Draw(Image.new("RGB", (960, 1)))
    lines = render._wrap(draw, text, 210, 23)
    assert "".join(lines) == text
    assert all(draw.textlength(line, font=render._font(23)) <= 210 for line in lines)
    assert all(not line.startswith(tuple("，。！？；：、")) for line in lines)


def test_tutorial_auto_grows_for_more_text(monkeypatch):
    baseline = Image.open(BytesIO(render.render_menu(game="gomoku"))).height
    original = render._tutorial_rules
    monkeypatch.setattr(render, "_tutorial_rules", lambda game: original(game) + [("补充说明", "测试较长规则不截断。" * 80)])
    expanded = Image.open(BytesIO(render.render_menu(game="gomoku"))).height
    assert expanded > baseline + 500


def test_menu_commands_and_timeout_text(monkeypatch):
    rendered_text = []
    original = render._paragraph

    def record(draw, xy, text, *args, **kwargs):
        rendered_text.append(str(text))
        return original(draw, xy, text, *args, **kwargs)

    monkeypatch.setattr(render, "_paragraph", record)
    render.render_menu(game="gomoku", wait_seconds=90, idle_seconds=180, active_game={"game": "gomoku"})
    combined = "\n".join(rendered_text)
    assert "#五子棋 对战" in combined
    assert "90 秒" in combined and "3 分钟" in combined
    assert "打开教程不会重置棋盘" in combined
    assert "长连也算胜" in combined


@pytest.mark.parametrize("game", ["gomoku", "tictactoe"])
def test_winning_line_pixel_and_reverse_player_order(game):
    value = state(game)
    value.update(status="ended", winner=2, turn=2)
    value["players"] = {"1": {"id": 900, "name": "艾蕾"}, "2": {"id": 11, "name": "玩家"}}
    line = [0, 1, 2, 3, 4] if game == "gomoku" else [0, 1, 2]
    value["winning_line"] = line
    for index in line:
        value["board"][index] = 2
    with Image.open(BytesIO(render.render_board(value))) as image:
        assert image.getpixel((180, 272) if game == "gomoku" else (365, 377)) == (223, 79, 57)


def test_unsupported_game_rejected():
    with pytest.raises(ValueError):
        render.render_menu(game="unknown")
    with pytest.raises(ValueError):
        render.render_board(state("unknown"))
    with pytest.raises(ValueError):
        render.render_board({"game": "idiom", "board": []})


def collect_text(monkeypatch):
    captured = []
    for function in ("_text", "_paragraph", "_center"):
        original = getattr(render, function)
        def record(draw, xy, text, *args, _original=original, **kwargs):
            captured.append(str(text))
            return _original(draw, xy, text, *args, **kwargs)
        monkeypatch.setattr(render, function, record)
    return captured


def test_catalogue_is_dynamic_and_has_no_teaching_suffix(monkeypatch):
    text = collect_text(monkeypatch)
    render.render_menu({"id": "rin", "name": "远坂凛"})
    combined = "\n".join(text)
    assert "教你玩" not in combined
    assert "发送 #游戏名" in combined
    assert "6 款" in combined and "#成语填空" in combined and "#猜数字" in combined
    assert "远坂凛" in text
    text.clear()
    render.render_menu(idiom_enabled=False, number_enabled=False)
    combined = "\n".join(text)
    assert "4 款" in combined and "#成语填空" not in combined
    assert "#猜数字" not in combined


def test_missing_mascot_also_omits_teaching_suffix(monkeypatch, tmp_path):
    text = collect_text(monkeypatch)
    monkeypatch.setattr(render, "GAME_ASSET_DIR", tmp_path)
    render.render_menu({"id": "eres"})
    assert "教你玩" not in "\n".join(text)


def test_idiom_tutorial_is_not_a_board_game(monkeypatch):
    text = collect_text(monkeypatch)
    render.render_menu(game="idiom", active_game={"game": "idiom"})
    combined = "\n".join(text)
    assert "#作答 春暖花开" in combined and "#跳过" in combined
    assert "#题目" in combined
    assert not any(word in combined for word in ("#悔棋", "#落子", "#棋盘", "人机", "先手"))


@pytest.mark.parametrize("difficulty,label", [("casual", "娱乐人机"), ("serious", "认真人机"), ("easy", "旧版简单"), ("normal", "旧版普通")])
def test_difficulty_labels_include_legacy_games(monkeypatch, difficulty, label):
    text = collect_text(monkeypatch)
    value = state("gomoku")
    value["difficulty"] = difficulty
    render.render_board(value)
    assert label in text


def idiom_state(mode="solo"):
    return {"schema": 2, "game": "idiom", "mode": mode, "status": "active",
            "persona_id": "eres", "bot_name": "艾蕾", "host_id": 11,
            "players": {"11": {"id": 11, "name": "玩家"}}, "scores": {"11": 1},
            "question": {"id": "q2", "mask": "一□一意", "answers": ["一心一意"], "word": "一心一意"},
            "question_no": 2, "question_limit": 10 if mode == "solo" else 0,
            "question_started_at": 200, "question_deadline": 260,
            "started_at": 180, "display_now": 201, "deadline": 1080,
            "correct_count": 1, "skipped_count": 0, "timed_out_count": 0,
            "last_result": {"kind": "correct", "question_no": 1, "user_id": 11, "name": "玩家", "answers": ["画龙点睛"]}}


@pytest.mark.parametrize("mode", ["solo", "race"])
@pytest.mark.parametrize("status", ["active", "ended"])
def test_idiom_image_never_reveals_current_answer(monkeypatch, mode, status):
    text = collect_text(monkeypatch)
    value = idiom_state(mode)
    value["status"] = status
    value["finished_at"] = 205 if status == "ended" else None
    with Image.open(BytesIO(render.render_idiom(value))) as image:
        assert image.width == 960 and image.format == "PNG"
        assert image.getpixel((0, 0)) == render.THEMES["eres"]["accent"]
    combined = "\n".join(text)
    assert "一心一意" not in combined
    assert "画龙点睛" in combined
    assert "教你玩" not in combined
    assert "21 秒" in combined if status == "active" else "25 秒" in combined


def test_idiom_waiting_for_successful_send_has_no_timer_claim(monkeypatch):
    text = collect_text(monkeypatch)
    value = idiom_state()
    value.update(question_deadline=None, question_started_at=None, started_at=None)
    render.render_idiom(value)
    combined = "\n".join(text)
    assert "成功展示后开始计时" in combined
    assert "本题截止" not in combined


def test_idiom_long_scoreboard_and_simultaneous_bad_last_result_do_not_leak(monkeypatch):
    text = collect_text(monkeypatch)
    value = idiom_state("race")
    value["players"] = {str(uid): {"name": "很长的玩家昵称" * 10} for uid in range(20)}
    value["scores"] = {str(uid): uid % 10 for uid in range(20)}
    value["last_result"] = {"question_no": 2, "answers": ["一心一意"]}
    with Image.open(BytesIO(render.render_idiom(value))) as image:
        assert image.height > 1600
    combined = "\n".join(text)
    assert "一心一意" not in combined
    assert "前八名" in combined


@pytest.mark.parametrize("font_source", ["system", "fallback"])
def test_idiom_multisolution_result_wraps_every_answer_and_grows(monkeypatch, font_source):
    if font_source == "fallback":
        monkeypatch.setattr(render, "_font", lambda size, bold=False: ImageFont.load_default(size=size))
    value = idiom_state()
    with Image.open(BytesIO(render.render_idiom(value))) as image:
        before = image.height

    # Inspect text that is actually drawn, rather than the unwrapped input
    # passed to _paragraph. Font metrics differ across Windows/Linux/fallback.
    drawn, cards = [], []
    original_text, original_card = ImageDraw.ImageDraw.text, render._card
    def record_text(draw, xy, text, *args, **kwargs):
        drawn.append((str(text), draw.textbbox(xy, text, font=kwargs.get("font"))))
        return original_text(draw, xy, text, *args, **kwargs)
    def record_card(draw, bounds, **kwargs):
        cards.append(bounds)
        return original_card(draw, bounds, **kwargs)
    monkeypatch.setattr(ImageDraw.ImageDraw, "text", record_text)
    monkeypatch.setattr(render, "_card", record_card)

    answers = [chr(0x4e00 + index) * 4 for index in range(50)]
    value["last_result"]["answers"] = answers
    with Image.open(BytesIO(render.render_idiom(value))) as image:
        assert image.height > before
        answer_start = next(i for i, (text, _) in enumerate(drawn) if text.startswith("答案："))
        score_start = next(i for i, (text, _) in enumerate(drawn) if text == "挑战统计")
        answer_lines = drawn[answer_start:score_start]
        assert len(answer_lines) > 1
        assert "".join(text for text, _ in answer_lines) == "答案：" + " / ".join(answers)
        first_box = answer_lines[0][1]
        result_card = next(bounds for bounds in cards
                           if bounds[0] <= first_box[0] < bounds[2]
                           and bounds[1] <= first_box[1] < bounds[3])
        for _, (left, top, right, bottom) in answer_lines:
            assert result_card[0] <= left < right <= result_card[2]
            assert result_card[1] <= top < bottom <= result_card[3]
        assert all(previous[1][3] <= following[1][1]
                   for previous, following in zip(answer_lines, answer_lines[1:]))
        assert result_card[3] < drawn[score_start][1][1]
        assert max(box[3] for _, box in drawn) < image.height


def test_idiom_bot_name_snapshot_precedes_persona_name(monkeypatch):
    text = collect_text(monkeypatch)
    value = idiom_state()
    value["bot_name"], value["persona_name"] = "本局人格", "过时名字"
    render.render_idiom(value)
    assert "本局人格" in text and "过时名字" not in text


@pytest.mark.parametrize("reason,label", [("deadline", "整局时间已到"), ("undelivered", "题目长时间未能展示，本局结束"), ("pool_exhausted", "本局可用题目已用完"), ("bank_unavailable", "题库暂不可用，本局结束")])
def test_idiom_end_reason_labels(monkeypatch, reason, label):
    text = collect_text(monkeypatch)
    value = idiom_state()
    value.update(status="ended", end_reason=reason)
    render.render_idiom(value)
    assert label in text


def test_two_blank_idiom_tutorial_and_old_mask_compatibility(monkeypatch):
    text = collect_text(monkeypatch)
    render.render_menu(game="idiom")
    assert "挖去两个字" in "\n".join(text) and "春□花□" in "\n".join(text)
    value = idiom_state()
    for mask in ("一□一意", "一□一□"):
        value["question"]["mask"] = mask
        assert render.render_idiom(value).startswith(b"\x89PNG")


def test_idiom_tutorial_explains_bare_answers_and_nonadvancing_hint(monkeypatch):
    text = collect_text(monkeypatch)
    render.render_menu(game="idiom")
    combined = "\n".join(text)
    for wording in ("直接发送春暖花开", "答错时静默", "答错会明确提示",
                    "不会换题或扣分", "整局最多 10 分钟", "整局最多 15 分钟",
                    "局外四字聊天不会被捕获", "只有发起者能作答", "群友可直接发送"):
        assert wording in combined
    assert "超时会揭晓答案并进入下一题" not in combined


@pytest.mark.parametrize("mode", ["solo", "race"])
def test_idiom_hint_changes_display_only_without_answer_access(monkeypatch, mode):
    class PublicQuestion(dict):
        def get(self, key, *args):
            assert key not in {"answers", "word"}, "active render must not read answers"
            return super().get(key, *args)
    text = collect_text(monkeypatch)
    value = idiom_state(mode)
    value["question"] = PublicQuestion(value["question"])
    value["question"]["mask"] = "一□一□"
    value.update(hint_mask="一心一□", hinted_at=260, question_deadline=None)
    import copy
    before = copy.deepcopy(value)
    render.render_idiom(value)
    combined = "\n".join(text)
    assert "已揭示一字，继续作答" in combined
    assert "心" in text and "意" not in text
    assert "60 秒" not in combined and "本题截止" not in combined
    assert "整局截止" in combined and "一心一意" not in combined
    assert value == before


def test_idiom_old_single_blank_hint_does_not_reveal_last_character(monkeypatch):
    text = collect_text(monkeypatch)
    value = idiom_state()
    value.update(hint_mask="一□一意", hinted_at=260, question_deadline=None)
    render.render_idiom(value)
    combined = "\n".join(text)
    assert "本题已是单空，继续作答" in combined
    assert "心" not in text and "□" in text
    assert "一心一意" not in combined and "60 秒" not in combined


def test_idiom_before_hint_describes_reveal_time_not_question_expiry(monkeypatch):
    text = collect_text(monkeypatch)
    render.render_idiom(idiom_state())
    combined = "\n".join(text)
    assert "60 秒后揭示一字" in combined and "提示时间" in combined
    assert "本题截止" not in combined and "超时 0" not in combined


def test_idiom_skip_reveals_previous_answer_clearly(monkeypatch):
    text = collect_text(monkeypatch)
    value = idiom_state()
    value["last_result"]["kind"] = "skipped"
    value["skipped_count"] = 1
    render.render_idiom(value)
    combined = "\n".join(text)
    assert "已跳过" in combined and "答案：画龙点睛" in combined
    assert "跳过 1" in combined and "一心一意" not in combined


def number_state(mode="solo"):
    return {"schema": 3, "game": "number", "mode": mode, "status": "active",
            "persona_id": "ishtar", "bot_name": "伊什塔尔", "host_id": 11,
            "players": {"11": {"id": 11, "name": "玩家"}}, "secret": "9876",
            "started_at": 180, "display_now": 201, "deadline": 780 if mode == "solo" else 1080,
            "attempts": [{"user_id": 11, "name": "玩家", "guess": "1234", "a": 0, "b": 0, "number": 1, "at": 200}],
            "attempt_counts": {"11": 1}, "total_attempts": 1, "winner_id": None}


@pytest.mark.parametrize("mode", ["solo", "race"])
def test_number_active_never_accesses_secret(monkeypatch, mode):
    class Guarded(dict):
        def get(self, key, *args):
            assert key != "secret", "active render must not read secret"
            return super().get(key, *args)
    text = collect_text(monkeypatch)
    with Image.open(BytesIO(render.render_number(Guarded(number_state(mode))))) as image:
        assert image.width == 960 and image.format == "PNG"
    combined = "\n".join(text)
    assert "9876" not in combined and "1234" in combined and "0A0B" in combined
    assert "21 秒" in combined and "60 秒" not in combined


def test_number_ended_reveals_secret_and_winner(monkeypatch):
    text = collect_text(monkeypatch)
    value = number_state("race")
    value.update(status="ended", winner_id=11, finished_at=205, end_reason="win")
    render.render_number(value)
    assert all(character in text for character in "9876")
    combined = "\n".join(text)
    assert "玩家 猜中了" in combined and "25 秒" in combined


def test_number_tutorial_is_not_idiom_or_board(monkeypatch):
    text = collect_text(monkeypatch)
    render.render_menu(game="number", active_game={"game": "number"})
    combined = "\n".join(text)
    assert "#猜 1234" in combined and "1A2B" in combined
    assert "10 分钟" in combined and "15 分钟" in combined
    assert not any(word in combined for word in ("#跳过", "#棋盘", "#悔棋", "60 秒", "十分获胜", "先手"))


def test_number_menu_independent_switch(monkeypatch):
    text = collect_text(monkeypatch)
    render.render_menu(number_enabled=False)
    combined = "\n".join(text)
    assert "5 款" in combined and "#猜数字" not in combined and "#成语填空" in combined
    text.clear()
    render.render_menu(idiom_enabled=False)
    combined = "\n".join(text)
    assert "5 款" in combined and "#猜数字" in combined and "#成语填空" not in combined


def test_number_history_is_bounded_and_latest_first(monkeypatch):
    text = collect_text(monkeypatch)
    value = number_state("race")
    value["attempts"] = [{"name": "长昵称" * 30, "guess": str(1000 + index), "a": 1, "b": 2, "number": index + 1} for index in range(20)]
    value["total_attempts"] = 20
    with Image.open(BytesIO(render.render_number(value))) as image:
        assert 1700 < image.height < 2200
    assert "1000" not in text and "1008" in text and "1019" in text
    assert text.index("1019") < text.index("1008")


def test_number_pending_has_whole_game_timer_only(monkeypatch):
    text = collect_text(monkeypatch)
    value = number_state()
    value.update(started_at=None, deadline=None, attempts=[])
    render.render_number(value)
    combined = "\n".join(text)
    assert "成功展示后开始计时" in combined and "本局截止" not in combined
    assert "还没有猜测记录" in combined
