"""Pure 60-second hints: no transport, timer loop, database or model calls."""
from copy import deepcopy
import importlib
import json
from pathlib import Path
import random
import sys
import types

import pytest

package = types.ModuleType("_minigame_idiom_hint_tests")
package.__path__ = [str(Path(__file__).parents[1] / "plugins/minigames")]
sys.modules[package.__name__] = package
session = importlib.import_module(package.__name__ + ".idiom_session")
IdiomBank = importlib.import_module(package.__name__ + ".idioms").IdiomBank


def started(mode="solo"):
    bank = IdiomBank(["春" + chr(0x4e10 + i) + "花" + chr(0x4f10 + i) for i in range(80)],
                     rng=random.Random(6))
    state = session.create(bank, 900, 100, 11, "玩家", mode, 1000)
    state["version"] = 1
    assert session.mark_presented(state, 1000)
    session.validate(state)
    return bank, state


@pytest.mark.parametrize("mode", ["solo", "race"])
def test_sixty_seconds_reveals_one_char_once_without_resolving_question(mode):
    bank, state = started(mode)
    before = deepcopy(state)
    assert not session.expire(state, bank, 1059.999)
    assert state == before
    assert session.expire(state, bank, 1060)
    assert state["hint_mask"].count("□") == 1
    assert sum(a != b for a, b in zip(before["question"]["mask"], state["hint_mask"])) == 1
    assert state["question"] == before["question"]
    assert state["question_no"] == 1
    assert state["scores"] == before["scores"]
    assert state["correct_count"] == state["skipped_count"] == state["timed_out_count"] == 0
    assert state["last_result"] is None
    assert state["remaining_questions"] == before["remaining_questions"]
    assert state["used_words"] == before["used_words"]
    assert state["used_masks"] == before["used_masks"]
    assert state["hinted_at"] == 1060
    assert state["question_deadline"] is None
    assert state["expires_at"] == state["deadline"]
    after = deepcopy(state)
    assert not session.expire(state, bank, 1120)
    assert not session.expire(state, bank, 1250)
    assert not session.mark_presented(state, 1250)
    assert state == after
    session.validate(state)


def test_hint_preserves_every_original_multi_answer_even_if_hint_points_to_main_word(monkeypatch):
    bank, state = started("race")
    state["question"].update(mask="春□花□", word="春暖花开", answers=["春暖花开", "春色花香"])
    monkeypatch.setattr(session.random, "choice", lambda holes: holes[0])
    question_id = state["question"]["id"]
    assert session.expire(state, bank, 1060)
    assert state["hint_mask"] == "春暖花□"
    assert state["question"]["mask"] == "春□花□"
    assert state["question"]["id"] == question_id
    assert state["question"]["answers"] == ["春暖花开", "春色花香"]
    session.validate(state)


@pytest.mark.parametrize("mode", ["solo", "race"])
def test_restart_preserves_consumed_hint_and_does_not_restart_sixty_second_timer(mode):
    bank, state = started(mode)
    session.expire(state, bank, 1060)
    restored = json.loads(json.dumps(state))
    session.validate(restored)
    assert not session.expire(restored, bank, 1300)
    assert not session.mark_presented(restored, 1300)
    assert restored == state


@pytest.mark.parametrize("mode", ["solo", "race"])
def test_legacy_single_blank_consumes_timer_without_revealing_complete_answer(mode):
    bank, state = started(mode)
    question = state["question"]
    question.update(mask="□" + question["word"][1:], answers=[question["word"]])
    state.pop("hint_mask")
    state.pop("hinted_at")
    before = deepcopy(question)
    session.validate(state)
    assert session.expire(state, bank, 1100)
    assert state["hint_mask"] == before["mask"]
    assert state["hint_mask"] != before["word"]
    assert state["question"] == before and state["question_no"] == 1
    assert state["question_deadline"] is None
    assert state["expires_at"] == state["deadline"]
    assert state["timed_out_count"] == 0
    assert not session.expire(state, bank, 1160)
    session.validate(state)


def test_old_two_blank_snapshot_without_hint_fields_gets_one_hint_after_restart():
    bank, state = started()
    state.pop("hint_mask")
    state.pop("hinted_at")
    session.validate(state)
    assert session.expire(state, bank, 1130)
    assert state["hint_mask"].count("□") == 1
    assert state["question_no"] == 1 and state["timed_out_count"] == 0
    session.validate(state)


