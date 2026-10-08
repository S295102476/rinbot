"""GBVSR frame data lookup.

Commands:
  #GB 卡姐 fm
  #GB 卡塔莉娜 26a
  #GB katalina c.M
"""

from __future__ import annotations

import json
import base64
import re
from pathlib import Path
from typing import Any

from nonebot import on_command, on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageSegment
from nonebot.log import logger
from nonebot.params import CommandArg
from nonebot.rule import Rule
from sqlalchemy import delete, select

from plugins.db import Base, GBVSRFrameMove, GBVSROverview, engine, SessionFactory
from gbvsr_registry import compact, resolve_character


gb_cmd = on_command("#GB", aliases={"#gb", "#Gb", "#gB"}, priority=5, block=True)

_DATA_ROOT = Path("data/gbvsr")
_FRAME_DIR = _DATA_ROOT / "frame"
_OVERVIEW_PATH = _DATA_ROOT / "overview" / "gbvsr_overview.local.json"

_GUARD_MAP = {
    "Low": "下段",
    "Mid": "无段",
    "High": "中段",
    "All": "全部",
    "Throw": "投",
}

_BUTTON_TO_DL = {
    "a": "l",
    "b": "m",
    "c": "h",
    "d": "u",
    "l": "l",
    "m": "m",
    "h": "h",
    "u": "u",
}
_BUTTON_TO_DISPLAY = {"l": "L", "m": "M", "h": "H", "u": "U"}
_BUTTON_ALT = {"L": ("l", "a"), "M": ("m", "b"), "H": ("h", "c"), "U": ("u", "d")}
_MAX_RESULTS = 8
_MOTION_ONLY_BUTTONS = ("l", "m", "h", "u", "x")
_TC_SEQUENCE = ["cxx", "cxxx", "cxx6m", "cxx6h"]
_TC_CHAIN_BUTTON_ORDER = {"l": 0, "m": 1, "h": 2}
_UNIVERSAL_ORDER = {
    "groundthrow": 10,
    "dgroundthrow": 11,
    "airthrow": 20,
    "dairthrow": 21,
    "ragingstrike": 30,
    "ragingchain": 40,
    "bravecounter": 50,
}

_UNIVERSAL_MOVE_INFO = {
    "groundthrow": {
        "aliases": ("投", "地投", "普通投", "throw", "gt", "ab", "ad", "a+b", "a+d", "lu", "lm", "l+u", "l+m"),
        "display": "L+U / L+M",
    },
    "airthrow": {
        "aliases": ("空投", "空中投", "airthrow", "at", "jab", "jad", "j.a+b", "j.a+d", "jlu", "jlm", "j.l+u", "j.l+m"),
        "display": "j.L+U / j.L+M",
    },
    "ragingstrike": {
        "aliases": ("rs", "红技", "红", "怒火强攻", "ragingstrike", "bc", "b+c", "mh", "m+h"),
        "display": "bc",
    },
    "ragingchain": {
        "aliases": ("rc", "红连", "怒火突袭", "ragingchain", "bc", "b+c", "mh", "m+h"),
        "display": "bc",
    },
    "bravecounter": {
        "aliases": ("反击", "英勇反击", "勇气反击", "bravecounter", "bc", "b+c", "mh", "m+h"),
        "display": "bc（防御中）",
    },
}

_KATALINA_MOVE_NAMES = {
    "214L": "附魔剑击",
    "214M": "附魔剑击",
    "214H": "附魔剑击",
    "214U": "附魔剑击",
    "236L": "非凡旅程",
    "236M": "非凡旅程",
    "236H": "非凡旅程",
    "236U": "非凡旅程",
    "623L": "翡翠剑击",
    "623M": "翡翠剑击",
    "623H": "翡翠剑击",
    "623U": "翡翠剑击",
    "236236H": "寒冰剑阵",
    "236236U": "幻灵天启",
}

_CACHE: dict[str, dict[str, Any]] = {}
_OVERVIEW_CACHE: dict[str, dict[str, Any]] = {}
_FRAME_FILE_MAP: dict[str, Path] | None = None
_DB_LOADED = False
_OVERVIEW_WORDS = {"概览", "總覽", "总览", "overview", "基础", "基础数据", "角色数据"}

_GB_HELP_TEXT = (
    "GBVSR 帧数查询\n"
    "用法：#GB <角色名称/别称> <招式指令>\n"
    "例：#GB 卡姐 2b\n"
    "例：#GB 卡姐 26a\n"
    "例：#GB EX奶刀 5b\n"
    "例：#GB 巴萨拉卡 6246a\n"
    "例：#GB 贝熊 概览\n"
    "多个招式可用 / 或逗号分隔：#GB 卡姐 2b/26a\n"
    "TC 连段可查：#GB 卡姐 tc"
)


def _message_has_at_bot(event: GroupMessageEvent, bot: Bot) -> bool:
    bot_id = str(bot.self_id)
    for seg in event.message:
        if seg.type == "at" and str(seg.data.get("qq")) == bot_id:
            return True

    raw = getattr(event, "raw_message", "") or ""
    rendered = str(event.message)
    pattern = rf"\[(?:CQ:)?at[:,]qq={re.escape(bot_id)}(?:[,\]])"
    return bool(re.search(pattern, raw) or re.search(pattern, rendered))


