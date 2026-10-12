"""Additive Agent memory schema; imported before the shared metadata create_all."""
from datetime import datetime
from sqlalchemy import BigInteger, Boolean, DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.mysql import LONGTEXT
from .db import Base


class MemorySource(Base):
    __tablename__ = "agent_v2_sources"
    __table_args__ = (UniqueConstraint("group_id", "message_id", name="uq_av2_source_message"),
                      {"sqlite_autoincrement": True})
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(BigInteger, index=True)
    message_id: Mapped[int] = mapped_column(BigInteger)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    nickname: Mapped[str] = mapped_column(String(100), default="")
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True, default=datetime.now)


class MemoryJob(Base):
    __tablename__ = "agent_v2_jobs"
    group_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    checkpoint: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_run: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(String(120), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class MemoryRecord(Base):
    __tablename__ = "agent_v2_records"
    __table_args__ = (UniqueConstraint("group_id", "user_id", "kind", "fingerprint", name="uq_av2_record"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, default=0, index=True)
    kind: Mapped[str] = mapped_column(String(24), index=True)
    content: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(64))
    topic: Mapped[str] = mapped_column(String(100), default="")
    evidence: Mapped[str] = mapped_column(Text, default="[]")
    confidence: Mapped[float] = mapped_column(Float, default=0.7)
    protected: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(24), default="active", index=True)
    source: Mapped[str] = mapped_column(String(50), default="observed")
    version: Mapped[int] = mapped_column(Integer, default=1)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class MemoryPrivacy(Base):
    __tablename__ = "agent_v2_privacy"
    __table_args__ = (UniqueConstraint("group_id", "user_id", name="uq_av2_privacy"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(BigInteger, default=0, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    learning: Mapped[bool] = mapped_column(Boolean, default=True)
    style_learning: Mapped[bool] = mapped_column(Boolean, default=True)
    evaluation: Mapped[bool] = mapped_column(Boolean, default=True)
    purge_pending: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class MemoryTombstone(Base):
    __tablename__ = "agent_v2_tombstones"
    __table_args__ = (UniqueConstraint("group_id", "user_id", "fingerprint", name="uq_av2_tombstone"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class MemoryVector(Base):
    __tablename__ = "agent_v2_vectors"
    record_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    model: Mapped[str] = mapped_column(String(100))
    dimensions: Mapped[int] = mapped_column(Integer, default=768)
    version: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    values_json: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class MemoryBudget(Base):
    __tablename__ = "agent_v2_budgets"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    used: Mapped[int] = mapped_column(Integer, default=0)


class MemoryArchive(Base):
    __tablename__ = "agent_v2_archives"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    object_key: Mapped[str] = mapped_column(String(250), unique=True)
    checksum: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class MemoryReport(Base):
    __tablename__ = "agent_v2_reports"
    __table_args__ = (UniqueConstraint("group_id", "user_id", name="uq_av2_report"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    payload: Mapped[str] = mapped_column(Text().with_variant(LONGTEXT(), "mysql"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
