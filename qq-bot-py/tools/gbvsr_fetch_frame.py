"""Fetch GBVSR frame data from Dustloop MediaWiki API.

Usage:
  python tools/gbvsr_fetch_frame.py Katalina
  python tools/gbvsr_fetch_frame.py all --proxy http://127.0.0.1:7897

The script reads the rendered Frame Data HTML from MediaWiki, extracts frame
tables and expandable details, downloads original image files, and writes a
local JSON used by plugins/gbvsr_frame.py.
"""

from __future__ import annotations

import argparse
import copy
import html
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urljoin

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx
from bs4 import BeautifulSoup
from bs4.element import Tag

from gbvsr_registry import CHARACTER_NAMES, aliases_for, compact, normalize_canonical, resolve_character


API_URL = "https://www.dustloop.com/wiki/api.php"
SITE_ROOT = "https://www.dustloop.com"
DATA_ROOT = Path("data/gbvsr")
FRAME_DIR = DATA_ROOT / "frame"
IMAGE_ROOT = DATA_ROOT / "images"

EX_BASE_CHARACTER = {
    "Narmaya_(EX)": "Narmaya",
    "Gran_(EX)": "Gran",
    "Djeeta_(EX)": "Djeeta",
}

SECTION_HEADINGS = {
    "Normal Moves",
    "Unique Action",
    "Unique Mechanics",
    "Universal Mechanics",
    "Other",
    "Skills",
    "Skybound Arts",
}

UNIVERSAL_MOVE_INFO = {
    "groundthrow": {
        "aliases": ["投", "地投", "普通投", "throw", "gt"],
        "commands": ["L+U", "L+M"],
        "display": "L+U / L+M",
        "search_aliases": ["ab", "ad", "a+b", "a+d", "lu", "lm", "l+u", "l+m"],
    },
    "airthrow": {
        "aliases": ["空投", "空中投", "airthrow", "at"],
        "commands": ["j.L+U", "j.L+M"],
        "display": "j.L+U / j.L+M",
        "search_aliases": ["jab", "jad", "j.a+b", "j.a+d", "jlu", "jlm", "j.l+u", "j.l+m"],
    },
    "ragingstrike": {
        "aliases": ["rs", "红技", "红", "怒火强攻", "ragingstrike"],
        "commands": ["M+H"],
        "display": "bc",
        "search_aliases": ["bc", "b+c", "mh", "m+h"],
    },
    "ragingchain": {
        "aliases": ["rc", "红连", "怒火突袭", "ragingchain"],
        "commands": ["M+H"],
        "display": "bc",
        "search_aliases": ["bc", "b+c", "mh", "m+h"],
    },
    "bravecounter": {
        "aliases": ["反击", "英勇反击", "勇气反击", "bravecounter"],
        "commands": ["M+H"],
        "display": "bc（防御中）",
        "search_aliases": ["bc", "b+c", "mh", "m+h"],
    },
}


def _resolve_character(raw: str) -> str:
    resolved = resolve_character(raw)
    if resolved:
        return resolved
    raise SystemExit(f"Unknown character: {raw}")


def _characters_from_docs() -> list[str]:
    docs_path = Path("docs/gbf.md")
    if not docs_path.exists():
        return []
    out: list[str] = []
    seen: set[str] = set()
    for line in docs_path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\s*([^:：]+)\s*[:：]\s*(.+?)\s*[。.]?\s*$", line)
        if not m:
            continue
        name = resolve_character(m.group(1).strip()) or normalize_canonical(m.group(1).strip())
        key = compact(name)
        if key and key not in seen:
            seen.add(key)
            out.append(name)
    for name in CHARACTER_NAMES:
        key = compact(name)
        if key and key not in seen:
            seen.add(key)
            out.append(name)
    return out


def _frame_json_path(character: str) -> Path:
    stem = re.sub(r"[^A-Za-z0-9]+", "", character)
    return FRAME_DIR / f"{stem[:1].lower() + stem[1:]}Frame2.local.json"


def _image_dir(character: str) -> Path:
    return IMAGE_ROOT / character.replace(" ", "_")


