"""Transactional Xiangqi/Go integration with deterministic in-memory storage."""
import asyncio
from copy import deepcopy
import importlib
from pathlib import Path
import sys
import threading
import types

import pytest

package = types.ModuleType("_minigame_phase3_service_tests")
package.__path__ = [str(Path(__file__).parents[1] / "plugins/minigames")]
sys.modules[package.__name__] = package
commands = importlib.import_module(package.__name__ + ".commands")
module = importlib.import_module(package.__name__ + ".service")
strategy = importlib.import_module(package.__name__ + ".strategy_session")
go = importlib.import_module(package.__name__ + ".go_rules")
xq = importlib.import_module(package.__name__ + ".xiangqi")
KEY = (900, 100)


class Store:
    def __init__(self):
        self.rows = {}
        self.fail = False
        self.uncertain = False

    async def initialize(self):
        pass

    async def load_all(self):
        return deepcopy(list(self.rows.values()))

    async def load(self, *key):
        return deepcopy(self.rows.get(key))

    async def save(self, state, expected):
        if self.fail:
            raise RuntimeError("database offline")
        key = state["bot_id"], state["group_id"]
        old = self.rows.get(key)
        assert (old["version"] if old else None) == expected
        self.rows[key] = deepcopy(state)
        if self.uncertain:
            raise RuntimeError("commit acknowledgement lost")


async def setup(**options):
    store, clock, published = Store(), [1000.0], []
    service = module.GameService(store, clock=lambda: clock[0],
                                 publish=lambda *args: published.append(deepcopy(args)), **options)
    await service.initialize()
    return service, store, clock, published


def stamp(service, group=100):
    row = service.states.get((900, group))
    return {"session_id": row["session_id"] if row else None,
            "version": row["version"] if row else None,
            "score_id": (row.get("scoring") or {}).get("id") if row else None,
            "score_revision": (row.get("scoring") or {}).get("revision") if row else None}


async def command(service, text, user=11, mid=None, group=100, **kwargs):
    # Distinct IDs by default, while explicit IDs exercise deduplication.
    if mid is None:
        service._test_message_id = getattr(service, "_test_message_id", 0) + 1
        mid = service._test_message_id
    parsed = commands.parse_command(text, kwargs.pop("mentions", ()), bot_id=900, user_id=user)
    return await service.execute(900, group, user, f"player-{user}", parsed,
                                 kwargs.pop("arrival", stamp(service, group)),
                                 message_id=mid, **kwargs)


async def pvp(service, game="围棋", extra=""):
    await command(service, f"#{game} 双人 {extra}".strip())
    await command(service, "#加入游戏", user=22)


async def score_phase(service, *, mode="pvp", stones=True):
    if mode == "pvp":
        await pvp(service)
    else:
        await command(service, "#围棋 对战")
    if stones:
        await command(service, "A1")
        if mode == "pvp":
            await command(service, "B1", user=22)
        else:
            await service.bot_step(KEY, None, asyncio.Semaphore(1), strategy_choose=lambda *args: "B1")
    await command(service, "#停一手")
    if mode == "pvp":
        await command(service, "#停一手", user=22)
    else:
        await service.bot_step(KEY, None, asyncio.Semaphore(1), strategy_choose=lambda *args: "pass")
    assert service.states[KEY]["phase"] == "scoring"


@pytest.mark.parametrize("size", [9, 13, 19])
def test_go_pvp_dimensions_join_and_restart(size):
    async def scenario():
        service, store, clock, _ = await setup()
        result = await command(service, f"#围棋 双人 {size}路 后手")
        assert result.state["status"] == "waiting"
        assert len(result.state["board"]) == size * size
        assert result.state["players"]["2"]["id"] == 11
        with pytest.raises(ValueError):
            await command(service, "A1")
        await command(service, "#加入游戏", user=22)
        await command(service, "A1", user=22)
        last = f"{go.COLUMNS[size - 1]}{size}"
        await command(service, last)
        expected = deepcopy(service.states[KEY])
        restarted = module.GameService(store, clock=lambda: clock[0])
        await restarted.initialize()
        assert restarted.states[KEY] == expected
        assert restarted.states[KEY]["board"][0] == 1
        assert restarted.states[KEY]["board"][-1] == 2
    asyncio.run(scenario())