def _extract_gb_arg_from_plain(text: str) -> str | None:
    text = (text or "").strip().replace("＃", "#")
    text = re.sub(r"^#\s+", "#", text)
    match = re.match(r"^#\s*gb(?:\s+|$)(.*)$", text, flags=re.I)
    if not match:
        return None
    return match.group(1).strip()


def _at_gb_rule() -> Rule:
    async def _rule(bot: Bot, event: GroupMessageEvent) -> bool:
        return _message_has_at_bot(event, bot) and _extract_gb_arg_from_plain(event.get_plaintext()) is not None

    return Rule(_rule)


gb_at_cmd = on_message(rule=_at_gb_rule(), priority=3, block=True)


def _compact(text: str) -> str:
    return re.sub(r"[\s\u3000._-]+", "", (text or "").strip()).lower()


_OVERVIEW_WORD_KEYS = {_compact(word) for word in _OVERVIEW_WORDS}


def _normalize_button(ch: str) -> str:
    return _BUTTON_TO_DL.get(ch.lower(), ch.lower())


def _expand_motion_aliases(token: str) -> set[str]:
    aliases = {token}
    replacements = (
        ("632146", "6246"),
        ("63214", "624"),
        ("236236", "2626"),
        ("214214", "2424"),
        ("236", "26"),
        ("214", "24"),
    )

    prefixes = ("j", "")
    for prefix in prefixes:
        body = token[len(prefix):] if prefix and token.startswith(prefix) else token if not prefix else ""
        if not body:
            continue
        for long, short in replacements:
            if body.startswith(long):
                aliases.add(prefix + short + body[len(long):])
            if body.startswith(short):
                aliases.add(prefix + long + body[len(short):])
    return aliases


def _normalize_query_token(raw: str) -> str:
    token = _compact(raw)
    if not token:
        return ""

    if "+" in raw:
        plus_token = re.sub(r"\s+", "", raw.strip()).lower()
        return plus_token

    compact_raw = _compact(raw)
    universal_query_aliases = {
        "ab": "ab",
        "ad": "ad",
        "jab": "jab",
        "jad": "jad",
        "bc": "bc",
        "mh": "mh",
    }
    if compact_raw in universal_query_aliases:
        return universal_query_aliases[compact_raw]

    zh_prefix = {
        "近": "c",
        "近身": "c",
        "远": "f",
        "远身": "f",
        "跳": "j",
        "空": "j",
    }
    for prefix, repl in zh_prefix.items():
        if token.startswith(prefix):
            token = repl + token[len(prefix):]
            break

    if token.startswith("lu"):
        body = token[2:]
        bracket = re.fullmatch(r"(.+)\[([lmhu])\]", body)
        if bracket:
            body = bracket.group(1) + bracket.group(2)
        m = re.fullmatch(r"(j?\d+)([a-z]+)", body)
        if m:
            nums, suffix = m.groups()
            if len(suffix) == 1:
                suffix = _normalize_button(suffix)
            return "lu" + nums + suffix
        return "lu" + body

    m = re.fullmatch(r"([cfj])5([a-z])", token)
    if m:
        prefix, button = m.groups()
        return prefix + _normalize_button(button)

    if token.startswith("j") and len(token) >= 3 and token[1:].isdigit() is False:
        body = token[1:]
        m = re.fullmatch(r"(\d+)([a-z]+)", body)
        if m:
            nums, suffix = m.groups()
            if len(suffix) == 1:
                suffix = _normalize_button(suffix)
            return "j" + nums + suffix

    if len(token) >= 2 and token[0] in ("c", "f", "j") and not token[1].isdigit():
        button = _normalize_button(token[1])
        return token[0] + button + token[2:]

    m = re.fullmatch(r"(\d+)([a-z]+)", token)
    if m:
        nums, suffix = m.groups()
        if len(suffix) == 1:
            suffix = _normalize_button(suffix)
        token = nums + suffix

    return token


def _expand_query_token(raw: str) -> list[str]:
    # Luminiera-form inputs are written as "lu." on Dustloop.  Let users
    # spell that state explicitly as "变身", while plain 5D also keeps the
    # transformed 5U family reachable through its generated aliases.
    transformed = re.match(r"^\s*变身(?:态)?\s*", raw or "", flags=re.IGNORECASE)
    if transformed:
        inner = (raw or "")[transformed.end():].strip()
        return [
            token if token.startswith("lu") else "lu" + token
            for token in _expand_query_token(inner)
        ]

    token = _normalize_query_token(raw)
    if not token:
        return []

    if token == "tc":
        return list(_TC_SEQUENCE)

    # 5A/5B/5C mean "show both close and far normals".
    m = re.fullmatch(r"5([lmh])", token)
    if m:
        button = m.group(1)
        return [f"c{button}", f"f{button}", f"5{button}"]

    if token in {"ju", "jd"}:
        return ["ju", "jd", "j5u", "j5d", "j4u", "j4d", "j6u", "j6d", "j7u", "j7d", "j8u", "j8d", "j9u", "j9d"]

    return [token]


