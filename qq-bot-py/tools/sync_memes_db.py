"""
同步 MinIO memes bucket、Redis 情绪索引和 MySQL meme_items。

用法：
  cd /opt/qq-bot-py
  .venv/bin/python tools/sync_memes_db.py
"""

from __future__ import annotations

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

CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
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
    decode_responses=True,
)


def _md5_from_name(name: str) -> str:
    stem = name.rsplit(".", 1)[0]
    return stem if len(stem) == 32 else ""


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    objects = list(minio_client.list_objects(BUCKET))
    object_names = {obj.object_name for obj in objects}
    now = datetime.now()

    session = await get_session()
    inserted = 0
    updated = 0
    try:
        for obj in objects:
            name = obj.object_name
            tag = rds.get(f"{TAG_PREFIX}{name}") or ""
            item = (await session.execute(
                select(MemeItem).where(MemeItem.bucket == BUCKET, MemeItem.object_name == name)
            )).scalar_one_or_none()
            if item:
                item.size = int(obj.size or item.size or 0)
                item.emotion = tag or item.emotion
                item.status = "active" if item.status != "deleted" else item.status
                item.updated_at = now
                if tag and not item.classified_at:
                    item.classified_at = now
                updated += 1
            else:
                session.add(MemeItem(
                    bucket=BUCKET,
                    object_name=name,
                    md5=_md5_from_name(name),
                    size=int(obj.size or 0),
                    emotion=tag,
                    status="active",
                    collected_at=getattr(obj, "last_modified", None) or now,
                    classified_at=now if tag else None,
                    updated_at=now,
                    tags=json.dumps([], ensure_ascii=False),
                ))
                inserted += 1

        # MinIO 中已不存在的对象从 active 池移除，但不强删历史记录。
        existing_items = (await session.execute(
            select(MemeItem).where(MemeItem.bucket == BUCKET, MemeItem.status == "active")
        )).scalars().all()
        missing = 0
        for item in existing_items:
            if item.object_name not in object_names:
                item.status = "missing"
                item.updated_at = now
                missing += 1

        await session.commit()
    finally:
        await session.close()

    active_items = []
    session = await get_session()
    try:
        active_items = (await session.execute(
            select(MemeItem).where(MemeItem.bucket == BUCKET, MemeItem.status == "active")
        )).scalars().all()
    finally:
        await session.close()

    pipe = rds.pipeline()
    pipe.delete(POOL_KEY)
    for em in EMOTIONS:
        pipe.delete(f"{EMOTION_PREFIX}{em}")
    if active_items:
        pipe.sadd(POOL_KEY, *[x.object_name for x in active_items])
    for item in active_items:
        if item.emotion in EMOTIONS:
            pipe.set(f"{TAG_PREFIX}{item.object_name}", item.emotion)
            pipe.sadd(f"{EMOTION_PREFIX}{item.emotion}", item.object_name)
    pipe.execute()

    print(f"OK synced memes: minio={len(objects)} inserted={inserted} updated={updated} missing={missing}")
    print("emotion distribution:")
    for em in EMOTIONS:
        count = sum(1 for x in active_items if x.emotion == em)
        if count:
            print(f"  {em}: {count}")


if __name__ == "__main__":
    asyncio.run(main())
