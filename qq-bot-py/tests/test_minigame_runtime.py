"""Exercise real plugin handlers against isolated stores and fake QQ transport."""
import asyncio
from collections import OrderedDict
from copy import deepcopy
import time

import nonebot
import pytest

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(driver="~fastapi", log_level="ERROR")

from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message
from plugins import minigame_gate as gate
from plugins import minigames as runtime
from plugins.minigames.service import GameService


class Store:
    def __init__(self):
        self.rows = {}
    async def initialize(self):
        pass
    async def load_all(self):
        return deepcopy(list(self.rows.values()))
    async def load(self, *key):
        return deepcopy(self.rows.get(key))
    async def save(self, state, expected):
        self.rows[(state["bot_id"], state["group_id"])] = deepcopy(state)


class Bot:
    self_id = "900"
    def __init__(self):
        self.sent = []
        self.fail_image = False
    async def send_group_msg(self, *, group_id, message):
        if self.fail_image and not isinstance(message, str):
            raise RuntimeError("simulated transport failure")
        self.sent.append((group_id,message))
        return {"message_id":len(self.sent)}
    async def send(self,event,message):
        return await self.send_group_msg(group_id=event.group_id,message=message)


class Avatars:
    def __init__(self):
        self.requests = []
    async def get_many(self, user_ids):
        ids = set(user_ids)
        self.requests.append(ids)
        return {uid: b"avatar" for uid in ids}
    async def close(self):
        pass


def message(text,mid,user=11,group=100):
    return GroupMessageEvent(time=int(time.time()),self_id=900,post_type="message",
        sub_type="normal",user_id=user,group_id=group,message_type="group",message_id=mid,
        message=Message(text),raw_message=text,font=0,sender={"user_id":user,"nickname":"player"})


@pytest.fixture
def isolated(monkeypatch):
    monkeypatch.setattr(gate,"_states",{})
    monkeypatch.setattr(gate,"_arrivals",OrderedDict())
    monkeypatch.setattr(gate,"_answer_arrivals",OrderedDict())
    monkeypatch.setattr(gate,"_enabled",True)
    monkeypatch.setattr(gate,"_allowed",frozenset())
    monkeypatch.setattr(gate,"_blocked",frozenset({999}))
    service=GameService(Store(),publish=gate.publish,allowed=gate.allows)
    monkeypatch.setattr(runtime,"SERVICE",service)
    monkeypatch.setattr(runtime,"_tasks",{})
    monkeypatch.setattr(runtime,"_retry_after",{})
    monkeypatch.setattr(runtime,"_engine_notices",{})
    monkeypatch.setattr(runtime,"_rates",OrderedDict())
    monkeypatch.setattr(runtime,"_search_semaphore",asyncio.Semaphore(2))
    monkeypatch.setattr(runtime,"_idiom_load_lock",asyncio.Lock())
    monkeypatch.setattr(runtime,"_idiom_retry_after",0.0)
    monkeypatch.setattr(runtime,"_persona",lambda: {"id":"test","name":"机器人","avatar":""})
    monkeypatch.setattr(runtime,"_limit",lambda *args: True)
    monkeypatch.setattr(runtime,"AVATARS",Avatars())
    monkeypatch.setattr(runtime,"render_board",lambda state, **kwargs: b"test-image")
    monkeypatch.setattr(runtime,"render_menu",lambda *args, **kwargs: b"menu-image")
    monkeypatch.setattr(runtime,"render_number",lambda state: b"number-image")
    monkeypatch.setattr(runtime,"choose_move",lambda game,board,*args: next(i for i,v in enumerate(board) if not v))
    return service,Bot()


