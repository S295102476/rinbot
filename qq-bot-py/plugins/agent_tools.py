"""Explicit, permissioned tools exposed to the QQ Agent.

Tools return data; command-style tools may also send the original plugin card
through the current group event.  The model still cannot choose arbitrary QQ
targets or call unregistered handlers.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import re
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import yaml
from nonebot.log import logger


ToolHandler = Callable[[dict[str, Any], dict[str, Any]], Awaitable[Any]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    scopes: frozenset[str]
    requires_approval: bool
    handler: ToolHandler
    parameters: dict[str, Any] | None = None

    def schema(self) -> dict[str, Any]:
        schema = {
            "name": self.name,
            "description": self.description,
            "requires_approval": self.requires_approval,
        }
        if self.parameters:
            schema["parameters"] = self.parameters
        return schema


def _load_config() -> dict[str, Any]:
    config_path = Path("config.yaml")
    if not config_path.exists():
        config_path = Path(__file__).resolve().parents[1] / "config.yaml"
    try:
        return yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


_CONFIG = _load_config()
_DEV_CONFIG = ((_CONFIG.get("agent") or {}).get("dev") or {})
_WORKSPACE = Path(str(_DEV_CONFIG.get("workspace") or Path(__file__).resolve().parents[1])).resolve()
_MAX_READ_BYTES = 50_000
_MAX_SEARCH_FILE_BYTES = 1_000_000
_SECRET_NAMES = {
    ".env",
    ".env.local",
    "config.yaml",
    "config.yml",
    "openai_config.json",
}
_SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", "backups"}
_DEV_ALLOWED_CHECKS = frozenset(
    str(value) for value in (
        _DEV_CONFIG.get("allowed_checks")
        or ["git_diff_check", "py_compile", "pytest"]
    )
)
_DEV_DEFAULT_CHECKS = tuple(
    str(value) for value in (
        _DEV_CONFIG.get("default_checks") or ["git_diff_check"]
    )
    if str(value) in _DEV_ALLOWED_CHECKS
)
_DEV_COMMAND_TIMEOUT = max(10, int(_DEV_CONFIG.get("max_command_seconds", 120)))
_MUTE_CONFIG = _CONFIG.get("mute_control") or {}
_MUTE_EXEMPT_USERS = {
    int(value) for value in (_MUTE_CONFIG.get("exempt_users") or [])
}
_AGENT_ADMINS = {
    int(value) for value in (
        ((_CONFIG.get("agent") or {}).get("dev") or {}).get("admin_users") or []
    )
}
_COMMAND_ADMINS = {
    int(value) for value in ((_CONFIG.get("setu") or {}).get("admin_users") or [])
}
_MUTE_DONE: set[str] = set()
_MUTE_INFLIGHT: set[str] = set()
_MUTE_LOCK = asyncio.Lock()
_SIGN_IN_INTENT = re.compile(r"(?:签到|签个到|打卡|(?:给|帮)我.{0,2}签(?:到)?)(?!名)")
_SETU_INTENT = re.compile(r"(?:涩图|色图|瑟图|来点福利|发点福利)", re.IGNORECASE)
_SETU_DONE: set[str] = set()
_SETU_INFLIGHT: set[str] = set()
_SETU_LOCK = asyncio.Lock()
_AFFINITY_QUERY_DONE: set[str] = set()
_AFFINITY_QUERY_INFLIGHT: set[str] = set()
_AFFINITY_QUERY_LOCK = asyncio.Lock()


def _is_affinity_query_message(text: str) -> bool:
    """Check the newest message, never the historical context, for a query."""
    normalized = re.sub(r"\s+", "", str(text or "")).strip()
    if not normalized or not re.search(r"好感度|好感排行|好感排名|好感榜", normalized):
        return False
    if normalized in {"好感度", "好感排行", "好感排名", "好感榜"}:
        return True
    return any(
        marker in normalized
        for marker in (
            "查询", "查下", "查一下", "查看", "看看", "看下", "看一下", "排行", "排名",
            "榜单", "列表", "多少", "几分", "显示", "给我", "告诉我", "帮我查", "能不能查",
        )
    )

try:
    import redis as redis_lib

    _redis_cfg = _CONFIG.get("redis") or {}
    _MUTE_REDIS = redis_lib.Redis(
        host=_redis_cfg.get("host", "127.0.0.1"),
        port=int(_redis_cfg.get("port", 6379)),
        password=_redis_cfg.get("password") or None, db=int(_redis_cfg.get("db", 0)),
        decode_responses=True,
    )
except Exception:
    _MUTE_REDIS = None


def _is_secret_path(path: Path) -> bool:
    lowered_parts = {part.lower() for part in path.parts}
    name = path.name.lower()
    return bool(
        name in _SECRET_NAMES
        or name.startswith(".env")
        or lowered_parts.intersection({"secrets", "secret"})
    )


def _safe_path(
    value: object,
    *,
    allow_directory: bool = False,
    allow_missing: bool = False,
) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("path is required")
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = _WORKSPACE / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(_WORKSPACE)
    except ValueError as exc:
        raise ValueError("path is outside the configured workspace") from exc
    if _is_secret_path(resolved):
        raise PermissionError("secret files are not available to the agent")
    if not allow_missing and allow_directory and not resolved.exists():
        raise FileNotFoundError(str(resolved.relative_to(_WORKSPACE)))
    if not allow_missing and not allow_directory and not resolved.is_file():
        raise FileNotFoundError(str(resolved.relative_to(_WORKSPACE)))
    return resolved


async def _list_project_files(args: dict[str, Any], _ctx: dict[str, Any]) -> dict[str, Any]:
    root = _safe_path(args.get("path") or ".", allow_directory=True)
    if not root.is_dir():
        raise NotADirectoryError(str(root.relative_to(_WORKSPACE)))
    max_depth = max(1, min(6, int(args.get("max_depth") or 3)))
    max_entries = max(20, min(500, int(args.get("max_entries") or 250)))
    entries: list[str] = []
    root_depth = len(root.parts)
    for current, dirs, files in os.walk(root):
        current_path = Path(current)
        depth = len(current_path.parts) - root_depth
        dirs[:] = sorted(
            name for name in dirs
            if name not in _SKIP_DIRS and not _is_secret_path(current_path / name)
        )
        if depth >= max_depth:
            dirs[:] = []
        for name in sorted(files):
            path = current_path / name
            if _is_secret_path(path):
                continue
            entries.append(path.relative_to(_WORKSPACE).as_posix())
            if len(entries) >= max_entries:
                return {"entries": entries, "truncated": True}
    return {"entries": entries, "truncated": False}


async def _read_project_file(args: dict[str, Any], _ctx: dict[str, Any]) -> str:
    path = _safe_path(args.get("path"))
    start_line = max(1, int(args.get("start_line") or 1))
    max_lines = max(1, min(500, int(args.get("max_lines") or 240)))

    def read_lines() -> str:
        lines: list[str] = []
        used_bytes = 0
        truncated = False
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line_number, line in enumerate(handle, 1):
                if line_number < start_line:
                    continue
                encoded_size = len(line.encode("utf-8", errors="replace"))
                if len(lines) >= max_lines or used_bytes + encoded_size > _MAX_READ_BYTES:
                    truncated = True
                    break
                lines.append(f"{line_number:>6}: {line.rstrip()}")
                used_bytes += encoded_size
        suffix = "\n[内容已截断，可提高 start_line 继续读取]" if truncated else ""
        return "\n".join(lines) + suffix

    return await asyncio.to_thread(read_lines)


async def _search_project(args: dict[str, Any], _ctx: dict[str, Any]) -> list[dict[str, Any]]:
    query = str(args.get("query") or "").strip()
    if not query or len(query) > 200:
        raise ValueError("query must contain 1-200 characters")
    root = _safe_path(args.get("path") or ".", allow_directory=True)
    if not root.is_dir():
        raise NotADirectoryError(str(root.relative_to(_WORKSPACE)))
    file_glob = str(args.get("file_glob") or "*").strip() or "*"
    max_results = max(1, min(100, int(args.get("max_results") or 50)))
    results: list[dict[str, Any]] = []

    def scan() -> None:
        for current, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
            for filename in files:
                path = Path(current) / filename
                if _is_secret_path(path) or not fnmatch.fnmatch(filename, file_glob):
                    continue
                try:
                    if path.stat().st_size > _MAX_SEARCH_FILE_BYTES:
                        continue
                except OSError:
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                for line_no, line in enumerate(text.splitlines(), 1):
                    if query.casefold() in line.casefold():
                        results.append({
                            "path": str(path.relative_to(_WORKSPACE)),
                            "line": line_no,
                            "text": line[:300],
                        })
                        if len(results) >= max_results:
                            return

    await asyncio.to_thread(scan)
    return results


async def _git_readonly(args: dict[str, Any], _ctx: dict[str, Any]) -> str:
    command = str(args.get("command") or "status")
    if command not in {"status", "diff"}:
        raise ValueError("only git status and git diff are available")
    argv = ["git", "status", "--short"] if command == "status" else [
        "git", "diff", "--", ".",
        ":(exclude)config.yaml",
        ":(exclude)config.yml",
        ":(exclude).env",
        ":(exclude).env.*",
    ]
    completed = await asyncio.to_thread(
        subprocess.run,
        argv,
        cwd=str(_WORKSPACE),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    output = (completed.stdout or completed.stderr or "").strip()
    return output[:20_000] or "工作区干净。"


def _validated_check_names(value: object) -> list[str]:
    values = value if isinstance(value, list) else list(_DEV_DEFAULT_CHECKS)
    result: list[str] = []
    for item in values:
        name = str(item or "").strip()
        if not name or name in result:
            continue
        if name not in _DEV_ALLOWED_CHECKS:
            raise PermissionError(f"检查不可用：{name}")
        result.append(name)
    return result[:3]


def _validated_check_targets(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    targets: list[str] = []
    for item in value[:12]:
        path = _safe_path(item, allow_directory=True)
        targets.append(path.relative_to(_WORKSPACE).as_posix())
    return targets


async def _run_project_check(args: dict[str, Any], _ctx: dict[str, Any]) -> dict[str, Any]:
    check = str(args.get("check") or "").strip()
    if check not in _DEV_ALLOWED_CHECKS:
        raise PermissionError(f"检查不可用：{check}")
    targets = _validated_check_targets(args.get("targets"))
    if check == "git_diff_check":
        argv = ["git", "diff", "--check", "--", "."]
    elif check == "py_compile":
        argv = [sys.executable, "-m", "compileall", "-q", *(targets or ["plugins"])]
    elif check == "pytest":
        argv = [sys.executable, "-m", "pytest", *(targets or ["tests"]), "-q"]
    else:
        raise PermissionError(f"检查不可用：{check}")
    timeout = max(5, min(_DEV_COMMAND_TIMEOUT, int(args.get("timeout_seconds") or _DEV_COMMAND_TIMEOUT)))
    try:
        completed = await asyncio.to_thread(
            subprocess.run,
            argv,
            cwd=str(_WORKSPACE),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        output = "\n".join(
            part.strip() for part in (completed.stdout, completed.stderr) if part and part.strip()
        )
        return {
            "check": check,
            "ok": completed.returncode == 0,
            "exit_code": int(completed.returncode),
            "output": output[-20_000:] or "检查完成，无输出。",
        }
    except subprocess.TimeoutExpired:
        return {"check": check, "ok": False, "exit_code": -1, "output": f"检查超时（{timeout}s）"}


class ApprovalStore:
    """Short-lived approval records backed by Redis when available."""

    def __init__(self) -> None:
        self._memory: dict[str, tuple[float, dict[str, Any]]] = {}
        self._redis = None
        try:
            import redis

            redis_cfg = _CONFIG.get("redis") or {}
            self._redis = redis.Redis(
                host=redis_cfg.get("host", "127.0.0.1"),
                port=int(redis_cfg.get("port", 6379)),
                password=redis_cfg.get("password") or None, db=int(redis_cfg.get("db", 0)),
                decode_responses=True,
            )
        except Exception:
            self._redis = None

    async def create(self, payload: dict[str, Any], ttl: int = 300) -> str:
        token = secrets.token_urlsafe(9)
        record = {**payload, "created_at": int(time.time())}
        if self._redis is not None:
            try:
                await asyncio.to_thread(
                    self._redis.setex,
                    f"agent:approval:{token}",
                    max(30, ttl),
                    json.dumps(record, ensure_ascii=False),
                )
                return token
            except Exception:
                pass
        self._memory[token] = (time.time() + max(30, ttl), record)
        return token

    async def pop(self, token: str, user_id: int | None = None) -> dict[str, Any] | None:
        token = str(token or "").strip()
        if self._redis is not None:
            try:
                raw = await asyncio.to_thread(self._redis.get, f"agent:approval:{token}")
                if raw:
                    record = json.loads(raw)
                    if user_id is not None and int(record.get("user_id", 0)) != int(user_id):
                        return None
                    await asyncio.to_thread(self._redis.delete, f"agent:approval:{token}")
                    return record
            except Exception:
                pass
        item = self._memory.pop(token, None)
        if item and item[0] >= time.time():
            if user_id is not None and int(item[1].get("user_id", 0)) != int(user_id):
                self._memory[token] = item
                return None
            return item[1]
        return None


APPROVALS = ApprovalStore()


def _patch_paths(patch: str) -> list[Path]:
    paths: list[Path] = []
    for raw in re.findall(r"^\+\+\+ b/(.+)$", patch, flags=re.MULTILINE):
        paths.append(_safe_path(raw, allow_directory=True, allow_missing=True))
    if not paths:
        raise ValueError("patch does not contain a safe target file")
    return paths


async def _propose_patch(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    if not ctx.get("is_admin"):
        raise PermissionError("开发修改只对管理员开放")
    patch = str(args.get("patch") or "").strip()
    if not patch or len(patch) > 100_000:
        raise ValueError("patch is empty or too large")
    _patch_paths(patch)
    checks = _validated_check_names(args.get("checks"))
    ttl = int(_DEV_CONFIG.get("approval_ttl_seconds", 300))
    token = await APPROVALS.create(
        {
            "user_id": int(ctx.get("user_id", 0)),
            "patch": patch,
            "summary": str(args.get("summary") or "Agent 提议修改项目文件")[:500],
            "checks": checks,
        },
        ttl=ttl,
    )
    return {
        "status": "pending_approval",
        "approval_token": token,
        "expires_in": ttl,
        "summary": str(args.get("summary") or "Agent 提议修改项目文件")[:500],
        "checks": checks,
    }


async def approve_patch(token: str, user_id: int) -> str:
    record = await APPROVALS.pop(token, user_id=user_id)
    if not record:
        return "审批不存在、已过期、已被使用，或当前账号不是该审批的发起人。"
    patch = str(record.get("patch") or "")
    try:
        _patch_paths(patch)
        checked = await asyncio.to_thread(
            subprocess.run,
            ["git", "apply", "--check", "-"],
            cwd=str(_WORKSPACE),
            input=patch,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if checked.returncode != 0:
            return f"补丁检查失败：{(checked.stderr or checked.stdout).strip()[:1000]}"
        applied = await asyncio.to_thread(
            subprocess.run,
            ["git", "apply", "-"],
            cwd=str(_WORKSPACE),
            input=patch,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if applied.returncode != 0:
            return f"补丁应用失败：{(applied.stderr or applied.stdout).strip()[:1000]}"
        check_names = _validated_check_names(record.get("checks"))
        check_results: list[dict[str, Any]] = []
        for check in check_names:
            check_results.append(await _run_project_check({"check": check}, {}))
        if not check_results:
            return "补丁已应用，未配置自动检查。"
        lines = ["补丁已应用。自动检查结果："]
        for result in check_results:
            status = "通过" if result.get("ok") else "失败"
            output = str(result.get("output") or "").strip()
            lines.append(f"- {result.get('check')}: {status}")
            if not result.get("ok") and output:
                lines.append(output[-1200:])
        return "\n".join(lines)
    except Exception as exc:
        return f"补丁应用失败：{type(exc).__name__}: {exc}"


async def reject_patch(token: str, user_id: int) -> str:
    record = await APPROVALS.pop(token, user_id=user_id)
    if not record:
        return "审批不存在、已过期、已被使用，或当前账号不是该审批的发起人。"
    return "已拒绝该补丁，项目文件没有被修改。"


async def _search_web(args: dict[str, Any], _ctx: dict[str, Any]) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ValueError("query is required")
    try:
        from .ai_chat import _call_antigravity_openai, _antigravity_available
        if _antigravity_available():
            return await _call_antigravity_openai(
                [{"role": "user", "content": query}], enable_search=True)
        raw = _CONFIG.get("web_search") or {}
        if not raw.get("api_key") or not raw.get("model"):
            raise ValueError("Configure web_search.api_key/model or ai.antigravity before enabling search.")
        from .web_search import _call_gemini_search
        text, _tokens = await _call_gemini_search(query, [])
        return text
    except Exception as exc:
        raise RuntimeError(f"Search unavailable: {type(exc).__name__}") from exc


async def _send_visible_tool_message(ctx: dict[str, Any], message: Any) -> bool:
    """Send a command-style result immediately, while keeping the tool auditable."""
    bot = ctx.get("bot")
    group_id = int(ctx.get("group_id") or 0)
    if bot is None or group_id <= 0:
        return False
    await bot.send_group_msg(group_id=group_id, message=message)
    try:
        from .agent_metrics import record_agent_messages

        await record_agent_messages(group_id, 1)
    except Exception as exc:
        logger.debug(f"[agent_metrics] tool message record failed: {type(exc).__name__}")
    return True


def _affinity_query_key(group_id: int, source_message_id: int) -> str:
    return f"{int(group_id)}:{int(source_message_id)}"


async def claim_affinity_query(group_id: int, source_message_id: int) -> bool:
    """Claim one affinity-card query for a source message.

    A group command and the Agent can otherwise race (or a duplicated event can
    be delivered twice) and send the same card more than once.  Redis extends
    the claim across workers; the in-process sets still protect the common case
    when Redis is unavailable.
    """
    key = _affinity_query_key(group_id, source_message_id)
    async with _AFFINITY_QUERY_LOCK:
        if key in _AFFINITY_QUERY_DONE or key in _AFFINITY_QUERY_INFLIGHT:
            return False
        if _MUTE_REDIS is not None:
            try:
                claimed = await asyncio.to_thread(
                    _MUTE_REDIS.set,
                    f"agent:affinity:idempotency:{key}",
                    "pending",
                    nx=True,
                    ex=86400,
                )
                if not claimed:
                    _AFFINITY_QUERY_DONE.add(key)
                    return False
            except Exception:
                # A cache outage must not make the read-only command unusable.
                pass
        _AFFINITY_QUERY_INFLIGHT.add(key)
        return True


async def finish_affinity_query(group_id: int, source_message_id: int) -> None:
    """Mark an affinity query complete, including when sending raised."""
    key = _affinity_query_key(group_id, source_message_id)
    async with _AFFINITY_QUERY_LOCK:
        _AFFINITY_QUERY_INFLIGHT.discard(key)
        _AFFINITY_QUERY_DONE.add(key)
        if _MUTE_REDIS is not None:
            try:
                await asyncio.to_thread(
                    _MUTE_REDIS.setex,
                    f"agent:affinity:idempotency:{key}",
                    86400,
                    "1",
                )
            except Exception:
                pass


def _resolve_batch_source_event(args: dict[str, Any], ctx: dict[str, Any]) -> Any:
    try:
        source_message_id = int(args.get("source_message_id") or 0)
    except (TypeError, ValueError):
        raise ValueError("source_message_id must be an integer")
    if source_message_id <= 0:
        raise ValueError("source_message_id is required")
    for event in ctx.get("batch_events") or []:
        if int(getattr(event, "message_id", 0) or 0) == source_message_id:
            return event
    raise PermissionError("source message is not in the current batch")


async def _get_affinity(args: dict[str, Any], ctx: dict[str, Any]) -> str:
    """Send the filtered affinity card used by #好感度."""
    event = _resolve_batch_source_event(args, ctx)
    bot = ctx.get("bot")
    if bot is None or event is None:
        raise ValueError("group event context is required")
    group_id = int(ctx.get("group_id") or event.group_id)
    source_message_id = int(getattr(event, "message_id", 0) or 0)
    if source_message_id <= 0:
        raise ValueError("source message id is required")
    batch_events = [item for item in (ctx.get("batch_events") or []) if item is not None]
    if batch_events:
        latest_event = batch_events[-1]
        latest_id = int(getattr(latest_event, "message_id", 0) or 0)
        latest_getter = getattr(latest_event, "get_plaintext", None)
        latest_text = str(latest_getter() or "") if callable(latest_getter) else ""
        if source_message_id != latest_id or not _is_affinity_query_message(latest_text):
            logger.info(
                f"[agent_tool] name=get_affinity status=blocked "
                f"group={group_id} source_message_id={source_message_id} latest_message_id={latest_id}"
            )
            raise PermissionError(
                "只有当前批次最新消息明确查询好感度时才能调用该工具"
            )
    if not await claim_affinity_query(group_id, source_message_id):
        logger.info(
            f"[agent_tool] name=get_affinity status=duplicate "
            f"group={group_id} source_message_id={source_message_id}"
        )
        return "这条消息的好感度排行已经查询过了，不要重复发送。"
    from .mute_control import render_affinity_rank

    try:
        result = await render_affinity_rank(bot, event, target_group=group_id)
        if await _send_visible_tool_message(ctx, result):
            return "已发送原有的好感度排行卡片。不要再次用文字复述整张榜单。"
        return str(result)
    finally:
        await finish_affinity_query(group_id, source_message_id)


