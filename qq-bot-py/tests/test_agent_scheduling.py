import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from types import ModuleType, SimpleNamespace
import sys

import pytest

from plugins import agent_runtime as module
from plugins.agent_context import AgentContextCache, CachedMessage, ContextSnapshot
from plugins.agent_memory import _unique_recalled_facts
from plugins.agent_requests import REQUEST_CONTEXT
from plugins.agent_runtime import AgentRuntime


def runtime_for_groups():
    runtime = AgentRuntime()
    runtime.policy = replace(
        runtime.policy, enabled=True, active_groups=frozenset({10, 20}),
        burst_window_seconds=0,
    )
    return runtime


def snapshot(last_id=500):
    return ContextSnapshot(messages=tuple(
        CachedMessage(10, i, 42, "user", "message")
        for i in range(last_id - 499, last_id + 1)
    ))


def test_slow_batch_coalesces_new_events_and_keeps_explicit_trigger():
    async def run():
        runtime = runtime_for_groups()
        started, release = asyncio.Event(), asyncio.Event()
        batches = []

        async def handle(group_id, items):
            batches.append([(event.message_id, force) for _, event, force in items])
            if len(batches) == 1:
                started.set()
                await release.wait()

        runtime._handle_group_batch = handle

        async def enqueue(message_id):
            await runtime.enqueue_group_event(
                None, SimpleNamespace(group_id=10, message_id=message_id),
                force_reply=message_id == 3,
            )

        await enqueue(1)
        worker = runtime._batch_tasks[10]
        await asyncio.wait_for(started.wait(), 1)
        for message_id in (2, 3, 4):
            await enqueue(message_id)
            await asyncio.sleep(0)
            assert runtime._batch_tasks[10] is worker
        assert len(batches) == 1
        release.set()
        await asyncio.wait_for(worker, 1)
        assert batches == [[(1, False)], [(2, False), (3, True), (4, False)]]
        assert not runtime._batch_tasks
        assert not runtime._batch_queues
        await enqueue(5)
        await asyncio.wait_for(runtime._batch_tasks[10], 1)
        assert batches[-1] == [(5, False)]

    asyncio.run(run())


def test_failed_batch_does_not_strand_pending_messages():
    async def run():
        runtime = runtime_for_groups()
        seen = []

        async def handle(group_id, items):
            seen.extend(event.message_id for _, event, _ in items)
            if seen == [1]:
                await runtime.enqueue_group_event(
                    None, SimpleNamespace(group_id=10, message_id=2), force_reply=False,
                )
                raise RuntimeError("test batch failure")

        runtime._handle_group_batch = handle
        await runtime.enqueue_group_event(
            None, SimpleNamespace(group_id=10, message_id=1), force_reply=False,
        )
        await asyncio.wait_for(runtime._batch_tasks[10], 1)
        assert seen == [1, 2]
        assert not runtime._batch_tasks

    asyncio.run(run())


def test_worker_cancellation_cleans_pending_queue_without_restart():
    async def run():
        runtime = runtime_for_groups()
        started = asyncio.Event()

        async def handle(*args):
            started.set()
            await asyncio.Event().wait()

        runtime._handle_group_batch = handle
        await runtime.enqueue_group_event(None, SimpleNamespace(group_id=10), force_reply=False)
        worker = runtime._batch_tasks[10]
        await asyncio.wait_for(started.wait(), 1)
        await runtime.enqueue_group_event(None, SimpleNamespace(group_id=10), force_reply=True)
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
        assert not runtime._batch_tasks
        assert not runtime._batch_queues

    asyncio.run(run())


def test_summary_serializes_groups_and_reads_latest_window_after_wait(monkeypatch):
    async def run():
        runtime = runtime_for_groups()
        current = {10: snapshot(), 20: snapshot()}
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def read(group_id):
            return current[group_id]

        async def generate(group_id, messages):
            calls.append((group_id, messages[-1].message_id, REQUEST_CONTEXT.get().copy()))
            if group_id == 10:
                started.set()
                await release.wait()

        monkeypatch.setattr(module.CACHE, "snapshot", read)
        runtime._generate_summary = generate
        runtime._schedule_summary_refresh(10, current[10])
        first = runtime._summary_tasks[10]
        await asyncio.wait_for(started.wait(), 1)
        runtime._schedule_summary_refresh(20, current[20])
        second = runtime._summary_tasks[20]
        await asyncio.sleep(0)
        runtime._schedule_summary_refresh(20, current[20])
        assert runtime._summary_tasks[20] is second
        assert len(calls) == 1
        current[20] = snapshot(550)
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 1)
        assert [(group, last_id) for group, last_id, _ in calls] == [(10, 200), (20, 250)]
        assert all(ctx["purpose"] == "summary" and ctx["timeout"] == 180 for _, _, ctx in calls)
        assert calls[0][2]["request_id"] != calls[1][2]["request_id"]
        assert not runtime._summary_tasks

    asyncio.run(run())


