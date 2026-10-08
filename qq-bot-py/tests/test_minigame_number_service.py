"""1A2B persistence/permissions/timers with no live DB or QQ access."""
import asyncio
from copy import deepcopy
import importlib
from pathlib import Path
import sys
import types

import pytest

package = types.ModuleType("_number_service_tests")
package.__path__ = [str(Path(__file__).parents[1] / "plugins/minigames")]
sys.modules[package.__name__] = package
service_module = importlib.import_module(package.__name__ + ".service")
commands = importlib.import_module(package.__name__ + ".commands")
GameService, GameError = service_module.GameService, service_module.GameError
Command = commands.Command
KEY = (900, 100)


class Store:
    def __init__(self):
        self.rows = {}
        self.fail = False
    async def initialize(self):
        pass
    async def load_all(self):
        return deepcopy(list(self.rows.values()))
    async def load(self, *key):
        return deepcopy(self.rows.get(key))
    async def save(self, state, expected):
        if self.fail:
            raise OSError("simulated save failure")
        key = (state["bot_id"], state["group_id"])
        assert (self.rows.get(key) or {}).get("version") == expected
        self.rows[key] = deepcopy(state)


async def setup(mode="solo"):
    clock, store = [1000.0], Store()
    service = GameService(store, clock=lambda: clock[0])
    await service.initialize()
    await run(service, "start", mode=mode)
    return service, store, clock


def stamp(service):
    state = service.states.get(KEY) or {}
    return {"session_id": state.get("session_id"), "version": state.get("version"),
            "question_id": state.get("round_id"), "received_at": service.clock()}


async def run(service, action, *, mode="solo", uid=11, mid=1, argument="", arrival=None, admin=False):
    return await service.execute(*KEY, uid, f"player{uid}",
        Command(action, game="number" if action == "start" else "", mode=mode, argument=argument),
        stamp(service) if arrival is None else arrival, message_id=mid, is_admin=admin,
        persona={"id": "eres", "name": "艾蕾"})


async def present(service):
    state = service.states[KEY]
    async with service.locks[KEY]:
        await service.number_presented_locked(KEY, state["session_id"], state["round_id"], service.clock())


def wrong(service):
    secret = service.states[KEY]["secret"]
    return secret[:2] + secret[3] + secret[2]


@pytest.mark.parametrize("mode,seconds", [("solo", 600), ("race", 900)])
def test_scoring_mode_clock_and_winner(mode, seconds):
    async def scenario():
        service, store, clock = await setup(mode)
        clock[0] += 30
        assert service.states[KEY]["started_at"] is None
        await present(service)
        assert service.states[KEY]["deadline"] == 1030 + seconds
        clock[0] += 2
        result = await run(service, "guess", mid=2, argument=wrong(service))
        service.validate(result.state)
        assert (result.state["attempts"][-1]["a"], result.state["attempts"][-1]["b"]) == (2, 2)
        clock[0] += 3
        result = await run(service, "answer", mid=3, argument=service.states[KEY]["secret"])
        service.validate(result.state)
        assert result.state["status"] == "ended" and result.state["winner_id"] == 11
        assert result.state["total_attempts"] == 2
        assert result.state["finished_at"] - result.state["started_at"] == 5
        assert store.rows[KEY] == result.state
    asyncio.run(scenario())


def test_same_round_old_version_accepted_and_replays_never_score_twice():
    async def scenario():
        service, _, clock = await setup("race")
        await present(service)
        clock[0] += 1
        arrival, secret = stamp(service), service.states[KEY]["secret"]
        await run(service, "guess", uid=22, mid=2, argument=wrong(service), arrival=arrival)
        winner, late = await asyncio.gather(
            run(service, "guess", uid=33, mid=3, argument=secret, arrival=arrival),
            run(service, "guess", uid=22, mid=4, argument=secret, arrival=arrival))
        assert winner.state["winner_id"] == 33 and late.silent
        assert (await run(service, "guess", uid=33, mid=3, argument=secret)).silent
        await run(service, "start", mid=5)
        with pytest.raises(GameError):
            await run(service, "guess", uid=22, mid=6, argument=secret, arrival=arrival)
        assert service.states[KEY]["total_attempts"] == 0
    asyncio.run(scenario())


