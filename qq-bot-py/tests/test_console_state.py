"""Console persistence tests use only an in-memory SQLite database."""

import asyncio
import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from plugins import console_state as state


class AsyncSessionAdapter:
    """Exercise the production statements without a second async DB driver."""

    def __init__(self, engine, fail_commit=False):
        self.session = Session(engine, expire_on_commit=False)
        self.fail_commit = fail_commit

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.session.close()

    def add(self, value):
        self.session.add(value)

    def get_bind(self):
        return self.session.get_bind()

    async def execute(self, *args, **kwargs):
        return self.session.execute(*args, **kwargs)

    async def get(self, *args, **kwargs):
        return self.session.get(*args, **kwargs)

    async def flush(self):
        self.session.flush()

    async def commit(self):
        if self.fail_commit:
            raise OSError("simulated database outage")
        self.session.commit()

    async def rollback(self):
        self.session.rollback()

    async def delete(self, value):
        self.session.delete(value)


@pytest.fixture
def stores(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool)
    state.db.Base.metadata.create_all(engine, tables=state.CONSOLE_TABLES)
    now = datetime(2026, 9, 23, 15, 30)
    monkeypatch.setattr(state, "local_now", lambda: now)
    # Column defaults can already be captured in ORM-compiled INSERT state by
    # earlier test modules. Set the frozen timestamp on pending audit objects
    # before INSERT instead of mutating a cached default callable globally.
    def freeze_audit_timestamp(_mapper, _connection, target):
        if target.created_at is None:
            target.created_at = state.local_now()

    event.listen(state.ConsoleAudit, "before_insert", freeze_audit_timestamp)

    async def factory():
        return AsyncSessionAdapter(engine)

    config = {"agent": {"enabled": True, "active_groups": [100], "group": {"daily_reply_limit": 300, "group_overrides": {100: {"hourly_reply_soft_limit": 42}}}}, "dev_scope": {"enabled": True, "allowed_groups": [100, 200]}}
    settings = state.SettingsStore(config, factory)
    stats = state.StatsStore(factory)
    quota = state.QuotaStore(settings, stats, factory)
    try:
        asyncio.run(settings.load())
        yield engine, settings, stats, quota, factory, config
    finally:
        event.remove(state.ConsoleAudit, "before_insert", freeze_audit_timestamp)
        engine.dispose()


def run(coroutine):
    return asyncio.run(coroutine)


def set_count(engine, group_id, count, day=None):
    with Session(engine) as session:
        key = group_id, day or state.local_now().date()
        row = session.get(state.ConsoleQuotaDay, key)
        if row is None:
            row = state.ConsoleQuotaDay(group_id=key[0], day=key[1], reserved=0)
            session.add(row)
        row.count = count
        session.commit()


def test_initial_limits_preserved_but_new_groups_default_200(stores):
    _, settings, _, _, _, _ = stores
    assert settings.group(100)["daily_reply_limit"] == 300
    assert settings.group(100)["hourly_reply_soft_limit"] == 42
    assert settings.group(200)["daily_reply_limit"] == 200
    assert settings.group(200)["hourly_reply_soft_limit"] == 30
    assert settings.active_groups() == {100}


def test_context_defaults_and_six_field_update_preserve_unrelated_overrides(stores):
    engine, settings, _, _, factory, config = stores
    expected = {
        "decision_recent_messages": 300,
        "decision_direct_recent_messages": 300,
        "decision_context_chars": 30000,
        "decision_direct_context_chars": 30000,
        "decision_images": 3,
        "decision_avatars": 1,
    }
    assert {key: settings.global_values()[key] for key in expected} == expected
    run(settings.update_global({
        "decision_recent_messages": 40, "decision_direct_recent_messages": 60,
        "decision_context_chars": 5000, "decision_direct_context_chars": 5000,
        "model": "keep-model", "decision_timeout_seconds": 120,
        "decision_backend_search": True,
    }, actor="old-config"))
    restarted = state.SettingsStore(config, factory)
    run(restarted.load())
    # Existing console values keep precedence; shipping new defaults must not
    # silently overwrite a persistent administrator decision.
    assert restarted.global_values()["decision_recent_messages"] == 40
    version = restarted.global_values()["version"]
    run(restarted.update_global(expected, actor="context-300-deploy", version=version))
    restored = state.SettingsStore(config, factory)
    run(restored.load())
    values = restored.global_values()
    assert {key: values[key] for key in expected} == expected
    assert values["model"] == "keep-model"
    assert values["decision_timeout_seconds"] == 120
    assert values["decision_backend_search"] is True
    assert restored.group(100)["hourly_reply_soft_limit"] == 42
    with Session(engine) as session:
        audit = session.scalars(select(state.ConsoleAudit).where(state.ConsoleAudit.actor == "context-300-deploy")).one()
        assert set(json.loads(audit.detail_json)["fields"]) == set(expected)


