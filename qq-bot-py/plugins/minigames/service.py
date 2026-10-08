"""Transactional game state machine. No QQ transport or model API calls."""
from __future__ import annotations

import asyncio
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
import threading
import time
import uuid

from .commands import Command
from .rules import new_board, parse_move, play_move, outcome
from . import idiom_session as idiom
from . import number_session as number
from . import strategy_session as strategy
from .xiangqi import XiangqiUnavailable


class GameError(ValueError):
    pass


@dataclass
class Result:
    state: dict | None
    message: str = ""
    silent: bool = False


class GameService:
    def __init__(self, store, *, publish=lambda *_: None, allowed=lambda _: True,
                 clock=time.time, wait_seconds=120, idle_seconds=600, undo_seconds=60,
                 idiom_bank=None, idiom_enabled=True, number_enabled=True, strategy_enabled=None):
        self.store, self.publish, self.allowed, self.clock = store, publish, allowed, clock
        self.wait_seconds, self.idle_seconds, self.undo_seconds = wait_seconds, idle_seconds, undo_seconds
        self.states: dict[tuple[int, int], dict] = {}
        self.locks = defaultdict(asyncio.Lock)
        self.searches: dict[tuple[int, int], threading.Event] = {}
        self.ready = False
        self.idiom_bank, self.idiom_enabled = idiom_bank, idiom_enabled
        self.number_enabled = number_enabled
        self.strategy_enabled = {game: True for game in strategy.GAMES}
        self.strategy_enabled.update(strategy_enabled or {})
        self.recovery_errors = {}

    async def initialize(self):
        self.ready = False
        await self.store.initialize()
        rows = await self.store.load_all()
        # Validate everything before publishing any partial recovery.
        valid = []
        self.recovery_errors.clear()
        for row in rows:
            try:
                self.validate(row)
            except XiangqiUnavailable:
                self.recovery_errors[(row["bot_id"], row["group_id"])] = "象棋规则库未安装，原对局已保留；请管理员安装依赖后重启"
                self.publish(row["bot_id"], row["group_id"], None)
            else:
                valid.append(row)
        self.states = {(row["bot_id"], row["group_id"]): row for row in valid}
        for key, row in list(self.states.items()):
            if row["status"] != "ended" and row["expires_at"] <= self.clock():
                expired = deepcopy(row)
                if row["game"] == "number":
                    number.expire(expired, self.clock())
                elif row["game"] == "idiom":
                    idiom.expire(expired, self.idiom_bank, self.clock())
                else:
                    self._end(expired, "timeout")
                await self._commit(key, expired)
            else:
                self.publish(*key, row)
        self.ready = True

    @staticmethod
    def validate(row):
        if row.get("game") in strategy.GAMES:
            strategy.validate(row)
            return
        if row.get("game") == "number":
            number.validate(row)
            return
        if row.get("game") == "idiom":
            idiom.validate(row)
            return
        if row.get("schema") != 1 or row.get("game") not in {"gomoku", "tictactoe"}:
            raise ValueError("Unsupported minigame recovery data")
        if row.get("status") not in {"waiting", "active", "ended"} or row.get("mode") not in {"bot", "pvp"}:
            raise ValueError("Invalid minigame recovery status")
        if not isinstance(row.get("version"), int) or row["version"] < 1 or not row.get("session_id"):
            raise ValueError("Invalid minigame recovery version")
        import math
        if not math.isfinite(row.get("expires_at", float("nan"))):
            raise ValueError("Invalid minigame deadline")
        players = row.get("players", {})
        if set(players) != {"1", "2"} or row.get("turn") not in {1, 2}:
            raise ValueError("Invalid minigame players")
        ids = [players[str(side)]["id"] for side in (1, 2)]
        if row["status"] == "active" and (min(ids) <= 0 or ids[0] == ids[1]):
            raise ValueError("Invalid active players")
        board = new_board(row["game"])
        if not isinstance(row.get("moves"), list) or len(row["moves"]) > len(board):
            raise ValueError("Invalid minigame move history")
        turn = 1
        for move in row["moves"]:
            if move["side"] != turn or move["user_id"] != players[str(turn)]["id"] or outcome(row["game"], board)[0]:
                raise ValueError("Invalid minigame turn history")
            board = play_move(row["game"], board, move["index"], turn)
            turn = 3 - turn
        if board != row.get("board") or turn != row["turn"]:
            raise ValueError("Invalid minigame board history")
        if row["status"] == "active" and outcome(row["game"], board)[0]:
            raise ValueError("Finished game marked active")

    def cancel_search(self, key):
        flag = self.searches.get(key)
        if flag:
            flag.set()

    async def _commit(self, key, value, message_id=None):
        old = self.states.get(key)
        expected = old["version"] if old else None
        value["version"] = (expected or 0) + 1
        value["updated_at"] = self.clock()
        if message_id is not None:
            value["seen"] = (value.get("seen", []) + [str(message_id)])[-512:]
        try:
            await self.store.save(value, expected)
        except Exception:
            # Commit acknowledgement can be lost. Reconcile before allowing any
            # further command; never overwrite an uncertain durable state.
            self.cancel_search(key)
            try:
                actual = await self.store.load(*key)
                if actual is not None:
                    self.validate(actual)
                    self.states[key] = actual
                else:
                    self.states.pop(key, None)
                self.publish(*key, actual)
            except Exception:
                self.ready = False
                for existing in self.states:
                    self.cancel_search(existing)
            raise
        self.states[key] = value
        self.publish(*key, value)
        return value

    def _end(self, state, reason, winner=0):
        state.update(status="ended", end_reason=reason, winner=winner, pending_undo=None)

    def _move(self, state, index, user_id):
        side = state["turn"]
        state["board"] = play_move(state["game"], state["board"], index, side)
        state["moves"].append({"index": index, "side": side, "user_id": user_id})
        state.update(turn=3-side, expires_at=self.clock()+self.idle_seconds, pending_undo=None)
        winner, line = outcome(state["game"], state["board"])
        state["winning_line"] = line
        if winner:
            self._end(state, "draw" if winner == -1 else "win", winner)

    def _rebuild(self, state):
        state["board"] = new_board(state["game"])
        for move in state["moves"]:
            state["board"] = play_move(state["game"], state["board"], move["index"], move["side"])
        state.update(turn=1 if not state["moves"] else 3-state["moves"][-1]["side"],
                     pending_undo=None, winning_line=[], winner=0)
        # Undo is not a successful move and must not prolong an idle room.

    def _check_arrival(self, old, arrival, *, version=True):
        current_id = old["session_id"] if old else None
        current_version = old["version"] if old else None
        if (arrival.get("session_id") != current_id or
                (version and arrival.get("version") != current_version)):
            raise GameError("棋局或回合已变化，请查看 #棋盘 后重新操作")

    async def _execute_idiom(self, key, old, user_id, name, command, arrival, message_id, is_admin):
        """Called under the shared group lock; no network/identity lookups here."""
        bare = bool(arrival.get("bare_idiom"))
        if not self.idiom_enabled and command.action != "cancel":
            if bare:
                return Result(None, silent=True)
            raise GameError("成语填空已暂停，发起者或管理员可 #结束游戏")
        now = self.clock()
        state = deepcopy(old)
        # A valid answer is judged at ingress time, not after rendering/DB delays.
        received = min(now, float(arrival.get("received_at", now)))
        # Queries are read-only. A later #题目 must not time out the question
        # before an on-time answer has finished its awaited preprocessors.
        # Hints belong to the ticker. A guess arriving at the 60-second hint
        # boundary must still be judged, not consumed by the hint transition.
        if command.action in {"question", "view"}:
            return Result(deepcopy(state))
        if bare and arrival.get("session_id") != state["session_id"]:
            return Result(None, silent=True)
        self._check_arrival(state, arrival, version=False)
        if state["status"] != "active":
            return Result(None, silent=True)
        if command.action == "cancel":
            if user_id != state["host_id"] and not is_admin:
                raise GameError("只有发起者或管理员可以结束这局成语填空")
            idiom.finish(state, "cancelled", now)
        else:
            if command.action not in {"answer", "skip"}:
                raise GameError("成语填空请用 #作答 / #题目 / #结束游戏")
            if user_id == state["bot_id"] or (state["mode"] == "solo" and user_id != state["host_id"]):
                if bare:
                    return Result(None, silent=True)
                raise GameError("这是单人挑战，只有发起者可以作答")
            if command.action == "skip" and state["mode"] != "solo":
                raise GameError("抢答不能手动跳题；满一分钟提示一字，继续答本题")
            if arrival.get("question_id") != state["question"]["id"]:
                return Result(None, silent=True)
            if (command.action == "answer" and state.get("deadline") is not None
                    and received >= state["deadline"] and idiom.expire(state, self.idiom_bank, received)):
                await self._commit(key, state, message_id)
                return Result(deepcopy(state))
            shown = state["question_started_at"]
            accept_after = state.get("question_accept_after")
            if accept_after is None:
                accept_after = shown
            if shown is None or (command.action == "answer" and received < accept_after):
                return Result(None, "题目尚未成功发送，请用 #题目 重试", silent=bare)
            if command.action == "skip":
                # Only the whole-game deadline expires this question; a hint
                # being due is not a timeout and must not disable skipping.
                if state.get("deadline") is not None and now >= state["deadline"]:
                    return Result(None, silent=True)
                idiom.resolve(state, "skipped", self.idiom_bank, now, user_id, name)
            else:
                answer = idiom.normalise_answer(command.argument)
                if answer not in state["question"]["answers"]:
                    # Wrong answers may change persistence version, never question ID.
                    await self._commit(key, state, message_id)
                    message = "答错了，再想想，本题不会换题" + ("；也可以 #跳过" if state["mode"] == "solo" else "")
                    return Result(None, message, silent=bare)
                idiom.resolve(state, "correct", self.idiom_bank, received, user_id, name)
        await self._commit(key, state, message_id)
        return Result(deepcopy(state))

    async def _execute_number(self, key, old, user_id, name, command, arrival, message_id, is_admin):
        if not self.number_enabled and command.action != "cancel":
            raise GameError("猜数字已暂停，发起者或管理员可 #结束游戏")
        if command.action in {"question", "view"}:
            return Result(deepcopy(old))
        self._check_arrival(old, arrival, version=False)
        if old["status"] != "active":
            return Result(None, silent=True)
        state = deepcopy(old)
        now = self.clock()
        if command.action == "cancel":
            if user_id != state["host_id"] and not is_admin:
                raise GameError("只有发起者或管理员可以结束这局猜数字")
            number.finish(state, "cancelled", now)
        else:
            if command.action not in {"guess", "answer"}:
                raise GameError("猜数字请用 #猜 1234 / #题目 / #结束游戏")
            if user_id == state["bot_id"] or (state["mode"] == "solo" and user_id != state["host_id"]):
                raise GameError("这是单人挑战，只有发起者可以猜数字")
            if arrival.get("question_id") != state["round_id"]:
                return Result(None, silent=True)
            received = min(now, float(arrival.get("received_at", now)))
            # Expiry is coordinated with the ingress queue, never by a query or
            # another game's start command overtaking an on-time answer.
            if number.expire(state, received):
                await self._commit(key, state, message_id)
                return Result(deepcopy(state))
            if state["started_at"] is None or received < state["accept_after"]:
                return Result(None, "题目尚未成功发送，请用 #题目 重试", silent=state["mode"] == "race")
            guess = number.normalise_guess(command.argument)
            number.submit(state, guess, user_id, name, received)
        await self._commit(key, state, message_id)
        return Result(deepcopy(state))

    async def number_presented_locked(self, key, session_id, round_id, sent_at, *, send_started_at=None):
        old = self.states.get(key)
        if (not old or old["game"] != "number" or old["session_id"] != session_id
                or old["round_id"] != round_id):
            return
        state = deepcopy(old)
        if number.mark_presented(state, sent_at, send_started_at=send_started_at):
            await self._commit(key, state)

    async def presented_locked(self, key, session_id, question_id, sent_at, *, send_started_at=None):
        """Transport calls while holding the group lock after successful send."""
        old = self.states.get(key)
        if (not old or old["game"] != "idiom" or old["session_id"] != session_id
                or old["question"]["id"] != question_id):
            return
        state = deepcopy(old)
        if idiom.mark_presented(state, sent_at, send_started_at=send_started_at):
            await self._commit(key, state)

    async def execute(self, bot_id, group_id, user_id, name, command: Command, arrival,
                      *, message_id, is_admin=False, persona=None, invitee_name=""):
        key = (int(bot_id), int(group_id))
        async with self.locks[key]:
            if not self.ready:
                raise GameError("小游戏存储尚未就绪，请稍后再试")
            if not self.allowed(group_id):
                raise GameError("本群未启用小游戏")
            if key in self.recovery_errors:
                raise GameError(self.recovery_errors[key])
            old = self.states.get(key)
            if old and str(message_id) in old.get("seen", []):
                return Result(None, silent=True)
            now = self.clock()
            if old and old["game"] == "number" and command.action != "start":
                return await self._execute_number(key, old, user_id, name, command, arrival, message_id, is_admin)
            if old and old["game"] == "idiom" and command.action != "start":
                return await self._execute_idiom(key, old, user_id, name, command, arrival, message_id, is_admin)
            if command.action in {"answer", "question", "skip", "guess"}:
                raise GameError("本群没有对应的答题对局，发送 #小游戏 查看玩法")
            # Idiom expiry belongs to the answer queue/ticker: even a new start
            # must not expire or replace a question with an on-time answer pending.
            if old and old["game"] not in {"idiom", "number"} and old["status"] != "ended" and old["expires_at"] <= now:
                expired = deepcopy(old)
                self._end(expired, "timeout")
                self.cancel_search(key)
                await self._commit(key, expired)
                old = expired
                # A new start captured before expiration is allowed to replace
                # this same expired session; no other pending action is replayed.
                if command.action == "start" and arrival.get("session_id") == expired["session_id"]:
                    arrival = dict(arrival, version=expired["version"])
            if command.action == "view":
                if not old:
                    raise GameError("本群还没有棋局，发送 #小游戏 选择游戏")
                return Result(deepcopy(old))
            self._check_arrival(old, arrival, version=command.action not in {"cancel", "resign", "confirm_score"})
            if command.action == "confirm_score" and old and old.get("phase") == "scoring":
                if (arrival.get("score_revision") != old["scoring"]["revision"]
                        or arrival.get("score_id") != old["scoring"]["id"]):
                    raise GameError("数目提案已变化，请 #棋盘 查看后重新确认")
            if command.action == "start":
                if old and old["status"] != "ended":
                    raise GameError("本群已有一盘游戏，请 #棋盘 查看或等待结束")
                persona = persona or {}
                if command.game in strategy.GAMES:
                    if not self.strategy_enabled[command.game]:
                        raise GameError("此小游戏尚未启用")
                    state = strategy.create(bot_id, group_id, user_id, name, command, now, persona,
                        invitee_name, old.get("seen", []) if old else [], self.wait_seconds, self.idle_seconds)
                    await self._commit(key, state, message_id)
                    return Result(deepcopy(state), "发送 #加入游戏 加入对局" if command.mode == "pvp" else "")
                if command.game == "number":
                    if not self.number_enabled:
                        raise GameError("猜数字暂未启用")
                    state = number.create(bot_id, group_id, user_id, name, command.mode, now,
                                          persona, old.get("seen", []) if old else ())
                    await self._commit(key, state, message_id)
                    return Result(deepcopy(state))
                if command.game == "idiom":
                    if not self.idiom_enabled:
                        raise GameError("成语填空暂未启用")
                    state = idiom.create(self.idiom_bank, bot_id, group_id, user_id, name,
                                         command.mode, now, persona, old.get("seen", []) if old else ())
                    await self._commit(key, state, message_id)
                    return Result(deepcopy(state))
                host_side = 1 if command.first else 2
                opponent = {"id": bot_id if command.mode == "bot" else 0,
                            "name": persona.get("name", "机器人") if command.mode == "bot" else (invitee_name or "等待加入")}
                state = {"schema": 1, "session_id": uuid.uuid4().hex, "version": 0,
                         "bot_id": bot_id, "group_id": group_id, "game": command.game,
                         "mode": command.mode, "difficulty": command.difficulty,
                         "status": "active" if command.mode == "bot" else "waiting",
                         "host_id": user_id, "host_side": host_side, "invitee": command.invitee,
                         "players": {str(host_side): {"id": user_id, "name": name[:40]}, str(3-host_side): opponent},
                         "persona_id": persona.get("id", ""), "bot_name": persona.get("name", "机器人"),
                         "persona_avatar": persona.get("avatar", ""),
                         "board": new_board(command.game), "moves": [], "turn": 1,
                         "winner": 0, "winning_line": [], "end_reason": "",
                         "undo_counts": {}, "pending_undo": None,
                         "created_at": now, "expires_at": now+(self.idle_seconds if command.mode == "bot" else self.wait_seconds),
                         "seen": list(old.get("seen", [])) if old else []}
                await self._commit(key, state, message_id)
                return Result(deepcopy(state), "发送 #加入游戏 接受邀请" if command.invitee else
                              "等待另一位群友发送 #加入游戏" if command.mode == "pvp" else "可直接发坐标，也可用 #落子")
            if not old or old["status"] == "ended":
                raise GameError("当前没有进行中的棋局，发送 #小游戏 开始")
            if old["game"] in strategy.GAMES:
                if not self.strategy_enabled[old["game"]] and command.action != "cancel":
                    raise GameError("此小游戏已暂停，可以 #结束游戏")
                state = deepcopy(old)
                message = strategy.execute(state, user_id, name, command, now, is_admin=is_admin,
                    idle_seconds=self.idle_seconds, undo_seconds=self.undo_seconds)
                await self._commit(key, state, message_id)
                if command.action in {"cancel", "resign", "undo", "accept_undo", "resume"}:
                    self.cancel_search(key)
                return Result(deepcopy(state), message)
            state = deepcopy(old)
            player_ids = {p["id"] for p in state["players"].values()}
            participant = user_id in player_ids and user_id != bot_id
            if command.action == "join":
                if state["status"] != "waiting" or state["mode"] != "pvp":
                    raise GameError("当前棋局没有等待加入")
                if participant or user_id == bot_id:
                    raise GameError("不能加入自己创建的棋局")
                if state["invitee"] and state["invitee"] != user_id:
                    raise GameError("这盘棋只接受被邀请的群友加入")
                state["players"][str(3-state["host_side"])] = {"id": user_id, "name": name[:40]}
                state.update(status="active", expires_at=now+self.idle_seconds)
            elif command.action == "cancel":
                if not participant and not is_admin:
                    raise GameError("只有对局玩家或管理员可以结束游戏")
                self._end(state, "cancelled")
            else:
                if not participant:
                    raise GameError("只有本局玩家可以操作，旁观请用 #棋盘")
                if state["status"] != "active":
                    raise GameError("还在等待对手加入，暂不能落子或悔棋")
                side = next(int(s) for s, p in state["players"].items() if p["id"] == user_id)
                if command.action == "move":
                    if state["turn"] != side:
                        raise GameError("还没轮到你，请等对手落子")
                    self._move(state, parse_move(state["game"], command.argument), user_id)
                elif command.action == "resign":
                    self._end(state, "resigned", 3-side)
                elif command.action == "undo":
                    if state["undo_counts"].get(str(user_id), 0) >= 3:
                        raise GameError("本局已用完 3 次悔棋")
                    if not state["moves"]:
                        raise GameError("还没有可以撤回的落子")
                    if state["mode"] == "bot":
                        indices = [i for i, move in enumerate(state["moves"]) if move["user_id"] == user_id]
                        if not indices:
                            raise GameError("你还没有落子，不能撤回机器人的开局")
                        state["moves"] = state["moves"][:indices[-1]]
                        state["undo_counts"][str(user_id)] = state["undo_counts"].get(str(user_id), 0)+1
                        self._rebuild(state)
                    else:
                        if state["moves"][-1]["user_id"] != user_id:
                            raise GameError("只能在对手走下一步之前，申请撤回自己刚下的一步")
                        pending = state.get("pending_undo")
                        if pending and pending["expires_at"] > now:
                            raise GameError("已有悔棋申请，请等待对手处理")
                        state["pending_undo"] = {"user_id": user_id, "expires_at": now+self.undo_seconds,
                                                 "move_count": len(state["moves"])}
                elif command.action in {"accept_undo", "reject_undo"}:
                    pending = state.get("pending_undo")
                    if (state["mode"] != "pvp" or not pending or pending["expires_at"] <= now
                            or pending["move_count"] != len(state["moves"])):
                        raise GameError("没有有效的悔棋申请")
                    if pending["user_id"] == user_id:
                        raise GameError("需要对手处理你的悔棋申请")
                    if command.action == "accept_undo":
                        owner = str(pending["user_id"])
                        state["moves"].pop()
                        state["undo_counts"][owner] = state["undo_counts"].get(owner, 0)+1
                        self._rebuild(state)
                    state["pending_undo"] = None
                else:
                    raise GameError("未知游戏操作")
            await self._commit(key, state, message_id)
            if command.action in {"cancel", "resign", "undo", "accept_undo"}:
                self.cancel_search(key)
            message = "请对手在 60 秒内发送 #同意悔棋 或 #拒绝悔棋" if state.get("pending_undo") else ""
            return Result(deepcopy(state), message)

    @staticmethod
    def needs_bot(state):
        return bool(state and state["status"] == "active" and state["mode"] == "bot"
                    and state.get("phase", "play") == "play"
                    and state["players"][str(state["turn"])]["id"] == state["bot_id"])

    async def bot_step(self, key, choose, semaphore, *, budget=1.0, strategy_choose=None):
        async with self.locks[key]:
            state = self.states.get(key)
            if (not self.ready or not self.allowed(key[1]) or not self.needs_bot(state)
                    or not self.strategy_enabled.get(state["game"], True)
                    or state["expires_at"] <= self.clock() or key in self.searches):
                return None
            snapshot = deepcopy(state)
            flag = threading.Event()
            self.searches[key] = flag
        try:
            async with semaphore:
                if flag.is_set():
                    return None
                # Keep the semaphore until the actual worker finishes, even if
                # shutdown cancels its awaiting coroutine. No orphan CPU workers.
                if snapshot["game"] in strategy.GAMES:
                    if strategy_choose is None:
                        raise GameError("本地引擎尚未就绪")
                    worker = asyncio.create_task(asyncio.to_thread(strategy_choose, snapshot, flag))
                else:
                    worker = asyncio.create_task(asyncio.to_thread(choose, snapshot["game"], snapshot["board"],
                        snapshot["turn"], snapshot["difficulty"], budget, flag))
                try:
                    index = await asyncio.shield(worker)
                except asyncio.CancelledError:
                    flag.set()
                    try:
                        await worker
                    except Exception:
                        pass
                    raise
            async with self.locks[key]:
                current = self.states.get(key)
                if (flag.is_set() or not self.ready or not self.allowed(key[1]) or not current
                        or current["session_id"] != snapshot["session_id"]
                        or current["version"] != snapshot["version"]
                        or not self.strategy_enabled.get(current["game"], True)
                        or not self.needs_bot(current) or current["expires_at"] <= self.clock()):
                    return None
                changed = deepcopy(current)
                if current["game"] in strategy.GAMES:
                    strategy.move(changed, index, current["bot_id"], self.clock(), self.idle_seconds)
                else:
                    self._move(changed, index, current["bot_id"])
                await self._commit(key, changed)
                return Result(deepcopy(changed))
        except Exception:
            if flag.is_set():
                return None
            raise
        finally:
            if self.searches.get(key) is flag:
                self.searches.pop(key, None)

    async def scoring_step(self, key, suggest, semaphore):
        async with self.locks[key]:
            old = self.states.get(key)
            if (not self.ready or not self.allowed(key[1]) or not self.strategy_enabled["go"]
                    or not strategy.scoring_needed(old) or old["expires_at"] <= self.clock() or key in self.searches):
                return None
            snapshot, flag = deepcopy(old), threading.Event()
            self.searches[key] = flag
        fallback = False
        try:
            async with semaphore:
                if flag.is_set(): return None
                task = asyncio.create_task(asyncio.to_thread(suggest, snapshot, flag))
                try:
                    dead = await asyncio.shield(task)
                except asyncio.CancelledError:
                    flag.set()
                    await asyncio.gather(task, return_exceptions=True)
                    raise
                except Exception:
                    if flag.is_set(): return None
                    if snapshot["mode"] != "pvp": raise
                    dead, fallback = None, True
            async with self.locks[key]:
                current = self.states.get(key)
                if (flag.is_set() or not self.ready or not self.allowed(key[1]) or not self.strategy_enabled["go"]
                        or not strategy.scoring_needed(current) or current["session_id"] != snapshot["session_id"]
                        or current["version"] != snapshot["version"] or current["expires_at"] <= self.clock()):
                    return None
                changed = deepcopy(current)
                try:
                    strategy.set_suggestion(changed, dead)
                except ValueError:
                    if current["mode"] != "pvp": raise
                    strategy.set_suggestion(changed, None)
                    fallback = True
                await self._commit(key, changed)
                return Result(deepcopy(changed), "自动死子建议不可用，请双方手动标死并确认，或继续对局" if fallback else "请检查死子标记，双方 #确认数目；有争议可 #继续对局")
        finally:
            if self.searches.get(key) is flag:
                self.searches.pop(key, None)

    async def expire(self, pending_answer=None):
        if not self.ready:
            return []
        results = []
        for key in list(self.states):
            async with self.locks[key]:
                old = self.states[key]
                if old["status"] != "ended" and old["expires_at"] <= self.clock():
                    state = deepcopy(old)
                    if old["game"] in {"idiom", "number"}:
                        if pending_answer and pending_answer(key, old):
                            continue
                        if old["game"] == "number":
                            number.expire(state, self.clock())
                        else:
                            idiom.expire(state, self.idiom_bank, self.clock())
                    else:
                        self._end(state, "timeout")
                    await self._commit(key, state)
                    self.cancel_search(key)
                    results.append(Result(deepcopy(state), "" if old["game"] in {"idiom", "number"} else "长时间没有落子，本局已取消"))
        return results
