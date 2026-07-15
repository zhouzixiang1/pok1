#!/usr/bin/env python3
"""Quick re-evaluation with enhanced bots: A1(range update) vs A2, A1 vs B(opp tracker), A2 vs B."""
import subprocess, sys, os, time, json, math
from pathlib import Path

REPO = Path("/home/zzx/project/pok")
SEVER = REPO / "sever"
WT_A = REPO / ".codex_worktrees/rebel-decisionholdem"
WT_B = REPO / ".codex_worktrees/cfr-neural-search"

BOTS = {
    "A1": {"cmd": [sys.executable,
        str(WT_A / "bots/research_native_lab/rebel_decisionholdem/rebel_like/native_entry.py"),
        "--deploy", "/tmp/m5b_run_v2/deploy.npz"], "cwd": str(WT_A)},
    "A2": {"cmd": [sys.executable,
        str(WT_A / "bots/research_native_lab/rebel_decisionholdem/decisionholdem_like/native_entry.py"),
        "--blueprint", str(WT_A / "bots/research_native_lab/rebel_decisionholdem/artifacts/hunl_m4_smoke_blueprint.json")],
        "cwd": str(WT_A)},
    "B": {"cmd": [sys.executable,
        str(WT_B / "bots/research_native_lab/cfr_neural_search/native_runtime/neural_native_entry.py"),
        "--artifact", str(WT_B / "bots/research_native_lab/cfr_neural_search/artifacts/m4/blueprint.rbbp"),
        "--cfv-model", "/tmp/cfv_model_b_v1.pt", "--wire-mode", "official-raw"],
        "cwd": str(WT_B)},
}

PAIRS = [("A1", "A2"), ("A1", "B"), ("A2", "B")]

def run_match(a_name, b_name, port, seed_a, seed_b, timeout=90):
    srv = subprocess.Popen(
        [sys.executable, str(SEVER / "main.py"), "--tcp-port", str(port), "--web-port", str(port + 8000)],
        cwd=str(SEVER), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(3)
    env = {**os.environ, "POK_NATIVE_LOCAL_ACTION_DELAY": "0", "PYTHONPATH": "."}
    a_cmd = BOTS[a_name]["cmd"] + ["--host", "127.0.0.1", "--port", str(port), "--name", a_name]
    b_cmd = BOTS[b_name]["cmd"] + ["--host", "127.0.0.1", "--port", str(port), "--name", b_name]
    if a_name == "B": a_cmd += ["--policy-seed", str(seed_a)]
    else: a_cmd += ["--seed", str(seed_a)]
    if b_name == "B": b_cmd += ["--policy-seed", str(seed_b)]
    else: b_cmd += ["--seed", str(seed_b)]
    pa = pb = None
    t0 = time.monotonic()
    try:
        pa = subprocess.Popen(a_cmd, cwd=BOTS[a_name]["cwd"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(0.5)
        pb = subprocess.Popen(b_cmd, cwd=BOTS[b_name]["cwd"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        pa.wait(timeout=timeout)
        pb.wait(timeout=10)
        elapsed = time.monotonic() - t0
        a_earn = 0.0; b_earn = 0.0
        for line in (pa.stderr.read().decode("utf-8","replace") + "\n" + pb.stderr.read().decode("utf-8","replace")).split("\n"):
            if "{" in line and ("TELEMETRY" in line or "cumulative_net" in line):
                try:
                    pl = json.loads(line[line.index("{"):])
                    bn = pl.get("bot_name","")
                    earn = float(pl.get("cumulative_net_hero", pl.get("earnings", pl.get("net_chips", 0))))
                    if a_name in bn: a_earn = earn
                    elif b_name in bn: b_earn = earn
                except: pass
        if a_earn == 0 and b_earn != 0: a_earn = -b_earn
        elif b_earn == 0 and a_earn != 0: b_earn = -a_earn
        winner = "a" if a_earn > 0 else ("b" if a_earn < 0 else "draw")
        return {"winner": winner, "a_earnings": a_earn, "elapsed": elapsed, "error": None}
    except Exception as e:
        return {"winner": "error", "a_earnings": 0, "elapsed": time.monotonic() - t0, "error": str(e)[:200]}
    finally:
        for p in [pa, pb]:
            if p and p.poll() is None: p.kill()
        if srv.poll() is None: srv.terminate(); srv.wait(timeout=5)

def main():
    N = 5
    base_seed = 9000
    base_port = 40000
    all_results = {}
    for pi, (a, b) in enumerate(PAIRS):
        print(f"\n=== {a} vs {b} (enhanced) ===", flush=True)
        results = []
        for mi in range(N):
            port = base_port + pi * N * 2 + mi * 2
            sa = base_seed + pi * 100 + mi * 10
            sb = sa + 1
            print(f"  Match {mi+1}/{N}...", end=" ", flush=True)
            r = run_match(a, b, port, sa, sb)
            results.append(r)
            w = r["winner"].upper()
            extra = f" earn={r['a_earnings']:.0f}" if not r["error"] else f" ERR:{r['error'][:60]}"
            print(f"-> {w} ({r['elapsed']:.0f}s){extra}", flush=True)
        valid = [r for r in results if not r["error"]]
        aw = sum(1 for r in valid if r["winner"] == "a")
        bw = sum(1 for r in valid if r["winner"] == "b")
        dr = sum(1 for r in valid if r["winner"] == "draw")
        avg = sum(r["a_earnings"] for r in valid) / len(valid) if valid else 0
        wr = aw / len(valid) if valid else 0
        ci = 1.96 * math.sqrt(wr * (1 - wr) / len(valid)) if valid else 0
        all_results[f"{a}_vs_{b}"] = {"a_wins": aw, "b_wins": bw, "draws": dr, "n": len(valid), "avg_earn": avg, "wr": wr, "ci95": ci}
        print(f"  => {a}={aw}W {b}={bw}W D={dr} avg={avg:.0f} WR={wr:.0%}+/-{ci:.0%}", flush=True)

    print("\n=== ENHANCED H2H SUMMARY ===", flush=True)
    for k, r in all_results.items():
        print(f"  {k}: A={r['a_wins']}W B={r['b_wins']}W D={r['draws']} avg={r['avg_earn']:.0f} WR={r['wr']:.0%}+/-{r['ci95']:.0%}", flush=True)

    output = Path("scripts/research_eval/results")
    output.mkdir(exist_ok=True)
    (output / "enhanced_h2h.json").write_text(json.dumps(all_results, indent=2))
    print(f"\nSaved to scripts/research_eval/results/enhanced_h2h.json", flush=True)

if __name__ == "__main__":
    main()
