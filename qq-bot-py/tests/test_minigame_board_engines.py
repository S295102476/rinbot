import importlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys
import threading
import time
import types

import pytest

package = types.ModuleType("_minigame_board_engine_tests")
package.__path__ = [str(Path(__file__).parents[1] / "plugins/minigames")]
sys.modules[package.__name__] = package
adapters = importlib.import_module(package.__name__ + ".board_engines")
broker_module = importlib.import_module(package.__name__ + ".engine_broker")


@pytest.fixture
def installed(tmp_path, monkeypatch):
    cfg = tmp_path / "minigames.cfg"
    model = tmp_path / "model.bin.gz"
    cfg.write_text("test config", encoding="utf-8")
    model.write_bytes(b"test model")
    original = subprocess.Popen
    mode, children, commands = {"value": "normal"}, [], []
    log = tmp_path / "protocol.log"
    def popen(command, **kwargs):
        assert kwargs["shell"] is False
        if sys.platform == "win32":
            assert kwargs["creationflags"] == subprocess.CREATE_NO_WINDOW
        protocol = "gtp" if "gtp" in command else "uci"
        commands.append(command)
        child = original([sys.executable, "-u", str(Path(__file__).parent / "helpers/fake_board_engine.py"),
                          "--protocol", protocol, "--mode", mode["value"], "--log", str(log)], **kwargs)
        assert all(p.poll() is not None for p in children), "two external engines overlapped"
        children.append(child)
        return child
    monkeypatch.setattr(adapters.subprocess, "Popen", popen)
    managers = []
    def create(**limits):
        config = {"engine_limits": {"startup_timeout": 2, "search_timeout": .5, "idle_seconds": 10, **limits},
                  "xiangqi": {"engine": {"enabled": True, "executable": sys.executable}},
                  "go": {"engine": {"enabled": True, "executable": sys.executable,
                                     "model_path": str(model), "config_path": str(cfg)}}}
        result = adapters.BoardEngines(config, tmp_path, broker=broker_module.EngineBroker())
        managers.append(result)
        return result
    yield create, mode, children, log, commands
    for manager in managers:
        manager.close()
    assert all(p.poll() is not None for p in children)


def xiangqi(**updates):
    return {"game": "xiangqi", "initial_fen": adapters.XQ_FEN, "difficulty": "casual",
            "turn": 1, "moves": [], **updates}


def go(**updates):
    return {"game": "go", "board_size": 9, "difficulty": "casual", "turn": 1,
            "komi": 7.5, "rules": "chinese-ogs", "moves": [], **updates}


def test_fairy_limits_full_history_and_no_shell(installed):
    create, _, children, log, _ = installed
    manager = create()
    manager.check_ready("xiangqi")
    assert manager.choose(xiangqi(moves=[{"move": "h3e3", "side": 1}], difficulty="serious")) == "h3e3"
    transcript = log.read_text(encoding="utf-8")
    for expected in ("setoption name Threads value 1", "setoption name Hash value 32",
                     "setoption name Ponder value false", "setoption name Use NNUE value true",
                     "setoption name Skill Level value 20", "go movetime 2000", "go movetime 300",
                     "position fen " + adapters.XQ_FEN + " moves h3e3"):
        assert expected in transcript
    assert len(children) == 1
    assert manager.diagnostics("xiangqi")["rss_bytes"] > 0


def test_go_limits_replay_pass_resume_and_dead_status(installed):
    create, _, children, log, commands = installed
    manager = create()
    state = go(board_size=19, turn=2, difficulty="serious", resume_after=[4], moves=[
        {"side": 1, "move": 8}, {"side": 2, "move": 360},
        {"side": 1, "move": "pass"}, {"side": 2, "move": "pass"},
        {"side": 1, "move": 0}])
    assert manager.choose(state) == 60
    assert manager.suggest_dead(state) == [0, 40, 41]
    transcript = log.read_text(encoding="utf-8")
    for expected in ("boardsize 19", "kata-set-rules chinese-ogs", "komi 7.5", "clear_cache",
                     "play B J1", "play W T19", "play B pass", "play W pass", "play B A1",
                     "kata-set-param maxVisits 256", "kata-set-param maxTime 3",
                     "kata-set-param chosenMoveTemperature 0", "kata-search W", "final_status_list dead"):
        assert expected in transcript
    assert "set_position" not in transcript
    assert len(children) == 1
    assert "nnCacheSizePowerOfTwo=14" in commands[0][-1]
    assert "numEigenThreadsPerModel=1" in commands[0][-1]


def test_switching_kills_previous_child_before_next_start(installed):
    create, _, children, _, _ = installed
    manager = create()
    manager.choose(go())
    manager.choose(xiangqi())
    manager.choose(go())
    assert len(children) == 3
    assert children[0].poll() is not None and children[1].poll() is not None


@pytest.mark.parametrize("game", ["xiangqi", "go"])
@pytest.mark.parametrize("mode_name", ["search_hang", "crash", "bad_move"])
def test_failures_close_child_retain_no_reused_output(installed, game, mode_name):
    create, mode, children, _, _ = installed
    mode["value"] = mode_name
    manager = create()
    with pytest.raises(adapters.BoardEngineUnavailable):
        manager.choose(xiangqi() if game == "xiangqi" else go())
    assert children[-1].poll() is not None
    mode["value"] = "normal"
    assert manager.choose(xiangqi() if game == "xiangqi" else go()) is not None


