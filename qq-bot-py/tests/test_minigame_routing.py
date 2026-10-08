from copy import deepcopy
from types import SimpleNamespace
import time

import pytest
from plugins import minigame_gate as gate


def event(text, *, uid=11, gid=100, mid=1, segments=None):
    return SimpleNamespace(group_id=gid, user_id=uid, message_id=mid,
        message=segments or [SimpleNamespace(type="text", data={"text": text})],
        get_plaintext=lambda: text, to_me=False)


def state(game="gomoku", status="active", version=3):
    return {"session_id": "session-one", "version": version, "status": status,
            "game": game, "players": {"1": {"id": 11}, "2": {"id": 22}},
            "expires_at": time.time()+600}


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    monkeypatch.setattr(gate, "_states", {})
    from collections import OrderedDict
    monkeypatch.setattr(gate, "_arrivals", OrderedDict())
    monkeypatch.setattr(gate, "_answer_arrivals", OrderedDict())
    monkeypatch.setattr(gate, "_enabled", True)
    monkeypatch.setattr(gate, "_allowed", frozenset())
    monkeypatch.setattr(gate, "_blocked", frozenset({999}))
    monkeypatch.setattr(gate, "_disabled_games", frozenset())


def test_exact_command_boundary():
    for text in ("#小游戏", "＃落子 H8", "#五子棋 娱乐", "# 同意悔棋", "#认输", "#成语填空 抢答", "#作答 春暖花开", "#题目", "#跳过", "#猜数字", "#猜数字 抢答", "＃猜 1234", "#猜1234", "#作答1234"):
        assert gate.is_command(text)
    for text in ("#小游戏foo", "先#棋盘", "5", "#签到", "bmpt", "#游戏名", "#随便游戏", "春暖花开", "1234", "#猜数字foo", "#猜12345", "#猜1234abc"):
        assert not gate.is_command(text)


def test_only_participants_in_active_games_get_shorthand():
    assert gate.capture(900, event("H8")) is None
    gate.publish(900, 100, state())
    assert gate.capture(900, event("H8", mid=2))
    assert gate.capture(900, event("B7", mid=3))
    assert gate.capture(900, event("H8", uid=33, mid=4)) is None
    assert gate.capture(900, event("看看H8", mid=5)) is None
    assert gate.capture(900, event("5", mid=6)) is None
    assert gate.capture(901, event("H8", mid=7)) is None
    gate.publish(900, 100, state(status="waiting"))
    assert gate.capture(900, event("H8", mid=8)) is None


def test_tic_numbers_media_and_mentions():
    gate.publish(900, 100, state(game="tictactoe"))
    assert gate.capture(900, event("5"))
    assert gate.capture(900, event("c3", mid=2))
    assert gate.capture(900, event("10", mid=3)) is None
    assert gate.capture(900, event("5", mid=4, segments=[SimpleNamespace(type="image", data={})])) is None
    assert gate.capture(900, event("5", mid=5, segments=[SimpleNamespace(type="at", data={"qq": 22})])) is None
    assert gate.capture(900, event("5", mid=6, segments=[SimpleNamespace(type="at", data={"qq": 900}),
        SimpleNamespace(type="reply", data={"id": 123})]))


def test_arrival_snapshot_survives_concurrent_move_or_new_game():
    gate.publish(900, 100, state())
    message = event("H8")
    first = gate.capture(900, message)
    gate.publish(900, 100, state(version=4))
    assert gate.capture(900, message) == first
    assert first["version"] == 3
    ordinary = event("5", mid=2)
    assert gate.capture(900, ordinary) is None
    gate.publish(900, 100, state(game="tictactoe", version=5))
    assert gate.capture(900, ordinary) is None
    gate.publish(900, 100, state(status="ended", version=6))
    assert gate.capture(900, event("#井字棋", mid=3))["version"] == 6


def test_group_policy_and_expired_session():
    gate.publish(900, 999, state())
    assert not gate.allows(999)
    assert gate.capture(900, event("H8", gid=999)) is None
    assert gate.capture(900, event("#五子棋", gid=999, mid=2))  # handler denies
    expired = state()
    expired["expires_at"] = time.time()-1
    gate.publish(900, 100, expired)
    assert gate.capture(900, event("H8", mid=3)) is None
    gate.configure(enabled=True, allowed_groups={101}, blocked_groups={999})
    assert not gate.allows(100) and gate.allows(101)


