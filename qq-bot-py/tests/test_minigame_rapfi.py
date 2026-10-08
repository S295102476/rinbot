import importlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys
import threading
import time
import types

import pytest

package = types.ModuleType("_minigame_rapfi_tests")
package.__path__ = [str(Path(__file__).parents[1] / "plugins/minigames")]
sys.modules[package.__name__] = package
rapfi = importlib.import_module(package.__name__ + ".rapfi")


@pytest.fixture
def installed(tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    config_path.write_text("""[general]
coord_conversion_mode = "none"
default_thread_num = 1
reload_config_each_move = false
[model]
binary_file = "classical.bin"
[model.evaluator]
type = "mix9svq"
[[model.evaluator.weights]]
weight_file = "freestyle.bin"
[database]
enable_by_default = false
""", encoding="utf-8")
    for asset in ("classical.bin", "freestyle.bin"):
        (tmp_path / asset).write_bytes(b"fake test weight")
    original_popen = subprocess.Popen
    children = []
    mode = {"value": "normal"}
    log = tmp_path / "protocol.log"
    def fake_popen(command, **kwargs):
        assert kwargs["shell"] is False
        assert kwargs["cwd"] == str(tmp_path)
        assert len(command) == 1
        if sys.platform == "win32":
            assert kwargs["creationflags"] == subprocess.CREATE_NO_WINDOW
        child = original_popen([sys.executable, "-u", str(Path(__file__).parent / "helpers/fake_rapfi.py"),
                                "--mode", mode["value"], "--log", str(log)], **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(rapfi.subprocess, "Popen", fake_popen)
    engines = []
    def create(**overrides):
        config = dict(enabled=True, executable=sys.executable, config_path=str(config_path),
                      startup_timeout=2, search_timeout=.6, move_seconds=.05, idle_seconds=10)
        config.update(overrides)
        engine = rapfi.RapfiEngine(config, root=tmp_path)
        engines.append(engine)
        return engine
    yield create, mode, log, children, config_path
    for engine in engines:
        engine.close()
    assert all(child.poll() is not None for child in children)


def position(side=1):
    board = [0] * 225
    board[112], board[1] = 1, 2
    if side == 2:
        board[128] = 1
    return board


def test_protocol_limits_probe_reuse_and_full_board_order(installed):
    create, _, log, children, _ = installed
    engine = create()
    engine.check_ready()
    assert "FAKE TEST ONLY" in engine.engine_info
    first_pid = children[0].pid
    for side in (1, 2):
        board = position(side)
        original = board[:]
        assert board[engine.choose_move(board, side)] == 0
        assert board == original
    assert len(children) == 1 and children[0].pid == first_pid
    text = log.read_text(encoding="utf-8")
    for option in ("INFO RULE 0", "INFO THREAD_NUM 1", "INFO HASH_SIZE 65536",
                   "INFO PONDERING 0", "INFO USEDATABASE 0", "INFO TIMEOUT_TURN 50"):
        assert option in text
    # First stone must be real black, not the first occupied row-major point.
    assert "BOARD\n7,7,1\n1,0,2\nDONE" in text
    assert "BOARD\n7,7,2\n1,0,1\n8,8,2\nDONE" in text


@pytest.mark.parametrize("mode", ["timeout", "crash", "overlong", "malformed", "range", "occupied"])
def test_bad_move_never_silently_falls_back_and_process_stops(installed, mode):
    create, behavior, _, children, _ = installed
    behavior["value"] = mode
    engine = create()
    engine.check_ready()
    started = time.monotonic()
    with pytest.raises(rapfi.RapfiUnavailable):
        engine.choose_move(position(), 1)
    assert time.monotonic() - started < 2
    assert children[0].poll() is not None


@pytest.mark.parametrize("mode", ["startup_timeout", "startup_crash", "probe_error"])
def test_startup_failure_refuses_ready(installed, mode):
    create, behavior, _, children, _ = installed
    behavior["value"] = mode
    engine = create(startup_timeout=.25)
    with pytest.raises(rapfi.RapfiUnavailable):
        engine.check_ready()
    assert children[0].poll() is not None


def test_cancel_inflight_kills_process_then_next_game_restarts(installed):
    create, behavior, _, children, _ = installed
    behavior["value"] = "timeout"
    engine = create(search_timeout=3)
    engine.check_ready()
    cancel = threading.Event()
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(engine.choose_move, position(), 1, cancel)
        time.sleep(.08)
        cancel.set()
        with pytest.raises(rapfi.SearchCancelled):
            future.result(timeout=1)
    assert children[0].poll() is not None
    behavior["value"] = "normal"
    assert engine.choose_move(position(), 1) == 0
    assert len(children) == 2


def test_cancel_waiting_global_search_does_not_launch_child(installed):
    create, _, _, children, _ = installed
    engine = create()
    cancel = threading.Event()
    rapfi._GLOBAL_SEARCH_LOCK.acquire()
    try:
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(engine.check_ready, cancel)
            time.sleep(.07)
            cancel.set()
            with pytest.raises(rapfi.SearchCancelled):
                future.result(timeout=.5)
        assert not children
    finally:
        rapfi._GLOBAL_SEARCH_LOCK.release()


def test_idle_release_then_lazy_restart(installed):
    create, _, _, children, _ = installed
    engine = create(idle_seconds=.06)
    engine.check_ready()
    deadline = time.monotonic() + 1
    while children[0].poll() is None and time.monotonic() < deadline:
        time.sleep(.02)
    assert children[0].poll() is not None
    engine.check_ready()
    assert len(children) == 2


def test_stderr_is_drained_and_bounded(installed):
    create, behavior, _, _, _ = installed
    behavior["value"] = "stderr"
    engine = create(search_timeout=2)
    assert engine.choose_move(position(), 1) == 0
    assert len(engine._stderr) <= 16
    assert all(len(line) <= 512 for line in engine._stderr)


def test_preflight_off_missing_asset_and_unsafe_coordinates(installed):
    create, _, _, children, config_path = installed
    with pytest.raises(rapfi.RapfiUnavailable, match="尚未启用"):
        create(enabled=False).check_ready()
    with pytest.raises(rapfi.RapfiUnavailable, match="未安装"):
        create(executable="missing").check_ready()
    original = config_path.read_text(encoding="utf-8")
    config_path.write_text(original.replace('= "none"', '= "flipY_X"'), encoding="utf-8")
    with pytest.raises(rapfi.RapfiUnavailable, match="none 坐标"):
        create().check_ready()
    config_path.write_text(original.replace("freestyle.bin", "missing.bin"), encoding="utf-8")
    with pytest.raises(rapfi.RapfiUnavailable, match="缺少模型文件"):
        create().check_ready()
    assert not children


def test_invalid_or_finished_board_does_not_launch(installed):
    create, _, _, children, _ = installed
    engine = create()
    with pytest.raises(ValueError):
        engine.choose_move(position(2), 1)
    board = [0] * 225
    board[:5] = [1] * 5
    board[16:20] = [2] * 4
    with pytest.raises(ValueError):
        engine.choose_move(board, 2)
    assert not children


def test_close_interrupts_search(installed):
    create, behavior, _, children, _ = installed
    behavior["value"] = "timeout"
    engine = create(search_timeout=3)
    engine.check_ready()
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(engine.choose_move, position(), 1)
        time.sleep(.07)
        engine.close()
        with pytest.raises(rapfi.RapfiUnavailable, match="关闭"):
            future.result(timeout=1)
    assert children[0].poll() is not None


def test_different_instances_cannot_search_concurrently(installed, monkeypatch):
    create, behavior, _, _, _ = installed
    behavior["value"] = "slow"
    engines = [create(), create()]
    for engine in engines:
        engine.check_ready()
    original = rapfi.RapfiEngine._search
    active = 0
    peak = 0
    guard = threading.Lock()
    def measured(self, *args, **kwargs):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        try:
            return original(self, *args, **kwargs)
        finally:
            with guard:
                active -= 1
    monkeypatch.setattr(rapfi.RapfiEngine, "_search", measured)
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(engine.choose_move, position(), 1) for engine in engines]
        assert [future.result(timeout=2) for future in futures] == [0, 0]
    assert peak == 1


def test_check_cli_tactical_positions_are_legal():
    from importlib.util import spec_from_file_location, module_from_spec
    spec = spec_from_file_location("_check_rapfi_cli_tests", Path(__file__).parents[1] / "tools/check_rapfi.py")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    assert len(list(module._cases())) == 4
    for _, board, side, expected in module._cases():
        assert board[expected] == 0
        rapfi.RapfiEngine._board_request(board, side)
