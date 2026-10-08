"""Test-only UCI/GTP child. Never used by the production loader."""
import argparse
from pathlib import Path
import sys
import time

p = argparse.ArgumentParser()
p.add_argument("--protocol", choices=("uci", "gtp"), required=True)
p.add_argument("--mode", default="normal")
p.add_argument("--log", required=True)
a = p.parse_args()
searches = 0


def out(value):
    print(value, flush=True)


for raw in sys.stdin:
    line = raw.strip()
    with Path(a.log).open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    if a.mode == "startup_hang":
        time.sleep(60)
    if a.mode == "huge_line":
        out("X" * 20000)
        continue
    if a.protocol == "uci":
        if line == "uci":
            out("id name Fairy-Stockfish FAKE TEST ONLY")
            for name in ("Threads", "Hash", "Ponder", "Skill Level", "Use NNUE"):
                out(f"option name {name} type spin default 1")
            out("option name UCI_Variant type combo default chess var xiangqi")
            out("option name EvalFile type string default " +
                ("wrong.nnue" if a.mode == "wrong_network" else "xiangqi-c07e94a5c7cb.nnue"))
            out("uciok")
        elif line == "isready":
            out("readyok")
        elif line.startswith("go "):
            searches += 1
            if a.mode == "search_hang" and searches > 1:
                time.sleep(60)
            if a.mode == "crash" and searches > 1:
                sys.exit(2)
            out("info depth 1 score cp 0 nodes 1")
            out("bestmove z99z99" if a.mode == "bad_move" and searches > 1 else "bestmove h3e3")
    else:
        req, command = line.split(" ", 1)
        result = ""
        if command == "protocol_version":
            result = "2"
        elif command == "name":
            result = "KataGo"
        elif command == "version":
            result = "9.9" if a.mode == "bad_version" else "1.16.3+g170-b10c128-s1141M"
        elif command.startswith("kata-search "):
            searches += 1
            if a.mode == "search_hang" and searches > 1:
                time.sleep(60)
            if a.mode == "crash" and searches > 1:
                sys.exit(2)
            result = "I4" if a.mode == "bad_move" and searches > 1 else "D4"
            if a.mode == "pass" and searches > 1:
                result = "pass"
        elif command == "final_status_list dead":
            result = "C3 D3\nA1"
        if a.mode == "bad_id":
            req = str(int(req) + 1)
        out("=" + req + (" " + result if result else "") + "\n")