def _input_aliases(input_name: str) -> set[str]:
    raw = (input_name or "").strip()
    compact = _compact(raw)
    # [g]/[k] are stance markers, while [M]/[H]/[U] are button markers.
    # Do not discard the latter: 236[M] must be searchable as 26b.
    base = re.sub(r"\[[gk]\]$", "", compact, flags=re.IGNORECASE)
    aliases = {compact}
    if base:
        aliases.add(base)

    # Expand bracketed button notation used by charged moves, including
    # forms such as [2]8[H] and lu.236[M].
    bracket_variants: set[str] = set()
    for token in (compact, base):
        if not token:
            continue
        for match in re.finditer(r"\[([lmhux])\]", token, flags=re.IGNORECASE):
            button = match.group(1).lower()
            for alt in _BUTTON_ALT.get(button.upper(), (button,)):
                bracket_variants.add(
                    token[:match.start()] + alt + token[match.end():]
                )
    aliases.update(bracket_variants)
    for token in bracket_variants:
        aliases.update(_expand_motion_aliases(token))

    # Inputs prefixed with lu. are transformed-state moves.  Their unprefixed
    # aliases make 5D/j66/26B work naturally, while lu5u groups the base move
    # with its ~2/~4/~6/~8/~X follow-ups.
    transformed_tokens = {
        token for token in (compact, base, *bracket_variants)
        if token.startswith("lu")
    }
    for token in transformed_tokens:
        body = token[2:]
        if not body:
            continue
        aliases.add(body)
        aliases.update(_expand_motion_aliases(body))
        if re.match(r"^5u(?:~|$)", body, flags=re.IGNORECASE):
            aliases.add("5u")
            aliases.add("lu5u")

    if "/" in raw or "／" in raw:
        raw_parts = [part.strip() for part in re.split(r"[/／]+", raw) if part.strip()]
        prefix = ""
        for part in raw_parts:
            token = _compact(part)
            if not token:
                continue
            if re.match(r"^[cfj]", token):
                prefix = token[0]
            elif prefix and re.match(r"^\d", token):
                token = prefix + token
            aliases.add(token)
            aliases.add(token.replace(".", ""))
            m = re.fullmatch(r"([cfj]?)(\d*)([lmhu])", token.replace(".", ""))
            if m:
                part_prefix, nums, btn = m.groups()
                for alt in _BUTTON_ALT.get(btn.upper(), (btn,)):
                    aliases.add(f"{part_prefix}{nums}{alt}")

    for key, info in _UNIVERSAL_MOVE_INFO.items():
        if compact == key or base == key:
            aliases.update(info.get("aliases", ()))
            display = str(info.get("display") or "")
            if display:
                aliases.add(display)
                aliases.add(display.replace("+", ""))
            aliases.add(key)

    for token in {compact, base}:
        if not token:
            continue

        for plus_expr in re.findall(r"j?\.\s*[lmhu]\s*\+\s*[lmhu](?:\s*\+\s*[lmhu])?|\b[lmhu]\s*\+\s*[lmhu](?:\s*\+\s*[lmhu])?\b", raw.lower()):
            expr = re.sub(r"\s+", "", plus_expr)
            aliases.add(expr)
            aliases.add(expr.replace("+", ""))

        m = re.fullmatch(r"\[(\d)\](\d+)([lmhux])", token)
        if m:
            hold, motion, btn = m.groups()
            motion_token = hold + motion
            for alt in _BUTTON_ALT.get(btn.upper(), (btn,)):
                aliases.update(_expand_motion_aliases(motion_token + alt))
            aliases.update(_expand_motion_aliases(motion_token + btn))
            continue

        m = re.fullmatch(r"([cfj])([lmhu])", token)
        if m:
            prefix, btn = m.groups()
            aliases.add(prefix + btn)
            for alt in _BUTTON_ALT.get(btn.upper(), ()):
                aliases.add(prefix + alt)
            continue

        m = re.fullmatch(r"(j?)(\d+)u(?:lvl|lv)(\d+)", token)
        if m:
            prefix, nums, level = m.groups()
            aliases.add(f"{prefix}{nums}u")
            aliases.add(f"{prefix}{nums}d")
            aliases.add(f"{prefix}{nums}ulvl{level}")
            aliases.add(f"{prefix}{nums}dlvl{level}")
            continue

        m = re.fullmatch(r"td.+", token)
        if m:
            aliases.add("5u")
            aliases.add("5d")
            aliases.add("td")
            continue

        m = re.fullmatch(r"cxx(6[mu])?", token)
        if m:
            aliases.add(token)
            aliases.add(token.replace("cxx", "tc", 1))
            continue

        m = re.fullmatch(r"(\d+)([lmh])~.+", token)
        if m:
            nums, btn = m.groups()
            for alt in _BUTTON_ALT.get(btn.upper(), (btn,)):
                aliases.update(_expand_motion_aliases(nums + alt))
            aliases.update(_expand_motion_aliases(nums + btn))
            continue

        m = re.fullmatch(r"(\d+)([lmhu])", token)
        if m:
            nums, btn = m.groups()
            for alt in _BUTTON_ALT.get(btn.upper(), (btn,)):
                aliases.update(_expand_motion_aliases(nums + alt))
            aliases.update(_expand_motion_aliases(nums + btn))
            continue

        # Dustloop sometimes uses X for normal special versions.
        m = re.fullmatch(r"(\d+)x(?:~.+)?", token)
        if m:
            nums = m.group(1)
            for alt in ("l", "m", "h", "a", "b", "c", "x"):
                aliases.update(_expand_motion_aliases(nums + alt))
            aliases.update(_expand_motion_aliases(nums + "x"))
            continue

        aliases.update(_expand_motion_aliases(token))

    return aliases


