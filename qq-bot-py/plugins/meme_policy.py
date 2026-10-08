from collections.abc import Mapping
from typing import Any


def resolve_group_rate(
    overrides: Mapping[Any, Any],
    group_id: int,
    key: str,
    default: Any,
) -> Any:
    """读取群级配置，兼容数字/字符串群号并保留显式的 0。"""
    group_cfg = overrides.get(group_id)
    if group_cfg is None:
        group_cfg = overrides.get(str(group_id))
    if not isinstance(group_cfg, Mapping) or key not in group_cfg:
        return default
    return group_cfg[key]


def group_is_enabled(group_id: int, enabled_groups: set[int]) -> bool:
    return int(group_id) in enabled_groups
