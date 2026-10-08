"""Idiom sessions exercise real transitions with isolated storage and clocks."""
import asyncio
from copy import deepcopy
import importlib
from pathlib import Path
import random
import sys
import types

import pytest

package = types.ModuleType("_idiom_session_tests")
package.__path__ = [str(Path(__file__).parents[1] / "plugins/minigames")]
sys.modules[package.__name__] = package
service_module = importlib.import_module(package.__name__ + ".service")
commands = importlib.import_module(package.__name__ + ".commands")
idioms = importlib.import_module(package.__name__ + ".idioms")
GameService, GameError = service_module.GameService, service_module.GameError
Command = commands.Command


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
            raise OSError("simulated storage failure")
        key = (state["bot_id"], state["group_id"])
        assert (self.rows.get(key) or {}).get("version") == expected
        self.rows[key] = deepcopy(state)


async def setup():
    # Synthetic strings isolate mechanics from the licensed production corpus.
    bank = idioms.IdiomBank(["春" + chr(0x4e10+i) + "花" + chr(0x4f10+i) for i in range(80)], rng=random.Random(7))
    now, store = [1000.0], Store()
    service = GameService(store, clock=lambda: now[0], idiom_bank=bank)
    await service.initialize()
    return service, store, now


def stamp(service):
    value = service.states.get((900, 100)) or {}
    return {"session_id": value.get("session_id"), "version": value.get("version"),
            "question_id": (value.get("question") or {}).get("id"), "received_at": service.clock()}


async def run(service, action, *, mode="solo", uid=11, mid=1, argument="", arrival=None, admin=False):
    return await service.execute(900, 100, uid, f"user{uid}",
        Command(action, game="idiom" if action == "start" else "", mode=mode, argument=argument),
        stamp(service) if arrival is None else arrival, message_id=mid, is_admin=admin,
        persona={"id": "rin", "name": "凛"})


async def present(service):
    value = service.states[(900, 100)]
    async with service.locks[(900, 100)]:
        await service.presented_locked((900, 100), value["session_id"], value["question"]["id"], service.clock())


def answer(service):
    return service.states[(900, 100)]["question"]["answers"][0]


def test_solo_ten_correct_and_clock_begins_only_after_send():
    async def scenario():
        service, store, now = await setup()
        await run(service, "start")
        assert service.states[(900, 100)]["started_at"] is None
        now[0] += 30
        assert await service.expire() == []
        await present(service)
        started = service.states[(900, 100)]["started_at"]
        for number in range(10):
            now[0] += 2
            result = await run(service, "answer", argument=answer(service), mid=number+2)
            service.validate(result.state)
            if number < 9:
                assert result.state["question_started_at"] is None
                await present(service)
        final = store.rows[(900, 100)]
        assert final["correct_count"] == 10 and final["winner_id"] == 11
        assert final["finished_at"] - started == 20
        assert final["status"] == "ended" and final["end_reason"] == "completed"
    asyncio.run(scenario())


def test_skips_and_timeouts_are_not_correct_or_a_win():
    async def scenario():
        service, _, now = await setup()
        await run(service, "start")
        await present(service)
        for number in range(9):
            await run(service, "skip", mid=number+2)
            await present(service)
        now[0] += 61
        results = await service.expire()
        assert len(results) == 1
        hinted = results[0].state
        assert hinted["question_no"] == 10 and hinted["status"] == "active"
        assert hinted["hint_mask"].count("□") == 1
        assert (hinted["correct_count"], hinted["skipped_count"], hinted["timed_out_count"]) == (0, 9, 0)
        assert await service.expire() == []
        now[0] = hinted["deadline"] + 1
        results = await service.expire()
        assert len(results) == 1
        final = results[0].state
        assert (final["correct_count"], final["skipped_count"], final["timed_out_count"]) == (0, 9, 1)
        assert final["winner_id"] == 0 and final["status"] == "ended"
        assert await service.expire() == []
    asyncio.run(scenario())


def test_wrong_answer_does_not_invalidate_same_question_and_duplicate_is_silent():
    async def scenario():
        service, _, _ = await setup()
        await run(service, "start", mode="race")
        await present(service)
        arrival = stamp(service)
        target = answer(service)
        wrong = await run(service, "answer", uid=22, argument="乱写答案", mid=2, arrival=arrival)
        assert not wrong.silent and wrong.state is None and "答错" in wrong.message
        assert service.states[(900, 100)]["version"] != arrival["version"]
        correct = await run(service, "answer", uid=33, argument=target, mid=3, arrival=arrival)
        assert correct.state["scores"]["33"] == 1
        late = await run(service, "answer", uid=22, argument=target, mid=4, arrival=arrival)
        assert late.silent
        assert (await run(service, "answer", uid=33, argument=target, mid=3)).silent
        assert correct.state["correct_count"] == 1
    asyncio.run(scenario())


