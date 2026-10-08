"""Bounded administrative operations for personas, memory and meme metadata."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import math
import os
import re
import sys
import tempfile
import warnings
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException
from sqlalchemy import DateTime, String, Text, func, select
from sqlalchemy.orm import Mapped, mapped_column

from . import db

EMOTIONS = ("happy", "sad", "angry", "surprised", "funny", "cool", "disgusted",
            "confused", "curious", "calm", "shy", "smug", "neutral")
MEME_STATUSES = ("active", "disabled", "deleted", "missing")
MEME_IMAGE_MAX_BYTES = 20 * 1024 * 1024
FACT_CATEGORIES = {"identity", "preference", "habit", "project", "relationship", "general"}
MEMORY_WRITE_LOCK = asyncio.Lock()


class ConsoleMemoryProtection(db.Base):
    """Semantic tombstones also protect deleted rows from automatic recreation."""

    __tablename__ = "console_memory_protections"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(24), index=True)
    identity_json: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(100), default="admin")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class ConsolePersonaRevision(db.Base):
    __tablename__ = "console_persona_revisions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[str] = mapped_column(String(64))
    content: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(100), default="admin")
    reason: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


CONSOLE_SERVICE_TABLES = [ConsoleMemoryProtection.__table__, ConsolePersonaRevision.__table__]


def _protection_key(kind: str, scope: str = "group", group_id: int = 0,
                    user_id: int = 0, identity: str = "", persona_id: str = "") -> tuple[str, str]:
    values = [kind, scope, int(group_id), int(user_id), str(identity), str(persona_id)]
    encoded = json.dumps(values, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode()).hexdigest(), encoded


async def is_memory_protected(session: Any, kind: str, scope: str = "group", group_id: int = 0,
                              user_id: int = 0, identity: str = "", persona_id: str = "") -> bool:
    key, _ = _protection_key(kind, scope, group_id, user_id, identity, persona_id)
    return await session.get(ConsoleMemoryProtection, key) is not None


async def protect_memory(session: Any, kind: str, scope: str = "group", group_id: int = 0,
                         user_id: int = 0, identity: str = "", persona_id: str = "", actor: str = "admin") -> None:
    key, encoded = _protection_key(kind, scope, group_id, user_id, identity, persona_id)
    if await session.get(ConsoleMemoryProtection, key) is None:
        session.add(ConsoleMemoryProtection(key=key, kind=kind, identity_json=encoded, actor=actor))


def _fingerprint(value: str) -> str:
    normalized = re.sub(r"[\s\W_]+", "", value.casefold())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _version(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _row(row: Any, exclude: set[str] | None = None) -> dict:
    result = {}
    for column in row.__table__.columns:
        if exclude and column.name in exclude:
            continue
        value = getattr(row, column.name)
        result[column.name] = value.isoformat() if isinstance(value, (date, datetime)) else value
    return result


def _only(payload: dict, fields: set[str]) -> None:
    if not isinstance(payload, dict) or set(payload) - fields:
        raise HTTPException(422, "Unknown or invalid fields")


def _integer(value: Any, name: str, low: int = 1, high: int = 2**63 - 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise HTTPException(422, f"Invalid {name}")
    return value


def _text(value: Any, name: str, maximum: int, required: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        raise HTTPException(422, f"Invalid {name}")
    return value.strip()


def _audit(session: Any, action: str, target: str, detail: dict, actor: str) -> None:
    from .console_state import ConsoleAudit
    session.add(ConsoleAudit(action=action, target=target, actor=actor,
                             detail_json=json.dumps(detail, ensure_ascii=True)))


class ConsoleServices:
    def __init__(self, session_factory: Callable | None = None, config: dict | None = None,
                 persona_manager: Any = None, minio_client: Any = None, redis_client: Any = None,
                 backup_root: Path | None = None):
        self.session_factory = session_factory or db.get_session
        if config is None:
            import yaml
            path = Path("config.yaml")
            if not path.exists():
                path = Path(__file__).resolve().parents[1] / "config.yaml"
            config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        self.config = config
        self.persona_manager = persona_manager
        self.minio = minio_client
        self.redis = redis_client
        self.bucket = str(((config.get("meme") or {}).get("minio") or {}).get("bucket", "memes"))
        self.backup_root = backup_root or Path(__file__).resolve().parents[1] / "data" / "console-persona-backups"
        self._persona_lock = asyncio.Lock()
        self._image_slots = asyncio.Semaphore(4)

    def _personas(self):
        if self.persona_manager is None:
            from . import persona_manager
            self.persona_manager = persona_manager
        return self.persona_manager

    def _documents(self) -> dict[str, tuple[Path, dict]]:
        pm = self._personas()
        discovered = {}
        roots = [("shared", Path(pm._SHARED_DIR)), ("legacy", Path(pm._LEGACY_DIR))]
        profiles = Path(pm._PROFILE_DIR)
        if profiles.is_dir():
            resolved_profiles = profiles.resolve()
            roots += [(directory.name, directory) for directory in profiles.iterdir()
                      if directory.is_dir() and directory.resolve().is_relative_to(resolved_profiles)]
        for persona_id, root in roots:
            if not root.is_dir():
                continue
            if persona_id == "legacy" and (profiles / "rin").is_dir():
                continue
            resolved_root = root.resolve()
            for path in sorted(root.glob("*.md")):
                resolved = path.resolve()
                if path.name.startswith("_") or not resolved.is_relative_to(resolved_root) or not resolved.is_file():
                    continue
                label = f"{persona_id}/{path.name}"
                document_id = hashlib.sha256(label.encode()).hexdigest()[:24]
                discovered[document_id] = (resolved, {"id": document_id, "persona_id": "rin" if persona_id == "legacy" else persona_id,
                                                      "name": path.name, "path": label, "shared": persona_id == "shared"})
        return discovered

    async def personas(self) -> dict:
        pm = self._personas()
        docs = self._documents()
        return {"active_id": pm.get_active_persona_id(), "items": [
            {"persona_id": profile.persona_id, "name": profile.name,
             "documents": [meta for _, meta in docs.values() if meta["persona_id"] in {profile.persona_id, "shared"}]}
            for profile in pm.list_personas()
        ]}

    def _document(self, document_id: str) -> tuple[Path, dict]:
        entry = self._documents().get(document_id)
        if entry is None:
            raise HTTPException(404, "Document not found")
        return entry

    async def document(self, document_id: str) -> dict:
        path, metadata = self._document(document_id)
        content = await asyncio.to_thread(path.read_text, encoding="utf-8")
        return {**metadata, "content": content, "version": _version(content)}

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp") as handle:
                temporary = Path(handle.name)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    def _backup(self, document_id: str, content: str) -> None:
        self.backup_root.mkdir(parents=True, exist_ok=True)
        path = self.backup_root / f"{document_id}-{_version(content)}.md"
        if not path.exists():
            self._atomic_write(path, content)

    async def save_document(self, document_id: str, payload: dict, actor: str) -> dict:
        _only(payload, {"content", "version", "reason"})
        content = payload.get("content")
        if not isinstance(content, str) or len(content.encode("utf-8")) > 256000:
            raise HTTPException(422, "Document must be UTF-8 text under 256 KB")
        reason = _text(payload.get("reason", ""), "reason", 300)
        async with self._persona_lock:
            path, metadata = self._document(document_id)
            previous = await asyncio.to_thread(path.read_text, encoding="utf-8")
            if payload.get("version") != _version(previous):
                raise HTTPException(409, "Document changed; reload before saving")
            await asyncio.to_thread(self._backup, document_id, previous)
            changed = False
            async with await self.session_factory() as session:
                try:
                    for text in (previous, content):
                        revision_id = f"{document_id}:{_version(text)[:32]}"
                        if await session.get(ConsolePersonaRevision, revision_id) is None:
                            session.add(ConsolePersonaRevision(id=revision_id, document_id=document_id,
                                        version=_version(text), content=text, actor=actor, reason=reason))
                    _audit(session, "persona.document.save", document_id,
                           {"before": _version(previous), "after": _version(content), "reason": reason}, actor)
                    await session.flush()
                    await asyncio.to_thread(self._atomic_write, path, content)
                    changed = True
                    await session.commit()
                except Exception:
                    await session.rollback()
                    if changed:
                        await asyncio.to_thread(self._atomic_write, path, previous)
                    raise
            self._personas().reload_profiles()
            return {**metadata, "content": content, "version": _version(content), "ok": True}

    async def revisions(self, document_id: str) -> dict:
        self._document(document_id)
        async with await self.session_factory() as session:
            rows = (await session.execute(select(ConsolePersonaRevision).where(ConsolePersonaRevision.document_id == document_id)
                    .order_by(ConsolePersonaRevision.created_at.desc()).limit(100))).scalars().all()
            return {"items": [_row(row, {"content"}) for row in rows], "total": len(rows)}

    async def restore_document(self, document_id: str, revision_id: str, version: str, actor: str) -> dict:
        self._document(document_id)
        async with await self.session_factory() as session:
            row = await session.get(ConsolePersonaRevision, revision_id)
            if row is None or row.document_id != document_id:
                raise HTTPException(404, "Revision not found")
            content = row.content
        return await self.save_document(document_id, {"content": content, "version": version, "reason": "Restore revision"}, actor)

    async def switch_persona(self, persona_id: str, reason: str, actor: str) -> dict:
        pm = self._personas()
        if persona_id not in {profile.persona_id for profile in pm.list_personas()}:
            raise HTTPException(422, "Unknown persona")
        _text(reason, "reason", 300, required=True)
        result = await pm.switch_persona(persona_id, actor_user_id=0, note=f"console:{actor} {reason}"[:500])
        if pm.get_active_persona_id() != persona_id:
            raise HTTPException(503, "Persona switch could not be persisted")
        if hasattr(pm, "_read_persisted_persona_id") and pm._read_persisted_persona_id() != persona_id:
            raise HTTPException(503, {"message": "Persona changed in memory but its restart state could not be saved",
                                      "active_id": persona_id, "persisted": False})
        async with await self.session_factory() as session:
            _audit(session, "persona.switch", persona_id, {"reason": reason}, actor)
            await session.commit()
        return {"ok": True, "active_id": persona_id, "message": result}

    def _remote_clients(self) -> None:
        if self.minio is None:
            from minio import Minio
            import urllib3
            cfg = ((self.config.get("meme") or {}).get("minio") or {})
            if not cfg.get("endpoint"):
                raise HTTPException(503, "Meme storage is not configured")
            self.minio = Minio(
                cfg["endpoint"], access_key=cfg.get("access_key"), secret_key=cfg.get("secret_key"),
                secure=bool(cfg.get("secure", False)),
                http_client=urllib3.PoolManager(timeout=urllib3.Timeout(connect=3, read=15),
                                               retries=urllib3.Retry(total=1)),
            )
        if self.redis is None:
            import redis
            cfg = self.config.get("redis") or {}
            self.redis = redis.Redis(host=cfg.get("host", "127.0.0.1"), port=int(cfg.get("port", 6379)),
                                     db=int(cfg.get("db", 0)), password=cfg.get("password") or None,
                                     decode_responses=True, socket_timeout=3, socket_connect_timeout=3)

    def _sync_meme(self, item: Any) -> None:
        self._remote_clients()
        pipeline = self.redis.pipeline(transaction=True)
        pipeline.srem("meme:pool", item.object_name)
        for emotion in EMOTIONS:
            pipeline.srem(f"meme:emotion:{emotion}", item.object_name)
        pipeline.delete(f"meme:tags:{item.object_name}")
        if item.status == "active":
            pipeline.sadd("meme:pool", item.object_name)
            if item.emotion in EMOTIONS:
                pipeline.set(f"meme:tags:{item.object_name}", item.emotion)
                pipeline.sadd(f"meme:emotion:{item.emotion}", item.object_name)
        pipeline.execute()

    async def memes(self, emotion: str = "", status: str = "active", q: str = "", offset: int = 0, limit: int = 24) -> dict:
        if status and status not in MEME_STATUSES:
            raise HTTPException(422, "Invalid status")
        if emotion and emotion not in (*EMOTIONS, "unclassified"):
            raise HTTPException(422, "Invalid emotion")
        filters = [db.MemeItem.bucket == self.bucket]
        if status:
            filters.append(db.MemeItem.status == status)
        if emotion:
            filters.append(db.MemeItem.emotion == ("" if emotion == "unclassified" else emotion))
        if q:
            filters.append(db.MemeItem.object_name.contains(q[:200], autoescape=True))
        async with await self.session_factory() as session:
            total = (await session.execute(select(func.count()).select_from(db.MemeItem).where(*filters))).scalar() or 0
            rows = (await session.execute(select(db.MemeItem).where(*filters).order_by(db.MemeItem.id.desc()).offset(offset).limit(limit))).scalars().all()
            items = [_row(row) for row in rows]
        for item in items:
            item["url"] = f"/api/admin/memes/{item['id']}/image"
        return {"items": items, "total": total, "emotions": list(EMOTIONS), "statuses": list(MEME_STATUSES)}

    def _read_meme_image(self, object_name: str) -> tuple[bytes, str]:
        from PIL import Image

        self._remote_clients()
        response = None
        try:
            response = self.minio.get_object(self.bucket, object_name)
            data = response.read(MEME_IMAGE_MAX_BYTES + 1)
            if len(data) > MEME_IMAGE_MAX_BYTES:
                raise HTTPException(413, "Meme image exceeds the preview size limit")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(502, "Meme image is unavailable from storage") from None
        finally:
            if response is not None:
                try:
                    response.close()
                finally:
                    response.release_conn()
        # Do not trust object metadata: HTML/SVG served from an authenticated
        # same-origin route would otherwise create an active-content surface.
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as picture:
                    mime = {"JPEG": "image/jpeg", "PNG": "image/png", "GIF": "image/gif",
                            "WEBP": "image/webp", "BMP": "image/bmp", "TIFF": "image/tiff",
                            "AVIF": "image/avif"}.get(picture.format)
                    if mime is None:
                        raise ValueError("Unsupported image format")
                    picture.verify()
        except Exception:
            raise HTTPException(415, "Stored object is not a supported raster image") from None
        return data, mime

    async def meme_image(self, item_id: int) -> tuple[bytes, str]:
        async with await self.session_factory() as session:
            item = (await session.execute(select(db.MemeItem).where(
                db.MemeItem.id == item_id, db.MemeItem.bucket == self.bucket
            ))).scalar_one_or_none()
            if item is None or item.status == "missing":
                raise HTTPException(404, "Meme image not found")
            object_name = item.object_name
        async with self._image_slots:
            return await asyncio.to_thread(self._read_meme_image, object_name)

    async def update_meme(self, item_id: int, payload: dict, actor: str, hard: bool = False) -> dict:
        _only(payload, {"emotion", "status", "note"})
        if "emotion" in payload and payload["emotion"] not in ("", *EMOTIONS):
            raise HTTPException(422, "Invalid emotion")
        if "status" in payload and payload["status"] not in MEME_STATUSES:
            raise HTTPException(422, "Invalid status")
        if "note" in payload:
            _text(payload["note"], "note", 2000)
        async with await self.session_factory() as session:
            item = (await session.execute(select(db.MemeItem).where(db.MemeItem.id == item_id, db.MemeItem.bucket == self.bucket).with_for_update())).scalar_one_or_none()
            if item is None:
                raise HTTPException(404, "Meme not found")
            if not hard and payload.get("status") == "active" and item.status in {"deleted", "missing"}:
                self._remote_clients()
                try:
                    await asyncio.to_thread(self.minio.stat_object, self.bucket, item.object_name)
                except Exception:
                    raise HTTPException(502, "Stored image unavailable; cannot restore meme") from None
            if hard:
                self._remote_clients()
                try:
                    await asyncio.to_thread(self.minio.remove_object, self.bucket, item.object_name)
                except Exception:
                    raise HTTPException(502, "Remote deletion failed; metadata was not changed") from None
            for key, value in payload.items():
                setattr(item, key, value)
            if hard:
                item.status = "missing"
            if payload.get("emotion"):
                item.classified_at = datetime.now()
            item.updated_at = datetime.now()
            _audit(session, "meme.delete" if payload.get("status") == "deleted" else "meme.update",
                   str(item_id), {"fields": sorted(payload), "hard": hard}, actor)
            try:
                await session.commit()
            except Exception:
                await session.rollback()
                if hard:
                    raise HTTPException(503, {"message": "Image removed but metadata could not be saved; retry cleanup",
                                              "remote_deleted": True, "saved": False, "cache_synced": False}) from None
                raise
            result = _row(item)
            try:
                await asyncio.to_thread(self._sync_meme, item)
            except Exception:
                raise HTTPException(502, {"message": "Metadata saved but meme cache sync failed", "saved": True,
                                          "cache_synced": False, "remote_deleted": bool(hard), "item_id": item_id}) from None
            return {"ok": True, "item": result, "cache_synced": True, "remote_deleted": bool(hard)}

    async def batch_memes(self, payload: dict, actor: str) -> dict:
        _only(payload, {"ids", "hard", "action", "patch"})
        ids = payload.get("ids")
        if not isinstance(ids, list) or not 1 <= len(ids) <= 200:
            raise HTTPException(422, "Provide 1 to 200 meme IDs")
        ids = list(dict.fromkeys(_integer(value, "meme ID") for value in ids))
        action = payload.get("action", "delete")
        if action not in {"delete", "update"} or not isinstance(payload.get("hard", False), bool):
            raise HTTPException(422, "Invalid batch action")
        patch = {"status": "deleted"} if action == "delete" else payload.get("patch", {})
        succeeded, failed = [], []
        for item_id in ids:
            try:
                await self.update_meme(item_id, patch, actor, hard=action == "delete" and payload.get("hard", False))
                succeeded.append(item_id)
            except HTTPException as error:
                failed.append({"id": item_id, "status": error.status_code, "detail": error.detail})
            except Exception:
                failed.append({"id": item_id, "status": 503, "detail": "Meme operation failed"})
        return {"ok": not failed, "succeeded_ids": succeeded, "deleted_ids": succeeded if action == "delete" else [], "failed": failed}

    @staticmethod
    def _memory_model(kind: str, scope: str):
        if scope not in {"group", "global"}:
            raise HTTPException(422, "Invalid memory scope")
        models = {"facts": db.AgentGlobalPersonFact if scope == "global" else db.AgentPersonFact,
                  "episodes": db.AgentMemoryEpisode, "summaries": db.AgentContextSummary,
                  "relationships": db.AgentPersonaRelationshipState, "affinities": db.AgentPersonaAffinity}
        if kind not in models:
            raise HTTPException(404, "Memory collection not found")
        return models[kind]

    def _check_persona(self, persona_id: str) -> str:
        if persona_id not in {profile.persona_id for profile in self._personas().list_personas()}:
            raise HTTPException(422, "Unknown persona")
        return persona_id

    async def memories(self, kind: str, scope: str = "group", group_id: int = 0, user_id: int = 0,
                       persona_id: str = "", q: str = "", offset: int = 0, limit: int = 50,
                       sort_by: str = "updated_at", sort_order: str = "desc") -> dict:
        model = self._memory_model(kind, scope)
        allowed_sorts = {"updated_at"}
        if kind == "relationships":
            allowed_sorts.update({"message_count", "explicit_interaction_count", "affinity_score"})
        elif kind == "affinities":
            allowed_sorts.add("affinity_score")
        if sort_by not in allowed_sorts or sort_order not in {"asc", "desc"}:
            raise HTTPException(422, "Invalid memory sort field or direction")
        column = getattr(model, sort_by)
        ordering = column.desc() if sort_order == "desc" else column.asc()
        primary_key = model.group_id if kind == "summaries" else model.id
        scope = scope if kind == "facts" else "global" if kind == "affinities" else "group"
        filters = []
        if group_id and hasattr(model, "group_id"):
            filters.append(model.group_id == group_id)
        if user_id and hasattr(model, "user_id"):
            filters.append(model.user_id == user_id)
        if persona_id and hasattr(model, "persona_id"):
            filters.append(model.persona_id == self._check_persona(persona_id))
        if q and kind in {"facts", "episodes", "summaries"}:
            column = model.fact if kind == "facts" else model.summary
            filters.append(column.contains(q[:200], autoescape=True))
        async with await self.session_factory() as session:
            total = (await session.execute(select(func.count()).select_from(model).where(*filters))).scalar() or 0
            rows = (await session.execute(select(model).where(*filters).order_by(ordering, primary_key.asc()).offset(offset).limit(limit))).scalars().all()
            items = []
            for row in rows:
                item = _row(row)
                if kind == "summaries":
                    item["id"] = row.group_id
                item["scope"] = scope
                item["protected"] = await self._protected_row(session, kind, scope, row)
                items.append(item)
            return {"items": items, "total": total, "kind": kind, "scope": scope,
                    "sort_by": sort_by, "sort_order": sort_order}

    @staticmethod
    def _memory_identity(kind: str, scope: str, row: Any) -> dict:
        return {"kind": kind, "scope": scope,
                "group_id": int(getattr(row, "group_id", 0) or 0),
                "user_id": int(getattr(row, "user_id", 0) or 0),
                "identity": str(row.fingerprint if kind == "facts" else row.end_message_id if kind == "episodes" else ""),
                "persona_id": str(getattr(row, "persona_id", "") or "")}

    async def _protected_row(self, session: Any, kind: str, scope: str, row: Any) -> bool:
        return await is_memory_protected(session, **self._memory_identity(kind, scope, row))

    def _memory_values(self, kind: str, scope: str, payload: dict, creating: bool) -> dict:
        common = {"reason", "scope"}
        identities = {"group_id", "user_id", "persona_id"} if creating else set()
        editable = {"facts": {"fact", "category", "importance", "confidence", "status", "expires_at", "source_message_id", "source_group_id"},
                    "episodes": {"summary", "start_message_id", "end_message_id", "participant_ids"},
                    "summaries": {"summary", "covered_message_id"},
                    "relationships": {"message_count", "explicit_interaction_count", "last_reason"}}
        if kind not in editable:
            raise HTTPException(422, "Use the affinity endpoint for score changes")
        _only(payload, common | identities | editable[kind])
        _text(payload.get("reason", ""), "reason", 300, required=True)
        values = {key: value for key, value in payload.items() if key not in common}
        for key in ("group_id", "user_id", "source_group_id", "source_message_id", "start_message_id", "end_message_id", "covered_message_id", "message_count", "explicit_interaction_count"):
            if key in values:
                _integer(values[key], key, low=1 if key in {"group_id", "user_id"} else 0)
        for key, maximum in (("fact", 500), ("summary", 10000), ("last_reason", 300)):
            if key in values:
                values[key] = _text(values[key], key, maximum, required=key != "last_reason")
        if "category" in values and values["category"] not in FACT_CATEGORIES:
            raise HTTPException(422, "Invalid fact category")
        if "status" in values and values["status"] not in {"active", "disabled", "deleted"}:
            raise HTTPException(422, "Invalid memory status")
        if "importance" in values:
            _integer(values["importance"], "importance", 1, 5)
        if "confidence" in values:
            value = values["confidence"]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
                raise HTTPException(422, "Invalid confidence")
        if "expires_at" in values and values["expires_at"] is not None:
            try:
                values["expires_at"] = datetime.fromisoformat(values["expires_at"]).replace(tzinfo=None)
            except (TypeError, ValueError):
                raise HTTPException(422, "Invalid expiration date") from None
        if "participant_ids" in values:
            ids = values["participant_ids"]
            if not isinstance(ids, list) or len(ids) > 500:
                raise HTTPException(422, "Invalid participant IDs")
            values["participant_ids"] = json.dumps([_integer(value, "participant ID") for value in ids])
        if "persona_id" in values:
            self._check_persona(values["persona_id"])
        if creating:
            required = {"facts": {"fact", "user_id"} | ({"group_id"} if scope == "group" else set()),
                        "episodes": {"group_id", "summary", "end_message_id"},
                        "summaries": {"group_id", "summary"},
                        "relationships": {"group_id", "user_id", "persona_id"}}[kind]
            if not required <= values.keys():
                raise HTTPException(422, "Missing memory identity or content")
        if "fact" in values:
            values["fingerprint"] = _fingerprint(values["fact"])
        return values

    async def mutate_memory(self, kind: str, scope: str, payload: dict, actor: str,
                            item_id: int | None = None, deleting: bool = False) -> dict:
        model = self._memory_model(kind, scope)
        scope = scope if kind == "facts" else "group"
        if kind == "affinities":
            raise HTTPException(422, "Use the affinity endpoint for score changes")
        if deleting:
            _only(payload, {"reason", "scope"})
            _text(payload.get("reason", ""), "reason", 300, required=True)
            values = {}
        else:
            values = self._memory_values(kind, scope, payload, creating=item_id is None)
        if set(values) - {column.name for column in model.__table__.columns}:
            raise HTTPException(422, "Invalid fields for memory scope")
        async with MEMORY_WRITE_LOCK:
            async with await self.session_factory() as session:
                if item_id is None:
                    row = model(**values)
                    session.add(row)
                    await session.flush()
                else:
                    row = await session.get(model, item_id, with_for_update=True)
                    if row is None:
                        raise HTTPException(404, "Memory record not found")
                    await protect_memory(session, **self._memory_identity(kind, scope, row), actor=actor)
                    for key, value in values.items():
                        setattr(row, key, value)
                await protect_memory(session, **self._memory_identity(kind, scope, row), actor=actor)
                row.updated_at = datetime.now()
                result = _row(row)
                result["id"] = getattr(row, "id", getattr(row, "group_id", None))
                result["protected"] = True
                if deleting:
                    await session.delete(row)
                _audit(session, f"memory.{kind}.{'delete' if deleting else 'update' if item_id else 'create'}",
                       f"{scope}:{result['id']}", {"reason": payload["reason"], "fields": sorted(values)}, actor)
                await session.commit()
                if kind == "summaries":
                    context = sys.modules.get("plugins.agent_context")
                    if context is not None:
                        context.CACHE.set_summary(row.group_id, "" if deleting else row.summary,
                                                  0 if deleting else row.covered_message_id)
                return {"ok": True, "item": result}

    async def set_affinity(self, persona_id: str, user_id: int, payload: dict, actor: str) -> dict:
        self._check_persona(persona_id)
        _integer(user_id, "user ID")
        _only(payload, {"score", "reason", "group_id"})
        score = payload.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not -100 <= score <= 100:
            raise HTTPException(422, "Score must be between -100 and 100")
        reason = _text(payload.get("reason", ""), "reason", 300, required=True)
        gid = _integer(payload.get("group_id", 0), "group ID", low=0)
        async with MEMORY_WRITE_LOCK:
            async with await self.session_factory() as session:
                affinity = (await session.execute(select(db.AgentPersonaAffinity).where(db.AgentPersonaAffinity.persona_id == persona_id,
                            db.AgentPersonaAffinity.user_id == user_id).with_for_update())).scalar_one_or_none()
                old = float(affinity.affinity_score or 0) if affinity else 0.0
                if affinity is None:
                    affinity = db.AgentPersonaAffinity(persona_id=persona_id, user_id=user_id)
                    session.add(affinity)
                mirrors = (await session.execute(select(db.AgentPersonaRelationshipState).where(
                    db.AgentPersonaRelationshipState.persona_id == persona_id, db.AgentPersonaRelationshipState.user_id == user_id).with_for_update())).scalars().all()
                if gid and not any(row.group_id == gid for row in mirrors):
                    row = db.AgentPersonaRelationshipState(persona_id=persona_id, group_id=gid, user_id=user_id)
                    session.add(row)
                    mirrors.append(row)
                if persona_id == "rin":
                    legacy = (await session.execute(select(db.UserAffinity).where(db.UserAffinity.user_id == user_id).with_for_update())).scalar_one_or_none()
                    if legacy is None:
                        legacy = db.UserAffinity(user_id=user_id)
                        session.add(legacy)
                    mirrors.append(legacy)
                    legacy_relations = (await session.execute(select(db.AgentRelationshipState).where(db.AgentRelationshipState.user_id == user_id).with_for_update())).scalars().all()
                    mirrors.extend(legacy_relations)
                for row in [affinity, *mirrors]:
                    row.affinity_score = float(score)
                    row.last_delta = float(score) - old
                    row.last_reason = reason
                    row.updated_at = datetime.now()
                _audit(session, "memory.affinity.set", f"{persona_id}:{user_id}", {"before": old, "after": score, "reason": reason}, actor)
                await session.commit()
                return {"ok": True, "item": _row(affinity), "synced_relationships": len(mirrors)}

    async def roster(self, start: date) -> dict:
        async with await self.session_factory() as session:
            rows = (await session.execute(select(db.DutyRosterEntry).where(db.DutyRosterEntry.duty_date >= start,
                    db.DutyRosterEntry.duty_date < start + timedelta(days=14)).order_by(db.DutyRosterEntry.duty_date))).scalars().all()
            return {"items": [_row(row) for row in rows], "total": len(rows)}

    async def update_roster(self, payload: dict, actor: str) -> dict:
        _only(payload, {"duty_date", "persona_id", "reason"})
        try:
            day = date.fromisoformat(payload.get("duty_date", ""))
        except (TypeError, ValueError):
            raise HTTPException(422, "Invalid duty date") from None
        from .console_state import local_now
        if day < local_now().date():
            raise HTTPException(422, "Past duty dates cannot be changed")
        persona_id = self._check_persona(payload.get("persona_id", ""))
        reason = _text(payload.get("reason", ""), "reason", 300, required=True)
        async with await self.session_factory() as session:
            row = await session.get(db.DutyRosterEntry, day)
            if row is None:
                row = db.DutyRosterEntry(duty_date=day)
                session.add(row)
            row.persona_id, row.source, row.actor_user_id = persona_id, "manual", 0
            row.applied_at = None
            row.updated_at = datetime.now()
            _audit(session, "roster.update", day.isoformat(), {"persona_id": persona_id, "reason": reason}, actor)
            await session.commit()
            return {"ok": True, "item": _row(row)}
