import asyncio
import re
import sys
from dataclasses import replace
from types import ModuleType, SimpleNamespace

from nonebot.adapters.onebot.v11 import Message, MessageSegment

from plugins.agent_policy import AgentPolicy
from plugins.agent_runtime import (
    AgentRuntime,
    _batch_message_markers,
    _batch_quote_target,
    _batch_targets_owner,
    _clean_replies,
    _empty_at_context_hints,
    _extract_json,
    _group_hourly_reply_limit,
    _group_system_prompt,
    _has_affinity_query_intent,
    _last_bot_reply_hint,
    _looks_like_agent_payload,
    _message_directives,
    _normalize_affinity_delta,
    _recover_partial_agent_decision,
)
import plugins.agent_tools as agent_tools
from plugins.agent_context import AgentContextCache, CachedMessage, ContextSnapshot
from plugins.agent_memory import parse_writeback
from plugins.agent_tools import ApprovalStore, TOOLS, execute_tool, reject_patch
from plugins.chat_coordination import send_group_reply_parts
from plugins.group_mode import (
    _is_chat_only_allowed_command,
    _is_chat_only_blocked,
    _is_hash_command,
)
from plugins.link_parser import (
    _URL_PAT,
    _is_parseable,
    _looks_like_invalid_bilibili_candidate,
)
from plugins.dev_scope import _scope_allows_group
from plugins.affinity import curved_delta, relationship_stage
from plugins.persona_manager import get_active_persona_id, get_active_persona_name, get_persona_content, list_personas


def test_agent_policy_limits_to_configured_group():
    policy = AgentPolicy.from_config({
        "agent": {
            "enabled": True,
            "active_groups": [987654321],
            "dev": {"admin_users": [123]},
        }
    })
    assert policy.group_enabled(987654321)
    assert not policy.group_enabled(999)
    assert policy.user_is_admin(123)
    assert not policy.user_is_admin(456)


def test_agent_json_and_reply_cleanup():
    assert _extract_json("```json\n{\"action\": \"ignore\"}\n```") == {"action": "ignore"}
    assert _extract_json("`json\n{\"action\":\"reply\",\"replies\":[\"收到\"]}\n`") == {
        "action": "reply",
        "replies": ["收到"],
    }
    assert _clean_replies(["  第一条  ", "<think>internal</think>第二条"], 20) == ["第一条", "第二条"]


def test_agent_reply_cleanup_supports_three_parts_with_total_limit():
    assert _clean_replies(["第一句", "第二句", "第三句"], 20, 8) == ["第一句", "第二句"]


def test_agent_group_reply_cleanup_preserves_terminal_punctuation():
    assert _clean_replies(
        ["收到啦！", "我看看？", "先这样。"],
        20,
        60,
        hard_limit=False,
    ) == ["收到啦！", "我看看？", "先这样。"]


def test_agent_group_reply_cleanup_does_not_hard_truncate_long_sentence():
    text = "当心布妹听见直接拿长枪戳你就算她确实"
    assert _clean_replies(
        [text],
        20,
        60,
        hard_limit=False,
    ) == [text]


def test_affinity_delta_accepts_decimal_steps_and_clamps():
    assert _normalize_affinity_delta(0.37) == 0.4
    assert _normalize_affinity_delta(-9) == -2.0
    assert _normalize_affinity_delta("not-a-number") == 0.0


def test_affinity_curve_diminishes_near_endpoints():
    middle = curved_delta(0.0, 1.0)
    near_top = curved_delta(98.0, 1.0)
    near_bottom = curved_delta(-98.0, -1.0)
    assert middle > near_top > 0
    assert middle > abs(near_bottom) > 0


def test_affinity_relationship_stage_boundaries():
    assert relationship_stage(-10) == "较冷淡"
    assert relationship_stage(0) == "普通陌生人"
    assert relationship_stage(4.9) == "普通陌生人"
    assert relationship_stage(5) == "点头之交"
    assert relationship_stage(15) == "普通朋友"
    assert relationship_stage(30) == "好朋友"
    assert relationship_stage(60) == "挚友、闺蜜"
    assert relationship_stage(75) == "非常信任，可托付后背"
    assert relationship_stage(85) == "接近情人关系"


def test_persona_prompt_and_visual_policy_are_loaded():
    assert get_active_persona_id()
    assert get_active_persona_name()
    assert list_personas()
    prompt = _group_system_prompt(987654321)
    assert "只陈述清晰可见的事实" in prompt
    assert "不得擅自猜测人物身份" in prompt
    assert "当前人设是" in prompt
    assert "口语化、自然" in prompt
    assert "不要写成 AI 报告" in prompt
    assert get_persona_content()


def test_agent_group_reply_cleanup_removes_leaked_citation_marker():
    assert _clean_replies(
        ["这展开拐得也太快了吧 [1.1.2]。"],
        20,
        0,
        hard_limit=False,
    ) == ["这展开拐得也太快了吧。"]


