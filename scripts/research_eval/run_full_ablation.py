#!/usr/bin/env python3
"""Full component ablation: all three bots with core components on/off."""
import subprocess, sys, os, time, json, math
from pathlib import Path

REPO = Path("/home/zzx/project/pok")
SEVER = REPO / "sever"
WT_A = REPO / ".codex_worktrees/rebel-decisionholdem"
WT_B = REPO / ".codex_worktrees/cfr-neural-search"

# All bot variants
VARIANTS = {
    "A1_net": {
        "cmd": [sys.executable,
            str(WT_A / "bots/research_native_lab/rebel_decisionholdem/rebel_like/native_entry.py"),
            "--deploy", "/tmp/m5b_run_v2/deploy.npz"],
        "cwd": str(WT_A),
        "extra_env": {},
    },
    "A1_heuristic": {
        "cmd": [sys.executable,
            str(WT_A / "bots/research_native_lab/rebel_decisionholdem/rebel_like/native_entry.py"),
            "--deploy", "/nonexistent/path.npz"],
        "cwd": str(WT_A),
        "extra_env": {},
    },
    "A2_resolve_on": {
        "cmd": [sys.executable,
            str(WT_A / "bots/research_native_lab/rebel_decisionholdem/decisionholdem_like/native_entry.py"),
            "--blueprint", str(WT_A / "bots/research_native_lab/rebel_decisionholdem/artifacts/hunl_m4_smoke_blueprint.json")],
        "cwd": str(WT_A),
        "extra_env": {"A2_RESOLVE": "1"},
    },
    "A2_resolve_off": {
        "cmd": [sys.executable,
            str(WT_A / "bots/research_native_lab/rebel_decisionholdem/decisionholdem_like/native_entry.py"),
            "--blueprint", str(WT_A / "bots/research_native_lab/rebel_decisionholdem/artifacts/hunl_m4_smoke_blueprint.json")],
        "cwd": str(WT_A),
        "extra_env": {"A2_RESOLVE": "0"},
    },
    "B_cfv": {
        "cmd": [sys.executable,
            str(WT_B / "bots/research_native_lab/cfr_neural_search/native_runtime/neural_native_entry.py"),
            "--artifact", str(WT_B / "bots/research_native_lab/cfr_neural_search/artifacts/m4/blueprint.rbbp"),
            "--cfv-model", "/tmp/cfv_model_b_v1.pt",
            "--wire-mode", "official-raw"],
        "cwd": str(WT_B),
        "extra_env": {},
    },
    "B_blueprint": {
        "cmd": [sys.executable,
            str(WT_B / "bots/research_native_lab/cfr_neural_search/native_runtime/neural_native_entry.py"),
            "--artifact", str(WT_B / "bots/research_native_lab/cfr_neural_search/artifacts/m4/blueprint.rbbp"),
            "--wire-mode", "official-raw"],
        "cwd": str(WT_B),
        "extra_env": {},
    },
}

# Ablation pairs: each variant vs the standard test_client and vs each other
ABLATION_PAIRS = [
    # Component on/off against test_client
    ("A1_net", "test_client"),
    ("A1_heuristic", "test_client"),
    ("A2_resolve_on", "test_client"),
    ("A2_resolve_off", "test_client"),
    ("B_cfv", "test_client"),
    ("B_blueprint", "test_client"),
    # Component ablation direct H2H
    ("A1_net", "A1_heuristic"),
    ("A2_resolve_on", "A2_resolve_off"),
    ("B_cfv", "B_blueprint"),
]

N_MATCHES = 3


