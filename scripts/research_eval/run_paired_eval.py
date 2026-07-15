#!/usr/bin/env python3
"""Paired seed block H2H evaluation with Holm-Bonferroni correction.

For each pair, runs N blocks where each block plays two matches with the SAME
deck seed but swapped seat assignments. The paired difference eliminates card
luck variance and isolates skill.
"""
import subprocess, sys, os, time, json, math
from pathlib import Path

REPO = Path("/home/zzx/project/pok")
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


def run_one_match(bot_a_name, bot_b_name, port, seed_a, seed_b, deck_seed_base, timeout=90):
    """Run one 70-hand match with deterministic deck seed."""
    a_cfg = BOT_CONFIGS[bot_a_name]
    b_cfg = BOT_CONFIGS[bot_b_name]

    env = {**os.environ,
           "POK_NATIVE_LOCAL_ACTION_DELAY": "0",
           "PYTHONPATH": ".",
           "POK_DECK_SEED_BASE": str(deck_seed_base)}

    srv = subprocess.Popen(
        [sys.executable, str(SEVER / "main.py"), "--tcp-port", str(port), "--web-port", str(port + 8000)],
        cwd=str(SEVER), stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    time.sleep(3)

    a_cmd = a_cfg["cmd"] + ["--host", "127.0.0.1", "--port", str(port), "--name", bot_a_name]
    b_cmd = b_cfg["cmd"] + ["--host", "127.0.0.1", "--port", str(port), "--name", bot_b_name]
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
                    if bot_a_name in bn: a_earn = earn
                    elif bot_b_name in bn: b_earn = earn
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


def sign_test_pvalue(diffs):
    """Two-sided sign test p-value from paired differences."""
    n = len(diffs)
    pos = sum(1 for d in diffs if d > 0)
    neg = sum(1 for d in diffs if d < 0)
    nonzero = pos + neg
    if nonzero == 0:
        return 1.0
    # Binomial test: under H0, P(positive) = 0.5
    k = min(pos, neg)
    p = 0.0
    for i in range(k + 1):
        p += math.comb(nonzero, i) * (0.5 ** nonzero)
    p *= 2  # two-sided
    return min(1.0, p)


def holm_bonferroni(pvalues, alpha=0.05):
    """Apply Holm-Bonferroni correction. Returns list of (reject, adjusted_p)."""
    m = len(pvalues)
    indexed = sorted(enumerate(pvalues), key=lambda x: x[1])
    results = [None] * m
    for rank, (orig_idx, p) in enumerate(indexed):
        adjusted = p * (m - rank)
        adjusted = min(adjusted, 1.0)
        # Enforce monotonicity
        if rank > 0:
            prev_adjusted = results[indexed[rank - 1][0]][1]
            adjusted = max(adjusted, prev_adjusted)
        reject = adjusted <= alpha
        results[orig_idx] = (reject, adjusted)
    return results


def main():
    n_blocks = 5  # 5 blocks × 2 matches = 10 matches per pair
    base_policy_seed = 8000
    base_port = 30000

    all_results = {}
    all_pvalues = []
    pair_names = []

    for pair_idx, (a_name, b_name) in enumerate(PAIRS):
        print(f"\n=== {a_name} vs {b_name} (paired, {n_blocks} blocks) ===", flush=True)
        blocks = []
        for block_idx in range(n_blocks):
            port = base_port + pair_idx * n_blocks * 4 + block_idx * 4
            deck_seed = 100000 + pair_idx * 1000 + block_idx * 10
            seed_a_m1 = base_policy_seed + pair_idx * 100 + block_idx * 10
            seed_b_m1 = seed_a_m1 + 1
            # Match 1: A=seat0, B=seat1
            r1 = run_one_match(a_name, b_name, port, seed_a_m1, seed_b_m1, deck_seed)
            # Match 2: B=seat0, A=seat1 (swap connection order), same deck seed
            r2 = run_one_match(b_name, a_name, port + 2, seed_b_m1 + 100, seed_a_m1 + 100, deck_seed)
            
            # Paired difference: A's earnings from both perspectives
            # In match 2, A is the second bot, so a_earnings is from B's perspective
            # We need to negate it to get A's perspective
            a_earn_m1 = r1["a_earnings"]
            a_earn_m2 = -r2["a_earnings"]  # negate because A was in seat 1
            paired_diff = a_earn_m1 + a_earn_m2
            
            blocks.append({
                "block": block_idx,
                "deck_seed": deck_seed,
                "match1_a_earn": a_earn_m1,
                "match2_a_earn": a_earn_m2,
                "paired_diff": paired_diff,
                "m1_winner": r1["winner"],
                "m2_winner": r2["winner"],
            })
            print(f"  Block {block_idx+1}: m1={a_earn_m1:+.0f} m2={a_earn_m2:+.0f} diff={paired_diff:+.0f}", flush=True)
        
        diffs = [b["paired_diff"] for b in blocks]
        pos = sum(1 for d in diffs if d > 0)
        neg = sum(1 for d in diffs if d < 0)
        zero = sum(1 for d in diffs if d == 0)
        avg_diff = sum(diffs) / len(diffs)
        pval = sign_test_pvalue(diffs)
        
        all_results[f"{a_name}_vs_{b_name}"] = {
            "blocks": blocks,
            "n_blocks": n_blocks,
            "positive_diffs": pos,
            "negative_diffs": neg,
            "zero_diffs": zero,
            "avg_paired_diff": avg_diff,
            "sign_test_pvalue": pval,
        }
        all_pvalues.append(pval)
        pair_names.append(f"{a_name}_vs_{b_name}")
        print(f"  Summary: +{pos}/-{neg}/0={zero}, avg_diff={avg_diff:+.0f}, p={pval:.4f}", flush=True)

    # Holm-Bonferroni correction
    holm = holm_bonferroni(all_pvalues, alpha=0.05)
    print("\n=== HOLM-BONFERRONI CORRECTION (alpha=0.05) ===", flush=True)
    for name, (reject, adj_p), raw_p in zip(pair_names, holm, all_pvalues):
        sig = "SIGNIFICANT" if reject else "not significant"
        print(f"  {name}: raw_p={raw_p:.4f} adj_p={adj_p:.4f} -> {sig}", flush=True)
        all_results[name]["holm_adjusted_p"] = adj_p
        all_results[name]["holm_significant"] = reject

    print("\n=== PAIRED SEED BLOCK SUMMARY ===", flush=True)
    for name in pair_names:
        r = all_results[name]
        print(f"  {name}: avg_diff={r['avg_paired_diff']:+.0f} +{r['positive_diffs']}/-{r['negative_diffs']}/0={r['zero_diffs']} "
              f"p={r['sign_test_pvalue']:.4f} adj_p={r['holm_adjusted_p']:.4f} {'*' if r['holm_significant'] else ''}")

    output = Path("scripts/research_eval/results")
    output.mkdir(exist_ok=True)
    (output / "paired_seed_holm.json").write_text(json.dumps(all_results, indent=2))
    print(f"\nSaved to scripts/research_eval/results/paired_seed_holm.json", flush=True)


if __name__ == "__main__":
    main()
