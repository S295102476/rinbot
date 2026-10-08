"""Export meaningful untranslated GBVSR note sentences for manual translation."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
GLOSSARY_PATH = ROOT / "data" / "gbvsr" / "notes" / "_glossary.json"
QUEUE_PATH = GLOSSARY_PATH.with_name("_manual_translation_queue.json")
FRAME_DIR = ROOT / "data" / "gbvsr" / "frame"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def character_names() -> set[str]:
    names: set[str] = set()
    for path in FRAME_DIR.glob("*Frame2.local.json"):
        try:
            data = load_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        name = str(data.get("character") or "").strip()
        if not name:
            continue
        names.add(name.casefold())
        names.add(name.replace("_(EX)", "").replace("_", " ").casefold())
    return names


def ignored_reason(text: str, names: set[str]) -> str | None:
    normalized = str(text or "").strip()
    if not normalized:
        return "empty"
    if re.fullmatch(r":?\s*\d+(?:\.\d+)?\.?\s*", normalized):
        return "parser_remnant"
    if normalized in {": 4, 3"}:
        return "parser_remnant"
    if normalized.casefold() in names:
        return "character_name"
    return None


def saved_translations() -> dict[str, str]:
    if not QUEUE_PATH.exists():
        return {}
    try:
        data = load_json(QUEUE_PATH)
    except (OSError, json.JSONDecodeError):
        return {}
    return {
        str(item.get("text") or "").strip(): str(item.get("zh") or "").strip()
        for item in data.get("entries", [])
        if str(item.get("text") or "").strip() and str(item.get("zh") or "").strip()
    }


def main() -> None:
    glossary = load_json(GLOSSARY_PATH)
    saved = saved_translations()
    names = character_names()
    entries: list[dict[str, Any]] = []
    ignored: list[dict[str, str]] = []

    for item in glossary.get("entries", []):
        text = str(item.get("text") or "").strip()
        if str(item.get("zh") or "").strip():
            continue
        reason = ignored_reason(text, names)
        if reason:
            ignored.append({"text": text, "reason": reason})
            continue
        entries.append(
            {
                "text": text,
                "zh": saved.get(text, ""),
                "count": item.get("count", 0),
                "examples": item.get("examples", []),
            }
        )

    payload = {
        "description": "Fill zh with a reviewed Chinese translation. Do not edit text; it is the stable key used to merge translations back into _glossary.json.",
        "entries": entries,
        "ignored": ignored,
    }
    QUEUE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"OK queue: {len(entries)} pending, {len(ignored)} ignored")
    print(QUEUE_PATH)


if __name__ == "__main__":
    main()