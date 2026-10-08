"""Smoke-test the configured OpenAI Responses endpoint without printing secrets."""

from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from plugins.responses_api import ResponsesAPIError, call_responses, load_responses_config  # noqa: E402


async def _probe(config: dict, *, search: bool) -> bool:
    label = "web-search" if search else "text"
    prompt = "请联网查询今天的日期，只回复日期。" if search else "只回复 pong"
    started = time.monotonic()
    try:
        result = await call_responses(
            config,
            [{"role": "user", "content": prompt}],
            enable_web_search=search,
            max_output_tokens=100,
        )
    except Exception as exc:
        elapsed = time.monotonic() - started
        print(f"FAIL {label}: {type(exc).__name__}: {exc} ({elapsed:.2f}s)")
        return False
    elapsed = time.monotonic() - started
    print(f"OK {label}: elapsed={elapsed:.2f}s result={result.replace(chr(10), ' ')[:120]!r}")
    return True


async def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(ROOT / "config.yaml"))
    parser.add_argument("--skip-search", action="store_true")
    args = parser.parse_args()
    with open(args.config, encoding="utf-8") as file:
        config = yaml.safe_load(file) or {}
    try:
        cfg = load_responses_config(config)
    except ResponsesAPIError as exc:
        print(f"FAIL config: {exc}")
        return 1
    print(f"Responses endpoint: {cfg.responses_url}")
    print(f"Responses model: {cfg.model}")
    if not await _probe(config, search=False):
        return 1
    if args.skip_search:
        return 0
    return 0 if await _probe(config, search=True) else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