def test_go_invitation_only_accepts_named_player_and_keeps_single_group_slot():
    async def scenario():
        service, _, _, _ = await setup()
        await command(service, "#围棋 13路 后手", mentions=[22])
        for user in (11, 33, 900):
            with pytest.raises(ValueError):
                await command(service, "#加入游戏", user=user)
        await command(service, "#加入游戏", user=22)
        with pytest.raises(ValueError):
            await command(service, "#井字棋 对战")
        await command(service, "#井字棋 对战", group=101)
        assert service.states[(900, 101)]["game"] == "tictactoe"
        with pytest.raises(ValueError):
            await command(service, "#结束游戏", user=33)
        assert (await command(service, "#结束游戏", user=33, is_admin=True)).state["status"] == "ended"
    asyncio.run(scenario())


def test_go_two_passes_wait_for_dead_suggestion_and_both_confirmations():
    async def scenario():
        service, store, _, _ = await setup()
        await score_phase(service)
        pending = deepcopy(service.states[KEY])
        assert pending["passes"] == 2 and pending["scoring"]["source"] == "pending"
        assert not service.needs_bot(pending)
        with pytest.raises(ValueError, match="尚未就绪"):
            await command(service, "#确认数目")
        with pytest.raises(ValueError, match="先.*继续对局"):
            await command(service, "#悔棋")
        result = await service.scoring_step(KEY, lambda state, flag: [], asyncio.Semaphore(1))
        assert result.state["scoring"]["source"] == "engine"
        first = await command(service, "#确认数目")
        assert first.state["status"] == "active" and first.state["scoring"]["confirmed"] == [11]
        with pytest.raises(ValueError, match="已确认"):
            await command(service, "#确认数目")
        with pytest.raises(ValueError, match="本局玩家"):
            await command(service, "#确认数目", user=33)
        ended = await command(service, "#确认数目", user=22)
        assert ended.state["status"] == "ended" and ended.state["end_reason"] == "scored"
        assert ended.state["winner"] == ended.state["scoring"]["score"]["winner"]
        service.validate(store.rows[KEY])
        await command(service, "#井字棋 对战")
    asyncio.run(scenario())


def test_changed_dead_proposal_clears_confirmations_without_extending_deadline():
    async def scenario():
        service, _, clock, _ = await setup()
        await score_phase(service)
        await service.scoring_step(KEY, lambda *args: [], asyncio.Semaphore(1))
        await command(service, "#确认数目")
        deadline = service.states[KEY]["expires_at"]
        clock[0] += 40
        changed = await command(service, "#标死 A1", user=22)
        assert changed.state["scoring"]["confirmed"] == []
        assert changed.state["scoring"]["dead"] == [0]
        assert changed.state["expires_at"] == deadline
        await command(service, "#确认数目", user=22)
        changed = await command(service, "#取消死子 A1")
        assert changed.state["scoring"]["confirmed"] == []
        assert changed.state["scoring"]["dead"] == []
        assert changed.state["expires_at"] == deadline
        service.validate(changed.state)
    asyncio.run(scenario())


def test_two_confirmations_captured_for_same_proposal_are_both_accepted():
    async def scenario():
        service, _, _, _ = await setup()
        await score_phase(service)
        await service.scoring_step(KEY, lambda *args: [], asyncio.Semaphore(1))
        arrival = stamp(service)
        results = await asyncio.gather(
            command(service, "#确认数目", user=11, mid=101, arrival=arrival),
            command(service, "#确认数目", user=22, mid=102, arrival=arrival),
        )
        assert results[0].state["scoring"]["revision"] == results[1].state["scoring"]["revision"]
        assert service.states[KEY]["status"] == "ended"
        assert set(service.states[KEY]["scoring"]["confirmed"]) == {11, 22}
    asyncio.run(scenario())


def test_confirmation_for_old_proposal_does_not_accept_changed_dead_marks():
    async def scenario():
        service, _, _, _ = await setup()
        await score_phase(service)
        await service.scoring_step(KEY, lambda *args: [], asyncio.Semaphore(1))
        arrival = stamp(service)
        await command(service, "#标死 A1", user=22)
        before = deepcopy(service.states[KEY])
        with pytest.raises(ValueError):
            await command(service, "#确认数目", user=11, mid=101, arrival=arrival)
        assert service.states[KEY] == before
        assert service.states[KEY]["scoring"]["confirmed"] == []
    asyncio.run(scenario())