async def _sign_in(args: dict[str, Any], ctx: dict[str, Any]) -> str:
    """Send the same image card as #签到, then return a short tool observation."""
    event = _resolve_batch_source_event(args, ctx)
    bot = ctx.get("bot")
    if bot is None or event is None:
        raise ValueError("group event context is required")
    if not _SIGN_IN_INTENT.search(str(event.get_plaintext() or "")):
        raise PermissionError("source message does not contain a sign-in request")
    from .sign_in import render_sign_in

    result = await render_sign_in(bot, event)
    if await _send_visible_tool_message(ctx, result):
        return "已发送原有的签到卡片。不要再次用文字复述卡片全部内容。"
    return str(result)


async def _claim_setu(key: str) -> bool:
    """Claim a natural-language setu request before consuming its quota."""
    async with _SETU_LOCK:
        if key in _SETU_DONE or key in _SETU_INFLIGHT:
            return False
        if _MUTE_REDIS is not None:
            try:
                claimed = await asyncio.to_thread(
                    _MUTE_REDIS.set,
                    f"agent:setu:idempotency:{key}",
                    "pending",
                    nx=True,
                    ex=86400,
                )
                if not claimed:
                    _SETU_DONE.add(key)
                    return False
            except Exception:
                pass
        _SETU_INFLIGHT.add(key)
        return True


