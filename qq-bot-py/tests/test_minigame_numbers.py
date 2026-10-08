"""Pure 1A2B state, scoring and restart-validation tests; no QQ or DB."""
from copy import deepcopy
import importlib
import itertools
import json
from pathlib import Path
from random import SystemRandom
import sys
import types

import pytest

package = types.ModuleType("_minigame_number_tests")
package.__path__ = [str(Path(__file__).parents[1] / "plugins/minigames")]
sys.modules[package.__name__] = package
number = importlib.import_module(package.__name__ + ".number_session")


def state(mode="solo", presented=True):
    row = number.create(900, 100, 11, "host", mode, 1000)
    row["secret"] = "1023"
    row["version"] = 1
    if presented:
        number.mark_presented(row, 1005)
    return row


@pytest.mark.parametrize("value", ["1234", "1023", " 1023 ", "「1023」。", "（1023）！", "\n1023\t"])
def test_guess_normalisation(value):
    assert number.normalise_guess(value) == ("1234" if value == "1234" else "1023")


@pytest.mark.parametrize("value", [None, 1234, True, "", "0123", "1123", "0000", "123", "12345",
    "１２３４", "١٢٣٤", "123４", "1 234", "12,34", "1234abc", "1234😃", "#作答 1234",
    "一二三四", "12□4", "+1234", " " * 257])
def test_invalid_guess_is_never_extracted_or_repaired(value):
    with pytest.raises(ValueError):
        number.normalise_guess(value)


@pytest.mark.parametrize("secret,guess,expected", [("1234", "1234", (4,0)), ("1234", "4321", (0,4)),
    ("1234", "1243", (2,2)), ("1234", "5678", (0,0)), ("1234", "1023", (1,2)), ("1023", "1302", (1,3))])
def test_a_b_feedback(secret, guess, expected):
    assert number.score(secret, guess) == expected


def test_all_digit_permutations_score_consistently():
    for digits in itertools.permutations("1234"):
        guess = "".join(digits)
        a, b = number.score("1234", guess)
        assert a == sum(x == y for x, y in zip("1234", guess))
        assert a + b == 4


def test_secret_generation_is_system_random_unique_and_allows_nonleading_zero(monkeypatch):
    assert isinstance(number._RANDOM, SystemRandom)
    for _ in range(200):
        secret = number.create(900, 100, 11, "host", "solo", 1000)["secret"]
        assert number.normalise_guess(secret) == secret
    class Deterministic:
        def choice(self, choices):
            assert choices == "123456789"
            return "1"
        def sample(self, choices, count):
            assert "0" in choices and "1" not in choices and count == 3
            return ["0", "2", "3"]
    monkeypatch.setattr(number, "_RANDOM", Deterministic())
    assert number.create(900, 100, 11, "host", "race", 1000)["secret"] == "1023"


@pytest.mark.parametrize("mode,seconds", [("solo",600),("race",900)])
def test_first_successful_display_starts_one_nonrenewable_timer(mode, seconds):
    row = state(mode, presented=False)
    assert row["expires_at"] == 1600 and row["started_at"] is None
    number.validate(row)
    assert number.mark_presented(row, 1010, send_started_at=1008) is True
    assert row["started_at"] == 1010 and row["accept_after"] == 1008
    assert row["deadline"] == row["expires_at"] == 1010 + seconds
    before = deepcopy(row)
    assert number.mark_presented(row, 1030) is False
    assert row == before
    number.validate(row)


def test_send_failure_never_starts_timer_and_undelivered_game_expires_once():
    row = state(presented=False)
    before = deepcopy(row)
    with pytest.raises(ValueError, match="尚未成功发送"):
        number.submit(row, "1234", 11, "host", 1001)
    assert row == before
    assert not number.expire(row, 1599)
    assert number.expire(row, 1600)
    assert row["end_reason"] == "undelivered" and row["secret"] == before["secret"]
    assert not number.expire(row, 1700)
    number.validate(row)


def test_late_send_does_not_resurrect_expired_game():
    row = state(presented=False)
    assert number.mark_presented(row, 1600)
    assert row["status"] == "ended" and row["started_at"] is None
    number.validate(row)


def test_only_host_answers_solo_and_failures_do_not_mutate():
    row = state()
    for guess, user, now in [("1234",22,1006),("1234",900,1006),("1123",11,1006),
                             ("1234",11,1004),("1234",11,1605)]:
        before = deepcopy(row)
        with pytest.raises(ValueError):
            number.submit(row, guess, user, "player", now)
        assert row == before


def test_score_history_and_winner_snapshot_are_independent():
    row = state()
    original_secret, round_id = row["secret"], row["round_id"]
    feedback = number.submit(row, "1234", 11, "renamed", 1006)
    assert feedback == {"user_id":11,"name":"renamed","guess":"1234","a":1,"b":2,"number":1,"at":1006}
    feedback["a"] = 4
    assert row["attempts"][0]["a"] == 1
    assert row["attempt_counts"] == {"11":1}
    number.validate(row)
    number.submit(row, "1023", 11, "host", 1007)
    assert row["status"] == "ended" and row["winner_id"] == 11 and row["end_reason"] == "win"
    assert row["total_attempts"] == 2 and row["finished_at"] == 1007
    assert row["secret"] == original_secret and row["round_id"] == round_id
    number.validate(row)
    before = deepcopy(row)
    with pytest.raises(ValueError, match="已经结束"):
        number.submit(row, "1023", 11, "host", 1008)
    assert row == before


