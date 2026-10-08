"""Bounded synchronous adapter for the trusted local Rapfi 250615 executable.

Call through ``asyncio.to_thread``; this module never downloads an executable.
Protocol source: https://github.com/dhbloo/rapfi/blob/250615/Rapfi/command/gomocup.cpp
No Rapfi source code or model weights are embedded here.
"""
from collections import deque
from contextlib import contextmanager
from pathlib import Path
import os
import queue
import re
import subprocess
import threading
import time
import tomllib

from .ai import SearchCancelled
from .rules import outcome, validate_board


class RapfiUnavailable(RuntimeError):
    """No move was made; retain the game and tell the user why."""


_GLOBAL_SEARCH_LOCK = threading.Lock()
_COORD = re.compile(r"^(-?\d{1,3})\s*,\s*(-?\d{1,3})$")
_MAX_LINE = 4096
_MAX_OUTPUT = 256


def _seconds(config, key, default, minimum, maximum):
    try:
        value = float(config.get(key, default))
    except (TypeError, ValueError) as exc:
        raise RapfiUnavailable(f"Rapfi {key} 配置无效") from exc
    if not minimum <= value <= maximum:
        raise RapfiUnavailable(f"Rapfi {key} 必须在 {minimum}～{maximum} 秒内")
    return value


