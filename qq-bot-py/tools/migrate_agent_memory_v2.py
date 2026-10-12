"""Audit/import old memory without calling models or touching existing tables.

Dry run by default. Take a database backup before running with --apply.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def migrate(apply=False):
    from sqlalchemy import select
    from plugins.db import (AgentGlobalPersonFact, AgentMemoryEpisode, AgentPersonFact,
                            Base, UserMemory, engine, get_session)
    from plugins.agent_memory_models import MemoryRecord, MemoryTombstone
    from plugins.agent_memory_v2 import MEMORY_V2, fingerprint
    from plugins.console_services import is_memory_protected

    counts = {"facts": 0, "episodes": 0, "legacy_candidates": 0, "skipped": 0}
    if apply:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    async with await get_session() as session:
        candidates = []
        for model in (AgentPersonFact, AgentGlobalPersonFact):
            for row in (await session.execute(select(model))).scalars():
                group = int(getattr(row, "group_id", 0) or getattr(row, "source_group_id", 0))
                candidates.append(("facts", group, row.user_id, "fact", row.fact, row,
                    "legacy_global_unverified" if model is AgentGlobalPersonFact else "legacy_group"))
        for row in (await session.execute(select(AgentMemoryEpisode))).scalars():
            candidates.append(("episodes", row.group_id, 0, "episode", row.summary, row, "legacy_episode"))
        for row in (await session.execute(select(UserMemory))).scalars():
            candidates.append(("legacy_candidates", 0, row.user_id, "candidate", row.content, row, "legacy_short"))
        for count_key, group, user, kind, content, original, source in candidates:
            if not content or (kind == "fact" and getattr(original, "status", "active") != "active"):
                counts["skipped"] += 1
                continue
            fp = fingerprint(content)
            # Legacy global facts did not reliably establish explicit self-disclosure.
            # Import to the source group, never promote them globally by inference.
            if kind != "candidate" and not group:
                counts["skipped"] += 1
                continue
            identity = str(original.end_message_id) if kind == "episode" else fp
            protected = False
            if kind in {"fact", "episode"}:
                protected = await is_memory_protected(session, "episodes" if kind == "episode" else "facts",
                    "global" if source == "legacy_global_unverified" else "group", group_id=group,
                    user_id=user, identity=identity)
                # Deleted rows are absent or inactive (filtered above). An existing
                # active row in the ledger is an explicit manual edit; preserve it.
            if apply:
                prefs = await MEMORY_V2._privacy(session, user, group)
                source_mid = int(getattr(original, "source_message_id", 0) or getattr(original, "end_message_id", 0))
                tombstones = (await session.execute(select(MemoryTombstone).where(
                    MemoryTombstone.group_id.in_([0, group]), MemoryTombstone.user_id.in_([0, user]),
                    MemoryTombstone.fingerprint.in_([fp, "*", f"source:{source_mid}"])))).scalars().all()
                if not prefs["learning"] or tombstones:
                    counts["skipped"] += 1
                    continue
                existing = (await session.execute(select(MemoryRecord.id).where(
                    MemoryRecord.group_id == group, MemoryRecord.user_id == user,
                    MemoryRecord.kind == kind, MemoryRecord.fingerprint == fp))).scalar()
                if existing:
                    counts["skipped"] += 1
                    continue
                mid = int(getattr(original, "source_message_id", 0) or getattr(original, "end_message_id", 0))
                session.add(MemoryRecord(group_id=group, user_id=user, kind=kind,
                    content=content[:1500], fingerprint=fp, source=source,
                    confidence=0.3 if kind == "candidate" else min(0.6, float(getattr(original, "confidence", 0.6))),
                    status="candidate" if kind == "candidate" else "active", protected=protected,
                    evidence=json.dumps([{"group_id": group, "user_id": user,
                        "message_id": mid, "legacy_table": original.__tablename__,
                        "legacy_id": original.id, "source_incomplete": True}], ensure_ascii=False),
                    expires_at=getattr(original, "expires_at", None),
                    created_at=original.created_at))
            counts[count_key] += 1
        if apply:
            await session.commit()
    await engine.dispose()
    return {"mode": "applied" if apply else "dry_run", **counts}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write additive imports after taking a database backup")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(migrate(args.apply)), ensure_ascii=False))