def test_agent_at_scope_and_legacy_quota_do_not_block_games(monkeypatch):
    import asyncio
    import nonebot
    try:
        nonebot.get_driver()
    except ValueError:
        nonebot.init(driver="~fastapi", log_level="ERROR")
    from plugins import dev_scope, quota
    from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message
    message = GroupMessageEvent(time=int(time.time()), self_id=900, post_type="message",
        sub_type="normal", user_id=11, group_id=100, message_type="group", message_id=44,
        message=Message("B7"), raw_message="B7", font=0, sender={"user_id":11,"nickname":"player"})
    gate.publish(900, 100, state())
    monkeypatch.setattr(dev_scope.console_runtime, "observe_incoming", lambda *a: None)
    monkeypatch.setattr(dev_scope.console_runtime, "ready", lambda: False)
    monkeypatch.setattr(dev_scope, "AT_ONLY_ENABLED", True)
    monkeypatch.setattr(quota, "_is_valid_call", lambda *a: (_ for _ in ()).throw(AssertionError("must bypass legacy quota")))
    async def scenario():
        bot = SimpleNamespace(self_id="900")
        await dev_scope._development_scope_gate(bot, message)
        await quota._quota_check(bot, message)
        message.message_id = 45
        message.message = Message("＃落子 B7")
        await quota._quota_check(bot, message)
    asyncio.run(scenario())


def idiom_state(mode="race"):
    return dict(state(game="idiom"), mode=mode, host_id=11, deadline=time.time()+900,
                question={"id": "same-question"}, secret="must-not-copy")


def test_bare_idioms_only_capture_active_eligible_players_and_four_han():
    assert gate.capture(900, event("春暖花开")) is None
    gate.publish(900, 100, idiom_state("solo"))
    assert gate.capture(900, event("春暖花开", uid=22, mid=2)) is None
    captured = gate.capture(900, event(" 「春暖花开！」 " , mid=3))
    assert captured["bare_idiom"] and captured["answer_text"] == "春暖花开"
    assert not captured["explicit"] and captured["question_id"] == "same-question"
    for mid, text in enumerate(("我猜春暖花开", "春 暖花开", "1234", "H8", "春暖花"), 10):
        assert gate.capture(900, event(text, mid=mid)) is None
    gate.publish(900, 100, idiom_state())
    assert gate.capture(900, event("春暖花开", uid=33, mid=20))["bare_idiom"]
    assert gate.capture(900, event("春暖花开", uid=900, mid=21)) is None
    assert gate.capture(900, event("春暖花开", mid=22, segments=[SimpleNamespace(type="image", data={})])) is None
    assert gate.capture(900, event("春暖花开", mid=23, segments=[SimpleNamespace(type="at", data={"qq": 33})])) is None
    assert gate.capture(900, event("春暖花开", gid=999, mid=24)) is None
    ended = idiom_state()
    ended["status"] = "ended"
    gate.publish(900, 100, ended)
    assert gate.capture(900, event("春暖花开", mid=25)) is None


def test_bare_idiom_capture_remains_valid_across_hint_deadline_not_whole_deadline():
    current = idiom_state()
    current["expires_at"] = time.time()-1  # 60s hint due, game still active
    gate.publish(900, 100, current)
    assert gate.capture(900, event("春暖花开", mid=1))["bare_idiom"]
    current["deadline"] = time.time()-1
    gate.publish(900, 100, current)
    assert gate.capture(900, event("春暖花开", mid=2)) is None


def test_disabled_idiom_does_not_capture_chat_or_enter_agent_context():
    gate.configure(enabled=True, allowed_groups=[], blocked_groups=[], disabled_games=["idiom"])
    gate.publish(900, 100, idiom_state())
    assert gate.capture(900, event("春暖花开")) is None
    assert gate.game_context(100, 900) == ""
    assert gate.capture(900, event("#结束游戏", mid=2))


def test_answer_after_hint_threshold_before_whole_deadline_protects_expiry(monkeypatch):
    clock = [1899.0]
    monkeypatch.setattr(gate.time, "time", lambda: clock[0])
    current = dict(idiom_state(), expires_at=1060, question_deadline=1060, deadline=1900)
    gate.publish(900, 100, current)
    captured = gate.capture(900, event("春暖花开", mid=50, uid=22))
    assert captured["received_at"] == 1899
    clock[0] = 1901
    assert gate.has_pending_answer((900, 100), current)
    gate.finish_answer(900, 100, 50)
    assert not gate.has_pending_answer((900, 100), current)


def test_bare_idiom_isolated_from_agent_at_only_and_legacy_quota(monkeypatch):
    import asyncio
    from plugins import dev_scope, quota
    from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message
    gate.publish(900, 100, idiom_state())
    msg = GroupMessageEvent(time=int(time.time()), self_id=900, post_type="message", sub_type="normal",
        user_id=33, group_id=100, message_type="group", message_id=50,
        message=Message("春暖花开"), raw_message="春暖花开", font=0, sender={"user_id":33,"nickname":"player"})
    monkeypatch.setattr(dev_scope.console_runtime, "observe_incoming", lambda *a: None)
    monkeypatch.setattr(dev_scope.console_runtime, "ready", lambda: False)
    monkeypatch.setattr(dev_scope, "AT_ONLY_ENABLED", True)
    monkeypatch.setattr(quota, "_is_valid_call", lambda *a: (_ for _ in ()).throw(AssertionError("game answer should not consume command quota")))
    async def scenario():
        bot = SimpleNamespace(self_id="900")
        await dev_scope._development_scope_gate(bot, msg)
        await quota._quota_check(bot, msg)
        assert gate.capture(900, msg)["bare_idiom"]
    asyncio.run(scenario())
