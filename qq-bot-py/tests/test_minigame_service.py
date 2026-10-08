import asyncio
from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
import threading
import types

import pytest


# Load pure modules without starting the NoneBot plugin or external services.
PACKAGE = "_minigame_service_tests"
BASE = Path(__file__).resolve().parents[1] / "plugins" / "minigames"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(BASE)]
sys.modules[PACKAGE] = package
from _minigame_service_tests.commands import Command, parse_command
from _minigame_service_tests.service import GameService, GameError


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


def stamp(service, group=100):
    row = service.states.get((900, group))
    return {"session_id": row["session_id"] if row else None,
            "version": row["version"] if row else None}


async def setup():
    store, clock = Store(), [1000.]
    service = GameService(store, clock=lambda: clock[0])
    await service.initialize()
    return service, store, clock


async def run_command(service, text, user=11, mid=1, group=100, **kwargs):
    return await service.execute(900, group, user, f"player-{user}",
        parse_command(text, kwargs.pop("mentions", ()), bot_id=900, user_id=user),
        kwargs.pop("arrival", stamp(service, group)), message_id=mid, **kwargs)


def test_command_parser():
    assert parse_command("＃小游戏 五子棋 简单 后手").game == "gomoku"
    assert not parse_command("#井字棋 后手").first
    assert parse_command("#五子棋", [22], bot_id=900, user_id=11).invitee == 22
    assert parse_command("#五子棋 双人 普通").mode == "pvp"
    for text, targets in [("#五子棋", ["all"]), ("#五子棋", [11]),
                          ("#井字棋", [22, 33]), ("#五子棋 普通 简单", []),
                          ("#棋盘 x", []), ("#落子", []), ("#小游戏foo", [])]:
        with pytest.raises(ValueError):
            parse_command(text, targets, bot_id=900, user_id=11)


def test_move_dedup_turn_and_stale_arrival():
    async def scenario():
        service, store, _ = await setup()
        await run_command(service, "#井字棋 双人")
        await run_command(service, "#加入游戏", user=22, mid=2)
        arrival = stamp(service)
        first = await run_command(service, "1", mid=3, arrival=arrival)
        assert first.state["board"][0] == 1
        duplicate = await run_command(service, "1", mid=3, arrival=arrival)
        assert duplicate.silent
        with pytest.raises(GameError):
            await run_command(service, "2", mid=4, arrival=arrival)
        with pytest.raises(GameError):
            await run_command(service, "2", mid=5)
        with pytest.raises(ValueError):
            await run_command(service, "1", user=22, mid=6)
        await run_command(service, "5", user=22, mid=7)
        assert len(store.rows[(900, 100)]["moves"]) == 2
        service.validate(service.states[(900, 100)])
    asyncio.run(scenario())


def test_invitation_join_admin_and_group_isolation():
    async def scenario():
        service, _, _ = await setup()
        await run_command(service, "#五子棋 后手", mentions=[22])
        with pytest.raises(GameError):
            await run_command(service, "#加入游戏", user=33, mid=2)
        with pytest.raises(GameError):
            await run_command(service, "#加入游戏", mid=3)
        joined = await run_command(service, "#加入游戏", user=22, mid=4)
        assert joined.state["players"]["1"]["id"] == 22
        await run_command(service, "#井字棋 对战", group=101, mid=5)
        with pytest.raises(GameError):
            await run_command(service, "#结束游戏", user=33, mid=6)
        ended = await run_command(service, "#结束游戏", user=33, mid=7, is_admin=True)
        assert ended.state["status"] == "ended"
        assert service.states[(900, 101)]["status"] == "active"
    asyncio.run(scenario())


def test_pvp_undo_approval_refusal_and_timeout():
    async def scenario():
        service, _, clock = await setup()
        await run_command(service, "#井字棋 双人")
        await run_command(service, "#加入游戏", user=22, mid=2)
        await run_command(service, "1", mid=3)
        deadline = service.states[(900, 100)]["expires_at"]
        clock[0] += 10
        await run_command(service, "#悔棋", mid=4)
        with pytest.raises(GameError):
            await run_command(service, "#同意悔棋", mid=5)
        await run_command(service, "#拒绝悔棋", user=22, mid=6)
        await run_command(service, "#悔棋", mid=7)
        undone = await run_command(service, "#同意悔棋", user=22, mid=8)
        assert undone.state["moves"] == [] and undone.state["turn"] == 1
        assert undone.state["undo_counts"]["11"] == 1
        assert undone.state["expires_at"] == deadline
        await run_command(service, "1", mid=9)
        await run_command(service, "#悔棋", mid=10)
        clock[0] += 61
        with pytest.raises(GameError):
            await run_command(service, "#同意悔棋", user=22, mid=11)
        await run_command(service, "5", user=22, mid=12)
        assert not service.states[(900, 100)]["pending_undo"]
    asyncio.run(scenario())