def test_handler_combines_human_and_bot_move_in_one_image(isolated):
    service,bot=isolated
    async def scenario():
        await service.initialize()
        await runtime.handle_game(bot,message("#小游戏",1))
        assert not service.states and len(bot.sent)==1
        await runtime.handle_game(bot,message("#井字棋 对战",2))
        assert len(bot.sent)==2
        await runtime.handle_game(bot,message("5",3))
        await asyncio.gather(*list(runtime._tasks.values()))
        assert len(bot.sent)==3
        assert len(service.states[(900,100)]["moves"])==2
        await runtime.handle_game(bot,message("5",3))
        assert len(bot.sent)==3
        assert runtime.game_cmd.priority == -1 and runtime.game_cmd.block
    asyncio.run(scenario())


def test_failed_image_keeps_saved_state_and_view_can_retry(isolated):
    service,bot=isolated
    async def scenario():
        await service.initialize()
        bot.fail_image=True
        await runtime.handle_game(bot,message("#井字棋 对战",1))
        assert service.states[(900,100)]["status"]=="active"
        assert "棋局已保存" in bot.sent[-1][1]
        bot.fail_image=False
        version=service.states[(900,100)]["version"]
        await runtime.handle_game(bot,message("#棋盘",2))
        assert service.states[(900,100)]["version"]==version
        assert len(bot.sent)==2
    asyncio.run(scenario())


def test_forbidden_groups_and_nonplayer_shorthand_ignored(isolated):
    service,bot=isolated
    async def scenario():
        await service.initialize()
        await runtime.handle_game(bot,message("#井字棋 对战",1,group=999))
        assert not service.states and not bot.sent
        await runtime.handle_game(bot,message("#井字棋 对战",2))
        assert not await runtime._rule(bot,message("5",3,user=22))
        assert not await runtime._rule(bot,message("hello 5",4))
        assert await runtime._rule(bot,message("5",5))
    asyncio.run(scenario())


def test_reconnected_bot_schedules_only_one_recovery_move(isolated):
    service,bot=isolated
    async def scenario():
        await service.initialize()
        await runtime.handle_game(bot,message("#井字棋 后手",1))
        await runtime._connected(bot)
        await runtime._connected(bot)
        await asyncio.gather(*list(runtime._tasks.values()))
        assert len(service.states[(900,100)]["moves"])==1
        assert len(bot.sent)==1
    asyncio.run(scenario())


def test_tutorials_need_no_storage_and_cannot_change_a_game(isolated, monkeypatch):
    service, bot = isolated
    pages = []
    def render(*args, **kwargs):
        pages.append((args, kwargs))
        return b"menu"
    monkeypatch.setattr(runtime, "render_menu", render)
    async def scenario():
        assert not service.ready
        for mid, text in enumerate(("#小游戏", "#五子棋", "＃小游戏 井字棋"), 1):
            await runtime.handle_game(bot, message(text, mid))
        assert not service.states and not service.store.rows
        assert [kwargs["game"] for _, kwargs in pages] == ["", "gomoku", "tictactoe"]
        assert runtime.AVATARS.requests == []
        await service.initialize()
        await runtime.handle_game(bot, message("#井字棋 对战", 4))
        before = deepcopy(service.states[(900, 100)])
        monkeypatch.setattr(runtime, "_persona", lambda: {"id": "rin", "name": "凛", "avatar": ""})
        await runtime.handle_game(bot, message("#五子棋", 5))
        assert service.states[(900, 100)] == before
        assert service.store.rows[(900, 100)] == before
        assert pages[-1][0][0]["id"] == "rin"
        assert pages[-1][1]["active_game"]["session_id"] == before["session_id"]
        assert before["persona_id"] == "test"
        assert len(bot.sent) == 5
    asyncio.run(scenario())


