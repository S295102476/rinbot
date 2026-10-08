"""Pure idiom-session transitions; transport, persistence and locks live outside."""
from copy import deepcopy
import math
import random
import re
import uuid

from .idioms import normalise_answer


QUESTION_SECONDS = 60
SOLO_SECONDS = 600
RACE_SECONDS = 900


def _question(value):
    result = deepcopy(value)
    result["id"] = uuid.uuid4().hex
    return result


def _remember(state, question):
    state["used_words"] = sorted(set(state["used_words"]) | set(question["answers"]))
    state["used_masks"].append(question["mask"])


def create(bank, bot_id, group_id, user_id, name, mode, now, persona=None, seen=()):
    if mode not in {"solo", "race"} or bank is None:
        raise ValueError("成语题库暂不可用，请稍后再试")
    questions = bank.questions(10 if mode == "solo" else 1)
    persona = persona or {}
    state = {"schema": 2, "game": "idiom", "session_id": uuid.uuid4().hex,
             "version": 0, "bot_id": int(bot_id), "group_id": int(group_id),
             "mode": mode, "status": "active", "host_id": int(user_id),
             "players": {str(user_id): {"id": int(user_id), "name": name[:40]}},
             "scores": {str(user_id): 0}, "persona_id": persona.get("id", "rin"),
             "bot_name": persona.get("name", "机器人"), "created_at": now,
             "expires_at": now + SOLO_SECONDS, "started_at": None, "deadline": None,
             "question": _question(questions[0]), "question_no": 1,
             "question_limit": 10 if mode == "solo" else 0,
             "remaining_questions": questions[1:], "question_started_at": None,
             "question_accept_after": None,
             "question_deadline": None, "hint_mask": None, "hinted_at": None,
             "correct_count": 0, "skipped_count": 0,
             "timed_out_count": 0, "used_words": [], "used_masks": [],
             "last_result": None, "winner_id": 0, "end_reason": "",
             "finished_at": None, "seen": list(seen)[-512:]}
    _remember(state, state["question"])
    return state


def finish(state, reason, now, winner_id=0):
    state.update(status="ended", end_reason=reason, winner_id=winner_id,
                 finished_at=now, expires_at=now)


def mark_presented(state, now, *, send_started_at=None):
    """Only called once QQ has acknowledged sending this exact question."""
    if state["status"] != "active" or state["question_started_at"] is not None:
        return False
    if state["deadline"] is not None and now >= state["deadline"]:
        finish(state, "deadline", now)
        return True
    if state["started_at"] is None:
        state["started_at"] = now
        state["deadline"] = now + (SOLO_SECONDS if state["mode"] == "solo" else RACE_SECONDS)
    state["question_started_at"] = now
    # The image may be visible before OneBot returns its acknowledgement.
    # Accept ingress during this successful send, but not pre-send guesses.
    # Timers still begin only after acknowledgement.
    state["question_accept_after"] = min(now, send_started_at) if send_started_at is not None else now
    state["question_deadline"] = min(now + QUESTION_SECONDS, state["deadline"])
    state["expires_at"] = state["question_deadline"]
    return True


def _advance(state, bank, now):
    if state["mode"] == "solo" and state["question_no"] >= 10:
        finish(state, "completed", now, state["host_id"] if state["correct_count"] == 10 else 0)
        return
    if state["deadline"] is not None and now >= state["deadline"]:
        finish(state, "deadline", now)
        return
    if state["mode"] == "solo":
        question = state["remaining_questions"].pop(0)
    else:
        if bank is None:
            finish(state, "bank_unavailable", now)
            return
        try:
            question = bank.next_question(excluded_words=state["used_words"],
                                         excluded_masks=state["used_masks"])
        except ValueError:
            finish(state, "pool_exhausted", now)
            return
    state.update(question=_question(question), question_no=state["question_no"] + 1,
                 question_started_at=None, question_accept_after=None, question_deadline=None,
                 hint_mask=None, hinted_at=None,
                 expires_at=state["deadline"] or now + SOLO_SECONDS)
    _remember(state, state["question"])


