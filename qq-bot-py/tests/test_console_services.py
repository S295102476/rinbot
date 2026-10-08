import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from plugins import db
from plugins.console_services import ConsoleMemoryProtection, ConsoleServices, is_memory_protected
from plugins.console_state import ConsoleAudit


class FakePersonaManager:
    def __init__(self, root):
        self._PROFILE_DIR = root / "profiles"
        self._LEGACY_DIR = root / "legacy"
        self._SHARED_DIR = root / "shared"
        self.reload_count = 0

    def list_personas(self):
        return [SimpleNamespace(persona_id="rin", name="Rin"), SimpleNamespace(persona_id="other", name="Other")]

    def get_active_persona_id(self):
        return "rin"

    def reload_profiles(self):
        self.reload_count += 1


class FakeRedis:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def pipeline(self, transaction=True):
        return self

    def __getattr__(self, name):
        def call(*args):
            self.calls.append((name, args))
            if name == "execute" and self.fail:
                raise RuntimeError("private redis failure")
        return call


class FakeMinio:
    def __init__(self, fail=False):
        self.fail = fail
        self.removed = []

    def remove_object(self, bucket, name):
        if self.fail:
            raise RuntimeError("private storage failure")
        self.removed.append((bucket, name))

    def presigned_get_object(self, bucket, name, **kwargs):
        return f"https://example.invalid/{bucket}/{name}"


class AsyncSessionAdapter:
    def __init__(self, engine):
        self.session = Session(engine, expire_on_commit=False)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.session.close()

    def add(self, item):
        self.session.add(item)

    def add_all(self, items):
        self.session.add_all(items)

    async def get(self, *args, **kwargs):
        return self.session.get(*args, **kwargs)

    async def execute(self, *args, **kwargs):
        return self.session.execute(*args, **kwargs)

    async def flush(self):
        self.session.flush()

    async def commit(self):
        self.session.commit()

    async def rollback(self):
        self.session.rollback()

    async def delete(self, item):
        self.session.delete(item)

    async def close(self):
        self.session.close()


@asynccontextmanager
async def service_context(tmp_path, **kwargs):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    factory = lambda: AsyncSessionAdapter(engine)
    db.Base.metadata.create_all(engine)

    async def session_factory():
        return factory()

    service = ConsoleServices(session_factory=session_factory, config={"meme": {"minio": {"bucket": "approved"}}},
                              persona_manager=FakePersonaManager(tmp_path), backup_root=tmp_path / "backup",
                              minio_client=kwargs.get("minio", FakeMinio()), redis_client=kwargs.get("redis", FakeRedis()))
    try:
        yield service, factory
    finally:
        engine.dispose()


def test_meme_bucket_scope_remote_failure_and_cache_failure(tmp_path):
    async def run():
        remote = FakeMinio(fail=True)
        async with service_context(tmp_path, minio=remote) as (service, factory):
            async with factory() as session:
                first = db.MemeItem(bucket="approved", object_name="one.png")
                foreign = db.MemeItem(bucket="other", object_name="two.png")
                session.add_all([first, foreign])
                await session.commit()
                first_id, foreign_id = first.id, foreign.id
            with pytest.raises(HTTPException) as error:
                await service.update_meme(foreign_id, {"status": "disabled"}, "admin")
            assert error.value.status_code == 404
            with pytest.raises(HTTPException) as error:
                await service.update_meme(first_id, {"status": "deleted"}, "admin", hard=True)
            assert error.value.status_code == 502
            async with factory() as session:
                assert (await session.get(db.MemeItem, first_id)).status == "active"
            remote.fail = False
            service.redis.fail = True
            with pytest.raises(HTTPException) as error:
                await service.update_meme(first_id, {"status": "deleted"}, "admin", hard=True)
            assert error.value.detail["saved"] is True
            assert error.value.detail["cache_synced"] is False
            assert remote.removed == [("approved", "one.png")]
    asyncio.run(run())


def test_batch_deletion_reports_partial_failure(tmp_path):
    async def run():
        async with service_context(tmp_path) as (service, factory):
            async with factory() as session:
                item = db.MemeItem(bucket="approved", object_name="one.png")
                session.add(item)
                await session.commit()
                item_id = item.id
            result = await service.batch_memes({"ids": [item_id, 999], "hard": True}, "admin")
            assert not result["ok"]
            assert result["deleted_ids"] == [item_id]
            assert result["failed"][0]["id"] == 999
    asyncio.run(run())


