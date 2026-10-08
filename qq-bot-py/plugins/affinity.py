"""Pure affinity scoring helpers shared by Agent and command handlers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def _load_config() -> dict[str, Any]:
    for path in (Path("config.yaml"), Path(__file__).resolve().parents[1] / "config.yaml"):
        try:
            return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
    return {}


_CONFIG = ((_load_config().get("agent") or {}).get("affinity") or {})


def relationship_stage(score: float) -> str:
    """Return the user-facing-independent relationship band for an affinity score."""
    try:
        score = float(score)
    except (TypeError, ValueError):
        score = 0.0
    if score >= 85:
        return "接近情人关系"
    if score >= 75:
        return "非常信任，可托付后背"
    if score >= 60:
        return "挚友、闺蜜"
    if score >= 30:
        return "好朋友"
    if score >= 15:
        return "普通朋友"
    if score >= 5:
        return "点头之交"
    if score >= 0:
        return "普通陌生人"
    # Negative-stage wording and moderation thresholds deliberately remain
    # unchanged from the previous system.
    if score > -10:
        return "略有距离"
    if score >= -30:
        return "较冷淡"
    if score >= -50:
        return "有些戒备"
    if score >= -70:
        return "反感"
    if score >= -90:
        return "厌烦"
    return "强烈厌恶"


def normalize_delta(value: object) -> float:
    try:
        raw = float(value)
    except (TypeError, ValueError):
        return 0.0
    if raw != raw or raw in {float("inf"), float("-inf")}:
        return 0.0
    minimum = float(_CONFIG.get("raw_delta_min", -2.0))
    maximum = float(_CONFIG.get("raw_delta_max", 2.0))
    step = max(0.01, float(_CONFIG.get("step", 0.1)))
    raw = max(minimum, min(maximum, raw))
    return round(round(raw / step) * step, 2)


def curved_delta(score: float, raw_delta: float) -> float:
    """Apply diminishing returns near either endpoint of the score range."""
    minimum = float(_CONFIG.get("min_score", -100))
    maximum = float(_CONFIG.get("max_score", 100))
    exponent = max(0.1, float(_CONFIG.get("curve_exponent", 0.7)))
    positive_scale = float(_CONFIG.get("positive_scale", 1.0))
    negative_scale = float(_CONFIG.get("negative_scale", 1.0))
    score = max(minimum, min(maximum, float(score)))
    raw_delta = normalize_delta(raw_delta)
    if raw_delta >= 0:
        available = max(0.0, (maximum - score) / (maximum - minimum))
        return raw_delta * positive_scale * (available ** exponent)
    available = max(0.0, (score - minimum) / (maximum - minimum))
    return raw_delta * negative_scale * (available ** exponent)
