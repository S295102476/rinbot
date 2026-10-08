"""Task-local metadata shared by Agent jobs and provider request logs."""

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator
from uuid import uuid4


REQUEST_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar(
    "agent_request_context", default=None
)


@contextmanager
def request_context(purpose: str, group_id: int, timeout: float) -> Iterator[str]:
    request_id = uuid4().hex[:12]
    token = REQUEST_CONTEXT.set({
        "request_id": request_id,
        "purpose": purpose,
        "group_id": int(group_id),
        "timeout": float(timeout),
    })
    try:
        yield request_id
    finally:
        REQUEST_CONTEXT.reset(token)