def test_race_first_correct_wins_question_and_ten_points_wins_game():
    async def scenario():
        service, _, now = await setup()
        await run(service, "start", mode="race")
        await present(service)
        for number in range(10):
            now[0] += 1
            arrival, target = stamp(service), answer(service)
            results = await asyncio.gather(
                run(service, "answer", uid=22, argument=target, mid=10+number*2, arrival=arrival),
                run(service, "answer", uid=33, argument=target, mid=11+number*2, arrival=arrival))
            assert results[1].silent
            if number < 9:
                await present(service)
        final = service.states[(900, 100)]
        assert final["winner_id"] == 22 and final["scores"]["22"] == 10
        assert "33" not in final["scores"]
        assert final["end_reason"] == "win"
    asyncio.run(scenario())


def test_multi_answer_question_accepts_alternatives_and_normalises_punctuation():
    async def scenario():
        service, _, _ = await setup()
        service.idiom_bank = idioms.IdiomBank(["有声有色", "有声有势", "春暖花开"])
        await run(service, "start", mode="race")
        current = service.states[(900, 100)]
        current["question"].update(mask="有声有□", word="有声有色", answers=["有声有色", "有声有势"])
        await present(service)
        result = await run(service, "answer", argument="「有声有势！」", mid=2)
        assert result.state["correct_count"] == 1
    asyncio.run(scenario())


def test_permissions_and_chess_share_a_single_group_slot():
    async def scenario():
        service, _, _ = await setup()
        await run(service, "start")
        await present(service)
        with pytest.raises(GameError):
            await run(service, "answer", uid=22, mid=2, argument=answer(service))
        with pytest.raises(GameError):
            await service.execute(900, 100, 22, "other", Command("start", "gomoku"), stamp(service), message_id=3)
        await run(service, "cancel", uid=22, admin=True, mid=4)
        await run(service, "start", mode="race", mid=5)
        await present(service)
        with pytest.raises(GameError):
            await run(service, "skip", mid=6)
        await run(service, "answer", uid=22, argument=answer(service), mid=7)
        with pytest.raises(GameError):
            await run(service, "cancel", uid=22, mid=8)
        await run(service, "cancel", mid=9)
        assert service.states[(900, 100)]["status"] == "ended"
    asyncio.run(scenario())


def test_failed_storage_cannot_award_a_point_or_confirm_a_timer():
    async def scenario():
        service, store, _ = await setup()
        await run(service, "start")
        store.fail = True
        with pytest.raises(OSError):
            await present(service)
        assert service.states[(900, 100)]["started_at"] is None
        store.fail = False
        await present(service)
        before = deepcopy(service.states[(900, 100)])
        store.fail = True
        with pytest.raises(OSError):
            await run(service, "answer", argument=answer(service), mid=2)
        assert service.states[(900, 100)] == before
    asyncio.run(scenario())


def test_restart_persists_one_hint_without_changing_question_and_final_expiry_is_silent():
    async def scenario():
        service, store, now = await setup()
        await run(service, "start", mode="race")
        await present(service)
        now[0] += 85
        restored = GameService(store, clock=lambda: now[0], idiom_bank=service.idiom_bank)
        await restored.initialize()
        state = restored.states[(900, 100)]
        assert state["question_no"] == 1 and state["timed_out_count"] == 0
        assert state["question_started_at"] == 1000
        assert state["hint_mask"].count("□") == 1
        assert state["hinted_at"] == now[0]
        assert state["question_deadline"] is None
        assert state["expires_at"] == state["deadline"]
        assert await restored.expire() == []
        snapshot = deepcopy(state)
        now[0] += 100
        await restored.initialize()
        await present(restored)
        assert restored.states[(900, 100)] == snapshot
        assert store.rows[(900, 100)] == snapshot
        assert await restored.expire() == []
        now[0] += 1000
        await restored.initialize()
        assert restored.states[(900, 100)]["end_reason"] == "deadline"
        assert restored.states[(900, 100)]["timed_out_count"] == 1
        assert await restored.expire() == []
    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["solo", "race"])