def test_old_confirmation_cannot_accept_new_scoring_phase_with_same_revision():
    async def scenario():
        service, _, _, _ = await setup()
        await score_phase(service)
        await service.scoring_step(KEY, lambda *args: [], asyncio.Semaphore(1))
        arrival = stamp(service)
        await command(service, "#继续对局", user=22)
        await command(service, "#停一手", user=11)
        await command(service, "#停一手", user=22)
        await service.scoring_step(KEY, lambda *args: [], asyncio.Semaphore(1))
        current = stamp(service)
        assert current["session_id"] == arrival["session_id"]
        assert current["score_revision"] == arrival["score_revision"]
        assert current["score_id"] != arrival["score_id"]
        before = deepcopy(service.states[KEY])
        with pytest.raises(ValueError):
            await command(service, "#确认数目", user=11, mid=101, arrival=arrival)
        assert service.states[KEY] == before
        assert service.states[KEY]["scoring"]["confirmed"] == []
        await command(service, "#确认数目", user=11, mid=102, arrival=current)
        result = await command(service, "#确认数目", user=22, mid=103, arrival=current)
        assert result.state["status"] == "ended"
    asyncio.run(scenario())


def test_bot_accepts_only_original_engine_dead_suggestion():
    async def scenario():
        service, _, _, _ = await setup()
        await score_phase(service, mode="bot")
        await service.scoring_step(KEY, lambda *args: [], asyncio.Semaphore(1))
        assert service.states[KEY]["scoring"]["confirmed"] == [900]
        await command(service, "#标死 B1")
        assert service.states[KEY]["scoring"]["confirmed"] == []
        with pytest.raises(ValueError, match="机器人不同意"):
            await command(service, "#确认数目")
        unchanged = deepcopy(service.states[KEY])
        await command(service, "#取消死子 B1")
        assert service.states[KEY]["scoring"]["confirmed"] == [900]
        assert service.states[KEY]["expires_at"] == unchanged["expires_at"]
        assert (await command(service, "#确认数目")).state["status"] == "ended"
    asyncio.run(scenario())


def test_mark_dead_marks_whole_connected_block_and_pending_confirmation_restores():
    async def scenario():
        service, store, clock, _ = await setup()
        await pvp(service)
        for text, user in [("A1", 11), ("J9", 22), ("A2", 11), ("J8", 22),
                           ("#停一手", 11), ("#停一手", 22)]:
            await command(service, text, user=user)
        await service.scoring_step(KEY, lambda *args: [], asyncio.Semaphore(1))
        marked = await command(service, "#标死 A1")
        assert marked.state["scoring"]["dead"] == [0, 9]
        await command(service, "#确认数目")
        before = deepcopy(service.states[KEY])
        clock[0] += 20
        recovered = module.GameService(store, clock=lambda: clock[0])
        await recovered.initialize()
        assert recovered.states[KEY] == before
        assert recovered.states[KEY]["scoring"]["confirmed"] == [11]
        ended = await command(recovered, "#确认数目", user=22, mid=100)
        assert ended.state["status"] == "ended"
        assert ended.state["scoring"]["dead"] == [0, 9]
    asyncio.run(scenario())


@pytest.mark.parametrize("bad_suggestion", ["raise", [0, 2]])
def test_failed_or_invalid_suggestion_pvp_allows_honest_manual_scoring(bad_suggestion):
    async def scenario():
        service, _, _, _ = await setup()
        await score_phase(service)
        def suggest(*args):
            if bad_suggestion == "raise":
                raise RuntimeError("engine unavailable")
            return bad_suggestion
        result = await service.scoring_step(KEY, suggest, asyncio.Semaphore(1))
        assert result.state["scoring"]["source"] == "manual"
        assert "不可用" in result.message and result.state["scoring"]["suggested"] is None
        await command(service, "#标死 A1")
        await command(service, "#确认数目")
        assert (await command(service, "#确认数目", user=22)).state["status"] == "ended"
    asyncio.run(scenario())


