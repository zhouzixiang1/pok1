#!/usr/bin/env python3
"""Time budget evaluation: test each bot at 250ms, 5s, 20s, 50s per-decision limits.

Each bot's POK_DECISION_HARD_DEADLINE_SEC controls how long it can think.
At 250ms, bots that need network inference fall back to fast heuristics.
At 50s, all computation paths are available.
"""
import subprocess, sys, os, time, json
from pathlib import Path

REPO = Path("/home/zzx/project/pok")
SEVER = REPO / "sever"
WT_A = REPO / ".codex_worktrees/rebel-decisionholdem"
WT_B = REPO / ".codex_worktrees/cfr-neural-search"

# Bots with configurable deadline
BOTS = {
    "A1": {
        "cmd": [sys.executable,
            str(WT_A / "bots/research_native_lab/rebel_decisionholdem/rebel_like/native_entry.py"),
            "--deploy", "/tmp/m5b_run_v2/deploy.npz"],
        "cwd": str(WT_A),
        "deadline_env": "POK_DECISION_HARD_DEADLINE_SEC",
    },
    "A2": {
        "cmd": [sys.executable,
            str(WT_A / "bots/research_native_lab/rebel_decisionholdem/decisionholdem_like/native_entry.py"),
            "--blueprint", str(WT_A / "bots/research_native_lab/rebel_decisionholdem/artifacts/hunl_m4_smoke_blueprint.json")],
        "cwd": str(WT_A),
        "deadline_env": "POK_DECISION_HARD_DEADLINE_SEC",
    },
    "B": {
        "cmd": [sys.executable,
            str(WT_B / "bots/research_native_lab/cfr_neural_search/native_runtime/neural_native_entry.py"),
            "--artifact", str(WT_B / "bots/research_native_lab/cfr_neural_search/artifacts/m4/blueprint.rbbp"),
            "--cfv-model", "/tmp/cfv_model_b_v1.pt",
            "--wire-mode", "official-raw"],
        "cwd": str(WT_B),
        "deadline_env": None,  # B's deadline is internal to socket_client
    },
}

TIME_BUDGETS = [0.25, 5.0, 20.0, 50.0]
N_MATCHES = 2


def run_match(bot_key, deadline_sec, port, seed, timeout=120):
    cfg = BOTS[bot_key]
    srv = subprocess.Popen(
        [sys.executable, str(SEVER / "main.py"), "--tcp-port", str(port), "--web-port", str(port + 8000)],
        cwd=str(SEVER), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(3)
    
    env = {**os.environ, "POK_NATIVE_LOCAL_ACTION_DELAY": "0", "PYTHONPATH": "."}
    if cfg["deadline_env"]:
        env[cfg["deadline_env"]] = str(deadline_sec)
    
    bot_cmd = cfg["cmd"] + ["--host", "127.0.0.1", "--port", str(port), "--name", bot_key]
    if bot_key == "B":
        bot_cmd += ["--policy-seed", str(seed)]
    else:
        bot_cmd += ["--seed", str(seed)]
    test_cmd = [sys.executable, str(SEVER / "test_client.py"), "127.0.0.1", str(port), "TestClient"]

    pa = pb = None
    t0 = time.monotonic()
    try:
        pa = subprocess.Popen(bot_cmd, cwd=cfg["cwd"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(0.5)
        pb = subprocess.Popen(test_cmd, cwd=str(REPO), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        pa.wait(timeout=timeout)
        pb.wait(timeout=10)
        elapsed = time.monotonic() - t0
        
        a_earn = 0.0
        stderr = pa.stderr.read().decode("utf-8", "replace")
        decisions = 0
        for line in stderr.split("\n"):
            if "{" in line and ("TELEMETRY" in line or "cumulative_net" in line):
                try:
                    pl = json.loads(line[line.index("{"):])
                    a_earn = float(pl.get("cumulative_net_hero", pl.get("earnings", pl.get("net_chips", 0))))
                    decisions = pl.get("decisions", 0)
                except: pass
        winner = "win" if a_earn > 0 else ("loss" if a_earn < 0 else "draw")
        return {"winner": winner, "earnings": a_earn, "elapsed": elapsed, "decisions": decisions, "error": None}
    except Exception as e:
        return {"winner": "error", "earnings": 0, "elapsed": time.monotonic() - t0, "decisions": 0, "error": str(e)[:200]}
    finally:
        for p in [pa, pb]:
            if p and p.poll() is None:
                p.kill()
        if srv.poll() is None:
            srv.terminate()
            srv.wait(timeout=5)


def main():
    base_port = 35000
    base_seed = 7000
    all_results = {}
    
    for bot_key in BOTS:
        print(f"\n=== {bot_key} time budget sweep ===", flush=True)
        all_results[bot_key] = {}
        for budget in TIME_BUDGETS:
            budget_label = f"{budget}s"
            print(f"  Budget {budget_label}:", flush=True)
            matches = []
            for m in range(N_MATCHES):
                port = base_port + hash(f"{bot_key}_{budget}_{m}") % 1000
                seed = base_seed + int(budget * 100) + m * 10
                r = run_match(bot_key, budget, port, seed)
                matches.append(r)
                w = r["winner"].upper()
                extra = f" earn={r['earnings']:.0f} dec={r['decisions']}" if not r["error"] else f" ERR:{r['error'][:60]}"
                print(f"    Match {m+1}: {w} ({r['elapsed']:.0f}s){extra}", flush=True)
            
            valid = [r for r in matches if not r["error"]]
            wins = sum(1 for r in valid if r["winner"] == "win")
            losses = sum(1 for r in valid if r["winner"] == "loss")
            draws = sum(1 for r in valid if r["winner"] == "draw")
            avg_earn = sum(r["earnings"] for r in valid) / len(valid) if valid else 0
            avg_wall = sum(r["elapsed"] for r in valid) / len(valid) if valid else 0
            all_results[bot_key][budget_label] = {
                "wins": wins, "losses": losses, "draws": draws,
                "avg_earn": avg_earn, "avg_wall_s": avg_wall,
                "matches": matches,
            }
            print(f"    => {wins}W/{losses}L/{draws}D avg_earn={avg_earn:.0f} avg_wall={avg_wall:.0f}s", flush=True)

    print("\n=== TIME BUDGET SUMMARY ===", flush=True)
    print(f"{'Bot':<6} {'Budget':>8} {'W/L/D':>10} {'AvgEarn':>10} {'AvgWall':>8}", flush=True)
    print("-" * 48, flush=True)
    for bot_key in BOTS:
        for budget in [f"{b}s" for b in TIME_BUDGETS]:
            r = all_results[bot_key][budget]
            wld = f"{r['wins']}/{r['losses']}/{r['draws']}"
            print(f"{bot_key:<6} {budget:>8} {wld:>10} {r['avg_earn']:>10.0f} {r['avg_wall_s']:>8.0f}s", flush=True)

    output = Path("scripts/research_eval/results")
    output.mkdir(exist_ok=True)
    (output / "time_budget.json").write_text(json.dumps(all_results, indent=2, default=str))
    print(f"\nSaved to scripts/research_eval/results/time_budget.json", flush=True)


if __name__ == "__main__":
    main()