def test_context_defaults_and_console_overrides_feed_the_same_summary_boundary(monkeypatch):
    overrides = {}
    monkeypatch.setattr(module, "GROUP_CONFIG", {})
    monkeypatch.setattr(module.CONSOLE, "global_value", lambda key, default: overrides.get(key, default))
    assert module._decision_context_limits(False) == (300, 30000)
    assert module._decision_context_limits(True) == (300, 30000)
    runtime = runtime_for_groups()
    assert len(runtime._summary_messages(10, snapshot())) == 200
    overrides.update(decision_recent_messages=250, decision_direct_recent_messages=350,
                     decision_context_chars=20000, decision_direct_context_chars=32000)
    assert module._decision_context_limits(False) == (250, 20000)
    assert module._decision_context_limits(True) == (350, 32000)
    assert runtime._summary_messages(10, snapshot()) == snapshot().messages[:150]


def test_followup_context_uses_effective_prompt_limits_not_legacy_cache_limit(monkeypatch):
    async def run():
        captured = []

        async def render(group_id, **kwargs):
            captured.append((group_id, kwargs))
            return "current context"

        monkeypatch.setattr(module.CACHE, "render_text", render)
        monkeypatch.setattr(module, "_decision_context_limits", lambda force_reply=False: (300, 30000))
        assert await module._recent_group_context(10, 500) == "current context"
        assert captured == [(10, {"recent_messages": 300, "max_chars": 30000})]
        monkeypatch.setattr(module, "_decision_context_limits", lambda force_reply=False: (250, 20000))
        await module._recent_group_context(10, 150)
        assert captured[-1] == (10, {"recent_messages": 250, "max_chars": 20000})

    asyncio.run(run())


def test_overlapping_summary_rebuilds_immediately_without_waiting_for_50_messages(monkeypatch):
    monkeypatch.setattr(module, "_decision_context_limits", lambda force_reply=False: (300, 30000))
    runtime = runtime_for_groups()
    current = replace(snapshot(), summary="old350", summary_covered_message_id=350)
    assert runtime._messages_since_summary[10] == 0
    assert len(runtime._summary_messages(10, current)) == 200
    # Once rebuilt, no immediate duplicate summary request. Refresh after 50
    # new records, still from the old-only window rather than all 500 messages.
    current = replace(current, summary="old200", summary_covered_message_id=200)
    assert runtime._summary_messages(10, current) == ()
    advanced = replace(snapshot(549), summary="old200", summary_covered_message_id=200)
    assert runtime._summary_messages(10, advanced) == ()
    advanced = replace(snapshot(550), summary="old200", summary_covered_message_id=200)
    assert len(runtime._summary_messages(10, advanced)) == 200
    assert runtime._summary_messages(10, advanced)[-1].message_id == 250


def test_summary_boundary_order_and_short_caches_do_not_reuse_unknown_ids(monkeypatch):
    monkeypatch.setattr(module, "_decision_context_limits", lambda force_reply=False: (300, 30000))
    runtime = runtime_for_groups()
    records = tuple(replace(record, message_id=1000 - index) for index, record in enumerate(snapshot().messages))
    current = ContextSnapshot(records, "valid-old", 1.0, records[199].message_id)
    assert runtime._summary_messages(10, current) == ()
    current = replace(current, summary_covered_message_id=records[349].message_id)
    assert runtime._summary_messages(10, current) == records[:200]
    current = replace(current, summary_covered_message_id=99999)
    assert runtime._summary_messages(10, current) == records[:200]
    assert runtime._summary_messages(10, replace(current, messages=records[-300:])) == ()


def test_refreshing_overlapping_summary_never_blocks_current_raw_context(monkeypatch):
    async def run():
        runtime = runtime_for_groups()
        monkeypatch.setattr(module, "_decision_context_limits", lambda force_reply=False: (300, 30000))
        current = replace(snapshot(), summary="do-not-inject", summary_covered_message_id=350)
        started, release = asyncio.Event(), asyncio.Event()

        async def read(group_id):
            return current

        async def generate(group_id, messages):
            assert len(messages) == 200
            started.set()
            await release.wait()

        monkeypatch.setattr(module.CACHE, "snapshot", read)
        runtime._generate_summary = generate
        runtime._schedule_summary_refresh(10, current)
        task = runtime._summary_tasks[10]
        await asyncio.wait_for(started.wait(), 1)
        rendered = AgentContextCache.render_snapshot(current)
        assert rendered.summary_status == "overlap"
        assert rendered.history_messages == 300
        assert "do-not-inject" not in rendered.text
        assert not task.done()
        release.set()
        await asyncio.wait_for(task, 1)

    asyncio.run(run())


