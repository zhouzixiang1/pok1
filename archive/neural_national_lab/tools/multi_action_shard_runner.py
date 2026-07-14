#!/usr/bin/env python3
"""Run deterministic multi-action counterfactual shards and merge rows."""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import json
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
PROBE = Path(__file__).resolve().with_name("multi_action_counterfactual_probe.py")
LABELS = ("fold", "call", "raise_half", "raise_pot", "raise_2pot", "allin")
TARGET_FIELDS = ("delta_vs_rule", "regret_vs_mean", "action_values")


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
    stderr = stddev / (n**0.5) if n >= 2 else 0.0
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


def _summarize(rows: list[dict[str, Any]], labels: list[str] | tuple[str, ...] = LABELS) -> dict[str, Any]:
    by_label_delta: dict[str, list[float]] = {name: [] for name in labels}
    by_label_regret: dict[str, list[float]] = {name: [] for name in labels}
    by_stage: dict[str, list[float]] = {}
    best_counts: dict[str, int] = {}
    active_best_counts: dict[str, int] = {}
    unique_counts: list[int] = []
    evaluated_branch_counts: list[int] = []
    legal_label_counts: list[int] = []
    active_target_counts: list[int] = []
    active_positive_counts: list[int] = []
    off_menu_rule_rows = 0
    ok_rows = 0
    for row in rows:
        if row.get("status") != "ok":
            continue
        ok_rows += 1
        unique_counts.append(int(row.get("unique_final_action_count", 0) or 0))
        evaluated_branch_counts.append(int(row.get("evaluated_branch_count", 0) or 0))
        legal_label_counts.append(sum(int(value) for value in row.get("legal_mask", [])))
        if not row.get("rule_final_in_menu", False):
            off_menu_rule_rows += 1
        best = row.get("best_label")
        if best:
            best_counts[str(best)] = best_counts.get(str(best), 0) + 1
        active_best = row.get("active_best_label")
        if active_best:
            active_best_counts[str(active_best)] = active_best_counts.get(str(active_best), 0) + 1
        active_target_counts.append(int(row.get("active_targets", 0) or 0))
        active_positive_counts.append(int(row.get("active_positive_targets", 0) or 0))
        rule_value = row.get("rule_value")
        if rule_value is not None:
            by_stage.setdefault(str(row.get("stage")), []).append(float(rule_value))
        deltas = row.get("delta_vs_rule", [None] * len(labels))
        regrets = row.get("regret_vs_mean", [None] * len(labels))
        for idx, name in enumerate(labels):
            delta = deltas[idx] if idx < len(deltas) else None
            regret = regrets[idx] if idx < len(regrets) else None
            if delta is not None:
                by_label_delta[name].append(float(delta))
            if regret is not None:
                by_label_regret[name].append(float(regret))
    return {
        "rows": len(rows),
        "ok_rows": ok_rows,
        "failed_rows": len(rows) - ok_rows,
        "mean_unique_final_action_count": sum(unique_counts) / len(unique_counts) if unique_counts else 0.0,
        "mean_evaluated_branch_count": sum(evaluated_branch_counts) / len(evaluated_branch_counts)
        if evaluated_branch_counts
        else 0.0,
        "mean_legal_label_count": sum(legal_label_counts) / len(legal_label_counts) if legal_label_counts else 0.0,
        "mean_active_targets": sum(active_target_counts) / len(active_target_counts) if active_target_counts else 0.0,
        "mean_active_positive_targets": (
            sum(active_positive_counts) / len(active_positive_counts) if active_positive_counts else 0.0
        ),
        "off_menu_rule_rows": off_menu_rule_rows,
        "best_label_counts": dict(sorted(best_counts.items())),
        "active_best_label_counts": dict(sorted(active_best_counts.items())),
        "rule_value_by_stage": {label: _stats(values) for label, values in sorted(by_stage.items())},
        "delta_vs_rule_by_label": {
            label: _stats(values) for label, values in by_label_delta.items() if values
        },
        "regret_vs_mean_by_label": {
            label: _stats(values) for label, values in by_label_regret.items() if values
        },
    }


def _display_cmd(cmd: list[str]) -> str:
    display: list[str] = []
    for idx, part in enumerate(cmd):
        if idx == 0:
            display.append("python")
            continue
        path = Path(part)
        display.append(_rel(path) if path.is_absolute() else part)
    return " ".join(display)


def _shard_seed_offset(args: argparse.Namespace, shard_idx: int) -> int:
    return args.seed_offset + shard_idx * args.games_per_shard * args.seed_stride


