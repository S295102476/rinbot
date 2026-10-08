"""Versioned Xiangqi/Go state transitions, independent of transport and AI."""
from copy import deepcopy
import math
import uuid

from . import go_rules as go

GAMES = {"xiangqi", "go"}
MAX_MOVES = 2048


def create(bot_id, group_id, user_id, name, command, now, persona, invitee_name, seen, wait_seconds, idle_seconds):
    side = 1 if command.first else 2
    state = {"schema": 4, "session_id": uuid.uuid4().hex, "version": 0,
        "bot_id": bot_id, "group_id": group_id, "game": command.game,
        "mode": command.mode, "difficulty": command.difficulty,
        "status": "active" if command.mode == "bot" else "waiting", "phase": "play",
        "host_id": user_id, "host_side": side, "invitee": command.invitee,
        "players": {str(side): {"id": user_id, "name": name[:40]}, str(3-side): {
            "id": bot_id if command.mode == "bot" else 0,
            "name": persona.get("name", "机器人") if command.mode == "bot" else (invitee_name or "等待加入")}},
        "persona_id": persona.get("id", ""), "bot_name": persona.get("name", "机器人"),
        "persona_avatar": persona.get("avatar", ""),
        "turn": 1, "moves": [], "winner": 0, "winning_line": [], "end_reason": "",
        "undo_counts": {}, "pending_undo": None, "pending_draw": None,
        "created_at": now, "expires_at": now+(idle_seconds if command.mode == "bot" else wait_seconds),
        "seen": list(seen)[-512:]}
    if command.game == "xiangqi":
        from . import xiangqi
        xiangqi.ensure_available()
        state["initial_fen"] = xiangqi.START_FEN
    else:
        state.update(board_size=go.check_size(command.board_size), komi=go.KOMI,
                     rules="chinese-ogs", resume_after=[], scoring=None)
    rebuild(state)
    return state


def finish(state, reason, winner=0):
    state.update(status="ended", end_reason=reason, winner=winner, pending_undo=None, pending_draw=None)


def rebuild(state):
    if state["game"] == "xiangqi":
        from . import xiangqi
        data = xiangqi.replay([m["move"] for m in state["moves"]], state["initial_fen"])
        for field in ("board", "turn", "fen", "in_check"):
            state[field] = data[field]
        if data["winner"]:
            finish(state, data["end_reason"], data["winner"])
    else:
        data = go.replay(state["moves"], state["board_size"], state.get("resume_after", []))
        for field in ("board", "turn", "captures", "passes"):
            state[field] = data[field]
    return data


def move(state, value, user_id, now, idle_seconds):
    if state["phase"] != "play":
        raise ValueError("正在确认数目，请先 #继续对局")
    if len(state["moves"]) >= MAX_MOVES:
        raise ValueError("本局已达到安全步数上限，请结束后重新开局")
    side = state["turn"]
    if state["players"][str(side)]["id"] != user_id:
        raise ValueError("还没轮到你，请等对手走棋")
    if state["game"] == "xiangqi":
        from . import xiangqi
        value = xiangqi.parse_move(value, [m["move"] for m in state["moves"]], state["initial_fen"])
    elif value != "pass" and type(value) is not int:
        value = go.parse_move(value, state["board_size"])
    state["moves"].append({"move": value, "side": side, "user_id": user_id})
    rebuild(state)
    state.update(expires_at=now+idle_seconds, pending_undo=None, pending_draw=None)
    if state["game"] == "go" and state["passes"] == 2:
        state.update(phase="scoring", expires_at=now+600, scoring={
            "id": uuid.uuid4().hex,
            "dead": [], "confirmed": [], "suggested": None, "source": "pending",
            "revision": 0, "score": go.score(state["board"], state["board_size"]),
            "deadline": now+600})
    if len(state["moves"]) >= MAX_MOVES and state["status"] != "ended":
        finish(state, "move_limit")


def scoring_needed(state):
    return bool(state and state.get("game") == "go" and state["status"] == "active"
                and state.get("phase") == "scoring" and state["scoring"]["source"] == "pending")


