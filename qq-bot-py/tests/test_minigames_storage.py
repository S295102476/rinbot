"""Durable writes on isolated SQLite, never on the configured production DB."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import types

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase


class _TestBase(DeclarativeBase):
    pass


async def _forbidden_default_session():
    raise AssertionError("Tests must inject their isolated session factory")


_db = types.ModuleType("_isolated_minigames.db")
_db.Base, _db.engine, _db.get_session = _TestBase, None, _forbidden_default_session
sys.modules[_db.__name__] = _db
_spec = importlib.util.spec_from_file_location(
    "_isolated_minigames.plugins.storage",
    Path(__file__).parents[1] / "plugins/minigames/storage.py",
)
_storage = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _storage
_spec.loader.exec_module(_storage)
SQLStore = _storage.SQLStore
SessionConflict = _storage.SessionConflict
MinigameSession = _storage.MinigameSession


def _state(bot_id=99, group_id=100, version=1):
    return {"bot_id": bot_id, "group_id": group_id, "version": version,
            "game": "tictactoe", "board": [0] * 9, "name": "棋局"}


async def _with_store(check):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def session_factory():
        return factory()

    try:
        store = SQLStore(session_factory=session_factory, database_engine=engine)
        await store.initialize()
        await check(store, factory)
    finally:
        await engine.dispose()


def test_save_load_and_replace_preserve_one_row():
    async def check(store, _factory):
        assert await store.load(99, 100) is None
        value = _state()
        await store.save(value, None)
        assert await store.load(99, 100) == value
        next_value = _state(version=2)
        next_value["board"][4] = 1
        await store.save(next_value, 1)
        assert await store.load_all() == [next_value]
        restored = SQLStore(session_factory=store.session_factory, database_engine=store.engine)
        await restored.initialize()
        assert await restored.load(99, 100) == next_value
    asyncio.run(_with_store(check))


def test_compare_and_swap_rejects_stale_write_without_losing_latest_move():
    async def check(store, _factory):
        await store.save(_state(), None)
        current = _state(version=2)
        current["board"][0] = 1
        await store.save(current, 1)
        stale = _state(version=2)
        stale["board"][8] = 1
        with pytest.raises(SessionConflict):
            await store.save(stale, 1)
        assert await store.load(99, 100) == current
    asyncio.run(_with_store(check))


def test_bot_and_group_identity_isolation():
    async def check(store, _factory):
        values = [_state(), _state(bot_id=98), _state(group_id=101)]
        for value in values:
            await store.save(value, None)
        assert len(await store.load_all()) == 3
        for value in values:
            assert await store.load(value["bot_id"], value["group_id"]) == value
    asyncio.run(_with_store(check))


@pytest.mark.parametrize("field,value", [("version", 9), ("bot_id", 88), ("group_id", 77)])
def test_corrupt_persisted_identity_is_not_silently_loaded(field, value):
    async def check(store, factory):
        await store.save(_state(), None)
        corrupt = _state()
        corrupt[field] = value
        async with factory() as session:
            await session.execute(update(MinigameSession).values(payload=json.dumps(corrupt)))
            await session.commit()
        with pytest.raises(SessionConflict):
            await store.load_all()
    asyncio.run(_with_store(check))


def test_invalid_json_fails_restore_without_resetting_data():
    async def check(store, factory):
        await store.save(_state(), None)
        async with factory() as session:
            await session.execute(update(MinigameSession).values(payload="broken"))
            await session.commit()
        with pytest.raises(ValueError):
            await store.load_all()
        async with factory() as session:
            row = await session.get(MinigameSession, (99, 100))
            assert row.payload == "broken"
    asyncio.run(_with_store(check))
