"""Shared helpers for GBVSR note export/apply tools."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
FRAME_DIR = ROOT / "data" / "gbvsr" / "frame"
NOTES_DIR = ROOT / "data" / "gbvsr" / "notes"
GLOSSARY_PATH = NOTES_DIR / "_glossary.json"

BUTTON_TO_QUERY = {
    "L": "a",
    "M": "b",
    "H": "c",
    "U": "d",
    "X": "x",
}

MOTION_REPLACEMENTS = (
    ("632146", "6246"),
    ("63214", "624"),
    ("236236", "2626"),
    ("214214", "2424"),
    ("236", "26"),
    ("214", "24"),
)


def compact(text: str) -> str:
    return re.sub(r"[\s\u3000._-]+", "", (text or "").strip()).lower()


def _is_noise_note(part: str) -> bool:
    text = str(part or "").strip()
    if not text:
        return True
    # Scraped expandable rows occasionally leak a bare table value such as
    # Combo Limit Scaling ("2") into notes. Those fragments are not notes.
    if re.fullmatch(r":?\s*\d+(?:\.\d+)?\.?\s*", text):
        return True
    return False


def split_note(note: str) -> list[str]:
    return [
        part.strip()
        for part in str(note or "").split(";")
        if part.strip() and not _is_noise_note(part)
    ]


def _strip_period(text: str) -> str:
    return text.strip().rstrip(".")


def _frames_zh(text: str) -> str:
    value = _strip_period(text).replace("~", "-").strip()
    if value.endswith(("F", "f")):
        value = value[:-1]
    return f"{value}帧"


def _format_clash_level(value: str) -> str:
    levels = [part.strip() for part in value.split(",") if part.strip()]
    if len(levels) > 1:
        return "相杀等级为" + "、".join(levels)
    return f"相杀等级为{value.strip()}"


def auto_translate_sentence(text: str) -> str:
    """Translate common variable GBVSR note sentences.

    Manual glossary entries still take precedence. These rules only cover
    repeated mechanical snippets where the English phrase is stable and only
    numbers or frame ranges change.
    """

    raw = str(text or "").strip()
    if not raw:
        return ""

    s = raw.replace("：", ":")
    s = re.sub(r"\s+", " ", s).strip()

    m = re.fullmatch(r"Clash level:?\s*([0-9,\s]+)\.?", s, flags=re.I)
    if m:
        return _format_clash_level(m.group(1))

    m = re.fullmatch(
        r"(?:.+? is in )?Counter ?hit state(?: frames?)?\s+([0-9Ff~\-,\s]+)\.?",
        s,
        flags=re.I,
    )
    if m:
        return f"本招{_frames_zh(m.group(1))}处于被康状态"

    m = re.fullmatch(
        r"(?:.+? is )?airborne(?: frames?)?\s+([0-9Ff~\-,\s]+)\.?",
        s,
        flags=re.I,
    )
    if m:
        return f"本招{_frames_zh(m.group(1))}为空中判定"

    m = re.fullmatch(r"Slowdown frames\s+([0-9Ff~\-,\s]+)\.?", s, flags=re.I)
    if m:
        return f"慢动作帧：{_frames_zh(m.group(1))}"

    m = re.fullmatch(r"Checks for charge on frame\s+([0-9Ff]+)\.?", s, flags=re.I)
    if m:
        return f"第{_frames_zh(m.group(1))}检查蓄力输入"

    m = re.fullmatch(r"Projectile (?:Health|Durability):?\s*([0-9]+)\.?", s, flags=re.I)
    if m:
        return f"飞行道具耐久：{m.group(1)}"

    m = re.fullmatch(r"Total base damage:\s*([0-9,\s\[\]]+)\.?", s, flags=re.I)
    if m:
        return f"总基础伤害：{_strip_period(m.group(1))}"

    m = re.fullmatch(r"Total minimum damage:\s*([0-9,\s\[\]]+)\.?", s, flags=re.I)
    if m:
        return f"总最低伤害：{_strip_period(m.group(1))}"

    m = re.fullmatch(
        r"([0-9]+)% initial damage scaling only when used as a combo starter\.?",
        s,
        flags=re.I,
    )
    if m:
        return f"作为连段起手时有{m.group(1)}%的起手修正"

    m = re.fullmatch(r"([0-9Ff]+) gap on block\.?", s, flags=re.I)
    if m:
        return f"防御时有{_frames_zh(m.group(1))}空隙"

    m = re.fullmatch(
        r"([0-9Ff]+) gap between first and second hit on block\.?",
        s,
        flags=re.I,
    )
    if m:
        return f"防御时第1段和第2段之间有{_frames_zh(m.group(1))}空隙"

    m = re.fullmatch(
        r"Fastest startup including travel time is\s+([0-9]+)\s+frames\.?",
        s,
        flags=re.I,
    )
    if m:
        return f"包含移动时间的最快发生为{m.group(1)}帧"

    m = re.fullmatch(
        r"(Wallbounce )?combo (?:launch|wallbounce) minimum\s+([0-9Ff]+)\s+\(([0-9Ff]+) on Counter Hit\)\.?",
        s,
        flags=re.I,
    )
    if m:
        prefix = "墙弹连段" if m.group(1) else "连段"
        return f"{prefix}最低浮空时间为{_frames_zh(m.group(2))}（康特时{_frames_zh(m.group(3))}）"

    m = re.fullmatch(
        r"(Wallbounce )?combo (?:launch|wallbounce) minimum\s+([0-9Ff]+)(?:\s+on Counter Hit( only)?)?\.?",
        s,
        flags=re.I,
    )
    if m:
        prefix = "墙弹连段" if m.group(1) else "连段"
        if m.group(3):
            return f"仅康特时，{prefix}最低浮空时间为{_frames_zh(m.group(2))}"
        if "on counter hit" in s.lower():
            return f"康特时，{prefix}最低浮空时间为{_frames_zh(m.group(2))}"
        return f"{prefix}最低浮空时间为{_frames_zh(m.group(2))}"

    m = re.fullmatch(
        r"Crumple frames on hit:\s*([0-9Ff]+) standing after contact,\s*([0-9Ff]+) crouching after standing\.?",
        s,
        flags=re.I,
    )
    if m:
        return f"命中后的崩溃硬直：站姿接触后{_frames_zh(m.group(1))}，蹲姿在站姿后{_frames_zh(m.group(2))}"

    m = re.fullmatch(
        r"Recovers in the air, forces\s+([0-9Ff]+)\s+of landing recovery\.?",
        s,
        flags=re.I,
    )
    if m:
        return f"空中恢复，强制产生{_frames_zh(m.group(1))}落地硬直"

    m = re.fullmatch(
        r"Can(?:cel|cell)able into follow-ups? on frames\s+([0-9~\-]+)\.?",
        s,
        flags=re.I,
    )
    if m:
        return f"{_frames_zh(m.group(1))}可取消到派生"

    m = re.fullmatch(
        r"Cancellable into (.+?) frames\s+([0-9~\-]+)\.?",
        s,
        flags=re.I,
    )
    if m:
        return f"{_frames_zh(m.group(2))}可取消到 {m.group(1)}"

    m = re.fullmatch(
        r"(.+?) cannot dash for\s+([0-9]+)\s+frames after recovery ends\.?",
        s,
        flags=re.I,
    )
    if m:
        return f"{m.group(1)}恢复结束后{m.group(2)}帧内不能冲刺"

    m = re.fullmatch(
        r"(.+?) cannot block for\s+([0-9]+)\s+frames after recovery ends\.?",
        s,
        flags=re.I,
    )
    if m:
        return f"{m.group(1)}恢复结束后{m.group(2)}帧内不能防御"

    m = re.fullmatch(
        r"Heals\s+([0-9+]+)\s+HP on block and\s+([0-9+]+)\s+HP on hit while Blood of the Dragon is active\.?",
        s,
        flags=re.I,
    )
    if m:
        return f"龙血状态下，防御时回复{m.group(1)}HP，命中时回复{m.group(2)}HP"

    m = re.fullmatch(
        r"Consumes\s+([0-9]+)\s+Dragon Gauge on frame\s+([0-9Ff]+)\.?",
        s,
        flags=re.I,
    )
    if m:
        return f"第{_frames_zh(m.group(2))}消耗{m.group(1)}点龙之能量"

    return ""


def note_id(character: str, section: str, input_name: str, move_name: str, note: str) -> str:
    payload = json.dumps(
        [character, section, input_name, move_name, note],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def frame_files() -> list[Path]:
    return sorted(FRAME_DIR.glob("*Frame2.local.json"))


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def note_file_path(character: str) -> Path:
    stem = re.sub(r"[^A-Za-z0-9]+", "", character)
    return NOTES_DIR / f"{stem}.notes.json"


def note_md_path(character: str) -> Path:
    stem = re.sub(r"[^A-Za-z0-9]+", "", character)
    return NOTES_DIR / f"{stem}.notes.md"


def _replace_motion(motion: str) -> str:
    for long, short in MOTION_REPLACEMENTS:
        if motion.startswith(long):
            return short + motion[len(long):]
    return motion


def _replace_buttons(text: str) -> str:
    return "".join(BUTTON_TO_QUERY.get(ch, ch.lower()) for ch in text)


def input_to_query(input_name: str, move_name: str = "") -> str:
    raw = str(input_name or "").strip()
    name = str(move_name or "").strip()
    if not raw:
        return ""

    if name in {"bc", "bc（防御中）"}:
        return name
    if raw == "Ground Throw":
        return "ab/ad"
    if raw == "d.Ground Throw":
        return "d.ab/d.ad"
    if raw == "Air Throw":
        return "jab/jad"
    if raw == "d.Air Throw":
        return "d.jab/d.jad"

    text = re.sub(r"\s+", "", raw)
    text = text.replace(".", "")

    suffix = ""
    m_suffix = re.search(r"(\[[gkGK]\])$", text)
    if m_suffix:
        suffix = m_suffix.group(1).lower()
        text = text[: -len(m_suffix.group(1))]

    prefix = ""
    if text.startswith("j"):
        prefix = "j"
        text = text[1:]

    hold_match = re.match(r"^\[(\d)\](\d+)(.*)$", text)
    if hold_match:
        hold, motion, rest = hold_match.groups()
        return prefix + hold + _replace_motion(motion) + _replace_buttons(rest) + suffix

    motion_match = re.match(r"^(\d+)(.*)$", text)
    if motion_match:
        motion, rest = motion_match.groups()
        return prefix + _replace_motion(motion) + _replace_buttons(rest) + suffix

    normal_match = re.match(r"^([cf]?)([LMHUX]+.*)$", text)
    if normal_match:
        normal_prefix, rest = normal_match.groups()
        return prefix + normal_prefix.lower() + _replace_buttons(rest) + suffix

    return compact(raw)


def iter_note_items(data: dict[str, Any]):
    character = str(data.get("character") or "")
    for item in data.get("rows", []) or []:
        note = str(item.get("notes") or "").strip()
        if not note:
            continue
        row = item.get("row") or {}
        input_name = str(row.get("Input") or "")
        move_name = str(row.get("Name") or "")
        section = str(item.get("section") or "")
        yield {
            "id": note_id(character, section, input_name, move_name, note),
            "character": character,
            "section": section,
            "query": input_to_query(input_name, move_name),
            "input": input_name,
            "name": move_name,
            "note": note,
            "noteZh": str(item.get("notesZh") or ""),
        }, item