def test_new_sessions_create_two_blank_question_snapshots(mode):
    async def scenario():
        service, _, _ = await setup()
        result = await run(service, "start", mode=mode)
        for question in [result.state["question"], *result.state["remaining_questions"]]:
            assert question["mask"].count("□") == 2
        service.validate(result.state)
    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["solo", "race"])
def test_old_one_blank_sessions_restore_without_rewriting_saved_questions(mode):
    async def scenario():
        service, store, now = await setup()
        await run(service, "start", mode=mode)
        await present(service)
        legacy = deepcopy(store.rows[(900, 100)])
        for question in [legacy["question"], *legacy["remaining_questions"]]:
            word = question["word"]
            question.update(mask="□" + word[1:], answers=[word])
        legacy["used_masks"] = [legacy["question"]["mask"]]
        legacy["used_words"] = list(legacy["question"]["answers"])
        legacy.pop("question_accept_after", None)
        store.rows[(900, 100)] = deepcopy(legacy)
        restored = GameService(store, clock=lambda: now[0], idiom_bank=service.idiom_bank)
        await restored.initialize()
        assert restored.states[(900, 100)] == legacy
        now[0] += 1
        result = await run(restored, "answer", argument=legacy["question"]["word"], mid=2)
        assert result.state["correct_count"] == 1
        # The old solo queue is a snapshot; race generates its next question anew.
        assert result.state["question"]["mask"].count("□") == (1 if mode == "solo" else 2)
        restored.validate(result.state)
    asyncio.run(scenario())


@pytest.mark.parametrize("holes", [0, 3, 4])
def test_recovery_rejects_zero_or_excessive_blank_counts(holes):
    async def scenario():
        service, _, _ = await setup()
        result = await run(service, "start", mode="race")
        question = result.state["question"]
        question.update(mask="□" * holes + question["word"][holes:], answers=[question["word"]])
        with pytest.raises(ValueError, match="Invalid idiom question"):
            service.validate(result.state)
    asyncio.run(scenario())


def test_query_does_not_extend_deadline_and_new_question_rejects_unsent_guesses():
    async def scenario():
        service, _, now = await setup()
        await run(service, "start")
        value = await run(service, "answer", argument=answer(service), mid=2)
        assert value.state is None
        await present(service)
        before = service.states[(900, 100)]["question_deadline"]
        now[0] += 5
        await run(service, "question", mid=3)
        await present(service)
        assert service.states[(900, 100)]["question_deadline"] == before
    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["question", "view"])
def test_query_after_deadline_cannot_discard_pending_on_time_answer(action):
    async def scenario():
        service, _, now = await setup()
        await run(service, "start", mode="race")
        await present(service)
        now[0] = service.states[(900, 100)]["deadline"] - 1
        arrival, target = stamp(service), answer(service)
        before = deepcopy(service.states[(900, 100)])
        now[0] += 2
        query = await run(service, action, uid=33, mid=3)
        assert query.state == before
        assert service.states[(900, 100)] == before
        result = await run(service, "answer", uid=22, mid=2, argument=target, arrival=arrival)
        assert result.state["scores"]["22"] == 1
        assert result.state["timed_out_count"] == 0
    asyncio.run(scenario())


def test_late_skip_cannot_discard_pending_on_time_solo_answer():
    async def scenario():
        service, _, now = await setup()
        await run(service, "start")
        await present(service)
        now[0] = service.states[(900, 100)]["deadline"] - 1
        arrival, target = stamp(service), answer(service)
        before = deepcopy(service.states[(900, 100)])
        now[0] += 2
        skipped = await run(service, "skip", mid=3)
        assert skipped.silent and service.states[(900, 100)] == before
        result = await run(service, "answer", mid=2, argument=target, arrival=arrival)
        assert result.state["correct_count"] == 1
        assert result.state["skipped_count"] == result.state["timed_out_count"] == 0
    asyncio.run(scenario())


@pytest.mark.parametrize("game", ["gomoku", "idiom"])
def test_new_start_cannot_discard_pending_on_time_idiom_answer(game):
    async def scenario():
        service, _, now = await setup()
        await run(service, "start", mode="race")
        await present(service)
        now[0] = service.states[(900, 100)]["deadline"] - 1
        arrival, target = stamp(service), answer(service)
        before = deepcopy(service.states[(900, 100)])
        now[0] += 2
        with pytest.raises(GameError):
            await service.execute(900, 100, 33, "user33", Command("start", game),
                                  stamp(service), message_id=3)
        assert service.states[(900, 100)] == before
        result = await run(service, "answer", uid=22, mid=2, argument=target, arrival=arrival)
        assert result.state["scores"]["22"] == 1
        assert result.state["timed_out_count"] == 0
    asyncio.run(scenario())