def resolve(state, kind, bank, now, user_id=0, name=""):
    question = state["question"]
    state["last_result"] = {"kind": kind, "question_no": state["question_no"],
                            "user_id": user_id, "name": name[:40],
                            "answers": list(question["answers"])}
    if kind == "correct":
        uid = str(user_id)
        state["players"][uid] = {"id": user_id, "name": name[:40]}
        state["scores"][uid] = state["scores"].get(uid, 0) + 1
        state["correct_count"] += 1
        if state["mode"] == "race" and state["scores"][uid] >= 10:
            finish(state, "win", now, user_id)
            return
    elif kind == "skipped":
        state["skipped_count"] += 1
    elif kind == "timeout":
        state["timed_out_count"] += 1
    else:
        raise ValueError("Invalid idiom resolution")
    _advance(state, bank, now)


def expire(state, bank, now):
    """Reveal one character once at 60s; only the whole-game limit ends a turn.

    The original mask/answer snapshot remains the scoring contract. A hint is
    display-only so it cannot invalidate an alternate answer already in flight.
    Callers commit this transition, but must not treat a hint as a solved turn.
    """
    if state["status"] != "active":
        return False
    deadline = state.get("deadline")
    if deadline is not None and now >= deadline:
        # Count/reveal only a question which players actually received. If a
        # send failed after the previous answer, do not penalise an unseen one.
        if state.get("question_started_at") is not None:
            state["last_result"] = {"kind": "timeout", "question_no": state["question_no"],
                                    "user_id": 0, "name": "",
                                    "answers": list(state["question"]["answers"])}
            state["timed_out_count"] += 1
        finish(state, "deadline", now)
        return True
    if state["question_deadline"] is not None and now >= state["question_deadline"]:
        original = state["question"]["mask"]
        holes = [index for index, char in enumerate(original) if char == "□"]
        hint = original
        if len(holes) == 2:
            reveal = random.choice(holes)
            hint = original[:reveal] + state["question"]["word"][reveal] + original[reveal + 1:]
        # A recovered one-blank question consumes its old 60s timer without
        # revealing the whole answer. It likewise stays until solved/skipped.
        state.update(hint_mask=hint, hinted_at=now, question_deadline=None,
                     expires_at=deadline)
        return True
    if now >= state["expires_at"]:
        finish(state, "deadline" if state["started_at"] is not None else "undelivered", now)
        return True
    return False