async def _finish_setu_claim(key: str) -> None:
    async with _SETU_LOCK:
        _SETU_INFLIGHT.discard(key)
        _SETU_DONE.add(key)
        if _MUTE_REDIS is not None:
            try:
                await asyncio.to_thread(
                    _MUTE_REDIS.setex,
                    f"agent:setu:idempotency:{key}",
                    86400,
                    "1",
                )
            except Exception:
                pass


def _setu_tags(args: dict[str, Any]) -> list[str]:
    raw_tags = args.get("tags")
    if isinstance(raw_tags, str):
        values = raw_tags.split()
    elif isinstance(raw_tags, list):
        values = raw_tags
    else:
        values = []
    tags: list[str] = []
    for value in values:
        tag = re.sub(r"\s+", " ", str(value or "")).strip()
        if not tag or len(tag) > 40 or tag in tags:
            continue
        tags.append(tag)
        if len(tags) >= 4:
            break
    return tags


async def _send_setu(args: dict[str, Any], ctx: dict[str, Any]) -> str:
    """Send actual setu content through the established quota and R18 policy."""
    event = _resolve_batch_source_event(args, ctx)
    bot = ctx.get("bot")
    group_id = int(ctx.get("group_id") or getattr(event, "group_id", 0) or 0)
    if bot is None or group_id <= 0:
        raise ValueError("group event context is required")
    plaintext = str(event.get_plaintext() or "")
    if not _SETU_INTENT.search(plaintext):
        raise PermissionError("source message does not contain an explicit setu request")

    source_message_id = int(getattr(event, "message_id", 0) or 0)
    claim_key = f"{group_id}:{source_message_id}"
    if not await _claim_setu(claim_key):
        return "这条请求已经处理过了，不要重复发送。"

    async def notice(message: str) -> None:
        await _send_visible_tool_message(ctx, message)

    try:
        from .setu import (
            AGENT_NUM,
            _chinese_to_pixiv_tags,
            is_disallowed_setu_request,
            send_setu,
        )

        tags = _setu_tags(args)
        if is_disallowed_setu_request(plaintext) or is_disallowed_setu_request(tags):
            await notice("这个主题不能处理，换成明确成年角色或普通主题吧。")
            return "涩图请求未执行：请求主题不被允许。"
        if tags:
            # Agent callers describe images in natural language.  The existing
            # command's Pixiv tag conversion is reused, but R18 and count stay
            # entirely server-controlled.
            tags = await _chinese_to_pixiv_tags(" ".join(tags))
        result = await send_setu(
            bot,
            group_id,
            int(event.user_id),
            tags=tags,
            target_count=AGENT_NUM,
            force_safe_mode=True,
            announce=False,
            notify=notice,
        )
        if int(result.get("sent") or 0) > 0:
            try:
                from .agent_metrics import record_agent_messages

                await record_agent_messages(group_id, 1)
            except Exception as exc:
                logger.debug(f"[agent_metrics] setu message record failed: {type(exc).__name__}")
        status = str(result.get("status") or "unknown")
        sent = int(result.get("sent") or 0)
        return f"涩图工具已执行，status={status}，实际发送={sent}。不要声称发送了更多图片。"
    finally:
        # The request may already have consumed quota or sent a forward message
        # when a OneBot response fails, so retries must remain disabled.
        await _finish_setu_claim(claim_key)