def test_read_only_late_query_does_not_prevent_one_hint_from_normal_ticker():
    async def scenario():
        service, _, now = await setup()
        await run(service, "start", mode="race")
        await present(service)
        now[0] += 61
        await run(service, "question", mid=2)
        assert service.states[(900, 100)]["question_no"] == 1
        updates = await service.expire(pending_answer=lambda *_: False)
        assert len(updates) == 1
        state = updates[0].state
        assert state["question_no"] == 1
        assert state["timed_out_count"] == 0
        assert state["hint_mask"].count("□") == 1
        assert state["question_deadline"] is None
        assert state["expires_at"] == state["deadline"]
        assert await service.expire() == []
    asyncio.run(scenario())


@pytest.mark.parametrize("boundary", ["question_deadline", "deadline"])
def test_received_on_time_answer_is_not_penalised_for_processing_delay(boundary):
    async def scenario():
        service, _, now = await setup()
        await run(service, "start", mode="race")
        await present(service)
        now[0] = service.states[(900, 100)][boundary] - 1
        arrival = stamp(service)
        now[0] += 2
        result = await run(service, "answer", argument=answer(service), mid=2, arrival=arrival)
        assert result.state["correct_count"] == 1 and result.state["timed_out_count"] == 0
    asyncio.run(scenario())


def test_disabled_idiom_feature_does_not_disable_chess_or_cancel():
    async def scenario():
        service, _, _ = await setup()
        await run(service, "start")
        service.idiom_enabled = False
        with pytest.raises(GameError):
            await run(service, "question", mid=2)
        await run(service, "cancel", mid=3)
        with pytest.raises(GameError):
            await run(service, "start", mid=4)
        result = await service.execute(900, 100, 11, "player", Command("start", "gomoku"), stamp(service), message_id=5)
        assert result.state["game"] == "gomoku"
    asyncio.run(scenario())


def test_successful_send_accepts_inflight_answer_but_not_pre_send_guess():
    async def scenario():
        service, _, now = await setup()
        await run(service, "start", mode="race")
        early = stamp(service)
        now[0] += 2
        in_flight = stamp(service)
        word = answer(service)
        now[0] += 1
        current = service.states[(900, 100)]
        async with service.locks[(900, 100)]:
            await service.presented_locked((900, 100), current["session_id"], current["question"]["id"],
                                           now[0], send_started_at=1001)
        shown = service.states[(900, 100)]
        assert shown["question_started_at"] == 1003
        assert shown["question_accept_after"] == 1001
        assert shown["question_deadline"] == 1063
        service.validate(shown)
        rejected = await run(service, "answer", uid=22, mid=2, argument=word, arrival=early)
        assert not rejected.silent and rejected.state is None and "尚未成功发送" in rejected.message
        assert service.states[(900, 100)]["correct_count"] == 0
        result = await run(service, "answer", uid=33, mid=3, argument=word, arrival=in_flight)
        assert result.state["scores"]["33"] == 1 and "22" not in result.state["scores"]
        assert result.state["question_accept_after"] is None
    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["solo", "race"])
def test_answer_received_at_second_sixty_one_is_scored_not_consumed_by_due_hint(mode):
    async def scenario():
        service, store, now = await setup()
        await run(service, "start", mode=mode)
        await present(service)
        old = deepcopy(service.states[(900, 100)])
        target = answer(service)
        now[0] = old["question_started_at"] + 61
        result = await run(service, "answer", argument=target, mid=2)
        assert result.state["correct_count"] == 1
        assert result.state["scores"]["11"] == 1
        assert result.state["timed_out_count"] == 0
        assert result.state["question_no"] == 2
        assert result.state["question"]["id"] != old["question"]["id"]
        assert result.state["last_result"]["kind"] == "correct"
        assert result.state["hint_mask"] is result.state["hinted_at"] is None
        assert store.rows[(900, 100)] == result.state
        service.validate(result.state)
    asyncio.run(scenario())


