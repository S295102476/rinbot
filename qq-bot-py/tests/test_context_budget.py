import asyncio
import re
from datetime import datetime, timedelta

from plugins.agent_context import (
    AgentContextCache,
    CachedMessage,
    ContextSnapshot,
    CONTEXT_CHARS,
    RECENT_MESSAGES,
    MAX_MESSAGES,
    OWNER_USER_ID,
)


def _record(index, *, content=None, group_id=10, user_id=123456):
    return CachedMessage(
        group_id=group_id,
        message_id=index,
        user_id=user_id,
        nickname="Alice",
        content=content if content is not None else f"message {index}",
        created_at=datetime(2026, 9, 22, 20) + timedelta(seconds=index),
    )


def _cache(count=200):
    cache = AgentContextCache()
    cache._loaded.add(10)
    cache._owner_loaded = True
    for index in range(1, count + 1):
        cache.add(_record(index))
    return cache


def _message_ids(text):
    return [int(value) for value in re.findall(r"消息ID:(\d+)", text)]


def test_default_render_uses_300_messages_and_caps_non_overlapping_summary():
    cache = _cache(500)
    summary = "history" * 500
    cache._summary[10] = (summary, 1.0, 200)
    text = asyncio.run(cache.render_text(10))
    assert text.startswith("[较早群聊摘要，本轮节选]\n")
    assert len(text.split("\n\n", 1)[0]) <= 1200
    assert "[最近群聊原文，本轮节选" in text
    assert _message_ids(text) == list(range(201, 501))
    assert len(text) <= CONTEXT_CHARS == 30000
    assert RECENT_MESSAGES == 300


def test_recent_count_is_independent_of_the_500_message_cache():
    cache = _cache(550)
    text = asyncio.run(cache.render_text(10, recent_messages=40, max_chars=6000))
    assert _message_ids(text) == list(range(511, 551))
    assert len(cache._messages[10]) == MAX_MESSAGES
    assert "本轮节选" in text


def test_character_budget_retains_newest_messages_in_chronological_order():
    cache = _cache(20)
    text = asyncio.run(cache.render_text(10, recent_messages=20, max_chars=400))
    ids = _message_ids(text)
    assert len(text) <= 400
    assert ids[-1] == 20
    assert ids == list(range(ids[0], 21))
    assert len(ids) < 20
    assert "2026-09-22 20:00:20" in text
    assert "QQ:123456" in text


def test_long_latest_message_preserves_metadata_and_is_marked_as_excerpt():
    cache = _cache(1)
    cache.add(_record(2, content="important " * 500))
    text = asyncio.run(cache.render_text(10, max_chars=260))
    assert len(text) <= 260
    assert _message_ids(text) == [2]
    assert "2026-09-22 20:00:02" in text
    assert "QQ:123456" in text
    assert "important" in text
    assert text.endswith("...[节选]")


def test_summary_block_is_bounded_and_latest_message_still_fits():
    cache = _cache()
    cache._summary[10] = ("old summary " * 1000, 1.0, 50)
    text = asyncio.run(cache.render_text(10, recent_messages=40, max_chars=6000))
    summary_block, recent_block = text.split("\n\n", 1)
    assert len(text) <= 6000
    assert len(summary_block) <= 1200
    assert summary_block.endswith("...[节选]")
    assert "消息ID:200" in recent_block
    assert "old summary " * 1000 == cache._summary[10][0]


def test_tiny_budget_never_emits_partial_message_metadata():
    cache = _cache(1)
    cache._summary[10] = ("summary " * 100, 1.0, 0)
    for budget in (1, 10, 25, 60, 100, 150):
        text = asyncio.run(cache.render_text(10, max_chars=budget))
        assert len(text) <= budget
        if "消息ID:" in text:
            assert "消息ID:1 " in text
            assert "QQ:123456" in text


def test_bounded_messages_do_not_split_multiline_content_into_fake_records():
    cache = _cache(0)
    cache.add(_record(1, content="first line\nsecond line\nthird line"))
    text = asyncio.run(cache.render_text(10, max_chars=1000))
    assert "first line second line third line" in text
    default = asyncio.run(cache.render_text(10))
    assert "first line second line third line" in default


