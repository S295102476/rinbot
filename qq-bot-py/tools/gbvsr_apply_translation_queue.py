"""Apply reviewed GBVSR note translations from the manual queue to the glossary."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
GLOSSARY_PATH = ROOT / "data" / "gbvsr" / "notes" / "_glossary.json"
QUEUE_PATH = GLOSSARY_PATH.with_name("_manual_translation_queue.json")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    glossary = load_json(GLOSSARY_PATH)
    queue = load_json(QUEUE_PATH)
    by_text = {
        str(item.get("text") or "").strip(): item
        for item in glossary.get("entries", [])
        if str(item.get("text") or "").strip()
    }

    updated = 0
    conflicts: list[str] = []
    missing: list[str] = []
    for item in queue.get("entries", []):
        text = str(item.get("text") or "").strip()
        zh = str(item.get("zh") or "").strip()
        if not zh:
            continue
        target = by_text.get(text)
        if target is None:
            missing.append(text)
            continue
        existing = str(target.get("zh") or "").strip()
        if existing and existing != zh:
            conflicts.append(text)
            continue
        if not existing:
            target["zh"] = zh
            updated += 1

    GLOSSARY_PATH.write_text(json.dumps(glossary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"OK applied: {updated}")
    print(f"conflicts: {len(conflicts)}")
    print(f"missing: {len(missing)}")
    for text in conflicts:
        print("CONFLICT", text)
    for text in missing:
        print("MISSING", text)


if __name__ == "__main__":
    main()