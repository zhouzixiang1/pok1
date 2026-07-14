#!/usr/bin/env python3
"""Run deterministic counterfactual rollout shards and merge their summaries."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
PROBE = Path(__file__).resolve().with_name("counterfactual_rollout_probe.py")


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _resolve(path: str) -> Path:
    p = Path(path)
    return (ROOT / p).resolve() if not p.is_absolute() else p


def _stats(values: list[float]) -> dict[str, Any]:
    n = len(values)
    mean = statistics.mean(values) if values else 0.0
    median = statistics.median(values) if values else 0.0
    stddev = statistics.stdev(values) if n >= 2 else 0.0
    stderr = stddev / (n ** 0.5) if n >= 2 else 0.0
    margin = 1.96 * stderr if n >= 2 else 0.0
    return {
        "samples": n,
        "mean": mean,
        "median": median,
        "stddev": stddev,
        "stderr": stderr,
        "ci95_low": mean - margin if n >= 2 else None,
        "ci95_high": mean + margin if n >= 2 else None,
        "significant_positive_95": bool(n >= 2 and mean - margin > 0.0),
        "significant_negative_95": bool(n >= 2 and mean + margin < 0.0),
    }


def _pot_band(value: Any) -> str:
    try:
        pot = float(value)
    except (TypeError, ValueError):
        return "unknown"
    if pot < 300:
        return "pot_lt_300"
    if pot < 700:
        return "pot_300_699"
    if pot < 1500:
        return "pot_700_1499"
    if pot < 3000:
        return "pot_1500_2999"
    return "pot_ge_3000"


def _raise_delta_band(value: Any) -> str:
    try:
        amount = int(value)
    except (TypeError, ValueError):
        return "unknown"
    if amount <= 0:
        return "non_raise"
    if amount <= 125:
        return "raise_le_125"
    if amount <= 250:
        return "raise_126_250"
    if amount <= 500:
        return "raise_251_500"
    return "raise_gt_500"


def _summarize(probes: list[dict[str, Any]]) -> dict[str, Any]:
    primary = [float(row["primary_delta"]) for row in probes if row.get("status") == "ok" and row.get("primary_delta") is not None]
    by_kind: dict[str, list[float]] = {}
    by_stage: dict[str, list[float]] = {}
    by_stage_kind: dict[str, list[float]] = {}
    by_pot_band: dict[str, list[float]] = {}
    by_advised_final: dict[str, list[float]] = {}
    by_raise_delta_band: dict[str, list[float]] = {}
    for row in probes:
        if row.get("status") != "ok" or row.get("primary_delta") is None:
            continue
        delta = float(row["primary_delta"])
        by_kind.setdefault(str(row.get("kind")), []).append(delta)
        by_stage.setdefault(str(row.get("stage")), []).append(delta)
        by_stage_kind.setdefault(f"{row.get('stage')}|{row.get('kind')}", []).append(delta)
        by_pot_band.setdefault(_pot_band(row.get("pot")), []).append(delta)
        by_advised_final.setdefault(str(row.get("advised_final")), []).append(delta)
        by_raise_delta_band.setdefault(_raise_delta_band(row.get("advised_final")), []).append(delta)
    return {
        "ok_probes": len(primary),
        "failed_probes": len(probes) - len(primary),
        "primary_delta": _stats(primary),
        "by_kind": {key: _stats(values) for key, values in sorted(by_kind.items())},
        "by_stage": {key: _stats(values) for key, values in sorted(by_stage.items())},
        "by_stage_kind": {key: _stats(values) for key, values in sorted(by_stage_kind.items())},
        "by_pot_band": {key: _stats(values) for key, values in sorted(by_pot_band.items())},
        "by_advised_final": {key: _stats(values) for key, values in sorted(by_advised_final.items())},
        "by_raise_delta_band": {key: _stats(values) for key, values in sorted(by_raise_delta_band.items())},
    }


def _probe_cmd(args: argparse.Namespace, shard_idx: int, shard_output: Path) -> list[str]:
    seed_offset = args.seed_offset + shard_idx * args.games_per_shard * args.seed_stride
    cmd = [
        sys.executable,
        str(PROBE),
        "--version",
        args.version,
        "--opponent",
        args.opponent,
        "--games",
        str(args.games_per_shard),
        "--max-probes",
        str(args.max_probes_per_shard),
        "--max-scan-decisions",
        str(args.max_scan_decisions),
        "--kind",
        args.kind,
        "--stage",
        args.stage,
        "--branch-scope",
        args.branch_scope,
        "--max-branch-steps",
        str(args.max_branch_steps),
        "--seed-base",
        str(args.seed_base),
        "--seed-offset",
        str(seed_offset),
        "--seed-stride",
        str(args.seed_stride),
        "--max-hands",
        str(args.max_hands),
        "--output",
        str(shard_output),
    ]
    if args.bot_seed_base is not None:
        cmd.extend(
            [
                "--bot-seed-base",
                str(args.bot_seed_base + shard_idx * args.games_per_shard * args.bot_seed_stride * 2),
                "--bot-seed-stride",
                str(args.bot_seed_stride),
            ]
        )
    if args.min_conf > 0:
        cmd.extend(["--min-conf", str(args.min_conf)])
    if args.no_mirror:
        cmd.append("--no-mirror")
    if args.no_scan_persistent:
        cmd.append("--no-scan-persistent")
    return cmd


def _display_cmd(cmd: list[str]) -> str:
    display = []
    for idx, part in enumerate(cmd):
        if idx == 0:
            display.append("python")
            continue
        path = Path(part)
        if path.is_absolute():
            display.append(_rel(path))
        else:
            display.append(part)
    return " ".join(display)


def _shard_seed_offset(args: argparse.Namespace, shard_idx: int) -> int:
    return args.seed_offset + shard_idx * args.games_per_shard * args.seed_stride


def _run_shard(args: argparse.Namespace, shard_idx: int, shard_output: Path) -> dict[str, Any]:
    cmd = _probe_cmd(args, shard_idx, shard_output)
    skipped = shard_output.exists() and not args.rerun_existing
    if skipped:
        return {
            "shard": shard_idx,
            "output": _rel(shard_output),
            "seed_offset": _shard_seed_offset(args, shard_idx),
            "command": _display_cmd(cmd),
            "returncode": 0,
            "skipped_existing": True,
            "stdout_tail": "",
            "stderr_tail": "",
        }
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            text=True,
            capture_output=True,
            timeout=args.shard_timeout_sec or None,
        )
        returncode = proc.returncode
        stdout_tail = proc.stdout[-4000:]
        stderr_tail = proc.stderr[-4000:]
    except subprocess.TimeoutExpired as exc:
        returncode = 124
        stdout_tail = str(exc.stdout or "")[-4000:]
        stderr_tail = str(exc.stderr or "")[-4000:]
    return {
        "shard": shard_idx,
        "output": _rel(shard_output),
        "seed_offset": _shard_seed_offset(args, shard_idx),
        "command": _display_cmd(cmd),
        "returncode": returncode,
        "skipped_existing": False,
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
    }


def _merge_shard_entry(payload: dict[str, Any], shard_entry: dict[str, Any]) -> None:
    shard_output = ROOT / shard_entry["output"]
    if shard_output.exists():
        shard_data = json.loads(shard_output.read_text(encoding="utf-8"))
        shard_entry["summary"] = shard_data.get("summary", {})
        shard_entry["probe_count"] = len(shard_data.get("probes", []))
        for row in shard_data.get("probes", []):
            payload["probes"].append(
                {
                    **row,
                    "shard": shard_entry["shard"],
                    "shard_seed_offset": shard_entry.get("seed_offset"),
                }
            )
    payload["shard_results"].append(shard_entry)
    payload["summary"] = _summarize(payload["probes"])


def _write(output: Path | None, payload: dict[str, Any]) -> None:
    if output is None:
        return
    out = output if output.is_absolute() else ROOT / output
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--opponent", required=True)
    parser.add_argument("--shards", type=int, default=4)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--games-per-shard", type=int, default=2)
    parser.add_argument("--max-probes-per-shard", type=int, default=8)
    parser.add_argument("--max-scan-decisions", type=int, default=400)
    parser.add_argument("--min-conf", type=float, default=0.0)
    parser.add_argument("--kind", choices=["any", "to_raise", "to_call", "to_fold", "to_allin", "fold_to_call"], default="to_raise")
    parser.add_argument("--stage", choices=["any", "preflop", "flop", "turn", "river"], default="any")
    parser.add_argument("--branch-scope", choices=["hand", "match"], default="hand")
    parser.add_argument("--max-branch-steps", type=int, default=5000)
    parser.add_argument("--seed-base", type=int, default=20260704)
    parser.add_argument("--seed-offset", type=int, default=0)
    parser.add_argument("--seed-stride", type=int, default=1)
    parser.add_argument("--bot-seed-base", type=int)
    parser.add_argument("--bot-seed-stride", type=int, default=10000)
    parser.add_argument("--max-hands", type=int, default=70)
    parser.add_argument("--shard-timeout-sec", type=int, default=0)
    parser.add_argument("--rerun-existing", action="store_true")
    parser.add_argument("--no-scan-persistent", action="store_true")
    parser.add_argument("--no-mirror", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    output = args.output if args.output.is_absolute() else ROOT / args.output
    shard_dir = output.with_suffix("")
    shard_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "mode": "counterfactual_rollout_shards",
        "version": _rel(_resolve(args.version)),
        "opponent": _rel(_resolve(args.opponent)),
        "shards": args.shards,
        "workers": args.workers,
        "games_per_shard": args.games_per_shard,
        "max_probes_per_shard": args.max_probes_per_shard,
        "seed_base": args.seed_base,
        "seed_offset": args.seed_offset,
        "seed_stride": args.seed_stride,
        "bot_seed_base": args.bot_seed_base,
        "bot_seed_stride": args.bot_seed_stride,
        "filters": {
            "kind": args.kind,
            "stage": args.stage,
            "min_conf": args.min_conf,
            "branch_scope": args.branch_scope,
        },
        "shard_results": [],
        "probes": [],
        "summary": {},
    }
    _write(output, payload)

    shard_outputs = [shard_dir / f"shard_{idx:03d}.json" for idx in range(args.shards)]
    if args.workers <= 1:
        shard_entries = []
        for shard_idx, shard_output in enumerate(shard_outputs):
            if shard_output.exists() and not args.rerun_existing:
                print(f"shard {shard_idx + 1}/{args.shards}: reuse {_rel(shard_output)}")
            else:
                print(f"shard {shard_idx + 1}/{args.shards}: {' '.join(_probe_cmd(args, shard_idx, shard_output))}")
            shard_entries.append(_run_shard(args, shard_idx, shard_output))
    else:
        print(f"running {args.shards} shards with {args.workers} workers")
        shard_entries = [None] * args.shards
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
            futures = {
                executor.submit(_run_shard, args, shard_idx, shard_output): shard_idx
                for shard_idx, shard_output in enumerate(shard_outputs)
            }
            for future in as_completed(futures):
                shard_idx = futures[future]
                shard_entry = future.result()
                shard_entries[shard_idx] = shard_entry
                print(
                    f"  shard {shard_idx + 1}/{args.shards} rc={shard_entry['returncode']} "
                    f"output={shard_entry['output']}"
                )

    for shard_entry in shard_entries:
        if shard_entry is None:
            continue
        _merge_shard_entry(payload, shard_entry)
        _write(output, payload)
        print(
            f"merge shard {shard_entry['shard'] + 1}/{args.shards}: "
            f"rc={shard_entry['returncode']} probes={shard_entry.get('probe_count', 0)} "
            f"merged={len(payload['probes'])} mean={payload['summary'].get('primary_delta', {}).get('mean', 0.0):.1f}"
        )
        if shard_entry["returncode"] != 0 and not (ROOT / shard_entry["output"]).exists():
            break

    payload["summary"] = _summarize(payload["probes"])
    _write(output, payload)
    print(json.dumps(payload["summary"], indent=2))


if __name__ == "__main__":
    main()
