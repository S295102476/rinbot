"""Natural requests use real transactional games with no synthetic QQ events."""
import asyncio
from collections import OrderedDict
from copy import deepcopy

import pytest
from pydantic import ValidationError

from test_minigame_runtime import isolated, message, runtime, gate
from test_minigame_phase3_runtime import Engines
from plugins import agent_game_tools as tools
from plugins.minigames.idioms import IdiomBank


@pytest.fixture(autouse=True)
def arrivals(monkeypatch):
    monkeypatch.setattr(gate, "_agent_arrivals", OrderedDict())
    monkeypatch.setattr(gate, "_agent_enabled", True)
    monkeypatch.setattr(gate, "_agent_groups", frozenset())


def context(bot, event):
    return {"bot": bot, "event": event, "batch_events": [event], "group_id": event.group_id,
            "game_snapshots": tools.capture_snapshots(bot, [event])}


@pytest.mark.parametrize("game,title", [("gomoku", "五子棋"), ("tictactoe", "井字棋"),
    ("xiangqi", "象棋"), ("go", "围棋"), ("idiom", "成语填空"), ("number", "猜数字")])
def test_six_real_games_start_show_and_finish(isolated, monkeypatch, game, title):
    service, bot = isolated
    monkeypatch.setattr(runtime, "ENGINES", Engines())
    monkeypatch.setattr(runtime, "render_idiom", lambda state: b"idiom-image")
    service.idiom_bank = IdiomBank(["春" + chr(0x4e10+i) + "花" + chr(0x4f10+i) for i in range(80)])
    async def scenario():
        await service.initialize()
        event = message("凛，来一局" + title, 10001)
        ctx = context(bot, event)
        original = deepcopy(event.model_dump())
        result = await tools.start({"source_message_id": event.message_id, "game": game}, ctx)
        assert result["terminal"] and result["game"] == game
        assert event.model_dump() == original
        assert service.states[(900, 100)]["host_id"] == 11
        assert len(bot.sent) == 1
        repeated = await tools.start({"source_message_id": event.message_id, "game": game}, ctx)
        assert repeated["status"] == "duplicate" and len(bot.sent) == 1
        show = message("看看棋盘", 10002)
        assert (await tools.show({"source_message_id": 10002}, context(bot, show)))["terminal"]
        end = message("结束这局", 10003)
        await tools.action({"source_message_id": 10003, "action": "cancel"}, context(bot, end))
        assert service.states[(900, 100)]["status"] == "ended"
    asyncio.run(scenario())


def test_mutation_is_bound_to_before_model_version(isolated):
    service, bot = isolated
    async def scenario():
        await service.initialize()
        start = message("来一局五子棋", 10100)
        await tools.start({"source_message_id": 10100, "game": "gomoku"}, context(bot, start))
        resign = message("我认输了", 10101)
        ctx = context(bot, resign)
        move = message("下 H8", 10102)
        await tools.action({"source_message_id": 10102, "action": "move", "argument": "H8"}, context(bot, move))
        await asyncio.gather(*list(runtime._tasks.values()))
        with pytest.raises(ValueError, match="变化"):
            await tools.action({"source_message_id": 10101, "action": "resign"}, ctx)
        assert service.states[(900, 100)]["status"] == "active"
    asyncio.run(scenario())


def test_host_identity_game_intent_and_invitee_cannot_be_invented(isolated):
    service, bot = isolated
    async def scenario():
        await service.initialize()
        event = message("五子棋真好玩", 10200)
        with pytest.raises(ValueError, match="明确"):
            await tools.start({"source_message_id": 10200, "game": "gomoku"}, context(bot, event))
        event = message("来一局五子棋", 10201)
        with pytest.raises(ValidationError):
            await tools.start({"source_message_id": 10201, "game": "gomoku", "user_id": 22}, context(bot, event))
        with pytest.raises(PermissionError):
            await tools.start({"source_message_id": 99999, "game": "gomoku"}, context(bot, event))
        with pytest.raises(ValueError, match="真实消息"):
            await tools.start({"source_message_id": 10201, "game": "gomoku", "mode": "pvp"}, context(bot, event))
        assert not service.states
    asyncio.run(scenario())