def test_settings_persist_restart_conflict_and_inherit(stores):
    _, settings, _, _, factory, config = stores
    result = run(settings.update_group(200, {"mode": "at", "daily_reply_limit": 100}, version=0))
    assert result["version"] == 1 and result["enabled"]
    restart = state.SettingsStore(config, factory)
    run(restart.load())
    assert restart.group(200) == result
    with pytest.raises(state.VersionConflict):
        run(restart.update_group(200, {"mode": "off"}, version=0))
    run(settings.update_global({"daily_reply_limit": 250}, version=0))
    inherited = run(settings.update_group(200, {"daily_reply_limit": None}, version=1))
    assert inherited["daily_reply_limit"] == 250
    assert "daily_reply_limit" in inherited["inherited"]
    assert settings.group(100)["daily_reply_limit"] == 300
    run(settings.reset_group(100))
    run(restart.load())
    assert restart.group(100)["enabled"]
    assert restart.group(100)["daily_reply_limit"] == 250


def test_settings_failed_save_does_not_change_cache_or_database(stores):
    engine, settings, _, _, _, _ = stores
    before = settings.global_values()

    async def broken_factory():
        return AsyncSessionAdapter(engine, fail_commit=True)

    settings._session_factory = broken_factory
    with pytest.raises(OSError):
        run(settings.update_global({"decision_images": 0}))
    assert settings.global_values() == before
    with Session(engine) as session:
        assert session.get(state.ConsoleSetting, "global") is None


@pytest.mark.parametrize("patch", [
    {"model": "secret\nmodel"}, {"decision_images": -1},
    {"decision_concurrency": True}, {"decision_context_chars": 10},
    {"test_groups": [False]}, {"test_groups": ["100"]}, {"api_key": "never"},
])
def test_settings_validation(stores, patch):
    with pytest.raises(ValueError):
        run(stores[1].update_global(patch))


def test_incoming_dedup_uses_bot_group_and_message_and_redacts_content(stores):
    engine, _, stats, _, _, _ = stores
    assert run(stats.record_event("incoming", 100, 9, message_id=22, bot_id=3, message="private", prompt="private", api_key="private"))
    assert not run(stats.record_event("incoming", 100, 9, message_id=22, bot_id=3))
    assert run(stats.record_event("incoming", 100, 9, message_id=22, bot_id=4))
    assert run(stats.record_event("incoming", 200, 9, message_id=22, bot_id=3))
    assert run(stats.record_event("interaction", 100, 9, message_id=22, bot_id=3))
    summary = run(stats.summary())
    assert summary["totals"]["incoming"] == 3
    assert summary["totals"]["interaction"] == 1
    assert summary["active_groups"] == 2 and summary["active_users"] == 1
    with Session(engine) as session:
        assert all("private" not in event.detail_json for event in session.scalars(select(state.ConsoleEvent)))


def test_request_tokens_remain_unknown_and_audit_redacts_text(stores):
    engine, _, stats, _, _, _ = stores
    run(stats.record_event("model_request", 100, provider="antigravity", model="example-model", success=True, latency_ms=42))
    request = run(stats.requests())["items"][0]
    assert request["input_tokens"] is None and request["total_tokens"] is None
    assert request["status"] == "ok" and request["latency_ms"] == 42
    run(stats.audit("memory.edit", "fact:12", {"fields": ["fact"], "fact": "private text", "api_key": "secret"}))
    audit = run(stats.audits())["items"][0]
    assert audit["detail"] == {"fields": ["fact"]}
    assert audit["created_at"].endswith("+08:00")
    with Session(engine) as session:
        assert "private" not in session.scalars(select(state.ConsoleAudit)).one().detail_json