def test_hint_does_not_narrow_original_multi_answer_snapshot_or_stale_version_admission():
    async def scenario():
        service, store, now = await setup()
        await run(service, "start", mode="race")
        current = service.states[(900, 100)]
        current["question"].update(mask="春□花□", word="春暖花开", answers=["春暖花开", "春色花香"])
        await present(service)
        original = deepcopy(service.states[(900, 100)]["question"])
        now[0] += 61
        arrival = stamp(service)
        hints = await service.expire()
        assert len(hints) == 1
        hinted = hints[0].state
        assert hinted["question"] == original
        assert hinted["version"] != arrival["version"]
        assert hinted["hint_mask"].count("□") == 1
        # The alternative deliberately differs at both newly revealable positions.
        alternative = original["answers"][1]
        assert any(h != "□" and h != a for h, a in zip(hinted["hint_mask"], alternative))
        now[0] += 1
        result = await run(service, "answer", uid=22, argument=alternative, mid=2, arrival=arrival)
        assert result.state["scores"]["22"] == 1
        assert result.state["correct_count"] == 1 and result.state["timed_out_count"] == 0
        assert result.state["last_result"]["answers"] == original["answers"]
        assert store.rows[(900, 100)] == result.state
    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["solo", "race"])
def test_failed_hint_storage_changes_neither_memory_nor_durable_question(mode):
    async def scenario():
        service, store, now = await setup()
        await run(service, "start", mode=mode)
        await present(service)
        before = deepcopy(service.states[(900, 100)])
        now[0] += 61
        store.fail = True
        with pytest.raises(OSError, match="simulated storage failure"):
            await service.expire()
        assert service.states[(900, 100)] == before
        assert store.rows[(900, 100)] == before
        assert service.ready
        store.fail = False
        updates = await service.expire()
        assert len(updates) == 1
        assert updates[0].state["hint_mask"].count("□") == 1
        assert updates[0].state["question"] == before["question"]
        assert updates[0].state["version"] == before["version"] + 1
        assert await service.expire() == []
    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["solo", "race"])
def test_saved_hint_survives_restart_and_does_not_produce_another_send_result(mode):
    async def scenario():
        service, store, now = await setup()
        await run(service, "start", mode=mode)
        await present(service)
        now[0] += 61
        updates = await service.expire()
        assert len(updates) == 1
        before = deepcopy(store.rows[(900, 100)])
        now[0] += 100
        restored = GameService(store, clock=lambda: now[0], idiom_bank=service.idiom_bank)
        await restored.initialize()
        assert restored.states[(900, 100)] == before
        assert await restored.expire() == []
        query = await run(restored, "question", mid=2)
        assert query.state == before
        await present(restored)
        assert restored.states[(900, 100)] == store.rows[(900, 100)] == before
        assert await restored.expire() == []
    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["solo", "race"])
@pytest.mark.parametrize("hinted", [False, True])
def test_bare_wrong_answer_is_silent_and_only_deduplication_metadata_changes(mode, hinted):
    async def scenario():
        service, store, now = await setup()
        await run(service, "start", mode=mode)
        await present(service)
        if hinted:
            now[0] += 61
            assert len(await service.expire()) == 1
        before = deepcopy(service.states[(900, 100)])
        now[0] += 1
        arrival = dict(stamp(service), bare_idiom=True)
        result = await run(service, "answer", argument="乱写答案", mid=2, arrival=arrival)
        assert result.silent and result.state is None
        after = deepcopy(service.states[(900, 100)])
        metadata = {"seen", "version", "updated_at"}
        assert {k: v for k, v in after.items() if k not in metadata} == {k: v for k, v in before.items() if k not in metadata}
        assert after["seen"] == before["seen"] + ["2"]
        assert after["version"] == before["version"] + 1
        assert after["updated_at"] == now[0]
        assert store.rows[(900, 100)] == after
        duplicate = await run(service, "answer", argument="乱写答案", mid=2, arrival=arrival)
        assert duplicate.silent and service.states[(900, 100)] == after
        service.validate(after)
    asyncio.run(scenario())


def test_whole_deadline_ticker_defers_to_pending_on_time_answer_after_hint():
    async def scenario():
        service, _, now = await setup()
        await run(service, "start", mode="race")
        await present(service)
        now[0] += 61
        await service.expire()
        now[0] = service.states[(900, 100)]["deadline"] - 1
        arrival, target = stamp(service), answer(service)
        before = deepcopy(service.states[(900, 100)])
        now[0] += 2
        assert await service.expire(pending_answer=lambda *_: True) == []
        assert service.states[(900, 100)] == before
        result = await run(service, "answer", uid=22, mid=2, argument=target, arrival=arrival)
        assert result.state["scores"]["22"] == 1
        assert result.state["timed_out_count"] == 0
    asyncio.run(scenario())
