"""Exercise real selection/send code with synthetic SQLite, Redis and OneBot responses."""

import ast
import asyncio
import importlib
import io
from datetime import datetime
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import nonebot
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class RedisStore:
    def __init__(self):
        self.sets = {}
        self.lists = {}
        self.cooldowns = set()

    def smembers(self, key):
        return self.sets.get(key, set())

    def scard(self, key):
        return len(self.smembers(key))

    def sismember(self, key, item):
        return item in self.smembers(key)

    def srem(self, key, item):
        self.sets.get(key, set()).discard(item)

    def lrange(self, key, start, stop):
        values = self.lists.get(key, [])
        return values[start:] if stop == -1 else values[start:stop + 1]

    def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)

    def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)

    def ltrim(self, key, start, stop):
        self.lists[key] = self.lrange(key, start, stop)

    def expire(self, *_):
        pass

    def exists(self, key):
        return int(key in self.cooldowns)

    def setex(self, key, *_):
        self.cooldowns.add(key)


@pytest.fixture
def collector(monkeypatch):
    try:
        nonebot.get_driver()
    except ValueError:
        nonebot.init(driver="~fastapi", log_level="ERROR")

    # Import the actual MemeItem class without executing production DB/config setup.
    root = Path(__file__).resolve().parents[1]
    package_name = "_meme_sending_tests"
    package = ModuleType(package_name)
    package.__path__ = [str(root / "plugins")]
    monkeypatch.setitem(sys.modules, package_name, package)
    database = ModuleType(package_name + ".db")

    class Base(DeclarativeBase):
        pass

    database.__dict__.update({name: getattr(sa, name) for name in (
        "Integer", "BigInteger", "String", "Text", "DateTime", "UniqueConstraint",
    )})
    database.__dict__.update(Base=Base, Mapped=Mapped, mapped_column=mapped_column, datetime=datetime)
    source = ast.parse((root / "plugins" / "db.py").read_text(encoding="utf-8"))
    item = next(node for node in source.body if isinstance(node, ast.ClassDef) and node.name == "MemeItem")
    exec(compile(ast.Module(body=[item], type_ignores=[]), "plugins/db.py:MemeItem", "exec"), database.__dict__)

    async def no_production_session():
        raise AssertionError("production DB must never be touched")

    database.get_session = no_production_session
    monkeypatch.setitem(sys.modules, database.__name__, database)
    runtime = ModuleType(package_name + ".console_runtime")
    runtime.ready = lambda: False
    runtime.SEND_CONTEXT = SimpleNamespace(get=lambda: None)
    monkeypatch.setitem(sys.modules, runtime.__name__, runtime)
    routes = ModuleType(package_name + ".model_routes")
    routes.complete = AsyncMock(return_value="happy")
    monkeypatch.setitem(sys.modules, routes.__name__, routes)

    # Read a public synthetic configuration while importing the collector.
    import builtins
    original_open = builtins.open

    def synthetic_open(file, *args, **kwargs):
        if str(file) == "config.yaml":
            return io.StringIO("""meme:
  minio:
    endpoint: localhost:9000
    access_key: test-access
    secret_key: test-secret
    bucket: approved
redis:
  host: localhost
  port: 6379
ai:
  group_chat: {}
""")
        return original_open(file, *args, **kwargs)

    with monkeypatch.context() as importing:
        importing.setattr(builtins, "open", synthetic_open)
        module = importlib.import_module(package_name + ".meme_collector")
    monkeypatch.setattr(module, "rds", RedisStore())
    monkeypatch.setattr(module, "_presigned_url", lambda name: "https://example.invalid/" + name)
    yield module
    sys.modules.pop(module.__name__, None)
    sys.modules.pop(package_name + ".meme_policy", None)


