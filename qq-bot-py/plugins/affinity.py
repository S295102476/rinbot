"""Pure affinity scoring helpers shared by Agent and command handlers."""

from __future__ import annotations

import math
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


def romance_eligible(score: object) -> bool:
    """Use the stored persona score, never its rounded display, for intimacy."""
    try:
        value = float(score)
    except (TypeError, ValueError):
        return False
    return math.isfinite(value) and value == 100.0


def affinity_context(score: object) -> str:
    """Render an unambiguous relationship boundary for model context."""
    try:
        value = float(score)
    except (TypeError, ValueError):
        value = 0.0
    if not math.isfinite(value) or not -100 <= value <= 100:
        value = 0.0
    # 99.99 is still friendship, even if a leaderboard rounds it to 100.0.
    display = min(value, 99.9) if 0 <= value < 100 else value
    boundary = "真实满好感：是，亲密回应仍需双方愿意" if romance_eligible(value) else "真实满好感：否，仅友情，不回应亲吻或默认恋人"
    return f"好感{display:.1f}/100，关系阶段{relationship_stage(value)}；{boundary}"


def relationship_stage(score: float) -> str:
    """Return the user-facing-independent relationship band for an affinity score."""
    try:
        score = float(score)
    except (TypeError, ValueError):
        score = 0.0
    if not math.isfinite(score) or not -100 <= score <= 100:
        score = 0.0
    if romance_eligible(score):
        return "恋人关系"
    if score >= 85:
        return "高度信任的朋友"
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
