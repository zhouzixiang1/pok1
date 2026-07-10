#!/usr/bin/env python3
"""Long-running counterfactual data collector for the opponent-aware value net.

Designed to run detached under ``nohup`` for hours. Each pass:
  1. Reads the live strongest classic pool from .evolution_pok glicko ratings.
  2. Runs sequential 2-hand counterfactual probes (port-isolated, so up to 4
     can run concurrently) across train/val/held_out opponents with rotating
     seeds.
  3. Appends annotated rows to cumulative train/val/held_out JSONL and logs
     progress.

Usage (detached):
    nohup python bots/neural_national_lab/tools/longrun_collect_oppmodel.py \
      --candidate bots/neural_national_lab/versions/v140_national_v123_overlay_no_large_commit_veto_tcp \
      --out-dir bots/neural_national_lab/data/oppmodel/longrun \
      --passes 60 --workers 4 > collect.log 2>&1 &

Check progress:
    tail -f bots/neural_national_lab/data/oppmodel/longrun/progress.log
    wc -l bots/neural_national_lab/data/oppmodel/longrun/cf_*.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
TOOLS = Path(__file__).resolve().parent
EVO = ROOT.parent / ".evolution_pok" / "web" / "core" / "results" / "glicko_ratings.json"


def _resolve(p: str) -> Path:
    raw = Path(p)
    return raw if raw.is_absolute() else (ROOT / raw).resolve()


def live_strongest(n: int = 12) -> list[str]:
    """Read the live strongest classic bots by conservative Glicko."""
    try:
        d = json.load(open(EVO))
        rows = [(k, v.get("r", 1500) - 2 * v.get("rd", 350))
                for k, v in d.items() if isinstance(v, dict) and k.startswith("national_v")]
        rows.sort(key=lambda x: x[1], reverse=True)
        return [k for k, _ in rows[:n]]
    except Exception:
        return ["national_v135", "national_v114", "national_v73", "national_v121",
                "national_v122", "national_v120", "national_v119", "national_v123"]


def probe_one(candidate: str, opponent_dir: str, split: str, name: str,
              hands: int, seed_base: int, bot_seed_base: int, out_dir: str,
              timeout_sec: int) -> tuple[int, str]:
    """Run one probe, append annotated rows to the cumulative split file. Returns (rows, name)."""
    tag = f"{split}_{name}_s{seed_base}_b{bot_seed_base}"
    tmp_jsonl = Path(out_dir) / f"_tmp_{tag}.jsonl"
    cmd = [
        sys.executable, str(TOOLS / "native_tcp_counterfactual_probe.py"),
        "--candidate", candidate, "--opponent", opponent_dir,
        "--hands", str(hands), "--seed-base", str(seed_base),
        "--bot-seed-base", str(bot_seed_base),
        "--max-decisions", "12", "--max-alternatives", "3", "--stage", "any",
        "--timeout-sec", str(timeout_sec),
        "--output", str(Path(out_dir) / f"_tmp_{tag}.json"),
        "--jsonl-output", str(tmp_jsonl),
    ]
    try:
        subprocess.run(cmd, cwd=str(ROOT), timeout=timeout_sec + 40,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        pass
    rows = 0
    if tmp_jsonl.exists():
        cum = Path(out_dir) / f"cf_{split}.jsonl"
        with open(tmp_jsonl, "r", encoding="utf-8") as src, open(cum, "a", encoding="utf-8") as dst:
            for line in src:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                obj["_split"] = split
                obj["_opponent_label"] = name
                obj["_seed_base"] = seed_base
                obj["_bot_seed_base"] = bot_seed_base
                dst.write(json.dumps(obj) + "\n")
                rows += 1
        try:
            tmp_jsonl.unlink()
        except OSError:
            pass
    try:
        (Path(out_dir) / f"_tmp_{tag}.json").unlink(missing_ok=True)
    except Exception:
        pass
    return rows, name


def build_pool(pass_num: int) -> list[tuple[str, str, str]]:
    """Build opponent list for this pass: train on live-strongest + old, val/held_out separate.

    Rotates which bots are val/held_out across passes so coverage broadens.
    """
    strong = live_strongest(16)
    old = ["national_v2", "national_v3", "national_v5", "national_v7",
           "national_v8", "national_v9", "national_v14", "national_v16"]
    all_bots = list(dict.fromkeys(strong + old))
    # Rotate held_out/val selection by pass for coverage.
    n = len(all_bots)
    ho_start = (pass_num * 3) % n
    held = [all_bots[(ho_start + i) % n] for i in range(2)]
    val_start = (pass_num * 5 + 2) % n
    val = [all_bots[(val_start + i) % n] for i in range(2)]
    train = [b for b in all_bots if b not in held and b not in val]
    pool = [(b, str(_resolve(f"bots/{b}")), "train") for b in train]
    pool += [(b, str(_resolve(f"bots/{b}")), "val") for b in val]
    pool += [(b, str(_resolve(f"bots/{b}")), "held_out") for b in held]
    # only keep bots that actually exist
    return [(n, p, s) for n, p, s in pool if Path(p).exists()]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--candidate", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--passes", type=int, default=40)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--hands", type=int, default=2)
    ap.add_argument("--timeout-sec", type=int, default=55)
    args = ap.parse_args(argv)
    out_dir = _resolve(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    plog = open(out_dir / "progress.log", "a", encoding="utf-8")
    def log(msg):
        line = f"[{time.strftime('%Y%m%dT%H%M%S')}] {msg}"
        print(line, flush=True)
        plog.write(line + "\n"); plog.flush()

    seed_offset = {"train": 0, "val": 100000, "held_out": 200000}
    total_rows = {"train": 0, "val": 0, "held_out": 0}
    t_global = time.time()
    for ps in range(args.passes):
        pool = build_pool(ps)
        seed_base = 5000 + ps * 17
        tasks = []
        for i, (name, path, split) in enumerate(pool):
            tasks.append((name, path, split, args.hands,
                          seed_base + seed_offset[split],
                          1000 + ps * 100 + i))
        t0 = time.time()
        pass_rows = 0
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(probe_one, args.candidate, p, s, n, h, sb, bsb,
                              str(out_dir), args.timeout_sec): (n, s)
                    for n, p, s, h, sb, bsb in tasks}
            for fut in as_completed(futs):
                try:
                    rows, name = fut.result()
                except Exception:
                    rows, name = 0, "?"
                pass_rows += rows
                total_rows["train" if futs[fut][1] == "train" else futs[fut][1]] += rows if futs[fut][1] in total_rows else 0
        # recount cumulative from disk for accuracy
        for sp in total_rows:
            cf = out_dir / f"cf_{sp}.jsonl"
            total_rows[sp] = sum(1 for _ in open(cf)) if cf.exists() else 0
        log(f"pass {ps+1}/{args.passes}: pool={len(pool)} rows_this_pass={pass_rows} "
            f"cumul={total_rows} dt={time.time()-t0:.0f}s elapsed={time.time()-t_global:.0f}s")
    log(f"DONE: {total_rows} total={sum(total_rows.values())} elapsed={time.time()-t_global:.0f}s")
    plog.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