def test_all_query_pages_share_a_rate_limit(isolated, monkeypatch):
    service, bot = isolated
    # Restore the real limiter with a deterministic clock, without affecting
    # wall-clock expiry or mocking asynchronous scheduling.
    def limiter(key, interval):
        now = clock[0]
        if now-runtime._rates.get(key, -float("inf")) < interval:
            return False
        runtime._rates[key] = now
        return True
    clock = [100.0]
    monkeypatch.setattr(runtime, "_limit", limiter)
    async def scenario():
        await service.initialize()
        await runtime.handle_game(bot, message("#井字棋 对战", 1))
        clock[0] += 1
        await runtime.handle_game(bot, message("#五子棋", 2))
        for mid, text in enumerate(("#小游戏", "#井字棋", "#棋盘"), 3):
            clock[0] += 1
            await runtime.handle_game(bot, message(text, mid))
        assert len(bot.sent) == 2
        clock[0] += 2
        await runtime.handle_game(bot, message("#棋盘", 6))
        assert len(bot.sent) == 3
    asyncio.run(scenario())


def test_only_game_players_avatars_are_prepared_and_passed_to_renderer(isolated, monkeypatch):
    service, bot = isolated
    received = []
    def render(state, avatars=None):
        received.append((deepcopy(state), avatars))
        return b"board"
    monkeypatch.setattr(runtime, "render_board", render)
    async def scenario():
        await service.initialize()
        await runtime.handle_game(bot, message("#五子棋 双人", 1))
        assert runtime.AVATARS.requests[-1] == {11}
        await runtime.handle_game(bot, message("#加入游戏", 2, user=22))
        assert runtime.AVATARS.requests[-1] == {11, 22}
        assert received[-1][1] == {11: b"avatar", 22: b"avatar"}
        await runtime.handle_game(bot, message("#井字棋 对战", 3, group=101))
        assert runtime.AVATARS.requests[-1] == {11}
        assert 900 not in received[-1][1]
    asyncio.run(scenario())


def test_avatar_failure_keeps_board_and_saved_state(isolated, monkeypatch):
    service, bot = isolated
    async def fail(_):
        raise RuntimeError("simulated avatar failure")
    monkeypatch.setattr(runtime.AVATARS, "get_many", fail)
    rendered = []
    def render(state, avatars=None):
        rendered.append(avatars)
        return b"board"
    monkeypatch.setattr(runtime, "render_board", render)
    async def scenario():
        await service.initialize()
        await runtime.handle_game(bot, message("#井字棋 对战", 1))
        assert service.store.rows[(900, 100)]["status"] == "active"
        assert rendered == [{}] and len(bot.sent) == 1
        assert not isinstance(bot.sent[0][1], str)
    asyncio.run(scenario())


def test_menu_render_failure_has_a_help_fallback_not_a_save_claim(isolated, monkeypatch):
    service, bot = isolated
    def fail(*args, **kwargs):
        raise ValueError("simulated invalid illustration")
    monkeypatch.setattr(runtime, "render_menu", fail)
    async def scenario():
        await runtime.handle_game(bot, message("#五子棋", 1))
        assert not service.store.rows
        assert "#五子棋 对战" in bot.sent[0][1]
        assert "棋局已保存" not in bot.sent[0][1]
    asyncio.run(scenario())


def test_cancel_during_avatar_download_cannot_publish_a_stale_board(isolated, monkeypatch):
    service, bot = isolated
    async def scenario():
        await service.initialize()
        await runtime.handle_game(bot, message("#井字棋 对战", 1))
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(user_ids):
            entered.set()
            await release.wait()
            return {}
        monkeypatch.setattr(runtime.AVATARS, "get_many", slow)
        viewing = asyncio.create_task(runtime.handle_game(bot, message("#棋盘", 2)))
        await entered.wait()
        # Skip image sending so cancellation completes without waiting on the
        # intentionally stalled asset fetch.
        arrival = gate.capture(900, message("#结束游戏", 3))
        from plugins.minigames.commands import parse_command
        await service.execute(900, 100, 11, "player", parse_command("#结束游戏"),
                              arrival, message_id=3)
        release.set()
        await viewing
        assert service.states[(900, 100)]["status"] == "ended"
        assert len(bot.sent) == 1
    asyncio.run(scenario())