def _expand_lu_motion_aliases(token: str) -> set[str]:
    """Expand motion aliases while preserving Vira's lu transformation prefix."""
    token = _normalize_query_token(token)
    if not token.startswith("lu"):
        return set()
    body = token[2:]
    return {"lu" + alias for alias in _expand_motion_aliases(body)}


def _append_index_item(index: dict[str, list[dict[str, Any]]], alias: str, item: dict[str, Any]) -> None:
    alias = _compact(alias)
    if not alias:
        return
    bucket = index.setdefault(alias, [])
    if not any(existing is item for existing in bucket):
        bucket.append(item)


def _id_dragon_aliases(input_name: str) -> set[str]:
    if not input_name.strip().lower().startswith("df."):
        return set()

    # Dragonform is entered with 214214H; that query includes every df. move.
    aliases = {"214214h"}
    body = _compact(input_name)[2:]
    button = re.fullmatch(r"5?([lmhu])", body)
    if button:
        for alt in _BUTTON_ALT[button.group(1).upper()]:
            aliases.update((f"龙{alt}", f"龙5{alt}"))
    elif body in {"s", "5s"}:
        aliases.update(("龙s", "龙5s"))
    elif body == "lu":
        aliases.update(("龙投", "龙lu"))
    return aliases


def _build_index(rows: list[dict[str, Any]], character: str = "") -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = {}
    for item in rows:
        row = item.get("row") or {}
        input_name = str(row.get("Input") or "")
        if not input_name:
            continue
        for alias in _input_aliases(input_name):
            _append_index_item(index, alias, item)
        if character == "Id":
            for alias in _id_dragon_aliases(input_name):
                _append_index_item(index, alias, item)
        for alias in item.get("_aliases", []) or item.get("aliases", []) or []:
            _append_index_item(index, str(alias), item)
    return index


def _load_character(key: str) -> dict[str, Any]:
    if key in _CACHE:
        return _CACHE[key]

    path = _discover_frame_file_map().get(compact(key))
    if path is None:
        raise FileNotFoundError(f"{key} frame data not found")
    if not path.exists():
        raise FileNotFoundError(str(path))

    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("rows") or []

    loaded = {"data": data, "index": _build_index(rows, key), "path": path}
    _CACHE[key] = loaded
    return loaded


async def _load_character_from_db(key: str) -> dict[str, Any] | None:
    async with SessionFactory() as session:
        rows = (await session.execute(
            select(GBVSRFrameMove).where(GBVSRFrameMove.character == key).order_by(GBVSRFrameMove.id.asc())
        )).scalars().all()

    if not rows:
        return None

    data_rows: list[dict[str, Any]] = []
    for row in rows:
        aliases = json.loads(row.aliases or "[]")
        row_map = {
            "Input": row.input_name,
            "Damage": row.damage,
            "Guard": row.guard,
            "Startup": row.startup,
            "Active": row.active,
            "Recovery": row.recovery,
            "On-Block": row.on_block,
            "On-Hit": row.on_hit,
            "On Counter Hit": row.on_counter_hit,
            "Level": row.level,
            "Invuln": row.invuln,
            "Combo Limit Scaling": row.combo_limit_scaling,
        }
        item = {
            "section": row.section,
            "row": row_map,
            "images": [],
            "hitboxes": [],
            "notes": row.notes,
            "notesZh": row.notes_zh,
            "imagePaths": json.loads(row.image_paths or "[]"),
            "hitboxPaths": json.loads(row.hitbox_paths or "[]"),
        }
        if row.move_name:
            row_map["Name"] = row.move_name
        item["_aliases"] = aliases
        data_rows.append(item)

    data = {
        "character": key,
        "source": "db://gbvsr_frame_moves",
        "capturedAt": "",
        "rows": data_rows,
    }

    index = _build_index(data_rows, key)

    for item in data_rows:
        item.pop("_aliases", None)

    loaded = {"data": data, "index": index, "path": Path("db://gbvsr_frame_moves")}
    _CACHE[key] = loaded
    return loaded


async def _load_overview_from_db(key: str) -> dict[str, Any] | None:
    if key in _OVERVIEW_CACHE:
        return _OVERVIEW_CACHE[key]

    async with SessionFactory() as session:
        row = (await session.execute(
            select(GBVSROverview).where(GBVSROverview.character == key)
        )).scalar_one_or_none()

    if row is None:
        return None

    item = {
        "character": row.character,
        "displayName": row.display_name or row.character,
        "portraitPath": row.portrait_path,
        "stats": {
            "health": row.health,
            "backdash": row.backdash,
            "jump_startup": row.jump_startup,
            "walk_speed": row.walk_speed,
            "backwalk_speed": row.backwalk_speed,
            "initial_dash_speed": row.initial_dash_speed,
            "dash_acceleration": row.dash_acceleration,
            "jump_height": row.jump_height,
            "forward_jump_distance": row.forward_jump_distance,
            "backward_jump_distance": row.backward_jump_distance,
            "superjump_height": row.superjump_height,
            "forward_superjump_distance": row.forward_superjump_distance,
            "backward_superjump_distance": row.backward_superjump_distance,
            "cl_proximity_range": row.cl_proximity_range,
            "cm_proximity_range": row.cm_proximity_range,
            "ch_proximity_range": row.ch_proximity_range,
        },
    }
    _OVERVIEW_CACHE[key] = item
    return item