def set_suggestion(state, dead):
    if not scoring_needed(state):
        raise ValueError("数目提案已变化")
    proposal = state["scoring"]
    if dead is None:
        if state["mode"] != "pvp":
            raise ValueError("机器人无法确认死子，保留棋局等待引擎恢复")
        proposal.update(source="manual", suggested=None)
    else:
        points = sorted(set(dead))
        score = go.score(state["board"], state["board_size"], points)
        proposal.update(source="engine", suggested=points, dead=points, score=score)
    proposal["revision"] += 1
    proposal["confirmed"] = [state["bot_id"]] if state["mode"] == "bot" else []


def execute(state, user_id, name, command, now, *, is_admin=False, idle_seconds=600, undo_seconds=60):
    action = command.action
    players = state["players"]
    participant = user_id != state["bot_id"] and user_id in {p["id"] for p in players.values()}
    if action == "cancel":
        if not participant and not is_admin:
            raise ValueError("只有对局玩家或管理员可以结束游戏")
        finish(state, "cancelled")
        return ""
    if action == "join":
        if state["status"] != "waiting" or state["mode"] != "pvp":
            raise ValueError("当前没有等待加入的棋局")
        if participant or user_id == state["bot_id"]:
            raise ValueError("不能加入自己创建的棋局")
        if state["invitee"] and state["invitee"] != user_id:
            raise ValueError("仅被邀请的群友可以加入")
        players[str(3-state["host_side"])] = {"id": user_id, "name": name[:40]}
        state.update(status="active", expires_at=now+idle_seconds)
        return ""
    if not participant:
        raise ValueError("只有本局玩家可以操作，旁观请用 #棋盘")
    if state["status"] != "active":
        raise ValueError("请先等待对手加入")
    side = next(int(s) for s, p in players.items() if p["id"] == user_id)
    if action == "resign":
        finish(state, "resigned", 3-side)
        return ""
    if action == "resume":
        if state["game"] != "go" or state["phase"] != "scoring":
            raise ValueError("只有围棋确认数目阶段才能继续对局")
        state["resume_after"].append(len(state["moves"]))
        state.update(phase="play", scoring=None, passes=0, expires_at=now+idle_seconds)
        return "已继续对局，保留原劫历史；请通过落子解决死活争议"
    if action in {"mark_dead", "unmark_dead", "confirm_score"}:
        if state["game"] != "go" or state["phase"] != "scoring":
            raise ValueError("双方连续停一手后才能确认数目")
        proposal = state["scoring"]
        if proposal["source"] == "pending":
            raise ValueError("死子建议尚未就绪，请稍后 #棋盘 查看，或 #继续对局")
        if action != "confirm_score":
            index = go.parse_move(command.argument, state["board_size"])
            stones = go.group(state["board"], index, state["board_size"])[0]
            dead = set(proposal["dead"])
            updated = dead | stones if action == "mark_dead" else dead-stones
            if updated == dead:
                raise ValueError("标记未变化")
            proposal.update(dead=sorted(updated), confirmed=[], revision=proposal["revision"]+1,
                            score=go.score(state["board"], state["board_size"], updated))
            if state["mode"] == "bot" and sorted(updated) == proposal["suggested"]:
                proposal["confirmed"] = [state["bot_id"]]
            return "死子标记已更新，需要重新确认；有争议请 #继续对局"
        if state["mode"] == "bot" and proposal["dead"] != proposal["suggested"]:
            raise ValueError("机器人不同意当前死子标记，请 #继续对局 把死活走清楚")
        if user_id in proposal["confirmed"]:
            raise ValueError("你已确认，正在等待对方")
        proposal["confirmed"].append(user_id)
        if set(proposal["confirmed"]) == {p["id"] for p in players.values()}:
            finish(state, "scored", proposal["score"]["winner"])
        return ""
    if state["phase"] != "play":
        raise ValueError("正在确认数目；如需落子或悔棋，请先 #继续对局")
    if action == "move" or action == "pass":
        if action == "pass" and state["game"] != "go":
            raise ValueError("象棋不能停一手")
        move(state, "pass" if action == "pass" else command.argument, user_id, now, idle_seconds)
    elif action in {"offer_draw", "accept_draw", "reject_draw"}:
        if state["game"] != "xiangqi" or state["mode"] != "pvp":
            raise ValueError("仅象棋双人对局支持协议求和")
        pending = state.get("pending_draw")
        if action == "offer_draw":
            if pending and pending["expires_at"] > now:
                raise ValueError("已有求和申请，请等待对方")
            state["pending_draw"] = {"user_id": user_id, "expires_at": now+60}
            return "请对手在60秒内 #同意和棋 或 #拒绝和棋；继续走棋即取消"
        if not pending or pending["expires_at"] <= now or pending["user_id"] == user_id:
            raise ValueError("没有需要你处理的有效求和申请")
        if action == "accept_draw": finish(state, "agreed_draw", -1)
        state["pending_draw"] = None
    elif action == "undo":
        if state["undo_counts"].get(str(user_id), 0) >= 3:
            raise ValueError("本局已用完3次悔棋")
        indices = [i for i, item in enumerate(state["moves"]) if item["user_id"] == user_id]
        if not indices:
            raise ValueError("你还没有可以撤回的走棋")
        if state["mode"] == "bot":
            undo(state, indices[-1], user_id)
        else:
            if state["moves"][-1]["user_id"] != user_id:
                raise ValueError("只能在对方下一步之前申请撤回自己最后一步")
            pending = state.get("pending_undo")
            if pending and pending["expires_at"] > now:
                raise ValueError("已有悔棋申请")
            state["pending_undo"] = {"user_id": user_id, "expires_at": now+undo_seconds,
                                     "move_count": len(state["moves"])}
            return "请对手在60秒内 #同意悔棋 或 #拒绝悔棋"
    elif action in {"accept_undo", "reject_undo"}:
        pending = state.get("pending_undo")
        if (state["mode"] != "pvp" or not pending or pending["expires_at"] <= now
                or pending["user_id"] == user_id or pending["move_count"] != len(state["moves"])):
            raise ValueError("没有需要你处理的有效悔棋申请")
        if action == "accept_undo": undo(state, len(state["moves"])-1, pending["user_id"])
        state["pending_undo"] = None
    else:
        raise ValueError("此游戏不支持这条操作，请查看对应教程")
    return ""


