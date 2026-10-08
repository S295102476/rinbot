"""Real SQL statements tested against isolated SQLite, never production MySQL."""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
import types

import pytest
from sqlalchemy import create_engine, inspect, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Session
from sqlalchemy.pool import StaticPool

class _SQLBase(DeclarativeBase):
    pass


async def forbidden_session():
    raise AssertionError("Production database access is forbidden in these tests")


_db = types.ModuleType("_minigames_sqltest.db")
_db.Base, _db.engine, _db.get_session = _SQLBase, None, forbidden_session
sys.modules[_db.__name__] = _db
_spec = importlib.util.spec_from_file_location(
    "_minigames_sqltest.plugins.storage",
    Path(__file__).parents[1] / "plugins/minigames/storage.py",
)
_storage = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _storage
_spec.loader.exec_module(_storage)
SQLStore = _storage.SQLStore
MinigameSession = _storage.MinigameSession
SessionConflict = _storage.SessionConflict


class SessionAdapter:
    def __init__(self, engine):
        self.session = Session(engine, expire_on_commit=False)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.session.close()

    def add(self, row):
        self.session.add(row)

    async def execute(self, *args):
        return self.session.execute(*args)

    async def get(self, *args):
        return self.session.get(*args)

    async def commit(self):
        self.session.commit()


class ConnectionAdapter:
    def __init__(self, connection):
        self.connection = connection

    async def run_sync(self, callback):
        return callback(self.connection)


class EngineAdapter:
    def __init__(self, engine):
        self.engine = engine

    @asynccontextmanager
    async def begin(self):
        with self.engine.begin() as connection:
            yield ConnectionAdapter(connection)


@pytest.fixture
def database():
    engine = create_engine("sqlite://", poolclass=StaticPool)

    async def session_factory():
        return SessionAdapter(engine)

    store = SQLStore(session_factory=session_factory, database_engine=EngineAdapter(engine))
    asyncio.run(store.initialize())
    yield store, engine
    engine.dispose()


def state(bot_id=99, group_id=10, version=1):
    return {"bot_id": bot_id, "group_id": group_id, "version": version,
            "board": [0] * 9, "players": {"1": {"id": 11, "name": "测试玩家"}},
            "status": "active"}


def test_initialize_only_creates_minigame_table_and_is_idempotent(database):
    store, engine = database
    asyncio.run(store.initialize())
    assert inspect(engine).get_table_names() == ["minigame_sessions"]


def test_roundtrip_and_missing_session(database):
    store, _ = database
    value = state()
    asyncio.run(store.save(value, None))
    assert asyncio.run(store.load(99, 10)) == value
    assert asyncio.run(store.load(99, 11)) is None
    assert asyncio.run(store.load_all()) == [value]


def test_successful_version_update_replaces_one_current_row(database):
    store, engine = database
    asyncio.run(store.save(state(), None))
    changed = state(version=2)
    changed["board"][4] = 1
    asyncio.run(store.save(changed, 1))
    assert asyncio.run(store.load(99, 10)) == changed
    with Session(engine) as session:
        assert len(session.execute(select(MinigameSession)).scalars().all()) == 1


def test_stale_version_never_overwrites_saved_board(database):
    store, _ = database
    asyncio.run(store.save(state(), None))
    winner = state(version=2)
    winner["board"][4] = 1
    asyncio.run(store.save(winner, 1))
    stale = deepcopy(winner)
    stale["board"][0] = 1
    with pytest.raises(SessionConflict):
        asyncio.run(store.save(stale, 1))
    assert asyncio.run(store.load(99, 10)) == winner


def test_duplicate_creation_does_not_reset_an_existing_game(database):
    store, _ = database
    value = state()
    value["board"][4] = 1
    asyncio.run(store.save(value, None))
    with pytest.raises(IntegrityError):
        asyncio.run(store.save(state(), None))
    assert asyncio.run(store.load(99, 10)) == value


def test_distinct_bots_and_groups_are_isolated(database):
    store, _ = database
    values = [state(99, 10), state(99, 20), state(100, 10)]
    for value in values:
        asyncio.run(store.save(value, None))
    changed = state(99, 10, 2)
    changed["status"] = "ended"
    asyncio.run(store.save(changed, 1))
    assert asyncio.run(store.load(99, 10)) == changed
    assert asyncio.run(store.load(99, 20)) == values[1]
    assert asyncio.run(store.load(100, 10)) == values[2]


@pytest.mark.parametrize("field,value", [("bot_id", 100), ("group_id", 20), ("version", 2)])
def test_recovery_rejects_payload_identity_or_version_corruption(database, field, value):
    store, engine = database
    original = state()
    asyncio.run(store.save(original, None))
    damaged = dict(original, **{field: value})
    with Session(engine) as session:
        session.execute(update(MinigameSession).values(payload=json.dumps(damaged)))
        session.commit()
    with pytest.raises(SessionConflict):
        asyncio.run(store.load_all())
    with pytest.raises(SessionConflict):
        asyncio.run(store.load(99, 10))


def test_nonfinite_json_is_rejected_without_modifying_storage(database):
    store, _ = database
    value = state()
    asyncio.run(store.save(value, None))
    damaged = state(version=2)
    damaged["expires_at"] = float("nan")
    with pytest.raises(ValueError):
        asyncio.run(store.save(damaged, 1))
    assert asyncio.run(store.load(99, 10)) == value


def test_number_session_roundtrip_and_restore_use_existing_table(database):
    from test_minigame_number_service import GameService, run, present, wrong, KEY
    store, _ = database
    async def scenario():
        service = GameService(store)
        await service.initialize()
        await run(service, "start", mode="race")
        await present(service)
        await run(service, "guess", uid=22, mid=2, argument=wrong(service))
        expected = deepcopy(service.states[KEY])
        assert await store.load(*KEY) == expected
        restored = GameService(store)
        await restored.initialize()
        assert restored.states[KEY] == expected
        result = await run(restored, "guess", uid=33, mid=3, argument=expected["secret"])
        restored.validate(result.state)
        assert (await store.load(*KEY))["winner_id"] == 33
    asyncio.run(scenario())
