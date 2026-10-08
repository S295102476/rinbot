"""Policy and admission checks for the local QQ Agent.

The policy deliberately stays independent from the model.  A model may suggest
an action, but this module decides whether that action is allowed to run.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


def _as_int_set(values: object) -> set[int]:
    result: set[int] = set()
    if not isinstance(values, (list, tuple, set)):
        return result
    for value in values:
        try:
            result.add(int(value))
        except (TypeError, ValueError):
            continue
    return result


@dataclass(frozen=True)
class AgentPolicy:
    enabled: bool
    mode: str
    active_groups: frozenset[int]
    admin_users: frozenset[int]
    cooldown_seconds: float
    burst_window_seconds: float
    max_context_messages: int
    daily_decision_limit: int
    daily_reply_limit: int
    max_reply_chars: int
    max_iterations: int
    mention_policy: str = "always"
    cadence_seconds: float = 10.0

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "AgentPolicy":
        raw = config.get("agent") or {}
        group = raw.get("group") or {}
        dev = raw.get("dev") or {}
        return cls(
            enabled=bool(raw.get("enabled", False)),
            mode=str(raw.get("mode", "shadow") or "shadow").lower(),
            active_groups=frozenset(_as_int_set(raw.get("active_groups", []))),
            admin_users=frozenset(_as_int_set(dev.get("admin_users", raw.get("admin_users", [])))),
            cooldown_seconds=max(0.0, float(group.get("cooldown_seconds", 20))),
            burst_window_seconds=max(0.0, float(group.get("burst_window_seconds", 3))),
            max_context_messages=max(5, int(group.get("max_context_messages", 50))),
            daily_decision_limit=max(0, int(group.get("daily_decision_limit", 500))),
            daily_reply_limit=max(0, int(group.get("daily_reply_limit", 80))),
            max_reply_chars=max(20, int(group.get("max_reply_chars", 160))),
            max_iterations=max(1, min(5, int(raw.get("max_iterations", 3)))),
            mention_policy=str(group.get("mention_policy", "always") or "always").lower(),
            cadence_seconds=max(0.0, float(group.get("cadence_seconds", 10))),
        )

    def group_enabled(self, group_id: int) -> bool:
        return self.enabled and int(group_id) in self.active_groups

    def user_is_admin(self, user_id: int) -> bool:
        return int(user_id) in self.admin_users

    def shadow_mode(self) -> bool:
        return self.mode != "active"


class GroupAdmission:
    """In-process burst/cooldown guard.

    Redis remains the source of truth for daily budgets in the runtime.  This
    small guard prevents duplicate tasks caused by rapid OneBot deliveries.
    """

    def __init__(self) -> None:
        self._last_started: dict[int, float] = {}
        self._last_seen: dict[int, float] = {}

    def observe(self, group_id: int) -> bool:
        now = time.monotonic()
        group_id = int(group_id)
        self._last_seen[group_id] = now
        previous = self._last_started.get(group_id, 0.0)
        return previous == 0.0

    def allow_start(self, group_id: int, cooldown_seconds: float) -> bool:
        now = time.monotonic()
        group_id = int(group_id)
        previous = self._last_started.get(group_id, 0.0)
        if previous and now - previous < max(0.0, cooldown_seconds):
            return False
        self._last_started[group_id] = now
        return True

    def last_seen(self, group_id: int) -> float:
        return self._last_seen.get(int(group_id), 0.0)
