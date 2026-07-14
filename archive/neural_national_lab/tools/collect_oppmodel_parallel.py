#!/usr/bin/env python3
"""Parallel counterfactual data collector using subprocess.isolation.

Each probe runs as its own subprocess calling
``native_tcp_counterfactual_probe.py``. The native TCP runner binds to an
ephemeral port (``127.0.0.1:0``) per process, so probes are fully isolated and
can run concurrently. This script spawns up to N worker subprocesses and merges
all annotated JSONL into train/val/held_out splits.

This is the scalable data-collection path for the opponent-aware value network.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
TOOLS = Path(__file__).resolve().parent


def _resolve(p: str) -> Path:
    raw = Path(p)
    return raw if raw.is_absolute() else (ROOT / raw).resolve()


def _probe_one(task: dict) -> dict:
    """Run a single counterfactual probe subprocess. Returns a result dict."""
    import json as _json
    candidate = task["candidate"]
    opponent = task["opponent"]
    split = task["split"]
    name = task["name"]
    hands = task["hands"]
    seed_base = task["seed_base"]
    bot_seed_base = task["bot_seed_base"]
    out_dir = task["out_dir"]
    max_decisions = task["max_decisions"]
    max_alternatives = task["max_alternatives"]
    stage = task["stage"]
    timeout_sec = task["timeout_sec"]
    tag = f"{split}_{name}_seed{seed_base}_bs{bot_seed_base}"
    jsonl_path = Path(out_dir) / f"{tag}.jsonl"
    summary_path = Path(out_dir) / f"{tag}.json"
    cmd = [
        sys.executable, str(TOOLS / "native_tcp_counterfactual_probe.py"),
        "--candidate", str(candidate),
        "--opponent", str(opponent),
        "--hands", str(hands),
        "--seed-base", str(seed_base),
        "--bot-seed-base", str(bot_seed_base),
        "--max-decisions", str(max_decisions),
        "--max-alternatives", str(max_alternatives),
        "--stage", stage,
        "--timeout-sec", str(timeout_sec),
        "--output", str(summary_path),
        "--jsonl-output", str(jsonl_path),
    ]
    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd, cwd=str(ROOT), timeout=timeout_sec + 40,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        rc = -1
    rows = 0
    if jsonl_path.exists():
        with open(jsonl_path, "r", encoding="utf-8") as fh:
            rows = sum(1 for line in fh if line.strip())
    # Annotate rows with split metadata.
    annotated = Path(out_dir) / f"{tag}.split.jsonl"
    if rows > 0 and jsonl_path.exists():
        with open(jsonl_path, "r", encoding="utf-8") as src, \
             open(annotated, "w", encoding="utf-8") as dst:
            for line in src:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = _json.loads(line)
                except _json.JSONDecodeError:
                    continue
                obj["_split"] = split
                obj["_opponent_label"] = name
                obj["_seed_base"] = seed_base
                obj["_bot_seed_base"] = bot_seed_base
                dst.write(_json.dumps(obj) + "\n")
    return {
        "split": split, "opponent": name, "seed_base": seed_base,
        "rows": rows, "rc": rc, "dt": round(time.time() - t0),
        "annotated": str(annotated),
    }


def build_task_list(args) -> list[dict]:
    """Build the full probe task list across opponents, splits, and repeats."""
    # Split assignment: train on the realtime strong + old pool, val/held_out separate.
    train_opps = args.train_opponents.split(",") if args.train_opponents else [
        "national_v135", "national_v114", "national_v73", "national_v63",
        "national_v121", "national_v122", "national_v54", "national_v70",
        "national_v2", "national_v3", "national_v5", "national_v7",
        "national_v8", "national_v9", "national_v14", "national_v16",
    ]
    val_opps = args.val_opponents.split(",") if args.val_opponents else [
        "national_v123", "national_v119", "national_v120", "national_v66",
    ]
    held_opps = args.held_opponents.split(",") if args.held_opponents else [
        "national_v40", "national_v98", "national_v39", "national_v53",
    ]
    tasks = []
    idx = 0
    for split, opps, base in (("train", train_opps, 5000),
                              ("val", val_opps, 6000),
                              ("held_out", held_opps, 7000)):
        for name in opps:
            name = name.strip()
            if not name:
                continue
            for rep in range(args.repeats):
                tasks.append({
                    "candidate": args.candidate,
                    "opponent": str(_resolve(f"bots/{name}")),
                    "split": split, "name": name,
                    "hands": args.hands,
                    "seed_base": base + rep * 13,
                    "bot_seed_base": 1000 + idx,
                    "out_dir": args.out_dir,
                    "max_decisions": args.max_decisions,
                    "max_alternatives": args.max_alternatives,
                    "stage": args.stage,
                    "timeout_sec": args.timeout_sec,
                })
                idx += 1
    return tasks


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--candidate", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--train-opponents", help="Comma-separated train opponent names.")
    ap.add_argument("--val-opponents", help="Comma-separated val opponent names.")
    ap.add_argument("--held-opponents", help="Comma-separated held_out opponent names.")
    ap.add_argument("--hands", type=int, default=8)
    ap.add_argument("--repeats", type=int, default=3, help="Probe repeats per opponent.")
    ap.add_argument("--max-decisions", type=int, default=14)
    ap.add_argument("--max-alternatives", type=int, default=3)
    ap.add_argument("--stage", default="any")
    ap.add_argument("--timeout-sec", type=int, default=90)
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args(argv)
    _resolve(args.out_dir).mkdir(parents=True, exist_ok=True)

    tasks = build_task_list(args)
    print(f"[collect] {len(tasks)} probes, workers={args.workers}, "
          f"hands={args.hands} repeats={args.repeats}", flush=True)
    t0 = time.time()
    results = []
    done = 0
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_probe_one, t): t for t in tasks}
        for fut in as_completed(futs):
            try:
                r = fut.result()
            except Exception as e:
                r = {"split": "?", "opponent": "?", "rows": 0, "rc": -2, "dt": 0,
                     "annotated": "", "error": str(e)}
            results.append(r)
            done += 1
            total_rows = sum(x["rows"] for x in results)
            if r["rows"] == 0:
                print(f"[collect] WARN {done}/{len(tasks)} {r['split']}/{r['opponent']} "
                      f"rc={r['rc']} rows=0 dt={r['dt']}s", flush=True)
            elif done % 5 == 0 or done == len(tasks):
                print(f"[collect] {done}/{len(tasks)} {r['split']}/{r['opponent']} "
                      f"rows={r['rows']} (cumulative {total_rows} rows, "
                      f"{(time.time()-t0):.0f}s elapsed)", flush=True)
    # Merge annotated files into per-split combined jsonl.
    splits = ("train", "val", "held_out")
    counts = {}
    for split in splits:
        combined = _resolve(args.out_dir) / f"cf_{split}.jsonl"
        n = 0
        with open(combined, "w", encoding="utf-8") as dst:
            for r in results:
                if r["split"] != split or r["rows"] == 0:
                    continue
                ap = Path(r["annotated"])
                if not ap.exists():
                    continue
                for line in open(ap, "r", encoding="utf-8"):
                    if line.strip():
                        dst.write(line)
                        n += 1
        counts[split] = n
    manifest = {"generated_at": time.strftime("%Y%m%dT%H%M%S"),
                "candidate": args.candidate, "splits": counts,
                "total_probes": len(tasks), "elapsed_sec": round(time.time() - t0),
                "shards": results}
    with open(_resolve(args.out_dir) / "manifest.json", "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    print(f"[collect] DONE in {time.time()-t0:.0f}s splits={counts} "
          f"total={sum(counts.values())}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