def test_summary_failure_backoff_and_success_reset_preserve_new_message_count(monkeypatch):
    async def run():
        runtime = runtime_for_groups()
        clock = [1000.0]
        current = snapshot()
        fail = [True]

        async def read(group_id):
            return current

        async def generate(group_id, messages):
            if fail[0]:
                raise TimeoutError()
            runtime._messages_since_summary[group_id] += 7

        monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
        monkeypatch.setattr(module.CACHE, "snapshot", read)
        runtime._generate_summary = generate
        for expected_delay in (60, 120, 300, 300):
            runtime._schedule_summary_refresh(10, current)
            await runtime._summary_tasks[10]
            assert runtime._summary_retry_at[10] == clock[0] + expected_delay
            runtime._schedule_summary_refresh(10, current)
            assert not runtime._summary_tasks
            clock[0] += expected_delay
        fail[0] = False
        runtime._messages_since_summary[10] = 80
        runtime._schedule_summary_refresh(10, current)
        await runtime._summary_tasks[10]
        assert runtime._messages_since_summary[10] == 7
        assert 10 not in runtime._summary_retry_at
        assert 10 not in runtime._summary_failures

    asyncio.run(run())


def test_cancelled_summary_releases_global_slot(monkeypatch):
    async def run():
        runtime = runtime_for_groups()
        started = asyncio.Event()
        seen = []

        async def read(group_id):
            return snapshot()

        async def generate(group_id, messages):
            seen.append(group_id)
            if group_id == 10:
                started.set()
                await asyncio.Event().wait()

        monkeypatch.setattr(module.CACHE, "snapshot", read)
        runtime._generate_summary = generate
        runtime._schedule_summary_refresh(10, snapshot())
        first = runtime._summary_tasks[10]
        await asyncio.wait_for(started.wait(), 1)
        runtime._schedule_summary_refresh(20, snapshot())
        second = runtime._summary_tasks[20]
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        await asyncio.wait_for(second, 1)
        assert seen == [10, 20]
        assert not runtime._summary_tasks
        assert not runtime._summary_retry_at

    asyncio.run(run())


def test_summary_generation_passes_its_http_timeout_and_cancels_slow_call(monkeypatch):
    async def run():
        runtime = runtime_for_groups()
        runtime.summary_timeout = 0.02
        captured = {}
        fake_ai = ModuleType("plugins.ai_chat")

        async def call(messages, **kwargs):
            captured.update(kwargs)
            try:
                await asyncio.Event().wait()
            finally:
                captured["cancelled"] = True

        fake_ai._call_primary = call
        monkeypatch.setitem(sys.modules, "plugins.ai_chat", fake_ai)
        with pytest.raises(TimeoutError):
            await runtime._generate_summary(10, snapshot().messages[:200])
        assert captured == {"timeout": 0.02, "cancelled": True}

    asyncio.run(run())


def test_decision_uses_independent_deadline_and_resets_context():
    async def run():
        runtime = runtime_for_groups()
        runtime.decision_timeout = 0.02
        runtime._messages = lambda **kwargs: []
        captured = {}

        async def complete(messages):
            captured.update(REQUEST_CONTEXT.get())
            await asyncio.Event().wait()

        runtime._complete = complete
        with pytest.raises(TimeoutError):
            await runtime._decide(scope="group", request="hello", group_id=10)
        assert captured["purpose"] == "group_decision"
        assert captured["group_id"] == 10
        assert captured["timeout"] == 0.02
        assert REQUEST_CONTEXT.get() is None

    asyncio.run(run())


def test_stalled_summary_snapshot_releases_slot_and_backs_off(monkeypatch):
    async def run():
        runtime = runtime_for_groups()
        runtime.summary_storage_timeout = 0.02
        completed = []

        async def read(group_id):
            if group_id == 10:
                await asyncio.Event().wait()
            return snapshot()

        async def generate(group_id, messages):
            completed.append(group_id)

        monkeypatch.setattr(module.CACHE, "snapshot", read)
        runtime._generate_summary = generate
        runtime._schedule_summary_refresh(10, snapshot())
        runtime._schedule_summary_refresh(20, snapshot())
        await asyncio.wait_for(asyncio.gather(*runtime._summary_tasks.values()), 1)
        assert completed == [20]
        assert 10 in runtime._summary_retry_at
        assert not runtime._summary_tasks

    asyncio.run(run())