def _load_overview_from_json(key: str) -> dict[str, Any] | None:
    if key in _OVERVIEW_CACHE:
        return _OVERVIEW_CACHE[key]
    if not _OVERVIEW_PATH.exists():
        return None

    data = json.loads(_OVERVIEW_PATH.read_text(encoding="utf-8"))
    for item in data.get("characters", []):
        if compact(str(item.get("character") or "")) == compact(key):
            _OVERVIEW_CACHE[key] = item
            return item
    return None


def _discover_frame_file_map() -> dict[str, Path]:
    global _FRAME_FILE_MAP
    if _FRAME_FILE_MAP is not None:
        return _FRAME_FILE_MAP

    file_map: dict[str, Path] = {}
    candidates = list(_FRAME_DIR.glob("*Frame2.local.json")) + list(_FRAME_DIR.glob("*Frame2.json"))
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        character = str(data.get("character") or "").strip()
        if not character:
            m = re.match(r"^(.*)Frame2(?:\.local)?\.json$", path.name, flags=re.I)
            if m:
                character = m.group(1)
        if not character:
            continue
        key = compact(character)
        if key and key not in file_map:
            file_map[key] = path

    _FRAME_FILE_MAP = file_map
    return file_map


def _parse_args(text: str) -> tuple[str | None, list[tuple[str, list[str]]]]:
    parts = [p for p in re.split(r"\s+", (text or "").strip()) if p]
    if len(parts) < 2:
        return None, []

    char_key = resolve_character(parts[0])
    if not char_key:
        return None, []

    move_parts: list[str] = []
    for part in parts[1:]:
        move_parts.extend(re.split(r"[/／,，]+", part))
    moves: list[tuple[str, list[str]]] = []
    for move_part in move_parts:
        tokens = [x for x in _expand_query_token(move_part) if x]
        if tokens:
            moves.append((move_part, tokens))
    return char_key, moves[:_MAX_RESULTS]


def _parse_overview_args(text: str) -> str | None:
    parts = [p for p in re.split(r"\s+", (text or "").strip()) if p]
    if len(parts) != 2:
        return None
    if _compact(parts[1]) not in _OVERVIEW_WORD_KEYS:
        return None
    return resolve_character(parts[0])


def _fmt_frame(value: str, suffix: str = "F") -> str:
    value = str(value or "").strip()
    if not value:
        return "-"
    if re.fullmatch(r"[+-]?\d+", value):
        return value + suffix
    return value