def test_race_allows_any_human_and_first_winner_ends_session():
    row = state("race")
    number.submit(row, "1234", 22, "second", 1006)
    number.submit(row, "1023", 33, "third", 1007)
    assert row["attempt_counts"] == {"11":0,"22":1,"33":1}
    assert row["winner_id"] == 33
    with pytest.raises(ValueError):
        number.submit(row, "1023", 22, "second", 1007)
    number.validate(row)


def test_history_bounded_at_twelve_without_limiting_total_attempts():
    row = state("race")
    for index in range(30):
        number.submit(row, "1234", 11 + index % 3, f"p{index}", 1006 + index)
    assert row["total_attempts"] == 30
    assert sum(row["attempt_counts"].values()) == 30
    assert len(row["attempts"]) == 12
    assert [item["number"] for item in row["attempts"]] == list(range(19,31))
    assert row["deadline"] == 1905  # Submissions do not renew the timer.
    number.validate(json.loads(json.dumps(row)))


def test_answer_during_successful_send_cannot_produce_negative_elapsed():
    row = state(presented=False)
    number.mark_presented(row, 1010, send_started_at=1008)
    number.submit(row, "1023", 11, "host", 1009)
    assert row["attempts"][-1]["at"] == 1009
    assert row["finished_at"] == row["started_at"] == 1010
    number.validate(row)


@pytest.mark.parametrize("mode", ["solo","race"])
def test_recovery_does_not_reset_secret_counts_or_deadline(mode):
    row = state(mode)
    number.submit(row, "1234", 11, "host", 1006)
    restored = json.loads(json.dumps(row))
    number.validate(restored)
    assert not number.mark_presented(restored, 1100)
    assert restored == row
    assert number.expire(restored, restored["deadline"])
    assert restored["end_reason"] == "deadline" and restored["winner_id"] == 0
    assert restored["secret"] == row["secret"] and restored["total_attempts"] == 1
    number.validate(restored)


def test_cancellation_and_finish_are_idempotent():
    for shown in (True, False):
        row = state(presented=shown)
        assert number.finish(row, "cancelled", 1010)
        number.validate(row)
        before = deepcopy(row)
        assert not number.finish(row, "cancelled", 1020)
        assert not number.mark_presented(row, 1020)
        assert row == before


@pytest.mark.parametrize("reason,when,winner", [("unknown",1010,0),("deadline",1010,0),
    ("undelivered",1700,0),("win",1010,11),("cancelled",1010,True),("cancelled",999,0)])
def test_invalid_finish_cannot_create_an_unrecoverable_state(reason,when,winner):
    row = state()
    before = deepcopy(row)
    with pytest.raises(ValueError):
        number.finish(row,reason,when,winner)
    assert row == before


@pytest.mark.parametrize("key,value", [("schema",3.0),("game","idiom"),("mode","bot"),("status","waiting"),
    ("version",0),("version",True),("session_id","bad"),("round_id","bad"),("host_id",900),("group_id",True),
    ("secret","my-secret-do-not-print"),("secret","1123"),("secret","0123"),
    ("expires_at",float("nan")),("created_at",float("inf")),("accept_after",1006),
    ("deadline",1606),("started_at",None),("total_attempts",True),("total_attempts",4),
    ("players",[]),("attempt_counts",{"11":2}),("attempts",[]),
    ("winner_id",11),("end_reason","win"),("finished_at",1006),("seen",[1])])
def test_corrupt_recovery_rejected_without_revealing_secret(key,value):
    row = state()
    number.submit(row, "1234", 11, "host", 1006)
    row[key] = value
    with pytest.raises(ValueError) as caught:
        number.validate(row)
    assert str(caught.value) == number._BAD_STATE
    assert "1023" not in str(caught.value) and "my-secret" not in str(caught.value)


@pytest.mark.parametrize("field,value", [("a",4),("b",0),("guess"," 1234"),("number",2),
    ("at",1605),("at",1004),("user_id",22),("name",None)])
def test_retained_history_feedback_and_identity_are_verified(field,value):
    row = state()
    number.submit(row, "1234", 11, "host", 1006)
    row["attempts"][0][field] = value
    with pytest.raises(ValueError):
        number.validate(row)


def test_corrupt_truncated_counts_or_sequence_rejected():
    row = state("race")
    for index in range(25):
        number.submit(row, "1234", 11, "host", 1006 + index)
    row["attempt_counts"] = {"11":10,"22":15}
    row["players"]["22"] = {"id":22,"name":"other"}
    with pytest.raises(ValueError):
        number.validate(row)
    row["attempt_counts"] = {"11":25}
    row["players"].pop("22")
    row["attempts"][0]["number"] = 1
    with pytest.raises(ValueError):
        number.validate(row)


def test_creation_trims_seen_and_nickname_without_shared_mutable_state():
    seen = [str(index) for index in range(520)]
    row = number.create(900,100,11,"x"*100,"solo",1000,seen=seen)
    assert row["seen"] == seen[-512:] and len(row["players"]["11"]["name"]) == 40
    row["seen"].append("new")
    assert len(seen) == 520


@pytest.mark.parametrize("data", [None,[],{}, {"schema":3}, {"schema":3,"game":"number","status":[]}])
def test_malformed_record_always_returns_a_sanitised_value_error(data):
    with pytest.raises(ValueError,match=number._BAD_STATE):
        number.validate(data)