@pytest.mark.parametrize("kind", ["correct", "skipped"])
def test_solving_or_skipping_after_hint_reveals_old_answer_and_resets_only_new_hint(kind):
    bank, state = started()
    session.expire(state, bank, 1060)
    old_question = deepcopy(state["question"])
    game_deadline = state["deadline"]
    session.resolve(state, kind, bank, 1100, user_id=11, name="玩家")
    assert state["last_result"]["answers"] == old_question["answers"]
    assert state["last_result"]["kind"] == kind
    assert state["last_result"]["question_no"] == 1
    assert state["question"]["id"] != old_question["id"]
    assert state["question_no"] == 2
    assert state["hint_mask"] is state["hinted_at"] is None
    assert state["question_started_at"] is state["question_deadline"] is None
    assert state["deadline"] == game_deadline
    assert session.mark_presented(state, 1120)
    assert state["question_deadline"] == 1180
    assert session.expire(state, bank, 1180)
    assert state["hinted_at"] == 1180 and state["question_no"] == 2
    session.validate(state)


@pytest.mark.parametrize("mode", ["solo", "race"])
@pytest.mark.parametrize("already_hinted", [False, True])
def test_whole_game_deadline_reveals_current_answers_once_and_does_not_generate_next(mode, already_hinted):
    bank, state = started(mode)
    original = deepcopy(state["question"])
    if already_hinted:
        session.expire(state, bank, 1060)
    deadline = state["deadline"]
    assert session.expire(state, bank, deadline)
    assert state["status"] == "ended" and state["end_reason"] == "deadline"
    assert state["question"] == original and state["question_no"] == 1
    assert state["timed_out_count"] == 1 and state["correct_count"] == 0
    assert state["last_result"]["kind"] == "timeout"
    assert state["last_result"]["answers"] == original["answers"]
    snapshot = deepcopy(state)
    assert not session.expire(state, bank, deadline + 100)
    assert state == snapshot
    session.validate(state)


def test_whole_game_deadline_takes_priority_when_hint_and_final_deadline_coincide():
    bank, state = started()
    session.resolve(state, "correct", bank, 1570, user_id=11, name="玩家")
    session.mark_presented(state, 1575)
    assert state["question_deadline"] == state["deadline"] == 1600
    assert session.expire(state, bank, 1600)
    assert state["status"] == "ended"
    assert state["hint_mask"] is None
    assert state["last_result"]["kind"] == "timeout"


def test_first_failed_send_expires_without_revealing_or_counting_an_unseen_question():
    bank, state = started()
    fresh = session.create(bank, 900, 100, 11, "玩家", "solo", 1000)
    fresh["version"] = 1
    assert not session.expire(fresh, bank, 1060)
    assert fresh["hint_mask"] is None and fresh["last_result"] is None
    assert session.expire(fresh, bank, 1600)
    assert fresh["end_reason"] == "undelivered"
    assert fresh["timed_out_count"] == 0 and fresh["last_result"] is None
    session.validate(fresh)


def test_failed_send_of_next_question_at_final_deadline_does_not_penalise_unseen_question():
    bank, state = started()
    session.resolve(state, "correct", bank, 1500, user_id=11, name="玩家")
    previous = deepcopy(state["last_result"])
    assert state["question_started_at"] is None
    assert session.expire(state, bank, 1600)
    assert state["status"] == "ended" and state["timed_out_count"] == 0
    assert state["last_result"] == previous
    session.validate(state)


@pytest.mark.parametrize("mutation", [
    lambda row: row.update(hint_mask=row["question"]["word"]),
    lambda row: row.update(hinted_at=None),
    lambda row: row.update(question_deadline=1180),
    lambda row: row.update(expires_at=1180),
    lambda row: row.update(hinted_at=999),
    lambda row: row.update(hint_mask="春暖花□"),
])
def test_recovery_rejects_corrupt_or_full_answer_hints(mutation):
    bank, state = started()
    session.expire(state, bank, 1060)
    mutation(state)
    with pytest.raises(ValueError):
        session.validate(state)