def _format_invuln(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return "-"

    replacements = (
        (r"\bSuperarmor\s+High\b", "强霸体（仅无段，中段）"),
        (r"\bSuper\s*Armor\s+High\b", "强霸体（仅无段，中段）"),
        (r"\bSuperarmor\b", "强霸体"),
        (r"\bSuper\s*Armor\b", "强霸体"),
        (r"\bArmor\s*[×x]\s*2\b", "2层霸体"),
        (r"\bStrike\b", "打击无敌"),
        (r"\bProjectile\b", "飞行道具无敌"),
        (r"\bThrow\b", "投无敌"),
        (r"\bAll\b", "完全无敌"),
        (r"\bFull\b", "完全无敌"),
        (r"\bLow\s+Profile\b", "低姿态"),
        (r"\bArmor\b", "霸体"),
    )
    for pattern, repl in replacements:
        text = re.sub(pattern, repl, text, flags=re.IGNORECASE)
    return text


def _format_input_name(value: str) -> str:
    text = str(value or "").strip()
    text = (
        text.replace("d.Ground Throw", "Ground Throw")
        .replace("d.Air Throw", "Air Throw")
    )
    text = re.sub(r"^lu\.", "变身 ", text, flags=re.IGNORECASE)
    return (
        text.replace("[g]", "[源氏]")
        .replace("[G]", "[源氏]")
        .replace("[k]", "[神乐]")
        .replace("[K]", "[神乐]")
    )


def _format_title(char_label: str, input_name: str, move_name: str = "") -> str:
    raw_input_name = str(input_name or "").strip()
    input_name = _format_input_name(raw_input_name)
    move_name = (move_name or "").strip()
    info_key = _compact(raw_input_name.removeprefix("d."))
    info = _UNIVERSAL_MOVE_INFO.get(info_key)
    if info and not move_name:
        move_name = str(info.get("display") or "")
    if raw_input_name == "d.Ground Throw":
        move_name = "d.ab / d.ad"
    elif raw_input_name == "d.Air Throw":
        move_name = "d.jab / d.jad"
    if not move_name and char_label == "Katalina":
        move_name = _KATALINA_MOVE_NAMES.get(input_name, "")
    if move_name:
        return f"【{char_label} {input_name} {move_name}】"
    return f"【{char_label} {input_name}】"


def _format_text(char_label: str, item: dict[str, Any]) -> str:
    row = item.get("row") or {}
    input_name = str(row.get("Input", "-") or "-")
    move_name = row.get("Name", "")
    guard = _GUARD_MAP.get(row.get("Guard", ""), row.get("Guard") or "-")
    invuln = _format_invuln(row.get("Invuln") or "")
    notes = (item.get("notesZh") or item.get("notes") or "").strip()
    notes = notes.replace(";", "\n") if notes else ""

    lines = [
        _format_title(char_label, input_name, move_name),
        f"防御属性：{guard}",
        f"启动：{_fmt_frame(row.get('Startup'))}",
        f"持续：{_fmt_frame(row.get('Active'))}",
        f"恢复：{_fmt_frame(row.get('Recovery'))}",
        f"打防：{_fmt_frame(row.get('On-Block'))}",
        f"命中：{_fmt_frame(row.get('On-Hit'))}",
        f"打康命中：{_fmt_frame(row.get('On Counter Hit'))}",
        f"伤害：{row.get('Damage') or '-'}",
        f"打击等级：{row.get('Level') or '-'}",
        f"无敌/特殊帧数：{invuln}",
        f"连击限制增加值：{row.get('Combo Limit Scaling') or '-'}",
    ]
    if notes:
        lines.append(f"备注：{notes}")
    return "\n".join(lines)


def _fmt_plain(value: Any) -> str:
    text = str(value or "").strip()
    return text or "-"


def _format_backdash(value: str) -> str:
    text = _fmt_plain(value)
    if text == "-":
        return text

    text = re.sub(r"(\d+)\s*F?\s+Duration\b", r"\1F持续", text, flags=re.IGNORECASE)

    def _range_repl(match: re.Match[str], label: str) -> str:
        frame_range = re.sub(r"\s+", "", match.group(1)).upper()
        if not frame_range.endswith("F"):
            frame_range += "F"
        return f"，{frame_range}为{label}"

    text = re.sub(r"\s+，", "，", text)
    text = re.sub(
        r"(\d+\s*-\s*\d+\s*F?)\s+Airborne\b",
        lambda m: _range_repl(m, "浮空判定"),
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"(\d+\s*-\s*\d+\s*F?)\s+Throw\s+Invuln\b",
        lambda m: _range_repl(m, "投无敌"),
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\s+，", "，", text)
    return text


def _format_overview_text(item: dict[str, Any]) -> str:
    character = str(item.get("displayName") or item.get("character") or "-")
    stats = item.get("stats") or {}
    lines = [
        f"【{character} 概览】",
        f"血量：{_fmt_plain(stats.get('health'))}",
        f"后跳：{_format_backdash(stats.get('backdash') or '')}",
        f"跳启动：{_fmt_plain(stats.get('jump_startup'))}",
        f"前走速度：{_fmt_plain(stats.get('walk_speed'))}",
        f"后走速度：{_fmt_plain(stats.get('backwalk_speed'))}",
        f"初始冲刺速度：{_fmt_plain(stats.get('initial_dash_speed'))}",
        f"冲刺加速度：{_fmt_plain(stats.get('dash_acceleration'))}",
        f"跳跃高度：{_fmt_plain(stats.get('jump_height'))}",
        f"前跳距离：{_fmt_plain(stats.get('forward_jump_distance'))}",
        f"后跳距离：{_fmt_plain(stats.get('backward_jump_distance'))}",
        f"大跳高度：{_fmt_plain(stats.get('superjump_height'))}",
        f"大前跳距离：{_fmt_plain(stats.get('forward_superjump_distance'))}",
        f"大后跳距离：{_fmt_plain(stats.get('backward_superjump_distance'))}",
    ]
    proximity_lines = (
        ("cl_proximity_range", "近L生效距离"),
        ("cm_proximity_range", "近M生效距离"),
        ("ch_proximity_range", "近H生效距离"),
    )
    for key, label in proximity_lines:
        value = str(stats.get(key) or "").strip()
        if value:
            lines.append(f"{label}：{value}")
    return "\n".join(lines)


def _build_overview_message(item: dict[str, Any]) -> Message:
    msg = Message()
    portrait_path = Path(str(item.get("portraitPath") or ""))
    if portrait_path.exists() and portrait_path.is_file():
        try:
            msg += MessageSegment.image(portrait_path.read_bytes())
        except Exception as e:
            logger.warning(f"[gbvsr_frame] 读取角色立绘失败 {portrait_path}: {type(e).__name__}: {e}")
    msg += MessageSegment.text(_format_overview_text(item))
    return msg


def _item_identity(item: dict[str, Any]) -> tuple[str, str, str]:
    row = item.get("row") or {}
    return (
        str(row.get("Input") or ""),
        str(row.get("Name") or ""),
        str(item.get("section") or ""),
    )


def _item_sort_key(item: dict[str, Any]) -> tuple[int, str, str]:
    row = item.get("row") or {}
    input_name = _compact(str(row.get("Input") or ""))
    return (
        _UNIVERSAL_ORDER.get(input_name, 100),
        str(item.get("section") or ""),
        str(row.get("Input") or ""),
    )


def _find_items(index: dict[str, list[dict[str, Any]]], move: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    aliases = _expand_motion_aliases(move) | _expand_lu_motion_aliases(move)
    for alias in aliases:
        for item in index.get(_compact(alias), []):
            ident = _item_identity(item)
            if ident in seen:
                continue
            seen.add(ident)
            out.append(item)
    return sorted(out, key=_item_sort_key)


def _find_lu_family_items(index: dict[str, list[dict[str, Any]]], move: str) -> list[dict[str, Any]]:
    """Return a transformed move and its ~ follow-ups, e.g. lu5d~6/8/4."""
    token = _normalize_query_token(move)
    if not token.startswith("lu"):
        return []
    body = token[2:]
    if not re.fullmatch(r"j?\d+[lmhu]", body):
        return []

    prefixes = _expand_lu_motion_aliases(token)
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for alias, bucket in index.items():
        if not any(alias == prefix or alias.startswith(prefix + "~") for prefix in prefixes):
            continue
        for item in bucket:
            ident = _item_identity(item)
            if ident in seen:
                continue
            seen.add(ident)
            out.append(item)
    return sorted(out, key=_item_sort_key)


def _find_motion_family_items(index: dict[str, list[dict[str, Any]]], move: str) -> list[dict[str, Any]]:
    token = _normalize_query_token(move)
    if not re.fullmatch(r"j?\d{2,4}", token):
        return []

    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    prefixes = sorted({_compact(alias) for alias in _expand_motion_aliases(token)}, key=len, reverse=True)
    for alias, bucket in index.items():
        suffix = ""
        for prefix in prefixes:
            if alias.startswith(prefix) and alias != prefix:
                suffix = alias[len(prefix):]
                break
        if not suffix or suffix[0] not in _MOTION_ONLY_BUTTONS:
            continue
        for item in bucket:
            ident = _item_identity(item)
            if ident in seen:
                continue
            seen.add(ident)
            out.append(item)
    return sorted(out, key=_item_sort_key)


def _normal_chain_sort_key(item: dict[str, Any]) -> tuple[int, int, int, str]:
    row = item.get("row") or {}
    token = _compact(str(row.get("Input") or ""))
    m = re.fullmatch(r"5([lmh]+)", token)
    if m:
        buttons = m.group(1)
        return (_TC_CHAIN_BUTTON_ORDER.get(buttons[0], 99), 0, len(buttons), token)
    m = re.fullmatch(r"5([lmh])~([lmh])", token)
    if m:
        start, follow = m.groups()
        return (_TC_CHAIN_BUTTON_ORDER.get(start, 99), 1, _TC_CHAIN_BUTTON_ORDER.get(follow, 99), token)
    return (99, 99, 99, token)


def _find_normal_chain_items(index: dict[str, list[dict[str, Any]]], move: str) -> list[dict[str, Any]]:
    token = _normalize_query_token(move)
    m = re.fullmatch(r"5([lmh])", token)
    if not m:
        return []

    button = m.group(1)
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    patterns = (
        re.compile(rf"^5{button}+$"),
        re.compile(rf"^5{button}~[lmh]$"),
    )
    for alias, bucket in index.items():
        if not any(pattern.fullmatch(alias) for pattern in patterns):
            continue
        for item in bucket:
            ident = _item_identity(item)
            if ident in seen:
                continue
            seen.add(ident)
            out.append(item)
    return sorted(out, key=_normal_chain_sort_key)


def _find_tc_chain_items(index: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for alias, bucket in index.items():
        if not (re.fullmatch(r"5[lhm]{2,4}", alias) or re.fullmatch(r"5[lhm]~[lhm]", alias)):
            continue
        for item in bucket:
            ident = _item_identity(item)
            if ident in seen:
                continue
            seen.add(ident)
            out.append(item)
    return sorted(out, key=_normal_chain_sort_key)


def _find_unique_action_level_items(index: dict[str, list[dict[str, Any]]], move: str) -> list[dict[str, Any]]:
    token = _normalize_query_token(move)
    if token not in {"5u", "5d", "6u", "6d"}:
        return []

    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    prefixes = ("6ulvl", "6dlvl") if token in {"5u", "5d", "6u", "6d"} else ()
    for alias, bucket in index.items():
        if not alias.startswith(prefixes):
            continue
        for item in bucket:
            ident = _item_identity(item)
            if ident in seen:
                continue
            seen.add(ident)
            out.append(item)
    return sorted(out, key=_item_sort_key)


def _resolve_image_paths(item: dict[str, Any]) -> list[Path]:
    raw_paths = item.get("hitboxPaths") or item.get("imagePaths") or []
    paths: list[Path] = []
    for raw in raw_paths:
        path = Path(str(raw))
        if path.exists() and path.is_file():
            paths.append(path)
    return paths


def _build_message(char_label: str, item: dict[str, Any], *, include_images: bool = True) -> Message:
    msg = Message(MessageSegment.text(_format_text(char_label, item)))
    if not include_images:
        return msg
    for path in _resolve_image_paths(item):
        try:
            msg += MessageSegment.image(path.read_bytes())
        except Exception as e:
            logger.warning(f"[gbvsr_frame] 读取图片失败 {path}: {type(e).__name__}: {e}")
    return msg


def _build_node(bot: Bot, char_label: str, item: dict[str, Any]) -> dict[str, Any]:
    content: list[MessageSegment] = [MessageSegment.text(_format_text(char_label, item))]
    for path in _resolve_image_paths(item):
        try:
            b64 = base64.b64encode(path.read_bytes()).decode()
            content.append(MessageSegment.image(f"base64://{b64}"))
        except Exception as e:
            logger.warning(f"[gbvsr_frame] 读取图片失败 {path}: {type(e).__name__}: {e}")

    return {
        "type": "node",
        "data": {
            "name": "GBVSR帧数",
            "uin": str(bot.self_id),
            "content": content,
        },
    }


def _build_text_node(bot: Bot, text: str) -> dict[str, Any]:
    return {
        "type": "node",
        "data": {
            "name": "GBVSR帧数",
            "uin": str(bot.self_id),
            "content": [MessageSegment.text(text)],
        },
    }


def _build_overview_node(bot: Bot, item: dict[str, Any]) -> dict[str, Any]:
    content: list[MessageSegment] = []
    portrait_path = Path(str(item.get("portraitPath") or ""))
    if portrait_path.exists() and portrait_path.is_file():
        try:
            b64 = base64.b64encode(portrait_path.read_bytes()).decode()
            content.append(MessageSegment.image(f"base64://{b64}"))
        except Exception as e:
            logger.warning(f"[gbvsr_frame] 读取角色立绘失败 {portrait_path}: {type(e).__name__}: {e}")
    content.append(MessageSegment.text(_format_overview_text(item)))
    return {
        "type": "node",
        "data": {
            "name": "GBVSR概览",
            "uin": str(bot.self_id),
            "content": content,
        },
    }


async def _handle_gb_frame_query(bot: Bot, event: GroupMessageEvent, arg_text: str, matcher) -> None:
    arg_text = arg_text.strip()
    if _compact(arg_text) in {"帮助", "help", "说明", "用法"}:
        await matcher.finish(_GB_HELP_TEXT)
        return

    overview_key = _parse_overview_args(arg_text)
    if overview_key:
        try:
            overview = await _load_overview_from_db(overview_key)
            if overview is None:
                overview = _load_overview_from_json(overview_key)
        except Exception as e:
            logger.warning(f"[gbvsr_frame] 读取概览失败: {type(e).__name__}: {e}")
            await matcher.finish("GBVSR 概览数据尚未初始化，请管理员按 docs/extensions.md 的 GBVSR 步骤下载并导入数据。")
            return
        if overview is None:
            await matcher.finish("没找到该角色概览；首次部署请管理员按 docs/extensions.md 初始化 GBVSR 数据。")
            return
        node = _build_overview_node(bot, overview)
        try:
            await bot.call_api("send_group_forward_msg", group_id=event.group_id, messages=[node])
        except Exception as e:
            logger.warning(f"[gbvsr_frame] 概览合并转发失败: {type(e).__name__}: {e}")
            await matcher.finish(_build_overview_message(overview))
        return

    char_key, moves = _parse_args(arg_text)
    if not char_key or not moves:
        await matcher.finish("用法：#GB <角色名称/别称> <招式指令>。输入 #GB 帮助 查看说明。")
        return

    try:
        loaded = await _load_character_from_db(char_key)
        if loaded is None:
            loaded = _load_character(char_key)
    except Exception as e:
        logger.warning(f"[gbvsr_frame] 读取数据失败: {type(e).__name__}: {e}")
        await matcher.finish("GBVSR 帧数数据尚未初始化，请管理员按 docs/extensions.md 的 GBVSR 步骤下载并导入数据。")
        return

    char_label = str((loaded.get("data") or {}).get("character") or char_key)
    index = loaded["index"]
    not_found: list[str] = []
    nodes: list[dict[str, Any]] = []
    fallback_items: list[dict[str, Any]] = []
    seen_inputs: set[str] = set()

    for raw_move, query_tokens in moves:
        items: list[dict[str, Any]] = []
        seen_group_items: set[tuple[str, str, str]] = set()
        for query_token in query_tokens:
            found_items = _find_items(index, query_token)
            lu_family_items = _find_lu_family_items(index, query_token)
            if lu_family_items:
                found_items = found_items + lu_family_items
            level_items = _find_unique_action_level_items(index, query_token)
            if level_items:
                found_items = found_items + level_items
            normal_chain_items = _find_normal_chain_items(index, query_token)
            if normal_chain_items:
                found_items = found_items + normal_chain_items
            if not found_items:
                found_items = _find_motion_family_items(index, query_token)
            for item in found_items:
                ident = _item_identity(item)
                if ident in seen_group_items:
                    continue
                seen_group_items.add(ident)
                items.append(item)

        if not items and _normalize_query_token(raw_move) == "tc":
            for item in _find_tc_chain_items(index):
                ident = _item_identity(item)
                if ident in seen_group_items:
                    continue
                seen_group_items.add(ident)
                items.append(item)

        if not items:
            not_found.append(raw_move)
            continue

        for item in items:
            input_name = str((item.get("row") or {}).get("Input") or "")
            identity = f"{input_name}\0{(item.get('row') or {}).get('Name') or ''}\0{item.get('section') or ''}"
            if identity in seen_inputs:
                continue
            seen_inputs.add(identity)

            nodes.append(_build_node(bot, char_label, item))
            fallback_items.append(item)

    if not_found:
        nodes.append(_build_text_node(bot, f"没找到：{', '.join(not_found)}"))
    if not nodes:
        await matcher.finish("没找到对应招式。")

    try:
        await bot.call_api("send_group_forward_msg", group_id=event.group_id, messages=nodes)
    except Exception as e:
        logger.warning(f"[gbvsr_frame] 合并转发失败: {type(e).__name__}: {e}")
        for item in fallback_items:
            await bot.send(event, _build_message(char_label, item))
        if not_found:
            await matcher.finish(f"没找到：{', '.join(not_found)}")


@gb_cmd.handle()
async def handle_gb_frame(bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    await _handle_gb_frame_query(bot, event, args.extract_plain_text(), gb_cmd)


@gb_at_cmd.handle()
async def handle_at_gb_frame(bot: Bot, event: GroupMessageEvent):
    arg_text = _extract_gb_arg_from_plain(event.get_plaintext())
    if arg_text is None:
        return
    await _handle_gb_frame_query(bot, event, arg_text, gb_at_cmd)
