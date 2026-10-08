"""Export GBVSR move notes for manual translation.

Usage:
  python tools/gbvsr_export_notes.py
"""

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from gbvsr_note_utils import (
    GLOSSARY_PATH,
    NOTES_DIR,
    auto_translate_sentence,
    frame_files,
    iter_note_items,
    load_json,
    note_file_path,
    note_md_path,
    split_note,
    write_json,
)


def _load_existing_note_translations(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        data = load_json(path)
    except Exception:
        return {}
    out: dict[str, str] = {}
    for item in data.get("notes", []) or []:
        note_id = str(item.get("id") or "")
        note_zh = str(item.get("noteZh") or "")
        if note_id and note_zh:
            out[note_id] = note_zh
    return out


def _load_glossary() -> dict[str, dict[str, Any]]:
    if not GLOSSARY_PATH.exists():
        return {}
    try:
        data = load_json(GLOSSARY_PATH)
    except Exception:
        return {}
    entries = data.get("entries", []) if isinstance(data, dict) else []
    out: dict[str, dict[str, Any]] = {}
    for item in entries:
        text = str(item.get("text") or "").strip()
        if text:
            out[text] = item
    return out


def _prefill_from_glossary(note: str, glossary: dict[str, dict[str, Any]]) -> str:
    parts = split_note(note)
    if not parts:
        return ""
    zh_parts: list[str] = []
    for part in parts:
        zh = str((glossary.get(part) or {}).get("zh") or "").strip()
        if not zh:
            zh = auto_translate_sentence(part)
        if not zh:
            return ""
        zh_parts.append(zh)
    return "\n".join(zh_parts)


def _write_markdown(path: Path, character: str, notes: list[dict[str, Any]]) -> None:
    lines = [
        f"# {character} Notes",
        "",
        "> JSON 是翻译源文件；这个 Markdown 只用于阅读参考。",
        "",
    ]
    for item in notes:
        name = f" {item['name']}" if item.get("name") else ""
        lines.extend(
            [
                f"## {item['query']} / {item['input']}{name}",
                "",
                f"note: {item['note']}",
                "",
                f"noteZh: {item.get('noteZh') or ''}",
                "",
            ]
        )
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def export_notes() -> tuple[int, int, int]:
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    glossary = _load_glossary()
    sentence_counts: Counter[str] = Counter()
    sentence_examples: dict[str, set[str]] = defaultdict(set)

    character_count = 0
    note_count = 0

    for frame_path in frame_files():
        data = load_json(frame_path)
        character = str(data.get("character") or frame_path.stem)
        note_path = note_file_path(character)
        existing = _load_existing_note_translations(note_path)

        notes: list[dict[str, Any]] = []
        for item, _source in iter_note_items(data):
            for sentence in split_note(item["note"]):
                sentence_counts[sentence] += 1
                sentence_examples[sentence].add(f"{character} {item['input']}")

            if item["id"] in existing:
                item["noteZh"] = existing[item["id"]]
            elif not item["noteZh"]:
                item["noteZh"] = _prefill_from_glossary(item["note"], glossary)
            notes.append(item)

        if not notes:
            continue

        character_count += 1
        note_count += len(notes)
        payload = {
            "character": character,
            "sourceFrame": frame_path.relative_to(Path.cwd()).as_posix(),
            "notes": notes,
        }
        write_json(note_path, payload)
        _write_markdown(note_md_path(character), character, notes)

    glossary_entries = []
    existing_glossary = _load_glossary()
    for text, count in sentence_counts.most_common():
        old = existing_glossary.get(text) or {}
        zh = str(old.get("zh") or "")
        if not zh:
            zh = auto_translate_sentence(text)
        glossary_entries.append(
            {
                "text": text,
                "zh": zh,
                "count": count,
                "examples": sorted(sentence_examples[text])[:5],
            }
        )
    write_json(GLOSSARY_PATH, {"entries": glossary_entries})

    return character_count, note_count, len(glossary_entries)


def main() -> None:
    character_count, note_count, sentence_count = export_notes()
    print(f"OK notes: {character_count} characters, {note_count} note rows")
    print(f"OK glossary: {sentence_count} unique sentences")


if __name__ == "__main__":
    main()
