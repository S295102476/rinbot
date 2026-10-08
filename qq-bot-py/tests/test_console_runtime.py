"""Exercise actual NoneBot API hooks without an adapter connection."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from nonebot.adapters.onebot.v11 import Bot

from plugins import console_runtime as runtime


@pytest.fixture
def live(monkeypatch):
    quota = SimpleNamespace(
        reserve=AsyncMock(return_value="claim"), finish=AsyncMock(), uncertain=AsyncMock(),
        status=AsyncMock(return_value={"enforced": True, "stage": "normal"}),
    )
    monkeypatch.setattr(runtime, "QUOTA", quota)
    monkeypatch.setattr(runtime, "ready", lambda: True)
    monkeypatch.setattr(runtime, "scope_allows", lambda *a, **k: True)
    monkeypatch.setattr(runtime, "record", lambda *a, **k: None)
    monkeypatch.setattr(runtime, "notify_quota", AsyncMock())
    api = AsyncMock(return_value={"message_id": 7})
    bot = Bot(SimpleNamespace(_call_api=api), "123")
    return quota, bot, api


def test_multisegment_with_meme_is_one_round(live):
    quota, bot, api = live
    async def scenario():
        async with runtime.chat_turn(bot, 1, 2, 3, explicit=True):
            for _ in range(3):
                await bot.send_group_msg(group_id=1, message="part")
    asyncio.run(scenario())
    assert api.await_count == 3
    quota.finish.assert_awaited_once_with("claim", True, 2, 3, 3)


def test_partial_send_failure_still_counts_one_round(live):
    quota, bot, api = live
    api.side_effect = [{"message_id": 1}, OSError("transport disconnected")]
    async def scenario():
        with pytest.raises(OSError):
            async with runtime.chat_turn(bot, 1, 2, 3):
                await bot.send_group_msg(group_id=1, message="first")
                await bot.send_group_msg(group_id=1, message="second")
    asyncio.run(scenario())
    quota.finish.assert_awaited_once_with("claim", True, 2, 3, 1)


def test_ambiguous_first_send_is_not_fake_success(live):
    quota, bot, api = live
    api.side_effect = TimeoutError()
    async def scenario():
        with pytest.raises(TimeoutError):
            async with runtime.chat_turn(bot, 1, 2, 3):
                await bot.send_group_msg(group_id=1, message="first")
    asyncio.run(scenario())
    quota.uncertain.assert_awaited_once_with("claim")
    quota.finish.assert_not_called()


def test_closed_group_cancels_actual_adapter_send(live, monkeypatch):
    quota, bot, api = live
    async def scenario():
        async with runtime.chat_turn(bot, 1, 2, 3):
            monkeypatch.setattr(runtime, "scope_allows", lambda *a, **k: False)
            result = await bot.send_group_msg(group_id=1, message="must not leave process")
            assert result["_console_blocked"]
    asyncio.run(scenario())
    api.assert_not_called()
    quota.finish.assert_awaited_once_with("claim", False, 2, 3, 0)


def test_nonexplicit_round_cannot_borrow_another_users_at(live):
    quota, bot, _ = live
    quota.status.return_value = {"enforced": True, "stage": "at_only"}
    async def scenario():
        with pytest.raises(runtime.ChatBlocked):
            async with runtime.chat_turn(bot, 1, 2, 3, explicit=False):
                raise AssertionError("Not admitted")
        async with runtime.chat_turn(bot, 1, 4, 5, explicit=True, direct_at=True):
            await bot.send_group_msg(group_id=1, message="explicit response")
    asyncio.run(scenario())
    quota.reserve.assert_awaited_once_with(1, True)


def test_hard_limit_blocks_tools_and_followups(live):
    quota, _, _ = live
    quota.status.return_value = {"enforced": True, "stage": "hard_limit"}
    for proactive in (False, True):
        with pytest.raises(runtime.ChatBlocked):
            asyncio.run(runtime.check_agent_action(1, direct_at=True, proactive=proactive))


def test_at_detection_is_exact():
    bot = SimpleNamespace(self_id="123")
    assert not runtime.real_at(None, None)
    assert not runtime.real_at(bot, SimpleNamespace(raw_message="@123"))
    assert runtime.real_at(bot, SimpleNamespace(raw_message="[CQ:at,qq=123]"))
    assert not runtime.real_at(bot, SimpleNamespace(raw_message="[CQ:at,qq=1234]"))
