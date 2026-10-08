"""Pure, bounded-history 1A2B sessions; transport and storage live outside."""
import math
import re
from random import SystemRandom
import unicodedata
import uuid


SOLO_SECONDS = 600
RACE_SECONDS = 900
HISTORY_LIMIT = 12
_RANDOM = SystemRandom()
_DIGITS = "0123456789"
_NUMBER = re.compile(r"[1-9][0-9]{3}\Z")
_UUID = re.compile(r"[0-9a-f]{32}\Z")
_REASONS = {"win", "cancelled", "deadline", "undelivered"}
_BAD_GUESS = "请输入四位不重复的数字，首位不能为 0"
_BAD_STATE = "猜数字存档无效，请管理员检查"


def _valid_number(value):
    return isinstance(value, str) and _NUMBER.fullmatch(value) is not None and len(set(value)) == 4


def _time(value):
    return type(value) in (int, float) and value >= 0 and math.isfinite(value)


def _id(value):
    return type(value) is int and value > 0


def _name(value, user_id):
    if not isinstance(value, str):
        raise ValueError("玩家昵称无效")
    return value[:40] or str(user_id)[:40]


def normalise_guess(value):
    """Strip only surrounding whitespace/punctuation, never repair digits."""
    if not isinstance(value, str) or len(value) > 256:
        raise ValueError(_BAD_GUESS)
    start, end = 0, len(value)
    def peripheral(char):
        return char.isspace() or unicodedata.category(char).startswith("P")
    while start < end and peripheral(value[start]):
        start += 1
    while end > start and peripheral(value[end - 1]):
        end -= 1
    result = value[start:end]
    if not _valid_number(result):
        raise ValueError(_BAD_GUESS)
    return result


def score(secret, guess):
    if not _valid_number(secret) or not _valid_number(guess):
        # Do not include either value in error text, especially the secret.
        raise ValueError("猜数字评分数据无效")
    a = sum(left == right for left, right in zip(secret, guess))
    return a, len(set(secret) & set(guess)) - a


def create(bot_id, group_id, user_id, name, mode, now, persona=None, seen=()):
    if not isinstance(mode, str) or mode not in {"solo", "race"}:
        raise ValueError("猜数字模式无效")
    if not all(_id(value) for value in (bot_id, group_id, user_id)) or bot_id == user_id:
        raise ValueError("猜数字参与者无效")
    if not _time(now):
        raise ValueError("猜数字时间无效")
    if not isinstance(seen, (list, tuple)) or any(not isinstance(item, str) or not item or len(item) > 128 for item in seen[-512:]):
        raise ValueError("猜数字消息记录无效")
    persona = persona or {}
    if not isinstance(persona, dict):
        raise ValueError("猜数字角色信息无效")
    persona_id, bot_name = persona.get("id", "rin"), persona.get("name", "机器人")
    if not isinstance(persona_id, str) or not isinstance(bot_name, str):
        raise ValueError("猜数字角色信息无效")
    first = _RANDOM.choice(_DIGITS[1:])
    secret = first + "".join(_RANDOM.sample(_DIGITS.replace(first, ""), 3))
    return {"schema": 3, "game": "number", "session_id": uuid.uuid4().hex,
            "round_id": uuid.uuid4().hex, "version": 0, "bot_id": bot_id,
            "group_id": group_id, "host_id": user_id, "mode": mode,
            "status": "active", "secret": secret,
            "players": {str(user_id): {"id": user_id, "name": _name(name, user_id)}},
            "attempt_counts": {str(user_id): 0}, "attempts": [], "total_attempts": 0,
            "persona_id": persona_id[:80], "bot_name": bot_name[:80],
            "created_at": now, "expires_at": now + SOLO_SECONDS,
            "started_at": None, "accept_after": None, "deadline": None,
            "winner_id": 0, "end_reason": "", "finished_at": None,
            "seen": list(seen)[-512:]}