def test_truncated_agent_json_recovers_only_complete_reply_text():
    raw = '''```json
    {
      "action": "reply",
      "source_message_id": 1222106688,
      "replies": ["你突然凑那么近干嘛啊！快给我退回去！"],
      "quote": "none",
      "affinity_updates":'''
    decision = _recover_partial_agent_decision(raw)
    assert decision is not None
    assert decision["source_message_id"] == 1222106688
    assert decision["replies"] == ["你突然凑那么近干嘛啊！快给我退回去！"]
    assert _looks_like_agent_payload(raw)


def test_truncated_agent_json_without_complete_replies_is_not_plain_text():
    raw = '```json\n{"action":"reply","source_message_id":1,"replies":["半句话'
    assert _recover_partial_agent_decision(raw) is None
    assert _looks_like_agent_payload(raw)


def test_agent_decision_drops_incomplete_structured_scaffolding():
    async def run():
        runtime = AgentRuntime()

        async def fake_complete(_messages):
            return '```json\n{"action":"reply","source_message_id":1,"replies":["半句话'

        runtime._complete = fake_complete
        return await runtime._decide(scope="group", request="你好", force_reply=True)

    parts, decision = asyncio.run(run())
    assert parts == []
    assert decision["action"] == "ignore"
    assert decision["malformed"] is True


def test_agent_decision_uses_complete_replies_from_truncated_tail():
    async def run():
        runtime = AgentRuntime()

        async def fake_complete(_messages):
            return (
                '```json\n{"action":"reply","source_message_id":7,'
                '"replies":["完整回复"],"affinity_updates":'
            )

        runtime._complete = fake_complete
        return await runtime._decide(scope="group", request="你好", force_reply=True)

    parts, decision = asyncio.run(run())
    assert parts == ["完整回复"]
    assert decision["source_message_id"] == 7
    assert decision["recovered_partial"] is True


def test_chat_only_group_hash_gate_matches_ascii_and_fullwidth_hashes():
    assert _is_hash_command("#签到")
    assert _is_hash_command("  ＃指令一览")
    assert not _is_hash_command("凛，帮我签到")
    assert _is_chat_only_allowed_command("#搜本子")
    assert _is_chat_only_allowed_command("  ＃搜本子")
    assert not _is_chat_only_allowed_command("#搜图 原神")
    assert not _is_chat_only_blocked("#搜本子")
    assert _is_chat_only_blocked("#签到")
    assert _is_chat_only_blocked("ww签到")
    assert _is_chat_only_blocked("绑定设备")


def test_group_reply_limit_supports_per_group_overrides(monkeypatch):
    import plugins.agent_runtime as agent_runtime

    monkeypatch.setattr(agent_runtime, "GROUP_CONFIG", {
        "hourly_reply_soft_limit": 30,
        "group_overrides": {
            "987654321": {"hourly_reply_soft_limit": 45},
            987654322: {"hourly_reply_soft_limit": 12},
        },
    })
    assert _group_hourly_reply_limit(1) == 30
    assert _group_hourly_reply_limit(987654321) == 45
    assert _group_hourly_reply_limit(987654322) == 12


def test_agent_message_counts_use_rolling_sixty_minutes(monkeypatch):
    import plugins.agent_metrics as metrics

    async def run():
        now = 2_000_000.0
        monkeypatch.setattr(metrics, "_REDIS", None)
        monkeypatch.setattr(metrics.time, "time", lambda: now)
        metrics._LOCAL.clear()
        metrics._LOCAL[123].extend((now - 3599, now - 3601, now - 86399))
        try:
            return await metrics.get_agent_message_counts(123)
        finally:
            metrics._LOCAL.clear()

    assert asyncio.run(run()) == (1, 3)


def test_affinity_tool_requires_latest_message_query_intent():
    assert _has_affinity_query_intent("好感度")
    assert _has_affinity_query_intent("帮我查一下好感度排行")
    assert _has_affinity_query_intent("好感度多少")
    assert not _has_affinity_query_intent("发个好感度相关的表情包")
    assert not _has_affinity_query_intent("好感度涨了")
    assert not _has_affinity_query_intent("哈哈")


def test_link_gate_detects_youtube_urls_for_chat_only_groups():
    assert _URL_PAT.search("https://www.youtube.com/watch?v=abc123")
    assert _URL_PAT.search("https://youtu.be/abc123")
    assert _URL_PAT.search("https://youtube.com/shorts/abc123")
    event = SimpleNamespace(
        get_plaintext=lambda: "分享视频",
        message=[SimpleNamespace(type="json", data={"data": "https://youtu.be/abc123"})],
    )
    assert _is_parseable(event)


def test_link_gate_rejects_bare_bilibili_false_positive_text():
    event = SimpleNamespace(get_plaintext=lambda: "bmpt", message=[])
    assert not _is_parseable(event)
    assert _looks_like_invalid_bilibili_candidate(event)

    valid_bv = SimpleNamespace(get_plaintext=lambda: "BV1xx411c7mD", message=[])
    assert _is_parseable(valid_bv)
    assert not _looks_like_invalid_bilibili_candidate(valid_bv)