def test_memory_edits_and_deletes_create_durable_semantic_protection(tmp_path):
    async def run():
        async with service_context(tmp_path) as (service, factory):
            result = await service.mutate_memory("facts", "global", {"user_id": 7, "fact": "Old fact", "reason": "manual"}, "admin")
            row_id = result["item"]["id"]
            old_fingerprint = result["item"]["fingerprint"]
            updated = await service.mutate_memory("facts", "global", {"fact": "New fact", "reason": "correction"}, "admin", item_id=row_id)
            new_fingerprint = updated["item"]["fingerprint"]
            await service.mutate_memory("facts", "global", {"reason": "remove"}, "admin", item_id=row_id, deleting=True)
            async with factory() as session:
                assert await session.get(db.AgentGlobalPersonFact, row_id) is None
                assert await is_memory_protected(session, "facts", "global", user_id=7, identity=old_fingerprint)
                assert await is_memory_protected(session, "facts", "global", user_id=7, identity=new_fingerprint)
                audits = (await session.execute(select(ConsoleAudit))).scalars().all()
                assert len(audits) == 3
    asyncio.run(run())


def test_affinity_changes_only_selected_persona_and_syncs_rin_legacy(tmp_path):
    async def run():
        async with service_context(tmp_path) as (service, factory):
            async with factory() as session:
                session.add_all([
                    db.AgentPersonaAffinity(persona_id="rin", user_id=7, affinity_score=10),
                    db.AgentPersonaAffinity(persona_id="other", user_id=7, affinity_score=20),
                    db.AgentPersonaRelationshipState(persona_id="rin", group_id=1, user_id=7, affinity_score=10),
                    db.AgentPersonaRelationshipState(persona_id="other", group_id=1, user_id=7, affinity_score=20),
                    db.UserAffinity(user_id=7, affinity_score=10),
                    db.AgentRelationshipState(group_id=1, user_id=7, affinity_score=10),
                ])
                await session.commit()
            await service.set_affinity("other", 7, {"score": 42, "reason": "verified correction"}, "admin")
            async with factory() as session:
                assert (await session.execute(select(db.UserAffinity))).scalar_one().affinity_score == 10
                scores = {row.persona_id: row.affinity_score for row in (await session.execute(select(db.AgentPersonaAffinity))).scalars()}
                assert scores == {"rin": 10, "other": 42}
            await service.set_affinity("rin", 7, {"score": 55, "reason": "verified correction"}, "admin")
            async with factory() as session:
                assert (await session.execute(select(db.UserAffinity))).scalar_one().affinity_score == 55
                assert (await session.execute(select(db.AgentRelationshipState))).scalar_one().affinity_score == 55
                scores = {row.persona_id: row.affinity_score for row in (await session.execute(select(db.AgentPersonaRelationshipState))).scalars()}
                assert scores == {"rin": 55, "other": 42}
            with pytest.raises(HTTPException):
                await service.set_affinity("rin", 7, {"score": 50, "reason": ""}, "admin")
    asyncio.run(run())


def test_persona_document_ids_backups_conflicts_restore_and_no_arbitrary_path(tmp_path):
    async def run():
        root = tmp_path / "profiles" / "rin"
        root.mkdir(parents=True)
        target = root / "character.md"
        target.write_text("Original", encoding="utf-8")
        (root / "secret.txt").write_text("not editable", encoding="utf-8")
        async with service_context(tmp_path) as (service, factory):
            documents = (await service.personas())["items"][0]["documents"]
            assert [item["name"] for item in documents] == ["character.md"]
            doc_id = documents[0]["id"]
            original = await service.document(doc_id)
            saved = await service.save_document(doc_id, {"content": "Revised", "version": original["version"]}, "admin")
            assert target.read_text(encoding="utf-8") == "Revised"
            assert next((tmp_path / "backup").glob("*.md")).read_text(encoding="utf-8") == "Original"
            with pytest.raises(HTTPException) as error:
                await service.save_document(doc_id, {"content": "Stale", "version": original["version"]}, "admin")
            assert error.value.status_code == 409
            with pytest.raises(HTTPException) as error:
                await service.document("../secret.txt")
            assert error.value.status_code == 404
            revisions = await service.revisions(doc_id)
            old = next(item for item in revisions["items"] if item["version"] == original["version"])
            await service.restore_document(doc_id, old["id"], saved["version"], "admin")
            assert target.read_text(encoding="utf-8") == "Original"
    asyncio.run(run())


