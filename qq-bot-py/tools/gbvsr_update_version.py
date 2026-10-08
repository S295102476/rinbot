"""Stage a Dustloop patch update, preserving local edits and reporting conflicts.

Run from the project directory. --apply installs the validated staged data.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import shutil
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup
from PIL import Image

import gbvsr_fetch_frame as frame
import gbvsr_fetch_overview as overview
from gbvsr_note_utils import auto_translate_sentence, split_note
from gbvsr_registry import CHARACTER_NAMES, resolve_character


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def row_keys(rows):
    counts = Counter()
    result = {}
    for item in rows:
        name = item["row"]["Input"]
        counts[name] += 1
        result[(name, counts[name])] = item
    return result


def merge_local(character, rows, old_rows, glossary, report):
    old = row_keys(old_rows)
    new = row_keys(rows)
    for key, item in new.items():
        previous = old.get(key)
        if not previous:
            matches = [r for r in old_rows if key[0] in r.get("aliases", [])]
            if len(matches) == 1:
                previous = matches[0]
                item["row"]["Input"] = previous["row"]["Input"]
        if previous:
            for field in ("moveName", "move_name", "displayName"):
                if field in previous:
                    item[field] = previous[field]
            if re.search(r"[\u4e00-\u9fff]", previous["row"].get("Name", "")):
                item["row"]["Name"] = previous["row"]["Name"]
            item["aliases"] = list(dict.fromkeys(item.get("aliases", []) + previous.get("aliases", [])))
            if split_note(item.get("notes", "")) == split_note(previous.get("notes", "")):
                item["notesZh"] = previous.get("notesZh", "")
            elif previous.get("notesZh"):
                item["notesZh"] = ""
                report["notesToReview"].append({
                    "character": character, "input": key[0], "occurrence": key[1],
                    "oldNote": previous.get("notes", ""), "oldNoteZh": previous["notesZh"],
                    "newNote": item.get("notes", ""),
                })
            # These expanded values are user-authored; 2.61 does not change Light Wall.
            if report["version"] == "2.61" and character == "Katalina" and key[0] == "5U":
                for field in ("Name", "On-Block", "On-Hit", "On Counter Hit", "Invuln"):
                    item["row"][field] = previous["row"].get(field, "")
        if not item.get("notesZh"):
            parts = split_note(item.get("notes", ""))
            translations = [glossary.get(part) or auto_translate_sentence(part) for part in parts]
            if translations and all(translations):
                item["notesZh"] = "\n".join(translations)
        if previous:
            changes = {field: {"old": previous["row"].get(field, ""), "new": value}
                       for field, value in item["row"].items()
                       if field and value != previous["row"].get(field, "")}
            if changes:
                report["changes"].append({"character": character, "input": key[0], "fields": changes})
        else:
            report["addedMoves"].append({"character": character, "input": key[0]})
    used_inputs = {r["row"]["Input"] for r in rows}
    for key, item in old.items():
        if key not in new and key[0] not in used_inputs:
            report["removedMoves"].append({"character": character, "input": key[0], "old": item})


class Update:
    def __init__(self, args):
        self.args = args
        self.root = frame.ROOT
        self.backup = self.root / "backups" / f"gbvsr-{args.version}-{args.snapshot}"
        self.stage = self.backup / "staged"
        self.sources = self.backup / "sources"
        self.report = {"version": args.version, "patchSource": f"https://www.dustloop.com/w/GBVSR/Version/{args.version}",
                       "characters": [], "changes": [], "addedMoves": [], "removedMoves": [],
                       "notesToReview": [], "missingImages": [], "imageFailures": [], "overviewMissingFields": {}}

    async def request(self, client, url, **kwargs):
        for attempt in range(3):
            try:
                response = await client.get(url, **kwargs)
                response.raise_for_status()
                return response
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code not in (429, 500, 502, 503, 504, 520, 522, 524):
                    raise
                if attempt == 2:
                    raise
                await asyncio.sleep(attempt + 1)

    async def parse(self, client, page, cache, **params):
        path = self.sources / cache
        if path.exists():
            data = read_json(path)
        else:
            query = {"action": "parse", "page": page, "prop": "text|images|revid",
                     "format": "json", "formatversion": 2, **params}
            if "text" in query:
                query.pop("page")
                query["title"] = page
            data = (await self.request(client, frame.API_URL, params=query)).json()
            write_json(path, data)
        if "error" in data:
            if data["error"]["code"] == "missingtitle":
                return None
            raise RuntimeError(data["error"])
        return data["parse"]

    async def fetch_rows(self, client, character):
        stem = re.sub(r"[^a-z0-9]", "", character.lower())
        parsed = await self.parse(client, f"GBVSR/{character}/Frame_Data", stem + "-frame.json")
        source = f"https://www.dustloop.com/w/GBVSR/{quote(character)}/Frame_Data"
        if parsed is None:
            # Render Dustloop's own Cargo template when a new character lacks a subpage.
            query = (await self.request(client, frame.API_URL, params={
                "action": "cargoquery", "tables": "MoveData_GBVSR", "fields": "input,type",
                "where": f'chara="{character.replace("_", " ")}"', "limit": 500, "format": "json",
            })).json()
            if "error" in query or not query.get("cargoquery"):
                raise RuntimeError(f"No Cargo moves for {character}: {query}")
            types = list(dict.fromkeys(r["title"]["type"] for r in query["cargoquery"]))
            sections = {"normal": "Normal Moves", "unique": "Unique Action", "other": "Other",
                        "special": "Skills", "super": "Skybound Arts"}
            types.sort(key=lambda value: list(sections).index(value) if value in sections else 99)
            text = "\n".join(f"=={sections.get(kind, kind)}==\n"
                             + "{{GBVSR-FullFrameDataTable|chara=" + character.replace('_', ' ') + "|moveType=" + kind + "}}"
                             for kind in types)
            parsed = await self.parse(client, f"GBVSR/{character}/Frame_Data", stem + "-cargo-rendered.json", text=text)
            source = f"https://www.dustloop.com/w/GBVSR/{quote(character)}"
            if len(frame._parse_frame_rows(character, parsed["text"], {})) != len(query["cargoquery"]):
                raise RuntimeError(f"Incomplete Cargo parse for {character}")
        rows = frame._parse_frame_rows(character, parsed["text"], {})
        main = await self.parse(client, f"GBVSR/{character}", stem + "-main.json")
        if main:
            rows = frame._merge_main_universal_rows(rows, frame._parse_main_universal_rows(character, main["text"], {}))
        rows = frame._merge_ex_base_normals(character, rows)
        return rows, source, parsed.get("revid")

    async def imageinfo(self, client, filenames):
        result = {}
        names = sorted(set(filenames))
        for offset in range(0, len(names), 40):
            batch = names[offset:offset + 40]
            cache = self.sources / ("images-" + hashlib.sha1("|".join(batch).encode()).hexdigest() + ".json")
            if cache.exists():
                data = read_json(cache)
            else:
                data = (await self.request(client, frame.API_URL, params={
                    "action": "query", "titles": "|".join("File:" + n for n in batch),
                    "prop": "imageinfo", "iiprop": "url|mime|size|sha1", "format": "json", "formatversion": 2,
                })).json()
                if "error" in data:
                    raise RuntimeError(data["error"])
                write_json(cache, data)
            normalized = {x["from"]: x["to"] for x in data.get("query", {}).get("normalized", [])}
            pages = {p["title"]: p for p in data.get("query", {}).get("pages", [])}
            for filename in batch:
                title = "File:" + filename
                page = pages.get(normalized.get(title, title), {})
                result[filename] = (page.get("imageinfo") or [{}])[0]
        return result

    async def localize(self, client, datasets, portraits):
        uses = []
        for character, data in datasets.items():
            for row in data["rows"]:
                for field in ("images", "hitboxes"):
                    for item in row[field]:
                        uses.append((character, item, row["row"]["Input"]))
        uses.extend((char, item, "portrait") for char, item in portraits.items())
        info = await self.imageinfo(client, [i["filename"] for _, i, _ in uses])
        downloaded = set()
        for character, item, move in uses:
            filename = item["filename"]
            if Path(filename).name != filename:
                raise ValueError(f"Unexpected image filename: {filename}")
            relative = Path("data/gbvsr/images") / character.replace(" ", "_") / filename
            old_path = self.root / relative
            new_path = self.stage / relative
            remote = info.get(filename, {})
            if not remote.get("url"):
                self.report["missingImages"].append({"character": character, "input": move, "file": filename})
                if old_path.is_file():
                    with Image.open(old_path) as image:
                        image.verify()
                    item["localPath"] = relative.as_posix()
                continue
            if not str(remote.get("mime", "")).startswith("image/"):
                raise ValueError(f"Unexpected image: {filename}")
            item.update({"originalUrl": remote["url"], "mime": remote["mime"], "size": str(remote["size"]), "sha1": remote.get("sha1", "")})
            selected = new_path if new_path.exists() else old_path
            if not selected.exists() or hashlib.sha1(selected.read_bytes()).hexdigest() != remote.get("sha1"):
                try:
                    response = await self.request(client, remote["url"])
                    content = response.content
                    if hashlib.sha1(content).hexdigest() != remote.get("sha1"):
                        raise ValueError("Original image checksum mismatch")
                    new_path.parent.mkdir(parents=True, exist_ok=True)
                    new_path.write_bytes(content)
                    with Image.open(new_path) as image:
                        image.verify()
                    selected = new_path
                    downloaded.add(relative.as_posix())
                    if len(downloaded) % 25 == 0:
                        print(f"IMAGES downloaded={len(downloaded)} latest={filename}", flush=True)
                except Exception as exc:
                    self.report["imageFailures"].append({"character": character, "input": move, "file": filename, "error": str(exc)})
                    continue
            item["localPath"] = relative.as_posix()
        for data in datasets.values():
            for row in data["rows"]:
                for field, paths in (("images", "imagePaths"), ("hitboxes", "hitboxPaths")):
                    row[paths] = list(dict.fromkeys(i["localPath"] for i in row[field] if i.get("localPath")))
        self.report["downloadedImages"] = sorted(downloaded)
        self.report["stagedImages"] = sorted(
            p.relative_to(self.stage).as_posix()
            for p in (self.stage / "data/gbvsr/images").rglob("*") if p.is_file()
        )

    async def run(self):
        self.sources.mkdir(parents=True, exist_ok=True)
        for folder in ("frame", "overview"):
            destination = self.backup / folder
            if not destination.exists():
                shutil.copytree(self.root / "data/gbvsr" / folder, destination)
        glossary = {x["text"]: x.get("zh", "") for x in read_json(self.root / "data/gbvsr/notes/_glossary.json")["entries"]}
        async with httpx.AsyncClient(proxy=self.args.proxy or None, timeout=40, follow_redirects=True,
                                     headers={"User-Agent": "qq-bot-py gbvsr-update/1.0"}) as client:
            patch = await self.parse(client, f"GBVSR/Version/{self.args.version}", "patch.json", prop="text|wikitext|revid")
            self.report["patchRevision"] = patch.get("revid")
            headings = BeautifulSoup(patch["text"], "html.parser").select("h4")
            targets = list(dict.fromkeys(resolve_character(h.get_text(" ", strip=True)) for h in headings))
            targets = [t for t in targets if t]
            targets.sort(key=lambda c: (c in frame.EX_BASE_CHARACTER, CHARACTER_NAMES.index(c)))
            if not targets:
                raise RuntimeError("No patch characters identified")
            self.report["characters"] = targets
            original_frame_dir = frame.FRAME_DIR
            frame.FRAME_DIR = self.stage / "data/gbvsr/frame"
            datasets = {}
            try:
                for character in targets:
                    path = frame._frame_json_path(character)
                    previous_path = self.backup / "frame" / path.name
                    previous = read_json(previous_path) if previous_path.exists() else {"rows": []}
                    rows, source, revision = await self.fetch_rows(client, character)
                    if not rows or len(rows) < len(previous["rows"]) * 0.8:
                        raise RuntimeError(f"Unexpected row count: {character} {len(rows)} vs {len(previous['rows'])}")
                    merge_local(character, rows, previous["rows"], glossary, self.report)
                    data = {"character": character, "source": source, "capturedAt": datetime.now(timezone.utc).isoformat(),
                            "updateVersion": self.args.version, "sourceRevision": revision, "rows": rows}
                    write_json(path, data)
                    datasets[character] = data
                    print(f"ROWS {character}: {len(previous['rows'])} -> {len(rows)}", flush=True)
            finally:
                frame.FRAME_DIR = original_frame_dir

            parsed = await self.parse(client, "GBVSR/Frame_Data", "overview.json")
            items = overview._extract_table_rows(parsed["text"])
            overview._apply_ex_fallbacks(items)
            old_overview = read_json(self.backup / "overview/gbvsr_overview.local.json")
            if set(CHARACTER_NAMES) - set(items):
                raise RuntimeError(f"Missing overview characters: {set(CHARACTER_NAMES) - set(items)}")
            portraits = {}
            old_items = {item["character"]: item for item in old_overview["characters"]}
            for character, item in items.items():
                base = overview.EX_BASE_CHARACTER.get(character, character)
                old = old_items.get(base, {})
                filename = old.get("portrait", {}).get("filename") or f"GBVSR_{base.replace(' ', '_')}_Portrait.png"
                portraits[base] = {"filename": filename}
                self.report["overviewMissingFields"][character] = [key for key in overview.FIELD_ALIASES if not item["stats"].get(key)]
            await self.localize(client, datasets, portraits)
            for character, item in items.items():
                portrait = portraits[overview.EX_BASE_CHARACTER.get(character, character)]
                item["portrait"] = portrait
                item["portraitPath"] = portrait.get("localPath", "")
            for character, data in datasets.items():
                write_json(self.stage / frame._frame_json_path(character), data)
            updated_overview = {"source": old_overview["source"], "capturedAt": datetime.now(timezone.utc).isoformat(),
                                "updateVersion": self.args.version, "characters": [items[c] for c in sorted(items)]}
            write_json(self.stage / overview.OUTPUT_PATH, updated_overview)

        self.report["rows"] = {char: len(data["rows"]) for char, data in datasets.items()}
        self.report["backup"] = self.backup.relative_to(self.root).as_posix()
        report_path = self.stage / f"data/gbvsr/updates/{self.args.version}.json"
        write_json(report_path, self.report)
        print(f"READY {len(datasets)} characters, {sum(self.report['rows'].values())} moves; "
              f"changed={len(self.report['changes'])}, notes to review={len(self.report['notesToReview'])}, "
              f"download failures={len(self.report['imageFailures'])}", flush=True)
        if self.report["imageFailures"]:
            raise RuntimeError(f"Resolve image failures before applying; see {report_path}")
        if self.args.apply:
            # Back up originals before replacing any existing staged assets.
            for source in self.stage.rglob("*"):
                if source.is_file():
                    relative = source.relative_to(self.stage)
                    destination = self.root / relative
                    saved = self.backup / "replaced" / relative
                    if destination.exists() and not saved.exists():
                        saved.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(destination, saved)
            for source in self.stage.rglob("*"):
                if source.is_file():
                    destination = self.root / source.relative_to(self.stage)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
            print("APPLIED local JSON and original images", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="2.61")
    parser.add_argument("--snapshot", default=datetime.now().strftime("%Y%m%d"),
                        help="Snapshot date (YYYYMMDD); reuse it to resume a staged download.")
    parser.add_argument("--proxy", default="")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"\d+\.\d+", args.version) or not re.fullmatch(r"\d{8}", args.snapshot):
        parser.error("Expected a numeric version and YYYYMMDD snapshot date")
    asyncio.run(Update(args).run())


if __name__ == "__main__":
    main()
