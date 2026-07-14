#!/usr/bin/env python3
"""Run an early-stop-only multi-seed v3 architecture scaling sweep."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
from typing import Any


SUMMARY_SCHEMA = "opponent_multitask_v3_scaling_summary_v1"
TRAINING_REPORT_SCHEMA = "opponent_multitask_training_report_v3"
SCALES = ("small", "medium", "large")
ENCODERS = ("none", "deep_set", "gru", "gru_moe")
TRAINER = Path(__file__).with_name("train_opponent_multitask_v3.py")
ROOT = Path(__file__).resolve().parents[3]


def _csv_values(raw: str, *, choices: tuple[str, ...], field: str) -> list[str]:
    values = []
    for value in str(raw).split(","):
        normalized = value.strip()
        if normalized and normalized not in values:
            values.append(normalized)
    invalid = [value for value in values if value not in choices]
    if not values or invalid:
        raise ValueError(f"invalid {field}: {invalid or values}")
    return values


def _seeds(raw: str) -> list[int]:
    values = []
    for value in str(raw).split(","):
        normalized = value.strip()
        if not normalized:
            continue
        try:
            seed = int(normalized)
        except ValueError as exc:
            raise ValueError(f"invalid seed: {normalized}") from exc
        if seed < 0:
            raise ValueError("seeds must be non-negative")
        if seed not in values:
            values.append(seed)
    if not values:
        raise ValueError("at least one seed is required")
    return values


def _slug(scale: str, encoder: str, seed: int) -> str:
    return f"{scale}_{encoder}_seed{seed}"


def build_training_command(
    args: argparse.Namespace,
    *,
    scale: str,
    encoder: str,
    seed: int,
    output_dir: Path,
    run_id: str,
) -> list[str]:
    command = [
        sys.executable,
        str(TRAINER),
        "--role-manifest",
        str(args.role_manifest.resolve()),
        "--ledger",
        str(args.ledger.resolve()),
        "--run-id",
        run_id,
        "--out-dir",
        str(output_dir),
        "--scale",
        scale,
        "--cross-encoder",
        encoder,
        "--moe-experts",
        str(args.moe_experts),
        "--dropout",
        str(args.dropout),
        "--epochs",
        str(args.epochs),
        "--patience",
        str(args.patience),
        "--batch-size",
        str(args.batch_size),
        "--learning-rate",
        str(args.learning_rate),
        "--weight-decay",
        str(args.weight_decay),
        "--hand-clip",
        str(args.hand_clip),
        "--tail-clip",
        str(args.tail_clip),
        "--match-clip",
        str(args.match_clip),
        "--device",
        str(args.device),
        "--seed",
        str(seed),
    ]
    if args.allow_incomplete_smoke:
        command.append("--allow-incomplete-smoke")
    return command


def validate_training_report(
    report: dict[str, Any],
    *,
    scale: str,
    encoder: str,
    seed: int,
    run_id: str,
) -> None:
    config = report.get("config") or {}
    model = report.get("model") or {}
    if (
        report.get("schema") != TRAINING_REPORT_SCHEMA
        or report.get("run_id") != run_id
        or report.get("opened_roles") != ["train", "early_stop"]
        or report.get("model_calibration_opened") is not False
        or report.get("policy_roles_opened") is not False
        or report.get("calibration_payload_sha256") is not None
        or report.get("calibration_summary") is not None
        or report.get("deployment_policy_value") is not False
        or report.get("strength_evidence") is not False
        or report.get("native_tcp_evaluated") is not False
        or model.get("scale") != scale
        or model.get("cross_encoder") != encoder
        or int(config.get("seed", -1)) != seed
    ):
        raise ValueError("training report violates the scaling role contract")
    score = (report.get("early_stop") or {}).get("selection_score")
    if not isinstance(score, (int, float)) or not float("-inf") < score < float("inf"):
        raise ValueError("training report has no finite early-stop score")


def _run_one(
    args: argparse.Namespace,
    *,
    root: Path,
    scale: str,
    encoder: str,
    seed: int,
) -> dict[str, Any]:
    slug = _slug(scale, encoder, seed)
    run_id = f"{args.run_id_prefix}-{slug}"
    output_dir = root / slug
    command = build_training_command(
        args,
        scale=scale,
        encoder=encoder,
        seed=seed,
        output_dir=output_dir,
        run_id=run_id,
    )
    environment = dict(os.environ)
    environment["PYTHONHASHSEED"] = str(seed)
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )
    log_path = root / f"{slug}.log"
    log_path.write_text(
        "COMMAND\n"
        + json.dumps(command)
        + "\n\nSTDOUT\n"
        + result.stdout
        + "\nSTDERR\n"
        + result.stderr,
        encoding="utf-8",
    )
    row = {
        "scale": scale,
        "encoder": encoder,
        "seed": seed,
        "slug": slug,
        "run_id": run_id,
        "output_dir": str(output_dir),
        "log": str(log_path),
        "returncode": result.returncode,
        "completed": False,
    }
    if result.returncode != 0:
        row["error"] = "trainer_failed"
        return row
    report_path = output_dir / "training_report.json"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        validate_training_report(
            report,
            scale=scale,
            encoder=encoder,
            seed=seed,
            run_id=run_id,
        )
        if (output_dir / "calibration.json").exists():
            raise ValueError("scaling run unexpectedly wrote calibration data")
    except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError) as exc:
        row["error"] = f"invalid_training_artifact: {exc}"
        return row
    early = report["early_stop"]
    row.update({
        "completed": True,
        "selection_score": float(early["selection_score"]),
        "best_epoch": int(report["best_epoch"]),
        "parameters": int(report["model"]["parameters"]),
        "checkpoint_sha256": report["checkpoint_sha256"],
        "role_manifest_sha256": report["role_manifest_sha256"],
        "source_collection_complete": report["source_collection_complete"],
        "incomplete_smoke": report["incomplete_smoke"],
        "match_direction_balanced_accuracy": early["value"][
            "match_delta_vs_rule"
        ]["direction_balanced_accuracy"],
        "response_balanced_accuracy": early["response"]["balanced_accuracy"],
        "response_nll": early["response"]["nll"],
    })
    return row


def summarize_runs(
    rows: list[dict[str, Any]], *, required_seeds: list[int]
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault((row["scale"], row["encoder"]), []).append(row)
    summaries = []
    for (scale, encoder), group in sorted(grouped.items()):
        complete = [row for row in group if row.get("completed")]
        observed_seeds = sorted(row["seed"] for row in complete)
        scores = [float(row["selection_score"]) for row in complete]
        all_seeds = observed_seeds == sorted(required_seeds)
        summaries.append({
            "scale": scale,
            "encoder": encoder,
            "requested_seeds": sorted(required_seeds),
            "completed_seeds": observed_seeds,
            "all_seeds_completed": all_seeds,
            "parameters": complete[0]["parameters"] if complete else None,
            "median_selection_score": statistics.median(scores) if scores else None,
            "mean_selection_score": statistics.mean(scores) if scores else None,
            "population_std_selection_score": statistics.pstdev(scores)
            if len(scores) > 1
            else 0.0 if scores else None,
            "worst_selection_score": max(scores) if scores else None,
        })
    eligible = [row for row in summaries if row["all_seeds_completed"]]
    best = min(
        eligible,
        key=lambda row: (
            row["median_selection_score"],
            row["worst_selection_score"],
            row["parameters"],
            row["scale"],
            row["encoder"],
        ),
        default=None,
    )
    return summaries, dict(best) if best is not None else None


def formal_selection_allowed(
    rows: list[dict[str, Any]],
    configurations: list[dict[str, Any]],
    best: dict[str, Any] | None,
    *,
    allow_incomplete_smoke: bool,
) -> bool:
    return bool(
        not allow_incomplete_smoke
        and best is not None
        and all(row.get("completed") is True for row in rows)
        and all(
            configuration["all_seeds_completed"]
            for configuration in configurations
        )
        and all(
            row.get("source_collection_complete") is True for row in rows
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role-manifest", required=True, type=Path)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--run-id-prefix", required=True)
    parser.add_argument("--scales", default="small,medium,large")
    parser.add_argument("--encoders", default="none,deep_set,gru,gru_moe")
    parser.add_argument("--seeds", default="101,211,307")
    parser.add_argument("--training-workers", type=int, default=1)
    parser.add_argument("--allow-incomplete-smoke", action="store_true")
    parser.add_argument("--moe-experts", type=int, default=4)
    parser.add_argument("--dropout", type=float, default=0.10)
    parser.add_argument("--epochs", type=int, default=100)
    parser.add_argument("--patience", type=int, default=15)
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--learning-rate", type=float, default=5.0e-4)
    parser.add_argument("--weight-decay", type=float, default=1.0e-5)
    parser.add_argument("--hand-clip", type=float, default=2_000.0)
    parser.add_argument("--tail-clip", type=float, default=2_000.0)
    parser.add_argument("--match-clip", type=float, default=2_000.0)
    parser.add_argument("--device", default="cuda")
    args = parser.parse_args(argv)
    try:
        scales = _csv_values(args.scales, choices=SCALES, field="scales")
        encoders = _csv_values(args.encoders, choices=ENCODERS, field="encoders")
        seeds = _seeds(args.seeds)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if not 1 <= args.training_workers <= 3:
        raise SystemExit("training-workers must be in [1, 3]")
    if not args.allow_incomplete_smoke and (
        len(seeds) < 3 or len(scales) * len(encoders) < 2
    ):
        raise SystemExit("formal scaling requires three seeds and two configurations")
    root = args.out_dir.resolve()
    if root.exists():
        raise SystemExit(f"output directory already exists: {root}")
    root.mkdir(parents=True)
    jobs = [
        (scale, encoder, seed)
        for scale in scales
        for encoder in encoders
        for seed in seeds
    ]
    rows = []
    with ThreadPoolExecutor(max_workers=args.training_workers) as executor:
        futures = {
            executor.submit(
                _run_one,
                args,
                root=root,
                scale=scale,
                encoder=encoder,
                seed=seed,
            ): (scale, encoder, seed)
            for scale, encoder, seed in jobs
        }
        for future in as_completed(futures):
            row = future.result()
            rows.append(row)
            print(
                f"[v3-scaling] {row['slug']} completed={row['completed']} "
                f"score={row.get('selection_score')}",
                flush=True,
            )
    rows.sort(key=lambda row: (row["scale"], row["encoder"], row["seed"]))
    configurations, best = summarize_runs(rows, required_seeds=seeds)
    formal_selection_eligible = formal_selection_allowed(
        rows,
        configurations,
        best,
        allow_incomplete_smoke=args.allow_incomplete_smoke,
    )
    payload = {
        "schema": SUMMARY_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "command": [sys.executable, *sys.argv],
        "role_manifest": str(args.role_manifest.resolve()),
        "ledger": str(args.ledger.resolve()),
        "run_id_prefix": args.run_id_prefix,
        "requested": {
            "scales": scales,
            "encoders": encoders,
            "seeds": seeds,
            "training_workers": args.training_workers,
        },
        "runs": rows,
        "configurations": configurations,
        "selection_eligible": formal_selection_eligible,
        "selected_configuration": best if formal_selection_eligible else None,
        "provisional_best_configuration": best,
        "selection_source": "opponent_disjoint_early_stop_only",
        "scaling_tool_sha256": hashlib.sha256(
            Path(__file__).read_bytes()
        ).hexdigest(),
        "model_calibration_opened": False,
        "policy_roles_opened": False,
        "deployment_policy_value": False,
        "strength_evidence": False,
    }
    (root / "scaling_summary.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    failed = [row for row in rows if not row["completed"]]
    if failed or best is None:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 1
    print(json.dumps({
        "out_dir": str(root),
        "runs": len(rows),
        "selection_eligible": formal_selection_eligible,
        "selected_configuration": best if formal_selection_eligible else None,
        "provisional_best_configuration": best,
        "strength_evidence": False,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