def test_solo_permissions_race_cancellation_and_shared_room():
    async def scenario():
        service, _, _ = await setup()
        await present(service)
        for action in ("guess", "cancel", "skip"):
            with pytest.raises(GameError):
                await run(service, action, uid=22, mid=2, argument=wrong(service))
        with pytest.raises(GameError):
            await service.execute(*KEY, 22, "other", Command("start", "gomoku"), stamp(service), message_id=3)
        await run(service, "cancel", uid=22, mid=4, admin=True)
        await run(service, "start", mode="race", mid=5)
        await present(service)
        await run(service, "guess", uid=22, mid=6, argument=wrong(service))
        with pytest.raises(GameError):
            await run(service, "cancel", uid=22, mid=7)
        await run(service, "cancel", mid=8)
        assert service.states[KEY]["end_reason"] == "cancelled"
    asyncio.run(scenario())


@pytest.mark.parametrize("value", ["0123", "1123", "123", "１２３４", "hello"])
def test_invalid_guess_does_not_change_state(value):
    async def scenario():
        service, store, _ = await setup()
        await present(service)
        before = deepcopy(store.rows[KEY])
        with pytest.raises(ValueError):
            await run(service, "guess", mid=2, argument=value)
        assert service.states[KEY] == before and store.rows[KEY] == before
    asyncio.run(scenario())


def test_failed_storage_does_not_start_clock_or_apply_guess():
    async def scenario():
        service, store, _ = await setup()
        store.fail = True
        with pytest.raises(OSError):
            await present(service)
        assert service.states[KEY]["started_at"] is None
        store.fail = False
        await present(service)
        before = deepcopy(store.rows[KEY])
        store.fail = True
        with pytest.raises(OSError):
            await run(service, "guess", mid=2, argument=wrong(service))
        assert service.states[KEY] == before and store.rows[KEY] == before
    asyncio.run(scenario())


def test_restart_preserves_secret_history_and_deadline_then_expires_silently():
    async def scenario():
        service, store, clock = await setup("race")
        await present(service)
        for mid in range(2, 18):
            clock[0] += 1
            await run(service, "guess", uid=22, mid=mid, argument=wrong(service))
        before = deepcopy(store.rows[KEY])
        restored = GameService(store, clock=lambda: clock[0])
        await restored.initialize()
        assert restored.states[KEY] == before
        assert len(before["attempts"]) == 12 and before["total_attempts"] == 16
        await present(restored)
        assert restored.states[KEY]["deadline"] == before["deadline"]
        clock[0] = before["deadline"] + 1
        await restored.initialize()
        assert restored.states[KEY]["secret"] == before["secret"]
        assert restored.states[KEY]["end_reason"] == "deadline"
        assert restored.states[KEY]["winner_id"] == 0
        assert await restored.expire() == []
    asyncio.run(scenario())


def test_on_time_answer_survives_late_query_new_start_and_tick():
    async def scenario():
        service, _, clock = await setup("race")
        await present(service)
        clock[0] = service.states[KEY]["deadline"] - 1
        arrival, secret = stamp(service), service.states[KEY]["secret"]
        clock[0] += 2
        await run(service, "question", mid=2)
        with pytest.raises(GameError):
            await run(service, "start", mid=3)
        assert await service.expire(pending_answer=lambda *_: True) == []
        result = await run(service, "guess", mid=4, uid=22, argument=secret, arrival=arrival)
        assert result.state["winner_id"] == 22 and result.state["end_reason"] == "win"
    asyncio.run(scenario())


def test_feature_switch_and_unshown_guesses():
    async def scenario():
        service, _, clock = await setup()
        assert (await run(service, "guess", mid=2, argument=wrong(service))).state is None
        service.number_enabled = False
        with pytest.raises(GameError):
            await run(service, "question", mid=3)
        await run(service, "cancel", mid=4)
        with pytest.raises(GameError):
            await run(service, "start", mid=5)
        service.number_enabled = True
        await run(service, "start", mid=6)
        clock[0] += 601
        result = await service.expire()
        assert result[0].state["end_reason"] == "undelivered"
        service.validate(result[0].state)
    asyncio.run(scenario())