def test_bot_suggestion_failure_preserves_pending_state_for_retry():
    async def scenario():
        service, _, _, _ = await setup()
        await score_phase(service, mode="bot")
        before = deepcopy(service.states[KEY])
        def fail(*args):
            raise RuntimeError("engine crashed")
        with pytest.raises(RuntimeError):
            await service.scoring_step(KEY, fail, asyncio.Semaphore(1))
        assert service.states[KEY] == before and not service.searches
        await service.scoring_step(KEY, lambda *args: [], asyncio.Semaphore(1))
        assert service.states[KEY]["scoring"]["source"] == "engine"
    asyncio.run(scenario())


def test_resume_preserves_go_history_and_undo_restores_captures_after_restart():
    async def scenario():
        service, store, clock, _ = await setup()
        await pvp(service)
        await command(service, "A1")
        await command(service, "B1", user=22)
        await command(service, "#停一手")
        await command(service, "A2", user=22)
        assert service.states[KEY]["captures"]["2"] == 1
        await command(service, "#停一手")
        await command(service, "#停一手", user=22)
        await command(service, "#继续对局")
        assert service.states[KEY]["resume_after"] == [6]
        await command(service, "C1")
        await command(service, "#悔棋")
        undone = await command(service, "#同意悔棋", user=22)
        assert len(undone.state["moves"]) == 6 and undone.state["passes"] == 0
        assert undone.state["resume_after"] == [6]
        assert undone.state["captures"] == {"1": 0, "2": 1}
        recovered = module.GameService(store, clock=lambda: clock[0])
        await recovered.initialize()
        assert recovered.states[KEY] == undone.state
        await command(recovered, "C1", mid=100)
        assert recovered.states[KEY]["turn"] == 2
    asyncio.run(scenario())


def test_go_bot_undo_rebuilds_capture_and_pass_history():
    async def scenario():
        service, _, _, _ = await setup()
        await command(service, "#围棋 对战")
        await command(service, "A1")
        await service.bot_step(KEY, None, asyncio.Semaphore(1), strategy_choose=lambda *args: "B1")
        await command(service, "#停一手")
        await service.bot_step(KEY, None, asyncio.Semaphore(1), strategy_choose=lambda *args: "A2")
        assert service.states[KEY]["captures"]["2"] == 1
        undone = await command(service, "#悔棋")
        assert len(undone.state["moves"]) == 2
        assert undone.state["board"][0] == 1
        assert undone.state["captures"] == {"1": 0, "2": 0}
        assert undone.state["turn"] == 1 and undone.state["passes"] == 0
        service.validate(undone.state)
    asyncio.run(scenario())


def test_waiting_and_scoring_deadlines_survive_views_and_restart():
    async def scenario():
        service, store, clock, _ = await setup()
        await command(service, "#围棋 双人", group=101)
        await score_phase(service)
        deadline = service.states[KEY]["expires_at"]
        clock[0] += 121
        await command(service, "#棋盘", user=44)
        expired = await service.expire()
        assert len(expired) == 1 and service.states[(900, 101)]["status"] == "ended"
        assert service.states[KEY]["expires_at"] == deadline
        clock[0] = deadline
        recovered = module.GameService(store, clock=lambda: clock[0])
        await recovered.initialize()
        assert recovered.states[KEY]["status"] == "ended"
        assert recovered.states[KEY]["winner"] == 0
        assert await recovered.expire() == []
    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["move", "suggestion", "confirm"])
def test_database_failures_never_publish_unsaved_go_state(operation):
    async def scenario():
        service, store, _, published = await setup()
        if operation == "move":
            await pvp(service)
        else:
            await score_phase(service)
            if operation == "confirm":
                await service.scoring_step(KEY, lambda *args: [], asyncio.Semaphore(1))
        before = deepcopy(service.states[KEY])
        store.fail = True
        with pytest.raises(RuntimeError):
            if operation == "suggestion":
                await service.scoring_step(KEY, lambda *args: [], asyncio.Semaphore(1))
            else:
                await command(service, "A1" if operation == "move" else "#确认数目")
        assert service.states[KEY] == store.rows[KEY] == before
        assert published[-1][2] == before
        assert not service.searches
    asyncio.run(scenario())