def run_match(bot_a_key, bot_b_key, port, seed_a, seed_b, timeout=90):
    if bot_b_key == "test_client":
        return run_vs_testclient(bot_a_key, port, seed_a, timeout)
    
    a_cfg = VARIANTS[bot_a_key]
    b_cfg = VARIANTS[bot_b_key]
    srv = subprocess.Popen(
        [sys.executable, str(SEVER / "main.py"), "--tcp-port", str(port), "--web-port", str(port + 8000)],
        cwd=str(SEVER), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(3)
    env = {**os.environ, "POK_NATIVE_LOCAL_ACTION_DELAY": "0", "PYTHONPATH": "."}
    env.update(a_cfg.get("extra_env", {}))
    b_env = {**os.environ, "POK_NATIVE_LOCAL_ACTION_DELAY": "0", "PYTHONPATH": "."}
    b_env.update(b_cfg.get("extra_env", {}))
    
    a_cmd = a_cfg["cmd"] + ["--host", "127.0.0.1", "--port", str(port), "--name", bot_a_key]
    b_cmd = b_cfg["cmd"] + ["--host", "127.0.0.1", "--port", str(port), "--name", bot_b_key]
    if "B" in bot_a_key:
        a_cmd += ["--policy-seed", str(seed_a)]
    else:
        a_cmd += ["--seed", str(seed_a)]
    if "B" in bot_b_key:
        b_cmd += ["--policy-seed", str(seed_b)]
    else:
        b_cmd += ["--seed", str(seed_b)]

    pa = pb = None
    t0 = time.monotonic()
    try:
        pa = subprocess.Popen(a_cmd, cwd=a_cfg["cwd"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(0.5)
        pb = subprocess.Popen(b_cmd, cwd=b_cfg["cwd"], env=b_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        pa.wait(timeout=timeout)
        pb.wait(timeout=10)
        elapsed = time.monotonic() - t0
        a_earn = 0.0
        b_earn = 0.0
        a_stderr = pa.stderr.read().decode("utf-8", "replace")
        b_stderr = pb.stderr.read().decode("utf-8", "replace")
        for line in (a_stderr + "\n" + b_stderr).split("\n"):
            if "{" in line and ("TELEMETRY" in line or "cumulative_net" in line):
                try:
                    pl = json.loads(line[line.index("{"):])
                    bn = pl.get("bot_name", "")
                    earn = float(pl.get("cumulative_net_hero", pl.get("earnings", pl.get("net_chips", 0))))
                    if bot_a_key in bn: a_earn = earn
                    elif bot_b_key in bn: b_earn = earn
                except: pass
        if a_earn == 0 and b_earn != 0: a_earn = -b_earn
        elif b_earn == 0 and a_earn != 0: b_earn = -a_earn
        winner = "a" if a_earn > 0 else ("b" if a_earn < 0 else "draw")
        return {"winner": winner, "a_earnings": a_earn, "elapsed": elapsed, "error": None}
    except Exception as e:
        return {"winner": "error", "a_earnings": 0, "elapsed": time.monotonic() - t0, "error": str(e)[:200]}
    finally:
        for p in [pa, pb]:
            if p and p.poll() is None:
                p.kill()
        if srv.poll() is None:
            srv.terminate()
            srv.wait(timeout=5)


def run_vs_testclient(bot_key, port, seed, timeout=90):
    cfg = VARIANTS[bot_key]
    srv = subprocess.Popen(
        [sys.executable, str(SEVER / "main.py"), "--tcp-port", str(port), "--web-port", str(port + 8000)],
        cwd=str(SEVER), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(3)
    env = {**os.environ, "POK_NATIVE_LOCAL_ACTION_DELAY": "0", "PYTHONPATH": "."}
    env.update(cfg.get("extra_env", {}))
    bot_cmd = cfg["cmd"] + ["--host", "127.0.0.1", "--port", str(port), "--name", bot_key]
    if "B" in bot_key:
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
        a_stderr = pa.stderr.read().decode("utf-8", "replace")
        for line in a_stderr.split("\n"):
            if "{" in line and ("TELEMETRY" in line or "cumulative_net" in line):
                try:
                    pl = json.loads(line[line.index("{"):])
                    earn = float(pl.get("cumulative_net_hero", pl.get("earnings", pl.get("net_chips", 0))))
                    a_earn = earn
                except: pass
        winner = "a" if a_earn > 0 else ("b" if a_earn < 0 else "draw")
        return {"winner": winner, "a_earnings": a_earn, "elapsed": elapsed, "error": None}
    except Exception as e:
        return {"winner": "error", "a_earnings": 0, "elapsed": time.monotonic() - t0, "error": str(e)[:200]}
    finally:
        for p in [pa, pb]:
            if p and p.poll() is None:
                p.kill()
        if srv.poll() is None:
            srv.terminate()
            srv.wait(timeout=5)


def main():
    base_seed = 6000
    base_port = 31000
    all_results = {}
    
    for pair_idx, (a_key, b_key) in enumerate(ABLATION_PAIRS):
        label = f"{a_key}_vs_{b_key}"
        print(f"\n=== {label} ({N_MATCHES} matches) ===", flush=True)
        results = []
        for m in range(N_MATCHES):
            port = base_port + pair_idx * N_MATCHES * 2 + m * 2
            seed_a = base_seed + pair_idx * 100 + m * 10
            seed_b = seed_a + 1
            print(f"  Match {m+1}/{N_MATCHES}...", end=" ", flush=True)
            r = run_match(a_key, b_key, port, seed_a, seed_b)
            results.append(r)
            w = r["winner"].upper()
            extra = f" earn={r['a_earnings']:.0f}" if not r["error"] else f" ERR:{r['error'][:60]}"
            print(f"-> {w} ({r['elapsed']:.0f}s){extra}", flush=True)
        
        valid = [r for r in results if not r["error"]]
        aw = sum(1 for r in valid if r["winner"] == "a")
        bw = sum(1 for r in valid if r["winner"] == "b")
        dr = sum(1 for r in valid if r["winner"] == "draw")
        avg = sum(r["a_earnings"] for r in valid) / len(valid) if valid else 0
        all_results[label] = {"a_wins": aw, "b_wins": bw, "draws": dr, "n": len(valid), "avg_earn": avg, "matches": results}
        print(f"  => A={aw}W B={bw}W D={dr} avg_earn={avg:.0f}", flush=True)

    print("\n=== FULL ABLATION SUMMARY ===", flush=True)
    print(f"{'Matchup':<40} {'A-Best':>6} {'B-Best':>6} {'Draws':>6} {'AvgEarn':>10}", flush=True)
    print("-" * 72, flush=True)
    for label, r in all_results.items():
        print(f"{label:<40} {r['a_wins']:>6} {r['b_wins']:>6} {r['draws']:>6} {r['avg_earn']:>10.0f}", flush=True)

    output = Path("scripts/research_eval/results")
    output.mkdir(exist_ok=True)
    (output / "full_ablation.json").write_text(json.dumps(all_results, indent=2, default=str))
    print(f"\nSaved to scripts/research_eval/results/full_ablation.json")


if __name__ == "__main__":
    main()
