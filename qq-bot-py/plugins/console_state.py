"""Durable console settings, content-free telemetry, and Agent reply quotas."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import re
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import yaml
from sqlalchemy import BigInteger, Boolean, Date, DateTime, Integer, String, Text
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from . import db


SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")


def local_now() -> datetime:
    return datetime.now(SHANGHAI).replace(tzinfo=None)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def _read_config() -> dict:
    path = Path("config.yaml")
    if not path.exists():
        path = Path(__file__).resolve().parents[1] / "config.yaml"
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


class ConsoleSetting(db.Base):
    __tablename__ = "console_settings"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    values_json: Mapped[str] = mapped_column(Text, default="{}")
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=local_now)


class ConsoleEvent(db.Base):
    __tablename__ = "console_events"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    group_id: Mapped[int] = mapped_column(BigInteger, default=0, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, default=0, index=True)
    detail_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=local_now, index=True)


class ConsoleDailyStat(db.Base):
    __tablename__ = "console_daily_stats"
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    group_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    count: Mapped[int] = mapped_column(BigInteger, default=0)


class ConsoleRequest(db.Base):
    __tablename__ = "console_requests"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    group_id: Mapped[int] = mapped_column(BigInteger, default=0, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, default=0)
    provider: Mapped[str] = mapped_column(String(100), default="")
    model: Mapped[str] = mapped_column(String(160), default="")
    source: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(32), default="unknown")
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    request_id: Mapped[str] = mapped_column(String(100), default="")
    attempt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    queue_wait_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_chars: Mapped[int | None] = mapped_column(Integer, nullable=True)
    image_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payload_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=local_now, index=True)


class ConsoleAudit(db.Base):
    __tablename__ = "console_audits"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(100), default="admin")
    action: Mapped[str] = mapped_column(String(100))
    target: Mapped[str] = mapped_column(String(160))
    detail_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=local_now, index=True)


class ConsoleQuotaDay(db.Base):
    __tablename__ = "console_quota_days"
    group_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    count: Mapped[int] = mapped_column(Integer, default=0)
    reserved: Mapped[int] = mapped_column(Integer, default=0)


class ConsoleQuotaClaim(db.Base):
    __tablename__ = "console_quota_claims"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    group_id: Mapped[int] = mapped_column(BigInteger, index=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    explicit: Mapped[bool] = mapped_column(Boolean, default=False)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=local_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)


class ConsoleQuotaNotice(db.Base):
    __tablename__ = "console_quota_notices"
    group_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    threshold: Mapped[int] = mapped_column(Integer, primary_key=True)
    claim: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=local_now)


CONSOLE_TABLES = [
    ConsoleSetting.__table__, ConsoleEvent.__table__, ConsoleDailyStat.__table__,
    ConsoleRequest.__table__, ConsoleAudit.__table__, ConsoleQuotaDay.__table__,
    ConsoleQuotaClaim.__table__, ConsoleQuotaNotice.__table__,
]


class VersionConflict(ValueError):
    """The editor attempted to replace a newer committed settings revision."""


class _Store:
    def __init__(self, session_factory: Callable | None = None):
        self._session_factory = session_factory
        self._lock = asyncio.Lock()

    async def _session(self):
        return await (self._session_factory or db.get_session)()


def _defaults(config: dict) -> tuple[dict, dict[int, dict]]:
    agent = config.get("agent", {})
    group = agent.get("group", {})
    dev = config.get("dev_scope", {})
    ai = config.get("ai", {})
    tests = [int(value) for value in dev.get("allowed_groups", [])]
    values = {
        "agent_enabled": bool(agent.get("enabled", False)),
        "model": ai.get("model", ""),
        "decision_timeout_seconds": agent.get("decision_timeout_seconds", agent.get("timeout_seconds", 90)),
        "decision_concurrency": group.get("decision_concurrency", 2),
        "decision_context_chars": group.get("decision_context_chars", 30000),
        "decision_direct_context_chars": group.get("decision_direct_context_chars", 30000),
        "decision_recent_messages": group.get("decision_recent_messages", 300),
        "decision_direct_recent_messages": group.get("decision_direct_recent_messages", 300),
        "decision_images": group.get("decision_images", 3),
        "decision_avatars": group.get("decision_avatars", 1),
        "decision_backend_search": group.get("decision_backend_search", ai.get("enable_search", False)),
        "summary_timeout_seconds": agent.get("summary", {}).get("timeout_seconds", 180),
        "hourly_reply_soft_limit": group.get("hourly_reply_soft_limit", 30),
        "daily_reply_limit": 200,
        "development_mode": "at" if dev.get("at_only", False) else ("test" if dev.get("enabled", False) else "all"),
        "test_groups": tests,
        "quota_enforcement_enabled": True,
        "quota_enforcement_groups": tests,
    }
    seeds = {}
    overrides = group.get("group_overrides", {})
    for raw in agent.get("active_groups", []):
        gid = int(raw)
        old = overrides.get(gid, overrides.get(str(gid), {}))
        seeds[gid] = {
            "mode": old.get("mode", group.get("default_mode", "at")), "enabled": True,
            "daily_reply_limit": int(old.get("daily_reply_limit", group.get("daily_reply_limit", 200))),
        }
        if "hourly_reply_soft_limit" in old:
            seeds[gid]["hourly_reply_soft_limit"] = int(old["hourly_reply_soft_limit"])
    return values, seeds


INTEGER_BOUNDS = {
    "decision_timeout_seconds": (5, 600), "decision_concurrency": (1, 32),
    "decision_context_chars": (500, 100000), "decision_direct_context_chars": (500, 100000),
    "decision_recent_messages": (1, 500), "decision_direct_recent_messages": (1, 500),
    "decision_images": (0, 5), "decision_avatars": (0, 10),
    "summary_timeout_seconds": (10, 900), "hourly_reply_soft_limit": (0, 10000),
    "daily_reply_limit": (0, 100000),
}
GROUP_KEYS = {"mode", "enabled", "hourly_reply_soft_limit", "daily_reply_limit"}


def _validate_patch(patch: dict, allowed: set[str]) -> dict:
    if not isinstance(patch, dict):
        raise ValueError("Settings patch must be an object")
    result = {}
    for key, value in patch.items():
        if key not in allowed:
            raise ValueError(f"Unknown setting: {key}")
        if value is None:
            result[key] = None
        elif key in INTEGER_BOUNDS:
            low, high = INTEGER_BOUNDS[key]
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"{key} must be an integer in [{low}, {high}]")
            result[key] = value
        elif key in {"agent_enabled", "enabled", "decision_backend_search", "quota_enforcement_enabled"}:
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be a boolean")
            result[key] = value
        elif key in {"test_groups", "quota_enforcement_groups"}:
            if not isinstance(value, list) or len(value) > 10000:
                raise ValueError(f"{key} must be a group ID list")
            if any(isinstance(gid, bool) or not isinstance(gid, int) or gid <= 0 or gid > 2**63 - 1 for gid in value):
                raise ValueError(f"{key} contains an invalid group ID")
            result[key] = sorted(set(value))
        elif key in {"mode", "development_mode"}:
            options = {"off", "auto", "at"} if key == "mode" else {"all", "test", "at"}
            if value not in options:
                raise ValueError(f"Invalid {key}")
            result[key] = value
        elif key == "model":
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:/+\-]{1,160}", value):
                raise ValueError("Invalid model identifier")
            result[key] = value
    return result


class SettingsStore(_Store):
    def __init__(self, config: dict | None = None, session_factory: Callable | None = None):
        super().__init__(session_factory)
        self._defaults, self._seed_groups = _defaults(_read_config() if config is None else config)
        self._global: dict = {}
        self._groups: dict[int, dict] = copy.deepcopy(self._seed_groups)
        self._versions: dict[str, int] = {}
        self.loaded = False

    def global_values(self) -> dict:
        return {**copy.deepcopy(self._defaults), **copy.deepcopy(self._global), "version": self._versions.get("global", 0)}

    def group(self, group_id: int) -> dict:
        gid = int(group_id)
        global_values = self.global_values()
        overrides = copy.deepcopy(self._groups.get(gid, {}))
        yaml_enabled = gid in self._seed_groups
        values = {"mode": "at" if yaml_enabled else "off", "enabled": yaml_enabled, "hourly_reply_soft_limit": global_values["hourly_reply_soft_limit"], "daily_reply_limit": global_values["daily_reply_limit"]}
        values.update(overrides)
        values["enabled"] = values["mode"] != "off" and bool(values["enabled"])
        return {**values, "group_id": gid, "version": self._versions.get(f"group:{gid}", 0), "overrides": overrides, "inherited": sorted(GROUP_KEYS - overrides.keys())}

    def active_groups(self) -> set[int]:
        return {gid for gid in self._groups if self.group(gid)["enabled"]}

    def known_groups(self) -> set[int]:
        return set(self._groups)

    def groups(self) -> list[dict]:
        return [self.group(gid) for gid in sorted(self._groups)]

    async def load(self) -> None:
        async with self._lock:
            async with await self._session() as session:
                rows = (await session.execute(select(ConsoleSetting))).scalars().all()
                by_key = {row.key: row for row in rows}
                # Import old overrides once, without undoing later inheritance resets.
                if "seeded" not in by_key:
                    for gid, values in self._seed_groups.items():
                        key = f"group:{gid}"
                        if key not in by_key:
                            await _insert_ignore(session, ConsoleSetting, {"key": key, "values_json": _json(values), "version": 1, "updated_at": local_now()})
                    await _insert_ignore(session, ConsoleSetting, {"key": "seeded", "values_json": "{}", "version": 1, "updated_at": local_now()})
                    await session.commit()
                    rows = (await session.execute(select(ConsoleSetting))).scalars().all()
                globals_next, groups_next, versions_next = {}, {}, {}
                for row in rows:
                    versions_next[row.key] = row.version
                    if row.key == "global":
                        globals_next = json.loads(row.values_json)
                    elif row.key.startswith("group:"):
                        groups_next[int(row.key.split(":", 1)[1])] = json.loads(row.values_json)
                self._global, self._groups, self._versions = globals_next, groups_next, versions_next
                self.loaded = True

    async def update_global(self, patch: dict, actor: str = "admin", version: int | None = None) -> dict:
        checked = _validate_patch(patch, set(self._defaults))
        await self._update("global", checked, actor, version)
        return self.global_values()

    async def update_group(self, group_id: int, patch: dict, actor: str = "admin", version: int | None = None) -> dict:
        gid = int(group_id)
        if gid <= 0 or gid > 2**63 - 1:
            raise ValueError("Invalid group ID")
        checked = _validate_patch(patch, GROUP_KEYS)
        if checked.get("mode") is not None:
            if "enabled" in checked and checked["enabled"] is not None and checked["enabled"] != (checked["mode"] != "off"):
                raise ValueError("enabled and mode disagree")
            checked["enabled"] = checked["mode"] != "off"
        elif checked.get("enabled") is not None:
            checked["mode"] = (self.group(gid)["mode"] if self.group(gid)["mode"] != "off" else "at") if checked["enabled"] else "off"
        await self._update(f"group:{gid}", checked, actor, version)
        return self.group(gid)

    async def reset_global(self, actor: str = "admin", version: int | None = None) -> dict:
        return await self.update_global(dict.fromkeys(self._defaults), actor, version)

    async def reset_group(self, group_id: int, actor: str = "admin", version: int | None = None) -> dict:
        return await self.update_group(group_id, dict.fromkeys(GROUP_KEYS), actor, version)

    async def _update(self, key: str, patch: dict, actor: str, version: int | None) -> None:
        async with self._lock:
            async with await self._session() as session:
                row = (await session.execute(select(ConsoleSetting).where(ConsoleSetting.key == key).with_for_update())).scalar_one_or_none()
                actual_version = row.version if row else 0
                if version is not None and (isinstance(version, bool) or version != actual_version):
                    raise VersionConflict("Settings were changed by another editor; reload before saving")
                values = json.loads(row.values_json) if row else {}
                before = copy.deepcopy(values)
                for field, value in patch.items():
                    if value is None:
                        values.pop(field, None)
                    else:
                        values[field] = value
                new_version = actual_version + 1
                if row:
                    result = await session.execute(update(ConsoleSetting).where(ConsoleSetting.key == key, ConsoleSetting.version == actual_version).values(values_json=_json(values), version=new_version, updated_at=local_now()))
                    if result.rowcount != 1:
                        raise VersionConflict("Settings were changed by another editor")
                else:
                    session.add(ConsoleSetting(key=key, values_json=_json(values), version=new_version))
                changes = {field: {"before": before.get(field), "after": values.get(field)} for field in patch}
                _add_audit(session, "settings.update", key, {"fields": sorted(patch), "version": new_version, "changes": changes}, actor)
                try:
                    await session.commit()
                except IntegrityError as exc:
                    await session.rollback()
                    raise VersionConflict("Settings were changed by another editor") from exc
                # Never publish a candidate revision before its transaction commits.
                if key == "global":
                    self._global = values
                else:
                    self._groups[int(key.split(":", 1)[1])] = values
                self._versions[key] = new_version
                runtime = sys.modules.get("plugins.console_runtime")
                if self is SETTINGS and runtime is not None:
                    runtime.settings_changed()


async def _insert_ignore(session, model, values: dict) -> None:
    dialect = session.get_bind().dialect.name
    if dialect == "mysql":
        statement = mysql_insert(model).values(**values)
        first_key = next(iter(values))
        statement = statement.on_duplicate_key_update(**{first_key: getattr(model, first_key)})
    elif dialect == "sqlite":
        statement = sqlite_insert(model).values(**values).on_conflict_do_nothing()
    else:
        raise RuntimeError(f"Unsupported console database: {dialect}")
    await session.execute(statement)


EVENT_KINDS = {"incoming", "interaction", "direct_at", "decision_batch", "model_request", "reply_round", "tool", "outbound", "notice"}
_NUMERIC_METADATA = {"bot_id", "message_id", "source_message_id", "latency_ms", "duration_ms", "input_tokens", "output_tokens", "prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens", "message_count", "reply_messages", "tool_calls", "threshold", "http_status", "attempt", "queue_wait_ms", "input_chars", "image_count", "payload_bytes"}
_LABEL_METADATA = {"provider", "model", "source", "status", "tool_name", "action", "reason_code", "error_code", "persona_id", "claim_id", "request_id"}


def _safe_metadata(metadata: dict) -> dict:
    result = {}
    for key, value in metadata.items():
        if key in _NUMERIC_METADATA and (value is None or isinstance(value, int) and not isinstance(value, bool) and value >= 0):
            result[key] = value
        elif key in {"success", "explicit"} and isinstance(value, bool):
            result[key] = value
        elif key in _LABEL_METADATA and isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:/+\-]{0,160}", value):
            result[key] = value
    return result


def _safe_audit(detail: Any) -> dict:
    # Audit changes, identifiers, and outcomes, never edited memory or chat text.
    if not isinstance(detail, dict):
        return {"redacted": True}
    allowed = {"fields", "version", "ids", "count", "success", "status", "mode", "deleted", "hard", "group_id", "user_id", "record_id", "kind", "reason_code", "from", "to", "source_floor", "changes"}
    safe = {}
    for key, value in detail.items():
        if key not in allowed:
            continue
        if isinstance(value, (bool, int)) or value is None:
            safe[key] = value
        elif isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:/+\-]{0,160}", value):
            safe[key] = value
        elif key in {"fields", "ids"} and isinstance(value, list):
            safe[key] = [item for item in value[:200] if isinstance(item, int) or isinstance(item, str) and re.fullmatch(r"[A-Za-z0-9_.:\-]{1,100}", item)]
        elif key == "changes" and isinstance(value, dict):
            changes = {}
            allowed_settings = set(INTEGER_BOUNDS) | GROUP_KEYS | {"model", "agent_enabled", "decision_backend_search", "test_groups", "development_mode", "quota_enforcement_enabled", "quota_enforcement_groups"}
            for field, revisions in value.items():
                if field not in allowed_settings or not isinstance(revisions, dict):
                    continue
                try:
                    changes[field] = {revision: _validate_patch({field: item}, allowed_settings)[field] for revision, item in revisions.items() if revision in {"before", "after"}}
                except ValueError:
                    continue
            safe[key] = changes
    return safe


def _add_audit(session, action: str, target: str, detail: Any, actor: str) -> None:
    session.add(ConsoleAudit(actor=str(actor)[:100], action=str(action)[:100], target=str(target)[:160], detail_json=_json(_safe_audit(detail))))


def _date_range(date_from=None, date_to=None) -> tuple[date, date]:
    def parse(value):
        if value is None:
            return local_now().date()
        if isinstance(value, datetime):
            return value.astimezone(SHANGHAI).date() if value.tzinfo else value.date()
        if isinstance(value, date):
            return value
        return date.fromisoformat(str(value))
    start, end = parse(date_from), parse(date_to)
    if start > end or (end - start).days > 365:
        raise ValueError("Date range must be ordered and at most 366 days")
    return start, end


class StatsStore(_Store):
    async def health(self, *, heartbeat: bool = False, error: str = "", dropped: int = 0) -> dict:
        now = local_now()
        async with await self._session() as session:
            row = await session.get(ConsoleSetting, "telemetry_health")
            saved = json.loads(row.values_json) if row else {}
            previous = saved.get("last_heartbeat")
            gap = bool(previous and now - datetime.fromisoformat(previous) > timedelta(minutes=3))
            if heartbeat:
                saved = {"last_heartbeat": now.isoformat(), "gap_detected": bool(saved.get("gap_detected") or gap),
                         "error_recorded": error or saved.get("error_recorded"), "dropped_at_least": max(dropped, saved.get("dropped_at_least", 0))}
                if row is None:
                    row = ConsoleSetting(key="telemetry_health", version=1)
                    session.add(row)
                row.values_json = _json(saved)
                row.updated_at = now
                await session.commit()
            return {**saved, "gap_detected": bool(saved.get("gap_detected") or gap)}

    async def _record(self, session, kind: str, group_id: int, user_id: int, event_key: str | None, metadata: dict, now: datetime) -> bool:
        clean = _safe_metadata(metadata)
        if kind not in EVENT_KINDS:
            raise ValueError("Unknown event kind")
        if event_key is None and kind in {"incoming", "interaction", "direct_at"} and clean.get("message_id"):
            event_key = str(clean["message_id"])
        event_id = hashlib.sha256(f"{kind}:{clean.get('bot_id', 0)}:{group_id}:{event_key}".encode()).hexdigest() if event_key is not None else uuid.uuid4().hex
        if await session.get(ConsoleEvent, event_id):
            return False
        session.add(ConsoleEvent(id=event_id, kind=kind, group_id=group_id, user_id=user_id, detail_json=_json(clean), created_at=now))
        await session.flush()
        values = {"day": now.date(), "group_id": group_id, "user_id": user_id, "kind": kind, "count": 1}
        dialect = session.get_bind().dialect.name
        if dialect == "mysql":
            statement = mysql_insert(ConsoleDailyStat).values(**values).on_duplicate_key_update(count=ConsoleDailyStat.count + 1)
        elif dialect == "sqlite":
            statement = sqlite_insert(ConsoleDailyStat).values(**values).on_conflict_do_update(index_elements=["day", "group_id", "user_id", "kind"], set_={"count": ConsoleDailyStat.count + 1})
        else:
            raise RuntimeError(f"Unsupported console database: {dialect}")
        await session.execute(statement)
        if kind == "model_request":
            status = clean.get("status", "ok" if clean.get("success") is True else "error" if clean.get("success") is False else "unknown")
            session.add(ConsoleRequest(id=event_id, group_id=group_id, user_id=user_id,
                provider=clean.get("provider", ""), model=clean.get("model", ""), source=clean.get("source", ""), status=status,
                latency_ms=clean.get("latency_ms", clean.get("duration_ms")),
                input_tokens=clean.get("input_tokens", clean.get("prompt_tokens")),
                output_tokens=clean.get("output_tokens", clean.get("completion_tokens")), total_tokens=clean.get("total_tokens"),
                request_id=clean.get("request_id", ""), attempt=clean.get("attempt"), queue_wait_ms=clean.get("queue_wait_ms"),
                input_chars=clean.get("input_chars"), image_count=clean.get("image_count"), payload_bytes=clean.get("payload_bytes"), created_at=now))
        return True

    async def record_event(self, kind: str, group_id: int, user_id: int = 0, event_key: str | None = None, **metadata) -> bool:
        async with self._lock:
            async with await self._session() as session:
                try:
                    changed = await self._record(session, kind, int(group_id), int(user_id), event_key, metadata, local_now())
                    await session.commit()
                    return changed
                except IntegrityError:
                    await session.rollback()
                    return False

    async def audit(self, action: str, target: str, detail: Any, actor: str = "admin") -> None:
        async with await self._session() as session:
            _add_audit(session, action, target, detail, actor)
            await session.commit()

    async def summary(self, date_from=None, date_to=None, group_id: int | None = None) -> dict:
        start, end = _date_range(date_from, date_to)
        conditions = [ConsoleDailyStat.day >= start, ConsoleDailyStat.day <= end]
        if group_id is not None:
            conditions.append(ConsoleDailyStat.group_id == int(group_id))
        async with await self._session() as session:
            totals = {kind: 0 for kind in sorted(EVENT_KINDS)}
            for kind, count in (await session.execute(select(ConsoleDailyStat.kind, func.sum(ConsoleDailyStat.count)).where(*conditions).group_by(ConsoleDailyStat.kind))).all():
                totals[kind] = int(count)
            active = conditions + [ConsoleDailyStat.kind == "incoming", ConsoleDailyStat.user_id > 0]
            users = (await session.execute(select(func.count(func.distinct(ConsoleDailyStat.user_id))).where(*active))).scalar() or 0
            groups = (await session.execute(select(func.count(func.distinct(ConsoleDailyStat.group_id))).where(*active))).scalar() or 0
            daily_rows = (await session.execute(select(ConsoleDailyStat.day, ConsoleDailyStat.kind, func.sum(ConsoleDailyStat.count)).where(*conditions).group_by(ConsoleDailyStat.day, ConsoleDailyStat.kind).order_by(ConsoleDailyStat.day))).all()
            daily = {}
            for day, kind, count in daily_rows:
                daily.setdefault(day.isoformat(), {"day": day.isoformat(), **dict.fromkeys(EVENT_KINDS, 0)})[kind] = int(count)
            seed = await session.get(ConsoleSetting, "seeded")
            since = seed.updated_at if seed else None
            request_conditions = [ConsoleRequest.created_at >= datetime.combine(start, datetime.min.time()),
                                  ConsoleRequest.created_at < datetime.combine(end + timedelta(days=1), datetime.min.time())]
            if group_id is not None:
                request_conditions.append(ConsoleRequest.group_id == int(group_id))
            requests = (await session.execute(select(ConsoleRequest.status, ConsoleRequest.latency_ms, ConsoleRequest.created_at).where(*request_conditions))).all()
            latencies = [row.latency_ms for row in requests if row.latency_ms is not None]
            latency_days = {}
            for row in requests:
                if row.latency_ms is not None:
                    latency_days.setdefault(row.created_at.date().isoformat(), []).append(row.latency_ms)
            metrics = {"count": len(requests), "timeout_rate": sum(row.status == "timeout" for row in requests) / len(requests) if requests else None,
                       "average_ms": sum(latencies) / len(latencies) if latencies else None,
                       "daily": [{"day": day, "latency_ms": round(sum(values) / len(values))} for day, values in sorted(latency_days.items())]}
        return {"date_from": start.isoformat(), "date_to": end.isoformat(), "timezone": "Asia/Shanghai", "group_id": group_id, "totals": totals,
                "statistics_since": since.replace(tzinfo=SHANGHAI).isoformat() if since else None,
                "coverage_incomplete": since is None or start <= since.date(), "request_metrics": metrics,
                "active_users": int(users), "active_groups": int(groups), "daily": list(daily.values())}

    async def activity(self, date_from=None, date_to=None, group_id: int | None = None, limit: int = 100, offset: int = 0, **_) -> dict:
        start, end = _date_range(date_from, date_to)
        conditions = [ConsoleDailyStat.day >= start, ConsoleDailyStat.day <= end, ConsoleDailyStat.user_id > 0]
        if group_id is not None:
            conditions.append(ConsoleDailyStat.group_id == int(group_id))
        async with await self._session() as session:
            rows = (await session.execute(select(ConsoleDailyStat.group_id, ConsoleDailyStat.user_id, ConsoleDailyStat.kind, func.sum(ConsoleDailyStat.count)).where(*conditions).group_by(ConsoleDailyStat.group_id, ConsoleDailyStat.user_id, ConsoleDailyStat.kind))).all()
        pairs = {}
        for gid, uid, kind, count in rows:
            pair = pairs.setdefault((gid, uid), {"group_id": gid, "user_id": uid, **dict.fromkeys(EVENT_KINDS, 0)})
            pair[kind] = int(count)
        ordered = sorted(pairs.values(), key=lambda item: (-item["incoming"], -item["interaction"], item["group_id"], item["user_id"]))
        limit, offset = max(1, min(int(limit), 500)), max(0, int(offset))
        return {"items": ordered[offset:offset + limit], "total": len(ordered), "limit": limit, "offset": offset, "date_from": start.isoformat(), "date_to": end.isoformat()}

    async def requests(self, date_from=None, date_to=None, group_id: int | None = None, limit: int = 100, offset: int = 0, status: str | None = None, source: str | None = None, **_) -> dict:
        filters = {key: value for key, value in {"status": status, "source": source}.items() if value}
        return await self._page(ConsoleRequest, date_from, date_to, group_id, limit, offset, filters)

    async def audits(self, date_from=None, date_to=None, group_id: int | None = None, limit: int = 100, offset: int = 0, action: str | None = None, **_) -> dict:
        return await self._page(ConsoleAudit, date_from, date_to, None, limit, offset, {"action": action} if action else None)

    async def _page(self, model, date_from, date_to, group_id, limit, offset, extra: dict | None = None) -> dict:
        start, end = _date_range(date_from, date_to)
        conditions = [model.created_at >= datetime.combine(start, datetime.min.time()), model.created_at < datetime.combine(end + timedelta(days=1), datetime.min.time())]
        if group_id is not None:
            conditions.append(model.group_id == int(group_id))
        for field, value in (extra or {}).items():
            if value is not None and hasattr(model, field):
                conditions.append(getattr(model, field) == str(value))
        limit, offset = max(1, min(int(limit), 500)), max(0, int(offset))
        async with await self._session() as session:
            total = (await session.execute(select(func.count()).select_from(model).where(*conditions))).scalar() or 0
            rows = (await session.execute(select(model).where(*conditions).order_by(model.created_at.desc(), model.id.desc()).offset(offset).limit(limit))).scalars().all()
            items = []
            for row in rows:
                item = {column.name: getattr(row, column.name) for column in model.__table__.columns}
                item["created_at"] = item["created_at"].replace(tzinfo=SHANGHAI).isoformat()
                if "detail_json" in item:
                    item["detail"] = json.loads(item.pop("detail_json"))
                items.append(item)
        return {"items": items, "total": int(total), "limit": limit, "offset": offset}

    async def prune(self) -> dict:
        now = local_now()
        counts = {}
        async with await self._session() as session:
            for model, condition in [
                (ConsoleEvent, ConsoleEvent.created_at < now - timedelta(days=30)),
                (ConsoleRequest, ConsoleRequest.created_at < now - timedelta(days=30)),
                (ConsoleAudit, ConsoleAudit.created_at < now - timedelta(days=365)),
                (ConsoleDailyStat, ConsoleDailyStat.day < now.date() - timedelta(days=365)),
                (ConsoleQuotaDay, ConsoleQuotaDay.day < now.date() - timedelta(days=365)),
                (ConsoleQuotaClaim, ConsoleQuotaClaim.day < now.date() - timedelta(days=30)),
                (ConsoleQuotaNotice, ConsoleQuotaNotice.day < now.date() - timedelta(days=365)),
            ]:
                result = await session.execute(delete(model).where(condition))
                counts[model.__tablename__] = result.rowcount
            await session.commit()
        return counts


class QuotaStore(_Store):
    def __init__(self, settings: SettingsStore, stats: StatsStore, session_factory: Callable | None = None):
        super().__init__(session_factory)
        self.settings, self.stats = settings, stats

    def _enforced(self, group_id: int) -> bool:
        values = self.settings.global_values()
        return values["quota_enforcement_enabled"] and group_id in values["quota_enforcement_groups"]

    def _result(self, group_id: int, day: date, count: int, reserved: int, hour_count: int, uncertain: int) -> dict:
        limit = int(self.settings.group(group_id)["daily_reply_limit"])
        hard = math.ceil(limit * 1.5) if limit else 0
        stage = "unlimited" if not limit else "hard_limit" if count >= hard else "at_only" if count >= limit else "normal"
        return {"group_id": group_id, "day": day.isoformat(), "count": count, "limit": limit, "hard_limit": hard, "stage": stage, "hour_count": hour_count, "reserved": reserved, "uncertain": uncertain, "enforced": self._enforced(group_id)}

    async def _day_row(self, session, group_id: int, day: date):
        await _insert_ignore(session, ConsoleQuotaDay, {"group_id": group_id, "day": day, "count": 0, "reserved": 0})
        return (await session.execute(select(ConsoleQuotaDay).where(ConsoleQuotaDay.group_id == group_id, ConsoleQuotaDay.day == day).with_for_update())).scalar_one()

    async def status(self, group_id: int) -> dict:
        gid, now = int(group_id), local_now()
        async with await self._session() as session:
            row = await session.get(ConsoleQuotaDay, (gid, now.date()))
            hour = (await session.execute(select(func.count()).select_from(ConsoleQuotaClaim).where(ConsoleQuotaClaim.group_id == gid, ConsoleQuotaClaim.status == "counted", ConsoleQuotaClaim.finished_at >= now - timedelta(hours=1)))).scalar() or 0
            uncertain = (await session.execute(select(func.count()).select_from(ConsoleQuotaClaim).where(ConsoleQuotaClaim.group_id == gid, ConsoleQuotaClaim.day == now.date(), ConsoleQuotaClaim.status == "uncertain"))).scalar() or 0
            return self._result(gid, now.date(), row.count if row else 0, row.reserved if row else 0, int(hour), int(uncertain))

    async def reserve(self, group_id: int, explicit: bool) -> str | None:
        gid, now = int(group_id), local_now()
        async with self._lock:
            async with await self._session() as session:
                row = await self._day_row(session, gid, now.date())
                limit = int(self.settings.group(gid)["daily_reply_limit"])
                boundary = math.ceil(limit * 1.5) if explicit else limit
                if self._enforced(gid) and limit and row.count + row.reserved >= boundary:
                    await session.rollback()
                    return None
                claim = uuid.uuid4().hex
                row.reserved += 1
                session.add(ConsoleQuotaClaim(id=claim, group_id=gid, day=now.date(), explicit=bool(explicit), created_at=now))
                await session.commit()
                return claim

    async def finish(self, claim: str, success: bool, user_id: int = 0, source_message_id: int = 0, message_count: int = 1) -> bool:
        async with self._lock:
            async with await self._session() as session:
                pending = (await session.execute(select(ConsoleQuotaClaim).where(ConsoleQuotaClaim.id == str(claim)).with_for_update())).scalar_one_or_none()
                if pending is None or pending.status != "pending":
                    return False
                row = await self._day_row(session, pending.group_id, pending.day)
                row.reserved = max(0, row.reserved - 1)
                counted = bool(success) and int(message_count) > 0
                pending.status = "counted" if counted else "failed"
                pending.message_count = max(0, int(message_count)) if counted else 0
                pending.finished_at = local_now()
                if counted:
                    if pending.finished_at.date() != pending.day:
                        pending.day = pending.finished_at.date()
                        row = await self._day_row(session, pending.group_id, pending.day)
                    row.count += 1
                    await self.stats._record(session, "reply_round", pending.group_id, int(user_id), claim, {"source_message_id": int(source_message_id), "message_count": int(message_count), "explicit": pending.explicit, "claim_id": claim}, pending.finished_at)
                await session.commit()
                return counted

    async def recover_pending(self) -> int:
        async with await self._session() as session:
            result = await session.execute(update(ConsoleQuotaClaim).where(ConsoleQuotaClaim.status == "pending").values(status="uncertain"))
            await session.execute(update(ConsoleQuotaNotice).where(ConsoleQuotaNotice.status == "pending").values(status="uncertain"))
            await session.commit()
            return result.rowcount

    async def uncertain(self, claim: str) -> bool:
        """Retain the reservation when transport cannot confirm send outcome."""
        async with self._lock:
            async with await self._session() as session:
                result = await session.execute(update(ConsoleQuotaClaim).where(ConsoleQuotaClaim.id == str(claim), ConsoleQuotaClaim.status == "pending").values(status="uncertain"))
                await session.commit()
                return result.rowcount == 1

    async def claim_notice(self, group_id: int) -> dict | None:
        gid, now = int(group_id), local_now()
        limit = int(self.settings.group(gid)["daily_reply_limit"])
        if not limit or not self._enforced(gid):
            return None
        async with self._lock:
            async with await self._session() as session:
                row = await self._day_row(session, gid, now.date())
                thresholds = [percent for percent in (50, 75, 100, 150) if row.count >= math.ceil(limit * percent / 100)]
                if not thresholds:
                    return None
                threshold = max(thresholds)
                highest = (await session.execute(select(func.max(ConsoleQuotaNotice.threshold)).where(ConsoleQuotaNotice.group_id == gid, ConsoleQuotaNotice.day == now.date()))).scalar() or 0
                if highest >= threshold:
                    return None
                claim = uuid.uuid4().hex
                session.add(ConsoleQuotaNotice(group_id=gid, day=now.date(), threshold=threshold, claim=claim))
                await session.commit()
                return {"claim": claim, "group_id": gid, "day": now.date().isoformat(), "threshold": threshold, "count": row.count, "limit": limit, "hard_limit": math.ceil(limit * 1.5)}

    async def finish_notice(self, claim: str | dict, success: bool = True) -> None:
        token = claim["claim"] if isinstance(claim, dict) else claim
        async with self._lock:
            async with await self._session() as session:
                row = (await session.execute(select(ConsoleQuotaNotice).where(ConsoleQuotaNotice.claim == token).with_for_update())).scalar_one_or_none()
                if row is None or row.status != "pending":
                    return
                if success:
                    row.status = "sent"
                    await self.stats._record(session, "notice", row.group_id, 0, token, {"threshold": row.threshold}, local_now())
                else:
                    await session.delete(row)
                await session.commit()

    async def release_notice(self, claim: str | dict) -> None:
        await self.finish_notice(claim, False)

    async def release(self, claim: str) -> None:
        """Release a confirmed unsent reply; notices use release_notice instead."""
        await self.finish(claim, False, message_count=0)


SETTINGS = SettingsStore()
STATS = StatsStore()
QUOTA = QuotaStore(SETTINGS, STATS)


async def initialize_console() -> None:
    """Create only additive console tables; startup must fail closed on errors."""
    from .console_services import CONSOLE_SERVICE_TABLES
    async with db.engine.begin() as connection:
        await connection.run_sync(lambda sync: db.Base.metadata.create_all(
            sync, tables=CONSOLE_TABLES + CONSOLE_SERVICE_TABLES
        ))
    await SETTINGS.load()
    await QUOTA.recover_pending()
    await STATS.prune()
    await STATS.health(heartbeat=True)
