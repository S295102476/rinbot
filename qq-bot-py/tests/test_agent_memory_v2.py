"""Synthetic SQLite integration tests. Never import the configured live DB."""
import asyncio
from datetime import datetime, timedelta
import importlib
import io
import json
from pathlib import Path
import sys
import types

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase


PKG = "_memory_v2_tests"
package = types.ModuleType(PKG)
package.__path__ = [str(Path(__file__).resolve().parents[1]/"plugins")]
sys.modules[PKG] = package
database = types.ModuleType(PKG+".db")
class Base(DeclarativeBase):
    pass
database.Base = Base
async def absent_session():
    raise AssertionError("configured DB must never be touched")
database.get_session = absent_session
sys.modules[PKG+".db"] = database
protection = types.ModuleType(PKG+".console_services")
protection.protected = set()
async def is_protected(session, kind, scope, **kwargs):
    return kwargs.get("identity") in protection.protected
protection.is_memory_protected = is_protected
from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column
class FakeAudit(Base):
    __tablename__ = "test_console_audit"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    action: Mapped[str] = mapped_column(String(100))
    target: Mapped[str] = mapped_column(String(100))
    actor: Mapped[str] = mapped_column(String(100))
def audit(session, action, target, detail, actor):
    session.add(FakeAudit(action=action, target=target, actor=actor))
protection._audit = audit
sys.modules[PKG+".console_services"] = protection
models = importlib.import_module(PKG+".agent_memory_models")
memory = importlib.import_module(PKG+".agent_memory_v2")
report = importlib.import_module(PKG+".agent_memory_report")


class Store:
    def __init__(self):
        self.objects = {}
        self.policy = None
        self.corrupt = False
    def bucket_exists(self, bucket):
        return True
    def make_bucket(self, bucket):
        pass
    def set_bucket_policy(self, bucket, policy):
        self.policy = policy
    def delete_bucket_policy(self, bucket):
        self.policy = ""
    def put_object(self, bucket, key, data, length, **kwargs):
        self.objects[key] = data.read()
    def get_object(self, bucket, key):
        data = io.BytesIO(b"corrupted" if self.corrupt else self.objects[key])
        data.release_conn = lambda: None
        return data
    def remove_object(self, bucket, key):
        self.objects.pop(key, None)
    def list_objects(self, bucket, prefix="", recursive=True):
        return [types.SimpleNamespace(object_name=key, size=len(value))
                for key, value in self.objects.items() if key.startswith(prefix)]


async def setup(tmp_path, session_class=AsyncSession):
    protection.protected.clear()
    engine = create_async_engine("sqlite+aiosqlite:///"+str(tmp_path/"memory.sqlite"))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=session_class, expire_on_commit=False)
    clock = [datetime(2026, 10, 9, 12)]
    replies, calls = [{"records": []}], []
    async def complete(role, messages, **kwargs):
        calls.append((role, messages, kwargs))
        response = replies[0]
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return await response()
        return response
    service = memory.MemoryService(factory, {"enabled": True, "groups": [100, 200],
        "embeddings_enabled": False}, complete, clock=lambda: clock[0], spool_dir=tmp_path/"inbox")
    return service, factory, clock, replies, calls, engine


async def ingest(service, factory, mid, user=11, group=100, text="喜欢五子棋", when=None, commit=True):
    row = types.SimpleNamespace(group_id=group, message_id=mid, user_id=user,
        content=text, is_bot=False, nickname=f"person{user}", created_at=when or service.clock())
    async with factory() as session:
        added = await service.ingest(session, row)
        if commit:
            await session.commit()
        return added


def fact(mid=1, user=11, content="喜欢五子棋", **kwargs):
    return {"kind": "fact", "user_id": user, "content": content,
        "topic": "偏爱的棋类", "source_ids": [mid], **kwargs}


def test_transactional_ingest_dedup_and_restart(tmp_path):
    async def scenario():
        service, factory, clock, replies, calls, engine = await setup(tmp_path)
        await ingest(service, factory, 1, commit=False)
        async with factory() as session:
            assert list((await session.execute(select(models.MemorySource))).scalars()) == []
            assert await session.get(models.MemoryJob, 100) is None
        assert await ingest(service, factory, 1)
        assert not await ingest(service, factory, 1)
        replies[0] = {"records": [fact()]}
        restarted = memory.MemoryService(factory, service.config, service.complete, clock=service.clock)
        assert await restarted.process_pending(force=True) == 1
        assert "喜欢五子棋" in await restarted.recall(100, [11], "棋")
        assert await restarted.process_pending(force=True) == 0
        await engine.dispose()
    asyncio.run(scenario())


