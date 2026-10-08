"""New board games through the real matcher, without network or real engines."""
import asyncio
from copy import deepcopy

import pytest
from test_minigame_runtime import isolated, message, runtime, gate
from test_minigames_sqlstore import database, state as sql_state, MinigameSession
from sqlalchemy.orm import Session


class Engines:
    def __init__(self): self.calls = []
    def check_ready(self, game, cancel=None): self.calls.append(("ready", game))
    def choose(self, state, cancel=None):
        self.calls.append(("move", state["game"], state["difficulty"]))
        if state["game"] == "xiangqi":
            from plugins.minigames import xiangqi
            return xiangqi.legal_moves([m["move"] for m in state["moves"]])[0]
        return next(i for i, stone in enumerate(state["board"]) if stone == 0)
    def suggest_dead(self, state, cancel=None):
        self.calls.append(("score", state["game"]))
        return []


@pytest.mark.parametrize("game,move", [("象棋", "炮二平五"), ("围棋", "D4")])
@pytest.mark.parametrize("difficulty", ["娱乐", "认真"])
def test_new_games_menu_readonly_human_bot_combined(isolated, monkeypatch, game, move, difficulty):
    service, bot = isolated
    engines = Engines()
    monkeypatch.setattr(runtime, "ENGINES", engines)
    async def scenario():
        await service.initialize()
        await runtime.handle_game(bot, message("#"+game, 1))
        assert not service.states and not engines.calls
        await runtime.handle_game(bot, message(f"#{game} 对战 {difficulty}", 2))
        assert len(bot.sent) == 2
        assert service.states[(900, 100)]["difficulty"] == ("serious" if difficulty == "认真" else "casual")
        await runtime.handle_game(bot, message(move, 3))
        await asyncio.gather(*list(runtime._tasks.values()))
        assert len(bot.sent) == 3
        assert len(service.states[(900, 100)]["moves"]) == 2
        assert sum(call[0] == "move" for call in engines.calls) == 1
        await runtime.handle_game(bot, message(move, 3))
        assert len(bot.sent) == 3
    asyncio.run(scenario())


def test_public_go_scoring_confirmations_use_captured_proposal(isolated, monkeypatch):
    service, bot = isolated
    engines = Engines()
    monkeypatch.setattr(runtime, "ENGINES", engines)
    async def scenario():
        await service.initialize()
        for mid, user, text in [(1, 11, "#围棋 双人 13路"), (2, 22, "#加入游戏"),
                                (3, 11, "#停一手"), (4, 22, "#停一手")]:
            await runtime.handle_game(bot, message(text, mid, user))
        await asyncio.gather(*list(runtime._tasks.values()))
        state = service.states[(900, 100)]
        assert state["phase"] == "scoring" and engines.calls == [("score", "go")]
        first, second = message("#确认数目", 5), message("#确认数目", 6, 22)
        a, b = gate.capture(900, first), gate.capture(900, second)
        assert a["score_id"] == b["score_id"] == state["scoring"]["id"]
        await runtime.handle_game(bot, first)
        await runtime.handle_game(bot, second)
        assert service.states[(900, 100)]["end_reason"] == "scored"
        assert service.states[(900, 100)]["winner"] == 2
    asyncio.run(scenario())


@pytest.mark.parametrize("game", ["象棋", "围棋"])
def test_missing_engine_refuses_start_but_pvp_works(isolated, monkeypatch, game):
    service, bot = isolated
    engines = Engines()
    def unavailable(*args): raise RuntimeError("offline")
    engines.check_ready = unavailable
    monkeypatch.setattr(runtime, "ENGINES", engines)
    async def scenario():
        await service.initialize()
        await runtime.handle_game(bot, message(f"#{game} 对战", 1))
        assert not service.store.rows and "尚不可用" in str(bot.sent[-1][1])
        await runtime.handle_game(bot, message(f"#{game} 双人", 2))
        assert service.states[(900, 100)]["status"] == "waiting"
    asyncio.run(scenario())


@pytest.mark.parametrize("text", ["H3-E3", "h3e3", "H3 E3", "H3→E3", "Ｈ３－Ｅ３", "炮二平五", "馬８進７", "前车进一"])
def test_xiangqi_shorthand_routing_matches_parser_surface(isolated, text):
    state = {"session_id": "game", "version": 1, "game": "xiangqi", "mode": "pvp",
             "status": "active", "expires_at": 4000000000,
             "players": {"1": {"id": 11}, "2": {"id": 22}}}
    gate.publish(900, 100, state)
    assert gate.capture(900, message(text, 1))
    assert not gate.capture(900, message(text, 2, 33))
    assert not gate.capture(900, message("我想走 "+text, 3))
    assert not gate.capture(900, message(text, 4, group=999))


def test_strategy_snapshot_has_public_status_not_engine_or_board(isolated):
    state = {"session_id": "game", "version": 1, "game": "go", "mode": "bot",
             "status": "active", "phase": "scoring", "expires_at": 4000000000,
             "bot_name": "艾蕾", "board": [1]*81, "analysis": "private-engine-analysis",
             "scoring": {"id": "proposal", "revision": 1, "dead": [4]},
             "players": {"1": {"id": 11, "name": "旅行者"}, "2": {"id": 900, "name": "艾蕾"}}}
    gate.publish(900, 100, state)
    text = gate.game_context(100, 900)
    assert "群友与机器人" in text and "围棋" in text and "确认" in text
    assert "private-engine-analysis" not in text and "board" not in gate._states[(900, 100)]


def test_compact_strategy_sql_roundtrip(database):
    store, engine = database
    row = dict(sql_state(), schema=4, game="go", moves=[{"move": 3, "side": 1, "user_id": 11},
                                                         {"move": "pass", "side": 2, "user_id": 22}])
    before = deepcopy(row)
    asyncio.run(store.save(row, None))
    assert asyncio.run(store.load(99, 10)) == row == before
    with Session(engine) as session:
        payload = session.get(MinigameSession, (99, 10)).payload
        assert "compact-v1" in payload and '[3,1,11]' in payload


def test_storage_rejects_oversize_record_before_write(database):
    store, _ = database
    row = dict(sql_state(), noise="x"*65000)
    with pytest.raises(ValueError, match="budget"):
        asyncio.run(store.save(row, None))
    assert asyncio.run(store.load(99, 10)) is None