def _shard_bot_seed_base(args: argparse.Namespace, shard_idx: int) -> int | None:
    if args.bot_seed_base is None:
        return None
    return args.bot_seed_base + shard_idx * args.games_per_shard * args.bot_seed_stride * 2


def _probe_cmd(args: argparse.Namespace, shard_idx: int, shard_output: Path) -> list[str]:
    cmd = [
        sys.executable,
        str(PROBE),
        "--version",
        args.version,
        "--opponent",
        args.opponent,
        "--games",
        str(args.games_per_shard),
        "--max-rows",
        str(args.max_rows_per_shard),
        "--max-scan-decisions",
        str(args.max_scan_decisions),
        "--stage",
        args.stage,
        "--branch-scope",
        args.branch_scope,
        "--max-branch-steps",
        str(args.max_branch_steps),
        "--min-unique-actions",
        str(args.min_unique_actions),
        "--seed-base",
        str(args.seed_base),
        "--seed-offset",
        str(_shard_seed_offset(args, shard_idx)),
        "--seed-stride",
        str(args.seed_stride),
        "--max-hands",
        str(args.max_hands),
        "--output",
        str(shard_output),
    ]
    bot_seed_base = _shard_bot_seed_base(args, shard_idx)
    if bot_seed_base is not None:
        cmd.extend(["--bot-seed-base", str(bot_seed_base), "--bot-seed-stride", str(args.bot_seed_stride)])
    for label in args.prefilter_rule_label:
        cmd.extend(["--prefilter-rule-label", label])
    for label in args.prefilter_top_label:
        cmd.extend(["--prefilter-top-label", label])
    if args.prefilter_min_top_conf:
        cmd.extend(["--prefilter-min-top-conf", str(args.prefilter_min_top_conf)])
    if args.prefilter_max_top_conf is not None:
        cmd.extend(["--prefilter-max-top-conf", str(args.prefilter_max_top_conf)])
    if args.prefilter_free_action:
        cmd.append("--prefilter-free-action")
    if args.prefilter_max_to_call is not None:
        cmd.extend(["--prefilter-max-to-call", str(args.prefilter_max_to_call)])
    if args.prefilter_min_interaction_score is not None:
        cmd.extend(["--prefilter-min-interaction-score", str(args.prefilter_min_interaction_score)])
    if args.prefilter_max_interaction_score is not None:
        cmd.extend(["--prefilter-max-interaction-score", str(args.prefilter_max_interaction_score)])
    if args.active_min_targets:
        cmd.extend(["--active-min-targets", str(args.active_min_targets)])
    if args.active_min_positive_targets:
        cmd.extend(["--active-min-positive-targets", str(args.active_min_positive_targets)])
    if args.active_target != "delta_vs_rule":
        cmd.extend(["--active-target", args.active_target])
    if args.active_min_abs_target != 1e-9:
        cmd.extend(["--active-min-abs-target", str(args.active_min_abs_target)])
    for label in args.active_drop_label:
        cmd.extend(["--active-drop-label", label])
    if args.no_mirror:
        cmd.append("--no-mirror")
    if args.no_scan_persistent:
        cmd.append("--no-scan-persistent")
    return cmd