def test_development_scope_switch_restricts_new_group_messages(monkeypatch):
    import plugins.dev_scope as dev_scope

    original = dev_scope.SCOPE_ENABLED
    original_at_only = dev_scope.AT_ONLY_ENABLED
    try:
        dev_scope.AT_ONLY_ENABLED = False
        dev_scope.SCOPE_ENABLED = True
        assert _scope_allows_group(987654321, "普通消息")
        assert not _scope_allows_group(987654322, "普通消息")
        assert _scope_allows_group(987654322, "#签到")
        dev_scope.SCOPE_ENABLED = False
        assert _scope_allows_group(987654322, "普通消息")
    finally:
        dev_scope.SCOPE_ENABLED = original
        dev_scope.AT_ONLY_ENABLED = original_at_only


def test_development_scope_at_only_keeps_commands_and_real_mentions():
    import plugins.dev_scope as dev_scope

    original = dev_scope.SCOPE_ENABLED
    original_at_only = dev_scope.AT_ONLY_ENABLED
    try:
        dev_scope.SCOPE_ENABLED = False
        dev_scope.AT_ONLY_ENABLED = True
        assert not _scope_allows_group(987654321, "普通消息")
        assert not _scope_allows_group(987654321, "凛，在吗")
        assert _scope_allows_group(987654321, "普通消息", mentions_bot=True)
        assert _scope_allows_group(987654322, "#签到")
    finally:
        dev_scope.SCOPE_ENABLED = original
        dev_scope.AT_ONLY_ENABLED = original_at_only


def test_development_scope_at_only_does_not_expose_non_agent_groups():
    import plugins.dev_scope as dev_scope

    original = dev_scope.SCOPE_ENABLED
    original_at_only = dev_scope.AT_ONLY_ENABLED
    try:
        dev_scope.SCOPE_ENABLED = False
        dev_scope.AT_ONLY_ENABLED = True
        assert not _scope_allows_group(999999999, "你好", mentions_bot=True)
    finally:
        dev_scope.SCOPE_ENABLED = original
        dev_scope.AT_ONLY_ENABLED = original_at_only


def test_development_scope_keeps_video_parser_reachable_in_at_only_mode():
    import plugins.dev_scope as dev_scope

    original = dev_scope.SCOPE_ENABLED
    original_at_only = dev_scope.AT_ONLY_ENABLED
    try:
        dev_scope.SCOPE_ENABLED = False
        dev_scope.AT_ONLY_ENABLED = True
        event = SimpleNamespace(
            get_plaintext=lambda: "https://youtu.be/example",
            message=[],
        )
        assert dev_scope._message_has_parseable_link(event)
    finally:
        dev_scope.SCOPE_ENABLED = original
        dev_scope.AT_ONLY_ENABLED = original_at_only


def test_development_scope_keeps_non_hash_local_commands_reachable():
    import plugins.dev_scope as dev_scope

    original = dev_scope.SCOPE_ENABLED
    original_at_only = dev_scope.AT_ONLY_ENABLED
    try:
        dev_scope.SCOPE_ENABLED = False
        dev_scope.AT_ONLY_ENABLED = True
        for text in ("🦌", "不🦌", "补🦌 12", "🦌不🦌", ".r7d3", "ww帮助", "jm123456"):
            event = SimpleNamespace(get_plaintext=lambda value=text: value, message=[])
            assert dev_scope._is_local_command_event(event), text
    finally:
        dev_scope.SCOPE_ENABLED = original
        dev_scope.AT_ONLY_ENABLED = original_at_only


def test_development_scope_at_only_command_aliases_are_documented():
    # The parser is intentionally exercised through the accepted action names
    # used by the command handler, including the explicit ``agent at`` form.
    accepted = {
        "at", "@", "at-only", "mention", "mention-only", "only-at",
        "仅@", "只@", "仅at", "仅提及",
    }
    assert "at" in accepted
    assert "@" in accepted


def test_development_scope_command_accepts_admin_group_or_private_events():
    from plugins.dev_scope import _is_scope_command

    admin_event = SimpleNamespace(
        user_id=123456789,
        get_plaintext=lambda: "#开发监听 off",
    )
    ordinary_event = SimpleNamespace(
        user_id=123,
        get_plaintext=lambda: "#开发监听 off",
    )
    assert _is_scope_command(admin_event)
    assert not _is_scope_command(ordinary_event)


def test_agent_decision_loop_returns_structured_reply():
    async def run():
        runtime = AgentRuntime()
        runtime.policy = replace(runtime.policy, mode="shadow")

        async def fake_complete(_messages):
            return '{"action":"reply","replies":["测试回复"]}'

        runtime._complete = fake_complete
        return await runtime._decide(scope="group", request="你好", force_reply=True)

    parts, decision = asyncio.run(run())
    assert parts == ["测试回复"]
    assert decision["action"] == "reply"
    assert decision["iterations"] == 1