def finish(state, reason, now, winner_id=0):
    if state["status"] != "active":
        return False
    if not isinstance(reason, str) or reason not in _REASONS or not _time(now) or now < state["created_at"]:
        raise ValueError("猜数字结束参数无效")
    if type(winner_id) is not int or (reason == "win" and (not _id(winner_id) or str(winner_id) not in state["players"])) or (reason != "win" and winner_id != 0):
        raise ValueError("猜数字胜者无效")
    if reason == "win" and (not state["attempts"] or state["attempts"][-1]["a"] != 4 or state["attempts"][-1]["user_id"] != winner_id):
        raise ValueError("尚未产生猜数字胜者")
    if reason == "deadline" and (state["deadline"] is None or now < state["deadline"]):
        raise ValueError("猜数字尚未到期")
    if reason == "undelivered" and (state["started_at"] is not None or now < state["created_at"] + SOLO_SECONDS):
        raise ValueError("猜数字发送等待尚未到期")
    # A response can arrive during a successful send before the acknowledgement;
    # preserve its ingress time in history but never report negative elapsed time.
    finished = max(now, state["started_at"] or state["created_at"],
                   state["attempts"][-1]["at"] if state["attempts"] else state["created_at"])
    state.update(status="ended", end_reason=reason, winner_id=winner_id,
                 finished_at=finished, expires_at=finished)
    return True


def mark_presented(state, now, send_started_at=None):
    if state["status"] != "active" or state["started_at"] is not None:
        return False
    if not _time(now) or now < state["created_at"] or (send_started_at is not None and not _time(send_started_at)):
        raise ValueError("猜数字展示时间无效")
    if now >= state["expires_at"]:
        return finish(state, "undelivered", now)
    state["started_at"] = now
    state["accept_after"] = max(state["created_at"], min(now, send_started_at)) if send_started_at is not None else now
    state["deadline"] = now + (SOLO_SECONDS if state["mode"] == "solo" else RACE_SECONDS)
    state["expires_at"] = state["deadline"]
    return True


def expire(state, now):
    if not _time(now):
        raise ValueError("猜数字时间无效")
    if state["status"] != "active" or now < state["expires_at"]:
        return False
    return finish(state, "undelivered" if state["started_at"] is None else "deadline", now)


def submit(state, guess, user_id, name, now):
    """Record one valid guess and return its feedback. No persistence/dedup here."""
    if state["status"] != "active":
        raise ValueError("本局猜数字已经结束")
    if not _id(user_id) or user_id == state["bot_id"]:
        raise ValueError("猜数字参与者无效")
    if state["mode"] == "solo" and user_id != state["host_id"]:
        raise ValueError("这是单人挑战，只有发起者可以作答")
    if state["started_at"] is None:
        raise ValueError("题目尚未成功发送，请用 #题目 重试")
    if not _time(now) or now < state["accept_after"] or now >= state["deadline"]:
        raise ValueError("作答不在本局有效时间内")
    if state["attempts"] and now < state["attempts"][-1]["at"]:
        raise ValueError("作答时间顺序无效")
    guess = normalise_guess(guess)
    name = _name(name, user_id)
    a, b = score(state["secret"], guess)
    uid = str(user_id)
    state["players"][uid] = {"id": user_id, "name": name}
    state["attempt_counts"][uid] = state["attempt_counts"].get(uid, 0) + 1
    state["total_attempts"] += 1
    attempt = {"user_id": user_id, "name": name, "guess": guess, "a": a, "b": b,
               "number": state["total_attempts"], "at": now}
    state["attempts"] = (state["attempts"] + [attempt])[-HISTORY_LIMIT:]
    if a == 4:
        finish(state, "win", now, user_id)
    return dict(attempt)


def _require(condition):
    if not condition:
        raise ValueError(_BAD_STATE)