def test_timeout_retry_preserves_checkpoint_and_source(tmp_path):
    async def scenario():
        service, factory, clock, replies, calls, engine = await setup(tmp_path)
        await ingest(service, factory, 1)
        replies[0] = TimeoutError("must not log this private provider string")
        assert await service.process_pending(force=True) == 0
        async with factory() as session:
            job = await session.get(models.MemoryJob, 100)
            assert job.checkpoint == 0 and job.last_error == "TimeoutError"
            assert (await session.execute(select(models.MemorySource))).scalar_one()
        assert await service.process_pending() == 0
        replies[0] = {"records": [fact()]}
        clock[0] += timedelta(hours=1)
        assert await service.process_pending() == 1
        assert len(await service.list_records(100)) == 1
        await engine.dispose()
    asyncio.run(scenario())


def test_database_write_failure_records_and_checkpoint_roll_back_together(tmp_path):
    class FailSession(AsyncSession):
        fail_once = True
        async def commit(self):
            if self.fail_once and any(isinstance(row, models.MemoryRecord) for row in self.new):
                type(self).fail_once = False
                raise RuntimeError("injected persistence failure")
            return await super().commit()
    async def scenario():
        service, factory, _, replies, _, engine = await setup(tmp_path, FailSession)
        await ingest(service, factory, 1)
        replies[0] = {"records": [fact()]}
        assert await service.process_pending(force=True) == 0
        async with factory() as session:
            assert (await session.get(models.MemoryJob, 100)).checkpoint == 0
            assert not list((await session.execute(select(models.MemoryRecord))).scalars())
        assert await service.process_pending(force=True) == 1
        assert len(await service.list_records(100)) == 1
        await engine.dispose()
    asyncio.run(scenario())


def test_source_owner_scope_targets_and_manual_protection(tmp_path):
    async def scenario():
        service, factory, _, replies, _, engine = await setup(tmp_path)
        await ingest(service, factory, 1)
        replies[0] = {"records": [fact(user=22), fact(999), fact(explicit_personal=True)]}
        await service.process_pending(force=True)
        rows = await service.list_records(100)
        assert len(rows) == 1
        assert await service.recall(200, [11], "棋") == ""
        assert await service.recall(100, [22], "棋") == ""
        assert "喜欢五子棋" in await service.recall(100, [22, 11], "棋")
        await service.update_record(rows[0]["id"], content="喜欢围棋", expected_version=1)
        await ingest(service, factory, 2, text="喜欢五子棋")
        replies[0] = {"records": [fact(2)]}
        await service.process_pending(force=True)
        assert "喜欢围棋" in await service.recall(100, [11], "棋")
        assert "喜欢五子棋" not in await service.recall(100, [11], "棋")
        with pytest.raises(ValueError):
            await service.update_record(rows[0]["id"], content="stale", expected_version=1)
        with pytest.raises(PermissionError):
            await service.update_record(rows[0]["id"], content="other user", actor_user_id=22)
        await engine.dispose()
    asyncio.run(scenario())


def test_new_preferences_supersede_old_and_explicit_fact_can_cross_group(tmp_path):
    async def scenario():
        service, factory, _, replies, _, engine = await setup(tmp_path)
        await ingest(service, factory, 1)
        replies[0] = {"records": [fact()]}
        await service.process_pending(force=True)
        await ingest(service, factory, 2, text="我现在喜欢围棋，我是一名学生")
        replies[0] = {"records": [fact(2, content="现在喜欢围棋"),
            fact(2, content="我是一名学生", explicit_personal=True, category="identity", topic="职业") ]}
        await service.process_pending(force=True)
        recall = await service.recall(100, [11], "棋")
        assert "现在喜欢围棋" in recall and "喜欢五子棋" not in recall
        assert "我是一名学生" in await service.recall(200, [11], "身份")
        await engine.dispose()
    asyncio.run(scenario())


def test_forget_during_extraction_wins_and_derivatives_are_deleted(tmp_path):
    async def scenario():
        service, factory, _, replies, _, engine = await setup(tmp_path)
        await ingest(service, factory, 1)
        async def forget_mid_call():
            await service.set_privacy(11, 100, forget=True)
            return {"records": [fact(), {"kind": "episode", "user_id": 0,
                "content": "某群友喜欢五子棋", "source_ids": [1]}]}
        replies[0] = forget_mid_call
        assert await asyncio.wait_for(service.process_pending(force=True), 5) == 1
        assert not await service.list_records(100)
        assert not await ingest(service, factory, 2)
        assert await service.recall(100, [11], "棋") == ""
        await engine.dispose()
    asyncio.run(scenario())


def test_record_delete_removes_sources_derived_rows_and_blocks_paraphrase(tmp_path):
    async def scenario():
        service, factory, _, replies, _, engine = await setup(tmp_path)
        await ingest(service, factory, 1)
        replies[0] = {"records": [fact(), {"kind": "episode", "user_id": 0,
            "content": "群友聊了喜欢的棋", "source_ids": [1]}]}
        await service.process_pending(force=True)
        rows = await service.list_records(100, "fact")
        await service.update_record(rows[0]["id"], delete_record=True, actor_user_id=11)
        assert not await ingest(service, factory, 1)
        assert await service.recall(100, [11], "棋") == ""
        assert all(row["status"] == "deleted" for row in await service.list_records(100))
        await engine.dispose()
    asyncio.run(scenario())


