"""Persistent, owner-bound development tasks over an isolated source snapshot.

Production source is read only until a human approves the exact checked diff.
No generated code or project checks are ever executed on the bot host.
"""
from __future__ import annotations

import asyncio
import contextvars
import difflib
import fnmatch
import hashlib
import json
import os
import re
import secrets
import shutil
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from sqlalchemy import Integer, String, Text, select
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import Mapped, mapped_column

from . import db


class DeveloperTask(db.Base):
    __tablename__ = "agent_developer_tasks"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    actor: Mapped[str] = mapped_column(String(100), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    payload_json: Mapped[str] = mapped_column(Text().with_variant(LONGTEXT(), "mysql"))


DEVELOPER_TABLES = [DeveloperTask.__table__]
ACTIVE = {"queued", "running", "checking"}
TERMINAL = {"applied", "rolled_back", "rejected", "cancelled", "failed", "completed", "expired"}
SOURCE_DIRS = {"plugins", "tests", "tools", "console"}
SOURCE_EXTENSIONS = {".py", ".ts", ".tsx", ".js", ".jsx", ".css", ".html", ".toml", ".md"}
ROOT_FILES = {"requirements.txt", "requirements.lock", "pyproject.toml", "pytest.ini", "README.md", "Dockerfile"}
DENY_PARTS = {".git", ".vscode", ".idea", ".venv", "venv", "node_modules", "__pycache__", "data", "runtime", "dist", "build", "backups", "secrets", "secret", "personas", "profiles", "agentdocs"}
SECRET_ASSIGNMENT = re.compile(r"(?i)(?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*['\"]([^'\"\r\n]{12,})['\"]")
TASK_ID = re.compile(r"^[a-f0-9]{24}$")
_CONTEXT: contextvars.ContextVar[tuple[Any, str, str] | None] = contextvars.ContextVar("developer_task", default=None)
_SERVICE: DeveloperTasks | None = None


class TaskConflict(ValueError):
    """A review or source baseline is stale."""


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def path_name(value: Any) -> str:
    raw = str(value or "")
    path = PurePosixPath(raw)
    if not raw or "\\" in raw or ":" in raw or "\x00" in raw or path.is_absolute() or any(p in {"..", "."} for p in raw.split("/")):
        raise ValueError("Use a relative source file path")
    parts = path.parts
    if any(p.lower() in DENY_PARTS or p.startswith(".") for p in parts):
        raise PermissionError("Private paths are excluded")
    if any(word in path.name.lower() for word in ("sftp", "special_users", "credential", "private_key")) or path.name.lower().endswith((".pem", ".key")):
        raise PermissionError("Private paths are excluded")
    if len(parts) == 1:
        allowed = raw in ROOT_FILES or path.suffix == ".py"
    else:
        allowed = parts[0] in SOURCE_DIRS and path.suffix in SOURCE_EXTENSIONS
        if parts[0] == "console":
            allowed = (len(parts) > 2 and parts[1] in {"src", "tests"} and path.suffix in SOURCE_EXTENSIONS) or raw in {"console/package.json", "console/tsconfig.json", "console/vite.config.ts", "console/index.html"}
    if not allowed:
        raise PermissionError("File is outside the source allowlist")
    return path.as_posix()


def inside(root: Path, name: str) -> Path:
    name = path_name(name)
    candidate = root / name
    current = root
    for part in PurePosixPath(name).parts:
        current = current / part
        if current.is_symlink():
            raise PermissionError("Symlinks are unavailable")
    candidate.resolve().relative_to(root.resolve())
    return candidate


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".task-" + secrets.token_hex(6))
    try:
        with temporary.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def unified_change(text: str, patch_lines: list[str]) -> str:
    """Strict unified hunk application; never shell out to git on production."""
    source = text.splitlines(keepends=True)
    output: list[str] = []
    cursor = 0
    index = 0
    while index < len(patch_lines):
        header = re.fullmatch(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@.*\n?", patch_lines[index])
        if not header:
            raise ValueError("Invalid unified diff hunk")
        old_start, old_count = int(header[1]), int(header[2] or 1)
        new_count = int(header[4] or 1)
        start = old_start if old_count == 0 else old_start - 1
        if start < cursor or start > len(source):
            raise TaskConflict("Patch context is stale")
        output.extend(source[cursor:start])
        cursor = start
        used = added = 0
        index += 1
        while index < len(patch_lines) and not patch_lines[index].startswith("@@ "):
            line = patch_lines[index]
            if not line or line[0] not in " +-":
                raise ValueError("Invalid unified diff body")
            content = line[1:]
            if index + 1 < len(patch_lines) and patch_lines[index + 1].startswith("\\ No newline at end of file"):
                content = content.rstrip("\r\n")
                index += 1
            if line[0] in " -":
                if cursor >= len(source) or source[cursor] != content:
                    raise TaskConflict("Patch content does not match snapshot")
                cursor += 1
                used += 1
            if line[0] in " +":
                output.append(content)
                added += 1
            index += 1
        if (used, added) != (old_count, new_count):
            raise ValueError("Patch hunk counts do not match")
    output.extend(source[cursor:])
    return "".join(output)


def apply_unified_patch(root: Path, patch: str) -> list[str]:
    if not patch or len(patch.encode()) > 200_000 or "GIT binary patch" in patch or "Subproject commit" in patch:
        raise ValueError("Expected a text diff below 200 KB")
    lines = patch.splitlines(keepends=True)
    index = 0
    updates: dict[str, bytes | None] = {}
    rename_from = rename_to = None

    def finish_rename():
        nonlocal rename_from, rename_to
        if bool(rename_from) != bool(rename_to):
            raise ValueError("Rename requires both source and target")
        if rename_from and rename_from not in updates:
            if rename_to in updates or inside(root, rename_to).exists():
                raise TaskConflict("Rename target already exists")
            updates[rename_from] = None
            updates[rename_to] = inside(root, rename_from).read_bytes()
        rename_from = rename_to = None

    while index < len(lines):
        line = lines[index]
        if line.startswith(("diff --git ", "index ", "similarity index ", "rename from ", "rename to ", "new file mode 100644", "deleted file mode 100644")):
            if line.startswith("diff --git "):
                finish_rename()
                targets = line.strip().split(" ")[2:]
                if len(targets) != 2:
                    raise ValueError("Quoted / whitespace diff paths are unsupported")
                for target in targets:
                    path_name(target[2:] if target.startswith(("a/", "b/")) else target)
            if line.startswith(("rename from ", "rename to ")):
                name = path_name(line.split(" ", 2)[2].strip())
                if line.startswith("rename from "):
                    rename_from = name
                else:
                    rename_to = name
            index += 1
            continue
        if not line.startswith("--- ") or index + 1 >= len(lines) or not lines[index + 1].startswith("+++ "):
            raise ValueError("Only regular text unified diffs are supported")
        old = line[4:].rstrip("\r\n").split("\t")[0]
        new = lines[index + 1][4:].rstrip("\r\n").split("\t")[0]
        old_name = None if old == "/dev/null" else path_name(old[2:] if old.startswith("a/") else old)
        new_name = None if new == "/dev/null" else path_name(new[2:] if new.startswith("b/") else new)
        if not old_name and not new_name:
            raise ValueError("Patch lacks a source target")
        if rename_from and (old_name != rename_from or new_name != rename_to):
            raise ValueError("Rename metadata and target headers differ")
        if old_name in updates or (new_name != old_name and new_name in updates):
            raise ValueError("Repeated target in diff")
        original = inside(root, old_name).read_text(encoding="utf-8") if old_name else ""
        if new_name and new_name != old_name and inside(root, new_name).exists():
            raise TaskConflict("New target already exists")
        index += 2
        hunks: list[str] = []
        while index < len(lines) and lines[index].startswith("@@ "):
            header = re.fullmatch(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@.*\n?", lines[index])
            if not header:
                raise ValueError("Invalid diff header")
            remaining_old, remaining_new = int(header[2] or 1), int(header[4] or 1)
            hunks.append(lines[index]); index += 1
            while remaining_old or remaining_new:
                if index >= len(lines) or not lines[index] or lines[index][0] not in " +-":
                    raise ValueError("Incomplete diff hunk")
                body = lines[index]
                if body[0] in " -":
                    remaining_old -= 1
                if body[0] in " +":
                    remaining_new -= 1
                if min(remaining_old, remaining_new) < 0:
                    raise ValueError("Invalid diff hunk counts")
                hunks.append(body); index += 1
                if index < len(lines) and lines[index].startswith("\\ No newline at end of file"):
                    hunks.append(lines[index]); index += 1
        result = unified_change(original, hunks)
        if new_name is None and result:
            raise ValueError("Delete diff must remove the full file")
        if old_name and old_name != new_name:
            updates[old_name] = None
        if new_name:
            updates[new_name] = result.encode("utf-8")
        rename_from = rename_to = None
    finish_rename()
    if not updates:
        raise ValueError("Diff did not change any files")
    for name, value in updates.items():
        target = inside(root, name)
        if value is None:
            target.unlink()
        else:
            atomic_write(target, value)
    return list(updates)


class DeveloperTasks:
    def __init__(self, source: Path, storage: Path, session_factory: Callable | None = None,
                 *, config: dict | None = None, check_runner: Callable | None = None):
        self.source = source.resolve()
        self.storage = storage.resolve()
        self.session_factory = session_factory or db.get_session
        self.config = config or {}
        self.check_runner = check_runner
        self.lock = asyncio.Lock()
        self.workers: dict[str, asyncio.Task] = {}
        self.model_slot = asyncio.Semaphore(1)
        self._secrets: set[str] = set()
        self._collect_secrets(self.config)

    @property
    def enabled(self) -> bool:
        settings = (self.config.get("agent") or {}).get("dev") or {}
        route = ((self.config.get("agent") or {}).get("model_routes") or {}).get("development") or {}
        from .model_routes import _OVERRIDES
        effective_enabled = _OVERRIDES.get("development", {}).get("enabled", route.get("enabled", True))
        return bool(settings.get("enabled", True)) and bool(effective_enabled)

    def require_enabled(self) -> None:
        if not self.enabled:
            raise PermissionError("Development tasks are disabled")

    def _collect_secrets(self, values: Any, name: str = "") -> None:
        if isinstance(values, dict):
            for key, item in values.items():
                self._collect_secrets(item, str(key))
        elif isinstance(values, list):
            for item in values:
                self._collect_secrets(item, name)
        elif isinstance(values, str) and len(values) >= 6 and any(word in name.lower() for word in ("key", "token", "password", "secret")):
            self._secrets.add(values)

    def sanitize(self, text: str) -> str:
        for value in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(value, "[REDACTED]")
        return re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._-]{10,}", r"\1[REDACTED]", text)

    def directory(self, task_id: str) -> Path:
        if not TASK_ID.fullmatch(task_id):
            raise ValueError("Invalid task ID")
        return self.storage / task_id

    def inventory(self, root: Path, include_private: bool = False) -> dict[str, str]:
        result: dict[str, str] = {}
        total = 0
        for current, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if d.lower() not in DENY_PARTS and not (Path(current) / d).is_symlink())
            for name in sorted(files):
                file = Path(current) / name
                try:
                    relative = path_name(file.relative_to(root).as_posix())
                    if file.is_symlink() or file.stat().st_size > 1_000_000:
                        continue
                    content = file.read_bytes()
                    text = content.decode("utf-8")
                    if not include_private and (self.sanitize(text) != text or any(not any(marker in match[1].lower() for marker in ("example", "placeholder", "test", "dummy", "invalid")) for match in SECRET_ASSIGNMENT.finditer(text))):
                        continue
                except (ValueError, PermissionError, UnicodeError):
                    continue
                total += len(content)
                if len(result) >= 3000 or total > 32 * 1024 * 1024:
                    raise ValueError("Source snapshot exceeds 32 MiB / 3000 files")
                result[relative] = digest(content)
        return result

    def _manifest(self, task_id: str) -> dict:
        return json.loads((self.directory(task_id) / "manifest.json").read_text(encoding="utf-8"))

    async def _load(self, task_id: str) -> dict:
        self.directory(task_id)
        async with await self.session_factory() as session:
            row = await session.get(DeveloperTask, task_id)
            if row is None:
                raise LookupError("Development task not found")
            return {**json.loads(row.payload_json), "id": row.id, "actor": row.actor, "status": row.status, "version": row.version}

    async def _save(self, item: dict, action: str, actor: str | None = None) -> dict:
        from .console_state import ConsoleAudit
        async with await self.session_factory() as session:
            row = (await session.execute(select(DeveloperTask).where(DeveloperTask.id == item["id"]).with_for_update())).scalar_one_or_none()
            if row is None:
                row = DeveloperTask(id=item["id"], actor=item["actor"], status=item["status"], version=1, payload_json="{}")
                session.add(row)
            else:
                if row.version != item["version"]:
                    raise TaskConflict("Task changed; refresh before continuing")
                row.version += 1
            item["version"] = row.version
            row.status = item["status"]
            item["updated_at"] = time.time()
            row.payload_json = json.dumps({k: v for k, v in item.items() if k not in {"id", "actor", "status", "version"}}, ensure_ascii=True)
            session.add(ConsoleAudit(action="developer_" + action, target=item["id"], actor=actor or item["actor"], detail_json=json.dumps({"status": item["status"], "version": row.version})))
            await session.commit()
        return item

    async def create(self, request: str, actor: str) -> dict:
        self.require_enabled()
        if not re.fullmatch(r"(?:qq:\d+|console:[^\r\n]{1,80})", actor):
            raise PermissionError("A host-authenticated administrator is required")
        if not request.strip() or len(request) > 8000:
            raise ValueError("Request must contain 1–8000 characters")
        async with self.lock:
            task_id = secrets.token_hex(12)
            task_root = self.directory(task_id)
            task_root.mkdir(parents=True, mode=0o700)
            os.chmod(task_root, 0o700)
            baseline = await asyncio.to_thread(self.inventory, self.source)
            source_manifest = await asyncio.to_thread(self.inventory, self.source, True)
            for name in baseline:
                content = inside(self.source, name).read_bytes()
                if digest(content) != baseline[name]:
                    raise TaskConflict("Source changed while creating snapshot")
                atomic_write(inside(task_root / "baseline", name), content)
                atomic_write(inside(task_root / "workspace", name), content)
            atomic_write(task_root / "manifest.json", json.dumps(baseline, sort_keys=True).encode())
            atomic_write(task_root / "source-manifest.json", json.dumps(source_manifest, sort_keys=True).encode())
            now = time.time()
            return await self._save({"id": task_id, "actor": actor, "version": 1, "status": "queued", "request": self.sanitize(request.strip()), "created_at": now, "iterations": 0, "elapsed_seconds": 0, "calls": [], "checks": [], "diff": "", "summary": "", "error": "", "messages": []}, "created")

    def _public(self, item: dict, detail: bool = True) -> dict:
        hidden = {"messages", "journal", "applied_manifest"}
        if not detail:
            hidden |= {"diff", "checks", "calls"}
        return {key: value for key, value in item.items() if key not in hidden}

    async def get(self, task_id: str) -> dict:
        return self._public(await self._load(task_id))

    async def list(self, limit: int = 50) -> dict:
        async with await self.session_factory() as session:
            rows = (await session.execute(select(DeveloperTask))).scalars().all()
        items = [self._public({**json.loads(row.payload_json), "id": row.id, "actor": row.actor, "status": row.status, "version": row.version}, False) for row in rows]
        items.sort(key=lambda row: row["created_at"], reverse=True)
        return {"items": items[:max(1, min(100, limit))], "total": len(items)}

    def _owner(self, item: dict, actor: str, version: int) -> None:
        if self.owner_key(item["actor"]) != self.owner_key(actor):
            raise PermissionError("Only the administrator who created this task can decide it")
        if not isinstance(version, int) or isinstance(version, bool) or version != item["version"]:
            raise TaskConflict("Task changed; refresh before continuing")

    def owner_key(self, actor: str) -> str:
        """Bind the one authenticated console account to a configured QQ admin.

        With several QQ administrators the association must be explicit; an
        arbitrary username/request body can never select an approval identity.
        """
        if not actor.startswith("console:"):
            return actor
        settings = (self.config.get("agent") or {}).get("dev") or {}
        values = [int(value) for value in settings.get("admin_users", []) if str(value).isdigit()]
        selected = settings.get("console_admin_user")
        if selected is None and len(values) == 1:
            selected = values[0]
        if selected is not None and str(selected).isdigit() and int(selected) in values:
            configured_username = str(settings.get("console_username") or os.environ.get("AGENT_CONSOLE_USERNAME") or "admin")
            if actor == "console:" + configured_username:
                return "qq:" + str(int(selected))
        return actor

    def changes(self, task_id: str) -> tuple[str, dict[str, str]]:
        root = self.directory(task_id)
        old = self._manifest(task_id)
        new = self.inventory(root / "workspace")
        parts: list[str] = []
        for name in sorted(set(old) | set(new)):
            if old.get(name) == new.get(name):
                continue
            a = inside(root / "baseline", name).read_text(encoding="utf-8").splitlines(keepends=True) if name in old else []
            b = inside(root / "workspace", name).read_text(encoding="utf-8").splitlines(keepends=True) if name in new else []
            diff = difflib.unified_diff(a, b, fromfile="a/" + name if name in old else "/dev/null", tofile="b/" + name if name in new else "/dev/null")
            for line in diff:
                parts.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
        return "".join(parts), new

    @contextmanager
    def context(self, task_id: str, actor: str):
        token = _CONTEXT.set((self, task_id, actor))
        try:
            yield
        finally:
            _CONTEXT.reset(token)

    async def tool(self, task_id: str, name: str, args: dict) -> Any:
        self.require_enabled()
        root = self.directory(task_id) / "workspace"
        item = await self._load(task_id)
        if item["status"] not in ACTIVE:
            raise TaskConflict("This task no longer accepts tools")
        if name == "list_project_files":
            prefix = str(args.get("path") or ".").rstrip("/")
            if prefix not in {"", "."}:
                if ".." in PurePosixPath(prefix).parts or prefix.startswith(("/", "\\")):
                    raise PermissionError("Invalid directory")
                prefix += "/"
            else:
                prefix = ""
            names = [n for n in self.inventory(root) if n.startswith(prefix)]
            maximum = max(1, min(500, int(args.get("max_entries") or 250)))
            return {"entries": names[:maximum], "truncated": len(names) > maximum}
        if name == "read_project_file":
            lines = inside(root, str(args.get("path") or "")).read_text(encoding="utf-8").splitlines()
            start = max(0, int(args.get("start_line") or 1) - 1)
            return "\n".join(f"{i + 1}: {line}" for i, line in enumerate(lines) if start <= i < start + min(500, int(args.get("max_lines") or 240)))[:50_000]
        if name == "search_project":
            query = str(args.get("query") or "")
            if not query or len(query) > 200:
                raise ValueError("Search query must contain 1–200 characters")
            results = []
            prefix = str(args.get("path") or ".").strip("/")
            if prefix not in {"", "."} and (".." in PurePosixPath(prefix).parts or "\\" in prefix or ":" in prefix):
                raise PermissionError("Invalid search directory")
            for filename in self.inventory(root):
                if prefix not in {"", "."} and not filename.startswith(prefix + "/"):
                    continue
                if not fnmatch.fnmatch(filename, str(args.get("file_glob") or "*")):
                    continue
                for number, line in enumerate(inside(root, filename).read_text(encoding="utf-8").splitlines(), 1):
                    if query.casefold() in line.casefold():
                        results.append({"path": filename, "line": number, "text": line[:300]})
                    if len(results) >= min(100, int(args.get("max_results") or 50)):
                        return results
            return results
        if name in {"git_status", "git_readonly"}:
            return self.changes(task_id)[0] or "Snapshot is unchanged."
        if name == "run_project_check":
            result = await self.check(task_id, str(args.get("check") or ""), args.get("targets") or [])
            item = await self._load(task_id)
            item["checks"].append(result)
            await self._save(item, "checked")
            return result
        if name == "propose_patch":
            patch = str(args.get("patch") or "")
            if self.sanitize(patch) != patch:
                raise PermissionError("Patch contains a configured secret")
            requested = args.get("checks") or ["py_compile"]
            if not isinstance(requested, list) or any(c not in {"git_diff_check", "py_compile", "pytest"} for c in requested):
                raise ValueError("Unknown check")
            with tempfile.TemporaryDirectory(prefix="proposal-", dir=self.directory(task_id)) as temporary:
                staging = Path(temporary) / "workspace"
                await asyncio.to_thread(shutil.copytree, root, staging)
                touched = await asyncio.to_thread(apply_unified_patch, staging, patch)
                if self.inventory(staging) != self.inventory(staging, True):
                    raise PermissionError("Patch introduced secret-like source content")
                for name in touched:
                    target, updated = inside(root, name), inside(staging, name)
                    if updated.exists():
                        atomic_write(target, updated.read_bytes())
                    else:
                        target.unlink()
            diff, manifest = self.changes(task_id)
            if len(diff.encode()) > 400_000:
                raise ValueError("Combined diff exceeds 400 KB")
            item["diff"], item["summary"] = diff, self.sanitize(str(args.get("summary") or "Development patch"))[:500]
            item["proposal_manifest"] = manifest
            item["checks"] = []
            # Syntax and whitespace are mandatory; tests can be requested in addition.
            item["required_checks"] = list(dict.fromkeys(["git_diff_check", "py_compile", *requested]))
            await self._save(item, "proposed")
            return {"status": "proposed", "task_id": task_id, "summary": item["summary"], "checks": item["required_checks"]}
        raise PermissionError("Tool is not a development snapshot tool")

    async def _process(self, argv: list[str], timeout: float = 300) -> tuple[int, str]:
        process = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        chunks: list[bytes] = []
        async def collect():
            used = 0
            while chunk := await process.stdout.read(8192):
                used += len(chunk)
                if used > 128_000:
                    process.kill()
                    raise ValueError("Check output exceeded 128 KB")
                chunks.append(chunk)
            return await process.wait()
        try:
            code = await asyncio.wait_for(collect(), timeout)
            return code, self.sanitize(b"".join(chunks).decode("utf-8", "replace"))
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def check(self, task_id: str, name: str, targets: list[str] | None = None) -> dict:
        if name not in {"git_diff_check", "py_compile", "pytest"}:
            raise PermissionError("Unknown development check")
        targets = targets or []
        root = self.directory(task_id) / "workspace"
        for target in targets:
            candidate = root / str(target)
            if candidate.is_dir():
                normalized = PurePosixPath(str(target))
                if "\\" in str(target) or ":" in str(target) or normalized.is_absolute() or ".." in normalized.parts or not normalized.parts or normalized.parts[0] not in SOURCE_DIRS or any(part.lower() in DENY_PARTS for part in normalized.parts) or candidate.is_symlink():
                    raise PermissionError("Invalid check directory")
                candidate.resolve().relative_to(root.resolve())
            else:
                inside(root, target)
        if name == "git_diff_check":
            diff, _ = self.changes(task_id)
            invalid = [line for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++") and line.rstrip() != line]
            return {"check": name, "ok": not invalid, "exit_code": int(bool(invalid)), "output": "Trailing whitespace found" if invalid else "Diff whitespace valid", "snapshot": digest(diff.encode())}
        if self.check_runner:
            result = await self.check_runner(root, name, targets)
            return {**result, "check": name, "snapshot": digest(self.changes(task_id)[0].encode())}
        image = str(((self.config.get("agent") or {}).get("dev") or {}).get("check_image") or "rinbot-dev-checks:local")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]{0,200}", image):
            raise ValueError("Invalid check image")
        command = ["python", "-m", "compileall", "-q", *(targets or ["plugins"])] if name == "py_compile" else ["python", "-m", "pytest", *(targets or ["tests"]), "-q", "-o", "cache_dir=/tmp/pytest-cache"]
        container = "rinbot-check-" + secrets.token_hex(10)
        staging = container + "-stage"
        staged_image = "rinbot-check-snapshot:" + secrets.token_hex(10)
        try:
            # docker cp cannot write to a read-only rootfs. Populate a stopped
            # staging container and commit it; generated code is never started
            # until it is in the final, restricted read-only container.
            code, output = await self._process(["docker", "create", "--name", staging, "--pull=never", "--network=none", image, "python", "--version"], 30)
            if code:
                return {"check": name, "ok": False, "exit_code": code, "output": "Check image unavailable; build tools/Dockerfile.developer-checks first.", "snapshot": digest(self.changes(task_id)[0].encode())}
            code, output = await self._process(["docker", "cp", str(root) + os.sep + ".", staging + ":/workspace"], 30)
            if not code:
                code, output = await self._process(["docker", "commit", staging, staged_image], 30)
            if not code:
                code, output = await self._process(["docker", "create", "--name", container, "--pull=never", "--network=none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit=64", "--memory=512m", "--memory-swap=512m", "--cpus=1", "--user=65534:65534", "--tmpfs=/tmp:rw,noexec,nosuid,size=128m", "--workdir=/workspace", "--env=PYTHONPYCACHEPREFIX=/tmp/pycache", "--env=HOME=/tmp", "--env=PYTHONDONTWRITEBYTECODE=1", staged_image, *command], 30)
            if not code:
                code, output = await self._process(["docker", "start", "--attach", container], 300)
            return {"check": name, "ok": code == 0, "exit_code": code, "output": output[-20_000:], "snapshot": digest(self.changes(task_id)[0].encode())}
        except (OSError, asyncio.TimeoutError, ValueError) as exc:
            return {"check": name, "ok": False, "exit_code": -1, "output": f"Isolated check unavailable ({type(exc).__name__}); no host fallback.", "snapshot": digest(self.changes(task_id)[0].encode())}
        finally:
            try:
                await asyncio.shield(self._process(["docker", "rm", "--force", container, staging], 15))
                await asyncio.shield(self._process(["docker", "image", "rm", "--force", staged_image], 15))
            except (OSError, asyncio.TimeoutError):
                pass

    def start(self, task_id: str, complete: Callable | None = None) -> None:
        self.require_enabled()
        if task_id not in self.workers or self.workers[task_id].done():
            self.workers[task_id] = asyncio.create_task(self.run(task_id, complete))

    async def run(self, task_id: str, complete: Callable | None = None) -> None:
        if not self.enabled:
            return
        started = time.monotonic()
        item = await self._load(task_id)
        if item["status"] not in ACTIVE:
            return
        used = float(item.get("elapsed_seconds") or 0)
        if complete is None:
            from .model_routes import complete as routed_complete
            async def complete(messages):
                return await routed_complete("development", messages)
        from .agent_tools import tool_schemas
        messages = item.get("messages") or [{"role": "system", "content": "You are the administrator's development assistant. Work only in the provided isolated source snapshot. Never claim a patch was deployed. Return JSON with keys reply and tool_calls [{name, arguments}]. Available tools: " + json.dumps(tool_schemas("dev"), ensure_ascii=False)}, {"role": "user", "content": item["request"]}]
        try:
            async with asyncio.timeout(max(0.01, 900 - used)):
                item["status"] = "running"
                await self._save(item, "started")
                with self.context(task_id, item["actor"]):
                    while item["iterations"] < 8:
                        self.require_enabled()
                        stamp = time.monotonic()
                        item["iterations"] += 1
                        item["calls"].append({"iteration": item["iterations"], "seconds": 0, "status": "running"})
                        item["call_started_at"] = time.time()
                        item["elapsed_seconds"] = used + stamp - started
                        await self._save(item, "model_started")
                        async with self.model_slot:
                            raw = await asyncio.wait_for(complete(messages), 180)
                        if not isinstance(raw, str):
                            raw = getattr(raw, "content", None) or str(raw)
                        raw = self.sanitize(raw)[:100_000]
                        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
                        try:
                            decision = json.loads(content)
                        except ValueError:
                            decision = {"reply": raw, "tool_calls": []}
                        if not isinstance(decision, dict):
                            raise ValueError("Model decision must be an object")
                        item = await self._load(task_id)
                        if item["status"] not in ACTIVE:
                            return
                        item["calls"][-1].update(seconds=round(time.monotonic() - stamp, 2), status="success")
                        item.pop("call_started_at", None)
                        try:
                            from .model_routes import resolve
                            route = resolve("development")
                            item["calls"][-1].update(model=getattr(route, "model", None), provider=getattr(route, "provider", None))
                        except (ImportError, AttributeError, TypeError):
                            pass
                        messages.append({"role": "assistant", "content": raw})
                        calls = decision.get("tool_calls") or []
                        if not isinstance(calls, list) or len(calls) > 4 or any(not isinstance(call, dict) for call in calls):
                            raise ValueError("At most four tools are allowed per iteration")
                        for call in calls:
                            try:
                                result = await self.tool(task_id, str(call.get("name") or ""), call.get("arguments") or call.get("args") or {})
                            except (ValueError, PermissionError, FileNotFoundError) as exc:
                                result = {"error": str(exc)}
                            messages.append({"role": "user", "content": "Tool result: " + self.sanitize(json.dumps(result, ensure_ascii=False))[:30_000]})
                        latest = await self._load(task_id)
                        if latest["status"] not in ACTIVE:
                            return
                        latest.update(iterations=item["iterations"], calls=item["calls"], messages=[messages[0], *messages[-29:]] if len(messages) > 30 else messages, elapsed_seconds=used + time.monotonic() - started)
                        latest.pop("call_started_at", None)
                        latest["summary"] = latest.get("summary") or str(decision.get("reply") or "")[:2000]
                        item = await self._save(latest, "checkpoint")
                        if not calls:
                            break
                if item.get("diff"):
                    item["status"] = "checking"
                    await self._save(item, "checking")
                    item["checks"] = []
                    for name in item.get("required_checks") or ["git_diff_check", "py_compile"]:
                        item["checks"].append(await self.check(task_id, name))
                    if all(result.get("ok") for result in item["checks"]):
                        item["status"] = "pending_approval"
                        item["expires_at"] = time.time() + 86400
                        item["diff_hash"] = digest(item["diff"].encode())
                    else:
                        item["status"] = "checks_failed"
                else:
                    item["status"] = "completed"
                item["elapsed_seconds"] = used + time.monotonic() - started
                await self._save(item, "ready")
        except asyncio.CancelledError:
            return
        except Exception as exc:
            item = await self._load(task_id)
            if item["status"] in ACTIVE:
                item["status"] = "failed"
                item["error"] = "Task timed out" if isinstance(exc, TimeoutError) else f"Task failed ({type(exc).__name__})"
                item["elapsed_seconds"] = used + time.monotonic() - started
                await self._save(item, "failed")

    async def decide(self, task_id: str, actor: str, version: int, action: str) -> dict:
        if action not in {"approve", "reject", "cancel", "rollback"}:
            raise ValueError("Unknown decision")
        async with self.lock:
            item = await self._load(task_id)
            self._owner(item, actor, version)
            if action in {"cancel", "reject"}:
                if item["status"] in TERMINAL:
                    raise TaskConflict("Task is already finished")
                if worker := self.workers.get(task_id):
                    worker.cancel()
                item["status"] = "cancelled" if action == "cancel" else "rejected"
                return self._public(await self._save(item, action))
            if action == "approve":
                self.require_enabled()
                if item["status"] != "pending_approval":
                    raise TaskConflict("Only a successfully checked task can be approved")
                if item.get("expires_at", 0) < time.time():
                    item["status"] = "expired"
                    await self._save(item, "expired")
                    raise TaskConflict("Approval expired; create a new review")
                current_diff, proposed = self.changes(task_id)
                if current_diff != item["diff"] or digest(current_diff.encode()) != item["diff_hash"] or proposed != item.get("proposal_manifest"):
                    raise TaskConflict("Snapshot changed after checks")
                if not item["checks"] or any(not row.get("ok") or row.get("snapshot") != item["diff_hash"] for row in item["checks"]):
                    raise TaskConflict("Checks do not match the reviewed diff")
                baseline = self._manifest(task_id)
                before = json.loads((self.directory(task_id) / "source-manifest.json").read_text(encoding="utf-8"))
                after = {name: value for name, value in before.items() if name not in baseline}
                if set(after).intersection(proposed):
                    raise PermissionError("Patch targets a file excluded from the private snapshot")
                after.update(proposed)
                content_root = self.directory(task_id) / "workspace"
            else:
                if item["status"] != "applied":
                    raise TaskConflict("Only an applied task can be rolled back")
                before = item["applied_manifest"]
                after = json.loads((self.directory(task_id) / "source-manifest.json").read_text(encoding="utf-8"))
                content_root = self.directory(task_id) / "baseline"
            if await asyncio.to_thread(self.inventory, self.source, True) != before:
                raise TaskConflict("Production source tree changed since review; no files were modified")
            touched = [name for name in sorted(set(before) | set(after)) if before.get(name) != after.get(name)]
            if any(name not in before and inside(self.source, name).exists() for name in touched):
                raise TaskConflict("A proposed new file already exists in production")
            # Record a durable recovery journal before the first filesystem change.
            item["journal"] = {"action": action, "before": before, "after": after, "touched": touched}
            item["status"] = "applying" if action == "approve" else "rolling_back"
            await self._save(item, "apply_started")
            backup = self.directory(task_id) / ("rollback" if action == "approve" else "rollback_undo")
            written: list[str] = []
            try:
                for name in touched:
                    target = inside(self.source, name)
                    current_hash = digest(target.read_bytes()) if target.is_file() else None
                    if current_hash != before.get(name):
                        raise TaskConflict("A source file changed during application")
                    if target.exists():
                        atomic_write(inside(backup, name), target.read_bytes())
                    if name in after:
                        atomic_write(target, inside(content_root, name).read_bytes())
                    else:
                        target.unlink()
                    written.append(name)
            except Exception:
                for name in written:
                    target = inside(self.source, name)
                    saved = inside(backup, name)
                    if name in before and saved.exists():
                        atomic_write(target, saved.read_bytes())
                    elif name not in before:
                        target.unlink(missing_ok=True)
                item["status"] = "failed"
                item["error"] = "Apply failed; original source restored"
                await self._save(item, "apply_failed")
                raise TaskConflict(item["error"]) from None
            item["status"] = "applied" if action == "approve" else "rolled_back"
            item["applied_manifest"] = after
            item.pop("journal", None)
            return self._public(await self._save(item, action))

    async def recover(self, complete: Callable | None = None) -> None:
        async with await self.session_factory() as session:
            ids = list((await session.execute(select(DeveloperTask.id).where(DeveloperTask.status.in_(ACTIVE | {"applying", "rolling_back"})))).scalars())
        for task_id in ids:
            item = await self._load(task_id)
            journal = item.get("journal")
            if journal:
                # After a crash, restore only files still matching either our before
                # or after hashes. Never overwrite a third party's later change.
                before, after = journal["before"], journal["after"]
                current = self.inventory(self.source, True)
                if any(current.get(name) not in {before.get(name), after.get(name)} for name in journal["touched"]):
                    item["status"], item["error"] = "failed", "Interrupted apply needs manual review: source changed"
                else:
                    backup = self.directory(task_id) / ("rollback" if journal["action"] == "approve" else "rollback_undo")
                    for name in journal["touched"]:
                        target = inside(self.source, name)
                        if name in before and inside(backup, name).exists():
                            atomic_write(target, inside(backup, name).read_bytes())
                        elif name not in before:
                            target.unlink(missing_ok=True)
                    item["status"], item["error"] = "failed", "Interrupted apply restored; a new review is required"
                item.pop("journal", None)
                await self._save(item, "recovered")
            elif item["status"] in ACTIVE:
                if item.get("call_started_at"):
                    elapsed = max(0, time.time() - item.pop("call_started_at"))
                    item["elapsed_seconds"] += elapsed
                    if item.get("calls"):
                        item["calls"][-1].update(status="interrupted", seconds=round(elapsed, 2))
                item["status"] = "queued"
                await self._save(item, "recovered")
                if self.enabled:
                    self.start(task_id, complete)