def test_stalled_summary_storage_preserves_previous_hot_summary_and_new_jobs_work(monkeypatch):
    async def run():
        runtime = runtime_for_groups()
        runtime.summary_storage_timeout = 0.02
        stored = []
        cancelled = []
        fake_ai = ModuleType("plugins.ai_chat")

        async def call(messages, **kwargs):
            return '{"summary":"fresh summary","facts":[]}'

        async def read(group_id):
            return snapshot()

        async def persist(group_id, *args):
            if group_id == 10:
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.append(group_id)

        fake_ai._call_primary = call
        monkeypatch.setitem(sys.modules, "plugins.ai_chat", fake_ai)
        monkeypatch.setattr(module.CACHE, "snapshot", read)
        monkeypatch.setattr(module.CACHE, "set_summary", lambda *args: stored.append(args))
        runtime._persist_summary = persist
        runtime._schedule_summary_refresh(10, snapshot())
        runtime._schedule_summary_refresh(20, snapshot())
        await asyncio.wait_for(asyncio.gather(*runtime._summary_tasks.values()), 1)
        # Only the protected persistence path may update the cache. A stalled
        # or replaced storage task must not publish an unchecked summary.
        assert stored == []
        assert cancelled == [10]
        assert not runtime._summary_tasks

    asyncio.run(run())


def test_fact_prompt_dedup_keeps_different_users_and_meanings():
    def fact(user_id, text):
        return SimpleNamespace(user_id=user_id, category="preference", fact=text)

    first = fact(1, "Likes tea")
    duplicate = fact(1, "Likes   tea")
    second = fact(2, "Likes tea")
    third = fact(1, "Does not like tea")
    assert _unique_recalled_facts([first, duplicate, second, third], 3) == [first, second, third]


def test_decision_queue_timeout_never_dispatches_and_cancellation_releases_slot():
    async def run():
        runtime = runtime_for_groups()
        runtime._decision_semaphore = asyncio.Semaphore(1)
        runtime.decision_queue_timeout = 0.02
        started = asyncio.Event()
        calls = []

        async def complete(messages):
            calls.append(dict(REQUEST_CONTEXT.get()))
            if len(calls) == 1:
                started.set()
                await asyncio.Event().wait()
            return "ok"

        runtime._complete = complete
        first = asyncio.create_task(runtime._request_decision([], "group", 10, 1, 1))
        await asyncio.wait_for(started.wait(), 1)
        with pytest.raises(TimeoutError):
            await runtime._request_decision([], "group", 20, 1, 1)
        assert len(calls) == 1
        assert runtime._decision_semaphore.locked()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert await runtime._request_decision([], "group", 20, 1, 1) == "ok"
        assert [ctx["group_id"] for ctx in calls] == [10, 20]
        assert calls[-1]["backend_search"] is True
        assert REQUEST_CONTEXT.get() is None
        assert not runtime._decision_semaphore.locked()

    asyncio.run(run())


def test_waiting_for_decision_slot_does_not_consume_provider_deadline():
    async def run():
        runtime = runtime_for_groups()
        runtime._decision_semaphore = asyncio.Semaphore(1)
        runtime.decision_queue_timeout = 0.01
        await runtime._decision_semaphore.acquire()
        started = asyncio.Event()

        async def complete(messages):
            started.set()
            return "ok"

        runtime._complete = complete
        task = asyncio.create_task(runtime._request_decision(
            [], "group", 10, 0.01, 1, force_reply=True,
        ))
        await asyncio.sleep(0.03)
        assert not task.done()
        assert not started.is_set()
        runtime._decision_semaphore.release()
        assert await asyncio.wait_for(task, 1) == "ok"

    asyncio.run(run())


@pytest.mark.parametrize("text,expected", [
    ("普通闲聊", (False, False)),
    ("这张图是谁", (True, False)),
    ("搜索今天的天气", (False, False)),
])
def test_visual_and_search_gates_only_add_expensive_inputs_when_needed(text, expected):
    event = SimpleNamespace(
        get_plaintext=lambda: text,
        message=[],
    )
    assert module._batch_visual_needs([(None, event, False)]) == expected
    assert module._request_needs_backend_search(text) is ("搜索" in text)