def _event_at_users(event: Any) -> set[int]:
    result: set[int] = set()
    for segment in getattr(event, "message", ()):
        if getattr(segment, "type", "") != "at":
            continue
        try:
            result.add(int(segment.data.get("qq")))
        except (AttributeError, TypeError, ValueError):
            continue
    return result


async def _member_role(bot: Any, group_id: int, user_id: int, event: Any = None) -> str:
    if event is not None and int(getattr(event, "user_id", 0) or 0) == int(user_id):
        role = str(getattr(getattr(event, "sender", None), "role", "") or "").lower()
        if role:
            return role
    try:
        member = await bot.get_group_member_info(group_id=int(group_id), user_id=int(user_id))
        return str(member.get("role") or "member").lower()
    except Exception:
        return "member"


async def _claim_mute(key: str) -> bool:
    async with _MUTE_LOCK:
        if key in _MUTE_DONE or key in _MUTE_INFLIGHT:
            return False
        if _MUTE_REDIS is not None:
            try:
                redis_key = f"agent:mute:idempotency:{key}"
                claimed = await asyncio.to_thread(
                    _MUTE_REDIS.set,
                    redis_key,
                    "pending",
                    nx=True,
                    ex=7 * 86400,
                )
                if not claimed:
                    _MUTE_DONE.add(key)
                    return False
            except Exception:
                pass
        _MUTE_INFLIGHT.add(key)
        return True


