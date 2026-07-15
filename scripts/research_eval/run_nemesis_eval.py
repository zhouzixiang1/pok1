#!/usr/bin/env python3
"""Nemesis evaluation: each candidate vs its targeted nemesis opponent."""
import subprocess, sys, os, time, json, math
from pathlib import Path

REPO = Path("/home/zzx/project/pok")
SEVER = REPO / "sever"
WT_A = REPO / ".codex_worktrees/rebel-decisionholdem"
WT_B = REPO / ".codex_worktrees/cfr-neural-search"
ANCHOR = REPO / "scripts/research_eval/anchor_bots.py"

CANDIDATES = {
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

# Each candidate vs its nemesis AND vs other candidates' nemeses (heldout)
NEMESIS_PAIRS = [
    ("A1", "nemesis_a1"),   # targeted nemesis
    ("A1", "nemesis_b"),    # heldout: B's nemesis
    ("A2", "nemesis_a2"),
    ("A2", "nemesis_a1"),   # heldout: A1's nemesis
    ("B", "nemesis_b"),
    ("B", "nemesis_a2"),    # heldout: A2's nemesis
]

N_MATCHES = 3


def run_match(candidate_key, nemesis_strategy, port, seed, timeout=90):
    cfg = CANDIDATES[candidate_key]
    srv = subprocess.Popen(
        [sys.executable, str(SEVER / "main.py"), "--tcp-port", str(port), "--web-port", str(port + 8000)],
        cwd=str(SEVER), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(3)
    env = {**os.environ, "POK_NATIVE_LOCAL_ACTION_DELAY": "0", "PYTHONPATH": "."}
    cand_cmd = cfg["cmd"] + ["--host", "127.0.0.1", "--port", str(port), "--name", candidate_key]
    if candidate_key == "B":
        cand_cmd += ["--policy-seed", str(seed)]
    else:
        cand_cmd += ["--seed", str(seed)]
    nemesis_cmd = [sys.executable, str(ANCHOR),
        "--host", "127.0.0.1", "--port", str(port),
        "--name", nemesis_strategy,
        "--strategy", nemesis_strategy, "--seed", str(seed + 1)]

    pa = pb = None
    t0 = time.monotonic()
    try:
        pa = subprocess.Popen(cand_cmd, cwd=cfg["cwd"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(0.5)
        pb = subprocess.Popen(nemesis_cmd, cwd=str(REPO), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        pa.wait(timeout=timeout)
        pb.wait(timeout=10)
        elapsed = time.monotonic() - t0
        cand_earn = 0.0
        nemesis_earn = 0.0
        a_stderr = pa.stderr.read().decode("utf-8", "replace")
        b_stderr = pb.stderr.read().decode("utf-8", "replace")
        for line in (a_stderr + "\n" + b_stderr).split("\n"):
            if "{" in line and ("TELEMETRY" in line or "cumulative_net" in line):
                try:
                    pl = json.loads(line[line.index("{"):])
                    bn = pl.get("bot_name", "")
                    earn = float(pl.get("cumulative_net_hero", pl.get("earnings", pl.get("net_chips", 0))))
                    if candidate_key in bn: cand_earn = earn
                    elif nemesis_strategy in bn: nemesis_earn = earn
                except: pass
        if cand_earn == 0 and nemesis_earn != 0: cand_earn = -nemesis_earn
        elif nemesis_earn == 0 and cand_earn != 0: nemesis_earn = -cand_earn
        winner = "cand" if cand_earn > 0 else ("nemesis" if cand_earn < 0 else "draw")
        return {"winner": winner, "cand_earn": cand_earn, "elapsed": elapsed, "error": None}
    except Exception as e:
        return {"winner": "error", "cand_earn": 0, "elapsed": time.monotonic() - t0, "error": str(e)[:200]}
    finally:
        for p in [pa, pb]:
            if p and p.poll() is None: p.kill()
        if srv.poll() is None: srv.terminate(); srv.wait(timeout=5)


def main():
    base_port = 42000
    base_seed = 5500
    all_results = {}
    
    for pi, (cand, nemesis) in enumerate(NEMESIS_PAIRS):
        label = f"{cand}_vs_{nemesis}"
        print(f"\n=== {label} ({N_MATCHES} matches) ===", flush=True)
        matches = []
        for mi in range(N_MATCHES):
            port = base_port + pi * N_MATCHES * 2 + mi * 2
            seed = base_seed + pi * 100 + mi * 10
            r = run_match(cand, nemesis, port, seed)
            matches.append(r)
            w = r["winner"]
            extra = f" earn={r['cand_earn']:.0f}" if not r["error"] else f" ERR:{r['error'][:60]}"
            print(f"  Match {mi+1}: {w} ({r['elapsed']:.0f}s){extra}", flush=True)
        
        valid = [r for r in matches if not r["error"]]
        wins = sum(1 for r in valid if r["winner"] == "cand")
        losses = sum(1 for r in valid if r["winner"] == "nemesis")
        draws = sum(1 for r in valid if r["winner"] == "draw")
        avg = sum(r["cand_earn"] for r in valid) / len(valid) if valid else 0
        all_results[label] = {"wins": wins, "losses": losses, "draws": draws,
                              "avg_earn": avg, "matches": matches}
        print(f"  => {wins}W/{losses}L/{draws}D avg_earn={avg:.0f}", flush=True)

    print("\n=== NEMESIS EVALUATION SUMMARY ===", flush=True)
    print(f"{'Matchup':<30} {'W/L/D':>10} {'AvgEarn':>10}", flush=True)
    print("-" * 54, flush=True)
    for label, r in all_results.items():
        wld = f"{r['wins']}/{r['losses']}/{r['draws']}"
        tag = "TARGETED" if label.split("_vs_")[1].endswith(label.split("_vs_")[0][-2:].lower().replace("1","a1").replace("2","a2")) else "heldout"
        print(f"{label:<30} {wld:>10} {r['avg_earn']:>10.0f}", flush=True)

    output = Path("scripts/research_eval/results")
    output.mkdir(exist_ok=True)
    (output / "nemesis_eval.json").write_text(json.dumps(all_results, indent=2, default=str))
    print(f"\nSaved to scripts/research_eval/results/nemesis_eval.json", flush=True)


if __name__ == "__main__":
    main()