def _enable_idioms(service, monkeypatch):
    from plugins.minigames.idioms import IdiomBank
    service.idiom_bank = IdiomBank(["春" + chr(0x4e10+i) + "花" + chr(0x4f10+i) for i in range(50)])
    monkeypatch.setattr(runtime, "render_idiom", lambda state: b"idiom-card")


def test_idiom_send_failure_does_not_start_timer_and_query_retries(isolated, monkeypatch):
    service, bot = isolated
    _enable_idioms(service, monkeypatch)
    async def scenario():
        await service.initialize()
        bot.fail_image = True
        await runtime.handle_game(bot, message("#成语填空 单人", 1))
        current = service.states[(900, 100)]
        assert current["question_started_at"] is None and current["started_at"] is None
        assert "#题目" in bot.sent[-1][1]
        bot.fail_image = False
        await runtime.handle_game(bot, message("#题目", 2))
        shown = service.states[(900, 100)]
        assert shown["question_started_at"] is not None
        deadline = shown["question_deadline"]
        await runtime.handle_game(bot, message("#题目", 3))
        assert service.states[(900, 100)]["question_deadline"] == deadline
        assert runtime.AVATARS.requests == []
    asyncio.run(scenario())


def test_race_orders_by_capture_before_awaited_preprocessors(isolated, monkeypatch):
    service, bot = isolated
    _enable_idioms(service, monkeypatch)
    async def scenario():
        await service.initialize()
        await runtime.handle_game(bot, message("#成语填空 抢答", 1))
        word = service.states[(900, 100)]["question"]["answers"][0]
        earlier = message("#作答 " + word, 777, user=22)
        later = message("#作答 " + word, 2, user=33)  # message ID is not chronological
        gate.capture(900, earlier)
        gate.capture(900, later)
        task = asyncio.create_task(runtime.handle_game(bot, later))
        await asyncio.sleep(0)
        assert not task.done()
        await runtime.handle_game(bot, earlier)
        await task
        current = service.states[(900, 100)]
        assert current["scores"].get("22") == 1 and "33" not in current["scores"]
        assert current["correct_count"] == 1 and current["question_no"] == 2
        assert len(bot.sent) == 2 and not gate._answer_arrivals
    asyncio.run(scenario())


def test_race_bare_wrong_silent_explicit_wrong_reports_without_advancing(isolated, monkeypatch):
    service, bot = isolated
    _enable_idioms(service, monkeypatch)
    async def scenario():
        await service.initialize()
        await runtime.handle_game(bot, message("#成语填空 抢答", 1))
        assert not await runtime._rule(bot, message("今天在玩什么", 9, user=22))
        assert await runtime._rule(bot, message("乱写答案", 2, user=22))
        before = deepcopy(service.states[(900, 100)])
        await runtime.handle_game(bot, message("乱写答案", 2, user=22))
        assert len(bot.sent) == 1 and not gate._answer_arrivals
        await runtime.handle_game(bot, message("#作答 乱写答案", 3, user=22))
        assert len(bot.sent) == 2 and "答错" in bot.sent[-1][1] and not gate._answer_arrivals
        assert service.states[(900, 100)]["question_no"] == 1
        for field in ("question", "deadline", "question_deadline", "scores", "correct_count", "timed_out_count"):
            assert service.states[(900, 100)][field] == before[field]
    asyncio.run(scenario())


@pytest.mark.parametrize("mode,user", [("单人", 11), ("抢答", 22)])
def test_direct_correct_idiom_advances_once_without_creating_agent_task(isolated, monkeypatch, mode, user):
    service, bot = isolated
    _enable_idioms(service, monkeypatch)
    async def scenario():
        await service.initialize()
        await runtime.handle_game(bot, message("#成语填空 " + mode, 1))
        target = service.states[(900, 100)]["question"]["answers"][0]
        event = message("「" + target + "！」", 2, user=user)
        assert await runtime._rule(bot, event)
        await runtime.handle_game(bot, event)
        current = service.states[(900, 100)]
        assert current["correct_count"] == 1 and current["question_no"] == 2
        assert len(bot.sent) == 2 and not runtime._tasks and not gate._answer_arrivals
        await runtime.handle_game(bot, event)
        assert len(bot.sent) == 2
    asyncio.run(scenario())


