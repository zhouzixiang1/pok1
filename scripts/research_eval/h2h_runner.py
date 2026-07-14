#!/usr/bin/env python3
"""H2H evaluation harness for research bots."""
from __future__ import annotations
import argparse, json, os, subprocess, sys, time, math
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parent.parent.parent
SEVER = REPO / "sever"

@dataclass
class MatchResult:
    match_id: int = 0
    a_earnings: float = 0.0
    duration: float = 0.0
    error: str = None
    @property
    def winner(self):
        if self.error: return "error"
        return "a" if self.a_earnings > 0 else ("b" if self.a_earnings < 0 else "draw")

def run_match(a_cmd, a_cwd, b_cmd, b_cwd, port, timeout=300):
    srv = subprocess.Popen(
        [sys.executable, str(SEVER/"main.py"), "--tcp-port", str(port), "--web-port", str(port+8000)],
        cwd=str(SEVER), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(3)
    env = {**os.environ, "POK_NATIVE_LOCAL_ACTION_DELAY": "0"}
    pa = pb = None
    t0 = time.monotonic()
    try:
        pa = subprocess.Popen(a_cmd, cwd=a_cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(0.5)
        pb = subprocess.Popen(b_cmd, cwd=b_cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        pa.wait(timeout=timeout)
        pb.wait(timeout=timeout)
        r = MatchResult(duration=time.monotonic()-t0)
        all_stderr = pa.stderr.read().decode("utf-8","replace") + "\n" + pb.stderr.read().decode("utf-8","replace")
        for line in all_stderr.split("\n"):
            if "TELEMETRY" in line and "{" in line:
                try:
                    p = json.loads(line[line.index("{"):])
                    r.a_earnings = float(p.get("earnings", p.get("net_chips", 0)))
                except: pass
        return r
    except Exception as e:
        return MatchResult(error=str(e)[:100])
    finally:
        for p in [pa, pb]:
            if p and p.poll() is None: p.kill()
        if srv.poll() is None: srv.terminate(); srv.wait(timeout=5)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bot-a-entry", required=True)
    ap.add_argument("--bot-a-args", default="")
    ap.add_argument("--bot-a-cwd", required=True)
    ap.add_argument("--bot-b-entry", required=True)
    ap.add_argument("--bot-b-args", default="")
    ap.add_argument("--bot-b-cwd", required=True)
    ap.add_argument("--n-matches", type=int, default=10)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--base-port", type=int, default=20001)
    ap.add_argument("--timeout", type=float, default=300)
    ap.add_argument("--output", default=None)
    args = ap.parse_args()
    aa = args.bot_a_args.split() if args.bot_a_args else []
    ba = args.bot_b_args.split() if args.bot_b_args else []
    a_cmd = [sys.executable, args.bot_a_entry, "--host", "127.0.0.1", "--port", "PORT", "--name", "BotA"] + aa + ["--seed", "SEED"]
    b_cmd = [sys.executable, args.bot_b_entry, "--host", "127.0.0.1", "--port", "PORT", "--name", "BotB"] + ba + ["--seed", "SEED"]
    print(f"H2H: {Path(args.bot_a_entry).stem} vs {Path(args.bot_b_entry).stem}")
    results = []
    for i in range(args.n_matches):
        port = args.base_port + i * 2
        seed = args.seed + i * 100
        ac = [x.replace("PORT", str(port)).replace("SEED", str(seed)) for x in a_cmd]
        bc = [x.replace("PORT", str(port)).replace("SEED", str(seed+1)) for x in b_cmd]
        print(f"  Match {i+1}/{args.n_matches}...", end=" ", flush=True)
        r = run_match(ac, args.bot_a_cwd, bc, args.bot_b_cwd, port, args.timeout)
        r.match_id = i
        results.append(r)
        w = r.winner.upper()
        e = f" ERR:{r.error[:40]}" if r.error else ""
        print(f"{w} ({r.duration:.0f}s){e}")
    valid = [r for r in results if not r.error]
    aw = sum(1 for r in valid if r.winner == "a")
    bw = sum(1 for r in valid if r.winner == "b")
    dr = sum(1 for r in valid if r.winner == "draw")
    n = len(valid)
    wr = aw/n if n else 0
    ci = 1.96*math.sqrt(wr*(1-wr)/n) if n else 0
    print(f"\nA={aw}W B={bw}W D={dr} (n={n})")
    print(f"A win rate: {wr:.1%} +/-{ci:.1%}")
    if args.output:
        Path(args.output).write_text(json.dumps({"n":n,"a_wins":aw,"b_wins":bw,"draws":dr,"a_wr":wr,"ci95":ci,"results":[{"id":r.match_id,"w":r.winner,"earn":r.a_earnings,"err":r.error} for r in results]}, indent=2))

if __name__ == "__main__":
    main()