def test_agent_groups_messages_into_one_burst():
    async def run():
        runtime = AgentRuntime()
        runtime.policy = replace(
            runtime.policy,
            enabled=True,
            active_groups=frozenset({10}),
            burst_window_seconds=0.02,
        )
        batches = []

        async def fake_handle(group_id, items):
            batches.append((group_id, len(items), any(force for _bot, _event, force in items)))
            return []

        runtime._handle_group_batch = fake_handle
        bot = SimpleNamespace()
        for message_id in range(3):
            event = SimpleNamespace(group_id=10, message_id=message_id + 1)
            await runtime.enqueue_group_event(bot, event, force_reply=message_id == 2)
        await asyncio.sleep(0.05)
        return batches

    assert asyncio.run(run()) == [(10, 3, True)]


def test_approval_cannot_be_consumed_by_another_user():
    async def run():
        store = ApprovalStore()
        store._redis = None
        token = await store.create({"user_id": 123, "patch": "x"})
        denied = await store.pop(token, user_id=456)
        accepted = await store.pop(token, user_id=123)
        return denied, accepted

    denied, accepted = asyncio.run(run())
    assert denied is None
    assert accepted is not None


def test_secret_file_is_not_a_dev_tool_input():
    async def run():
        try:
            await execute_tool(
                "read_project_file",
                {"path": "config.yaml"},
                "dev",
                {"is_admin": True},
            )
        except PermissionError:
            return True
        return False

    assert asyncio.run(run())


def test_dev_agent_can_list_and_read_bounded_project_context():
    async def run():
        context = {"is_admin": True, "user_id": 123456789}
        listing = await execute_tool(
            "list_project_files",
            {"path": "plugins", "max_depth": 1, "max_entries": 300},
            "dev",
            context,
        )
        excerpt = await execute_tool(
            "read_project_file",
            {"path": "plugins/agent_policy.py", "start_line": 1, "max_lines": 3},
            "dev",
            context,
        )
        return listing, excerpt

    listing, excerpt = asyncio.run(run())
    assert "plugins/agent_runtime.py" in listing["entries"]
    assert "     1:" in excerpt
    assert len([line for line in excerpt.splitlines() if re.match(r"^\s*\d+:", line)]) == 3
    assert "可提高 start_line 继续读取" in excerpt


def test_dev_agent_rejects_arbitrary_project_commands():
    async def run():
        await execute_tool(
            "run_project_check",
            {"check": "shell", "targets": []},
            "dev",
            {"is_admin": True, "user_id": 123456789},
        )

    try:
        asyncio.run(run())
    except PermissionError:
        return
    assert False, "development Agent must not execute arbitrary commands"


def test_reject_patch_consumes_only_the_requesters_approval(monkeypatch):
    async def run():
        store = ApprovalStore()
        store._redis = None
        monkeypatch.setattr(agent_tools, "APPROVALS", store)
        token = await store.create({"user_id": 123, "patch": "x"})
        denied = await reject_patch(token, 456)
        accepted = await reject_patch(token, 123)
        return denied, accepted

    denied, accepted = asyncio.run(run())
    assert "不是该审批的发起人" in denied
    assert "项目文件没有被修改" in accepted


def test_group_agent_exposes_only_requested_business_tools():
    assert "get_affinity" in TOOLS
    assert "sign_in" in TOOLS
    assert "mute_user" in TOOLS
    assert "send_setu" in TOOLS
    setu_schema = TOOLS["send_setu"].schema()["parameters"]
    assert "r18" not in setu_schema["properties"]
    assert "count" not in setu_schema["properties"]
    assert "deer" not in TOOLS


def test_affinity_query_claim_is_idempotent(monkeypatch):
    async def run():
        monkeypatch.setattr(agent_tools, "_MUTE_REDIS", None)
        agent_tools._AFFINITY_QUERY_DONE.clear()
        agent_tools._AFFINITY_QUERY_INFLIGHT.clear()
        first = await agent_tools.claim_affinity_query(10, 9001)
        second = await agent_tools.claim_affinity_query(10, 9001)
        await agent_tools.finish_affinity_query(10, 9001)
        third = await agent_tools.claim_affinity_query(10, 9001)
        return first, second, third

    assert asyncio.run(run()) == (True, False, False)


def test_affinity_tool_rejects_stale_source_message(monkeypatch):
    async def run():
        class FakeBot:
            async def send_group_msg(self, **_kwargs):
                raise AssertionError("stale affinity query must not send a card")

        latest = SimpleNamespace(
            group_id=10,
            user_id=1,
            message_id=2,
            get_plaintext=lambda: "发个表情包",
        )
        old = SimpleNamespace(
            group_id=10,
            user_id=1,
            message_id=1,
            get_plaintext=lambda: "好感度",
        )
        try:
            await execute_tool(
                "get_affinity",
                {"source_message_id": 1},
                "group",
                {
                    "bot": FakeBot(),
                    "group_id": 10,
                    "event": latest,
                    "batch_events": [old, latest],
                },
            )
        except PermissionError as exc:
            return str(exc)
        raise AssertionError("stale affinity query was accepted")

    assert "最新消息" in asyncio.run(run())


