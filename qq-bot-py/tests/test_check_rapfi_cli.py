from importlib.util import spec_from_file_location, module_from_spec
from pathlib import Path
import types

import pytest


@pytest.fixture
def cli(tmp_path, monkeypatch):
    spec = spec_from_file_location("_check_rapfi_cli_benchmark_tests", Path(__file__).parents[1] / "tools/check_rapfi.py")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / "config.yaml").write_text("minigames: {rapfi: {enabled: true}}", encoding="utf-8")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    return module


@pytest.mark.parametrize("argv,expected_moves,benchmark_calls", [( [], 0, 0), (["--self-test"], 4, 0), (["--benchmark"], 0, 1)])
def test_benchmark_only_runs_when_explicitly_requested(cli, monkeypatch, argv, expected_moves, benchmark_calls):
    calls = {"moves": 0, "benchmark": 0, "closed": 0}
    class FakeEngine:
        engine_info = "test only"
        def __init__(self, *_):
            pass
        def check_ready(self):
            pass
        def choose_move(self, *_):
            calls["moves"] += 1
            return 4
        def close(self):
            calls["closed"] += 1
    monkeypatch.setattr(cli, "_adapter", lambda: types.SimpleNamespace(RapfiEngine=FakeEngine, RapfiUnavailable=RuntimeError))
    def benchmark(*_):
        calls["benchmark"] += 1
        return {"failed": False}
    monkeypatch.setattr(cli, "_benchmark", benchmark)
    assert cli.main(argv) == 0
    assert calls == {"moves": expected_moves, "benchmark": benchmark_calls, "closed": 1}


def _benchmark_modules(cli, monkeypatch, terminal=None):
    def play(_, board, index, side):
        assert 0 <= index < 225 and not board[index]
        board = list(board)
        board[index] = side
        return board
    rules = types.SimpleNamespace(new_board=lambda _: [0]*225, play_move=play,
        outcome=lambda _, board: (terminal(board) if terminal else 0, []),
        format_move=lambda _, index: f"{chr(65+index%15)}{index//15+1}")
    def choose(game, board, side, difficulty, budget):
        assert (game, difficulty, budget) == ("gomoku", "normal", 1.0)
        return next(i for i, value in enumerate(board) if not value)
    ai = types.SimpleNamespace(choose_move=choose)
    original = cli.importlib.import_module
    def load(name):
        return {"_benchmark_fake.rules": rules, "_benchmark_fake.ai": ai}.get(name) or original(name)
    monkeypatch.setattr(cli.importlib, "import_module", load)
    return types.SimpleNamespace(__package__="_benchmark_fake", RapfiUnavailable=RuntimeError)


class BenchmarkEngine:
    move_seconds = 2
    def choose_move(self, board, side):
        return next(i for i, value in enumerate(board) if not value)
    def diagnostics(self):
        return {"peak_rss_kib": 128000}


def test_four_fixed_openings_sides_and_cap_are_not_false_draws(cli, monkeypatch, capsys):
    adapter = _benchmark_modules(cli, monkeypatch)
    report = cli._benchmark(BenchmarkEngine(), adapter)
    assert [game["engine_side"] for game in report["games"]] == [1, 2, 1, 2]
    assert [game["opening"] for game in report["games"]] == ["近身开局", "近身开局", "交错开局", "交错开局"]
    assert all(game["plies"] == 120 for game in report["games"])
    assert report["summary"] == {"win": 0, "loss": 0, "draw": 0, "unfinished": 4}
    assert report["peak_rss_kib"] == 128000
    assert all(value >= 0 for values in report["timing"].values() for value in values)
    output = capsys.readouterr().out
    assert "125.0 MiB" in output and "elapsed=" in output


@pytest.mark.parametrize("winner,expected", [(1, {"win": 2, "loss": 2, "draw": 0, "unfinished": 0}),
                                              (-1, {"win": 0, "loss": 0, "draw": 4, "unfinished": 0})])
def test_benchmark_respects_actual_outcome(cli, monkeypatch, winner, expected):
    adapter = _benchmark_modules(cli, monkeypatch, terminal=lambda board: winner if sum(v != 0 for v in board) >= 8 else 0)
    assert cli._benchmark(BenchmarkEngine(), adapter)["summary"] == expected


def test_benchmark_unknown_memory_is_not_hash_size(cli, monkeypatch, capsys):
    adapter = _benchmark_modules(cli, monkeypatch, terminal=lambda board: 1 if sum(v != 0 for v in board) >= 8 else 0)
    engine = BenchmarkEngine()
    engine.diagnostics = lambda: {"peak_rss_kib": None}
    report = cli._benchmark(engine, adapter)
    assert report["peak_rss_kib"] is None
    assert "VmHWM 最大值=未知" in capsys.readouterr().out


def test_benchmark_engine_failure_retains_unfinished_and_aborts_remaining(cli, monkeypatch):
    adapter = _benchmark_modules(cli, monkeypatch)
    engine = BenchmarkEngine()
    def fail(*_):
        raise RuntimeError("mock engine unavailable")
    engine.choose_move = fail
    report = cli._benchmark(engine, adapter)
    assert report["failed"] is True
    assert len(report["games"]) == 1
    assert report["summary"] == {"win": 0, "loss": 0, "draw": 0, "unfinished": 1}
