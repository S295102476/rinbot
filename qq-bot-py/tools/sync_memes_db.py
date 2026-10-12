"""
同步 MinIO memes bucket、Redis 情绪索引和 MySQL meme_items。

用法：
  cd /opt/qq-bot-py
  .venv/bin/python tools/sync_memes_db.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

import redis as redis_lib
import yaml
from minio import Minio
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from plugins.db import MemeItem, get_session, engine, Base  # noqa: E402

# Match plugins/classify_memes: an explicit working directory supplies config.
# Tests use a temporary synthetic config, never the author's source-tree file.
CONFIG_PATH = Path("config.yaml")
if not CONFIG_PATH.exists():
    CONFIG_PATH = ROOT / "config.yaml"
CONFIG = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
MEME_CFG = CONFIG["meme"]
MINIO_CFG = MEME_CFG["minio"]
BUCKET = MINIO_CFG["bucket"]

POOL_KEY = "meme:pool"
TAG_PREFIX = "meme:tags:"
EMOTION_PREFIX = "meme:emotion:"
EMOTIONS = [
    "happy", "sad", "angry", "surprised", "funny", "cool", "disgusted",
    "confused", "curious", "calm", "shy", "smug", "neutral",
]

minio_client = Minio(
    MINIO_CFG["endpoint"],
    access_key=MINIO_CFG["access_key"],
    secret_key=MINIO_CFG["secret_key"],
    secure=MINIO_CFG.get("secure", False),
)

rds = redis_lib.Redis(
    host=CONFIG["redis"]["host"],
    port=CONFIG["redis"]["port"],
    password=CONFIG["redis"].get("password") or None,
    db=int(CONFIG["redis"].get("db", 0)),
    decode_responses=True,
)


def _md5_from_name(name: str) -> str:
    stem = name.rsplit(".", 1)[0]
    return stem if len(stem) == 32 else ""


async def sync_metadata(objects, tag_reader, session_factory=get_session, *, dry_run=False):
    """Reconcile object metadata without reviving disabled or deleted rows."""
    object_map = {obj.object_name: obj for obj in objects}
    now = datetime.now()
    inserted = updated = missing = 0
    projected = []
    async with await session_factory() as session:
        rows = (await session.execute(select(MemeItem).where(MemeItem.bucket == BUCKET).with_for_update())).scalars().all()
        existing = {item.object_name: item for item in rows}
        for name, obj in object_map.items():
            raw_tag = tag_reader(f"{TAG_PREFIX}{name}") or ""
            tag = raw_tag if raw_tag in EMOTIONS else ""
            item = existing.get(name)
            if item is not None:
                emotion = tag or (item.emotion if item.emotion in EMOTIONS else "")
                # A restored object repairs a missing row. Administrative
                # disabled/deleted decisions remain in force until a human acts.
                status = "active" if item.status == "missing" else item.status
                updated += 1
                if not dry_run:
                    if tag and (item.emotion != tag or item.classified_at is None):
                        item.classified_at = now
                    item.size = int(obj.size or item.size or 0)
                    item.emotion, item.status, item.updated_at = emotion, status, now
            else:
                emotion, status = tag, "active"
                inserted += 1
                if not dry_run:
                    modified = getattr(obj, "last_modified", None) or now
                    session.add(MemeItem(bucket=BUCKET, object_name=name,
                        md5=_md5_from_name(name), size=int(obj.size or 0), emotion=emotion,
                        status=status, collected_at=modified.replace(tzinfo=None),
                        classified_at=now if emotion else None, updated_at=now,
                        tags=json.dumps([], ensure_ascii=False)))
            projected.append({"object_name": name, "emotion": emotion, "status": status})
        for name, item in existing.items():
            if name not in object_map:
                status = "missing" if item.status == "active" else item.status
                if item.status == "active":
                    missing += 1
                    if not dry_run:
                        item.status, item.updated_at = status, now
                projected.append({"object_name": name, "emotion": item.emotion, "status": status})
        if not dry_run:
            await session.commit()
    return {"minio": len(object_map), "inserted": inserted, "updated": updated,
            "missing": missing, "items": projected}


def rebuild_indexes(items, object_names, client=rds, *, dry_run=False):
    """Only active, present metadata participates in image selection."""
    if dry_run:
        return
    pipe = client.pipeline()
    pipe.delete(POOL_KEY)
    for emotion in EMOTIONS:
        pipe.delete(f"{EMOTION_PREFIX}{emotion}")
    # Older classifiers wrote an invalid emotion set; it is never selectable.
    pipe.delete(f"{EMOTION_PREFIX}invalid")
    for item in items:
        name, emotion = item["object_name"], item["emotion"]
        pipe.delete(f"{TAG_PREFIX}{name}")
        if name not in object_names:
            continue
        if emotion in EMOTIONS:
            # Keep cached labels for excluded objects, without exposing them
            # through the active pool. --skip-done can avoid paid reclassification.
            pipe.set(f"{TAG_PREFIX}{name}", emotion)
        if item["status"] != "active":
            continue
        pipe.sadd(POOL_KEY, name)
        if emotion in EMOTIONS:
            pipe.sadd(f"{EMOTION_PREFIX}{emotion}", name)
    pipe.execute()


async def main(dry_run=False):
    if not dry_run:
        async with engine.begin() as conn:
            await conn.run_sync(lambda connection: Base.metadata.create_all(connection, tables=[MemeItem.__table__]))
    objects = list(minio_client.list_objects(BUCKET))
    result = await sync_metadata(objects, rds.get, dry_run=dry_run)
    rebuild_indexes(result["items"], {obj.object_name for obj in objects}, dry_run=dry_run)
    print(("DRY-RUN " if dry_run else "OK ") + "synced memes: " +
          " ".join(f"{key}={result[key]}" for key in ("minio", "inserted", "updated", "missing")))
    print("emotion distribution (active):")
    for emotion in EMOTIONS:
        count = sum(1 for item in result["items"] if item["status"] == "active" and item["emotion"] == emotion)
        if count:
            print(f"  {emotion}: {count}")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Reconcile MinIO objects, database metadata and Redis emotion indexes")
    parser.add_argument("--dry-run", action="store_true", help="Read only: do not write database, Redis or MinIO")
    options = parser.parse_args()
    with asyncio.Runner() as runner:
        try:
            runner.run(main(dry_run=options.dry_run))
        finally:
            runner.run(engine.dispose())