def test_hint_tick_remains_same_question_and_skip_reveals_answer(isolated, monkeypatch):
    service, bot = isolated
    _enable_idioms(service, monkeypatch)
    clock, rendered = [1000.0], []
    service.clock = lambda: clock[0]
    monkeypatch.setattr(gate.time, "time", lambda: clock[0])
    monkeypatch.setattr(runtime, "get_bot", lambda *_: bot)
    def render(state):
        rendered.append(deepcopy(state))
        return b"idiom-image"
    monkeypatch.setattr(runtime, "render_idiom", render)
    async def scenario():
        await service.initialize()
        await runtime.handle_game(bot, message("#成语填空 单人", 1))
        original = deepcopy(service.states[(900, 100)])
        clock[0] += 61
        await runtime._tick()
        current = service.states[(900, 100)]
        assert current["question"] == original["question"]
        assert current["hint_mask"].count("□") == 1
        assert current["question_no"] == 1 and current["timed_out_count"] == 0
        assert len(bot.sent) == 2
        clock[0] += 60
        await runtime._tick()
        assert len(bot.sent) == 2
        await runtime.handle_game(bot, message("#跳过", 2))
        current = service.states[(900, 100)]
        assert current["question_no"] == 2 and current["skipped_count"] == 1
        assert rendered[-1]["last_result"]["kind"] == "skipped"
        assert rendered[-1]["last_result"]["answers"] == original["question"]["answers"]
        assert len(bot.sent) == 3
    asyncio.run(scenario())


def test_failed_hint_send_keeps_single_hint_and_query_recovers(isolated, monkeypatch):
    service, bot = isolated
    _enable_idioms(service, monkeypatch)
    clock = [1000.0]
    service.clock = lambda: clock[0]
    monkeypatch.setattr(gate.time, "time", lambda: clock[0])
    monkeypatch.setattr(runtime, "get_bot", lambda *_: bot)
    async def scenario():
        await service.initialize()
        await runtime.handle_game(bot, message("#成语填空 单人", 1))
        bot.fail_image = True
        clock[0] += 61
        await runtime._tick()
        hinted = deepcopy(service.states[(900, 100)])
        assert hinted["question_no"] == 1 and hinted["hinted_at"] == 1061
        assert "#题目" in bot.sent[-1][1]
        bot.fail_image = False
        clock[0] += 30
        await runtime.handle_game(bot, message("#题目", 2))
        assert service.states[(900, 100)] == hinted
        await runtime.handle_game(bot, message(hinted["question"]["word"], 3))
        assert service.states[(900, 100)]["correct_count"] == 1
    asyncio.run(scenario())


def test_bare_answer_and_explicit_answer_use_the_same_arrival_order(isolated, monkeypatch):
    service, bot = isolated
    _enable_idioms(service, monkeypatch)
    async def scenario():
        await service.initialize()
        await runtime.handle_game(bot, message("#成语填空 抢答", 1))
        target = service.states[(900, 100)]["question"]["word"]
        earlier, later = message(target, 600, user=22), message("#作答 " + target, 2, user=33)
        gate.capture(900, earlier)
        gate.capture(900, later)
        task = asyncio.create_task(runtime.handle_game(bot, later))
        await asyncio.sleep(0)
        assert not task.done()
        await runtime.handle_game(bot, earlier)
        await task
        assert service.states[(900, 100)]["scores"].get("22") == 1
        assert "33" not in service.states[(900, 100)]["scores"]
        assert len(bot.sent) == 2 and not gate._answer_arrivals
    asyncio.run(scenario())