async def _finish_mute_claim(key: str) -> None:
    async with _MUTE_LOCK:
        _MUTE_INFLIGHT.discard(key)
        _MUTE_DONE.add(key)
        if _MUTE_REDIS is not None:
            try:
                await asyncio.to_thread(
                    _MUTE_REDIS.setex,
                    f"agent:mute:idempotency:{key}",
                    7 * 86400,
                    "1",
                )
            except Exception:
                pass


async def _mute_user(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Execute the Agent's moderation decision with local permission checks."""
    bot = ctx.get("bot")
    group_id = int(ctx.get("group_id") or 0)
    current_event = ctx.get("event")
    batch_events = [item for item in (ctx.get("batch_events") or []) if item is not None]
    if bot is None or group_id <= 0 or current_event is None or not batch_events:
        raise ValueError("group batch context is required")

    try:
        source_message_id = int(args.get("source_message_id") or getattr(current_event, "message_id", 0))
    except (TypeError, ValueError):
        raise ValueError("source_message_id must be an integer")
    source_event = next(
        (item for item in batch_events if int(getattr(item, "message_id", 0)) == source_message_id),
        None,
    )
    if source_event is None or source_message_id <= 0:
        raise PermissionError("source message is not in the current batch")

    requester_id = int(getattr(source_event, "user_id", 0) or 0)
    try:
        target_user_id = int(args.get("target_user_id") or requester_id)
        suggested_duration = int(args.get("duration_seconds") or 60)
    except (TypeError, ValueError):
        raise ValueError("target_user_id and duration_seconds must be integers")
    duration = max(60, min(1800, suggested_duration))

    requester_role = await _member_role(bot, group_id, requester_id, source_event)
    requester_is_admin = (
        requester_id in _AGENT_ADMINS
        or requester_id in _COMMAND_ADMINS
        or requester_role in {"owner", "admin"}
    )
    allowed_targets = {int(getattr(item, "user_id", 0) or 0) for item in batch_events}
    for item in batch_events:
        allowed_targets.update(_event_at_users(item))
    allowed_targets.discard(0)
    if target_user_id not in allowed_targets:
        raise PermissionError("target user is not present in the current batch")
    if not requester_is_admin and target_user_id != requester_id:
        raise PermissionError("ordinary users may only request muting themselves")

    bot_id = int(getattr(bot, "self_id", 0) or 0)
    if target_user_id == bot_id:
        raise PermissionError("the bot cannot mute itself")
    target_role = await _member_role(bot, group_id, target_user_id)
    if (
        target_role in {"owner", "admin"}
        or target_user_id in _AGENT_ADMINS
        or target_user_id in _COMMAND_ADMINS
        or target_user_id in _MUTE_EXEMPT_USERS
    ):
        raise PermissionError("owners, administrators and exempt users cannot be muted")

    idempotency_key = f"{group_id}:{source_message_id}"
    if not await _claim_mute(idempotency_key):
        return {
            "status": "duplicate",
            "target_user_id": target_user_id,
            "source_message_id": source_message_id,
        }
    try:
        await bot.set_group_ban(
            group_id=group_id,
            user_id=target_user_id,
            duration=duration,
        )
    finally:
        # OneBot may apply the mute and then lose the response. Never repeat the
        # same source message automatically, even when the API call raises.
        await _finish_mute_claim(idempotency_key)
    return {
        "status": "executed",
        "target_user_id": target_user_id,
        "duration_seconds": duration,
        "source_message_id": source_message_id,
        "reason": str(args.get("reason") or "")[:120],
    }


TOOLS: dict[str, ToolSpec] = {
    "search_web": ToolSpec(
        "search_web", "联网搜索并返回简洁结果", frozenset({"group", "dev"}), False, _search_web
    ),
    "get_affinity": ToolSpec(
        "get_affinity",
        "查询source_message_id所在的本群好感度排行并发送排行卡片（隐藏管理员管理员；不要声称这是完整名单）",
        frozenset({"group"}),
        False,
        _get_affinity,
        {
            "type": "object",
            "properties": {
                "source_message_id": {"type": "integer", "description": "发起查询的当前批次消息ID"},
            },
            "required": ["source_message_id"],
        },
    ),
    "sign_in": ToolSpec(
        "sign_in",
        "为source_message_id对应消息的发送者执行每日签到并发送原有签到卡片",
        frozenset({"group"}),
        False,
        _sign_in,
        {
            "type": "object",
            "properties": {
                "source_message_id": {"type": "integer", "description": "明确提出签到请求的当前批次消息ID"},
            },
            "required": ["source_message_id"],
        },
    ),
    "send_setu": ToolSpec(
        "send_setu",
        "为明确提出涩图请求的source_message_id发送真实涩图；群分级、R18策略、每日配额和图片数均由服务器控制",
        frozenset({"group"}),
        False,
        _send_setu,
        {
            "type": "object",
            "properties": {
                "source_message_id": {"type": "integer", "description": "明确提出涩图请求的当前批次消息ID"},
                "tags": {
                    "type": "array",
                    "items": {"type": "string", "maxLength": 40},
                    "maxItems": 4,
                    "description": "可选的主题或角色标签；不得填写R18开关、数量、群号或用户号",
                },
            },
            "required": ["source_message_id"],
        },
    ),
    "mute_user": ToolSpec(
        "mute_user",
        "直接禁言当前批次中的违规发言者；普通用户只能请求禁言自己，管理员可请求禁言当前批次目标",
        frozenset({"group"}),
        False,
        _mute_user,
        {
            "type": "object",
            "properties": {
                "target_user_id": {"type": "integer", "description": "被禁言者QQ，必须来自当前批次"},
                "duration_seconds": {"type": "integer", "minimum": 60, "maximum": 1800},
                "source_message_id": {"type": "integer", "description": "触发决定的当前批次消息ID"},
                "reason": {"type": "string", "maxLength": 120},
            },
            "required": ["target_user_id", "duration_seconds", "source_message_id"],
        },
    ),
    "read_project_file": ToolSpec(
        "read_project_file",
        "按行读取项目中的非敏感文本文件；长文件应分段读取",
        frozenset({"dev"}),
        False,
        _read_project_file,
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "工作区内相对路径"},
                "start_line": {"type": "integer", "minimum": 1},
                "max_lines": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            "required": ["path"],
        },
    ),
    "list_project_files": ToolSpec(
        "list_project_files",
        "列出项目目录中的非敏感文件，用于先了解项目结构",
        frozenset({"dev"}),
        False,
        _list_project_files,
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "工作区内目录，默认根目录"},
                "max_depth": {"type": "integer", "minimum": 1, "maximum": 6},
                "max_entries": {"type": "integer", "minimum": 20, "maximum": 500},
            },
        },
    ),
    "search_project": ToolSpec(
        "search_project",
        "在项目源代码和文档中搜索文本，可限制目录和文件名模式",
        frozenset({"dev"}),
        False,
        _search_project,
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "maxLength": 200},
                "path": {"type": "string", "description": "工作区内目录，默认根目录"},
                "file_glob": {"type": "string", "description": "文件名模式，如 *.py"},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": ["query"],
        },
    ),
    "git_status": ToolSpec(
        "git_status",
        "查看项目 Git 状态或排除配置密钥后的只读 diff",
        frozenset({"dev"}),
        False,
        _git_readonly,
        {
            "type": "object",
            "properties": {"command": {"type": "string", "enum": ["status", "diff"]}},
        },
    ),
    "run_project_check": ToolSpec(
        "run_project_check",
        "运行固定白名单检查；不能执行任意 shell 命令",
        frozenset({"dev"}),
        False,
        _run_project_check,
        {
            "type": "object",
            "properties": {
                "check": {"type": "string", "enum": sorted(_DEV_ALLOWED_CHECKS)},
                "targets": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 12,
                },
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 5,
                    "maximum": _DEV_COMMAND_TIMEOUT,
                },
            },
            "required": ["check"],
        },
    ),
    "propose_patch": ToolSpec(
        "propose_patch",
        "提交 unified diff 和应用后检查计划，等待管理员确认后应用",
        frozenset({"dev"}),
        True,
        _propose_patch,
        {
            "type": "object",
            "properties": {
                "patch": {"type": "string", "description": "git apply 可接受的 unified diff"},
                "summary": {"type": "string", "maxLength": 500},
                "checks": {
                    "type": "array",
                    "items": {"type": "string", "enum": sorted(_DEV_ALLOWED_CHECKS)},
                    "maxItems": 3,
                },
            },
            "required": ["patch", "summary"],
        },
    ),
}