def _clean_text(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _move_key(text: str) -> str:
    return compact(re.sub(r"\[[^\]]+\]$", "", text or ""))


def _command_aliases(command: str) -> list[str]:
    command = _clean_text(command)
    if not command:
        return []

    compacted = compact(command)
    no_plus = compacted.replace("+", "")
    aliases = [command, compacted, no_plus]

    if compacted.startswith("j"):
        aliases.append("j." + compacted[1:])
        aliases.append("j" + no_plus[1:])

    return list(dict.fromkeys(x for x in aliases if x))


def _expand_motion_aliases(token: str) -> set[str]:
    token = compact(token)
    aliases = {token}
    replacements = (
        ("632146", "6246"),
        ("63214", "624"),
        ("236236", "2626"),
        ("214214", "2424"),
        ("236", "26"),
        ("214", "24"),
    )

    for prefix in ("j", ""):
        body = token[len(prefix):] if prefix and token.startswith(prefix) else token if not prefix else ""
        if not body:
            continue
        for long, short in replacements:
            if body.startswith(long):
                aliases.add(prefix + short + body[len(long):])
            if body.startswith(short):
                aliases.add(prefix + long + body[len(short):])

    return aliases


def _aliases_for_move(move_name: str, commands: list[str] | None = None) -> list[str]:
    aliases: list[str] = []
    key = _move_key(move_name)
    info = UNIVERSAL_MOVE_INFO.get(key, {})
    if key:
        aliases.append(key)
        aliases.append(move_name)
        aliases.extend(info.get("aliases", []))
        aliases.extend(info.get("search_aliases", []))

    for command in commands or info.get("commands", []):
        aliases.extend(_command_aliases(command))

    aliases.extend(_expand_motion_aliases(move_name))

    return list(dict.fromkeys(str(x) for x in aliases if str(x).strip()))


def _universal_display_command(move_name: str, commands: list[str] | None = None) -> str:
    key = _move_key(move_name)
    info = UNIVERSAL_MOVE_INFO.get(key, {})
    return str(info.get("display") or " / ".join(commands or info.get("commands", [])))


def _extract_commands(text: str) -> list[str]:
    text = _clean_text(text)
    if not text:
        return []

    commands: list[str] = []
    patterns = [
        r"j\.\s*[LMHU]\s*\+\s*[LMHU](?:\s*\+\s*[LMHU])?",
        r"\b[LMHU]\s*\+\s*[LMHU](?:\s*\+\s*[LMHU])?\b",
    ]
    for pattern in patterns:
        for m in re.finditer(pattern, text):
            command = re.sub(r"\s+", "", m.group(0))
            command = command[:2].lower() + command[2:] if command.lower().startswith("j.") else command
            if command not in commands:
                commands.append(command)
    return commands


def _file_label_to_name(label: str) -> str:
    label = _clean_text(label)
    label = re.sub(r"^\d+px-", "", label)
    return label


def _extract_file_names(cell: Tag) -> list[dict[str, str]]:
    files: list[dict[str, str]] = []
    seen: set[str] = set()

    for fig in cell.select("figure"):
        label_node = fig.select_one("figcaption")
        label = _file_label_to_name(label_node.get_text(" ", strip=True) if label_node else "")
        img = fig.select_one("img")
        src = img.get("src", "") if img else ""
        link_node = fig.select_one("a.mw-file-description")
        link = link_node.get("href", "") if link_node else ""

        if not label and link.startswith("/w/File:"):
            label = unquote(link.rsplit("File:", 1)[-1])
        if not label and src:
            label = _file_label_to_name(unquote(src.rsplit("/", 1)[-1]))
        if not label or label in seen:
            continue

        seen.add(label)
        files.append(
            {
                "url": urljoin(SITE_ROOT, src) if src else "",
                "label": label,
                "filename": label,
            }
        )

    return files


def _parse_details(details_html: str) -> tuple[list[dict[str, str]], list[dict[str, str]], str]:
    if not details_html:
        return [], [], ""

    details = BeautifulSoup(html.unescape(details_html), "html.parser")
    images: list[dict[str, str]] = []
    hitboxes: list[dict[str, str]] = []
    notes = ""

    for tr in details.select("tr"):
        cells = tr.select("td")
        if len(cells) < 2:
            continue
        key = cells[0].get_text(" ", strip=True).replace(":", "").strip().lower()
        value = cells[1]
        if key == "images":
            images = _extract_file_names(value)
        elif key == "hitboxes":
            hitboxes = _extract_file_names(value)
        elif key == "notes":
            notes = _clean_text(value.get_text(" ", strip=True))

    return images, hitboxes, notes


def _split_files_by_type(container: Tag) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    images: list[dict[str, str]] = []
    hitboxes: list[dict[str, str]] = []
    for item in _extract_file_names(container):
        filename = item.get("filename") or item.get("label") or ""
        if "hitbox" in filename.lower():
            hitboxes.append(item)
        else:
            images.append(item)
    return images, hitboxes


def _extract_section(table: Tag) -> str:
    current = table.previous_sibling
    steps = 0
    while current is not None and steps < 80:
        steps += 1
        if isinstance(current, Tag):
            heading = current.select_one("h2,h3")
            if heading:
                text = heading.get_text(" ", strip=True)
                if text:
                    return text
        current = current.previous_sibling
    return ""


def _normalize_header(header: str) -> str:
    raw = _clean_text(header)
    key = re.sub(r"[\s\u3000._-]+", "", raw).lower()
    mapping = {
        "": "",
        "input": "Input",
        "name": "Name",
        "damage": "Damage",
        "guard": "Guard",
        "startup": "Startup",
        "active": "Active",
        "recovery": "Recovery",
        "onblock": "On-Block",
        "onhit": "On-Hit",
        "onch": "On Counter Hit",
        "oncounterhit": "On Counter Hit",
        "level": "Level",
        "invuln": "Invuln",
        "sbggain": "SBG Gain",
        "combolimitscaling": "Combo Limit Scaling",
    }
    return mapping.get(key, raw)


def _heading_level(heading: Tag) -> int:
    m = re.match(r"h([1-6])", heading.name or "")
    return int(m.group(1)) if m else 6


def _heading_text(heading: Tag) -> str:
    headline = heading.select_one(".mw-headline")
    text = headline.get_text(" ", strip=True) if headline else heading.get_text(" ", strip=True)
    text = re.sub(r"\s*\[\s*edit\s*\]\s*$", "", text, flags=re.I)
    return _clean_text(text)


def _table_headers(table: Tag) -> list[str]:
    headers = [_normalize_header(th.get_text(" ", strip=True)) for th in table.select("thead th")]
    if headers:
        return headers
    first_row = table.select_one("tr")
    if not first_row:
        return []
    return [_normalize_header(th.get_text(" ", strip=True)) for th in first_row.select("th")]


def _heading_container(heading: Tag) -> Tag:
    parent = heading.parent
    if isinstance(parent, Tag) and "mw-heading" in " ".join(parent.get("class", [])):
        return parent
    return heading


def _collect_block_after_heading(heading: Tag) -> BeautifulSoup:
    level = _heading_level(heading)
    container = _heading_container(heading)
    chunks: list[str] = []

    current = container.next_sibling
    while current is not None:
        if isinstance(current, Tag):
            next_heading = current if re.fullmatch(r"h[1-6]", current.name or "") else current.select_one("h2,h3,h4,h5,h6")
            if next_heading and _heading_level(next_heading) <= level:
                break
        chunks.append(str(current))
        current = current.next_sibling

    return BeautifulSoup("".join(chunks), "html.parser")


def _first_stats_row(block: BeautifulSoup) -> dict[str, str]:
    for table in block.select("table"):
        headers = _table_headers(table)
        if "Damage" not in headers:
            continue
        tr = table.select_one("tbody tr")
        if not tr:
            all_rows = table.select("tr")
            tr = all_rows[1] if len(all_rows) > 1 else None
        if not tr:
            continue
        cells = tr.select("td")
        values = [_clean_text(td.get_text(" ", strip=True)) for td in cells]
        if len(values) < len(headers):
            values.extend([""] * (len(headers) - len(values)))
        return {headers[i]: values[i] if i < len(values) else "" for i in range(len(headers))}
    return {}


def _extract_block_notes(block: BeautifulSoup) -> str:
    notes: list[str] = []
    for li in block.select("li"):
        text = _clean_text(li.get_text(" ", strip=True))
        if not text:
            continue
        if text.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
            continue
        if text not in notes:
            notes.append(text)
    return ";".join(notes)


def _parse_main_universal_rows(character: str, text_html: str, old_notes_zh: dict[str, str]) -> dict[str, dict[str, Any]]:
    soup = BeautifulSoup(text_html, "html.parser")
    out: dict[str, dict[str, Any]] = {}

    for heading in soup.select("h2,h3,h4"):
        move_name = _heading_text(heading)
        key = _move_key(move_name)
        if key not in UNIVERSAL_MOVE_INFO:
            continue

        block = _collect_block_after_heading(heading)
        block_text = block.get_text(" ", strip=True)
        commands = _extract_commands(block_text)
        row = _first_stats_row(block)
        if not row:
            continue

        command_label = _universal_display_command(move_name, commands)
        row["Input"] = move_name
        row["Name"] = command_label

        images, hitboxes = _split_files_by_type(block)
        notes = _extract_block_notes(block)
        notes_zh = old_notes_zh.get(move_name, "") or old_notes_zh.get(command_label, "")

        out[key] = {
            "section": "Universal Mechanics",
            "row": row,
            "images": images,
            "hitboxes": hitboxes,
            "notes": notes,
            "notesZh": notes_zh,
            "imagePaths": [],
            "hitboxPaths": [],
            "aliases": _aliases_for_move(move_name, commands),
        }

    return out


def _parse_frame_rows(character: str, text_html: str, old_notes_zh: dict[str, str]) -> list[dict[str, Any]]:
    soup = BeautifulSoup(text_html, "html.parser")
    rows: list[dict[str, Any]] = []

    for table in soup.select("table"):
        headers = _table_headers(table)
        if "Damage" not in headers or ("Input" not in headers and "Name" not in headers):
            continue

        section = _extract_section(table)
        if section not in SECTION_HEADINGS:
            section = section or "Frame Data"

        for tr in table.select("tbody tr"):
            cells = tr.select("td")
            if not cells:
                continue
            values = [_clean_text(td.get_text(" ", strip=True)) for td in cells]
            if len(values) < len(headers):
                values.extend([""] * (len(headers) - len(values)))
            row = {headers[i]: values[i] if i < len(values) else "" for i in range(len(headers))}
            input_name = row.get("Input", "")
            if not input_name and row.get("Name"):
                input_name = row.get("Name", "")
                row["Input"] = input_name
                row["Name"] = _universal_display_command(input_name)
            if not input_name:
                continue

            images, hitboxes, notes = _parse_details(tr.get("data-mw-details") or "")
            notes_zh = old_notes_zh.get(input_name, "")
            rows.append(
                {
                    "section": section,
                    "row": row,
                    "images": images,
                    "hitboxes": hitboxes,
                    "notes": notes,
                    "notesZh": notes_zh,
                    "imagePaths": [],
                    "hitboxPaths": [],
                    "aliases": _aliases_for_move(input_name),
                }
            )

    return rows


def _merge_main_universal_rows(rows: list[dict[str, Any]], main_rows: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    if not main_rows:
        return rows

    merged: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for item in rows:
        row = item.get("row") or {}
        key = _move_key(str(row.get("Input") or row.get("Name") or ""))
        main_item = main_rows.get(key)
        if main_item:
            copied = copy.deepcopy(item)
            copied_row = copied.setdefault("row", {})
            main_row = main_item.get("row") or {}
            copied["section"] = main_item.get("section") or copied.get("section") or "Universal Mechanics"
            copied_row["Input"] = main_row.get("Input") or copied_row.get("Input") or ""
            copied_row["Name"] = main_row.get("Name") or copied_row.get("Name") or ""
            copied["images"] = main_item.get("images") or copied.get("images") or []
            copied["hitboxes"] = main_item.get("hitboxes") or copied.get("hitboxes") or []
            copied["notes"] = main_item.get("notes") or copied.get("notes") or ""
            copied["notesZh"] = main_item.get("notesZh") or copied.get("notesZh") or ""
            copied["aliases"] = list(dict.fromkeys((copied.get("aliases") or []) + (main_item.get("aliases") or [])))
            merged.append(copied)
            seen_keys.add(key)
        else:
            merged.append(item)

    for key, item in main_rows.items():
        if key not in seen_keys:
            merged.append(item)

    return merged


def _is_inheritable_normal(input_name: str) -> bool:
    text = (input_name or "").strip()
    return bool(
        re.fullmatch(
            r"(?:[cfj]\.?[LMHUlmhu]|[256][LMHUlmhu]|66[LMHUlmhu]|c\.XX(?:X|6M|6H)?)(?:\[[gkGK]\])?",
            text,
        )
    )


def _normal_key(input_name: str) -> str:
    return compact(input_name)


def _merge_ex_base_normals(character: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    base_character = EX_BASE_CHARACTER.get(character)
    if not base_character:
        return rows

    base_path = _frame_json_path(base_character)
    if not base_path.exists():
        return rows

    try:
        base_data = json.loads(base_path.read_text(encoding="utf-8"))
    except Exception:
        return rows

    existing = {
        _normal_key(str((item.get("row") or {}).get("Input") or ""))
        for item in rows
        if _is_inheritable_normal(str((item.get("row") or {}).get("Input") or ""))
    }

    inherited: list[dict[str, Any]] = []
    for item in base_data.get("rows", []):
        input_name = str((item.get("row") or {}).get("Input") or "")
        if not _is_inheritable_normal(input_name):
            continue
        key = _normal_key(input_name)
        if key in existing:
            continue

        copied = copy.deepcopy(item)
        copied["section"] = str(copied.get("section") or "Normal Moves")
        copied["inheritedFrom"] = base_character
        inherited.append(copied)
        existing.add(key)

    if not inherited:
        return rows

    insert_at = 0
    for i, item in enumerate(rows):
        if str(item.get("section") or "") in {"Normal Moves", "Genji Moves", "Kagura Moves", "Shared Moves"}:
            insert_at = i + 1

    return rows[:insert_at] + inherited + rows[insert_at:]


def _old_notes_zh(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

    out: dict[str, str] = {}
    for item in data.get("rows", []):
        input_name = str((item.get("row") or {}).get("Input") or "")
        notes_zh = str(item.get("notesZh") or "")
        if input_name and notes_zh:
            out[input_name] = notes_zh
    return out


async def _fetch_parse(client: httpx.AsyncClient, character: str) -> dict[str, Any]:
    page = f"GBVSR/{character}/Frame_Data"
    return await _fetch_parse_page(client, page)


async def _fetch_parse_page(client: httpx.AsyncClient, page: str) -> dict[str, Any]:
    resp = await client.get(
        API_URL,
        params={
            "action": "parse",
            "page": page,
            "prop": "text|images",
            "format": "json",
            "formatversion": "2",
        },
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(data["error"])
    return data["parse"]


async def _fetch_imageinfo(client: httpx.AsyncClient, filename: str) -> dict[str, str]:
    resp = await client.get(
        API_URL,
        params={
            "action": "query",
            "titles": f"File:{filename}",
            "prop": "imageinfo",
            "iiprop": "url|mime|size",
            "format": "json",
            "formatversion": "2",
        },
    )
    resp.raise_for_status()
    data = resp.json()
    pages = data.get("query", {}).get("pages", [])
    if not pages:
        return {}
    info = (pages[0].get("imageinfo") or [{}])[0]
    return {
        "originalUrl": str(info.get("url") or ""),
        "mime": str(info.get("mime") or ""),
        "size": str(info.get("size") or ""),
    }


async def _download_file(client: httpx.AsyncClient, url: str, path: Path, force: bool = False) -> None:
    if not url:
        return
    if path.exists() and path.stat().st_size > 500 and not force:
        return

    resp = await client.get(url)
    resp.raise_for_status()
    if len(resp.content) <= 500:
        raise RuntimeError(f"Downloaded file too small: {url}")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(resp.content)


async def _localize_images(
    client: httpx.AsyncClient,
    character: str,
    rows: list[dict[str, Any]],
    force: bool,
) -> None:
    img_dir = _image_dir(character)
    info_cache: dict[str, dict[str, str]] = {}

    async def localize_item(item: dict[str, str]) -> str:
        filename = item.get("filename") or item.get("label") or ""
        filename = _file_label_to_name(filename)
        if not filename:
            return ""

        info = info_cache.get(filename)
        if info is None:
            info = await _fetch_imageinfo(client, filename)
            info_cache[filename] = info
        item.update(info)

        url = item.get("originalUrl") or item.get("url") or ""
        path = img_dir / filename
        if url:
            try:
                await _download_file(client, url, path, force=force)
            except Exception as e:
                print(f"WARN {character}: image download failed {filename}: {type(e).__name__}: {e}")
        if not path.exists() or path.stat().st_size <= 500:
            item.pop("localPath", None)
            return ""

        local_path = str(path.as_posix())
        item["localPath"] = local_path
        return local_path

    for row in rows:
        image_paths: list[str] = []
        for item in row.get("images", []):
            local_path = await localize_item(item)
            if local_path:
                image_paths.append(local_path)

        hitbox_paths: list[str] = []
        for item in row.get("hitboxes", []):
            local_path = await localize_item(item)
            if local_path:
                hitbox_paths.append(local_path)

        row["imagePaths"] = image_paths
        row["hitboxPaths"] = hitbox_paths


async def fetch_character(character: str, proxy: str | None, force_images: bool) -> Path:
    output_path = _frame_json_path(character)
    old_notes = _old_notes_zh(output_path)

    async with httpx.AsyncClient(
        timeout=60,
        follow_redirects=True,
        proxy=proxy,
        headers={"User-Agent": "qq-bot-py gbvsr-frame-fetcher/1.0"},
    ) as client:
        parsed = await _fetch_parse(client, character)
        rows = _parse_frame_rows(character, parsed["text"], old_notes)
        try:
            main_parsed = await _fetch_parse_page(client, f"GBVSR/{character}")
            main_universal_rows = _parse_main_universal_rows(character, main_parsed["text"], old_notes)
            rows = _merge_main_universal_rows(rows, main_universal_rows)
        except Exception as e:
            print(f"WARN {character}: main page universal parse failed: {type(e).__name__}: {e}")
        rows = _merge_ex_base_normals(character, rows)
        if not rows:
            raise RuntimeError(f"No frame rows parsed for {character}")
        await _localize_images(client, character, rows, force=force_images)

    FRAME_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "character": character,
        "source": f"https://www.dustloop.com/w/GBVSR/{quote(character)}/Frame_Data",
        "capturedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "rows": rows,
    }
    output_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output_path


async def _amain() -> None:
    parser = argparse.ArgumentParser(description="Fetch GBVSR frame data from Dustloop.")
    parser.add_argument("character", help="Character name, alias, or 'all'.")
    parser.add_argument("--proxy", default="", help="HTTP proxy, e.g. http://127.0.0.1:7897")
    parser.add_argument("--force-images", action="store_true", help="Redownload existing image files.")
    args = parser.parse_args()

    if compact(args.character) == "all":
        targets = _characters_from_docs() or CHARACTER_NAMES
    else:
        targets = [_resolve_character(args.character)]
    for character in targets:
        try:
            path = await fetch_character(character, args.proxy or None, args.force_images)
            print(f"OK {character}: {path}")
        except Exception as e:
            print(f"SKIP {character}: {type(e).__name__}: {e}")


def main() -> None:
    import asyncio

    asyncio.run(_amain())


if __name__ == "__main__":
    main()