def test_style_requires_observations_across_three_days_and_respects_optout(tmp_path):
    async def scenario():
        service, factory, clock, replies, _, engine = await setup(tmp_path)
        for mid in range(1, 20):
            await ingest(service, factory, mid, when=clock[0]-timedelta(days=mid % 3))
        replies[0] = {"records": [{"kind": "style", "user_id": 11,
            "content": "偏好简短棋类讨论", "source_ids": [1, 2, 3]}]}
        await service.process_pending(force=True)
        assert not await service.list_records(100, "style")
        for mid in (20, 21, 22):
            await ingest(service, factory, mid)
        replies[0]["records"][0]["source_ids"] = [20]
        await service.process_pending(force=True)
        assert not await service.list_records(100, "style")
        await ingest(service, factory, 23)
        replies[0]["records"][0]["source_ids"] = [1, 2, 23]
        await service.process_pending(force=True)
        assert len(await service.list_records(100, "style")) == 1
        await service.set_privacy(11, 100, style_learning=False)
        assert await service.recall(100, [11], "棋") == ""
        await engine.dispose()
    asyncio.run(scenario())


def test_jokes_scoped_relevant_once_daily_and_interval_budget(tmp_path):
    async def scenario():
        service, factory, clock, replies, _, engine = await setup(tmp_path)
        for mid in range(1, 6):
            await ingest(service, factory, mid, text="五子棋梗"+str(mid))
        replies[0] = {"records": [{"kind": "joke", "user_id": 0,
            "content": "五子棋梗"+str(mid), "source_ids": [mid]} for mid in range(1, 6)]}
        await service.process_pending(force=True)
        first = await service.recall(100, [11], "五子棋梗")
        assert first.count("[joke") == 1
        assert await service.recall(100, [11], "五子棋梗") == ""
        assert await service.recall(200, [11], "五子棋梗") == ""
        assert await service.recall(100, [11], "救命 五子棋梗") == ""
        for _ in range(3):
            clock[0] += timedelta(minutes=16)
            assert (await service.recall(100, [11], "五子棋梗")).count("[joke") == 1
        clock[0] += timedelta(minutes=16)
        assert await service.recall(100, [11], "五子棋梗") == ""
        await engine.dispose()
    asyncio.run(scenario())


def test_embeddings_independent_scope_and_quota_falls_back_lexically(tmp_path):
    async def scenario():
        service, factory, _, replies, _, engine = await setup(tmp_path)
        service.config["embeddings_enabled"] = True
        embedded = []
        async def embed(text):
            embedded.append(text)
            return [1.0]+[0.0]*767
        service.embedder = embed
        await ingest(service, factory, 1)
        replies[0] = {"records": [fact(), fact(content="喜欢围棋", topic="another")]}
        await service.process_pending(force=True)
        assert await service.index_pending() == 2
        assert len(embedded) == 2 and all("\n" not in text for text in embedded)
        assert await service.index_pending() == 0
        async with factory() as session:
            session.add(models.MemoryBudget(key="embedding:bootstrap", used=1_000_000))
            today = "embedding:"+service.clock().date().isoformat()
            row = await session.get(models.MemoryBudget, today)
            row.used = 250_000
            await session.commit()
        assert await service._embed("new query") is None
        assert await service._embed("new record", bootstrap=True) is None
        assert "围棋" in await service.recall(100, [11], "围棋")
        assert await service.recall(200, [11], "围棋") == ""
        await engine.dispose()
    asyncio.run(scenario())


def test_archive_verifies_before_pruning_private_retention_and_purge(tmp_path):
    async def scenario():
        service, factory, clock, replies, _, engine = await setup(tmp_path)
        store = Store()
        service.object_store = store
        await ingest(service, factory, 1, when=clock[0]-timedelta(days=40))
        replies[0] = {"records": [fact()]}
        await service.process_pending(force=True)
        store.corrupt = True
        with pytest.raises(ValueError):
            await service.maintain_archive()
        async with factory() as session:
            assert (await session.execute(select(models.MemorySource))).scalar_one()
        store.corrupt = False
        assert await service.maintain_archive() == 1
        assert store.policy == ""
        async with factory() as session:
            assert not list((await session.execute(select(models.MemorySource))).scalars())
            archive = (await session.execute(select(models.MemoryArchive))).scalar_one()
            assert archive.expires_at == clock[0]+timedelta(days=50)
        await service.set_privacy(11, 100, forget=True)
        await service.maintain_archive()
        assert not store.objects
        assert (await service.jobs_stats())["archive_bytes"] == 0
        await engine.dispose()
    asyncio.run(scenario())


