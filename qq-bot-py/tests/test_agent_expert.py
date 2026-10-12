import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

from test_minigame_runtime import message, Bot
from plugins import agent_expert as expert
from plugins.agent_tools import execute_tool, tool_schemas


class Redis:
    def __init__(self):
        self.count = {}
        self.tasks = set()

    async def eval(self, script, nkeys, daily, task, limit, ttl):
        assert nkeys == 2 and ttl == 172800
        if task in self.tasks:
            return -1
        if self.count.get(daily, 0) >= limit:
            return 0
        self.tasks.add(task)
        self.count[daily] = self.count.get(daily, 0) + 1
        return self.count[daily]


@pytest.fixture
def isolated(monkeypatch):
    calls = []
    async def complete(role, messages, **kwargs):
        calls.append((role, messages, kwargs))
        return "独立专家建议"
    route = SimpleNamespace(enabled=True, daily_limit=20)
    api = SimpleNamespace(complete=complete, resolve=lambda *args, **kwargs: route)
    monkeypatch.setitem(sys.modules, "plugins.model_routes", api)
    import plugins
    monkeypatch.setattr(plugins, "model_routes", api, raising=False)
    monkeypatch.setattr(expert, "_REDIS", Redis())
    monkeypatch.setattr(expert, "_config", lambda: {})
    return calls, route


def ctx(mid=20000, group=100):
    event = message("GPT 帮忙分析这个问题", mid, group=group)
    return {"bot": Bot(), "event": event, "batch_events": [event], "group_id": group,
            "expert_context": "仅允许的记忆", "developer_history": "NEVER INCLUDE DEVELOPER HISTORY",
            "api_key": "NEVER INCLUDE API KEY", "tools": ["run_project_check"]}


def test_expert_receives_only_allowlisted_context_once_per_task(isolated):
    calls, route = isolated
    async def run():
        context = ctx()
        args = {"source_message_id": 20000, "question": "请分析"}
        response = await expert.ask_expert(args, context)
        assert response["status"] == "consulted"
        with pytest.raises(PermissionError, match="一次"):
            await expert.ask_expert(args, context)
        # A fresh process/task context still cannot duplicate this paid request.
        with pytest.raises(PermissionError, match="重复"):
            await expert.ask_expert(args, ctx())
    asyncio.run(run())
    assert len(calls) == 1 and calls[0][0] == "expert"
    payload = json.dumps(calls, ensure_ascii=False)
    assert "仅允许的记忆" in payload and "NEVER INCLUDE" not in payload
    assert "run_project_check" not in payload


def test_daily_budget_shared_across_tasks_but_not_groups(isolated):
    calls, route = isolated
    route.daily_limit = 2
    async def run():
        for mid in [20001, 20002]:
            await expert.ask_expert({"source_message_id": mid, "question": "测试"}, ctx(mid))
        with pytest.raises(PermissionError, match="额度"):
            await expert.ask_expert({"source_message_id": 20003, "question": "测试"}, ctx(20003))
        await expert.ask_expert({"source_message_id": 20004, "question": "测试"}, ctx(20004, 101))
    asyncio.run(run())
    assert len(calls) == 3


def test_unavailable_budget_or_expired_task_never_calls_provider(isolated, monkeypatch):
    calls, _ = isolated
    async def broken(*args):
        raise OSError("private connection details")
    monkeypatch.setattr(expert._REDIS, "eval", broken)
    async def run():
        with pytest.raises(RuntimeError, match="存储暂不可用"):
            await expert.ask_expert({"source_message_id": 20000, "question": "测试"}, ctx())
        context = ctx(20001)
        context["deadline"] = asyncio.get_running_loop().time() - 1
        with pytest.raises(TimeoutError):
            await expert.ask_expert({"source_message_id": 20001, "question": "测试"}, context)
    asyncio.run(run())
    assert not calls


def test_group_cannot_access_developer_and_expert_has_no_tools(isolated):
    group = {tool["name"] for tool in tool_schemas("group")}
    assert "ask_expert" in group and "propose_patch" not in group and "read_project_file" not in group
    assert not tool_schemas("expert")
    async def run():
        with pytest.raises(PermissionError):
            await execute_tool("read_project_file", {"path": "main.py"}, "group", ctx())
        context = ctx()
        context["scope"] = "dev"
        with pytest.raises(PermissionError):
            await expert.ask_expert({"source_message_id": 20000, "question": "测试"}, context)
        with pytest.raises(PermissionError):
            await expert.ask_expert({"source_message_id": 123, "question": "测试"}, ctx())
    asyncio.run(run())