def undo(state, count, user_id):
    state["moves"] = state["moves"][:count]
    if state["game"] == "go":
        state["resume_after"] = [n for n in state["resume_after"] if n <= count]
    state["undo_counts"][str(user_id)] = state["undo_counts"].get(str(user_id), 0)+1
    state.update(phase="play", pending_undo=None, pending_draw=None)
    rebuild(state)


def validate(row):
    if row.get("schema") != 4 or row.get("game") not in GAMES:
        raise ValueError("Unsupported strategy session")
    if (row.get("status") not in {"waiting", "active", "ended"}
            or row.get("mode") not in {"bot", "pvp"}
            or row.get("phase") not in {"play", "scoring"}
            or row.get("difficulty") not in {"casual", "serious"}):
        raise ValueError("Invalid strategy mode")
    if type(row.get("version")) is not int or row["version"] < 1 or not row.get("session_id"):
        raise ValueError("Invalid strategy version")
    for name in ("bot_id", "group_id", "host_id"):
        if type(row.get(name)) is not int or row[name] <= 0: raise ValueError("Invalid strategy identity")
    for name in ("created_at", "expires_at"):
        if not isinstance(row.get(name), (int, float)) or not math.isfinite(row[name]):
            raise ValueError("Invalid strategy time")
    players = row.get("players", {})
    if set(players) != {"1", "2"} or row.get("host_side") not in (1, 2):
        raise ValueError("Invalid strategy players")
    ids = [players[str(side)]["id"] for side in (1, 2)]
    if (any(type(uid) is not int or uid < 0 for uid in ids) or players[str(row["host_side"])]["id"] != row["host_id"]
            or row["host_id"] == row["bot_id"] or (row["status"] == "active" and (min(ids) <= 0 or ids[0] == ids[1]))):
        raise ValueError("Invalid strategy participants")
    if row["mode"] == "bot" and row["bot_id"] not in ids:
        raise ValueError("Missing bot player")
    if row["status"] == "waiting" and (row["mode"] != "pvp" or ids.count(0) != 1 or row["phase"] != "play"):
        raise ValueError("Invalid waiting room")
    moves = row.get("moves")
    if not isinstance(moves, list) or len(moves) > MAX_MOVES:
        raise ValueError("Invalid strategy history")
    for i, item in enumerate(moves):
        side = 1+i%2
        if (type(item.get("side")) is not int or item.get("side") != side
                or type(item.get("user_id")) is not int or item.get("user_id") != players[str(side)]["id"]):
            raise ValueError("Invalid strategy turn")
    if row["status"] == "waiting" and moves:
        raise ValueError("Waiting room has moves")
    if row["game"] == "go":
        if row.get("komi") != go.KOMI or row.get("rules") != "chinese-ogs":
            raise ValueError("Invalid Go rules")
        resumes = row.get("resume_after", [])
        if (not isinstance(resumes, list) or resumes != sorted(set(resumes))
                or any(type(n) is not int or n < 2 or n > len(moves) for n in resumes)):
            raise ValueError("Invalid Go resume history")
    changed = deepcopy(row)
    rebuild(changed)
    fields = ("board", "turn", "fen", "in_check") if row["game"] == "xiangqi" else ("board", "turn", "captures", "passes")
    if any(changed[field] != row.get(field) for field in fields):
        raise ValueError("Strategy history does not match board")
    if row["status"] != "ended" and changed["status"] == "ended":
        raise ValueError("Finished position marked active")
    if row["game"] == "go" and row["phase"] == "scoring":
        proposal = row.get("scoring") or {}
        if not isinstance(proposal.get("id"), str) or len(proposal["id"]) != 32:
            raise ValueError("Invalid scoring proposal identity")
        if row["passes"] != 2 or proposal.get("deadline") != row["expires_at"]:
            raise ValueError("Invalid scoring phase")
        if proposal.get("source") not in {"pending", "manual", "engine"}:
            raise ValueError("Invalid scoring source")
        if type(proposal.get("revision")) is not int or proposal["revision"] < 0:
            raise ValueError("Invalid scoring revision")
        confirmations = proposal.get("confirmed")
        if (not isinstance(confirmations, list) or any(type(uid) is not int for uid in confirmations)
                or len(confirmations) != len(set(confirmations)) or not set(confirmations) <= set(ids)):
            raise ValueError("Invalid scoring confirmation")
        for field in ("dead", "suggested"):
            points = proposal.get(field)
            if field == "suggested" and points is None: continue
            if not isinstance(points, list) or points != sorted(set(points)) or any(type(p) is not int for p in points):
                raise ValueError("Invalid dead groups")
        if proposal["source"] == "pending" and (confirmations or proposal["dead"] or proposal["suggested"] is not None):
            raise ValueError("Unresolved scoring proposal contains confirmations")
        if proposal["source"] == "engine" and proposal["suggested"] is None:
            raise ValueError("Missing engine proposal")
        if proposal["source"] == "manual" and row["mode"] != "pvp":
            raise ValueError("Bot cannot approve manual-only scoring")
        computed = go.score(row["board"], row["board_size"], proposal["dead"])
        if computed != proposal.get("score"):
            raise ValueError("Invalid stored score")
        if proposal.get("suggested") is not None:
            go.score(row["board"], row["board_size"], proposal["suggested"])
        if row["mode"] == "bot" and row["bot_id"] in proposal["confirmed"] and proposal["dead"] != proposal["suggested"]:
            raise ValueError("Bot did not approve scoring draft")
        if row.get("end_reason") == "scored" and (row["status"] != "ended" or set(confirmations) != set(ids) or row["winner"] != computed["winner"]):
            raise ValueError("Score was not confirmed by both players")
    elif row["game"] == "go" and (row["passes"] >= 2 or row.get("scoring") is not None):
        raise ValueError("Go scoring position marked as playing")
    elif row["phase"] != "play":
        raise ValueError("Only Go can enter scoring")