def _run_shard(args: argparse.Namespace, shard_idx: int, shard_output: Path) -> dict[str, Any]:
    cmd = _probe_cmd(args, shard_idx, shard_output)
    skipped = shard_output.exists() and not args.rerun_existing
    if skipped:
        return {
            "shard": shard_idx,
            "output": _rel(shard_output),
            "seed_offset": _shard_seed_offset(args, shard_idx),
            "bot_seed_base": _shard_bot_seed_base(args, shard_idx),
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
        "bot_seed_base": _shard_bot_seed_base(args, shard_idx),
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
        shard_entry["row_count"] = len(shard_data.get("rows", []))
        for row in shard_data.get("rows", []):
            payload["rows"].append(
                {
                    **row,
                    "shard": shard_entry["shard"],
                    "shard_seed_offset": shard_entry.get("seed_offset"),
                }
            )
    payload["shard_results"].append(shard_entry)
    payload["summary"] = _summarize(payload["rows"], payload.get("labels") or LABELS)


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
    parser.add_argument("--max-rows-per-shard", type=int, default=8)
    parser.add_argument("--max-scan-decisions", type=int, default=400)
    parser.add_argument("--stage", choices=["any", "preflop", "flop", "turn", "river"], default="any")
    parser.add_argument("--branch-scope", choices=["hand", "match"], default="hand")
    parser.add_argument("--max-branch-steps", type=int, default=5000)
    parser.add_argument("--min-unique-actions", type=int, default=2)
    parser.add_argument("--prefilter-rule-label", action="append", choices=LABELS, default=[])
    parser.add_argument("--prefilter-top-label", action="append", choices=LABELS, default=[])
    parser.add_argument("--prefilter-min-top-conf", type=float, default=0.0)
    parser.add_argument("--prefilter-max-top-conf", type=float)
    parser.add_argument("--prefilter-free-action", action="store_true")
    parser.add_argument("--prefilter-max-to-call", type=float)
    parser.add_argument("--prefilter-min-interaction-score", type=float)
    parser.add_argument("--prefilter-max-interaction-score", type=float)
    parser.add_argument("--active-target", choices=TARGET_FIELDS, default="delta_vs_rule")
    parser.add_argument("--active-drop-label", action="append", choices=LABELS, default=[])
    parser.add_argument("--active-min-targets", type=int, default=0)
    parser.add_argument("--active-min-positive-targets", type=int, default=0)
    parser.add_argument("--active-min-abs-target", type=float, default=1e-9)
    parser.add_argument("--seed-base", type=int, default=20260801)
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
        "mode": "multi_action_counterfactual_shards_v1",
        "target": "legal_mask_action_values_delta_vs_rule_regret_vs_mean",
        "version": _rel(_resolve(args.version)),
        "opponent": _rel(_resolve(args.opponent)),
        "labels": list(LABELS),
        "shards": args.shards,
        "workers": args.workers,
        "executor": "process_pool" if args.workers > 1 else "serial",
        "games_per_shard": args.games_per_shard,
        "max_rows_per_shard": args.max_rows_per_shard,
        "max_scan_decisions": args.max_scan_decisions,
        "seed_base": args.seed_base,
        "seed_offset": args.seed_offset,
        "seed_stride": args.seed_stride,
        "bot_seed_base": args.bot_seed_base,
        "bot_seed_stride": args.bot_seed_stride,
        "max_hands": args.max_hands,
        "filters": {
            "stage": args.stage,
            "branch_scope": args.branch_scope,
            "min_unique_actions": args.min_unique_actions,
            "prefilter_rule_label": list(args.prefilter_rule_label),
            "prefilter_top_label": list(args.prefilter_top_label),
            "prefilter_min_top_conf": args.prefilter_min_top_conf,
            "prefilter_max_top_conf": args.prefilter_max_top_conf,
            "prefilter_free_action": args.prefilter_free_action,
            "prefilter_max_to_call": args.prefilter_max_to_call,
            "prefilter_min_interaction_score": args.prefilter_min_interaction_score,
            "prefilter_max_interaction_score": args.prefilter_max_interaction_score,
            "active_target": args.active_target,
            "active_drop_label": list(args.active_drop_label),
            "active_min_targets": args.active_min_targets,
            "active_min_positive_targets": args.active_min_positive_targets,
            "active_min_abs_target": args.active_min_abs_target,
        },
        "shard_results": [],
        "rows": [],
        "summary": {},
    }
    _write(output, payload)

    shard_outputs = [shard_dir / f"shard_{idx:03d}.json" for idx in range(args.shards)]
    if args.workers <= 1:
        shard_entries: list[dict[str, Any] | None] = []
        for shard_idx, shard_output in enumerate(shard_outputs):
            if shard_output.exists() and not args.rerun_existing:
                print(f"shard {shard_idx + 1}/{args.shards}: reuse {_rel(shard_output)}")
            else:
                print(f"shard {shard_idx + 1}/{args.shards}: {_display_cmd(_probe_cmd(args, shard_idx, shard_output))}")
            shard_entries.append(_run_shard(args, shard_idx, shard_output))
    else:
        print(f"running {args.shards} multi-action shards with {args.workers} workers")
        shard_entries = [None] * args.shards
        with ProcessPoolExecutor(max_workers=max(1, args.workers)) as executor:
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
            f"rc={shard_entry['returncode']} rows={shard_entry.get('row_count', 0)} "
            f"merged={len(payload['rows'])}"
        )
        if shard_entry["returncode"] != 0 and not (ROOT / shard_entry["output"]).exists():
            break

    payload["summary"] = _summarize(payload["rows"], payload.get("labels") or LABELS)
    _write(output, payload)
    print(json.dumps(payload["summary"], indent=2))


if __name__ == "__main__":
    main()