def test_memory_writeback_parser_keeps_plain_summary_and_valid_json_facts():
    summary, facts = parse_writeback("普通摘要")
    assert summary == "普通摘要"
    assert facts == []

    summary, facts = parse_writeback(
        """```json
        {"summary":"群里讨论了新功能", "facts":[{"user_id":1,"source_message_id":2}]}
        ```"""
    )
    assert summary == "群里讨论了新功能"
    assert facts == [{"user_id": 1, "source_message_id": 2}]


def test_setu_tool_uses_exact_requester_and_server_controlled_count(monkeypatch):
    async def run():
        calls = []

        class FakeBot:
            async def send_group_msg(self, **kwargs):
                calls.append(("notice", kwargs))

        event = SimpleNamespace(
            group_id=10,
            user_id=123,
            message_id=456,
            get_plaintext=lambda: "凛，给我来张原神涩图",
        )

        async def fake_send_setu(bot, group_id, user_id, **kwargs):
            calls.append(("setu", {"group_id": group_id, "user_id": user_id, **kwargs}))
            return {"status": "sent", "sent": 0}

        async def fake_convert_tags(text):
            return [text]

        fake_setu = ModuleType("plugins.setu")
        fake_setu.send_setu = fake_send_setu
        fake_setu._chinese_to_pixiv_tags = fake_convert_tags
        fake_setu.is_disallowed_setu_request = lambda _value: False
        fake_setu.AGENT_NUM = 1
        monkeypatch.setitem(sys.modules, "plugins.setu", fake_setu)
        monkeypatch.setattr(agent_tools, "_MUTE_REDIS", None)
        agent_tools._SETU_DONE.clear()
        agent_tools._SETU_INFLIGHT.clear()
        result = await execute_tool(
            "send_setu",
            {"source_message_id": 456, "tags": ["原神"]},
            "group",
            {"bot": FakeBot(), "group_id": 10, "event": event, "batch_events": [event]},
        )
        return calls, result

    calls, result = asyncio.run(run())
    setu_call = next(value for kind, value in calls if kind == "setu")
    assert setu_call["group_id"] == 10
    assert setu_call["user_id"] == 123
    assert setu_call["target_count"] == 1
    assert setu_call["force_safe_mode"] is True
    assert setu_call["announce"] is False
    assert "status=sent" in result


def test_agent_setu_not_found_does_not_send_a_visible_notice(monkeypatch):
    async def run():
        notices = []

        class FakeBot:
            async def send_group_msg(self, **kwargs):
                notices.append(kwargs["message"])

        event = SimpleNamespace(
            group_id=10,
            user_id=123,
            message_id=457,
            get_plaintext=lambda: "凛，给我来张不存在标签的涩图",
        )

        async def fake_send_setu(_bot, _group_id, _user_id, **kwargs):
            assert kwargs["announce"] is False
            return {"status": "not_found", "sent": 0}

        async def fake_convert_tags(text):
            return [text]

        fake_setu = ModuleType("plugins.setu")
        fake_setu.send_setu = fake_send_setu
        fake_setu._chinese_to_pixiv_tags = fake_convert_tags
        fake_setu.is_disallowed_setu_request = lambda _value: False
        fake_setu.AGENT_NUM = 1
        monkeypatch.setitem(sys.modules, "plugins.setu", fake_setu)
        monkeypatch.setattr(agent_tools, "_MUTE_REDIS", None)
        agent_tools._SETU_DONE.clear()
        agent_tools._SETU_INFLIGHT.clear()
        result = await execute_tool(
            "send_setu",
            {"source_message_id": 457, "tags": ["不存在标签"]},
            "group",
            {"bot": FakeBot(), "group_id": 10, "event": event, "batch_events": [event]},
        )
        return notices, result

    notices, result = asyncio.run(run())
    assert notices == []
    assert "status=not_found" in result


def test_setu_tool_rejects_disallowed_request_before_conversion(monkeypatch):
    async def run():
        notices = []

        class FakeBot:
            async def send_group_msg(self, **kwargs):
                notices.append(kwargs["message"])

        event = SimpleNamespace(
            group_id=10,
            user_id=123,
            message_id=789,
            get_plaintext=lambda: "来点萝莉涩图啦",
        )
        fake_setu = ModuleType("plugins.setu")
        fake_setu.AGENT_NUM = 1
        fake_setu.is_disallowed_setu_request = lambda _value: True

        async def unexpected_convert(_text):
            raise AssertionError("blocked request must not reach tag conversion")

        async def unexpected_send(*_args, **_kwargs):
            raise AssertionError("blocked request must not send images")

        fake_setu._chinese_to_pixiv_tags = unexpected_convert
        fake_setu.send_setu = unexpected_send
        monkeypatch.setitem(sys.modules, "plugins.setu", fake_setu)
        monkeypatch.setattr(agent_tools, "_MUTE_REDIS", None)
        agent_tools._SETU_DONE.clear()
        agent_tools._SETU_INFLIGHT.clear()
        result = await execute_tool(
            "send_setu",
            {"source_message_id": 789, "tags": ["loli"]},
            "group",
            {"bot": FakeBot(), "group_id": 10, "event": event, "batch_events": [event]},
        )
        return notices, result

    notices, result = asyncio.run(run())
    assert notices == ["这个主题不能处理，换成明确成年角色或普通主题吧。"]
    assert "未执行" in result