def test_persona_save_restores_file_when_database_commit_fails(tmp_path):
    async def run():
        root = tmp_path / "profiles" / "rin"
        root.mkdir(parents=True)
        target = root / "character.md"
        target.write_text("Original", encoding="utf-8")
        async with service_context(tmp_path) as (service, factory):
            doc_id = (await service.personas())["items"][0]["documents"][0]["id"]
            original = await service.document(doc_id)

            async def failed_session():
                session = factory()
                async def failed_commit():
                    raise RuntimeError("database unavailable")
                session.commit = failed_commit
                return session

            service.session_factory = failed_session
            with pytest.raises(RuntimeError):
                await service.save_document(doc_id, {"content": "Uncommitted", "version": original["version"]}, "admin")
            assert target.read_text(encoding="utf-8") == "Original"
            assert service.persona_manager.reload_count == 0
    asyncio.run(run())


def test_summary_scope_is_canonical_even_after_global_facts_tab(tmp_path):
    async def run():
        async with service_context(tmp_path) as (service, factory):
            await service.mutate_memory("summaries", "global", {"group_id": 12, "summary": "Corrected summary", "reason": "manual"}, "admin")
            async with factory() as session:
                assert await is_memory_protected(session, "summaries", "group", group_id=12)
                assert not await is_memory_protected(session, "summaries", "global", group_id=12)
    asyncio.run(run())


def test_automatic_writeback_preserves_corrected_and_deleted_facts(tmp_path, monkeypatch):
    from plugins import agent_memory

    async def run():
        async with service_context(tmp_path) as (service, factory):
            monkeypatch.setattr(agent_memory, "get_session", service.session_factory)
            monkeypatch.setattr(agent_memory, "_ENABLED", True)
            old = await service.mutate_memory("facts", "global", {"user_id": 7, "fact": "Old preference", "category": "preference", "reason": "manual"}, "admin")
            corrected = await service.mutate_memory("facts", "global", {"fact": "Correct preference", "reason": "correction"}, "admin", item_id=old["item"]["id"])
            removed = await service.mutate_memory("facts", "group", {"group_id": 12, "user_id": 7, "fact": "Deleted relationship", "category": "relationship", "reason": "manual"}, "admin")
            await service.mutate_memory("facts", "group", {"reason": "delete"}, "admin", item_id=removed["item"]["id"], deleting=True)
            messages = [SimpleNamespace(message_id=101, user_id=7, is_bot=False)]
            raw = [{"user_id": 7, "source_message_id": 101, "fact": fact, "category": category}
                   for fact, category in [("Old preference", "preference"), ("Correct preference", "preference"), ("Deleted relationship", "relationship")]]
            await agent_memory.MEMORY.persist_writeback(12, messages, "Generated episode", raw)
            async with factory() as session:
                globals_rows = (await session.execute(select(db.AgentGlobalPersonFact))).scalars().all()
                assert [row.fact for row in globals_rows] == ["Correct preference"]
                assert globals_rows[0].confidence == corrected["item"]["confidence"]
                assert (await session.execute(select(db.AgentPersonFact))).scalars().all() == []
                assert (await session.execute(select(db.AgentMemoryEpisode))).scalar_one().summary == "Generated episode"
    asyncio.run(run())


def test_promotion_does_not_recreate_global_tombstones_or_promote_manual_group_facts(tmp_path, monkeypatch):
    from plugins import agent_memory

    async def run():
        async with service_context(tmp_path) as (service, factory):
            monkeypatch.setattr(agent_memory, "get_session", service.session_factory)
            monkeypatch.setattr(agent_memory, "_ENABLED", True)
            deleted = await service.mutate_memory("facts", "global", {"user_id": 7, "fact": "Removed preference", "category": "preference", "reason": "manual"}, "admin")
            await service.mutate_memory("facts", "global", {"reason": "delete"}, "admin", item_id=deleted["item"]["id"], deleting=True)
            await service.mutate_memory("facts", "group", {"group_id": 12, "user_id": 7, "fact": "Keep group scoped", "category": "preference", "reason": "manual"}, "admin")
            async with factory() as session:
                session.add(db.AgentPersonFact(group_id=12, user_id=7, fingerprint=agent_memory._fingerprint("Removed preference"), fact="Removed preference", category="preference"))
                session.add(db.AgentPersonFact(group_id=12, user_id=7, fingerprint=agent_memory._fingerprint("Ordinary preference"), fact="Ordinary preference", category="preference"))
                await session.commit()
            assert await agent_memory.MEMORY.promote_stable_facts() == 1
            async with factory() as session:
                assert [row.fact for row in (await session.execute(select(db.AgentGlobalPersonFact))).scalars()] == ["Ordinary preference"]
            recalled = await agent_memory.MEMORY.render_recall(12, [SimpleNamespace(user_id=7)], include_episodes=False)
            assert "Removed preference" not in recalled
            assert "Ordinary preference" in recalled
    asyncio.run(run())


