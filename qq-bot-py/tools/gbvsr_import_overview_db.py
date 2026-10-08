"""Import GBVSR overview JSON into MySQL."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import delete

from plugins.db import Base, GBVSROverview, SessionFactory, engine


DEFAULT_PATH = ROOT / "data" / "gbvsr" / "overview" / "gbvsr_overview.local.json"


def _stat(stats: dict, key: str) -> str:
    return str(stats.get(key) or "")


async def import_overview(path: Path) -> int:
    data = json.loads(path.read_text(encoding="utf-8"))
    characters = data.get("characters") or []

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with SessionFactory() as session:
        async with session.begin():
            await session.execute(delete(GBVSROverview))
            for item in characters:
                character = str(item.get("character") or "").strip()
                if not character:
                    continue
                stats = item.get("stats") or {}
                session.add(
                    GBVSROverview(
                        character=character,
                        display_name=str(item.get("displayName") or character),
                        health=_stat(stats, "health"),
                        backdash=_stat(stats, "backdash"),
                        jump_startup=_stat(stats, "jump_startup"),
                        walk_speed=_stat(stats, "walk_speed"),
                        backwalk_speed=_stat(stats, "backwalk_speed"),
                        initial_dash_speed=_stat(stats, "initial_dash_speed"),
                        dash_acceleration=_stat(stats, "dash_acceleration"),
                        jump_height=_stat(stats, "jump_height"),
                        forward_jump_distance=_stat(stats, "forward_jump_distance"),
                        backward_jump_distance=_stat(stats, "backward_jump_distance"),
                        superjump_height=_stat(stats, "superjump_height"),
                        forward_superjump_distance=_stat(stats, "forward_superjump_distance"),
                        backward_superjump_distance=_stat(stats, "backward_superjump_distance"),
                        cl_proximity_range=_stat(stats, "cl_proximity_range"),
                        cm_proximity_range=_stat(stats, "cm_proximity_range"),
                        ch_proximity_range=_stat(stats, "ch_proximity_range"),
                        portrait_path=str(item.get("portraitPath") or ""),
                        source=str(data.get("source") or ""),
                        raw_data=json.dumps(item, ensure_ascii=False),
                    )
                )

    return len(characters)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", default=str(DEFAULT_PATH), help="Overview json file.")
    args = parser.parse_args()

    count = await import_overview(Path(args.path))
    print(f"OK overview: {count} characters")
    await engine.dispose()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
