"""Group-only board games, isolated from Agent decisions and reply quotas."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from pathlib import Path
import threading
import time

import yaml
from nonebot import get_bot, get_driver, on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageSegment
from nonebot.log import logger
from nonebot.rule import Rule

from .. import minigame_gate as gate
from ..minigame_registry import GAME_REGISTRY, GAME_NAMES
from .ai import choose_move
from .avatars import AvatarCache
from .commands import Command, parse_command
from .render import render_board, render_menu, render_idiom, render_number
from .idioms import load_default, IdiomBankError
from .rapfi import RapfiEngine, RapfiUnavailable
from .service import GameError, GameService, Result
from .storage import SQLStore
from . import strategy_session as strategy
from .board_engines import BoardEngines

ROOT = Path(__file__).resolve().parents[2]
_config_path = Path("config.yaml")
if not _config_path.exists():
    _config_path = ROOT / "config.yaml"
_config = yaml.safe_load(_config_path.read_text(encoding="utf-8")) or {}
import copy
from runtime_config import feature_enabled
_cfg = copy.deepcopy(_config.get("minigames") or {})
if not feature_enabled(_config, "board_engines"):
    _cfg.setdefault("rapfi", {})["enabled"] = False
    for _game in ("xiangqi", "go"):
        _cfg.setdefault(_game, {}).setdefault("engine", {})["enabled"] = False
_mode = _config.get("group_mode") or {}
ENABLED = bool(_cfg.get("enabled", True))
ALLOWED_GROUPS = {int(g) for g in _cfg.get("allowed_groups", [])}
BLOCKED_GROUPS = {int(g) for key in ("chat_only_groups", "local_disabled_groups", "gb_frame_only_groups")
                  for g in _mode.get(key, [])}
ADMIN_USERS = {int(u) for u in ((_config.get("agent") or {}).get("dev") or {}).get("admin_users", [])}
gate.configure(enabled=ENABLED, allowed_groups=ALLOWED_GROUPS, blocked_groups=BLOCKED_GROUPS,
               disabled_games={game for game in ("idiom", "number", "xiangqi", "go")
                               if not bool((_cfg.get(game) or {}).get("enabled", True))})
SERVICE = GameService(SQLStore(), publish=gate.publish, allowed=gate.allows,
    wait_seconds=max(30, min(600, int(_cfg.get("wait_seconds", 120)))),
    idle_seconds=max(60, min(3600, int(_cfg.get("idle_seconds", 600)))),
    idiom_enabled=bool((_cfg.get("idiom") or {}).get("enabled", True)),
    number_enabled=bool((_cfg.get("number") or {}).get("enabled", True)),
    strategy_enabled={game: bool((_cfg.get(game) or {}).get("enabled", True)) for game in strategy.GAMES})
try:
    RAPFI = RapfiEngine(_cfg.get("rapfi") or {}, root=ROOT)
except Exception as exc:
    logger.warning(f"[minigame] rapfi_config_invalid={type(exc).__name__}; serious_mode_disabled")
    RAPFI = RapfiEngine({}, root=ROOT)
try:
    ENGINES = BoardEngines(_cfg, root=ROOT, rapfi=RAPFI)
except Exception as exc:
    logger.warning(f"[minigame] board_engine_config_invalid={type(exc).__name__}; new_ai_modes_disabled")
    ENGINES = BoardEngines({}, root=ROOT, rapfi=RAPFI)
_search_semaphore = asyncio.Semaphore(2)
_tasks: dict[tuple[int, int], asyncio.Task] = {}
_retry_after: dict[tuple[int, int], float] = {}
_engine_notices: dict[tuple[int, int], float] = {}
_rates: OrderedDict[tuple, float] = OrderedDict()
_ticker: asyncio.Task | None = None
AVATARS = AvatarCache()
_idiom_load_lock = asyncio.Lock()
_idiom_retry_after = 0.0
_idiom_last_error = "成语题库未就绪，请管理员运行 tools/check_idioms.py 检查"


async def _ensure_idiom_bank(*, force=False):
    """Allow a repaired SFTP upload to recover without taking chess offline."""
    global _idiom_retry_after, _idiom_last_error
    if SERVICE.idiom_bank is not None:
        return True
    async with _idiom_load_lock:
        if SERVICE.idiom_bank is not None:
            return True
        if not force and time.monotonic() < _idiom_retry_after:
            return False
        try:
            bank = await asyncio.to_thread(load_default, ROOT)
        except Exception as exc:
            _idiom_retry_after = time.monotonic() + 30
            detail = str(exc) if isinstance(exc, IdiomBankError) else type(exc).__name__
            logger.warning(f"[minigame] idiom_bank_unavailable detail={detail}; run tools/check_idioms.py; board_games_unaffected")
            filename = getattr(exc, "filename", "")
            code = getattr(exc, "code", "load_error")
            _idiom_last_error = (f"成语题库未就绪（{code}{': ' + filename if filename else ''}）。"
                "请管理员完整上传 data/minigames/idioms/，运行 tools/check_idioms.py 检查；补齐后30秒可重试")
            return False
        SERVICE.idiom_bank = bank
        _idiom_retry_after = 0.0
        logger.info(f"[minigame] idiom_bank_ready words={bank.word_count} pool={bank.pool_size} enabled={SERVICE.idiom_enabled}")
        return True


def _limit(key, interval):
    now = time.monotonic()
    if now - _rates.get(key, -float("inf")) < interval:
        return False
    _rates[key] = now
    _rates.move_to_end(key)
    while len(_rates) > 4096:
        _rates.popitem(last=False)
    return True


def _persona():
    from ..persona_manager import get_active_persona
    profile = get_active_persona()
    avatar_dir = Path((_config.get("duty_roster") or {}).get("avatar_dir", "data/dutyroster"))
    if not avatar_dir.is_absolute():
        avatar_dir = ROOT / avatar_dir
    avatar = avatar_dir / f"{profile.persona_id}.jpg" if profile.persona_id in {"rin", "eres", "ishtar"} else None
    return {"id": profile.persona_id, "name": profile.name, "avatar": str(avatar) if avatar else ""}


async def _rule(bot: Bot, event: GroupMessageEvent):
    if not isinstance(event, GroupMessageEvent):
        return False
    return gate.capture(int(bot.self_id), event) is not None


game_cmd = on_message(rule=Rule(_rule), priority=-1, block=True)


def _caption(state):
    if state["status"] == "waiting":
        return "等待加入 · #加入游戏 / #结束游戏"
    if state["status"] == "ended":
        winner = state.get("winner", 0)
        menu = "#"+GAME_NAMES[state["game"]]
        next_game = f" · 发送 {menu} 查看开局方式"
        if winner in (1, 2):
            result = f"{state['players'][str(winner)]['name']} 获胜"
            if state.get("end_reason") == "scored":
                result += f"，领先 {state['scoring']['score']['margin']:g} 点（白贴7.5点）"
            return result + next_game
        return ("平局" if winner == -1 else "本局已结束") + next_game
    if state.get("phase") == "scoring":
        return "等待确认数目 · #标死 D4 / #取消死子 D4 / #确认数目 / #继续对局"
    name = state["players"][str(state["turn"])]["name"]
    return f"轮到 {name} · 可直接发坐标 · #悔棋 / #认输"


async def _send_result(bot, result):
    if result.silent or result.state is None:
        return
    state = result.state
    if state["game"] == "number":
        await _send_number_result(bot, result)
        return
    if state["game"] == "idiom":
        await _send_idiom_result(bot, result)
        return
    key = (state["bot_id"], state["group_id"])
    try:
        # Avatars are optional UI assets, never Agent context or game state.
        # Fetch only the human players; do not delay the transactional save.
        try:
            avatars = await AVATARS.get_many(
                player["id"] for player in state["players"].values()
                if player["id"] > 0 and player["id"] != state["bot_id"]
            )
        except Exception as exc:
            logger.debug(f"[minigame] group={key[1]} avatar_unavailable={type(exc).__name__}")
            avatars = {}
        image = await asyncio.to_thread(render_board, state, avatars=avatars)
        # A render can overlap cancellation or a new round. Serialize the
        # final version check/send with state changes to avoid stale boards.
        async with SERVICE.locks[key]:
            current = SERVICE.states.get(key)
            if not gate.allows(key[1]) or not current or current["version"] != state["version"]:
                return
            text = _caption(state)
            if result.message:
                text += "\n" + result.message
            await asyncio.wait_for(bot.send_group_msg(group_id=key[1],
                message=MessageSegment.image(image) + MessageSegment.text("\n"+text)), timeout=15)
    except Exception as exc:
        logger.warning(f"[minigame] group={key[1]} image_send_failed={type(exc).__name__}")
        # Never roll back a committed move just because transport failed.
        try:
            if gate.allows(key[1]):
                await asyncio.wait_for(bot.send_group_msg(group_id=key[1],
                    message="棋局已保存，图片暂时发送失败，请用 #棋盘 重试"), timeout=15)
        except Exception:
            logger.warning(f"[minigame] group={key[1]} fallback_send_failed")


async def _send_idiom_result(bot, result):
    state = result.state
    key = (state["bot_id"], state["group_id"])
    try:
        image = await asyncio.to_thread(render_idiom, dict(state, display_now=SERVICE.clock()))
        async with SERVICE.locks[key]:
            current = SERVICE.states.get(key)
            if (not SERVICE.ready or not gate.allows(key[1]) or not current
                    or current["session_id"] != state["session_id"]
                    or current["game"] != "idiom" or current["status"] != state["status"]
                    or current["question"]["id"] != state["question"]["id"]
                    or current.get("hint_mask") != state.get("hint_mask")
                    or current.get("hinted_at") != state.get("hinted_at")):
                return
            if current["status"] == "active" and current["expires_at"] <= SERVICE.clock():
                return
            caption = "本局已结束 · #成语填空 查看玩法" if state["status"] == "ended" else "直接发完整成语即可 · #作答 可获错答提示 · #题目 查看进度"
            send_started_at = SERVICE.clock()
            await asyncio.wait_for(bot.send_group_msg(group_id=key[1],
                message=MessageSegment.image(image) + MessageSegment.text("\n" + caption)), timeout=15)
            await SERVICE.presented_locked(key, current["session_id"], current["question"]["id"],
                                           SERVICE.clock(), send_started_at=send_started_at)
    except Exception as exc:
        logger.warning(f"[minigame] group={key[1]} idiom_send_or_timer_failed={type(exc).__name__}")
        try:
            if gate.allows(key[1]):
                await asyncio.wait_for(bot.send_group_msg(group_id=key[1],
                    message="对局已保留，题目发送或计时确认失败，请用 #题目 重试；未发送的新题不会开始倒计时"), timeout=15)
        except Exception:
            logger.warning(f"[minigame] group={key[1]} idiom_fallback_failed")


async def _send_number_result(bot, result):
    state = result.state
    key = (state["bot_id"], state["group_id"])
    try:
        image = await asyncio.to_thread(render_number, dict(state, display_now=SERVICE.clock()))
        async with SERVICE.locks[key]:
            current = SERVICE.states.get(key)
            if (not SERVICE.ready or not gate.allows(key[1]) or not current
                    or current["session_id"] != state["session_id"]
                    or current["game"] != "number" or current["version"] != state["version"]):
                return
            if current["status"] == "active" and current["expires_at"] <= SERVICE.clock():
                return
            caption = "本局已结束 · #猜数字 查看玩法" if state["status"] == "ended" else "#猜 1234 提交猜测 · #题目 查看记录"
            send_started_at = SERVICE.clock()
            await asyncio.wait_for(bot.send_group_msg(group_id=key[1],
                message=MessageSegment.image(image) + MessageSegment.text("\n" + caption)), timeout=15)
            await SERVICE.number_presented_locked(key, current["session_id"], current["round_id"],
                                                   SERVICE.clock(), send_started_at=send_started_at)
    except Exception as exc:
        logger.warning(f"[minigame] group={key[1]} number_send_or_timer_failed={type(exc).__name__}")
        try:
            if gate.allows(key[1]):
                await asyncio.wait_for(bot.send_group_msg(group_id=key[1],
                    message="对局已保留，图片发送或计时确认失败，请用 #题目 重试；首屏未确认不会开始计时"), timeout=15)
        except Exception:
            logger.warning(f"[minigame] group={key[1]} number_fallback_failed")


async def _reply(bot, event, message):
    try:
        await asyncio.wait_for(bot.send(event, message), timeout=15)
    except Exception as exc:
        logger.warning(f"[minigame] group={event.group_id} notice_send_failed={type(exc).__name__}")


async def _send_menu(bot, event, game):
    state = SERVICE.states.get((int(bot.self_id), int(event.group_id)))
    active = state if state and state["status"] != "ended" and state["expires_at"] > SERVICE.clock() else None
    try:
        image = await asyncio.to_thread(
            render_menu, _persona(), game=game, active_game=active,
            wait_seconds=SERVICE.wait_seconds, idle_seconds=SERVICE.idle_seconds,
            idiom_enabled=SERVICE.idiom_enabled,
            number_enabled=SERVICE.number_enabled,
            xiangqi_enabled=SERVICE.strategy_enabled["xiangqi"],
            go_enabled=SERVICE.strategy_enabled["go"],
        )
        await _reply(bot, event, MessageSegment.image(image))
    except Exception as exc:
        logger.warning(f"[minigame] group={event.group_id} menu_render_failed={type(exc).__name__}")
        title = GAME_NAMES.get(game) if game not in {"idiom", "number"} else None
        text = (f"#{title} 对战：人机开局，可加 娱乐 / 认真 / 后手\n"
                f"#{title} 双人 或 #{title} @群友：群友对战\n"
                "#加入游戏 / #落子 / #棋盘 / #悔棋 / #认输 / #结束游戏"
                if title else " / ".join(f"#{spec.name}" for spec in GAME_REGISTRY
                    if (spec.id != "idiom" or SERVICE.idiom_enabled) and
                       (spec.id != "number" or SERVICE.number_enabled) and
                       SERVICE.strategy_enabled.get(spec.id, True)) + " 查看玩法，菜单不会开局")
        if game == "idiom":
            text = "#成语填空 单人：10题计时\n#成语填空 抢答：全群先得10分获胜\n直接发完整成语：错答静默；#作答 答错会提示\n满60秒揭示一字，不换题；#跳过 揭晓答案（仅单人）/ #题目 / #结束游戏"
        elif game == "number":
            text = "#猜数字 单人：10分钟计步\n#猜数字 抢答：15分钟内首个4A获胜\n#猜 1234 / #题目 / #结束游戏；4个不同数字，首位不能为0"
        if active:
            text += "\n本群已有对局，发送 " + ("#题目" if active["game"] in {"idiom", "number"} else "#棋盘") + " 查看"
        await _reply(bot, event, text)


def _choose_move(game, board, side, difficulty, budget, flag):
    if game == "gomoku" and difficulty == "serious":
        return ENGINES.choose_rapfi(board, side, flag)
    return choose_move(game, board, side, difficulty, budget, flag)


async def _check_rapfi_ready():
    # Readiness probes use the same CPU admission pool as actual moves. Retain
    # the slot until cancellation has really stopped the blocking worker.
    async with _search_semaphore:
        flag = threading.Event()
        task = asyncio.create_task(asyncio.to_thread(ENGINES.check_rapfi, flag))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            flag.set()
            await asyncio.gather(task, return_exceptions=True)
            raise


async def _check_board_ready(game):
    async with _search_semaphore:
        flag = threading.Event()
        task = asyncio.create_task(asyncio.to_thread(ENGINES.check_ready, game, flag))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            flag.set()
            await asyncio.gather(task, return_exceptions=True)
            raise


def _needs_work(state):
    return bool(state and SERVICE.strategy_enabled.get(state["game"], True)
                and (SERVICE.needs_bot(state) or strategy.scoring_needed(state)))


async def _run_bot(key, bot):
    try:
        result = await SERVICE.bot_step(key, _choose_move, _search_semaphore, budget=1.0,
                                        strategy_choose=ENGINES.choose)
        scoring_result = await SERVICE.scoring_step(key, ENGINES.suggest_dead, _search_semaphore)
        result = scoring_result or result
        if result:
            _retry_after.pop(key, None)
            await _send_result(bot, result)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _retry_after[key] = time.monotonic() + 30
        logger.warning(f"[minigame] group={key[1]} bot_step_failed={type(exc).__name__}; retained_for_retry")
        state = SERVICE.states.get(key)
        if (state and (state.get("difficulty") == "serious" or state["game"] in strategy.GAMES) and gate.allows(key[1])
                and time.monotonic() >= _engine_notices.get(key, 0)):
            _engine_notices[key] = time.monotonic() + 120
            try:
                await asyncio.wait_for(bot.send_group_msg(group_id=key[1],
                    message="本地棋类引擎暂时不可用，棋局已保存，不会降级；稍后用 #棋盘 重试，或 #结束游戏"), 15)
            except Exception:
                pass
    finally:
        if _tasks.get(key) is asyncio.current_task():
            _tasks.pop(key, None)


def _schedule_bot(key, bot):
    if key not in _tasks and time.monotonic() >= _retry_after.get(key, 0):
        _tasks[key] = asyncio.create_task(_run_bot(key, bot))


@game_cmd.handle()
async def handle_game(bot: Bot, event: GroupMessageEvent):
    try:
        await _handle_game(bot, event)
    finally:
        gate.finish_answer(int(bot.self_id), int(event.group_id), int(event.message_id))


async def _handle_game(bot: Bot, event: GroupMessageEvent):
    bot_id, group_id, user_id = int(bot.self_id), int(event.group_id), int(event.user_id)
    arrival = gate.capture(bot_id, event)
    if not arrival or user_id == bot_id or not gate.allows(group_id):
        return
    if not _limit((bot_id, group_id, user_id), 1.0):
        return
    try:
        if any(segment.type not in {"text", "at", "reply"} for segment in event.message):
            raise GameError("请单独发送游戏指令或落子坐标，不要附带图片")
        mentions = [segment.data.get("qq") for segment in event.message if segment.type == "at"]
        command = (Command("answer", game="idiom", argument=arrival["answer_text"]) if arrival.get("bare_idiom")
                   else parse_command(arrival["text"], mentions, bot_id=bot_id, user_id=user_id))
        if command.action in {"menu", "view", "question"} and not _limit((bot_id, group_id, "query"), 5):
            return
        if command.action == "menu":
            await _send_menu(bot, event, command.game)
            return
        if not SERVICE.ready:
            raise GameError("小游戏存储尚未就绪，请稍后再试或联系管理员查看启动日志")
        if command.action in {"answer", "guess", "cancel", "skip"}:
            await gate.wait_answer_turn(bot_id, group_id, event.message_id)
        active = SERVICE.states.get((bot_id, group_id))
        idiom_start = command.action == "start" and command.game == "idiom"
        idiom_continue = (active and active["game"] == "idiom" and active["status"] == "active"
                          and command.action not in {"cancel", "start"})
        if SERVICE.idiom_enabled and (idiom_start or idiom_continue):
            loaded = await _ensure_idiom_bank()
            if not loaded and idiom_start:
                raise GameError(_idiom_last_error)
        if command.action == "start" and command.game == "gomoku" and command.mode == "bot" and command.difficulty == "serious":
            active = SERVICE.states.get((bot_id, group_id))
            if active and active["status"] != "ended" and active["expires_at"] > SERVICE.clock():
                raise GameError("本群已有对局，请先结束或等待完成")
            try:
                await _check_rapfi_ready()
            except RapfiUnavailable as exc:
                raise GameError(f"认真模式暂不可用：{exc}；可先发送 #五子棋 对战 娱乐") from None
        if command.action == "start" and command.game in strategy.GAMES:
            if not SERVICE.strategy_enabled[command.game]:
                raise GameError("此小游戏尚未启用")
            if active and active["status"] != "ended" and active["expires_at"] > SERVICE.clock():
                raise GameError("本群已有对局，请先结束或等待完成")
            if command.game == "xiangqi":
                from .xiangqi import ensure_available
                await asyncio.to_thread(ensure_available)
            if command.mode == "bot":
                try:
                    await _check_board_ready(command.game)
                except Exception as exc:
                    logger.warning(f"[minigame] engine_preflight_failed game={command.game} error={type(exc).__name__}")
                    raise GameError(f"{GAME_NAMES[command.game]}人机引擎尚不可用，请管理员运行 tools/check_board_engines.py 自检；可先尝试双人模式") from None
        invitee_name = ""
        if command.action == "start" and command.invitee:
            try:
                member = await asyncio.wait_for(bot.get_group_member_info(group_id=group_id, user_id=command.invitee), 5)
                if int(member.get("user_id", 0)) != command.invitee:
                    raise ValueError("member mismatch")
                invitee_name = str(member.get("card") or member.get("nickname") or command.invitee)[:40]
            except Exception:
                raise GameError("无法确认邀请对象在本群，请稍后重试") from None
        sender = event.sender
        name = str(getattr(sender, "card", "") or getattr(sender, "nickname", "") or user_id)
        is_admin = user_id in ADMIN_USERS or getattr(sender, "role", "") in {"owner", "admin"}
        result = await SERVICE.execute(bot_id, group_id, user_id, name, command, arrival,
            message_id=event.message_id, is_admin=is_admin,
            persona=_persona() if command.action == "start" else None, invitee_name=invitee_name)
        if result.silent:
            return
        if result.state is None:
            if result.message:
                await _reply(bot, event, result.message)
            return
        key = (bot_id, group_id)
        if command.action == "start":
            # Retry/notice backoff belongs to the previous game, not this
            # group's lifetime. A saved new game must not inherit a failed
            # serious engine's cooldown, especially when the bot moves first.
            _retry_after.pop(key, None)
            _engine_notices.pop(key, None)
        if _needs_work(result.state) and command.action != "view":
            _schedule_bot(key, bot)
        else:
            await _send_result(bot, result)
            if _needs_work(result.state) and command.action == "view":
                _retry_after.pop(key, None)
                _schedule_bot(key, bot)
        logger.info(f"[minigame] group={group_id} action={command.action} version={result.state['version'] if result.state else '-'}")
    except ValueError as exc:
        if not arrival.get("bare_idiom"):
            await _reply(bot, event, str(exc))
    except Exception as exc:
        logger.warning(f"[minigame] group={group_id} operation_failed={type(exc).__name__}")
        if not arrival.get("bare_idiom"):
            await _reply(bot, event, "对局暂时不可用，未确认操作成功；答题请用 #题目，棋类请用 #棋盘 查看")


async def _tick():
    if not SERVICE.ready:
        return
    for result in await SERVICE.expire(pending_answer=gate.has_pending_answer):
        if result.state["game"] == "idiom" and not SERVICE.idiom_enabled:
            continue
        if result.state["game"] == "number" and not SERVICE.number_enabled:
            continue
        if not SERVICE.strategy_enabled.get(result.state["game"], True):
            continue
        try:
            bot = get_bot(str(result.state["bot_id"]))
        except KeyError:
            continue
        await _send_result(bot, result)
    for key, state in list(SERVICE.states.items()):
        if gate.allows(key[1]) and _needs_work(state):
            try:
                bot = get_bot(str(key[0]))
            except KeyError:
                continue
            _schedule_bot(key, bot)


async def _tick_loop():
    while True:
        await asyncio.sleep(5)
        try:
            await _tick()
        except Exception as exc:
            logger.warning(f"[minigame] tick_failed={type(exc).__name__}")


driver = get_driver()


@driver.on_startup
async def _startup():
    global _ticker
    if not ENABLED:
        return
    try:
        if SERVICE.idiom_enabled:
            await _ensure_idiom_bank(force=True)
        await SERVICE.initialize()
        for key in SERVICE.recovery_errors:
            logger.error(f"[minigame] group={key[1]} xiangqi_recovery_unavailable; session_preserved")
        logger.info(f"[minigame] storage_ready sessions={len(SERVICE.states)}")
        _ticker = asyncio.create_task(_tick_loop())
    except Exception as exc:
        logger.error(f"[minigame] recovery_failed={type(exc).__name__}; refusing_to_overwrite_sessions")


@driver.on_bot_connect
async def _connected(bot: Bot):
    if SERVICE.ready:
        for key, state in list(SERVICE.states.items()):
            if key[0] == int(bot.self_id) and gate.allows(key[1]) and _needs_work(state):
                _schedule_bot(key, bot)


@driver.on_shutdown
async def _shutdown():
    global _ticker
    if _ticker:
        _ticker.cancel()
        await asyncio.gather(_ticker, return_exceptions=True)
        _ticker = None
    for key in list(SERVICE.searches):
        SERVICE.cancel_search(key)
    tasks = list(_tasks.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await AVATARS.close()
    await asyncio.to_thread(ENGINES.close)