def test_report_insufficient_evidence_cache_privacy_and_html_escape(tmp_path):
    async def scenario():
        service, factory, clock, replies, calls, engine = await setup(tmp_path)
        with pytest.raises(ValueError, match="观察还不够"):
            await report.create_report(100, 11, service=service)
        for mid in range(1, 11):
            await ingest(service, factory, mid, when=clock[0]-timedelta(days=mid % 2))
        replies[0] = {"traits": [{"title": "<script>", "text": "喜欢棋类讨论", "source_ids": [mid]}
            for mid in range(1, 5)], "summary": "积极参与棋类话题"}
        result = await report.create_report(100, 11, "<img onerror=alert(1)>", service=service)
        assert len(result["evidence"]) == 4
        assert len(calls) == 1
        assert await report.create_report(100, 11, service=service) == result
        assert len(calls) == 1
        card = report.render_html(result)
        assert "<script>" not in card and "&lt;script&gt;" in card
        await service.set_privacy(11, 100, evaluation=False)
        with pytest.raises(ValueError, match="关闭"):
            await report.create_report(100, 11, service=service)
        await service.set_privacy(11, 100, evaluation=True)
        replies[0]["traits"][0]["source_ids"] = [999]
        with pytest.raises(ValueError, match="依据"):
            await report.create_report(100, 11, service=service)
        await engine.dispose()
    asyncio.run(scenario())


def test_privacy_versions_audit_rollback_and_global_source_optout(tmp_path):
    async def scenario():
        service, factory, _, replies, _, engine = await setup(tmp_path)
        before = await service.privacy(11, 100)
        assert before["version"] == 0
        after = await service.set_privacy(11, 100, style_learning=False,
            expected_version=0, actor="admin")
        assert after["version"] == 1 and after["learning"]
        with pytest.raises(ValueError):
            await service.set_privacy(11, 100, learning=False, expected_version=0, actor="admin")
        assert (await service.privacy(11, 100))["learning"]
        async with factory() as session:
            assert len(list((await session.execute(select(FakeAudit))).scalars())) == 1
        await ingest(service, factory, 1, text="我是一名学生")
        replies[0] = {"records": [fact(content="是一名学生", explicit_personal=True, category="identity")]}
        await service.process_pending(force=True)
        assert "学生" in await service.recall(200, [11], "学生")
        await service.set_privacy(11, 100, learning=False)
        assert await service.recall(200, [11], "学生") == ""
        service.config["embeddings_enabled"] = True
        async def forbidden_embed(text):
            raise AssertionError("opted-out content must not leave the host")
        service.embedder = forbidden_embed
        assert await service.index_pending() == 0
        await engine.dispose()
    asyncio.run(scenario())


def test_archive_full_keeps_source_and_expiry_removes_objects(tmp_path):
    async def scenario():
        service, factory, clock, _, _, engine = await setup(tmp_path)
        store = Store()
        service.object_store = store
        await ingest(service, factory, 1, when=clock[0]-timedelta(days=40))
        await service.process_pending(force=True)
        async with factory() as session:
            session.add(models.MemoryArchive(group_id=200, user_id=22, object_key="existing",
                checksum="x"*64, size_bytes=5*1024**3,
                expires_at=clock[0]+timedelta(days=1)))
            await session.commit()
        assert await service.maintain_archive() == 0
        assert (await service.jobs_stats())["archive_warning"]
        async with factory() as session:
            assert (await session.execute(select(models.MemorySource))).scalar_one()
        clock[0] += timedelta(days=2)
        assert await service.maintain_archive() == 1
        clock[0] += timedelta(days=60)
        await service.maintain_archive()
        assert not store.objects
        await engine.dispose()
    asyncio.run(scenario())


def test_validated_source_tombstone_blocks_inflight_paraphrase(tmp_path):
    async def scenario():
        service, factory, _, replies, _, engine = await setup(tmp_path)
        await ingest(service, factory, 1)
        replies[0] = {"records": [fact()]}
        await service.process_pending(force=True)
        async with factory() as session:
            sources = list((await session.execute(select(models.MemorySource))).scalars())
        row = (await service.list_records(100))[0]
        await service.update_record(row["id"], delete_record=True)
        async with factory() as session:
            await service._persist_records(session, 100, sources, [fact(content="偏爱下五子棋")])
            await session.commit()
        assert await service.recall(100, [11], "棋") == ""
        assert len(await service.list_records(100)) == 1
        await engine.dispose()
    asyncio.run(scenario())


def test_hot_context_privacy_applies_on_feature_rollback_and_reopen(tmp_path):
    async def scenario():
        service, factory, clock, _, _, engine = await setup(tmp_path)
        old = types.SimpleNamespace(user_id=11, message_id=1, created_at=clock[0]-timedelta(hours=1))
        other = types.SimpleNamespace(user_id=22, message_id=2, created_at=clock[0])
        await service.set_privacy(11, 100, forget=True)
        service.config["enabled"] = False
        assert await service.filter_recent_sources((old, other), 100) == (other,)
        await service.set_privacy(11, 100, learning=True)
        new = types.SimpleNamespace(user_id=11, message_id=3, created_at=clock[0]+timedelta(seconds=1))
        assert await service.filter_recent_sources((old, other, new), 100) == (other, new)
        await engine.dispose()
    asyncio.run(scenario())