@pytest.mark.parametrize("limit,soft,hard", [(100, 100, 150), (200, 200, 300), (1, 1, 2), (3, 3, 5)])
def test_quota_soft_and_hard_boundaries(stores, limit, soft, hard):
    engine, settings, _, quota, _, _ = stores
    run(settings.update_group(200, {"mode": "auto", "daily_reply_limit": limit}))
    set_count(engine, 200, soft - 1)
    claim = run(quota.reserve(200, False))
    assert claim
    assert run(quota.reserve(200, False)) is None
    assert run(quota.finish(claim, True, user_id=9, message_count=3))
    assert run(quota.status(200))["count"] == soft
    assert run(quota.status(200))["stage"] == "at_only"
    assert run(quota.reserve(200, False)) is None
    explicit_claim = run(quota.reserve(200, True))
    assert explicit_claim
    run(quota.finish(explicit_claim, False))
    set_count(engine, 200, hard)
    assert run(quota.reserve(200, True)) is None
    assert run(quota.status(200))["stage"] == "hard_limit"


def test_partial_success_counts_one_round_and_finish_is_idempotent(stores):
    _, _, stats, quota, _, _ = stores
    claim = run(quota.reserve(100, True))
    assert run(quota.finish(claim, True, user_id=9, message_count=2))
    assert not run(quota.finish(claim, True, user_id=9, message_count=2))
    assert run(quota.status(100))["count"] == 1
    assert run(quota.status(100))["hour_count"] == 1
    assert run(stats.summary())["totals"]["reply_round"] == 1
    failed = run(quota.reserve(100, True))
    assert not run(quota.finish(failed, True, message_count=0))
    assert run(quota.status(100))["count"] == 1
    assert run(quota.status(100))["reserved"] == 0


def test_quota_reservation_survives_restart_as_ambiguous_without_recount(stores):
    _, settings, stats, quota, factory, _ = stores
    claim = run(quota.reserve(100, True))
    restarted = state.QuotaStore(settings, stats, factory)
    assert run(restarted.recover_pending()) == 1
    assert not run(restarted.finish(claim, True))
    status = run(restarted.status(100))
    assert status["count"] == 0 and status["reserved"] == 1 and status["uncertain"] == 1


def test_quota_zero_unlimited_and_rollout_scoped_to_test_groups(stores):
    engine, settings, _, quota, _, _ = stores
    run(settings.update_group(100, {"daily_reply_limit": 0}))
    set_count(engine, 100, 9999)
    assert run(quota.reserve(100, False))
    assert run(quota.status(100))["stage"] == "unlimited"
    assert run(quota.claim_notice(100)) is None
    set_count(engine, 300, 9999)
    assert run(quota.reserve(300, False))
    assert not run(quota.status(300))["enforced"]


def test_notices_highest_only_no_duplicate_and_failed_notice_retry(stores):
    engine, settings, _, quota, _, _ = stores
    run(settings.update_group(200, {"daily_reply_limit": 200}))
    set_count(engine, 200, 99)
    assert run(quota.claim_notice(200)) is None
    set_count(engine, 200, 100)
    first = run(quota.claim_notice(200))
    assert first["threshold"] == 50
    assert run(quota.claim_notice(200)) is None
    run(quota.release_notice(first))
    retried = run(quota.claim_notice(200))
    assert retried["threshold"] == 50 and retried["claim"] != first["claim"]
    run(quota.finish_notice(retried))
    assert run(quota.claim_notice(200)) is None
    set_count(engine, 200, 200)
    jumped = run(quota.claim_notice(200))
    assert jumped["threshold"] == 100
    run(quota.finish_notice(jumped))
    assert run(quota.claim_notice(200)) is None
    set_count(engine, 200, 300)
    assert run(quota.claim_notice(200))["threshold"] == 150


def test_restart_never_replays_ambiguous_notice(stores):
    engine, _, _, quota, _, _ = stores
    set_count(engine, 100, 150)
    notice = run(quota.claim_notice(100))
    run(quota.recover_pending())
    run(quota.release_notice(notice))
    assert run(quota.claim_notice(100)) is None


