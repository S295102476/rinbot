"""Fetch GBVSR character overview data from Dustloop.

Usage:
  python tools/gbvsr_fetch_overview.py --proxy http://127.0.0.1:7897

The script reads the rendered global Frame Data page from MediaWiki, extracts
character movement/stat tables, downloads portrait images, and writes a local
JSON consumed by tools/gbvsr_import_overview_db.py and plugins/gbvsr_frame.py.
"""

from __future__ import annotations

import argparse
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

from gbvsr_registry import CHARACTER_NAMES, compact, resolve_character


API_URL = "https://www.dustloop.com/wiki/api.php"
SITE_ROOT = "https://www.dustloop.com"
DATA_ROOT = Path("data/gbvsr")
OVERVIEW_DIR = DATA_ROOT / "overview"
IMAGE_ROOT = DATA_ROOT / "images"
OUTPUT_PATH = OVERVIEW_DIR / "gbvsr_overview.local.json"

EX_BASE_CHARACTER = {
    "Narmaya_(EX)": "Narmaya",
    "Gran_(EX)": "Gran",
    "Djeeta_(EX)": "Djeeta",
}

FIELD_ALIASES = {
    "health": {"health", "hp"},
    "backdash": {"backdash"},
    "jump_startup": {"prejump", "jumpstartup"},
    "walk_speed": {"walkspeed"},
    "backwalk_speed": {"backwalkspeed"},
    "initial_dash_speed": {"initialdashspeed"},
    "dash_acceleration": {"dashacceleration"},
    "jump_height": {"jumpheight"},
    "forward_jump_distance": {"forwardjumpdistance"},
    "backward_jump_distance": {"backwardjumpdistance"},
    "superjump_height": {"superjumpheight"},
    "forward_superjump_distance": {"forwardsuperjumpdistance"},
    "backward_superjump_distance": {"backwardsuperjumpdistance"},
    "cl_proximity_range": {"clrange", "clproximityrange"},
    "cm_proximity_range": {"cmrange", "cmproximityrange", "cmpromximityrange"},
    "ch_proximity_range": {"chrange", "chproximityrange"},
}


