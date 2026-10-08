"""Bounded CPU-only Fairy-Stockfish UCI / KataGo GTP adapters.

All public operations are synchronous: invoke using asyncio.to_thread. No
network, model API, shell, arbitrary engine commands or game-state writes.
The caller must revalidate returned moves against its rules and state version.
"""
from collections import deque
from pathlib import Path
import ctypes
import os
import queue
import re
import subprocess
import threading
import time

from .ai import SearchCancelled
from .engine_broker import GLOBAL_BROKER

XQ_VERSION = "xiangqi-c07e94a5c7cb"
GO_VERSION = "1.16.3"
GO_MODEL = "g170e-b10c128-s1141046784-d204142634.bin.gz"
XQ_FEN = "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w - - 0 1"
_UCI_MOVE = re.compile(r"[a-i](?:10|[1-9])[a-i](?:10|[1-9])")
_GO_COLS = "ABCDEFGHJKLMNOPQRST"
_MAX_LINE = 16384


class BoardEngineUnavailable(RuntimeError):
    """Do not advance the game or silently substitute a weaker algorithm."""


class _Cancellation:
    def __init__(self, owner, request):
        self.owner, self.request = owner, request

    def is_set(self):
        return self.owner.is_set() or (self.request is not None and self.request.is_set())


def process_rss(pid):
    """Bytes, or None when genuinely unavailable (never guess from Hash)."""
    if os.name == "posix":
        try:
            raw = Path(f"/proc/{pid}/status").read_text(encoding="ascii")
            match = re.search(r"^VmRSS:\s+(\d+)\s+kB$", raw, re.M)
            return int(match[1]) * 1024 if match else None
        except (OSError, ValueError):
            return None
    if os.name == "nt":
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in ("PeakWorkingSetSize", "WorkingSetSize",
                "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        psapi.GetProcessMemoryInfo.argtypes = (wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD)
        handle = kernel.OpenProcess(0x0400 | 0x0010, False, pid)
        if not handle:
            return None
        try:
            info = Counters()
            info.cb = ctypes.sizeof(info)
            return int(info.WorkingSetSize) if psapi.GetProcessMemoryInfo(handle, ctypes.byref(info), info.cb) else None
        finally:
            kernel.CloseHandle(handle)
    return None


def _number(config, name, default, minimum, maximum):
    value = config.get(name, default)
    if isinstance(value, bool):
        raise BoardEngineUnavailable(f"引擎 {name} 配置无效")
    try:
        value = float(value)
    except (TypeError, ValueError) as exc:
        raise BoardEngineUnavailable(f"引擎 {name} 配置无效") from exc
    if not minimum <= value <= maximum:
        raise BoardEngineUnavailable(f"引擎 {name} 配置超出安全范围")
    return value


class _Pipe:
    def __init__(self, command, cwd, limits):
        self.lines = queue.Queue(maxsize=512)
        self.writes = queue.Queue(maxsize=1)
        self.fault = threading.Event()
        self.stderr = deque(maxlen=16)
        self.rss_limit = int(limits.get("rss_limit_mib", 512) * 1024 * 1024)
        self.peak_rss = 0
        self._rss_at = 0.0
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            self.proc = subprocess.Popen(command, cwd=str(cwd), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, shell=False, creationflags=flags)
        except OSError as exc:
            raise BoardEngineUnavailable("无法启动本地引擎，请检查安装、执行权限和 CPU 架构") from exc
        self.threads = [
            threading.Thread(target=self._reader, args=(self.proc.stdout, False), daemon=True),
            threading.Thread(target=self._reader, args=(self.proc.stderr, True), daemon=True),
            threading.Thread(target=self._writer, daemon=True),
        ]
        for thread in self.threads:
            thread.start()

    def _reader(self, stream, is_stderr):
        try:
            while True:
                raw = stream.readline(_MAX_LINE + 1)
                if not raw:
                    return
                if len(raw) > _MAX_LINE:
                    self.fault.set()
                    return
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if is_stderr:
                    self.stderr.append(line[:512])
                    if any(term in line.lower() for term in ("failed to load", "unable to open",
                            "error opening", "network file")) and "error" in line.lower():
                        self.fault.set()
                else:
                    try:
                        self.lines.put_nowait(line)
                    except queue.Full:
                        self.fault.set()
                        return
        except (OSError, ValueError):
            return

    def _writer(self):
        while True:
            item = self.writes.get()
            if item is None:
                return
            data, done, errors = item
            try:
                data = memoryview(data)
                while data:
                    count = self.proc.stdin.write(data)
                    if not count:
                        raise OSError("closed engine stdin")
                    data = data[count:]
                self.proc.stdin.flush()
            except (OSError, ValueError) as exc:
                errors.append(exc)
            finally:
                done.set()

    def check(self, deadline, cancel):
        if cancel is not None and cancel.is_set():
            raise SearchCancelled()
        if time.monotonic() >= deadline:
            raise BoardEngineUnavailable("本地引擎等待超时，棋局已保留")
        if self.fault.is_set():
            raise BoardEngineUnavailable("本地引擎输出异常，棋局已保留")
        if self.proc.poll() is not None:
            raise BoardEngineUnavailable("本地引擎异常退出，棋局已保留")
        now = time.monotonic()
        if now >= self._rss_at:
            self._rss_at = now + .05
            rss = process_rss(self.proc.pid)
            if rss is None:
                raise BoardEngineUnavailable("无法监测本地引擎内存，已停止计算")
            self.peak_rss = max(self.peak_rss, rss)
            if rss > self.rss_limit:
                raise BoardEngineUnavailable("本地引擎内存超过保护阈值，棋局已保留")

    def send(self, command, deadline, cancel):
        if not isinstance(command, str) or "\n" in command or "\r" in command:
            raise BoardEngineUnavailable("非法引擎协议输入")
        data = (command + "\n").encode("ascii")
        if len(data) > 131072:
            raise BoardEngineUnavailable("棋局历史过长，拒绝引擎请求")
        self.check(deadline, cancel)
        done, errors = threading.Event(), []
        self.writes.put_nowait((data, done, errors))
        while not done.wait(.02):
            self.check(deadline, cancel)
        self.check(deadline, cancel)
        if errors:
            raise BoardEngineUnavailable("本地引擎写入失败，棋局已保留")

    def read(self, deadline, cancel):
        while True:
            self.check(deadline, cancel)
            try:
                return self.lines.get(timeout=.02)
            except queue.Empty:
                pass

    def close(self):
        proc = self.proc
        try:
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=.5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
                proc.wait(timeout=.5)
            except (OSError, subprocess.TimeoutExpired):
                pass
        if proc.poll() is None:
            raise BoardEngineUnavailable("旧引擎进程无法回收，已阻止启动其他引擎")
        try:
            self.writes.put_nowait(None)
        except queue.Full:
            pass
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                stream.close()
            except (OSError, ValueError):
                pass
        for thread in self.threads:
            thread.join(timeout=.1)


class _BaseEngine:
    def __init__(self, config, root, limits):
        self.config, self.root, self.limits = config, root, limits
        self.pipe = None
        self.engine_info = ""

    def path(self, name, default):
        value = self.config.get(name, default)
        if not isinstance(value, (str, Path)) or not str(value).strip():
            raise BoardEngineUnavailable(f"本地引擎 {name} 路径配置无效")
        path = Path(value)
        path = (path if path.is_absolute() else self.root / path).resolve()
        if not path.is_file() or not path.stat().st_size:
            raise BoardEngineUnavailable(f"缺少本地引擎文件：{path.name}，请管理员安装后重试")
        if name == "executable" and os.name != "nt" and not os.access(path, os.X_OK):
            raise BoardEngineUnavailable("本地引擎文件没有执行权限")
        return path

    def preflight(self):
        if self.config.get("enabled", False) is not True:
            raise BoardEngineUnavailable("此游戏的人机引擎尚未启用，可先进行双人对局")

    def close(self):
        pipe = self.pipe
        if pipe is not None:
            pipe.close()
        self.pipe = None

    def diagnostics(self):
        pipe = self.pipe
        return {"engine": self.engine_info, "pid": pipe.proc.pid if pipe else None,
                "rss_bytes": process_rss(pipe.proc.pid) if pipe else None,
                "observed_peak_rss_bytes": pipe.peak_rss if pipe else None}


class FairyEngine(_BaseEngine):
    def _until(self, token, deadline, cancel):
        rows, total = [], 0
        for _ in range(4096):
            line = self.pipe.read(deadline, cancel)
            total += len(line)
            if total > 1048576 or line.lower().startswith(("error", "unknown command")):
                raise BoardEngineUnavailable("象棋引擎协议异常")
            if line == token or line.startswith(token + " "):
                return line, rows
            if len(rows) < 256:
                rows.append(line)
        raise BoardEngineUnavailable("象棋引擎输出过多")

    def _send(self, command, deadline, cancel):
        self.pipe.send(command, deadline, cancel)

    def start(self, cancel):
        if self.pipe is not None:
            return
        self.preflight()
        default = f"data/engines/fairy-stockfish/{XQ_VERSION}/fairy-stockfish"
        exe = self.path("executable", default + (".exe" if os.name == "nt" else ""))
        deadline = time.monotonic() + self.limits["startup_timeout"]
        self.pipe = _Pipe([str(exe)], exe.parent, self.limits)
        self._send("uci", deadline, cancel)
        _, rows = self._until("uciok", deadline, cancel)
        self.engine_info = next((row[8:] for row in rows if row.startswith("id name ")), "")[:256]
        options = "\n".join(rows)
        if "fairy-stockfish" not in self.engine_info.lower() or "xiangqi" not in options:
            raise BoardEngineUnavailable("需要支持象棋的 Fairy-Stockfish 引擎")
        network = next((row for row in rows if row.startswith("option name EvalFile type ")), "")
        if XQ_VERSION + ".nnue" not in network:
            raise BoardEngineUnavailable("需要锁定版本的象棋内置 NNUE 引擎，不自动回退传统评估")
        for name in ("Threads", "Hash", "Ponder", "Skill Level", "Use NNUE", "UCI_Variant"):
            if f"option name {name} type" not in options:
                raise BoardEngineUnavailable(f"象棋引擎缺少必要选项：{name}")
        for name, value in (("UCI_Variant", "xiangqi"), ("Threads", 1), ("Hash", 32),
                            ("Ponder", "false"), ("Use NNUE", "true")):
            self._send(f"setoption name {name} value {value}", deadline, cancel)
        self._send("isready", deadline, cancel)
        self._until("readyok", deadline, cancel)
        # Search once at startup so a broken NNUE cannot create an unplayable game.
        self._search({"initial_fen": XQ_FEN, "moves": [], "difficulty": "casual"}, deadline, cancel)

    def _search(self, state, deadline, cancel):
        fen = state.get("initial_fen", XQ_FEN)
        if not isinstance(fen, str) or len(fen) > 256 or not re.fullmatch(r"[A-Za-z0-9/ -]+", fen):
            raise BoardEngineUnavailable("象棋初始局面无效")
        records = state.get("moves", [])
        if not isinstance(records, list) or len(records) > 10000:
            raise BoardEngineUnavailable("象棋历史无效或过长")
        moves = [row.get("move") if isinstance(row, dict) else row for row in records]
        if any(not isinstance(move, str) or not _UCI_MOVE.fullmatch(move) for move in moves):
            raise BoardEngineUnavailable("象棋历史着法无效")
        serious = state.get("difficulty") == "serious"
        self._send("ucinewgame", deadline, cancel)
        self._send(f"setoption name Skill Level value {20 if serious else 0}", deadline, cancel)
        self._send("isready", deadline, cancel)
        self._until("readyok", deadline, cancel)
        self._send("position fen " + fen + (" moves " + " ".join(moves) if moves else ""), deadline, cancel)
        self._send(f"go movetime {2000 if serious else 300}", deadline, cancel)
        result, _ = self._until("bestmove", deadline, cancel)
        tokens = result.split()
        if len(tokens) < 2 or not _UCI_MOVE.fullmatch(tokens[1]):
            raise BoardEngineUnavailable("象棋引擎未返回有效着法，棋局已保留")
        return tokens[1]

    def choose(self, state, cancel):
        self.start(cancel)
        return self._search(state, time.monotonic() + self.limits["search_timeout"], cancel)


def go_vertex(move, size):
    if move == "pass":
        return "pass"
    if type(move) is not int or not 0 <= move < size * size:
        raise BoardEngineUnavailable("围棋历史坐标无效")
    return f"{_GO_COLS[move % size]}{move // size + 1}"


def go_index(vertex, size):
    if vertex.lower() == "pass":
        return "pass"
    if not re.fullmatch(r"[A-HJ-T](?:[1-9]|1[0-9])", vertex.upper()):
        raise BoardEngineUnavailable("围棋引擎返回非法坐标")
    x, y = _GO_COLS.index(vertex[0].upper()), int(vertex[1:]) - 1
    if x >= size or y >= size:
        raise BoardEngineUnavailable("围棋引擎返回越界坐标")
    return y * size + x


class KataGoEngine(_BaseEngine):
    def __init__(self, *args):
        super().__init__(*args)
        self.request_id = 0

    def command(self, command, deadline, cancel):
        self.request_id += 1
        req = self.request_id
        self.pipe.send(f"{req} {command}", deadline, cancel)
        header, rows, total = None, [], 0
        for _ in range(1024):
            line = self.pipe.read(deadline, cancel)
            total += len(line)
            if total > 131072:
                raise BoardEngineUnavailable("围棋引擎输出过大")
            if header is None:
                if not line.strip():
                    continue
                match = re.fullmatch(r"([=?])(\d+)(?: +(.*))?", line)
                if not match or int(match[2]) != req:
                    raise BoardEngineUnavailable("围棋引擎协议响应不匹配")
                header = match[1]
                if match[3]:
                    rows.append(match[3])
            elif not line.strip():
                if header == "?":
                    raise BoardEngineUnavailable("围棋引擎拒绝请求，检查规则、模型与安装版本")
                return "\n".join(rows).strip()
            else:
                rows.append(line)
        raise BoardEngineUnavailable("围棋引擎输出过多")

    def start(self, cancel):
        if self.pipe is not None:
            return
        self.preflight()
        base = f"data/engines/katago/{GO_VERSION}"
        exe = self.path("executable", base + ("/katago.exe" if os.name == "nt" else "/katago"))
        model = self.path("model_path", base + "/" + GO_MODEL)
        cfg = self.path("config_path", base + "/minigames.cfg")
        # Critical limits override even an accidentally edited sample config.
        override = ("numSearchThreads=1,numEigenThreadsPerModel=1,numNNServerThreadsPerModel=1,"
                    "nnMaxBatchSize=1,nnCacheSizePowerOfTwo=14,nnMutexPoolSizePowerOfTwo=10,"
                    "ponderingEnabled=false,allowResignation=false,logAllGTPCommunication=false,"
                    "logSearchInfo=false,logSearchInfoForChosenMove=false,logToStderr=false")
        deadline = time.monotonic() + self.limits["startup_timeout"]
        self.pipe = _Pipe([str(exe), "gtp", "-model", str(model), "-config", str(cfg),
                           "-override-config", override], cfg.parent, self.limits)
        if self.command("protocol_version", deadline, cancel) != "2":
            raise BoardEngineUnavailable("围棋引擎 GTP 版本不匹配")
        name = self.command("name", deadline, cancel)
        version = self.command("version", deadline, cancel)
        # GTP appends the loaded model, e.g. 1.16.3+g170-b10c128-s1141M.
        if name.lower() != "katago" or version.removeprefix("v").split("+", 1)[0] != GO_VERSION:
            raise BoardEngineUnavailable(f"围棋引擎需要 KataGo {GO_VERSION}")
        self.engine_info = f"{name} {version}"
        probe = {"board_size": 9, "moves": [], "komi": 7.5, "rules": "chinese-ogs", "turn": 1}
        self._replay(probe, deadline, cancel)
        self._limits(False, deadline, cancel)
        go_index(self.command("kata-search B", deadline, cancel), 9)

    def _limits(self, serious, deadline, cancel):
        for name, value in (("maxVisits", 256 if serious else 16), ("maxTime", 3 if serious else .5),
                            ("chosenMoveTemperature", 0 if serious else .8),
                            ("chosenMoveTemperatureEarly", 0 if serious else .8)):
            self.command(f"kata-set-param {name} {value}", deadline, cancel)

    def _replay(self, state, deadline, cancel):
        size = state.get("board_size", 9)
        if type(size) is not int or size not in (9, 13, 19):
            raise BoardEngineUnavailable("围棋棋盘尺寸无效")
        if state.get("komi", 7.5) != 7.5 or state.get("rules", state.get("rule", "chinese-ogs")) != "chinese-ogs":
            raise BoardEngineUnavailable("围棋规则与引擎设置不一致")
        records = state.get("moves", [])
        if not isinstance(records, list) or len(records) > 10000:
            raise BoardEngineUnavailable("围棋历史无效或过长")
        # Replay, never set_position: preserve positional superko and passes.
        for command in (f"boardsize {size}", "clear_board", "clear_cache", "komi 7.5", "kata-set-rules chinese-ogs"):
            self.command(command, deadline, cancel)
        for row in records:
            if not isinstance(row, dict) or type(row.get("side")) is not int or row["side"] not in (1, 2):
                raise BoardEngineUnavailable("围棋着法历史无效")
            color = "B" if row["side"] == 1 else "W"
            self.command(f"play {color} {go_vertex(row.get('move'), size)}", deadline, cancel)
        return size

    def choose(self, state, cancel):
        self.start(cancel)
        deadline = time.monotonic() + self.limits["search_timeout"]
        size = self._replay(state, deadline, cancel)
        self._limits(state.get("difficulty") == "serious", deadline, cancel)
        side = state.get("turn")
        if type(side) is not int or side not in (1, 2):
            raise BoardEngineUnavailable("围棋当前回合无效")
        move = self.command("kata-search " + ("B" if side == 1 else "W"), deadline, cancel)
        return go_index(move, size)

    def suggest_dead(self, state, cancel):
        self.start(cancel)
        deadline = time.monotonic() + self.limits["search_timeout"]
        size = self._replay(state, deadline, cancel)
        self._limits(True, deadline, cancel)
        raw = self.command("final_status_list dead", deadline, cancel)
        points = [go_index(vertex, size) for vertex in raw.split()]
        if any(type(point) is not int for point in points):
            raise BoardEngineUnavailable("围棋死子建议包含非法坐标")
        return sorted(set(points))


class BoardEngines:
    """Pass the minigames config; one broker includes the optional Rapfi."""
    def __init__(self, config=None, root=None, rapfi=None, *, broker=None):
        self.config = dict(config or {})
        self.root = Path(root or Path.cwd()).resolve()
        cfg = self.config.get("engine_limits") or {}
        self.limits = {
            "startup_timeout": _number(cfg, "startup_timeout", 20, .05, 60),
            "search_timeout": _number(cfg, "search_timeout", 8, .05, 30),
            "idle_seconds": _number(cfg, "idle_seconds", 60, .05, 300),
            "rss_limit_mib": _number(cfg, "rss_limit_mib", 512, 1, 512),
        }
        self._broker = broker or GLOBAL_BROKER
        self._owner = object()  # Identity cannot be recycled while a lease exists.
        self._closed = threading.Event()
        self._rapfi_seed = rapfi
        self._rapfi_config = dict(getattr(rapfi, "config", self.config.get("rapfi") or {}))
        self._rapfi_type = type(rapfi) if rapfi is not None else None
        self._diagnostics = {}

    def _factory(self, game):
        if game == "rapfi":
            if self._rapfi_seed is not None:
                engine, self._rapfi_seed = self._rapfi_seed, None
                return engine
            from .rapfi import RapfiEngine
            return (self._rapfi_type or RapfiEngine)(self._rapfi_config, root=self.root)
        section = self.config.get(game) or {}
        config = section.get("engine") or {}
        if section.get("enabled", True) is not True:
            raise BoardEngineUnavailable("此小游戏已停用")
        cls = {"xiangqi": FairyEngine, "go": KataGoEngine}.get(game)
        if cls is None:
            raise BoardEngineUnavailable("未知本地棋类引擎")
        return cls(config, self.root, self.limits)

    def _run(self, game, operation, cancel):
        token = _Cancellation(self._closed, cancel)
        def execute(engine, token):
            result = operation(engine, token)
            self._diagnostics[game] = engine.diagnostics()
            return result
        return self._broker.run((self._owner, game), lambda: self._factory(game), execute,
                                token, self.limits["idle_seconds"])

    def check_ready(self, game, cancel=None):
        return self._run(game, lambda engine, token: engine.start(token), cancel)

    def choose(self, state, cancel=None):
        return self._run(state.get("game"), lambda engine, token: engine.choose(state, token), cancel)

    def suggest_dead(self, state, cancel=None):
        if state.get("game") != "go":
            raise BoardEngineUnavailable("只有围棋可以建议死子")
        return self._run("go", lambda engine, token: engine.suggest_dead(state, token), cancel)

    def check_rapfi(self, cancel=None):
        return self._run("rapfi", lambda engine, token: engine.check_ready(token), cancel)

    def choose_rapfi(self, board, side, cancel=None):
        return self._run("rapfi", lambda engine, token: engine.choose_move(board, side, token), cancel)

    def diagnostics(self, game):
        """Last measured observations; no invented CPU/strength measurements."""
        return dict(self._diagnostics.get(game, {}))

    def close(self):
        self._closed.set()
        self._broker.close_owner(self._owner)
        if self._rapfi_seed is not None:
            self._rapfi_seed.close()
            self._rapfi_seed = None
