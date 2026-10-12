"""Follow-up boundaries use fake sessions and providers, never a live service."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import nonebot
import pytest
from sqlalchemy import Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class FakeBase(DeclarativeBase):
    pass


class FakeFollowup(FakeBase):
    __tablename__ = "fake_followup_boundaries"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(Integer)
    user_id: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24))

    # The fake session does not flush; other fields need no SQL representation.
    def __init__(self, **values):
        for key, value in values.items():
            setattr(self, key, value)


@pytest.fixture
def isolated(monkeypatch):
    package_name = "_followup_boundary_tests"
    package = ModuleType(package_name)
    package.__path__ = [str(Path(__file__).resolve().parents[1] / "plugins")]
    monkeypatch.setitem(sys.modules, package_name, package)

    session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: None))),
        get=AsyncMock(), add=Mock(), commit=AsyncMock(), close=AsyncMock(),
    )
    database = ModuleType(package_name + ".db")
    database.AgentFollowup, database.Base = FakeFollowup, FakeBase
    database.engine = SimpleNamespace()
    database.get_session = AsyncMock(return_value=session)
    monkeypatch.setitem(sys.modules, database.__name__, database)

    console = ModuleType(package_name + ".console_runtime")
    console.ready = lambda: False
    console.check_agent_action = AsyncMock()
    @asynccontextmanager
    async def chat_turn(*args, **kwargs):
        yield {}
    console.chat_turn = chat_turn
    monkeypatch.setitem(sys.modules, console.__name__, console)

    runtime = ModuleType(package_name + ".agent_runtime")
    runtime.RUNTIME = SimpleNamespace(policy=SimpleNamespace(max_context_messages=500),
                                     _decide=AsyncMock(return_value=([], {"action": "ignore"})))
    runtime._recent_group_context = AsyncMock(return_value="最近群聊背景")
    monkeypatch.setitem(sys.modules, runtime.__name__, runtime)
    memory = ModuleType(package_name + ".agent_memory_v2")
    memory.MEMORY_V2 = SimpleNamespace(recall=AsyncMock(return_value=""))
    monkeypatch.setitem(sys.modules, memory.__name__, memory)

    metrics = ModuleType(package_name + ".agent_metrics")
    metrics.record_agent_messages = AsyncMock()
    monkeypatch.setitem(sys.modules, metrics.__name__, metrics)
    chat = ModuleType(package_name + ".ai_chat")
    chat.record_bot_reply = AsyncMock()
    monkeypatch.setitem(sys.modules, chat.__name__, chat)

    # Avoid plugin startup hooks/scheduled jobs as well as configured DB imports.
    monkeypatch.setattr(nonebot, "require", lambda name: None)
    monkeypatch.setattr(nonebot, "get_driver", lambda: SimpleNamespace(on_startup=lambda handler: handler))
    scheduling = ModuleType("nonebot_plugin_apscheduler")
    scheduling.scheduler = SimpleNamespace(scheduled_job=lambda *args, **kwargs: lambda handler: handler)
    monkeypatch.setitem(sys.modules, scheduling.__name__, scheduling)
    name = package_name + ".agent_followup"
    spec = importlib.util.spec_from_file_location(name, Path(package.__path__[0]) / "agent_followup.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_active_group", lambda group_id: True)
    module._SEND_TIMES.clear()
    return SimpleNamespace(module=module, session=session, database=database, runtime=runtime,
                           memory=memory.MEMORY_V2, console=console)


def make_item(question, status="pending"):
    return FakeFollowup(id=1, group_id=100, user_id=11, source_message_id=123,
                        question=question, status=status, stage=0,
                        created_at=datetime(2026, 10, 11, 12))


@pytest.mark.parametrize("question", [
    "早点睡吧，明天还要上班。", "这么晚了，快睡。", "你怎么还没睡？",
    "困不困？", "睡了没有", "别熬夜了", "还在熬夜吗？", "还不去睡觉吗？",
    "准备睡了吗？", "你睡了吗？", "早点休息吧。", "快 点 睡 吧", "睡了没？",
])
def test_routine_care_is_recognized(isolated, question):
    assert isolated.module._is_routine_care_question(question)


@pytest.mark.parametrize("question", [
    "电脑睡眠模式的报错日志能发我吗？", "服务器休眠后网络断开，日志呢？",
    "设备还没睡眠完成吗？", "睡眠监测接口返回什么数据？", "sleep(30) 会阻塞线程吗？",
    "熬夜导致认知损伤的论文，你指的是哪一篇？", "你的失眠持续了几天？",
    "想把睡觉提醒设置在几点？", "五子棋你想先手吗？", "代码编译结果出来了吗？", "",
])
def test_real_tasks_and_technical_sleep_questions_are_preserved(isolated, question):
    assert not isolated.module._is_routine_care_question(question)


def test_routine_care_never_creates_a_followup(isolated):
    asyncio.run(isolated.module.schedule_followup(
        group_id=100, user_id=11, source_message_id=123, question="早点睡吧？"))
    isolated.database.get_session.assert_not_awaited()
    isolated.session.add.assert_not_called()


def test_real_task_still_creates_a_followup(isolated):
    asyncio.run(isolated.module.schedule_followup(
        group_id=100, user_id=11, source_message_id=123, question="编译日志能发我吗？"))
    created = isolated.session.add.call_args.args[0]
    assert created.question == "编译日志能发我吗？"
    assert created.status == "pending"
    isolated.session.commit.assert_awaited_once()
    isolated.session.close.assert_awaited_once()


def test_existing_routine_care_expires_once_without_model_or_message(isolated):
    item = make_item("你还没睡？")
    isolated.session.get.return_value = item
    bot = SimpleNamespace(send_group_msg=AsyncMock())
    async def run():
        await isolated.module._check_one(bot, item)
        await isolated.module._check_one(bot, item)
    asyncio.run(run())
    assert item.status == "expired"
    assert item.last_checked_at == item.updated_at
    isolated.session.commit.assert_awaited_once()
    assert isolated.session.close.await_count == 2
    isolated.runtime.RUNTIME._decide.assert_not_awaited()
    isolated.memory.recall.assert_not_awaited()
    isolated.console.check_agent_action.assert_not_awaited()
    bot.send_group_msg.assert_not_awaited()


@pytest.mark.parametrize("status", ["resolved", "replaced", "skipped", "expired"])
def test_cancelled_care_tasks_are_not_rewritten(isolated, status):
    item = make_item("困不困？", status=status)
    isolated.session.get.return_value = item
    asyncio.run(isolated.module._check_one(SimpleNamespace(), item))
    assert item.status == status
    isolated.session.commit.assert_not_awaited()
    isolated.runtime.RUNTIME._decide.assert_not_awaited()


def test_followup_uses_only_target_interaction_preferences(isolated):
    item = make_item("这份日志能发我吗？")
    isolated.session.get.return_value = item
    isolated.memory.recall.return_value = (
        "[用户偏好#1;用户11;本群] 本人不希望被催促，喜欢温柔交流。\n"
        "[用户偏好#2;用户12;本群] 其他人的交流偏好。\n"
        "[fact#3;用户11;来源:observed] 无关私人资料。\n"
        "[joke#4;用户0;来源:observed] 无关玩梗。"
    )
    asyncio.run(isolated.module._check_one(SimpleNamespace(), item))
    args = isolated.runtime.RUNTIME._decide.call_args.kwargs
    assert "本人不希望被催促" in args["context"]
    assert "其他人的" not in args["context"]
    assert "无关私人资料" not in args["context"]
    assert "无关玩梗" not in args["context"]
    assert "不要仅因时间已晚催促或催睡" in args["request"]
    assert "合理拒绝或设定边界不是冒犯" in args["request"]
    isolated.memory.recall.assert_awaited_once_with(
        100, [11], item.question, limit_chars=1000, allow_jokes=False)


@pytest.mark.parametrize("memory_failure", [None, RuntimeError("unavailable"), TimeoutError()])
def test_normal_task_can_still_send_when_memory_is_empty_or_unavailable(isolated, memory_failure):
    item = make_item("编译结果能发我吗？")
    isolated.session.get.return_value = item
    isolated.memory.recall.side_effect = memory_failure
    isolated.runtime.RUNTIME._decide.return_value = (["方便的话，把结果发我看看。"], {"action": "reply"})
    bot = SimpleNamespace(send_group_msg=AsyncMock())
    async def run():
        await isolated.module._check_one(bot, item)
        await asyncio.sleep(0)  # Let the fake bot-history background task finish.
    asyncio.run(run())
    isolated.runtime.RUNTIME._decide.assert_awaited_once()
    bot.send_group_msg.assert_awaited_once()
    assert item.status == "pending"
    assert item.stage == 1
    isolated.session.commit.assert_awaited_once()