def test_go_commit_ack_lost_reconciles_and_duplicate_is_silent():
    async def scenario():
        service, store, _, _ = await setup()
        await pvp(service)
        arrival = stamp(service)
        store.uncertain = True
        with pytest.raises(RuntimeError):
            await command(service, "A1", mid=100, arrival=arrival)
        assert len(service.states[KEY]["moves"]) == 1
        store.uncertain = False
        assert (await command(service, "A1", mid=100, arrival=arrival)).silent
        assert len(service.states[KEY]["moves"]) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["#悔棋", "#结束游戏", "#认输"])
def test_late_go_engine_result_cannot_override_undo_cancel_or_resign(action):
    async def scenario():
        service, _, _, _ = await setup()
        await command(service, "#围棋 对战")
        await command(service, "A1")
        entered, release = threading.Event(), threading.Event()
        def choose(state, flag):
            entered.set()
            assert release.wait(3)
            return "B1"
        task = asyncio.create_task(service.bot_step(KEY, None, asyncio.Semaphore(1), strategy_choose=choose))
        assert await asyncio.to_thread(entered.wait, 3)
        try:
            result = await command(service, action)
        finally:
            release.set()
        assert await task is None
        assert service.states[KEY] == result.state and not service.searches
        assert service.states[KEY]["board"][1] == 0
    asyncio.run(scenario())


def test_late_scoring_suggestion_cannot_reenter_resumed_game():
    async def scenario():
        service, _, _, _ = await setup()
        await score_phase(service)
        entered, release = threading.Event(), threading.Event()
        def suggest(state, flag):
            entered.set()
            assert release.wait(3)
            return [0]
        task = asyncio.create_task(service.scoring_step(KEY, suggest, asyncio.Semaphore(1)))
        assert await asyncio.to_thread(entered.wait, 3)
        try:
            await command(service, "#继续对局", user=22)
        finally:
            release.set()
        assert await task is None
        assert service.states[KEY]["phase"] == "play"
        assert service.states[KEY]["scoring"] is None and not service.searches
    asyncio.run(scenario())


def test_late_go_search_cannot_revive_expired_session():
    async def scenario():
        service, _, clock, _ = await setup()
        await command(service, "#围棋 对战")
        await command(service, "A1")
        entered, release = threading.Event(), threading.Event()
        def choose(state, flag):
            entered.set()
            assert release.wait(3)
            return "B1"
        task = asyncio.create_task(service.bot_step(KEY, None, asyncio.Semaphore(1), strategy_choose=choose))
        assert await asyncio.to_thread(entered.wait, 3)
        try:
            clock[0] = service.states[KEY]["expires_at"]
            expired = await service.expire()
        finally:
            release.set()
        assert len(expired) == 1 and await task is None
        assert service.states[KEY]["status"] == "ended"
        assert len(service.states[KEY]["moves"]) == 1 and not service.searches
    asyncio.run(scenario())


def test_replayed_or_stale_go_move_does_not_land_on_next_turn():
    async def scenario():
        service, _, _, _ = await setup()
        await pvp(service)
        arrival = stamp(service)
        await command(service, "A1", arrival=arrival, mid=100)
        assert (await command(service, "A1", arrival=arrival, mid=100)).silent
        await command(service, "B1", user=22)
        with pytest.raises(module.GameError, match="已变化"):
            await command(service, "C1", arrival=arrival, mid=101)
        assert len(service.states[KEY]["moves"]) == 2
    asyncio.run(scenario())


@pytest.mark.parametrize("game", ["go", "xiangqi"])
def test_new_game_switch_prevents_start(game):
    async def scenario():
        service, store, _, _ = await setup(strategy_enabled={game: False})
        name = "围棋" if game == "go" else "象棋"
        with pytest.raises(module.GameError, match="尚未启用"):
            await command(service, f"#{name} 对战")
        assert not store.rows
    asyncio.run(scenario())