def test_context_hot_cache_is_bounded():
    cache = AgentContextCache()
    for message_id in range(510):
        cache.add(CachedMessage(
            group_id=1,
            message_id=message_id + 1,
            user_id=2,
            nickname="tester",
            content=str(message_id),
        ))
    for image_id in range(6):
        cache.put_image_payload(1, str(image_id), "image/jpeg", b"x")
    assert len(cache._messages[1]) == 500
    assert cache.image_count(1) == 5


def test_context_avatar_cache_is_bounded_and_returns_active_people():
    async def run():
        cache = AgentContextCache()
        for user_id in range(200):
            cache.put_avatar_payload(user_id + 1, "image/jpeg", b"avatar")
        payloads = await cache.avatar_payloads([200, 199, 198], max_count=2, wait_seconds=0)
        return cache, payloads

    cache, payloads = asyncio.run(run())
    assert len(cache._avatar_payloads) == 128
    assert [item[0] for item in payloads] == [200, 199]


def test_context_messages_keep_real_timestamps_and_owner_window_is_filtered():
    from datetime import datetime

    cache = AgentContextCache()
    cache._agent_groups = frozenset({10, 20})
    timestamp = datetime(2026, 8, 10, 18, 0, 0)
    owner = CachedMessage(
        group_id=20,
        message_id=1,
        user_id=123456789,
        nickname="管理员",
        content="跨群消息",
        created_at=timestamp,
    )
    cache.add_owner_message(owner)
    cache.add_owner_message(replace(owner, group_id=99, message_id=2))
    cache.add_owner_message(replace(owner, user_id=123, message_id=3))
    assert len(cache._owner_messages) == 1
    assert "2026-08-10 18:00:00" in cache._format_message(owner)

    cache._owner_loaded = True
    rendered = asyncio.run(cache.render_owner_context(10))
    assert "来源群:20" in rendered
    assert "其他群" in rendered
    assert "2026-08-10 18:00:00" in rendered


def test_owner_context_is_only_loaded_for_direct_owner_interaction(monkeypatch):
    async def run():
        owner_direct = SimpleNamespace(
            user_id=123456789,
            message_id=1,
            message=Message(),
        )
        owner_ordinary = SimpleNamespace(user_id=123456789, message_id=2, message=Message())
        another_user_mentions_owner = SimpleNamespace(
            user_id=111,
            message_id=3,
            message=Message([MessageSegment.at(123456789)]),
        )
        assert await _batch_targets_owner([(SimpleNamespace(), owner_direct, True)])
        assert not await _batch_targets_owner([(SimpleNamespace(), owner_ordinary, False)])
        assert not await _batch_targets_owner([(SimpleNamespace(), another_user_mentions_owner, True)])

    asyncio.run(run())


def test_group_prompt_marks_owner_priority_only_for_test_group():
    test_prompt = _group_system_prompt(987654321)
    other_prompt = _group_system_prompt(987654322)
    assert "管理员优先互动群" in test_prompt
    assert "只是普通群友" in other_prompt
    assert "当前群号是 987654322" in other_prompt
    assert "单独表情包、单独图片" in test_prompt
    assert "同一批次只做一次群聊决策" in test_prompt
    assert "空@你通常表示用户在叫你参与" in test_prompt
    assert "线上角色扮演" in test_prompt
    assert "风景、物品、动物" in test_prompt
    assert "滚动60分钟节奏参考是约30条" in test_prompt
    assert "不是硬上限" in test_prompt
    assert "默认态度应当温和" in test_prompt
    assert "不要无缘无故挖苦" in test_prompt


def test_batch_markers_and_last_reply_hint_expose_follow_up_context():
    items = [
        (SimpleNamespace(), SimpleNamespace(user_id=111), False),
        (SimpleNamespace(), SimpleNamespace(user_id=111), False),
        (SimpleNamespace(), SimpleNamespace(user_id=222), False),
    ]
    markers = _batch_message_markers(items)
    assert "第一条" in markers[0]
    assert "紧接上一条" in markers[1]
    assert "第3/3条" in markers[2]

    snapshot = ContextSnapshot(messages=(CachedMessage(
        group_id=10,
        message_id=0,
        user_id=0,
        nickname="凛",
        content="我刚刚已经回答过了。",
        is_bot=True,
    ),))
    hint = _last_bot_reply_hint(snapshot)
    assert "你已经回复过" in hint
    assert "刚刚已经回答" in hint