def test_embedding_route_disabled_and_incompatible_models_do_not_mix(tmp_path):
    async def scenario():
        service, factory, _, replies, _, engine = await setup(tmp_path)
        service.config["embeddings_enabled"] = True
        route = types.SimpleNamespace(model="new-embedding", api_key="synthetic", enabled=False,
            protocol="embedding", provider="google", version=2)
        module = types.ModuleType(PKG+".model_routes")
        module.resolve = lambda role: route
        sys.modules[PKG+".model_routes"] = module
        await ingest(service, factory, 1)
        replies[0] = {"records": [fact()]}
        await service.process_pending(force=True)
        assert await service.index_pending() == 0
        assert await service._embed("test") is None
        route.enabled = True
        route.protocol = "openai"
        assert await service._embed("test") is None
        route.protocol = "embedding"
        route.provider = "unrelated"
        assert await service._embed("test") is None
        await engine.dispose()
    asyncio.run(scenario())


def test_archived_empty_table_new_messages_do_not_reuse_checkpoint_ids(tmp_path):
    async def scenario():
        service, factory, clock, _, _, engine = await setup(tmp_path)
        service.object_store = Store()
        await ingest(service, factory, 1, when=clock[0]-timedelta(days=40))
        await service.process_pending(force=True)
        await service.maintain_archive()
        await ingest(service, factory, 2)
        assert await service.process_pending(force=True) == 1
        async with factory() as session:
            job = await session.get(models.MemoryJob, 100)
            assert job.checkpoint == 2
        await engine.dispose()
    asyncio.run(scenario())


def spool_event(service, mid=1):
    return types.SimpleNamespace(group_id=100, user_id=11, message_id=mid,
        sender={"nickname": "synthetic"}, time=service.clock().timestamp(),
        get_plaintext=lambda: "source held during database outage")


def test_db_outage_spools_durably_restart_replay_is_deduplicated(tmp_path):
    async def scenario():
        service, factory, _, replies, calls, engine = await setup(tmp_path)
        async def offline():
            raise ConnectionError("synthetic outage")
        service.session_factory = offline
        bot = types.SimpleNamespace(self_id="999")
        assert await service.ingest_event(bot, spool_event(service))
        assert await service.ingest_event(bot, spool_event(service))
        assert service.spool_stats()["pending"] == 1
        assert not calls
        stats = await service.jobs_stats()
        assert stats["database_error"] == "ConnectionError" and stats["spool"]["pending"] == 1
        restarted = memory.MemoryService(factory, service.config, service.complete,
            clock=service.clock, spool_dir=tmp_path/"inbox")
        assert await restarted.replay_spool() == 1
        assert restarted.spool_stats()["pending"] == 0
        assert await restarted.replay_spool() == 0
        # A crash after DB commit but before unlink must also be harmless.
        service.session_factory = offline
        await service.ingest_event(bot, spool_event(service))
        assert await restarted.replay_spool() == 1
        async with factory() as session:
            assert len(list((await session.execute(select(models.MemorySource))).scalars())) == 1
        await engine.dispose()
    asyncio.run(scenario())


def test_spool_privacy_cutoff_schema_path_and_retention(tmp_path):
    async def scenario():
        service, factory, clock, _, calls, engine = await setup(tmp_path)
        async def offline():
            raise ConnectionError("synthetic")
        service.session_factory = offline
        bot = types.SimpleNamespace(self_id="999")
        await service.ingest_event(bot, spool_event(service))
        service.session_factory = factory
        await service.set_privacy(11, 100, forget=True)
        await service.set_privacy(11, 100, learning=True)
        assert await service.replay_spool() == 1
        async with factory() as session:
            assert not list((await session.execute(select(models.MemorySource))).scalars())
        root = service._spool_root()
        malformed = root/("0"*64+".json")
        malformed.write_text('{"version":1,"path":"../../secret"}', encoding="utf-8")
        traversal = root/"invalid.json"
        traversal.write_text('{}', encoding="utf-8")
        assert await service.replay_spool() == 0
        assert malformed.exists() and traversal.exists()
        assert service.spool_stats()["last_error"].startswith("invalid_inbox")
        assert not calls
        service.session_factory = offline
        clock[0] += timedelta(seconds=1)
        await service.ingest_event(bot, spool_event(service, mid=2))
        clock[0] += timedelta(days=91)
        service.session_factory = factory
        assert await service.replay_spool() == 0
        assert len(list(root.iterdir())) == 2  # Invalid files retained; expired valid raw input removed.
        await engine.dispose()
    asyncio.run(scenario())