def _validate(row):
    _require(isinstance(row, dict) and type(row["schema"]) is int and row["schema"] == 3 and row["game"] == "number")
    _require(row["status"] in {"active", "ended"} and row["mode"] in {"solo", "race"})
    _require(type(row["version"]) is int and row["version"] >= 1)
    for key in ("session_id", "round_id"):
        _require(isinstance(row[key], str) and _UUID.fullmatch(row[key]) is not None)
    _require(all(_id(row[key]) for key in ("bot_id", "group_id", "host_id")))
    _require(row["host_id"] != row["bot_id"] and _valid_number(row["secret"]))
    for key in ("persona_id", "bot_name"):
        _require(isinstance(row[key], str) and len(row[key]) <= 80)
    _require(isinstance(row["seen"], list) and len(row["seen"]) <= 512)
    _require(all(isinstance(item, str) and 0 < len(item) <= 128 for item in row["seen"]))
    for key in ("created_at", "expires_at", "started_at", "accept_after", "deadline", "finished_at"):
        _require(row[key] is None and key not in {"created_at", "expires_at"} or _time(row[key]))
    _require(row["expires_at"] >= row["created_at"])
    players, counts, attempts = row["players"], row["attempt_counts"], row["attempts"]
    _require(isinstance(players, dict) and isinstance(counts, dict) and set(players) == set(counts))
    _require(str(row["host_id"]) in players)
    if row["mode"] == "solo":
        _require(set(players) == {str(row["host_id"])})
    total = row["total_attempts"]
    _require(type(total) is int and total >= 0)
    for uid, player in players.items():
        _require(isinstance(player, dict) and _id(player["id"]) and str(player["id"]) == uid and player["id"] != row["bot_id"])
        _require(isinstance(player["name"], str) and 0 < len(player["name"]) <= 40)
        _require(type(counts[uid]) is int and 0 <= counts[uid] <= total)
        _require(counts[uid] > 0 or player["id"] == row["host_id"])
    _require(sum(counts.values()) == total)
    _require(isinstance(attempts, list) and len(attempts) == min(total, HISTORY_LIMIT))
    shown = row["started_at"] is not None
    _require(shown == (row["accept_after"] is not None) == (row["deadline"] is not None))
    if shown:
        _require(row["created_at"] <= row["accept_after"] <= row["started_at"])
        limit = SOLO_SECONDS if row["mode"] == "solo" else RACE_SECONDS
        _require(math.isclose(row["deadline"], row["started_at"] + limit, rel_tol=0, abs_tol=1e-6))
    else:
        _require(total == 0 and set(players) == {str(row["host_id"])})
    retained = {uid: 0 for uid in players}
    previous_at = row["accept_after"] if shown else row["created_at"]
    for number, attempt in enumerate(attempts, total - len(attempts) + 1):
        _require(isinstance(attempt, dict) and _id(attempt["user_id"]) and str(attempt["user_id"]) in players)
        _require(isinstance(attempt["name"], str) and 0 < len(attempt["name"]) <= 40)
        _require(_valid_number(attempt["guess"]))
        _require(type(attempt["number"]) is int and attempt["number"] == number)
        _require(type(attempt["a"]) is int and type(attempt["b"]) is int)
        _require((attempt["a"], attempt["b"]) == score(row["secret"], attempt["guess"]))
        _require(_time(attempt["at"]) and previous_at <= attempt["at"] < row["deadline"])
        _require(attempt["a"] != 4 or (row["status"] == "ended" and row["end_reason"] == "win" and number == total))
        retained[str(attempt["user_id"])] += 1
        previous_at = attempt["at"]
    _require(all(counts[uid] >= seen for uid, seen in retained.items()))
    if total <= HISTORY_LIMIT:
        _require(counts == retained)
    if row["status"] == "active":
        _require(row["winner_id"] == 0 and type(row["winner_id"]) is int and row["end_reason"] == "" and row["finished_at"] is None)
        _require(row["expires_at"] == (row["deadline"] if shown else row["created_at"] + SOLO_SECONDS))
    else:
        reason, finished, winner = row["end_reason"], row["finished_at"], row["winner_id"]
        _require(reason in _REASONS and _time(finished) and finished >= previous_at)
        _require(finished >= (row["started_at"] if shown else row["created_at"]) and row["expires_at"] == finished)
        if reason == "win":
            _require(shown and _id(winner) and attempts and attempts[-1]["user_id"] == winner and attempts[-1]["a"] == 4 and finished < row["deadline"])
        else:
            _require(type(winner) is int and winner == 0)
        if reason == "deadline":
            _require(shown and finished >= row["deadline"])
        elif reason == "undelivered":
            _require(not shown and finished >= row["created_at"] + SOLO_SECONDS)


def validate(row):
    """Validate persisted schema 3, never expose its secret through errors."""
    try:
        _validate(row)
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
        raise ValueError(_BAD_STATE) from None
