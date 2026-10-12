"""Offline classification consistency with SQLite, fake Redis and object store."""
import asyncio
import importlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from plugins.db import Base, MemeItem

classifier = importlib.import_module("tools.classify_memes")
syncer = importlib.import_module("tools.sync_memes_db")


def test_syncer_uses_same_synthetic_configuration_as_classifier():
    assert syncer.CONFIG == classifier.config
    assert syncer.CONFIG["redis"]["host"] == "127.0.0.1"
    assert syncer.CONFIG["redis"]["port"] == 1
    assert syncer.CONFIG["meme"]["minio"]["secret_key"] == "test-secret-key"
    assert syncer.CONFIG_PATH.resolve().parent != syncer.ROOT


def test_syncer_and_classifier_preserve_redis_auth_and_selected_database():
    for client, config in ((syncer.rds, syncer.CONFIG), (classifier.rds, classifier.config)):
        options = client.connection_pool.connection_kwargs
        assert options["password"] == config["redis"]["password"] == "test-redis-password"
        assert options["db"] == config["redis"]["db"]


class Adapter:
    def __init__(self, engine):
        self.session = Session(engine, expire_on_commit=False)
    async def __aenter__(self): return self
    async def __aexit__(self, *_): self.session.close()
    async def execute(self, *args): return self.session.execute(*args)
    async def commit(self): self.session.commit()
    def add(self, row): self.session.add(row)


class Redis:
    def __init__(self):
        self.values = {}
        self.sets = {}
        self.writes = []
    def get(self, key): return self.values.get(key)
    def exists(self, key): return bool(key in self.values or key in self.sets)
    def scard(self, key): return len(self.sets.get(key, set()))
    def pipeline(self): return Pipeline(self)
    def set(self, key, value):
        self.writes.append(("set", key, value))
        self.values[key] = value
    def sadd(self, key, *values):
        self.writes.append(("sadd", key, values))
        self.sets.setdefault(key, set()).update(values)
    def srem(self, key, value):
        self.writes.append(("srem", key, value))
        self.sets.setdefault(key, set()).discard(value)
    def delete(self, key):
        self.writes.append(("delete", key))
        self.sets.pop(key, None)
        self.values.pop(key, None)


class Pipeline:
    def __init__(self, redis): self.redis, self.calls = redis, []
    def __getattr__(self, name):
        def command(*args):
            self.calls.append((name, args))
            return self
        return command
    def execute(self):
        for name, args in self.calls: getattr(self.redis, name)(*args)


class Minio:
    def __init__(self, objects): self.objects, self.deleted = objects, []
    def list_objects(self, bucket): return self.objects
    def remove_object(self, bucket, name): self.deleted.append((bucket, name))
    def get_object(self, *args): raise AssertionError("skip-done must not download")


def object_(name):
    return SimpleNamespace(object_name=name, size=42,
                           last_modified=datetime(2026, 1, 2, tzinfo=timezone.utc))


@pytest.fixture
def storage(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine, tables=[MemeItem.__table__])
    async def factory(): return Adapter(engine)
    redis, minio = Redis(), Minio([])
    runner = asyncio.Runner()
    monkeypatch.setattr(classifier, "get_session", factory)
    monkeypatch.setattr(classifier, "rds", redis)
    monkeypatch.setattr(classifier, "BUCKET", "test-memes")
    monkeypatch.setattr(classifier, "minio_client", minio)
    monkeypatch.setattr(classifier, "_ROUTE_RUNNER", runner)
    monkeypatch.setattr(syncer, "BUCKET", "test-memes")
    try:
        yield engine, factory, redis, minio, runner
    finally:
        runner.close()
        engine.dispose()


def add(engine, name, status="active", emotion="", sends=7):
    with Session(engine) as session:
        item = MemeItem(bucket="test-memes", object_name=name, emotion=emotion, status=status,
                        send_count=sends, note="human note", last_sent_at=datetime(2026, 1, 1))
        session.add(item)
        session.commit()
        return item.id


def read(engine, item_id):
    with Session(engine, expire_on_commit=False) as session:
        return session.get(MemeItem, item_id)


@pytest.mark.parametrize("status", ["active", "disabled", "deleted", "missing"])
def test_classification_updates_db_without_resetting_status_or_history(storage, status):
    engine, factory, redis, minio, runner = storage
    item_id = add(engine, "one.jpg", status, "sad", 12)
    redis.sadd("meme:pool", "one.jpg")
    redis.sadd("meme:emotion:sad", "one.jpg")
    redis.sadd("meme:emotion:cool", "one.jpg")  # stale multiple memberships
    classifier._write_to_redis("one.jpg", "happy", False)
    item = read(engine, item_id)
    assert (item.emotion, item.status, item.send_count, item.note) == ("happy", status, 12, "human note")
    assert item.classified_at and item.last_sent_at == datetime(2026, 1, 1)
    assert ("one.jpg" in redis.sets["meme:pool"]) == (status == "active")
    assert ("one.jpg" in redis.sets.get("meme:emotion:happy", set())) == (status == "active")
    assert all("one.jpg" not in redis.sets.get("meme:emotion:" + label, set()) for label in classifier.EMOTIONS if label != "happy")


