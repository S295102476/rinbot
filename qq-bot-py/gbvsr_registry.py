"""Shared GBVSR character registry for tools and plugins."""

from __future__ import annotations

import json
import re
from collections import OrderedDict
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent
REGISTRY_PATH = ROOT / "gbvsr_characters.json"

_CANONICAL_FIXES = {
    "Siefried": "Siegfried",
}

_EX_CHARACTERS = {
    "Narmaya_(EX)": ["EXNarmaya", "EX Narmaya", "EX奶刀", "EX娜尔梅亚"],
    "Gran_(EX)": ["EXGran", "EX Gran", "EX古兰"],
    "Djeeta_(EX)": ["EXDjeeta", "EX Djeeta", "EX姬塔", "EX吉他"],
}


def compact(text: str) -> str:
    return re.sub(r"[\s\u3000._-]+", "", (text or "").strip()).lower()


def _normalize_canonical(raw: str) -> str:
    raw = (raw or "").strip()
    return _CANONICAL_FIXES.get(raw, raw)


def normalize_canonical(raw: str) -> str:
    return _normalize_canonical(raw)


def _iter_registry_items() -> Iterable[tuple[str, list[str]]]:
    """Versioned names/aliases remain available without downloaded game data."""
    entries = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    if not isinstance(entries, dict):
        raise ValueError("GBVSR character registry must be a mapping")
    return [(_normalize_canonical(canonical), list(aliases))
            for canonical, aliases in entries.items()]


def _build_registry() -> tuple[dict[str, str], dict[str, list[str]]]:
    alias_to_canonical: dict[str, str] = {}
    aliases_by_canonical: "OrderedDict[str, list[str]]" = OrderedDict()

    for canonical, aliases in _iter_registry_items():
        aliases_by_canonical.setdefault(canonical, [])
        all_aliases = [canonical, *aliases]
        if canonical != _normalize_canonical(canonical):
            all_aliases.append(_normalize_canonical(canonical))

        for alias in all_aliases:
            alias = (alias or "").strip()
            if not alias:
                continue
            alias_key = compact(alias)
            if alias_key and alias_key not in alias_to_canonical:
                alias_to_canonical[alias_key] = canonical
            if alias not in aliases_by_canonical[canonical]:
                aliases_by_canonical[canonical].append(alias)

    for canonical, aliases in _EX_CHARACTERS.items():
        aliases_by_canonical.setdefault(canonical, [])
        all_aliases = [canonical, *aliases]
        for alias in all_aliases:
            alias = (alias or "").strip()
            if not alias:
                continue
            alias_key = compact(alias)
            if alias_key and alias_key not in alias_to_canonical:
                alias_to_canonical[alias_key] = canonical
            if alias not in aliases_by_canonical[canonical]:
                aliases_by_canonical[canonical].append(alias)

    return alias_to_canonical, dict(aliases_by_canonical)


ALIAS_TO_CANONICAL, ALIASES_BY_CANONICAL = _build_registry()
CHARACTER_NAMES = list(ALIASES_BY_CANONICAL.keys())


def resolve_character(raw: str) -> str | None:
    return ALIAS_TO_CANONICAL.get(compact(raw))


def aliases_for(character: str) -> set[str]:
    canonical = resolve_character(character) or character
    aliases = set(ALIASES_BY_CANONICAL.get(canonical, []))
    aliases.add(canonical)
    aliases.add(compact(canonical))
    return {a for a in aliases if a}