def test_existing_game_shown_not_replaced_and_nonplayer_rejected(isolated):
    service, bot = isolated
    async def scenario():
        await service.initialize()
        start = message("来一局五子棋", 10300)
        await tools.start({"source_message_id": 10300, "game": "gomoku"}, context(bot, start))
        original = service.states[(900, 100)]["session_id"]
        second = message("来一局井字棋", 10301, user=22)
        await tools.start({"source_message_id": 10301, "game": "tictactoe"}, context(bot, second))
        assert service.states[(900, 100)]["session_id"] == original
        bad = message("下 H8", 10302, user=22)
        with pytest.raises(ValueError, match="玩家"):
            await tools.action({"source_message_id": 10302, "action": "move", "argument": "H8"}, context(bot, bad))
    asyncio.run(scenario())


@pytest.mark.parametrize("text", ["凛，来一局五子棋", "艾蕾，玩猜数字", "看看棋盘", "下 H8", "我认输了", "猜 1234"])
def test_explicit_intent_recognized(text):
    assert gate.natural_intent(text)


@pytest.mark.parametrize("text", ["昨晚五子棋输了", "你会下五子棋吗", "不要开始五子棋", "游戏真好玩", "#五子棋", "如果我说我认输了"])
def test_discussion_or_negation_does_not_execute(text):
    assert gate.natural_intent(text) is None


def test_stale_snapshot_cached_at_ingress(isolated):
    service, bot = isolated
    event = message("来一局五子棋", 10400)
    first = gate.capture_agent(900, event)
    gate._states[(900, 100)] = {"session_id": "new", "version": 20}
    assert tools.capture_snapshots(bot, [event])[10400] == first


def test_at_only_exception_does_not_steal_hash_matcher(isolated):
    service, bot = isolated
    event = message("凛，来一局五子棋", 10500)
    assert gate.is_natural_request(900, event)
    assert gate.capture(900, event) is None
    assert not gate.is_natural_request(900, message("五子棋真好玩", 10501))


def test_runtime_stops_after_game_card_and_snapshots_before_model(isolated, monkeypatch):
    from plugins.agent_runtime import AgentRuntime
    from dataclasses import replace
    service, bot = isolated
    model_calls = []
    decision_runtime = AgentRuntime()
    decision_runtime.policy = replace(decision_runtime.policy, mode="active")
    async def fake_request(*args, **kwargs):
        model_calls.append(args)
        return '{"action":"tool","tool_calls":[{"name":"minigame_start","arguments":{"source_message_id":10600,"game":"gomoku"}}]}'
    monkeypatch.setattr(decision_runtime, "_request_decision", fake_request)
    async def scenario():
        await service.initialize()
        event = message("凛，来一局五子棋", 10600)
        parts, decision = await decision_runtime._decide(scope="group", request=event.get_plaintext(),
            force_reply=True, bot=bot, event=event, batch_events=[event], group_id=100, user_id=11)
        assert parts == [] and decision["terminal"] and decision["iterations"] == 1
        assert len(bot.sent) == 1 and len(model_calls) == 1
    asyncio.run(scenario())


def test_feature_off_preserves_manual_games(isolated, monkeypatch):
    service, bot = isolated
    monkeypatch.setattr(gate, "_agent_enabled", False)
    async def run():
        await service.initialize()
        event = message("来一局五子棋", 10700)
        assert not gate.is_natural_request(900, event)
        with pytest.raises(PermissionError):
            await tools.start({"source_message_id": 10700, "game": "gomoku"}, context(bot, event))
        await runtime.handle_game(bot, message("#五子棋 对战", 10701))
        assert service.states[(900, 100)]["game"] == "gomoku"
    asyncio.run(run())