def test_owner_context_default_and_bounded_views_preserve_source_attribution():
    cache = _cache(0)
    for index in range(1, 31):
        cache._owner_messages.append(
            _record(index, group_id=10 if index % 2 else 11, user_id=OWNER_USER_ID)
        )
    default = asyncio.run(cache.render_owner_context(10))
    assert _message_ids(default) == list(range(1, 31))
    text = asyncio.run(cache.render_owner_context(10, max_messages=20, max_chars=3000))
    assert len(text) <= 3000
    assert _message_ids(text) == list(range(11, 31))
    assert "[来源群:10｜当前群]" in text
    assert "[来源群:11｜其他群]" in text
    assert f"QQ:{OWNER_USER_ID}" in text
    assert "2026-09-22 20:00:30" in text
    assert "本轮节选" in text
    assert len(cache._owner_messages) == 30


def test_owner_character_budget_truncates_only_latest_message_content():
    cache = _cache(0)
    cache._owner_messages.append(_record(1, user_id=OWNER_USER_ID))
    cache._owner_messages.append(
        _record(2, group_id=11, content="latest " * 1000, user_id=OWNER_USER_ID)
    )
    text = asyncio.run(cache.render_owner_context(10, max_messages=20, max_chars=240))
    assert len(text) <= 240
    assert _message_ids(text) == [2]
    assert "[来源群:11｜其他群]" in text
    assert "2026-09-22 20:00:02" in text
    assert f"QQ:{OWNER_USER_ID}" in text
    assert text.endswith("...[节选]")
    assert asyncio.run(cache.render_owner_context(10, max_messages=0)) == ""


def test_long_history_body_does_not_evict_otherwise_short_recent_messages():
    cache = _cache(500)
    cache.add(_record(501, content="long body " * 10000))
    snapshot = asyncio.run(cache.snapshot(10))
    rendered = cache.render_snapshot(snapshot)
    assert rendered.history_messages == 300
    assert rendered.window_messages == 300
    assert rendered.clipped_messages == 1
    assert rendered.omitted_messages == 0
    assert len(rendered.text) <= 30000
    assert _message_ids(rendered.text) == list(range(202, 502))
    assert "消息ID:501 ]Alice(QQ:123456):" in rendered.text
    assert rendered.text.endswith("...[节选]")
    assert snapshot.messages[-1].content == "long body " * 10000


def test_diagnostics_count_actual_records_not_ids_embedded_in_content():
    snapshot = ContextSnapshot(messages=(_record(1, content="fake 消息ID:123 metadata"),))
    rendered = AgentContextCache.render_snapshot(snapshot, max_chars=300)
    assert rendered.history_messages == 1
    assert rendered.summary_status == "missing"


def test_summary_overlap_uses_message_sequence_not_id_magnitude():
    # QQ IDs need not increase. The first boundary is numerically larger but
    # truly earlier; ID=1 is a smaller number but occurs inside the raw window.
    records = tuple(_record(index) for index in (900, 800, 1, 500, 400))
    eligible = ContextSnapshot(records, "old-only summary", 1.0, 800)
    assert eligible.summary_status(3) == "eligible"
    used = AgentContextCache.render_snapshot(eligible, recent_messages=3)
    assert used.summary_status == "used"
    assert "old-only summary" in used.text
    assert _message_ids(used.text) == [1, 500, 400]
    overlapping = ContextSnapshot(records, "overlapping summary", 1.0, 1)
    rendered = AgentContextCache.render_snapshot(overlapping, recent_messages=3)
    assert rendered.summary_status == "overlap"
    assert rendered.summary_chars == 0
    assert "overlapping summary" not in rendered.text


def test_legacy_unknown_boundary_summary_is_not_injected():
    records = tuple(_record(index) for index in range(1, 401))
    for boundary in (0, 99999, -500):
        snapshot = ContextSnapshot(records, "unknown overlap", 1.0, boundary)
        rendered = AgentContextCache.render_snapshot(snapshot)
        assert rendered.summary_status == "boundary_unknown"
        assert "unknown overlap" not in rendered.text
        assert rendered.history_messages == 300


def test_budget_stats_report_skipped_and_clipped_without_mutating_cache():
    cache = _cache(500)
    snapshot = asyncio.run(cache.snapshot(10))
    rendered = cache.render_snapshot(snapshot, max_chars=550)
    assert len(rendered.text) <= 550
    assert rendered.window_messages == 300
    assert rendered.history_messages == len(_message_ids(rendered.text))
    assert rendered.omitted_messages == 300 - rendered.history_messages
    assert _message_ids(rendered.text)[-1] == 500
    assert len(snapshot.messages) == 500