async def database_for(collector, tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "memes.sqlite"),
                                 connect_args={"timeout": 30})
    async with engine.begin() as connection:
        await connection.run_sync(collector.MemeItem.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def session():
        return factory()

    collector.get_session = session
    return engine, factory


def test_random_selection_uses_entire_pool_without_favoring_new_memes(collector, tmp_path, monkeypatch):
    async def run():
        engine, factory = await database_for(collector, tmp_path)
        try:
            old_names = {f"old-{n}.png" for n in range(200)}
            new_names = {f"new-{n}.png" for n in range(3)}
            async with factory() as session:
                session.add_all([collector.MemeItem(bucket="approved", object_name=name, send_count=26)
                                 for name in old_names])
                session.add_all([collector.MemeItem(bucket="approved", object_name=name, send_count=0)
                                 for name in new_names])
                session.add(collector.MemeItem(bucket="other", object_name="foreign.png", send_count=0))
                session.add(collector.MemeItem(bucket="approved", object_name="disabled.png", status="disabled", send_count=0))
                await session.commit()
            collector.rds.lists[collector._recent_key(1)] = ["old-0.png", "new-0.png"]
            inspected = []

            def choice(pool):
                inspected.extend(pool)
                assert "old-199.png" in pool
                return "old-199.png"

            monkeypatch.setattr(collector.random, "choice", choice)
            assert await collector._pick_meme_object(1) == "old-199.png"
            expected = (old_names | new_names) - {"old-0.png", "new-0.png"}
            assert set(inspected) == expected
            assert len(inspected) == len(expected)
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_recent_exclusion_checks_candidates_beyond_previous_sql_limit(collector, tmp_path, monkeypatch):
    async def run():
        engine, factory = await database_for(collector, tmp_path)
        try:
            recent = {f"recent-{n}.png" for n in range(80)}
            async with factory() as session:
                session.add_all([collector.MemeItem(bucket="approved", object_name=name, send_count=0)
                                 for name in recent])
                session.add(collector.MemeItem(bucket="approved", object_name="old-unseen.png", send_count=27))
                await session.commit()
            monkeypatch.setattr(collector, "RECENT_SIZE", 80)
            collector.rds.lists[collector._recent_key(1)] = list(recent)
            assert await collector._pick_meme_object(1) == "old-unseen.png"
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("matching_count", [7, 8])
def test_random_selection_keeps_emotion_threshold_and_active_scope(collector, tmp_path, monkeypatch, matching_count):
    async def run():
        engine, factory = await database_for(collector, tmp_path)
        try:
            happy = {f"happy-{n}.png" for n in range(matching_count)}
            other = {f"other-{n}.png" for n in range(100)}
            async with factory() as session:
                session.add_all([collector.MemeItem(bucket="approved", object_name=name, emotion="happy", send_count=27)
                                 for name in happy])
                session.add_all([collector.MemeItem(bucket="approved", object_name=name, emotion="sad", send_count=0)
                                 for name in other])
                session.add(collector.MemeItem(bucket="other", object_name="foreign-happy.png", emotion="happy"))
                session.add(collector.MemeItem(bucket="approved", object_name="disabled-happy.png", emotion="happy", status="disabled"))
                await session.commit()
            collector.rds.lists[collector._recent_key(1)] = ["happy-0.png"]
            inspected = []

            def choice(pool):
                inspected.extend(pool)
                return pool[0]

            monkeypatch.setattr(collector.random, "choice", choice)
            await collector._pick_meme_object(1, "happy")
            expected = (happy if matching_count >= collector.EMOTION_MIN_POOL else happy | other) - {"happy-0.png"}
            assert set(inspected) == expected
            assert len(inspected) == len(expected)
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_all_recent_pool_can_be_reused_and_history_is_group_scoped(collector, tmp_path, monkeypatch):
    async def run():
        engine, factory = await database_for(collector, tmp_path)
        try:
            names = {"old.png", "new.png"}
            async with factory() as session:
                session.add_all([collector.MemeItem(bucket="approved", object_name=name) for name in names])
                await session.commit()
            collector.rds.lists[collector._recent_key(1)] = ["old.png", "new.png"]
            collector.rds.lists[collector._recent_key(2)] = ["new.png"]
            inspected = []

            def choice(pool):
                inspected.append(set(pool))
                return pool[0]

            monkeypatch.setattr(collector.random, "choice", choice)
            assert await collector._pick_meme_object(1) in names
            assert await collector._pick_meme_object(2) == "old.png"
            assert inspected == [names, {"old.png"}]
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_sql_empty_pool_does_not_restore_disabled_redis_members(collector, tmp_path):
    async def run():
        engine, factory = await database_for(collector, tmp_path)
        try:
            collector.rds.sets[collector.POOL_KEY] = {"disabled.png", "deleted.png"}
            async with factory() as session:
                session.add_all([collector.MemeItem(bucket="approved", object_name=name, status=status)
                                 for name, status in [("disabled.png", "disabled"), ("deleted.png", "deleted")]])
                await session.commit()
            assert await collector._pick_meme_object(1) is None
            assert await collector.get_reply_meme("", 1) is None
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_sql_failure_falls_back_to_active_redis_pool_only(collector, monkeypatch):
    async def unavailable():
        raise RuntimeError("synthetic unavailable DB")

    monkeypatch.setattr(collector, "get_session", unavailable)
    monkeypatch.setattr(collector, "EMOTION_MIN_POOL", 1)
    collector.rds.sets[collector.POOL_KEY] = {"active.png"}
    collector.rds.sets[collector.EMOTION_PREFIX + "happy"] = {"stale.png", "active.png"}
    assert asyncio.run(collector._pick_meme_object(1, "happy")) == "active.png"


def test_sql_failure_random_fallback_uses_entire_pool_and_recent_exclusion(collector, monkeypatch):
    async def unavailable():
        raise RuntimeError("synthetic unavailable DB")

    monkeypatch.setattr(collector, "get_session", unavailable)
    names = {f"old-{n}.png" for n in range(200)} | {"new.png"}
    collector.rds.sets[collector.POOL_KEY] = names
    collector.rds.sets[collector.EMOTION_PREFIX + "happy"] = {"new.png", "stale.png"}
    collector.rds.lists[collector._recent_key(1)] = ["old-0.png"]
    inspected = []

    def choice(pool):
        inspected.extend(pool)
        return "old-199.png"

    monkeypatch.setattr(collector.random, "choice", choice)
    assert asyncio.run(collector._pick_meme_object(1, "happy")) == "old-199.png"
    expected = names - {"old-0.png"}
    assert set(inspected) == expected
    assert len(inspected) == len(expected)


def test_sql_failure_fallback_can_reuse_all_recent_pool(collector, monkeypatch):
    async def unavailable():
        raise RuntimeError("synthetic unavailable DB")

    monkeypatch.setattr(collector, "get_session", unavailable)
    names = {"old.png", "new.png"}
    collector.rds.sets[collector.POOL_KEY] = names
    collector.rds.lists[collector._recent_key(1)] = list(names)
    inspected = []

    def choice(pool):
        inspected.extend(pool)
        return "old.png"

    monkeypatch.setattr(collector.random, "choice", choice)
    assert asyncio.run(collector._pick_meme_object(1)) == "old.png"
    assert set(inspected) == names


def test_pure_reply_selection_never_updates_send_state(collector, monkeypatch):
    collector.rds.sets[collector.POOL_KEY] = {"one.png"}
    collector.rds.sets[collector.EMOTION_PREFIX + "happy"] = {"one.png"}
    monkeypatch.setattr(collector, "_get_emotion_meme_url", AsyncMock(return_value=("one.png", "https://example.invalid/one")))
    mark = AsyncMock()
    monkeypatch.setattr(collector, "_mark_meme_sent", mark)
    assert asyncio.run(collector.get_reply_meme_url("真开心", 1)) == "https://example.invalid/one"
    mark.assert_not_called()
    assert collector.rds.lists == {}
    assert collector.rds.cooldowns == set()


@pytest.mark.parametrize("result", [None, False, {}, {"_console_blocked": True},
                                         {"_console_blocked": True, "message_id": 2}, {"message_id": False}])
def test_unconfirmed_reply_send_is_not_counted(collector, monkeypatch, result):
    monkeypatch.setattr(collector, "get_reply_meme", AsyncMock(return_value=("one.png", "https://example.invalid/one")))
    monkeypatch.setattr(collector.asyncio, "sleep", AsyncMock())
    mark = AsyncMock()
    monkeypatch.setattr(collector, "_mark_meme_sent", mark)
    bot = SimpleNamespace(send_group_msg=AsyncMock(return_value=result))
    assert not asyncio.run(collector.maybe_send_reply_meme(bot, 1, "hello", rate=1))
    mark.assert_not_called()
    assert collector.rds.lists == {}
    assert collector.rds.cooldowns == set()


def test_reply_transport_failure_is_not_counted(collector, monkeypatch):
    monkeypatch.setattr(collector, "get_reply_meme", AsyncMock(return_value=("one.png", "https://example.invalid/one")))
    monkeypatch.setattr(collector.asyncio, "sleep", AsyncMock())
    mark = AsyncMock()
    monkeypatch.setattr(collector, "_mark_meme_sent", mark)
    bot = SimpleNamespace(send_group_msg=AsyncMock(side_effect=TimeoutError()))
    assert not asyncio.run(collector.maybe_send_reply_meme(bot, 1, "hello", rate=1))
    mark.assert_not_called()
    assert collector.rds.lists == {}
    assert collector.rds.cooldowns == set()


def test_confirmed_reply_send_updates_real_sql_count_above_27(collector, tmp_path, monkeypatch):
    async def run():
        engine, factory = await database_for(collector, tmp_path)
        try:
            async with factory() as session:
                session.add(collector.MemeItem(bucket="approved", object_name="one.png", send_count=27))
                await session.commit()
            monkeypatch.setattr(collector, "get_reply_meme", AsyncMock(return_value=("one.png", "https://example.invalid/one")))
            monkeypatch.setattr(collector.asyncio, "sleep", AsyncMock())
            bot = SimpleNamespace(send_group_msg=AsyncMock(return_value={"message_id": 77}))
            assert await collector.maybe_send_reply_meme(bot, 1, "hello", rate=1)
            async with factory() as session:
                item = (await session.execute(sa.select(collector.MemeItem))).scalar_one()
                assert item.send_count == 28
                assert item.last_sent_at is not None
            assert collector._recent_sent(1) == {"one.png"}
            assert collector._is_cooling(1)
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_atomic_increment_preserves_concurrent_updates(collector, tmp_path):
    async def run():
        engine, factory = await database_for(collector, tmp_path)
        try:
            async with factory() as session:
                session.add(collector.MemeItem(bucket="approved", object_name="one.png", send_count=27))
                await session.commit()
            await asyncio.gather(*(collector._mark_meme_sent("one.png") for _ in range(12)))
            async with factory() as session:
                item = (await session.execute(sa.select(collector.MemeItem))).scalar_one()
                assert item.send_count == 39
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_confirmed_send_cache_failure_does_not_prevent_count_attempt(collector, monkeypatch):
    mark = AsyncMock()
    monkeypatch.setattr(collector, "_mark_meme_sent", mark)
    monkeypatch.setattr(collector, "_remember_sent", Mock(side_effect=RuntimeError("cache unavailable")))
    asyncio.run(collector._record_confirmed_meme_send(1, "one.png"))
    mark.assert_awaited_once_with("one.png")
    assert collector._is_cooling(1)


@pytest.mark.parametrize("result, counted", [({"message_id": 88}, True), ({"_console_blocked": True}, False), (None, False)])
def test_legacy_message_send_counts_only_confirmed_result(collector, monkeypatch, result, counted):
    monkeypatch.setattr(collector, "_push_chat_message", lambda *_: None)
    monkeypatch.setattr(collector, "_meme_rate", lambda group_id, key, default: 1)
    monkeypatch.setattr(collector, "_get_random_meme_url", AsyncMock(return_value=("one.png", "https://example.invalid/one")))
    mark = AsyncMock()
    monkeypatch.setattr(collector, "_mark_meme_sent", mark)
    collector.rds.sets[collector.POOL_KEY] = {"one.png"}
    event = SimpleNamespace(group_id=1, user_id=2, message=[SimpleNamespace(type="text", data={"text": "hello"})])
    bot = SimpleNamespace(send_group_msg=AsyncMock(return_value=result))
    asyncio.run(collector.handle_meme(bot, event))
    assert mark.await_count == int(counted)
    assert collector._is_cooling(1) == counted


def test_emotion_route_prompt_is_readable_and_returns_allowed_label(collector):
    routes = sys.modules[collector.__package__ + ".model_routes"]
    assert asyncio.run(collector._detect_emotion(["太开心了"])) == "happy"
    args, kwargs = routes.complete.await_args
    assert args[0] == "emotion"
    prompt = args[1][0]["content"]
    assert "只输出下列标签中的一个英文标签" in prompt
    assert "happy:" in prompt
    assert "太开心了" in prompt
    assert kwargs["timeout"] == 15