def test_invitee_requires_real_at_and_group_membership(isolated, monkeypatch):
    service, bot = isolated
    calls = []
    async def member(**kwargs):
        calls.append(kwargs)
        return {"user_id": kwargs["user_id"], "nickname": "opponent"}
    monkeypatch.setattr(bot, "get_group_member_info", member, raising=False)
    async def run():
        await service.initialize()
        event = message("来一局五子棋 [CQ:at,qq=22]", 10800)
        await tools.start({"source_message_id": 10800, "game": "gomoku", "mode": "pvp"}, context(bot, event))
        assert service.states[(900, 100)]["invitee"] == 22
        assert calls == [{"group_id": 100, "user_id": 22}]
        join = message("我加入", 10801, user=33)
        with pytest.raises(ValueError, match="邀请"):
            await tools.action({"source_message_id": 10801, "action": "join"}, context(bot, join))
        join = message("我加入", 10802, user=22)
        await tools.action({"source_message_id": 10802, "action": "join"}, context(bot, join))
        assert service.states[(900, 100)]["status"] == "active"
    asyncio.run(run())


def test_missing_engine_is_actionable_and_never_starts_game(isolated, monkeypatch):
    service, bot = isolated
    async def unavailable(game):
        raise OSError("private engine path")
    monkeypatch.setattr(runtime, "_check_board_ready", unavailable)
    async def run():
        await service.initialize()
        event = message("来一局围棋", 10900)
        with pytest.raises(ValueError, match="引擎尚不可用") as failure:
            await tools.start({"source_message_id": 10900, "game": "go"}, context(bot, event))
        assert "private engine path" not in str(failure.value) and not service.states
    asyncio.run(run())


def test_memory_ingress_sees_chatter_before_at_only_reject(monkeypatch):
    from plugins import dev_scope
    from plugins.agent_memory_v2 import MEMORY_V2
    from nonebot.exception import IgnoredException
    ingested = []
    async def ingest(bot, event):
        ingested.append(event.message_id)
    monkeypatch.setattr(dev_scope, "_config", {"agent": {"enabled": True, "active_groups": [100]}})
    monkeypatch.setattr(dev_scope, "AT_ONLY_ENABLED", True)
    monkeypatch.setattr(dev_scope, "AGENT_GROUPS", frozenset({100}))
    monkeypatch.setattr(dev_scope.console_runtime, "observe_incoming", lambda *a: None)
    monkeypatch.setattr(dev_scope.console_runtime, "ready", lambda: False)
    monkeypatch.setattr(MEMORY_V2, "enabled", lambda group: group == 100)
    monkeypatch.setattr(MEMORY_V2, "ingest_event", ingest)
    async def run():
        from test_minigame_runtime import Bot
        event = message("平常聊天学习风格", 10950)
        with pytest.raises(IgnoredException):
            await dev_scope._development_scope_gate(Bot(), event)
        assert ingested == [10950]
    asyncio.run(run())


def test_tool_rounds_keep_request_and_rules_inside_total_character_budget():
    from plugins.agent_runtime import _bound_messages
    rules = "R" * 28000
    request = "当前请求：\n凛，来一局五子棋。"
    messages = [{"role": "system", "content": rules},
        {"role": "user", "content": "H" * 12000 + request},
        {"role": "user", "content": "OBSERVATIONS:" + "O" * 6000}]
    bounded = _bound_messages(messages)
    assert bounded[0]["content"] == rules
    assert "凛，来一局五子棋。" in bounded[1]["content"]
    assert sum(len(item["content"]) for item in bounded) <= 32000
    for _ in range(3):
        bounded.append({"role": "user", "content": "LATEST" + "Z" * 6000})
        bounded = _bound_messages(bounded)
        assert "凛，来一局五子棋。" in bounded[1]["content"]
        assert bounded[0]["content"] == rules
        assert sum(len(item["content"]) for item in bounded) <= 32000