def get_developer_service() -> DeveloperTasks:
    global _SERVICE
    if _SERVICE is None:
        import yaml
        source = Path(__file__).resolve().parents[1]
        config_path = source / "config.yaml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
        settings = ((config or {}).get("agent") or {}).get("dev") or {}
        _SERVICE = DeveloperTasks(Path(settings.get("workspace") or source), Path(settings.get("task_storage") or source / "data" / "developer-tasks"), config=config or {})
    return _SERVICE


async def execute_developer_tool(name: str, args: dict, ctx: dict) -> Any:
    active = _CONTEXT.get()
    if active is None or not ctx.get("is_admin") or ctx.get("group_id") or ctx.get("scope", "dev") != "dev":
        raise PermissionError("Development tools require an administrator-owned isolated task")
    service, task_id, actor = active
    if actor.startswith("qq:") and actor != f"qq:{int(ctx.get('user_id') or 0)}":
        raise PermissionError("Development task owner mismatch")
    if actor.startswith("console:") and ctx.get("actor") != actor:
        raise PermissionError("Development task owner mismatch")
    return await service.tool(task_id, name, args)


async def create_developer_request(user_id: int, request: str) -> list[str]:
    service = get_developer_service()
    from .agent_policy import AgentPolicy
    if not AgentPolicy.from_config(service.config).user_is_admin(int(user_id)):
        return ["开发辅助只对管理员开放。"]
    item = await service.create(request, f"qq:{int(user_id)}")
    service.start(item["id"])
    return [f"开发任务已创建：{item['id']}\n请在控制台的开发任务中查看进度、完整差异和检查结果。通过后使用 #agent approve {item['id']} 批准；或 #agent reject {item['id']} 拒绝。"]


async def approve_developer_task(task_id: str, user_id: int) -> str:
    return await _qq_decision(task_id, user_id, "approve")


async def reject_developer_task(task_id: str, user_id: int) -> str:
    return await _qq_decision(task_id, user_id, "reject")


async def _qq_decision(task_id: str, user_id: int, action: str) -> str:
    service = get_developer_service()
    from .agent_policy import AgentPolicy
    if not AgentPolicy.from_config(service.config).user_is_admin(int(user_id)):
        return "开发辅助只对管理员开放。"
    try:
        item = await service.get(task_id)
        result = await service.decide(task_id, f"qq:{int(user_id)}", item["version"], action)
        return "补丁已应用，回滚副本已保留；服务尚未重启。" if result["status"] == "applied" else "开发任务已拒绝。"
    except (ValueError, PermissionError, LookupError) as exc:
        return str(exc)