def test_spool_cap_does_not_overwrite_or_delete_existing_inputs(tmp_path):
    async def scenario():
        service, factory, _, _, _, engine = await setup(tmp_path)
        root = service._spool_root(create=True)
        # Sparse file exercises capacity without allocating 128 MiB of test RAM.
        occupied = root/"operator-preserved.bin"
        with occupied.open("wb") as handle:
            handle.truncate(128*1024**2)
        async def offline():
            raise ConnectionError("synthetic")
        service.session_factory = offline
        with pytest.raises(RuntimeError, match="inbox unavailable"):
            await service.ingest_event(types.SimpleNamespace(self_id="999"), spool_event(service))
        assert occupied.stat().st_size == 128*1024**2
        assert len(list(root.iterdir())) == 1
        assert service.spool_stats()["warning"]
        await engine.dispose()
    asyncio.run(scenario())


def test_explicit_interaction_preference_needs_one_message_and_stays_in_source_group(tmp_path):
    async def scenario():
        service, factory, clock, replies, calls, engine = await setup(tmp_path)
        await ingest(service, factory, 1, text="我不喜欢凛一直损我，温柔点。")
        replies[0] = {"records": [fact(content="仅对凛：希望交流温柔些，不喜欢被挖苦。",
            topic="interaction_tone", category="preference", explicit_personal=True)]}
        # Low activity still reaches the normal timed worker, without invoking
        # style eligibility or forcing a 30-message batch.
        assert await service.process_pending() == 0
        clock[0] += timedelta(minutes=6)
        assert await service.process_pending() == 1
        prompt = calls[0][1][0]["content"]
        assert "interaction_tone" in prompt and "interaction_care" in prompt
        rows = await service.list_records(100, "fact", user_id=11)
        assert len(rows) == 1 and "仅对凛" in rows[0]["content"]
        assert not await service.list_records(0)
        async with factory() as session:
            assert (await session.execute(select(models.MemoryRecord))).scalar_one().topic == "interaction_tone"
        recall = await service.recall(100, [11], "明天的午餐怎么安排？")
        assert "[用户偏好" in recall and "仅对凛" in recall
        assert await service.recall(200, [11], "明天的午餐怎么安排？") == ""
        assert await service.recall(100, [22], "明天的午餐怎么安排？") == ""
        assert "温柔" in await service.recall(100, [22, 11], "明天的午餐怎么安排？")
        await engine.dispose()
    asyncio.run(scenario())


def test_interaction_preferences_prioritized_in_fixed_small_budget_preserve_other_facts(tmp_path):
    async def scenario():
        service, factory, _, replies, _, engine = await setup(tmp_path)
        await ingest(service, factory, 1, text="我喜欢下围棋，希望温柔点，也别总催我睡觉。")
        replies[0] = {"records": [
            fact(content="喜欢围棋。", topic="游戏偏好"),
            fact(content="不喜欢被挖苦，希望温柔些。", topic="interaction_tone", category="preference"),
            fact(content="不希望反复被催睡。", topic="interaction_care", category="preference"),
        ]}
        await service.process_pending(force=True)
        async with factory() as session:
            tone = (await session.execute(select(models.MemoryRecord).where(
                models.MemoryRecord.topic == "interaction_tone"))).scalar_one()
            ordinary = (await session.execute(select(models.MemoryRecord).where(
                models.MemoryRecord.topic == "游戏偏好"))).scalar_one()
            budget = len(memory._recall_line(tone, 100))+len(memory._recall_line(ordinary, 100))+2
        recalled = await service.recall(100, [11], "围棋围棋", limit_chars=budget)
        lines = recalled.splitlines()
        assert len(recalled)+1 <= budget
        assert lines[0].startswith("[用户偏好") and "温柔" in lines[0]
        assert "喜欢围棋" in recalled
        # Preference content must remain data; labels do not introduce a new
        # system role or copy arbitrary source instructions into the prompt.
        assert "来源" in recalled
        await engine.dispose()
    asyncio.run(scenario())


def test_interaction_preference_update_protection_delete_and_forged_author(tmp_path):
    async def scenario():
        service, factory, _, replies, _, engine = await setup(tmp_path)
        await ingest(service, factory, 1, text="别老催我睡觉。")
        replies[0] = {"records": [
            fact(content="不希望反复被催睡。", topic="interaction_care", category="preference"),
            fact(user=22, content="不喜欢被损。", topic="interaction_tone", category="preference"),
        ]}
        await service.process_pending(force=True)
        assert len(await service.list_records(100)) == 1
        await ingest(service, factory, 2, text="我现在接受偶尔提醒作息，但不要反复催。")
        replies[0] = {"records": [fact(2, content="接受偶尔提醒作息，不希望反复催睡。",
            topic="interaction_care", category="preference", explicit_personal=True)]}
        await service.process_pending(force=True)
        recall = await service.recall(100, [11], "游戏")
        assert "接受偶尔" in recall and "不希望反复被催睡" not in recall
        active = next(row for row in await service.list_records(100) if row["status"] == "active")
        await service.update_record(active["id"], content="只希望艾蕾偶尔提醒作息。", actor_user_id=11)
        await ingest(service, factory, 3, text="我不希望反复被催睡。")
        replies[0] = {"records": [fact(3, content="不要催睡。", topic="interaction_care", category="preference")]}
        await service.process_pending(force=True)
        assert "只希望艾蕾" in await service.recall(100, [11], "午餐")
        assert "不要催睡" not in await service.recall(100, [11], "午餐")
        await service.update_record(active["id"], delete_record=True, actor_user_id=11)
        async with factory() as session:
            # An in-flight extractor holding the previous source cannot restore
            # a deleted preference under a paraphrased content fingerprint.
            source = types.SimpleNamespace(message_id=2, user_id=11,
                content="偶尔提醒作息", created_at=service.clock())
            await service._persist_records(session, 100, [source], [fact(2,
                content="偶尔提醒作息就好。", topic="interaction_care", category="preference")])
            await session.commit()
        assert await service.recall(100, [11], "午餐") == ""
        assert await service.recall(200, [11], "午餐") == ""
        await engine.dispose()
    asyncio.run(scenario())