def validate(row):
    """Fail closed on corrupt recovery records, without changing old board schema."""
    if row.get("schema") != 2 or row.get("game") != "idiom":
        raise ValueError("Invalid idiom schema")
    if row.get("status") not in {"active", "ended"} or row.get("mode") not in {"solo", "race"}:
        raise ValueError("Invalid idiom mode/status")
    if not row.get("session_id") or type(row.get("version")) is not int or row["version"] < 1:
        raise ValueError("Invalid idiom identity/version")
    for key in ("created_at", "expires_at", "started_at", "deadline", "question_started_at", "question_accept_after", "question_deadline", "hinted_at", "finished_at"):
        value = row.get(key)
        if value is None and key not in {"created_at", "expires_at"}:
            continue
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            raise ValueError("Invalid idiom timestamp")
    players, scores = row.get("players", {}), row.get("scores", {})
    host = row.get("host_id")
    if type(host) is not int or host <= 0 or str(host) not in players or set(scores) != set(players):
        raise ValueError("Invalid idiom players")
    for uid, player in players.items():
        if not uid.isdigit() or int(uid) <= 0 or player.get("id") != int(uid) or int(uid) == row["bot_id"]:
            raise ValueError("Invalid idiom player")
        if type(scores[uid]) is not int or not 0 <= scores[uid] <= 10:
            raise ValueError("Invalid idiom score")
    if row["mode"] == "solo" and set(players) != {str(host)}:
        raise ValueError("Invalid solo participants")
    for key in ("correct_count", "skipped_count", "timed_out_count"):
        if type(row.get(key)) is not int or row[key] < 0:
            raise ValueError("Invalid idiom counters")
    if sum(scores.values()) != row["correct_count"]:
        raise ValueError("Inconsistent idiom score")
    number = row.get("question_no")
    if type(number) is not int or number < 1 or (row["mode"] == "solo" and number > 10):
        raise ValueError("Invalid question number")
    question = row.get("question", {})
    if not question.get("id"):
        raise ValueError("Missing question identity")
    pending = row.get("remaining_questions", [])
    if not isinstance(pending, list) or len(pending) > 9:
        raise ValueError("Invalid idiom queue")
    if row["mode"] == "solo" and len(pending) != 10 - number:
        raise ValueError("Incomplete solo question snapshot")
    if row["mode"] == "race" and pending:
        raise ValueError("Invalid race question queue")
    resolved = row["correct_count"] + row["skipped_count"] + row["timed_out_count"]
    if row["status"] == "active" and resolved != number - 1:
        raise ValueError("Inconsistent question progress")
    if not isinstance(row.get("used_words"), list) or not isinstance(row.get("used_masks"), list):
        raise ValueError("Missing question exclusion snapshot")
    for current in [question, *pending]:
        mask, answers = current.get("mask", ""), current.get("answers", [])
        # Existing one-blank question/queue snapshots retain their old rules;
        # only newly generated questions use two blanks. No migration needed.
        if len(mask) != 4 or mask.count("□") not in {1, 2} or not isinstance(answers, list) or not answers:
            raise ValueError("Invalid idiom question")
        if current.get("word") not in answers or len(answers) != len(set(answers)):
            raise ValueError("Invalid idiom answers")
        for answer in answers:
            if not isinstance(answer, str) or not re.fullmatch(r"[一-鿿]{4}", answer):
                raise ValueError("Invalid idiom answer")
            if any(a != "□" and a != b for a, b in zip(mask, answer)):
                raise ValueError("Answer does not fit mask")
    if row.get("question_started_at") is not None and (row.get("started_at") is None or row.get("deadline") is None):
        raise ValueError("Presented question lacks a whole-game timer")
    hinted_at, hint_mask = row.get("hinted_at"), row.get("hint_mask")
    if (hinted_at is None) != (hint_mask is None):
        raise ValueError("Incomplete idiom hint snapshot")
    if hinted_at is not None:
        shown = row.get("question_started_at")
        original, word = question["mask"], question["word"]
        if (shown is None or hinted_at < shown or row.get("question_deadline") is not None
                or not isinstance(hint_mask, str) or len(hint_mask) != 4 or hint_mask.count("□") != 1):
            raise ValueError("Invalid idiom hint state")
        if any(char != "□" and hint_mask[index] != char for index, char in enumerate(original)):
            raise ValueError("Hint overwrites a visible character")
        if any(char != "□" and char != word[index] for index, char in enumerate(hint_mask)):
            raise ValueError("Hint is inconsistent with the source word")
        if original.count("□") == 1 and hint_mask != original:
            raise ValueError("Legacy one-blank hint reveals the answer")
        if row.get("deadline") is None or (row["status"] == "active" and row["expires_at"] != row["deadline"]):
            raise ValueError("Hint must retain the whole-game deadline")
    elif (row.get("question_started_at") is None) != (row.get("question_deadline") is None):
        raise ValueError("Inconsistent presentation state")
    accept_after = row.get("question_accept_after")
    if accept_after is not None and (row.get("question_started_at") is None or accept_after > row["question_started_at"]):
        raise ValueError("Invalid answer ingress boundary")
    if row["status"] == "active" and any(value >= 10 for value in scores.values()):
        raise ValueError("Winning game still active")
