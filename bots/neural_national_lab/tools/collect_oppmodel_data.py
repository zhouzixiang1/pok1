#!/usr/bin/env python3
"""Collect native-TCP counterfactual action-value data across a diverse opponent pool.

This implements the data-engineering requirement of the neural-line objective:
reproducible training data spanning the realtime strongest classic pool, recent
completed national bots, old top bots, and held-out opponents, with
train/val/held-out split markers and the full per-decision feature set.

Output: a single JSONL file (one row per probed decision) plus a split manifest.

Opponent selection is evidence-driven, not "highest number = strongest". The
default pool is derived from the current realtime Glicko ratings in
``.evolution_pok`` plus the old-pool and held-out sets. Override with --opponents.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
TOOLS = Path(__file__).resolve().parent


def _resolve(p: str) -> Path:
    raw = Path(p)
    return raw if raw.is_absolute() else (ROOT / raw).resolve()


def default_opponent_pool() -> list[tuple[str, str, str]]:
    """Return (name, path, split) tuples. split in {train, val, held_out}.

    Selection rationale (read live, do not assume highest = strongest):
    - Realtime top classic bots by conservative Glicko -> train/val.
    - Old top8+v7 line -> train (these expose different action styles).
    - A few strong bots held out entirely -> held_out (generalization check).
    """
    pool: list[tuple[str, str, str]] = []
    # Realtime strong classic pool (from .evolution_pok glicko at collection time).
    train_strong = [
        "national_v135", "national_v114", "national_v73", "national_v63",
        "national_v121", "national_v122", "national_v54", "national_v70",
    ]
    val_pool = ["national_v123", "national_v119", "national_v120"]
    # Old completed strong bots (diverse action styles).
    old_pool = ["national_v2", "national_v3", "national_v5", "national_v7",
                "national_v8", "national_v9", "national_v14", "national_v16"]
    # Held-out opponents (never trained on).
    held_out = ["national_v66", "national_v40", "national_v98"]
    for name in train_strong + old_pool:
        pool.append((name, f"bots/{name}", "train"))
    for name in val_pool:
        pool.append((name, f"bots/{name}", "val"))
    for name in held_out:
        pool.append((name, f"bots/{name}", "held_out"))
    return pool


async def run_probe(candidate: str, opponent_path: str, split: str, name: str,
                    *, hands: int, seed_base: int, bot_seed_base: int,
                    max_decisions: int, max_alternatives: int, stage: str,
                    timeout_sec: int, out_dir: Path, idx: int) -> dict[str, Any]:
    tag = f"{idx:03d}_{name}_{split}_seed{seed_base}"
    jsonl_path = out_dir / f"{tag}.jsonl"
    summary_path = out_dir / f"{tag}.json"
    cmd = [
        sys.executable, str(TOOLS / "native_tcp_counterfactual_probe.py"),
        "--candidate", str(_resolve(candidate)),
        "--opponent", str(_resolve(opponent_path)),
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
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        cwd=str(ROOT),
    )
    _, stderr = await proc.communicate()
    rows = 0
    if jsonl_path.exists():
        with open(jsonl_path, "r", encoding="utf-8") as fh:
            rows = sum(1 for line in fh if line.strip())
    # Annotate each row with split + opponent for the manifest.
    annotated = jsonl_path.with_suffix(".split.jsonl")
    if jsonl_path.exists():
        with open(jsonl_path, "r", encoding="utf-8") as src, \
             open(annotated, "w", encoding="utf-8") as dst:
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
    return {
        "opponent": name, "split": split, "seed_base": seed_base,
        "rows": rows, "rc": proc.returncode,
        "jsonl": str(annotated.relative_to(ROOT)),
        "stderr_tail": (stderr or b"").decode("utf-8", "replace")[-300:],
    }


async def main_async(args: argparse.Namespace) -> int:
    out_dir = _resolve(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.opponents:
        names = [o.strip() for o in args.opponents.split(",") if o.strip()]
        pool = [(n, f"bots/{n}", "train") for n in names]
    else:
        pool = default_opponent_pool()

    tasks = []
    idx = 0
    for name, path, split in pool:
        # Use distinct seed ranges per split so train/val/held_out never overlap.
        seed_base = args.seed_base
        if split == "val":
            seed_base += 10000
        elif split == "held_out":
            seed_base += 20000
        bot_seed_base = args.bot_seed_base + idx
        for rep in range(args.repeats):
            tasks.append(run_probe(
                args.candidate, path, split, name,
                hands=args.hands, seed_base=seed_base + rep * 7,
                bot_seed_base=bot_seed_base,
                max_decisions=args.max_decisions,
                max_alternatives=args.max_alternatives,
                stage=args.stage, timeout_sec=args.timeout_sec,
                out_dir=out_dir, idx=idx,
            ))
            idx += 1

    print(f"[collect] {len(tasks)} probe tasks across {len(pool)} opponents, "
          f"workers={args.workers}", flush=True)
    sem = asyncio.Semaphore(args.workers)
    results: list[dict[str, Any]] = []

    async def _bounded(t):
        async with sem:
            return await t

    start = time.time()
    for coro in asyncio.as_completed([_bounded(t) for t in tasks]):
        r = await coro
        results.append(r)
        if r["rc"] != 0 or r["rows"] == 0:
            print(f"[collect] WARN {r['opponent']}/{r['split']} rc={r['rc']} rows={r['rows']} "
                  f"seed{r['seed_base']}: {r['stderr_tail'][:120]}", flush=True)
        else:
            print(f"[collect] ok {r['opponent']}/{r['split']} rows={r['rows']} "
                  f"seed{r['seed_base']}", flush=True)

    # Concatenate split-annotated jsonl into train/val/held_out files.
    manifest = {"generated_at": time.strftime("%Y%m%dT%H%M%S"), "candidate": args.candidate,
                "splits": {"train": 0, "val": 0, "held_out": 0}, "shards": results}
    for split in ("train", "val", "held_out"):
        combined = out_dir / f"cf_{split}.jsonl"
        n = 0
        with open(combined, "w", encoding="utf-8") as dst:
            for r in results:
                if r["split"] != split or r["rows"] == 0:
                    continue
                jp = _resolve(r["jsonl"])
                if not jp.exists():
                    continue
                for line in open(jp, "r", encoding="utf-8"):
                    if line.strip():
                        dst.write(line)
                        n += 1
        manifest["splits"][split] = n
        print(f"[collect] {combined}: {n} rows", flush=True)

    with open(out_dir / "manifest.json", "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    print(f"[collect] done in {time.time()-start:.0f}s; manifest at {out_dir/'manifest.json'}", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--candidate", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--opponents", help="Comma-separated opponent names (override default pool).")
    ap.add_argument("--hands", type=int, default=20)
    ap.add_argument("--repeats", type=int, default=1, help="Probe repeats per opponent.")
    ap.add_argument("--seed-base", type=int, default=5000)
    ap.add_argument("--bot-seed-base", type=int, default=1000)
    ap.add_argument("--max-decisions", type=int, default=12)
    ap.add_argument("--max-alternatives", type=int, default=3)
    ap.add_argument("--stage", default="any")
    ap.add_argument("--timeout-sec", type=int, default=100)
    ap.add_argument("--workers", type=int, default=8)
    return asyncio.run(main_async(ap.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