def test_automatic_expiration_pruning_and_episode_rewrite_preserve_manual_records(tmp_path, monkeypatch):
    from datetime import datetime, timedelta
    from plugins import agent_memory

    async def run():
        async with service_context(tmp_path) as (service, factory):
            monkeypatch.setattr(agent_memory, "get_session", service.session_factory)
            monkeypatch.setattr(agent_memory, "_ENABLED", True)
            monkeypatch.setattr(agent_memory, "_FACT_LIMIT_PER_USER", 1)
            monkeypatch.setattr(agent_memory, "_EPISODE_LIMIT", 1)
            manual = await service.mutate_memory("facts", "global", {"user_id": 7, "fact": "Manual expired fact", "expires_at": (datetime.now() - timedelta(days=1)).isoformat(), "reason": "manual"}, "admin")
            episode = await service.mutate_memory("episodes", "group", {"group_id": 12, "summary": "Corrected episode", "end_message_id": 100, "reason": "manual"}, "admin")
            for message_id in (100, 101):
                messages = [SimpleNamespace(message_id=message_id, user_id=7, is_bot=False)]
                await agent_memory.MEMORY.persist_writeback(12, messages, "Generated episode", [{"user_id": 7, "source_message_id": message_id, "category": "preference", "fact": "Ordinary fact"}])
            async with factory() as session:
                assert await session.get(db.AgentGlobalPersonFact, manual["item"]["id"]) is not None
                assert (await session.get(db.AgentMemoryEpisode, episode["item"]["id"])).summary == "Corrected episode"
    asyncio.run(run())


def test_summary_persistence_checks_tombstones_and_refreshes_cache(tmp_path, monkeypatch):
    from plugins.agent_context import AgentContextCache

    async def run():
        async with service_context(tmp_path) as (service, factory):
            monkeypatch.setattr(db, "get_session", service.session_factory)
            cache = AgentContextCache()
            assert await cache.persist_summary(12, "Automatic summary", 100)
            await service.mutate_memory("summaries", "group", {"summary": "Manual correction", "reason": "manual"}, "admin", item_id=12)
            assert not await cache.persist_summary(12, "Stale automatic summary", 101)
            assert cache._summary[12][0] == "Manual correction"
            await service.mutate_memory("summaries", "group", {"reason": "delete"}, "admin", item_id=12, deleting=True)
            assert not await cache.persist_summary(12, "Must not recreate", 102)
            assert cache._summary[12][0] == ""
            async with factory() as session:
                assert await session.get(db.AgentContextSummary, 12) is None
    asyncio.run(run())


def test_memory_write_lock_releases_when_session_creation_fails(monkeypatch):
    from plugins import agent_memory
    from plugins.console_services import MEMORY_WRITE_LOCK

    async def run():
        async def unavailable():
            raise RuntimeError("database unavailable")
        monkeypatch.setattr(agent_memory, "get_session", unavailable)
        monkeypatch.setattr(agent_memory, "_ENABLED", True)
        with pytest.raises(RuntimeError):
            await agent_memory.MEMORY.promote_stable_facts()
        assert not MEMORY_WRITE_LOCK.locked()
    asyncio.run(run())


def test_relationship_tombstones_block_recreation_but_existing_counters_keep_advancing(tmp_path, monkeypatch):
    from plugins import agent_memory

    async def run():
        async with service_context(tmp_path) as (service, factory):
            monkeypatch.setattr(agent_memory, "get_session", service.session_factory)
            monkeypatch.setattr(agent_memory, "_ENABLED", True)
            edited = await service.mutate_memory("relationships", "group", {"group_id": 12, "user_id": 7, "persona_id": "rin", "message_count": 30, "reason": "correct count"}, "admin")
            await agent_memory.MEMORY.record_batch_activity(12, [(None, SimpleNamespace(user_id=7, message_id=101), True)], "rin")
            async with factory() as session:
                assert (await session.get(db.AgentPersonaRelationshipState, edited["item"]["id"])).message_count == 31
            await service.mutate_memory("relationships", "group", {"reason": "delete"}, "admin", item_id=edited["item"]["id"], deleting=True)
            await agent_memory.MEMORY.record_batch_activity(12, [(None, SimpleNamespace(user_id=7, message_id=102), True)], "rin")
            await agent_memory.MEMORY.record_affinity_update(12, 7, 50, 1, "new activity", 102, "rin")
            async with factory() as session:
                assert (await session.execute(select(db.AgentPersonaRelationshipState))).scalars().all() == []
    asyncio.run(run())