class RapfiEngine:
    """One lazily launched engine, globally serialised with cancellation.

    The operator must install a compatible pinned engine with config.toml and
    all referenced weights. Paths are trusted server configuration, never chat
    input. 64 MiB limits the hash table, not the process's total memory.
    """

    def __init__(self, config=None, root=None):
        self.config = dict(config or {})
        self.root = Path(root or Path.cwd()).resolve()
        self.enabled = self.config.get("enabled", False) is True
        default_name = "rapfi.exe" if os.name == "nt" else "rapfi"
        self.executable = self._path(self.config.get("executable",
            f"data/engines/rapfi/{default_name}"))
        self.config_path = self._path(self.config.get("config_path",
            "data/engines/rapfi/config.toml"))
        self.move_seconds = _seconds(self.config, "move_seconds", 2, .01, 10)
        self.startup_timeout = _seconds(self.config, "startup_timeout", 15, .05, 60)
        self.search_timeout = _seconds(self.config, "search_timeout", 5, .05, 30)
        self.idle_seconds = _seconds(self.config, "idle_seconds", 60, .05, 3600)
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._proc = None
        self._lines = None
        self._fault = None
        self._threads = []
        self._stderr = deque(maxlen=16)
        self._idle_timer = None
        self._generation = 0
        self.engine_info = ""

    def _path(self, value):
        if not isinstance(value, (str, Path)) or not str(value).strip():
            raise RapfiUnavailable("Rapfi 文件路径配置无效")
        path = Path(value)
        return (path if path.is_absolute() else self.root / path).resolve()

    def _check(self, cancel_event):
        if cancel_event is not None and cancel_event.is_set():
            raise SearchCancelled()
        if self._closed.is_set():
            raise RapfiUnavailable("Rapfi 引擎已关闭")

    @contextmanager
    def _exclusive(self, cancel_event):
        # A queued cancelled board never launches a process or starts a search.
        while True:
            self._check(cancel_event)
            if _GLOBAL_SEARCH_LOCK.acquire(timeout=.05):
                break
        own = False
        try:
            while not own:
                self._check(cancel_event)
                own = self._lock.acquire(timeout=.05)
            self._check(cancel_event)
            self._cancel_idle()
            yield
        finally:
            if own:
                self._lock.release()
            _GLOBAL_SEARCH_LOCK.release()

    def _preflight(self):
        if not self.enabled:
            raise RapfiUnavailable("五子棋认真档尚未启用，可先选择娱乐")
        if not self.executable.is_file():
            raise RapfiUnavailable("未安装 Rapfi 引擎，可先选择娱乐")
        if os.name != "nt" and not os.access(self.executable, os.X_OK):
            raise RapfiUnavailable("Rapfi 文件不可执行，请管理员检查权限")
        if self.config_path.name != "config.toml" or not self.config_path.is_file():
            raise RapfiUnavailable("缺少 Rapfi config.toml，请管理员检查安装")
        try:
            if self.config_path.stat().st_size > 1024 * 1024:
                raise ValueError("oversized configuration")
            with self.config_path.open("rb") as handle:
                parsed = tomllib.load(handle)
            general = parsed.get("general", {})
            if (general.get("coord_conversion_mode") != "none"
                    or general.get("default_thread_num") != 1
                    or general.get("reload_config_each_move", False) is not False
                    or parsed.get("database", {}).get("enable_by_default", False) is not False):
                raise RapfiUnavailable("Rapfi 配置须使用 none 坐标、1 线程，关闭逐步重载和自动数据库")
            initial_hash = general.get("default_tt_size_kb", 32768)
            if type(initial_hash) is not int or not 1 <= initial_hash <= 65536:
                raise RapfiUnavailable("Rapfi 启动哈希表 default_tt_size_kb 必须不超过 65536 KiB")
            model = parsed.get("model", {})
            evaluator = model.get("evaluator", {})
            weights = evaluator.get("weights", [])
            if not model.get("binary_file") or not evaluator.get("type") or not weights:
                raise RapfiUnavailable("Rapfi 缺少完整模型配置，认真档不会降级为传统评估器")
            assets = [model["binary_file"]]
            for entry in weights:
                for key in ("weight_file", "weight_file_black", "weight_file_white"):
                    if entry.get(key):
                        assets.append(entry[key])
            if len(assets) < 2:
                raise RapfiUnavailable("Rapfi 缺少神经网络权重配置")
            for asset in assets:
                path = Path(asset)
                candidates = [path] if path.is_absolute() else [
                    self.config_path.parent / path, self.executable.parent / path]
                if not any(p.is_file() and p.stat().st_size > 0 for p in candidates):
                    raise RapfiUnavailable(f"Rapfi 缺少模型文件：{path.name}")
        except RapfiUnavailable:
            raise
        except (OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
            raise RapfiUnavailable("Rapfi config.toml 读取失败，请检查同版本配置和权重") from exc

    @staticmethod
    def _read_stdout(pipe, lines, fault):
        try:
            while True:
                raw = pipe.readline(_MAX_LINE + 1)
                if not raw:
                    break
                if len(raw) > _MAX_LINE:
                    fault.set()
                    return
                line = raw.decode("utf-8", errors="replace").strip()
                if line:
                    try:
                        lines.put_nowait(line)
                    except queue.Full:
                        fault.set()
                        return
        except (OSError, ValueError):
            pass

    @staticmethod
    def _read_stderr(pipe, recent):
        try:
            while True:
                raw = pipe.readline(_MAX_LINE)
                if not raw:
                    return
                recent.append(raw.decode("utf-8", errors="replace")[:512])
        except (OSError, ValueError):
            pass

    def _write(self, text):
        proc = self._proc
        if proc is None or proc.poll() is not None:
            raise RapfiUnavailable("Rapfi 进程异常退出，棋局已保留")
        data = text.encode("ascii")
        # Only fixed commands and at most 225 coordinate triples; one request
        # fits the minimum OS pipe capacity, with a reply barrier before reuse.
        if len(data) > 4096:
            raise RapfiUnavailable("Rapfi 协议请求过大")
        try:
            proc.stdin.write(data)
            proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise RapfiUnavailable("Rapfi 通信失败，棋局已保留") from exc

    def _read_until(self, expected, deadline, cancel_event):
        count = 0
        while True:
            self._check(cancel_event)
            if self._fault.is_set():
                raise RapfiUnavailable("Rapfi 输出异常或过多，已停止本次计算")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RapfiUnavailable("Rapfi 等待超时，棋局已保留")
            try:
                line = self._lines.get(timeout=min(.05, remaining))
            except queue.Empty:
                if self._proc.poll() is not None:
                    raise RapfiUnavailable("Rapfi 进程异常退出，请检查 CPU 架构及模型安装")
                continue
            count += 1
            if count > 2048:
                raise RapfiUnavailable("Rapfi 输出异常或过多，已停止本次计算")
            upper = line.upper()
            if upper.startswith(("ERROR", "UNKNOWN")) or "UNKNOWN INFO PARAMETER" in upper:
                raise RapfiUnavailable("Rapfi 拒绝协议或模型配置，请管理员检查安装")
            if any(token in upper for token in ("FAILED TO LOAD", "UNABLE TO OPEN", "FALLBACK")):
                raise RapfiUnavailable("Rapfi 模型加载失败，认真档不会自动降级")
            if expected == "ok" and upper == "OK":
                return None
            if expected == "about" and line.startswith('name="Rapfi",'):
                return line[:512]
            match = _COORD.fullmatch(line)
            if expected == "move" and match:
                x, y = map(int, match.groups())
                if not (0 <= x < 15 and 0 <= y < 15):
                    raise RapfiUnavailable("Rapfi 返回越界落子，棋局已保留")
                return y * 15 + x
            if upper.startswith(("MESSAGE", "DEBUG", "INFO")):
                continue
            raise RapfiUnavailable("Rapfi 返回无法识别的结果，棋局已保留")

    @staticmethod
    def _board_request(board, side):
        validate_board("gomoku", board)
        if type(side) is not int or side not in (1, 2):
            raise ValueError("棋子颜色无效")
        black = [i for i, value in enumerate(board) if value == 1]
        white = [i for i, value in enumerate(board) if value == 2]
        if (len(black) != len(white) + (side == 2) or outcome("gomoku", board)[0]):
            raise ValueError("Rapfi 仅接受合法轮次且尚未结束的棋盘")
        # Rapfi treats the FIRST submitted stone as black, and then replays
        # the list. Coordinate-sorted input can silently reverse black/white!
        # Reconstruct an alternating black-first order from the flat board.
        lines = ["BOARD"]
        for turn in range(len(black)):
            for stone, positions in ((1, black), (2, white)):
                if turn < len(positions):
                    index = positions[turn]
                    owner = 1 if stone == side else 2
                    lines.append(f"{index % 15},{index // 15},{owner}")
        return "\n".join(lines + ["DONE", ""])

    def _search(self, board, side, cancel_event, deadline):
        self._check(cancel_event)
        self._write("RESTART\n")
        self._read_until("ok", deadline, cancel_event)
        self._write(self._board_request(board, side))
        index = self._read_until("move", deadline, cancel_event)
        if board[index]:
            raise RapfiUnavailable("Rapfi 返回已占用的位置，棋局已保留")
        return index

    def _ensure_started(self, cancel_event):
        if self._proc is not None and self._proc.poll() is None:
            return
        self._stop_locked()
        self._preflight()
        deadline = time.monotonic() + self.startup_timeout
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            self._proc = subprocess.Popen([str(self.executable)], cwd=str(self.config_path.parent),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=0, shell=False, creationflags=flags)
        except OSError as exc:
            raise RapfiUnavailable("Rapfi 无法启动，请检查执行权限和 CPU 兼容版本") from exc
        self._lines, self._fault = queue.Queue(maxsize=_MAX_OUTPUT), threading.Event()
        self._stderr = deque(maxlen=16)
        self._threads = [
            threading.Thread(target=self._read_stdout, args=(self._proc.stdout, self._lines, self._fault), daemon=True),
            threading.Thread(target=self._read_stderr, args=(self._proc.stderr, self._stderr), daemon=True),
        ]
        for thread in self._threads:
            thread.start()
        self._write("INFO RULE 0\nINFO THREAD_NUM 1\nINFO HASH_SIZE 65536\n"
                    "INFO PONDERING 0\nINFO USEDATABASE 0\nINFO DATABASE_READONLY 1\n"
                    f"INFO TIMEOUT_TURN {int(self.move_seconds * 1000)}\nINFO TIMEOUT_MATCH 0\nSTART 15\n")
        self._read_until("ok", deadline, cancel_event)
        self._write("ABOUT\n")
        self.engine_info = self._read_until("about", deadline, cancel_event)
        # Non-opening preflight forces the engine to initialise its evaluator,
        # so broken weights cannot create a serious game that fails on move 1.
        probe = [0] * 225
        for index, stone in ((112, 1), (128, 2), (113, 1), (127, 2)):
            probe[index] = stone
        self._search(probe, 1, cancel_event, min(deadline, time.monotonic() + self.search_timeout))

    def check_ready(self, cancel_event=None):
        with self._exclusive(cancel_event):
            try:
                self._ensure_started(cancel_event)
                self._check(cancel_event)
            except BaseException:
                self._stop_locked()
                raise
            self._schedule_idle()

    def choose_move(self, board, side, cancel_event=None):
        board = list(board)
        self._board_request(board, side)  # Validate before starting a process.
        with self._exclusive(cancel_event):
            try:
                self._ensure_started(cancel_event)
                index = self._search(board, side, cancel_event, time.monotonic() + self.search_timeout)
                self._check(cancel_event)
            except BaseException:
                # Terminate instead of accepting a STOP result from the old
                # board. No cancelled output can leak into another group.
                self._stop_locked()
                raise
            self._schedule_idle()
            return index

    def _cancel_idle(self):
        self._generation += 1
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None

    def _schedule_idle(self):
        self._cancel_idle()
        generation = self._generation
        def expire():
            with self._lock:
                if generation == self._generation and self._proc is not None:
                    self._stop_locked()
        self._idle_timer = threading.Timer(self.idle_seconds, expire)
        self._idle_timer.daemon = True
        self._idle_timer.start()

    def _stop_locked(self):
        self._cancel_idle()
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.terminate()
                proc.wait(timeout=.4)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    proc.kill()
                    proc.wait(timeout=.4)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    pipe.close()
                except (OSError, ValueError):
                    pass
        for thread in self._threads:
            thread.join(timeout=.15)
        self._threads = []
        self._lines = self._fault = None

    def close(self):
        """Interrupt ongoing work and release the child. This instance is final."""
        self._closed.set()
        with self._lock:
            self._stop_locked()

    def diagnostics(self):
        """Read-only process observations; unavailable measurements stay None."""
        with self._lock:
            proc = self._proc
            pid = proc.pid if proc is not None and proc.poll() is None else None
        result = {"pid": pid, "peak_rss_kib": None, "rss_kib": None, "memory_source": None}
        if pid is None or os.name != "posix":
            return result
        try:
            # /proc is Linux-only. Do not substitute hash-table size or the
            # Python parent's resource usage when this is not readable.
            content = Path(f"/proc/{pid}/status").read_text(encoding="ascii")
            for field, key in (("VmHWM", "peak_rss_kib"), ("VmRSS", "rss_kib")):
                match = re.search(rf"^{field}:\s+(\d+)\s+kB$", content, re.MULTILINE)
                if match:
                    result[key] = int(match.group(1))
            if result["peak_rss_kib"] is not None or result["rss_kib"] is not None:
                result["memory_source"] = "linux_proc_status"
        except (OSError, UnicodeError, ValueError):
            pass
        return result