def test_interaction_preference_respects_learning_optout_and_legacy_global_scope(tmp_path):
    async def scenario():
        service, factory, _, replies, _, engine = await setup(tmp_path)
        await ingest(service, factory, 1, text="我不喜欢被呛。")
        replies[0] = {"records": [fact(content="不喜欢被呛。", topic="interaction_tone",
            category="preference", explicit_personal=True)]}
        await service.set_privacy(11, 100, learning=False)
        await service.process_pending(force=True)
        assert not await service.list_records(100)
        await service.set_privacy(11, 100, learning=True)
        async with factory() as session:
            session.add(models.MemoryRecord(group_id=0, user_id=11, kind="fact",
                content="旧模型错误提升的语气偏好。", topic="interaction_tone",
                fingerprint=memory.fingerprint("旧模型错误提升的语气偏好。"), evidence="[]"))
            await session.commit()
        assert await service.recall(200, [11], "语气偏好") == ""
        assert await service.recall(100, [11], "语气偏好") == ""
        await engine.dispose()
    asyncio.run(scenario())


def test_interaction_preference_upsert_separates_personas_and_same_persona_updates(tmp_path):
    async def scenario():
        service, factory, _, replies, _, engine = await setup(tmp_path)
        await ingest(service, factory, 1, text="凛别损我，温柔一点。")
        replies[0] = {"records": [fact(content="凛应当温柔交流，不要挖苦本人。",
            topic="interaction_tone", category="preference", explicit_personal=True)]}
        await service.process_pending(force=True)
        await ingest(service, factory, 2, text="伊什塔尔可以偶尔吐槽我。")
        replies[0] = {"records": [fact(2, content="伊什塔尔可以适度吐槽本人。",
            topic="interaction_tone", category="preference", explicit_personal=True)]}
        await service.process_pending(force=True)
        rows = await service.list_records(100, "fact", user_id=11)
        assert len(rows) == 2 and {row["status"] for row in rows} == {"active"}
        assert "凛应当" in await service.recall(100, [11], "午餐")
        assert "伊什塔尔可以" in await service.recall(100, [11], "午餐")
        await ingest(service, factory, 3, text="凛还是不要挖苦我，温柔一点。")
        replies[0] = {"records": [fact(3, content="凛应保持温柔，不应挖苦本人。",
            topic="interaction_tone", category="preference", explicit_personal=True)]}
        await service.process_pending(force=True)
        rows = await service.list_records(100, "fact", user_id=11)
        assert len([row for row in rows if row["status"] == "active"]) == 2
        assert "凛应保持温柔" in await service.recall(100, [11], "午餐")
        assert "凛应当温柔交流" not in await service.recall(100, [11], "午餐")
        assert "伊什塔尔可以" in await service.recall(100, [11], "午餐")
        await engine.dispose()
    asyncio.run(scenario())


def test_interaction_persona_scope_unknown_is_conservative_and_protection_is_local(tmp_path):
    async def scenario():
        service, factory, _, replies, _, engine = await setup(tmp_path)
        await ingest(service, factory, 1, text="我不喜欢被呛。")
        replies[0] = {"records": [fact(content="不要呛我。",
            topic="interaction_tone", category="preference", explicit_personal=True)]}
        await service.process_pending(force=True)
        first = (await service.list_records(100, "fact", user_id=11))[0]
        await service.update_record(first["id"], content="保留这条语气边界。", actor_user_id=11)
        await ingest(service, factory, 2, text="凛和艾蕾都不要挖苦我。")
        replies[0] = {"records": [fact(2, content="凛和艾蕾都不要挖苦本人。",
            topic="interaction_tone", category="preference", explicit_personal=True)]}
        await service.process_pending(force=True)
        # A broad multi-persona update cannot overwrite a protected unnamed
        # boundary; conservative handling leaves only the protected record.
        rows = await service.list_records(100, "fact", user_id=11)
        assert any(row["content"] == "保留这条语气边界。" and row["status"] == "active" for row in rows)
        assert len([row for row in rows if row["status"] == "active"]) == 1
        await service.update_record(first["id"], delete_record=True, actor_user_id=11)
        await ingest(service, factory, 3, text="凛和艾蕾都不要挖苦我。")
        replies[0] = {"records": [fact(3, content="凛和艾蕾都不要挖苦本人。",
            topic="interaction_tone", category="preference", explicit_personal=True)]}
        await service.process_pending(force=True)
        assert "保留这条语气边界" not in await service.recall(100, [11], "闲聊")
        assert "凛和艾蕾都不要挖苦" in await service.recall(100, [11], "闲聊")
        assert await service.recall(200, [11], "闲聊") == ""
        await engine.dispose()
    asyncio.run(scenario())