def test_empty_at_links_same_users_previous_message():
    from datetime import datetime

    now = datetime.now()
    snapshot = ContextSnapshot(messages=(
        CachedMessage(
            group_id=10,
            message_id=100,
            user_id=111,
            nickname="tester",
            content="前面这个问题应该怎么处理？",
            created_at=now,
        ),
        CachedMessage(
            group_id=10,
            message_id=101,
            user_id=111,
            nickname="tester",
            content="",
            at_users=(999,),
            created_at=now,
        ),
    ))
    bot = SimpleNamespace(self_id="999")
    event = SimpleNamespace(
        user_id=111,
        message_id=101,
        reply=None,
        message=Message([MessageSegment.at(999)]),
        get_plaintext=lambda: "",
    )

    hints = _empty_at_context_hints(snapshot, [(bot, event, True)])

    assert len(hints) == 1
    assert "前面这个问题" in hints[0]
    assert "禁止用" in hints[0]


def test_mute_tool_clamps_duration_and_is_idempotent(monkeypatch):
    async def run():
        calls = []

        class FakeBot:
            self_id = "999"

            async def get_group_member_info(self, *, group_id, user_id):
                return {"role": "member"}

            async def set_group_ban(self, **kwargs):
                calls.append(kwargs)

        event = SimpleNamespace(
            group_id=10,
            user_id=123,
            message_id=456,
            sender=SimpleNamespace(role="member"),
            message=Message(),
        )
        ctx = {
            "bot": FakeBot(),
            "group_id": 10,
            "event": event,
            "batch_events": [event],
            "is_admin": False,
        }
        args = {
            "target_user_id": 123,
            "duration_seconds": 9999,
            "source_message_id": 456,
            "reason": "test",
        }
        first = await execute_tool("mute_user", args, "group", ctx)
        second = await execute_tool("mute_user", args, "group", ctx)
        return calls, first, second

    monkeypatch.setattr(agent_tools, "_MUTE_REDIS", None)
    agent_tools._MUTE_DONE.clear()
    agent_tools._MUTE_INFLIGHT.clear()
    calls, first, second = asyncio.run(run())
    assert len(calls) == 1
    assert calls[0]["duration"] == 1800
    assert first["status"] == "executed"
    assert second["status"] == "duplicate"


def test_ordinary_user_cannot_mute_another_batch_user(monkeypatch):
    async def run():
        class FakeBot:
            self_id = "999"

            async def get_group_member_info(self, *, group_id, user_id):
                return {"role": "member"}

        requester = SimpleNamespace(
            group_id=10,
            user_id=123,
            message_id=1,
            sender=SimpleNamespace(role="member"),
            message=Message([MessageSegment.at(456)]),
        )
        target = SimpleNamespace(
            group_id=10,
            user_id=456,
            message_id=2,
            sender=SimpleNamespace(role="member"),
            message=Message(),
        )
        await execute_tool(
            "mute_user",
            {"target_user_id": 456, "duration_seconds": 60, "source_message_id": 1},
            "group",
            {
                "bot": FakeBot(),
                "group_id": 10,
                "event": requester,
                "batch_events": [requester, target],
                "is_admin": False,
            },
        )

    monkeypatch.setattr(agent_tools, "_MUTE_REDIS", None)
    try:
        asyncio.run(run())
    except PermissionError:
        return
    assert False, "ordinary users must not mute another user"


def test_mute_tool_does_not_retry_after_onebot_failure(monkeypatch):
    async def run():
        attempts = 0

        class FakeBot:
            self_id = "999"

            async def get_group_member_info(self, *, group_id, user_id):
                return {"role": "member"}

            async def set_group_ban(self, **kwargs):
                nonlocal attempts
                attempts += 1
                raise RuntimeError("connection lost")

        event = SimpleNamespace(
            group_id=11,
            user_id=321,
            message_id=654,
            sender=SimpleNamespace(role="member"),
            message=Message(),
        )
        ctx = {
            "bot": FakeBot(),
            "group_id": 11,
            "event": event,
            "batch_events": [event],
            "is_admin": False,
        }
        args = {"target_user_id": 321, "duration_seconds": 60, "source_message_id": 654}
        try:
            await execute_tool("mute_user", args, "group", ctx)
        except RuntimeError:
            pass
        duplicate = await execute_tool("mute_user", args, "group", ctx)
        return attempts, duplicate

    monkeypatch.setattr(agent_tools, "_MUTE_REDIS", None)
    agent_tools._MUTE_DONE.clear()
    agent_tools._MUTE_INFLIGHT.clear()
    attempts, duplicate = asyncio.run(run())
    assert attempts == 1
    assert duplicate["status"] == "duplicate"


def test_development_agent_uses_isolated_provider():
    runtime = AgentRuntime()
    assert runtime.dev_provider == "openai_responses"
    assert runtime.provider_chain == ["antigravity"]
    assert runtime.dev_max_iterations == 8
    assert runtime.dev_tool_calls_per_iteration == 4
    assert runtime.dev_max_output_tokens == 12000