_TOOL_FEATURES = {"search_web": "web_search", "send_setu": "pixiv", "sign_in": "sign_in"}


def _tool_enabled(name, scope):
    from runtime_config import feature_enabled
    if scope == "dev" and not feature_enabled(_CONFIG, "development_agent"):
        return False
    feature = _TOOL_FEATURES.get(name)
    return feature is None or feature_enabled(_CONFIG, feature)


def tool_schemas(scope: str) -> list[dict[str, Any]]:
    return [spec.schema() for spec in TOOLS.values() if scope in spec.scopes and _tool_enabled(spec.name, scope)]


async def execute_tool(name: str, args: dict[str, Any], scope: str, ctx: dict[str, Any]) -> Any:
    from . import console_runtime as console
    from .agent_requests import request_context

    spec = TOOLS.get(str(name))
    if spec is None or scope not in spec.scopes or not _tool_enabled(str(name), scope):
        raise PermissionError(f"工具不可用：{name}")
    if spec.requires_approval and not ctx.get("is_admin"):
        raise PermissionError("该工具需要管理员权限")
    group_id = int(ctx.get("group_id") or 0)
    source_event = next((event for event in ctx.get("batch_events", [])
                         if str(getattr(event, "message_id", "")) == str(args.get("source_message_id", ""))), ctx.get("event"))
    user_id = int(getattr(source_event, "user_id", ctx.get("user_id") or 0))
    direct_at = bool(ctx.get("bot") and source_event and console.real_at(ctx["bot"], source_event))
    if scope == "group":
        await console.check_agent_action(group_id, direct_at=direct_at)
    try:
        with console.sending(group_id, user_id, source="tool", explicit=True) as state, request_context(f"tool_{name}", group_id, 120):
            state["direct_at"] = direct_at
            result = await asyncio.wait_for(spec.handler(args, ctx), timeout=120)
        if scope == "group":
            console.record("tool", group_id, user_id, tool_name=name, status="success",
                           source_message_id=int(getattr(source_event, "message_id", 0)))
        logger.info(f"[agent_tool] name={name} status=ok")
        return result
    except Exception as exc:
        if scope == "group":
            console.record("tool", group_id, user_id, tool_name=name, status="failed", error=type(exc).__name__)
        logger.warning(f"[agent_tool] name={name} status=error error={type(exc).__name__}")
        raise