def test_answer_received_before_delayed_send_ack_is_accepted(isolated, monkeypatch):
    service, bot = isolated
    _enable_idioms(service, monkeypatch)
    clock = [1000.0]
    service.clock = lambda: clock[0]
    monkeypatch.setattr(gate.time, "time", lambda: clock[0])
    original_send = bot.send_group_msg
    async def scenario():
        visible, ack = asyncio.Event(), asyncio.Event()
        calls = 0
        async def delayed_ack(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                visible.set()
                await ack.wait()
            return await original_send(**kwargs)
        monkeypatch.setattr(bot, "send_group_msg", delayed_ack)
        await service.initialize()
        starting = asyncio.create_task(runtime.handle_game(bot, message("#成语填空 抢答", 1)))
        await visible.wait()
        assert service.states[(900, 100)]["question_started_at"] is None
        clock[0] = 1001.0
        word = service.states[(900, 100)]["question"]["answers"][0]
        answering = asyncio.create_task(runtime.handle_game(bot, message("#作答 " + word, 2, user=22)))
        await asyncio.sleep(0)
        assert not answering.done()
        clock[0] = 1003.0
        ack.set()
        await asyncio.gather(starting, answering)
        state = service.states[(900, 100)]
        assert state["started_at"] == 1003.0
        assert state["scores"]["22"] == 1
        assert state["question_no"] == 2 and len(bot.sent) == 2
    asyncio.run(scenario())


def test_serious_engine_missing_cannot_create_a_game_or_fall_back(isolated, monkeypatch):
    service, bot = isolated
    def unavailable(*args):
        raise runtime.RapfiUnavailable("未安装引擎")
    monkeypatch.setattr(runtime.ENGINES, "check_rapfi", unavailable)
    async def scenario():
        await service.initialize()
        await runtime.handle_game(bot, message("#五子棋 对战 认真", 1))
        assert not service.store.rows
        assert "认真模式暂不可用" in bot.sent[-1][1]
        await runtime.handle_game(bot, message("#五子棋 对战 娱乐", 2))
        assert service.states[(900, 100)]["difficulty"] == "casual"
    asyncio.run(scenario())


def test_serious_search_uses_external_engine_and_legacy_normal_stays_local(monkeypatch):
    calls = []
    class Engine:
        def choose_rapfi(self, board, side, flag):
            calls.append(("rapfi", side))
            return 112
    monkeypatch.setattr(runtime, "ENGINES", Engine())
    monkeypatch.setattr(runtime, "choose_move", lambda *a: calls.append(("local", a[3])) or 0)
    assert runtime._choose_move("gomoku", [0]*225, 1, "serious", 1, None) == 112
    assert runtime._choose_move("gomoku", [0]*225, 1, "normal", 1, None) == 0
    assert runtime._choose_move("tictactoe", [0]*9, 1, "serious", 1, None) == 0
    assert calls == [("rapfi", 1), ("local", "normal"), ("local", "serious")]


@pytest.mark.parametrize("new_game", ["#五子棋 对战 娱乐 后手", "#井字棋 对战 娱乐 后手"])
def test_failed_serious_backoff_cannot_delay_a_new_casual_game(isolated, monkeypatch, new_game):
    service, bot = isolated
    class FailingEngine:
        def check_rapfi(self, flag=None):
            pass
        def choose_rapfi(self, *args):
            raise runtime.RapfiUnavailable("simulated engine failure after readiness")
        def choose(self, *args):
            raise AssertionError("not a strategy game")
        def suggest_dead(self, *args):
            raise AssertionError("not a Go game")
    monkeypatch.setattr(runtime, "ENGINES", FailingEngine())
    async def scenario():
        key = (900, 100)
        await service.initialize()
        await runtime.handle_game(bot, message("#五子棋 对战 认真 后手", 1))
        await asyncio.gather(*list(runtime._tasks.values()))
        old_session = service.states[key]["session_id"]
        assert runtime._retry_after[key] > time.monotonic()
        assert runtime._engine_notices[key] > time.monotonic()
        previous_retry = runtime._retry_after[key]
        # A rejected start must not remove the current game's backoff.
        await runtime.handle_game(bot, message(new_game, 2))
        assert service.states[key]["session_id"] == old_session
        assert runtime._retry_after[key] == previous_retry
        await runtime.handle_game(bot, message("#结束游戏", 3))
        before = len(bot.sent)
        await runtime.handle_game(bot, message(new_game, 4))
        assert service.states[key]["session_id"] != old_session
        assert key not in runtime._retry_after and key not in runtime._engine_notices
        assert key in runtime._tasks  # Immediately scheduled, no 30-second wait.
        await asyncio.gather(*list(runtime._tasks.values()))
        assert service.states[key]["difficulty"] == "casual"
        assert len(service.states[key]["moves"]) == 1
        assert len(bot.sent) == before + 1
    asyncio.run(scenario())


def test_missing_idiom_files_are_reported_and_reupload_recovers_without_restart(isolated, monkeypatch):
    service, bot = isolated
    calls = []
    def missing(_):
        calls.append("missing")
        raise runtime.IdiomBankError("缺少 THUOCL_chengyu.txt", code="missing_file", filename="THUOCL_chengyu.txt")
    monkeypatch.setattr(runtime, "load_default", missing)
    async def scenario():
        await service.initialize()
        await runtime.handle_game(bot, message("#成语填空 单人", 1))
        assert not service.states and "THUOCL_chengyu.txt" in bot.sent[-1][1]
        await runtime.handle_game(bot, message("#成语填空 单人", 2))
        assert calls == ["missing"]  # failed loads have a cooldown
        _enable_idioms(service, monkeypatch)
        bank = service.idiom_bank
        service.idiom_bank = None
        monkeypatch.setattr(runtime, "load_default", lambda _: bank)
        monkeypatch.setattr(runtime, "_idiom_retry_after", 0.0)
        await runtime.handle_game(bot, message("#成语填空 单人", 3))
        assert service.states[(900, 100)]["question"]["mask"].count("□") == 2
        assert service.states[(900, 100)]["started_at"] is not None
    asyncio.run(scenario())


def test_number_menu_needs_no_db_and_normal_digits_are_not_captured(isolated):
    service, bot = isolated
    async def scenario():
        await runtime.handle_game(bot, message("#猜数字", 1))
        assert len(bot.sent) == 1 and not service.states
        await service.initialize()
        await runtime.handle_game(bot, message("#猜数字 单人", 2))
        assert service.states[(900, 100)]["game"] == "number"
        assert service.states[(900, 100)]["started_at"] is not None
        assert not await runtime._rule(bot, message("1234", 3))
        assert not await runtime._rule(bot, message("5", 4))
        assert not await runtime._rule(bot, message("H8", 5))
        assert runtime.AVATARS.requests == [] and not runtime._tasks
    asyncio.run(scenario())


@pytest.mark.parametrize("command,game", [("#五子棋 对战", "gomoku"), ("#猜数字 单人", "number")])
def test_missing_bank_in_finished_idiom_cannot_block_other_games(isolated, monkeypatch, command, game):
    service, bot = isolated
    _enable_idioms(service, monkeypatch)
    def unavailable(_):
        raise AssertionError("unrelated new game must not attempt a corpus load")
    async def scenario():
        await service.initialize()
        await runtime.handle_game(bot, message("#成语填空 单人", 1))
        await runtime.handle_game(bot, message("#结束游戏", 2))
        service.idiom_bank = None
        monkeypatch.setattr(runtime, "load_default", unavailable)
        await runtime.handle_game(bot, message(command, 3))
        assert service.states[(900, 100)]["game"] == game
        assert service.states[(900, 100)]["status"] == "active"
    asyncio.run(scenario())


def test_number_failed_send_query_retry_and_guess_persist_once(isolated):
    service, bot = isolated
    async def scenario():
        await service.initialize()
        bot.fail_image = True
        await runtime.handle_game(bot, message("#猜数字 单人", 1))
        secret = service.states[(900, 100)]["secret"]
        assert secret not in str(bot.sent)
        assert service.states[(900, 100)]["started_at"] is None
        bot.fail_image = False
        await runtime.handle_game(bot, message("#题目", 2))
        deadline = service.states[(900, 100)]["deadline"]
        await runtime.handle_game(bot, message("#题目", 3))
        assert service.states[(900, 100)]["deadline"] == deadline
        guess = secret[:2] + secret[3] + secret[2]
        bot.fail_image = True
        await runtime.handle_game(bot, message("#猜" + guess, 4))
        assert service.states[(900, 100)]["total_attempts"] == 1
        assert secret not in str(bot.sent)
        await runtime.handle_game(bot, message("#猜" + guess, 4))
        assert service.states[(900, 100)]["total_attempts"] == 1
        bot.fail_image = False
        await runtime.handle_game(bot, message("#作答" + secret, 5))
        assert service.states[(900, 100)]["winner_id"] == 11
    asyncio.run(scenario())


def test_number_race_capture_order_not_message_id_or_handler_start(isolated):
    service, bot = isolated
    async def scenario():
        await service.initialize()
        await runtime.handle_game(bot, message("#猜数字 抢答", 1))
        secret = service.states[(900, 100)]["secret"]
        earlier = message("#猜 " + secret, 9001, user=22)
        later = message("#作答 " + secret, 3, user=33)
        gate.capture(900, earlier)
        gate.capture(900, later)
        task = asyncio.create_task(runtime.handle_game(bot, later))
        await asyncio.sleep(0)
        assert not task.done()
        await runtime.handle_game(bot, earlier)
        await task
        assert service.states[(900, 100)]["winner_id"] == 22
        assert service.states[(900, 100)]["total_attempts"] == 1
        assert len(bot.sent) == 2 and not gate._answer_arrivals
    asyncio.run(scenario())


def test_number_cancel_cannot_overtake_earlier_winning_guess(isolated):
    service, bot = isolated
    async def scenario():
        await service.initialize()
        await runtime.handle_game(bot, message("#猜数字 抢答", 1))
        secret = service.states[(900, 100)]["secret"]
        earlier = message("#猜 " + secret, 2, user=22)
        later = message("#结束游戏", 3)
        gate.capture(900, earlier)
        gate.capture(900, later)
        cancelling = asyncio.create_task(runtime.handle_game(bot, later))
        await asyncio.sleep(0)
        assert not cancelling.done()
        await runtime.handle_game(bot, earlier)
        await cancelling
        assert service.states[(900, 100)]["winner_id"] == 22
        assert service.states[(900, 100)]["end_reason"] == "win"
        assert not gate._answer_arrivals
    asyncio.run(scenario())


def test_idiom_skip_cannot_overtake_earlier_correct_answer_or_skip_next_question(isolated, monkeypatch):
    service, bot = isolated
    _enable_idioms(service, monkeypatch)
    async def scenario():
        await service.initialize()
        await runtime.handle_game(bot, message("#成语填空 单人", 1))
        target = service.states[(900, 100)]["question"]["answers"][0]
        earlier = message("#作答 " + target, 2)
        later = message("#跳过", 3)
        gate.capture(900, earlier)
        gate.capture(900, later)
        skipping = asyncio.create_task(runtime.handle_game(bot, later))
        await asyncio.sleep(0)
        assert not skipping.done()
        await runtime.handle_game(bot, earlier)
        await skipping
        state = service.states[(900, 100)]
        assert state["correct_count"] == 1 and state["skipped_count"] == 0
        assert state["question_no"] == 2 and not gate._answer_arrivals
    asyncio.run(scenario())
