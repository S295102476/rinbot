"""Apply translated GBVSR notes back to frame JSON files.

Usage:
  python tools/gbvsr_apply_notes.py
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from gbvsr_note_utils import NOTES_DIR, frame_files, iter_note_items, load_json, note_id, write_json


def _note_files() -> list[Path]:
    return sorted(p for p in NOTES_DIR.glob("*.notes.json") if p.name != "_glossary.json")


def _load_translations() -> dict[str, dict[str, str]]:
    translations: dict[str, dict[str, str]] = {}
    for path in _note_files():
        data = load_json(path)
        character = str(data.get("character") or "")
        if not character:
            print(f"WARN {path}: missing character")
            continue
        bucket = translations.setdefault(character, {})
        for item in data.get("notes", []) or []:
            note_zh = str(item.get("noteZh") or "").strip()
            if not note_zh:
                continue
            item_id = str(item.get("id") or "")
            if item_id:
                bucket[item_id] = note_zh
    return translations


def _fallback_key(character: str, item: dict[str, Any]) -> str:
    return "\0".join(
        [
            character,
            str(item.get("section") or ""),
            str(item.get("input") or ""),
            str(item.get("name") or ""),
            str(item.get("note") or ""),
        ]
    )


def _load_fallback_translations() -> dict[str, str]:
    out: dict[str, str] = {}
    for path in _note_files():
        data = load_json(path)
        character = str(data.get("character") or "")
        for item in data.get("notes", []) or []:
            note_zh = str(item.get("noteZh") or "").strip()
            if not note_zh:
                continue
            out[_fallback_key(character, item)] = note_zh
    return out


def apply_notes(dry_run: bool = False) -> tuple[int, int, int]:
    translations = _load_translations()
    fallback_translations = _load_fallback_translations()

    changed_files = 0
    changed_rows = 0
    warnings = 0

    for frame_path in frame_files():
        data = load_json(frame_path)
        character = str(data.get("character") or frame_path.stem)
        char_translations = translations.get(character, {})
        if not char_translations and not any(k.startswith(character + "\0") for k in fallback_translations):
            continue

        changed = False
        matched_ids: set[str] = set()

        for generated, source_item in iter_note_items(data):
            item_id = generated["id"]
            note_zh = char_translations.get(item_id)
            if not note_zh:
                note_zh = fallback_translations.get(_fallback_key(character, generated), "")

            if not note_zh:
                continue

            matched_ids.add(item_id)
            if str(source_item.get("notesZh") or "") == note_zh:
                continue
            source_item["notesZh"] = note_zh
            changed = True
            changed_rows += 1

        requested_ids = set(char_translations)
        missing_ids = requested_ids - matched_ids
        for missing_id in sorted(missing_ids):
            print(f"WARN {character}: translated note id not matched: {missing_id}")
            warnings += 1

        if changed:
            changed_files += 1
            if not dry_run:
                write_json(frame_path, data)
            print(f"OK {character}: {frame_path.name}")

    return changed_files, changed_rows, warnings


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply translated GBVSR notes to frame JSON files.")
    parser.add_argument("--dry-run", action="store_true", help="Report changes without writing frame JSON files.")
    args = parser.parse_args()

    changed_files, changed_rows, warnings = apply_notes(dry_run=args.dry_run)
    action = "would update" if args.dry_run else "updated"
    print(f"DONE: {action} {changed_rows} rows in {changed_files} files, warnings={warnings}")


if __name__ == "__main__":
    main()