def test_legacy_preference_scope_uses_evidence_when_content_omits_persona(tmp_path):
    async def scenario():
        service, factory, _, replies, _, engine = await setup(tmp_path)
        # Old rows have no dedicated persona column. Source evidence supplies
        # the missing scope and makes the applicable persona visible on recall.
        await ingest(service, factory, 1, text="凛别呛我。")
        replies[0] = {"records": [fact(content="不喜欢被呛。",
            topic="interaction_tone", category="preference")]}
        await service.process_pending(force=True)
        recall = await service.recall(100, [11], "午餐")
        assert "适用人格:凛" in recall
        await ingest(service, factory, 2, text="伊什塔尔可以偶尔吐槽我。")
        replies[0] = {"records": [fact(2, content="伊什塔尔可以适度吐槽本人。",
            topic="interaction_tone", category="preference")]}
        await service.process_pending(force=True)
        await ingest(service, factory, 3, text="凛不要嘲讽我，还是温柔一点。")
        replies[0] = {"records": [fact(3, content="凛应温柔交流，不应嘲讽本人。",
            topic="interaction_tone", category="preference")]}
        await service.process_pending(force=True)
        rows = await service.list_records(100)
        assert len([row for row in rows if row["status"] == "active"]) == 2
        assert "不喜欢被呛" not in await service.recall(100, [11], "午餐")
        assert "伊什塔尔可以" in await service.recall(100, [11], "午餐")
        await engine.dispose()
    asyncio.run(scenario())


def test_protected_persona_preference_does_not_block_disjoint_persona_or_other_group(tmp_path):
    async def scenario():
        service, factory, _, replies, _, engine = await setup(tmp_path)
        await ingest(service, factory, 1, text="凛不要损我。")
        replies[0] = {"records": [fact(content="凛不要挖苦本人。",
            topic="interaction_tone", category="preference")]}
        await service.process_pending(force=True)
        first = (await service.list_records(100))[0]
        await service.update_record(first["id"], content="凛应保持温柔。", actor_user_id=11)
        await ingest(service, factory, 2, text="伊什塔尔可以吐槽我。")
        replies[0] = {"records": [fact(2, content="伊什塔尔可以适度吐槽。",
            topic="interaction_tone", category="preference")]}
        await service.process_pending(force=True)
        await ingest(service, factory, 3, text="凛可以损我了。")
        replies[0] = {"records": [fact(3, content="凛可以挖苦本人。",
            topic="interaction_tone", category="preference")]}
        await service.process_pending(force=True)
        recalled = await service.recall(100, [11], "闲聊")
        assert "凛应保持温柔" in recalled and "凛可以挖苦" not in recalled
        assert "伊什塔尔可以" in recalled
        await ingest(service, factory, 4, group=200, text="这个群凛可以吐槽我。")
        replies[0] = {"records": [fact(4, content="凛在这个群可以适度吐槽。",
            topic="interaction_tone", category="preference", explicit_personal=True)]}
        await service.process_pending(force=True)
        assert "凛应保持温柔" in await service.recall(100, [11], "闲聊")
        assert "这个群可以" not in await service.recall(100, [11], "闲聊")
        assert "凛在这个群可以" in await service.recall(200, [11], "闲聊")
        assert "凛应保持温柔" not in await service.recall(200, [11], "闲聊")
        await engine.dispose()
    asyncio.run(scenario())


def test_identical_preference_summary_keeps_distinct_persona_source_evidence(tmp_path):
    async def scenario():
        service, factory, _, replies, _, engine = await setup(tmp_path)
        for mid, persona in ((1, "凛"), (2, "艾蕾")):
            await ingest(service, factory, mid, text=f"{persona}不要损我。")
            replies[0] = {"records": [fact(mid, content="不喜欢被挖苦。",
                topic="interaction_tone", category="preference")]}
            await service.process_pending(force=True)
        rows = await service.list_records(100)
        assert len(rows) == 1
        assert {item["message_id"] for item in rows[0]["evidence"]} == {1, 2}
        recalled = await service.recall(100, [11], "闲聊")
        assert "适用人格" in recalled and "凛" in recalled and "艾蕾" in recalled
        await engine.dispose()
    asyncio.run(scenario())
