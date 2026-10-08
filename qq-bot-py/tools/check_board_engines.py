"""Read-only readiness and bounded real-engine probes. Does not start NoneBot.

.venv/bin/python tools/check_board_engines.py --self-test
Reports observed memory/time only; does not claim a human playing rank.
"""
import argparse
import importlib
import json
from pathlib import Path
import sys
import time
import types

import yaml

ROOT = Path(__file__).resolve().parents[1]


def adapter(root):
    name = "_board_engine_operator_check"
    package = types.ModuleType(name)
    package.__path__ = [str(root / "plugins/minigames")]
    sys.modules[name] = package
    return importlib.import_module(name + ".board_engines")


def verify_installed(root, game):
    """Recheck install-manifest hashes before running any installed binary."""
    import hashlib
    from install_board_engines import ASSETS, MODEL, XQ_TAG, GO_TAG
    if game == "xiangqi":
        target = root / "data/engines/fairy-stockfish" / XQ_TAG
        manifest = json.loads((target / "INSTALL.json").read_text(encoding="utf-8"))
        files = [("fairy-stockfish", ASSETS["fairy-" + manifest["architecture"]][1])]
    else:
        target = root / "data/engines/katago" / GO_TAG
        manifest = json.loads((target / "INSTALL.json").read_text(encoding="utf-8"))
        files = [("release.zip", ASSETS["katago-" + manifest["architecture"]][1]),
                 (MODEL, ASSETS["model"][1])]
        import zipfile
        with zipfile.ZipFile(target / "release.zip") as archive:
            original = [name for name in archive.namelist() if Path(name).name == "katago"]
            if len(original) != 1:
                raise ValueError("Pinned archive has no unambiguous KataGo executable")
            files.append(("katago", hashlib.sha256(archive.read(original[0])).hexdigest()))
    for name, expected in files:
        with (target / name).open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != expected:
            raise ValueError(f"Checksum mismatch: {name}")
    print(f"{game}: installed release/model checksums verified")


def _probe(manager, module, game):
    if game == "xiangqi":
        import pyffish
        state = {"game": game, "initial_fen": module.XQ_FEN, "moves": [], "turn": 1}
        for difficulty in ("casual", "serious"):
            state["difficulty"] = difficulty
            start = time.monotonic()
            move = manager.choose(state)
            legal = pyffish.legal_moves("xiangqi", state["initial_fen"], [r["move"] for r in state["moves"]])
            if move not in legal:
                raise ValueError("Fairy-Stockfish returned an illegal move")
            print(f"PASS xiangqi {difficulty}: move={move}; {time.monotonic()-start:.2f}s")
            state["moves"].append({"side": state["turn"], "move": move})
            state["turn"] = 3 - state["turn"]
    else:
        for size in (9, 13, 19):
            for difficulty in ("casual", "serious"):
                state = {"game": "go", "board_size": size, "komi": 7.5,
                         "rules": "chinese-ogs", "moves": [], "turn": 1, "difficulty": difficulty}
                start = time.monotonic()
                move = manager.choose(state)
                if move != "pass" and not (type(move) is int and 0 <= move < size * size):
                    raise ValueError("KataGo returned an invalid opening")
                print(f"PASS go {size} {difficulty}: move={module.go_vertex(move, size)}; {time.monotonic()-start:.2f}s")
        resumed = {"game": "go", "board_size": 9, "turn": 1, "difficulty": "casual",
                   "moves": [{"side": 1, "move": 20}, {"side": 2, "move": 60},
                             {"side": 1, "move": "pass"}, {"side": 2, "move": "pass"}],
                   "resume_after": [4]}
        dead = manager.suggest_dead(resumed)
        if any(point not in (20, 60) for point in dead):
            raise ValueError("KataGo marked an empty point dead")
        move = manager.choose(resumed)
        if move in (20, 60):
            raise ValueError("KataGo chose an occupied point after resuming")
        print(f"PASS go dead-status and full-history resume: dead={dead}; move={module.go_vertex(move, 9)}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--config", type=Path, help="Defaults to ROOT/config.yaml")
    parser.add_argument("--game", choices=("all", "xiangqi", "go"), default="all")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--verify-files", action="store_true", help="Verify pinned install files before launching")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    manager = None
    try:
        config = yaml.safe_load((args.config or root / "config.yaml").read_text(encoding="utf-8")) or {}
        module = adapter(root)
        manager = module.BoardEngines(config.get("minigames") or {}, root)
        for game in (("xiangqi", "go") if args.game == "all" else (args.game,)):
            if args.verify_files:
                verify_installed(root, game)
            start = time.monotonic()
            manager.check_ready(game)
            print(f"ready {game}: {time.monotonic()-start:.2f}s")
            if args.self_test:
                _probe(manager, module, game)
            info = manager.diagnostics(game)
            print(f"engine={info.get('engine', 'unknown')}")
            for key in ("rss_bytes", "observed_peak_rss_bytes"):
                value = info.get(key)
                print(f"{key}={value / (1024*1024):.1f} MiB" if isinstance(value, int) else f"{key}=unknown")
        print("检查仅验证当前安装的协议、基础走法及观测资源，不代表已测得真人段位。请再在测试群实测聊天响应和完整对局。")
        return 0
    except Exception as exc:
        message = str(exc) if type(exc).__name__ in ("BoardEngineUnavailable", "ValueError") else type(exc).__name__
        print(f"FAIL: {message}")
        return 1
    finally:
        if manager is not None:
            manager.close()


if __name__ == "__main__":
    raise SystemExit(main())
