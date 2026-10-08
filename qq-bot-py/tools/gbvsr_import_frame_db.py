"""Import GBVSR frame JSON files into MySQL."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import delete

from plugins.db import GBVSRFrameMove, SessionFactory, engine, Base


ROOT = Path(__file__).resolve().parents[1]
FRAME_DIR = ROOT / "data" / "gbvsr" / "frame"


def _unique_aliases(aliases: list) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for alias in aliases:
        text = str(alias).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _prepare_rows(rows: list[dict]) -> list[dict]:
    counts: dict[str, int] = {}
    prepared: list[dict] = []

    for item in rows:
        row = item.get("row") or {}
        input_name = str(row.get("Input") or "").strip()
        if not input_name:
            continue

        counts[input_name] = counts.get(input_name, 0) + 1
        index = counts[input_name]

        if index > 1:
            item = dict(item)
            row = dict(row)
            item["row"] = row

            original_input = input_name
            if original_input in {"Ground Throw", "Air Throw"} and index == 2:
                input_name = f"d.{original_input}"
            else:
                input_name = f"{original_input}#{index}"

            row["Input"] = input_name
            aliases = list(item.get("aliases") or [])
            aliases.extend([original_input, input_name])
            if input_name == "d.Ground Throw":
                aliases.extend(["d.ab", "d.ad", "dab", "dad", "d.a+b", "d.a+d"])
            elif input_name == "d.Air Throw":
                aliases.extend(["d.jab", "d.jad", "djab", "djad", "d.j.a+b", "d.j.a+d"])
            item["aliases"] = _unique_aliases(aliases)

        prepared.append(item)

    return prepared


async def import_character(path: Path) -> tuple[str, int]:
    data = json.loads(path.read_text(encoding="utf-8"))
    character = str(data.get("character") or path.stem.replace("Frame2.local", "").replace("Frame2", ""))
    rows = _prepare_rows(data.get("rows") or [])

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with SessionFactory() as session:
        async with session.begin():
            await session.execute(
                delete(GBVSRFrameMove).where(GBVSRFrameMove.character == character)
            )
            for item in rows:
                row = item.get("row") or {}
                move_name = (
                    item.get("moveName")
                    or item.get("move_name")
                    or item.get("displayName")
                    or row.get("Name")
                    or ""
                )
                session.add(
                    GBVSRFrameMove(
                        character=character,
                        section=str(item.get("section") or ""),
                        input_name=str(row.get("Input") or ""),
                        move_name=str(move_name or ""),
                        damage=str(row.get("Damage") or ""),
                        guard=str(row.get("Guard") or ""),
                        startup=str(row.get("Startup") or ""),
                        active=str(row.get("Active") or ""),
                        recovery=str(row.get("Recovery") or ""),
                        on_block=str(row.get("On-Block") or ""),
                        on_hit=str(row.get("On-Hit") or ""),
                        on_counter_hit=str(row.get("On Counter Hit") or ""),
                        level=str(row.get("Level") or ""),
                        invuln=str(row.get("Invuln") or ""),
                        combo_limit_scaling=str(row.get("Combo Limit Scaling") or ""),
                        notes=str(item.get("notes") or ""),
                        notes_zh=str(item.get("notesZh") or ""),
                        image_paths=json.dumps(item.get("imagePaths") or [], ensure_ascii=False),
                        hitbox_paths=json.dumps(item.get("hitboxPaths") or [], ensure_ascii=False),
                        aliases=json.dumps(item.get("aliases") or [], ensure_ascii=False),
                    )
                )

    return character, len(rows)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="*", help="Frame json files to import.")
    args = parser.parse_args()

    targets = [Path(p) for p in args.paths] if args.paths else sorted(FRAME_DIR.glob("*Frame2.local.json"))
    if not targets:
        raise SystemExit("No frame json files found.")

    for path in targets:
        character, count = await import_character(path)
        print(f"OK {character}: {count} rows")

    await engine.dispose()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