@pytest.mark.parametrize("mutation", ["board", "moves", "score", "confirmation", "phase"])
def test_corrupt_strategy_restore_rejected_not_replaced(mutation):
    async def scenario():
        service, store, clock, _ = await setup()
        await score_phase(service)
        await service.scoring_step(KEY, lambda *args: [], asyncio.Semaphore(1))
        row = store.rows[KEY]
        if mutation == "board": row["board"][2] = 1
        elif mutation == "moves": row["moves"][0]["user_id"] = 33
        elif mutation == "score": row["scoring"]["score"]["black"] += 1
        elif mutation == "confirmation": row["scoring"]["confirmed"] = [33]
        else: row["phase"] = "play"
        corrupted = deepcopy(row)
        recovered = module.GameService(store, clock=lambda: clock[0])
        with pytest.raises(ValueError):
            await recovered.initialize()
        assert store.rows[KEY] == corrupted and not recovered.ready
    asyncio.run(scenario())


def test_xiangqi_coordinate_chinese_undo_draw_and_restore():
    pytest.importorskip("pyffish")
    async def scenario():
        service, store, clock, _ = await setup()
        await pvp(service, "象棋")
        await command(service, "#走棋 炮二平五")
        await command(service, "马八进七", user=22)
        await command(service, "#求和")
        assert service.states[KEY]["pending_draw"]["user_id"] == 11
        await command(service, "#拒绝和棋", user=22)
        await command(service, "#求和")
        await command(service, "H1-G3")
        assert service.states[KEY]["pending_draw"] is None
        await command(service, "#悔棋")
        undone = await command(service, "#同意悔棋", user=22)
        expected = xq.replay(["h3e3", "h10g8"])
        assert undone.state["board"] == expected["board"] and undone.state["turn"] == 1
        recovered = module.GameService(store, clock=lambda: clock[0])
        await recovered.initialize()
        assert recovered.states[KEY] == undone.state
        await command(recovered, "#求和", mid=100)
        with pytest.raises(ValueError):
            await command(recovered, "#同意和棋", mid=101)
        clock[0] += 61
        with pytest.raises(ValueError):
            await command(recovered, "#同意和棋", user=22, mid=102)
        await command(recovered, "#求和", mid=103)
        ended = await command(recovered, "#同意和棋", user=22, mid=104)
        assert ended.state["winner"] == -1 and ended.state["end_reason"] == "agreed_draw"
        recovered.validate(ended.state)
    asyncio.run(scenario())


def test_xiangqi_bot_revalidates_engine_move_and_retains_state_on_failure():
    pytest.importorskip("pyffish")
    async def scenario():
        service, _, _, _ = await setup()
        await command(service, "#象棋 对战 后手")
        assert service.needs_bot(service.states[KEY])
        before = deepcopy(service.states[KEY])
        with pytest.raises(ValueError):
            await service.bot_step(KEY, None, asyncio.Semaphore(1), strategy_choose=lambda *args: "a1a10")
        assert service.states[KEY] == before and not service.searches
        first = await service.bot_step(KEY, None, asyncio.Semaphore(1), strategy_choose=lambda *args: "h3e3")
        assert first.state["turn"] == 2 and first.state["moves"][0]["user_id"] == 900
        with pytest.raises(ValueError):
            await command(service, "#悔棋")
        with pytest.raises(ValueError):
            await command(service, "#停一手")
        with pytest.raises(ValueError):
            await command(service, "#求和")
        await command(service, "马八进七")
        await service.bot_step(KEY, None, asyncio.Semaphore(1), strategy_choose=lambda *args: "h1g3")
        undone = await command(service, "#悔棋")
        assert len(undone.state["moves"]) == 1 and undone.state["turn"] == 2
    asyncio.run(scenario())


def test_missing_xiangqi_library_isolates_saved_session_and_other_games(monkeypatch):
    pytest.importorskip("pyffish")
    async def scenario():
        service, store, clock, _ = await setup()
        await command(service, "#象棋 对战")
        await command(service, "#围棋 对战", group=101)
        before = deepcopy(store.rows[KEY])
        def unavailable():
            raise xq.XiangqiUnavailable("not installed")
        monkeypatch.setattr(xq, "ensure_available", unavailable)
        recovered = module.GameService(store, clock=lambda: clock[0])
        await recovered.initialize()
        assert recovered.ready and KEY in recovered.recovery_errors
        assert KEY not in recovered.states and recovered.states[(900, 101)]["game"] == "go"
        with pytest.raises(module.GameError, match="原对局已保留"):
            await command(recovered, "#井字棋 对战")
        assert store.rows[KEY] == before
    asyncio.run(scenario())