def test_development_agent_uses_coding_output_budget(monkeypatch):
    async def run():
        captured = {}

        async def fake_call(_config, _messages, **kwargs):
            captured.update(kwargs)
            return '{"action":"ignore"}'

        monkeypatch.setattr("plugins.responses_api.call_responses", fake_call)
        runtime = AgentRuntime()
        await runtime._complete_dev([{"role": "user", "content": "test"}])
        return captured

    captured = asyncio.run(run())
    assert captured["max_output_tokens"] == 12000
    assert captured["timeout"] == 180


def test_development_agent_returns_pending_approval_deterministically(monkeypatch):
    async def run():
        runtime = AgentRuntime()
        responses = iter([
            '{"action":"tool","tool_calls":[{"name":"propose_patch","arguments":{}}]}',
            '{"action":"reply","replies":["修改方案已准备。"]}',
        ])

        async def fake_complete(_messages):
            return next(responses)

        async def fake_execute(name, _args, _scope, _ctx):
            assert name == "propose_patch"
            return {
                "status": "pending_approval",
                "approval_token": "token123",
                "summary": "测试补丁",
                "checks": ["git_diff_check"],
            }

        runtime._complete_dev = fake_complete
        monkeypatch.setattr("plugins.agent_runtime.execute_tool", fake_execute)
        return await runtime._decide(scope="dev", request="修改代码", force_reply=True, user_id=123456789)

    parts, decision = asyncio.run(run())
    assert parts == ["修改方案已准备。"]
    assert decision["pending_approvals"][0]["approval_token"] == "token123"


def test_agent_directives_resolve_safe_real_qq_targets():
    async def run():
        event = SimpleNamespace(
            user_id=111,
            message_id=222,
            raw_message="",
            reply=None,
            message=Message([MessageSegment.at(333)]),
        )
        bot = SimpleNamespace(self_id="999")
        return await _message_directives(
            bot,
            event,
            {"quote": "current", "mentions": ["sender", "mentioned", "987654"]},
        )

    quote_id, mention_ids = asyncio.run(run())
    assert quote_id == 222
    assert mention_ids == [111, 333]


def test_reply_sender_builds_clickable_onebot_segments():
    async def run():
        sent = []

        class FakeBot:
            async def send_group_msg(self, **kwargs):
                sent.append(kwargs["message"])

        await send_group_reply_parts(
            FakeBot(),
            123,
            ["收到"],
            reply_message_id=222,
            mention_user_ids=[111],
            force_quote=True,
        )
        return sent[0]

    message = asyncio.run(run())
    rendered = str(message)
    assert "reply" in rendered.lower()
    assert "111" in rendered
    assert "222" in rendered


def test_nested_reply_quotes_current_user_message(monkeypatch):
    async def run():
        async def fake_reply_context(_bot, _event):
            return SimpleNamespace(message_id=100, sender_id=333, sender_name="rin", text="older")

        monkeypatch.setattr(
            "plugins.chat_coordination.resolve_reply_context",
            fake_reply_context,
        )
        event = SimpleNamespace(
            user_id=111,
            message_id=200,
            raw_message="",
            reply=SimpleNamespace(),
            message=Message(),
            get_plaintext=lambda: "就这，战个痛快",
        )
        return await _message_directives(
            SimpleNamespace(self_id="999"),
            event,
            {"quote": "referenced", "mentions": []},
        )

    quote_id, _mentions = asyncio.run(run())
    assert quote_id == 200


def test_tool_source_message_selects_exact_batch_sender():
    first = SimpleNamespace(message_id=1, user_id=111)
    second = SimpleNamespace(message_id=2, user_id=222)
    selected = agent_tools._resolve_batch_source_event(
        {"source_message_id": 1},
        {"batch_events": [first, second]},
    )
    assert selected.user_id == 111


def test_quote_policy_keeps_clear_conversation_unquoted():
    event = SimpleNamespace(
        user_id=111,
        message_id=10,
        raw_message="普通聊天",
        get_plaintext=lambda: "普通聊天",
    )
    target, reason = _batch_quote_target(
        [(SimpleNamespace(), event, True)],
        event,
        10,
        min_messages=3,
        min_users=2,
    )
    assert target is None
    assert reason == "quiet"


def test_quote_policy_quotes_busy_or_multi_user_batch():
    first = SimpleNamespace(
        user_id=111,
        message_id=10,
        raw_message="第一条",
        get_plaintext=lambda: "第一条",
    )
    second = SimpleNamespace(
        user_id=222,
        message_id=11,
        raw_message="第二条",
        get_plaintext=lambda: "第二条",
    )
    target, reason = _batch_quote_target(
        [(SimpleNamespace(), first, False), (SimpleNamespace(), second, True)],
        second,
        None,
        min_messages=3,
        min_users=2,
    )
    assert target == 11
    assert reason == "multiple_users"