def test_shanghai_midnight_rollover(stores, monkeypatch):
    engine, _, stats, quota, _, _ = stores
    before = datetime(2026, 9, 23, 23, 59, 59)
    monkeypatch.setattr(state, "local_now", lambda: before)
    run(stats.record_event("incoming", 100, 9, message_id=1))
    set_count(engine, 100, 450)
    assert run(quota.reserve(100, True)) is None
    after = before + timedelta(seconds=2)
    monkeypatch.setattr(state, "local_now", lambda: after)
    assert run(quota.status(100))["count"] == 0
    assert run(quota.reserve(100, False))
    run(stats.record_event("incoming", 100, 9, message_id=2))
    summary = run(stats.summary("2026-09-23", "2026-09-24"))
    assert [day["incoming"] for day in summary["daily"]] == [1, 1]


def test_activity_aggregates_multiple_days_and_groups(stores, monkeypatch):
    _, _, stats, _, _, _ = stores
    run(stats.record_event("incoming", 100, 9))
    run(stats.record_event("interaction", 100, 9))
    run(stats.record_event("direct_at", 100, 9))
    run(stats.record_event("incoming", 200, 10))
    monkeypatch.setattr(state, "local_now", lambda: datetime(2026, 9, 24, 12))
    run(stats.record_event("incoming", 100, 9))
    rows = run(stats.activity("2026-09-23", "2026-09-24", limit=1))
    assert rows["total"] == 2
    assert rows["items"][0]["user_id"] == 9
    assert rows["items"][0]["incoming"] == 2
    assert rows["items"][0]["direct_at"] == 1


def test_retention_keeps_daily_and_audit_365_days_events_30(stores):
    engine, _, stats, _, _, _ = stores
    now = state.local_now()
    with Session(engine) as session:
        session.add(state.ConsoleEvent(id="old", kind="incoming", group_id=100, user_id=9, created_at=now - timedelta(days=31)))
        session.add(state.ConsoleRequest(id="old", created_at=now - timedelta(days=31)))
        session.add(state.ConsoleAudit(action="old", target="test", created_at=now - timedelta(days=366)))
        session.add(state.ConsoleAudit(action="kept", target="test", created_at=now - timedelta(days=31)))
        session.add(state.ConsoleDailyStat(day=now.date() - timedelta(days=31), group_id=100, user_id=9, kind="incoming", count=3))
        session.add(state.ConsoleDailyStat(day=now.date() - timedelta(days=366), group_id=100, user_id=9, kind="incoming", count=3))
        session.commit()
    run(stats.prune())
    with Session(engine) as session:
        assert not session.scalars(select(state.ConsoleEvent)).all()
        assert not session.scalars(select(state.ConsoleRequest)).all()
        assert len(session.scalars(select(state.ConsoleAudit)).all()) == 1
        assert len(session.scalars(select(state.ConsoleDailyStat)).all()) == 1


def test_reply_completing_after_midnight_counts_new_day(stores, monkeypatch):
    engine, _, _, quota, _, _ = stores
    before = datetime(2026, 9, 23, 23, 59, 59)
    monkeypatch.setattr(state, "local_now", lambda: before)
    claim = run(quota.reserve(100, False))
    monkeypatch.setattr(state, "local_now", lambda: before + timedelta(seconds=2))
    run(quota.finish(claim, True, user_id=9))
    assert run(quota.status(100))["count"] == 1
    with Session(engine) as session:
        yesterday = session.get(state.ConsoleQuotaDay, (100, before.date()))
        assert yesterday.count == 0 and yesterday.reserved == 0


def test_request_filters_and_real_measurements(stores):
    _, _, stats, _, _, _ = stores
    run(stats.record_event("model_request", 100, event_key="attempt-1", request_id="req1", source="summary", status="timeout", input_chars=123, image_count=3, queue_wait_ms=4, attempt=1))
    run(stats.record_event("model_request", 100, event_key="attempt-2", request_id="req2", source="group_decision", status="success", input_tokens=33, output_tokens=1))
    rows = run(stats.requests(status="timeout", source="summary"))
    assert rows["total"] == 1
    assert rows["items"][0]["request_id"] == "req1"
    assert rows["items"][0]["input_chars"] == 123
    assert rows["items"][0]["input_tokens"] is None


def test_durable_health_detects_restart_gap(stores, monkeypatch):
    _, _, stats, _, _, _ = stores
    run(stats.health(heartbeat=True))
    monkeypatch.setattr(state, "local_now", lambda: datetime(2026, 9, 23, 16, 0))
    assert run(stats.health(heartbeat=True))["gap_detected"]
    assert run(stats.health())["gap_detected"]
