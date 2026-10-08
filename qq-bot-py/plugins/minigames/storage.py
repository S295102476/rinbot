"""One recoverable session per bot/group; no historical score tables."""
import json
from datetime import datetime
from sqlalchemy import BigInteger, Integer, Text, DateTime, select, update
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from ..db import engine, get_session


class Base(DeclarativeBase):
    # Keep startup DDL out of unrelated plugins' concurrent create_all hooks.
    pass


class MinigameSession(Base):
    __tablename__ = "minigame_sessions"
    bot_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    group_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class SessionConflict(RuntimeError):
    pass


class SQLStore:
    def __init__(self, session_factory=get_session, database_engine=engine):
        self.session_factory = session_factory
        self.engine = database_engine

    async def initialize(self):
        async with self.engine.begin() as conn:
            await conn.run_sync(lambda sync: MinigameSession.__table__.create(sync, checkfirst=True))

    @staticmethod
    def _decode(row):
        value = json.loads(row.payload)
        if value.get("schema") == 4 and value.pop("move_encoding", None) == "compact-v1":
            value["moves"] = [{"move": move, "side": side, "user_id": uid}
                              for move, side, uid in value["moves"]]
        if (value.get("version") != row.version or value.get("bot_id") != row.bot_id
                or value.get("group_id") != row.group_id):
            raise SessionConflict("Invalid persisted session identity/version")
        return value

    async def load_all(self):
        async with await self.session_factory() as session:
            rows = (await session.execute(select(MinigameSession))).scalars().all()
            return [self._decode(row) for row in rows]

    async def load(self, bot_id, group_id):
        async with await self.session_factory() as session:
            row = await session.get(MinigameSession, (bot_id, group_id))
            return self._decode(row) if row else None

    async def save(self, state, expected_version):
        value = state
        if state.get("schema") == 4:
            value = dict(state, move_encoding="compact-v1",
                         moves=[[m["move"], m["side"], m["user_id"]] for m in state["moves"]])
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        if len(payload.encode("utf-8")) > 64000:
            raise ValueError("Game recovery record exceeds the safe TEXT storage budget")
        async with await self.session_factory() as session:
            if expected_version is None:
                session.add(MinigameSession(bot_id=state["bot_id"], group_id=state["group_id"],
                                           version=state["version"], payload=payload))
            else:
                result = await session.execute(update(MinigameSession).where(
                    MinigameSession.bot_id == state["bot_id"], MinigameSession.group_id == state["group_id"],
                    MinigameSession.version == expected_version).values(
                        version=state["version"], payload=payload, updated_at=datetime.now()))
                if result.rowcount != 1:
                    raise SessionConflict("Session changed concurrently")
            await session.commit()