@pytest.mark.parametrize("mode_name", ["startup_hang", "huge_line", "bad_id", "bad_version"])
def test_bad_startup_bounded_and_reaped(installed, mode_name):
    create, mode, children, _, _ = installed
    mode["value"] = mode_name
    manager = create(startup_timeout=.25)
    with pytest.raises(adapters.BoardEngineUnavailable):
        manager.check_ready("go")
    assert children[-1].poll() is not None


def test_rss_protection_and_unknown_measurement_fail_closed(installed, monkeypatch):
    create, _, children, _, _ = installed
    for rss in (1024**3, None):
        monkeypatch.setattr(adapters, "process_rss", lambda pid: rss)
        with pytest.raises(adapters.BoardEngineUnavailable):
            create().check_ready("go")
        assert children[-1].poll() is not None


def test_cancel_running_search_drops_child(installed):
    create, mode, children, _, _ = installed
    mode["value"] = "search_hang"
    manager = create(search_timeout=4)
    manager.check_ready("go")
    cancel = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(manager.choose, go(), cancel)
        time.sleep(.08)
        cancel.set()
        with pytest.raises(adapters.SearchCancelled):
            future.result(timeout=2)
    assert children[-1].poll() is not None


def test_cancelled_queue_does_not_start_new_child(installed):
    create, mode, children, _, _ = installed
    mode["value"] = "search_hang"
    manager = create(search_timeout=4)
    manager.check_ready("go")
    running_cancel, queued_cancel = threading.Event(), threading.Event()
    with ThreadPoolExecutor(max_workers=2) as executor:
        active = executor.submit(manager.choose, go(), running_cancel)
        time.sleep(.08)
        queued = executor.submit(manager.choose, xiangqi(), queued_cancel)
        queued_cancel.set()
        with pytest.raises(adapters.SearchCancelled):
            queued.result(timeout=2)
        running_cancel.set()
        with pytest.raises(adapters.SearchCancelled):
            active.result(timeout=2)
    assert len(children) == 1


def test_idle_exit_and_restart(installed):
    create, _, children, _, _ = installed
    manager = create(idle_seconds=.05)
    manager.check_ready("go")
    deadline = time.monotonic() + 2
    while children[-1].poll() is None and time.monotonic() < deadline:
        time.sleep(.02)
    assert children[-1].poll() is not None
    manager.check_ready("go")
    assert len(children) == 2


@pytest.mark.parametrize("state", [
    xiangqi(initial_fen="bad\ngo infinite"), xiangqi(moves=[{"move": "h3e3\nquit"}]),
    go(rules="japanese"), go(komi=0), go(board_size=25), go(moves=[{"side": 3, "move": 0}]),
])
def test_untrusted_state_rejected(installed, state):
    create, _, _, _, _ = installed
    with pytest.raises(adapters.BoardEngineUnavailable):
        create().choose(state)


def test_missing_or_disabled_engine_no_child(installed):
    create, _, children, _, _ = installed
    manager = create()
    manager.config["go"]["engine"]["enabled"] = False
    with pytest.raises(adapters.BoardEngineUnavailable):
        manager.check_ready("go")
    assert not children


def test_wrong_fairy_network_cannot_silently_downgrade(installed):
    create, mode, children, _, _ = installed
    mode["value"] = "wrong_network"
    with pytest.raises(adapters.BoardEngineUnavailable, match="NNUE"):
        create().check_ready("xiangqi")
    assert children[-1].poll() is not None


def test_gtp_pass_and_coordinate_roundtrip(installed):
    create, mode, _, _, _ = installed
    mode["value"] = "pass"
    assert create().choose(go()) == "pass"
    for size in (9, 13, 19):
        for index in range(size * size):
            assert adapters.go_index(adapters.go_vertex(index, size), size) == index
    with pytest.raises(adapters.BoardEngineUnavailable):
        adapters.go_index("I1", 19)


def test_rapfi_recreated_after_switch_no_final_closed_instance(installed):
    create, _, _, _, _ = installed
    manager = create()
    records = []
    class FakeRapfi:
        def __init__(self, config=None, root=None):
            self.config = config or {}
            self.closed = False
            records.append(self)
        def check_ready(self, token):
            assert not self.closed
        def choose_move(self, board, side, token):
            assert not self.closed
            return 112
        def diagnostics(self):
            return {}
        def close(self):
            self.closed = True
    seed = FakeRapfi()
    manager._rapfi_seed, manager._rapfi_type = seed, FakeRapfi
    manager.check_rapfi()
    manager.choose(go())
    assert seed.closed
    assert manager.choose_rapfi([0] * 225, 1) == 112
    assert len(records) == 2 and not records[1].closed


def test_broker_failed_termination_keeps_lease_blocks_new_child():
    broker = broker_module.EngineBroker()
    cancel = threading.Event()
    starts = []
    class Child:
        fails = True
        def close(self):
            if self.fails:
                raise RuntimeError("cannot reap")
    child = Child()
    broker.run((1, "first"), lambda: child, lambda child, token: None, cancel, 10)
    with pytest.raises(RuntimeError, match="cannot reap"):
        broker.run((1, "next"), lambda: starts.append(1), lambda child, token: None, cancel, 10)
    assert not starts
    assert broker._engine is child
    child.fails = False
    broker.close_owner(1)