@pytest.mark.parametrize("quoted_count", [0, 1, 2, 3])
def test_decision_image_budget_includes_quotes_but_not_avatar(monkeypatch, quoted_count):
    async def run():
        runtime = runtime_for_groups()
        monkeypatch.setitem(module.GROUP_CONFIG, "decision_images", 3)
        monkeypatch.setitem(module.GROUP_CONFIG, "decision_avatars", 1)
        selected = {}

        def image_parts(count, label):
            return [part for index in range(count) for part in (
                {"type": "text", "text": f"{label}-{index}"},
                {"type": "image_url", "image_url": {"url": f"{label}-{index}"}},
            )]

        async def quoted(*args):
            return image_parts(quoted_count, "quoted")

        async def context(*args, **kwargs):
            selected.update(kwargs)
            return image_parts(kwargs["max_images"], "chat")

        async def avatars(*args, **kwargs):
            assert kwargs["limit"] == 1
            return image_parts(1, "avatar")

        runtime._quoted_reply_images = quoted
        runtime._context_images = context
        runtime._context_avatars = avatars
        event = SimpleNamespace(
            get_plaintext=lambda: "",
            message=[],
        )
        result = await runtime._decision_images(
            None, 10, ContextSnapshot(messages=()), [(None, event, True)]
        )
        assert selected["max_images"] == 3 - quoted_count
        assert sum(part["type"] == "image_url" for part in result) == 4
        labels = [part["text"] for part in result if part["type"] == "text"]
        assert labels[:quoted_count] == [f"quoted-{index}" for index in range(quoted_count)]
        assert labels[-1] == "avatar-0"

    asyncio.run(run())


def test_current_images_survive_history_gate_and_old_history_is_not_downloaded(monkeypatch):
    async def run():
        runtime = runtime_for_groups()
        cache = AgentContextCache()
        now = datetime.now()
        messages = tuple(
            CachedMessage(
                10, index, 42, "user", "image", image_url=f"image-{index}",
                created_at=now - timedelta(seconds=age),
            )
            for index, age in [(1, 3600), (2, 120), (3, 0)]
        )
        for item in messages:
            cache.put_image_payload(10, item.image_url, "image/jpeg", b"test")
        monkeypatch.setattr(module, "CACHE", cache)
        fake_ai = ModuleType("plugins.ai_chat")
        fake_ai._read_group_img_b64 = lambda *args: pytest.fail("unexpected download")
        monkeypatch.setitem(sys.modules, "plugins.ai_chat", fake_ai)
        current = [SimpleNamespace(message_id=3)]
        snap = ContextSnapshot(messages=messages)
        parts = await runtime._context_images(10, snap, current, history_limit=0)
        assert sum(part["type"] == "image_url" for part in parts) == 1
        labels = " ".join(part.get("text", "") for part in parts)
        assert "\u6d88\u606fID:3" in labels
        parts = await runtime._context_images(10, snap, current, max_images=1)
        assert sum(part["type"] == "image_url" for part in parts) == 1
        labels = " ".join(part.get("text", "") for part in parts)
        assert "\u6d88\u606fID:3" in labels
        assert "\u6d88\u606fID:2" not in labels
        assert await runtime._context_images(10, snap, current, max_images=0) == []
        assert "\u6d88\u606fID:1" not in labels
        assert "\u5f53\u524d\u6279\u6b21\u56fe\u7247 1/1" in labels
        parts = await runtime._context_images(
            10, snap, current, history_limit=2, history_max_age_seconds=600,
        )
        assert sum(part["type"] == "image_url" for part in parts) == 2
        labels = " ".join(part.get("text", "") for part in parts)
        assert "\u6d88\u606fID:1" not in labels
        assert "\u6d88\u606fID:2" in labels
        assert "\u6d88\u606fID:3" in labels

    asyncio.run(run())


def test_avatar_budget_prioritizes_mentioned_target_over_sender(monkeypatch):
    async def run():
        runtime = runtime_for_groups()
        fake_coordination = ModuleType("plugins.chat_coordination")

        async def resolve_reply_context(*args):
            return None

        fake_coordination.resolve_reply_context = resolve_reply_context
        monkeypatch.setitem(sys.modules, "plugins.chat_coordination", fake_coordination)
        selected = []

        async def avatars(user_ids):
            selected.extend(user_ids)
            return []

        monkeypatch.setattr(module.CACHE, "avatar_payloads", avatars)
        event = SimpleNamespace(
            user_id=42, sender=SimpleNamespace(nickname="sender"),
            message=[SimpleNamespace(type="at", data={"qq": "99"})],
        )
        bot = SimpleNamespace(self_id=100)
        await runtime._context_avatars(bot, 10, [(bot, event, True)], limit=1)
        assert selected == [99]

    asyncio.run(run())