def _clean_text(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _header_key(text: str) -> str:
    return re.sub(r"[\s\u3000._()/-]+", "", _clean_text(text)).lower()


def _field_from_header(header: str) -> str | None:
    key = _header_key(header)
    for field, aliases in FIELD_ALIASES.items():
        if key in aliases:
            return field
    return None


def _character_from_cell(cell: Tag) -> tuple[str, str] | None:
    link = cell.select_one('a[href*="/w/GBVSR/"]')
    href = link.get("href", "") if link else ""
    text = _clean_text(cell.get_text(" ", strip=True))

    candidates: list[str] = []
    if href:
        m = re.search(r"/w/GBVSR/([^/#?]+)", href)
        if m:
            candidates.append(unquote(m.group(1)))
    if text:
        candidates.append(re.sub(r"\s*\([^)]*\)\s*$", "", text).strip())

    for candidate in candidates:
        if not candidate:
            continue
        resolved = resolve_character(candidate)
        if resolved:
            return resolved, text or resolved
        key = compact(candidate)
        for known in CHARACTER_NAMES:
            if compact(known) == key:
                return known, text or known
    return None


def _table_headers(table: Tag) -> list[str]:
    headers = [_clean_text(th.get_text(" ", strip=True)) for th in table.select("thead th")]
    if headers:
        return headers
    first_row = table.select_one("tr")
    if not first_row:
        return []
    return [_clean_text(th.get_text(" ", strip=True)) for th in first_row.select("th")]


def _extract_table_rows(parsed_html: str) -> dict[str, dict[str, Any]]:
    soup = BeautifulSoup(parsed_html, "html.parser")
    out: dict[str, dict[str, Any]] = {}

    for table in soup.select("table"):
        headers = _table_headers(table)
        if not headers:
            continue

        character_index = next((i for i, h in enumerate(headers) if _header_key(h) == "character"), -1)
        field_by_index = {i: field for i, h in enumerate(headers) if (field := _field_from_header(h))}
        if character_index < 0 or not field_by_index:
            continue

        for tr in table.select("tbody tr"):
            cells = tr.select("td")
            if len(cells) <= character_index:
                continue

            char_info = _character_from_cell(cells[character_index])
            if not char_info:
                continue
            character, display_name = char_info
            # State-specific rows share the base page link, but their values
            # must not replace the default character stats. EX is a character.
            state = re.search(r"\(([^)]+)\)\s*$", display_name)
            if state and state.group(1).casefold() != "ex":
                continue

            item = out.setdefault(
                character,
                {
                    "character": character,
                    "displayName": display_name or character,
                    "portrait": {},
                    "portraitPath": "",
                    "stats": {},
                    "raw": {},
                },
            )
            if display_name and not item.get("displayName"):
                item["displayName"] = display_name

            for index, field in field_by_index.items():
                if index >= len(cells):
                    continue
                value = _clean_text(cells[index].get_text(" ", strip=True))
                if not value:
                    continue
                item["stats"][field] = value
                item["raw"][headers[index]] = value

    return out


async def _fetch_parse_page(client: httpx.AsyncClient, page: str, props: str = "text|images") -> dict[str, Any]:
    resp = await client.get(
        API_URL,
        params={
            "action": "parse",
            "page": page,
            "prop": props,
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
    if not pages or pages[0].get("missing"):
        return {}
    info = (pages[0].get("imageinfo") or [{}])[0]
    return {
        "originalUrl": str(info.get("url") or ""),
        "mime": str(info.get("mime") or ""),
        "size": str(info.get("size") or ""),
    }


def _file_label_to_name(label: str) -> str:
    label = _clean_text(label)
    return re.sub(r"^\d+px-", "", label)


def _portrait_candidates_from_main_page(parsed_html: str) -> list[str]:
    soup = BeautifulSoup(parsed_html, "html.parser")
    candidates: list[str] = []
    for img in soup.select("img"):
        alt = img.get("alt", "")
        src = img.get("src", "")
        if "portrait" not in (alt + " " + src).lower():
            continue
        filename = ""
        link = img.find_parent("a")
        href = link.get("href", "") if isinstance(link, Tag) else ""
        if href.startswith("/w/File:"):
            filename = unquote(href.rsplit("File:", 1)[-1])
        if not filename and src:
            filename = _file_label_to_name(unquote(src.rsplit("/", 1)[-1]))
        if filename and filename not in candidates:
            candidates.append(filename)
    return candidates


def _image_dir(character: str) -> Path:
    return IMAGE_ROOT / character.replace(" ", "_")


async def _download_file(client: httpx.AsyncClient, url: str, path: Path, force: bool = False) -> bool:
    if not url:
        return False
    if path.exists() and path.stat().st_size > 500 and not force:
        return True
    resp = await client.get(url)
    resp.raise_for_status()
    if len(resp.content) <= 500:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(resp.content)
    return True


async def _localize_portrait(
    client: httpx.AsyncClient,
    character: str,
    force: bool,
) -> tuple[dict[str, str], str]:
    portrait_character = EX_BASE_CHARACTER.get(character, character)
    page_character = portrait_character
    filename_stem = portrait_character.replace(" ", "_")
    candidates = [f"GBVSR_{filename_stem}_Portrait.png"]

    try:
        main = await _fetch_parse_page(client, f"GBVSR/{page_character}", props="text|images")
        candidates.extend(_portrait_candidates_from_main_page(main.get("text", "")))
    except Exception as e:
        print(f"WARN {character}: portrait main page parse failed: {type(e).__name__}: {e}")

    seen: set[str] = set()
    for filename in candidates:
        filename = _file_label_to_name(filename)
        if not filename or filename in seen:
            continue
        seen.add(filename)

        info = await _fetch_imageinfo(client, filename)
        url = info.get("originalUrl") or ""
        if not url:
            continue

        path = _image_dir(portrait_character) / filename
        try:
            ok = await _download_file(client, url, path, force=force)
        except Exception as e:
            print(f"WARN {character}: portrait download failed {filename}: {type(e).__name__}: {e}")
            continue
        if not ok or not path.exists():
            continue

        portrait = {
            "filename": filename,
            "originalUrl": url,
            "mime": info.get("mime", ""),
            "size": info.get("size", ""),
            "localPath": path.as_posix(),
        }
        return portrait, path.as_posix()

    return {}, ""


def _apply_ex_fallbacks(items: dict[str, dict[str, Any]]) -> None:
    for ex_character, base_character in EX_BASE_CHARACTER.items():
        if ex_character not in items and base_character in items:
            copied = json.loads(json.dumps(items[base_character], ensure_ascii=False))
            copied["character"] = ex_character
            copied["displayName"] = ex_character
            items[ex_character] = copied

        if ex_character in items and base_character in items:
            ex_item = items[ex_character]
            base_item = items[base_character]
            for key, value in base_item.get("stats", {}).items():
                ex_item.setdefault("stats", {}).setdefault(key, value)
            if not ex_item.get("portraitPath"):
                ex_item["portrait"] = base_item.get("portrait") or {}
                ex_item["portraitPath"] = base_item.get("portraitPath") or ""


async def fetch_overview(proxy: str | None, force_images: bool) -> Path:
    async with httpx.AsyncClient(
        timeout=60,
        follow_redirects=True,
        proxy=proxy,
        headers={"User-Agent": "qq-bot-py gbvsr-overview-fetcher/1.0"},
    ) as client:
        parsed = await _fetch_parse_page(client, "GBVSR/Frame_Data")
        items = _extract_table_rows(parsed["text"])

        for character in sorted(items):
            portrait, portrait_path = await _localize_portrait(client, character, force_images)
            if portrait:
                items[character]["portrait"] = portrait
                items[character]["portraitPath"] = portrait_path
            print(f"OK {character}: overview")

        _apply_ex_fallbacks(items)

    OVERVIEW_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "source": "https://www.dustloop.com/w/GBVSR/Frame_Data",
        "capturedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "characters": [items[key] for key in sorted(items)],
    }
    OUTPUT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return OUTPUT_PATH


async def _amain() -> None:
    parser = argparse.ArgumentParser(description="Fetch GBVSR overview data from Dustloop.")
    parser.add_argument("--proxy", default="", help="HTTP proxy, e.g. http://127.0.0.1:7897")
    parser.add_argument("--force-images", action="store_true", help="Redownload existing portrait images.")
    args = parser.parse_args()

    path = await fetch_overview(args.proxy or None, args.force_images)
    print(f"DONE: {path}")


def main() -> None:
    import asyncio

    asyncio.run(_amain())


if __name__ == "__main__":
    main()