def test_memory_sorting_is_allowlisted_stable_and_applied_before_pagination(tmp_path):
    async def run():
        async with service_context(tmp_path) as (service, factory):
            async with factory() as session:
                for user_id, count, score in [(1, 2, 20), (2, 50, -1), (3, 50, 55), (4, 1, 55)]:
                    session.add(db.AgentPersonaRelationshipState(persona_id="rin", group_id=12, user_id=user_id, message_count=count))
                    session.add(db.AgentPersonaAffinity(persona_id="rin", user_id=user_id, affinity_score=score))
                await session.commit()
            first = await service.memories("relationships", limit=2, sort_by="message_count")
            second = await service.memories("relationships", offset=2, limit=2, sort_by="message_count")
            assert first["total"] == 4
            assert [row["user_id"] for row in first["items"]] == [2, 3]
            assert [row["user_id"] for row in second["items"]] == [1, 4]
            highest = await service.memories("affinities", limit=2, sort_by="affinity_score")
            assert [row["user_id"] for row in highest["items"]] == [3, 4]
            lowest = await service.memories("affinities", limit=2, sort_by="affinity_score", sort_order="asc")
            assert [row["user_id"] for row in lowest["items"]] == [2, 1]
            for kind, sort_by, order in [("facts", "affinity_score", "desc"), ("affinities", "id; DROP TABLE", "desc"), ("affinities", "affinity_score", "random")]:
                with pytest.raises(HTTPException) as error:
                    await service.memories(kind, sort_by=sort_by, sort_order=order)
                assert error.value.status_code == 422
    asyncio.run(run())


def test_meme_image_proxy_is_scoped_validated_and_closes_storage_response(tmp_path, monkeypatch):
    import io
    import threading
    from PIL import Image
    from plugins import console_services

    output = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(output, format="PNG")
    png = output.getvalue()

    class ImageResponse:
        def __init__(self, data):
            self.data = data
            self.closed = self.released = False
        def read(self, amount):
            return self.data[:amount]
        def close(self):
            self.closed = True
        def release_conn(self):
            self.released = True

    class ImageMinio(FakeMinio):
        def __init__(self):
            super().__init__()
            self.payload = png
            self.responses = []
            self.calls = []
        def get_object(self, bucket, name):
            assert threading.current_thread() is not threading.main_thread()
            self.calls.append((bucket, name))
            response = ImageResponse(self.payload)
            self.responses.append(response)
            return response

    async def run():
        remote = ImageMinio()
        async with service_context(tmp_path, minio=remote) as (service, factory):
            async with factory() as session:
                item = db.MemeItem(bucket="approved", object_name="real.png", content_type="text/html")
                foreign = db.MemeItem(bucket="other", object_name="private.png")
                session.add_all([item, foreign])
                await session.commit()
                item_id, foreign_id = item.id, foreign.id
            listing = await service.memes()
            assert listing["items"][0]["url"] == f"/api/admin/memes/{item_id}/image"
            assert not remote.calls
            data, mime = await service.meme_image(item_id)
            assert (data, mime) == (png, "image/png")
            assert remote.calls == [("approved", "real.png")]
            with pytest.raises(HTTPException) as error:
                await service.meme_image(foreign_id)
            assert error.value.status_code == 404
            assert len(remote.calls) == 1
            remote.payload = b"<html><script>bad()</script></html>"
            with pytest.raises(HTTPException) as error:
                await service.meme_image(item_id)
            assert error.value.status_code == 415
            monkeypatch.setattr(console_services, "MEME_IMAGE_MAX_BYTES", 8)
            remote.payload = png
            with pytest.raises(HTTPException) as error:
                await service.meme_image(item_id)
            assert error.value.status_code == 413
            assert all(response.closed and response.released for response in remote.responses)
    asyncio.run(run())
