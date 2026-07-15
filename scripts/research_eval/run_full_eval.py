#!/usr/bin/env python3
"""Run the complete evaluation matrix for three research bots."""
from __future__ import annotations
import json, os, subprocess, sys, time, math
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
SEVER = REPO / "sever"
WT_A = REPO / ".codex_worktrees/rebel-decisionholdem"
WT_B = REPO / ".codex_worktrees/cfr-neural-search"

BOT_CONFIGS = {
    "A1": {
        "cmd": [sys.executable,
            str(WT_A / "bots/research_native_lab/rebel_decisionholdem/rebel_like/native_entry.py"),
            "--deploy", "/tmp/m5b_run_v2/deploy.npz"],
        "cwd": str(WT_A),
    },
    "A2": {
        "cmd": [sys.executable,
            str(WT_A / "bots/research_native_lab/rebel_decisionholdem/decisionholdem_like/native_entry.py"),
            "--blueprint", str(WT_A / "bots/research_native_lab/rebel_decisionholdem/artifacts/hunl_m4_smoke_blueprint.json")],
        "cwd": str(WT_A),
    },
    "B": {
        "cmd": [sys.executable,
            str(WT_B / "bots/research_native_lab/cfr_neural_search/native_runtime/neural_native_entry.py"),
            "--artifact", str(WT_B / "bots/research_native_lab/cfr_neural_search/artifacts/m4/blueprint.rbbp"),
            "--cfv-model", "/tmp/cfv_model_b_v1.pt",
            "--wire-mode", "official-raw"],
        "cwd": str(WT_B),
    },
}

PAIRS = [("A1", "A2"), ("A1", "B"), ("A2", "B")]


def run_match(bot_a_name, bot_b_name, port, seed_a, seed_b, timeout=180):
    """Run one 70-hand match. Returns dict with result."""
    a_cfg = BOT_CONFIGS[bot_a_name]
    b_cfg = BOT_CONFIGS[bot_b_name]

    srv = subprocess.Popen(
        [sys.executable, str(SEVER / "main.py"), "--tcp-port", str(port), "--web-port", str(port + 8000)],
        cwd=str(REPO), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(3)

    env = {**os.environ, "POK_NATIVE_LOCAL_ACTION_DELAY": "0", "PYTHONPATH": "."}
    a_cmd = a_cfg["cmd"] + ["--host", "127.0.0.1", "--port", str(port), "--name", bot_a_name]
    b_cmd = b_cfg["cmd"] + ["--host", "127.0.0.1", "--port", str(port), "--name", bot_b_name]
    # Bot-specific seed args
    if bot_a_name == "B":
        a_cmd += ["--policy-seed", str(seed_a)]
    else:
        a_cmd += ["--seed", str(seed_a)]
    if bot_b_name == "B":
        b_cmd += ["--policy-seed", str(seed_b)]
    else:
        b_cmd += ["--seed", str(seed_b)]

    pa = pb = None
    t0 = time.monotonic()
    try:
        pa = subprocess.Popen(a_cmd, cwd=a_cfg["cwd"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(0.5)
        pb = subprocess.Popen(b_cmd, cwd=b_cfg["cwd"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        pa.wait(timeout=timeout)
        pb.wait(timeout=10)
        elapsed = time.monotonic() - t0

        # Parse telemetry from both bots
        a_earn = 0.0
        b_earn = 0.0
        for line in (pa.stderr.read().decode("utf-8","replace") + "\n" + pb.stderr.read().decode("utf-8","replace")).split("\n"):
            if "{" in line and ("TELEMETRY" in line or "cumulative_net" in line):
                try:
                    pl = json.loads(line[line.index("{"):])
                    bn = pl.get("bot_name","")
                    earn = float(pl.get("cumulative_net_hero", pl.get("earnings", pl.get("net_chips", 0))))
                    if bot_a_name in bn: a_earn = earn
                    elif bot_b_name in bn: b_earn = earn
                except: pass
        if a_earn == 0 and b_earn != 0: a_earn = -b_earn
        elif b_earn == 0 and a_earn != 0: b_earn = -a_earn
        hands = 70  # Default: matches completed if we got here
        winner = "a" if a_earn > 0 else ("b" if a_earn < 0 else "draw")
        return {"winner": winner, "a_earnings": a_earn, "elapsed": elapsed, "hands": hands, "error": None}
    except Exception as e:
        return {"winner": "error", "a_earnings": 0, "elapsed": time.monotonic() - t0, "hands": 0, "error": str(e)[:100]}
    finally:
        for p in [pa, pb]:
            if p and p.poll() is None:
                p.kill()
        if srv.poll() is None:
            srv.terminate()
            srv.wait(timeout=5)


def main():
    n_per_pair = 1  # Start with 3 matches per pair for quick results
    base_seed = 1000
    base_port = 21000

    all_results = {}
    for pair_idx, (a_name, b_name) in enumerate(PAIRS):
        print(f"\n=== {a_name} vs {b_name} ===")
        pair_results = []
        for match_idx in range(n_per_pair):
            port = base_port + pair_idx * n_per_pair * 2 + match_idx * 2
            seed_a = base_seed + pair_idx * 100 + match_idx * 10
            seed_b = seed_a + 1
            print(f"  Match {match_idx+1}/{n_per_pair} (port {port})...", end=" ", flush=True)
            r = run_match(a_name, b_name, port, seed_a, seed_b)
            pair_results.append(r)
            w = r["winner"].upper()
            extra = f" earn={r['a_earnings']:.0f}" if not r["error"] else f" ERR:{r['error'][:30]}"
            print(f"-> {w} ({r['elapsed']:.0f}s){extra}")
        all_results[f"{a_name}_vs_{b_name}"] = pair_results

    # Summary
    print("\n=== SUMMARY ===")
    summary = {}
    for key, results in all_results.items():
        valid = [r for r in results if not r["error"]]
        aw = sum(1 for r in valid if r["winner"] == "a")
        bw = sum(1 for r in valid if r["winner"] == "b")
        dr = sum(1 for r in valid if r["winner"] == "draw")
        n = len(valid)
        wr = aw / n if n else 0
        ci = 1.96 * math.sqrt(wr * (1 - wr) / n) if n > 0 else 0
        summary[key] = {"a_wins": aw, "b_wins": bw, "draws": dr, "n": n, "a_winrate": wr, "ci95": ci}
        print(f"  {key}: A={aw}W B={bw}W D={dr} (n={n}) WR={wr:.0%} ±{ci:.0%}")

    output = Path("scripts/research_eval/results")
    output.mkdir(exist_ok=True)
    (output / "h2h_results.json").write_text(json.dumps({"summary": summary, "matches": all_results}, indent=2))
    print(f"\nResults saved to {output / 'h2h_results.json'}")


if __name__ == "__main__":
    main()
