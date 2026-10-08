"""Read-only readiness/tactical check using configured, preinstalled Rapfi.

Run from the project venv: python tools/check_rapfi.py --self-test
Explicit longer comparison: python tools/check_rapfi.py --benchmark
No config, DB, .env, downloaded binary, or game state is modified.
"""
import argparse
import importlib
from pathlib import Path
import sys
import time
import types

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _adapter():
    # Import only the pure engine module, not the NoneBot plugin initializer.
    name = "_rapfi_operator_check"
    package = types.ModuleType(name)
    package.__path__ = [str(ROOT / "plugins/minigames")]
    sys.modules[name] = package
    return importlib.import_module(name + ".rapfi")


def _cases():
    scattered = [32, 64, 96, 128, 160]
    for side, blocking in ((1, False), (2, False), (1, True), (2, True)):
        threat = 3-side if blocking else side
        board = [0] * 225
        for index in range(4):
            board[index] = threat
        counts = {1: 4, 2: 4} if side == 1 else ({1: 4, 2: 3} if threat == 1 else {1: 5, 2: 4})
        for index in scattered[:counts[3-threat]]:
            board[index] = 3-threat
        yield f"{'黑' if side == 1 else '白'}方{'防直接输棋' if blocking else '直接取胜'}", board, side, 4


def _openings():
    # Actual chronological alternating placements, not arbitrary colour counts.
    return (
        ("近身开局", (112, 127, 113, 111)),
        ("交错开局", (112, 128, 127, 97, 113, 126)),
    )


def _benchmark(engine, adapter):
    """Four bounded games, explicitly requested only; never a default check."""
    rules = importlib.import_module(adapter.__package__ + ".rules")
    ai = importlib.import_module(adapter.__package__ + ".ai")
    summary = {"win": 0, "loss": 0, "draw": 0, "unfinished": 0}
    timing = {"rapfi": [], "normal": []}
    games = []
    peak = None
    failed = False
    started = time.monotonic()
    print("benchmark: 2 套固定开局，双方轮换先后手共 4 局；娱乐旧 normal=1s，"
          f"Rapfi={engine.move_seconds:g}s；每局最多 120 步，预计需数分钟。")
    for opening, moves in _openings():
        for engine_side in (1, 2):
            board = rules.new_board("gomoku")
            for number, index in enumerate(moves):
                board = rules.play_move("gomoku", board, index, number % 2 + 1)
            total_plies = len(moves)
            next_side = total_plies % 2 + 1
            winner = rules.outcome("gomoku", board)[0]
            error = None
            game_id = len(games) + 1
            print(f"game={game_id} opening={opening} Rapfi={'黑/先手' if engine_side == 1 else '白/后手'}")
            while not winner and total_plies < 120:
                role = "rapfi" if next_side == engine_side else "normal"
                step_start = time.monotonic()
                try:
                    if role == "rapfi":
                        index = engine.choose_move(board, next_side)
                    else:
                        index = ai.choose_move("gomoku", board, next_side, "normal", 1.0)
                    board = rules.play_move("gomoku", board, index, next_side)
                except Exception as exc:
                    # Keep the failed partial game, but do not keep stressing a
                    # broken installation for the remaining planned games.
                    error = str(exc) if isinstance(exc, adapter.RapfiUnavailable) else type(exc).__name__
                    failed = True
                    print(f"game={game_id} role={role} error={error}")
                    break
                elapsed = time.monotonic() - step_start
                timing[role].append(elapsed)
                total_plies += 1
                print(f"game={game_id} ply={total_plies} role={role} "
                      f"move={rules.format_move('gomoku', index)} elapsed={elapsed:.3f}s")
                observed = engine.diagnostics().get("peak_rss_kib")
                if isinstance(observed, int):
                    peak = observed if peak is None else max(peak, observed)
                winner = rules.outcome("gomoku", board)[0]
                next_side = 3 - next_side
            if winner == -1:
                result = "draw"
            elif winner == engine_side:
                result = "win"
            elif winner in (1, 2):
                result = "loss"
            else:
                result = "unfinished"
            summary[result] += 1
            games.append({"opening": opening, "engine_side": engine_side, "plies": total_plies,
                          "result": result, "error": error})
            print(f"game={game_id} result={result} plies={total_plies}")
            if failed:
                break
        if failed:
            break
    for role, values in timing.items():
        if values:
            print(f"timing {role}: moves={len(values)} mean={sum(values)/len(values):.3f}s max={max(values):.3f}s")
        else:
            print(f"timing {role}: 未知（无成功落子）")
    memory = f"{peak / 1024:.1f} MiB" if peak is not None else "未知"
    print(f"memory: 所读 Rapfi 子进程 VmHWM 最大值={memory}，不是哈希表大小或 Python 进程内存")
    print(f"summary: 胜={summary['win']} 负={summary['loss']} 真平={summary['draw']} "
          f"未完成={summary['unfinished']} 未开始={4-len(games)} elapsed={time.monotonic()-started:.2f}s")
    print("120 步截断只记未完成；4 局仅供本机对比，不代表已测得人类段位。")
    return {"summary": summary, "games": games, "timing": timing, "peak_rss_kib": peak, "failed": failed}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true", help="额外运行四个黑白方必胜/必防战术局面")
    parser.add_argument("--benchmark", action="store_true", help="显式运行轮换先后手的 4 局棋力及资源对比，可能耗时数分钟")
    args = parser.parse_args(argv)
    engine = None
    adapter = _adapter()
    try:
        document = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
        config = (document.get("minigames") or {}).get("rapfi") or {}
        engine = adapter.RapfiEngine(config, ROOT)
        start = time.monotonic()
        engine.check_ready()
        print(f"ready: {time.monotonic() - start:.2f}s; protocol=250615")
        print(f"engine: {engine.engine_info or '版本未知'}")
        print("resources: thread=1; hash=64 MiB（非进程总内存）; ponder=off")
        passed = True
        if args.self_test:
            for name, board, side, expected in _cases():
                start = time.monotonic()
                move = engine.choose_move(board, side)
                good = move == expected
                passed &= good
                print(f"{'PASS' if good else 'FAIL'} {name}: {chr(65+move%15)}{move//15+1}; "
                      f"{time.monotonic()-start:.2f}s")
            print("这些检查只验证协议与基础战术，不代表已测得人类段位。")
        if args.benchmark:
            report = _benchmark(engine, adapter)
            passed &= not report["failed"]
        return 0 if passed else 1
    except adapter.RapfiUnavailable as exc:
        print(f"RapfiUnavailable: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"检查失败：{type(exc).__name__}（未输出配置或凭据）", file=sys.stderr)
        return 2
    finally:
        if engine is not None:
            engine.close()


if __name__ == "__main__":
    raise SystemExit(main())
