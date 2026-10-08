"""Harmless protocol subprocess for tests; not an actual chess engine."""
import argparse
import os
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument("--mode", default="normal")
parser.add_argument("--log", required=True)
args = parser.parse_args()
searches = 0
board = None

for raw in sys.stdin:
    line = raw.strip()
    with open(args.log, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    if line.startswith("START"):
        if args.mode == "startup_timeout":
            time.sleep(10)
        if args.mode == "startup_crash":
            os._exit(2)
        print("MESSAGE test engine ready", flush=True)
        print("OK", flush=True)
    elif line == "ABOUT":
        print('name="Rapfi", version="FAKE TEST ONLY", author="test"', flush=True)
    elif line == "RESTART":
        print("OK", flush=True)
    elif line == "BOARD":
        board = {}
    elif line == "DONE":
        searches += 1
        if args.mode == "probe_error":
            print("ERROR Failed to load evaluator", flush=True)
            continue
        if searches > 1:
            if args.mode == "timeout":
                time.sleep(10)
            elif args.mode == "crash":
                os._exit(3)
            elif args.mode == "overlong":
                print("x" * 20000, flush=True)
                continue
            elif args.mode == "stderr":
                for _ in range(300):
                    print("diagnostic" * 600, file=sys.stderr, flush=True)
            elif args.mode in {"malformed", "range", "occupied"}:
                print({"malformed": "SWAP", "range": "15,-1", "occupied": "7,7"}[args.mode], flush=True)
                continue
            elif args.mode == "slow":
                time.sleep(.2)
        index = next(i for i in range(225) if (i % 15, i // 15) not in board)
        print(f"{index % 15},{index // 15}", flush=True)
        board = None
    elif board is not None:
        x, y, color = map(int, line.split(","))
        board[x, y] = color