def test_purge_marks_deleted_preserves_counters_and_removes_object(storage):
    engine, _, redis, minio, _ = storage
    item_id = add(engine, "one.jpg", "active", "cool", 15)
    redis.set("meme:tags:one.jpg", "cool")
    redis.sadd("meme:pool", "one.jpg")
    redis.sadd("meme:emotion:cool", "one.jpg")
    classifier._delete_meme("one.jpg", False)
    item = read(engine, item_id)
    assert item.status == "deleted" and item.send_count == 15 and item.emotion == "cool"
    assert not redis.get("meme:tags:one.jpg") and "one.jpg" not in redis.sets["meme:pool"]
    assert minio.deleted == [("test-memes", "one.jpg")]


def test_dry_run_never_writes_db_redis_or_minio(storage):
    engine, factory, redis, minio, runner = storage
    item_id = add(engine, "one.jpg", "disabled", "sad", 18)
    async def forbid(): raise AssertionError("dry run must not open classification write session")
    old_factory = classifier.get_session
    classifier.get_session = forbid
    try:
        classifier._write_to_redis("one.jpg", "happy", True)
        classifier._delete_meme("one.jpg", True)
    finally:
        classifier.get_session = old_factory
    result = runner.run(syncer.sync_metadata([object_("one.jpg"), object_("new.jpg")],
        lambda key: "happy", factory, dry_run=True))
    syncer.rebuild_indexes(result["items"], {"one.jpg", "new.jpg"}, redis, dry_run=True)
    assert not redis.writes and not minio.deleted
    item = read(engine, item_id)
    assert (item.status, item.emotion, item.send_count) == ("disabled", "sad", 18)
    with Session(engine) as session:
        assert len(session.scalars(select(MemeItem)).all()) == 1


def test_sync_restores_missing_but_preserves_disabled_deleted_and_invalid_labels(storage):
    engine, factory, redis, minio, runner = storage
    ids = {status: add(engine, status + ".jpg", status, "sad") for status in ["active", "disabled", "deleted", "missing"]}
    absent_id = add(engine, "absent.jpg", "active", "happy")
    redis.set("meme:tags:missing.jpg", "cool")
    redis.set("meme:tags:active.jpg", "invalid")
    redis.set("meme:tags:new.jpg", "invalid")
    objects = [object_(status + ".jpg") for status in ids] + [object_("new.jpg")]
    result = runner.run(syncer.sync_metadata(objects, redis.get, factory))
    syncer.rebuild_indexes(result["items"], {obj.object_name for obj in objects}, redis)
    assert read(engine, ids["disabled"]).status == "disabled"
    assert read(engine, ids["deleted"]).status == "deleted"
    assert read(engine, ids["missing"]).status == "active"
    assert read(engine, ids["missing"]).emotion == "cool"
    assert read(engine, absent_id).status == "missing"
    assert read(engine, ids["active"]).emotion == "sad"
    assert redis.sets["meme:pool"] == {"active.jpg", "missing.jpg", "new.jpg"}
    with Session(engine) as session:
        assert session.scalar(select(MemeItem).where(MemeItem.object_name == "new.jpg")).emotion == ""
    assert "invalid" not in redis.values.values()


@pytest.mark.parametrize("dry_run", [False, True])
def test_skip_done_repairs_existing_labels_without_model_or_download(storage, monkeypatch, dry_run):
    engine, _, redis, minio, _ = storage
    item_id = add(engine, "one.jpg", "disabled", "")
    redis.set("meme:tags:one.jpg", "cool")
    minio.objects = [object_("one.jpg")]
    async def readonly_load(): pass
    monkeypatch.setattr(classifier, "_load_routes_readonly", readonly_load)
    monkeypatch.setattr(classifier, "_classify_image", lambda *args: pytest.fail("skip-done must not pay for model"))
    monkeypatch.setattr(classifier, "_download_from_minio", lambda *args: pytest.fail("skip-done must not download"))
    monkeypatch.setattr("sys.argv", ["classify_memes.py", "--skip-done"] + (["--dry-run"] if dry_run else []))
    redis.writes.clear()
    classifier.main()
    assert read(engine, item_id).emotion == ("" if dry_run else "cool")
    assert read(engine, item_id).status == "disabled"
    assert bool(redis.writes) == (not dry_run)


def test_no_metadata_tag_cached_for_sync_script_without_joining_live_pool(storage):
    engine, factory, redis, minio, runner = storage
    classifier._write_to_redis("new.jpg", "funny", False)
    assert redis.get("meme:tags:new.jpg") == "funny"
    assert "new.jpg" not in redis.sets.get("meme:pool", set())
    with Session(engine) as session:
        assert not session.scalars(select(MemeItem)).all()
    result = runner.run(syncer.sync_metadata([object_("new.jpg")], redis.get, factory))
    syncer.rebuild_indexes(result["items"], {"new.jpg"}, redis)
    assert "new.jpg" in redis.sets["meme:pool"]


def test_invalid_never_written_as_emotion(storage):
    with pytest.raises(ValueError): classifier._write_to_redis("one.jpg", "invalid", False)


def test_failed_storage_purge_keeps_untracked_object_deleted_after_sync(storage, monkeypatch):
    engine, factory, redis, minio, runner = storage
    def fail(*args): raise OSError("storage unavailable")
    monkeypatch.setattr(minio, "remove_object", fail)
    classifier._delete_meme("unknown.jpg", False)
    result = runner.run(syncer.sync_metadata([object_("unknown.jpg")], redis.get, factory))
    syncer.rebuild_indexes(result["items"], {"unknown.jpg"}, redis)
    with Session(engine) as session:
        row = session.scalar(select(MemeItem).where(MemeItem.object_name == "unknown.jpg"))
        assert row.status == "deleted" and row.send_count == 0
    assert "unknown.jpg" not in redis.sets.get("meme:pool", set())