def test_bot_undo_cancels_running_search_and_late_result():
    async def scenario():
        service, _, _ = await setup()
        await run_command(service, "#井字棋 对战")
        await run_command(service, "1", mid=2)
        entered, release = threading.Event(), threading.Event()
        def choose(*args):
            entered.set()
            assert release.wait(3)
            return 4
        task = asyncio.create_task(service.bot_step((900, 100), choose, asyncio.Semaphore(2)))
        assert await asyncio.to_thread(entered.wait, 3)
        result = await run_command(service, "#悔棋", mid=3)
        release.set()
        assert await task is None
        assert result.state["moves"] == []
        assert service.states[(900, 100)]["board"] == [0]*9
        assert not service.searches
    asyncio.run(scenario())


def test_bot_step_once_and_full_round_undo():
    async def scenario():
        service, _, _ = await setup()
        await run_command(service, "#井字棋 对战")
        await run_command(service, "1", mid=2)
        result = await service.bot_step((900, 100), lambda *a: 4, asyncio.Semaphore(2))
        assert len(result.state["moves"]) == 2
        assert await service.bot_step((900, 100), lambda *a: 8, asyncio.Semaphore(2)) is None
        await run_command(service, "#悔棋", mid=3)
        assert service.states[(900, 100)]["moves"] == []
        for i in range(2):
            await run_command(service, "1", mid=10+i*2)
            await run_command(service, "#悔棋", mid=11+i*2)
        await run_command(service, "1", mid=20)
        with pytest.raises(GameError):
            await run_command(service, "#悔棋", mid=21)
    asyncio.run(scenario())


def test_restart_and_expiry_persist_without_view_extending():
    async def scenario():
        service, store, clock = await setup()
        await run_command(service, "#五子棋 双人")
        await run_command(service, "#井字棋 后手", group=101, mid=2)
        rebuilt = GameService(store, clock=lambda: clock[0])
        await rebuilt.initialize()
        assert rebuilt.needs_bot(rebuilt.states[(900, 101)])
        clock[0] += 121
        await run_command(rebuilt, "#棋盘", mid=3)
        assert rebuilt.states[(900, 100)]["status"] == "ended"
        assert rebuilt.states[(900, 101)]["status"] == "active"
        clock[0] += 600
        again = GameService(store, clock=lambda: clock[0])
        await again.initialize()
        assert all(s["status"] == "ended" for s in again.states.values())
        assert await again.expire() == []
    asyncio.run(scenario())


def test_database_failure_does_not_publish_false_success():
    async def scenario():
        service, store, _ = await setup()
        await run_command(service, "#井字棋 对战")
        old = deepcopy(service.states[(900, 100)])
        store.fail = True
        with pytest.raises(RuntimeError):
            await run_command(service, "1", mid=2)
        assert service.states[(900, 100)] == old
        store.fail, store.uncertain = False, True
        with pytest.raises(RuntimeError):
            await run_command(service, "1", mid=2)
        assert len(service.states[(900, 100)]["moves"]) == 1
        store.uncertain = False
        assert (await run_command(service, "1", mid=2)).silent
    asyncio.run(scenario())


def test_simultaneous_starts_and_no_next_round_buffering():
    async def scenario():
        service, _, _ = await setup()
        initial = stamp(service)
        results = await asyncio.gather(
            run_command(service, "#井字棋 对战", mid=1, arrival=initial),
            run_command(service, "#五子棋 对战", user=22, mid=2, arrival=initial), return_exceptions=True)
        assert sum(isinstance(r, GameError) for r in results) == 1
        first_turn = stamp(service)
        await run_command(service, "1", mid=3, arrival=first_turn)
        await service.bot_step((900, 100), lambda *a: 4, asyncio.Semaphore(2))
        with pytest.raises(GameError):
            await run_command(service, "2", mid=4, arrival=first_turn)
        assert len(service.states[(900, 100)]["moves"]) == 2
    asyncio.run(scenario())


def test_complete_pvp_win_releases_slot_and_resign():
    async def scenario():
        service, _, _ = await setup()
        await run_command(service, "#井字棋 双人")
        await run_command(service, "#加入游戏", user=22, mid=2)
        for mid, (move, user) in enumerate([("1",11),("4",22),("2",11),("5",22),("3",11)],3):
            result = await run_command(service, move, user=user, mid=mid)
        assert result.state["winner"] == 1 and result.state["status"] == "ended"
        service.validate(result.state)
        await run_command(service, "#五子棋 对战", mid=8)
        result = await run_command(service, "#认输", mid=9)
        assert result.state["winner"] == 2 and result.state["end_reason"] == "resigned"
    asyncio.run(scenario())
