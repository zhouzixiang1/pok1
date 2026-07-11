#!/usr/bin/env python3
"""Run an early-stop-only multi-seed v4 architecture scaling sweep."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
from typing import Any

from opponent_multitask_model_v4 import MODEL_FORMAT, MODEL_SCALES
from train_opponent_multitask_v4 import REPORT_SCHEMA as TRAINING_REPORT_SCHEMA


SUMMARY_SCHEMA = "opponent_multitask_v4_scaling_summary_v1"
SELECTION_METHOD = "lexicographic_componentwise_seed_median_then_worst_v1"
SELECTION_KEY_ORDER = (
    "match_flip_balanced_error",
    "match_balanced_error",
    "match_nll",
    "secondary_v3_value_response_score",
)
ENCODERS = ("none", "deep_set", "gru", "gru_moe")
TRAINER = Path(__file__).with_name("train_opponent_multitask_v4.py")
ROOT = Path(__file__).resolve().parents[3]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _is_cuda_device(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("cuda")


def _csv_values(
    raw: str, *, choices: tuple[str, ...], field: str
) -> list[str]:
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
    passthrough = (
        ("--moe-experts", args.moe_experts),
        ("--dropout", args.dropout),
        ("--epochs", args.epochs),
        ("--patience", args.patience),
        ("--minimum-improvement", args.minimum_improvement),
        ("--batch-size", args.batch_size),
        ("--learning-rate", args.learning_rate),
        ("--weight-decay", args.weight_decay),
        ("--gradient-clip-norm", args.gradient_clip_norm),
        ("--hand-clip", args.hand_clip),
        ("--tail-clip", args.tail_clip),
        ("--match-clip", args.match_clip),
        ("--mean-loss-weight", args.mean_loss_weight),
        ("--quantile-loss-weight", args.quantile_loss_weight),
        ("--match-ranking-weight", args.match_ranking_weight),
        ("--match-q20-ranking-weight", args.match_q20_ranking_weight),
        ("--ranking-margin", args.ranking_margin),
        ("--ranking-temperature", args.ranking_temperature),
        ("--outcome-loss-weight", args.outcome_loss_weight),
        ("--outcome-pairwise-weight", args.outcome_pairwise_weight),
        ("--outcome-pairwise-temperature", args.outcome_pairwise_temperature),
        ("--response-loss-weight", args.response_loss_weight),
        ("--response-size-weight", args.response_size_weight),
        ("--device", args.device),
    )
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
        "--seed",
        str(seed),
    ]
    for flag, value in passthrough:
        command.extend((flag, str(value)))
    if args.allow_incomplete_smoke:
        command.append("--allow-incomplete-smoke")
    return command


def _finite_selection_key(raw: Any) -> list[float]:
    if not isinstance(raw, list) or len(raw) != len(SELECTION_KEY_ORDER):
        raise ValueError("v4 training report has an invalid selection key")
    key = [float(value) for value in raw]
    if any(not math.isfinite(value) for value in key):
        raise ValueError("v4 training report has a non-finite selection key")
    return key


def validate_training_report(
    report: dict[str, Any],
    *,
    scale: str,
    encoder: str,
    seed: int,
    run_id: str,
    device: str,
) -> list[float]:
    config = report.get("config") or {}
    model = report.get("model") or {}
    early = report.get("early_stop") or {}
    environment = report.get("environment") or {}
    if (
        report.get("schema") != TRAINING_REPORT_SCHEMA
        or report.get("run_id") != run_id
        or report.get("opened_roles") != ["train", "early_stop"]
        or report.get("model_calibration_opened") is not False
        or report.get("policy_roles_opened") is not False
        or report.get("deployment_policy_value") is not False
        or report.get("strength_evidence") is not False
        or report.get("native_tcp_evaluated") is not False
        or model.get("format") != MODEL_FORMAT
        or model.get("scale") != scale
        or model.get("cross_encoder") != encoder
        or int(config.get("seed", -1)) != seed
        or environment.get("device") != device
        or early.get("selection_key_order") != list(SELECTION_KEY_ORDER)
        or early.get("selection_key_is_lexicographic") is not True
        or early.get("selection_score_is_strength_evidence") is not False
    ):
        raise ValueError("training report violates the v4 scaling role contract")
    return _finite_selection_key(early.get("selection_key"))


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
    row: dict[str, Any] = {
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
    try:
        report = json.loads(
            (output_dir / "training_report.json").read_text(encoding="utf-8")
        )
        selection_key = validate_training_report(
            report,
            scale=scale,
            encoder=encoder,
            seed=seed,
            run_id=run_id,
            device=str(args.device),
        )
        forbidden = (
            "calibration.json",
            "outcome_calibration.json",
            "policy_selection_result.json",
            "policy_gate_result.json",
        )
        if any((output_dir / name).exists() for name in forbidden):
            raise ValueError("scaling run wrote a protected downstream artifact")
    except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError) as exc:
        row["error"] = f"invalid_training_artifact: {exc}"
        return row
    row.update({
        "completed": True,
        "selection_key": selection_key,
        "selection_key_order": list(SELECTION_KEY_ORDER),
        "best_epoch": int(report["best_epoch"]),
        "parameters": int(report["model"]["parameters"]),
        "checkpoint_sha256": report["checkpoint_sha256"],
        "role_manifest_sha256": report["role_manifest_sha256"],
        "source_collection_complete": report["source_collection_complete"],
        "source_completed_passes": report.get("source_completed_passes"),
        "source_requested_passes": report.get("source_requested_passes"),
        "incomplete_smoke": report["incomplete_smoke"],
        "training_device": str((report.get("environment") or {}).get("device")),
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
        complete = sorted(
            (row for row in group if row.get("completed") is True),
            key=lambda row: int(row["seed"]),
        )
        observed_seeds = [int(row["seed"]) for row in complete]
        keys = [row["selection_key"] for row in complete]
        median_key = [
            statistics.median(key[index] for key in keys)
            for index in range(len(SELECTION_KEY_ORDER))
        ] if keys else None
        mean_key = [
            statistics.mean(key[index] for key in keys)
            for index in range(len(SELECTION_KEY_ORDER))
        ] if keys else None
        worst_key = [
            max(key[index] for key in keys)
            for index in range(len(SELECTION_KEY_ORDER))
        ] if keys else None
        summaries.append({
            "scale": scale,
            "encoder": encoder,
            "requested_seeds": sorted(required_seeds),
            "completed_seeds": observed_seeds,
            "all_seeds_completed": observed_seeds == sorted(required_seeds),
            "parameters": complete[0]["parameters"] if complete else None,
            "selection_key_order": list(SELECTION_KEY_ORDER),
            "median_selection_key": median_key,
            "mean_selection_key": mean_key,
            "worst_selection_key": worst_key,
        })
    eligible = [row for row in summaries if row["all_seeds_completed"]]
    best = min(
        eligible,
        key=lambda row: (
            tuple(row["median_selection_key"]),
            tuple(row["worst_selection_key"]),
            int(row["parameters"]),
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
        and len({str(row.get("scale")) for row in configurations}) >= 2
        and len({str(row.get("encoder")) for row in configurations}) >= 2
        and len({int(row.get("seed")) for row in rows}) >= 3
        and all(row.get("completed") is True for row in rows)
        and all(row.get("all_seeds_completed") is True for row in configurations)
        and all(row.get("source_collection_complete") is True for row in rows)
        and all(
            row.get("source_completed_passes") == 160
            and row.get("source_requested_passes") == 160
            for row in rows
        )
        and all(_is_cuda_device(row.get("training_device")) for row in rows)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role-manifest", required=True, type=Path)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--run-id-prefix", required=True)
    parser.add_argument("--scales", default="small,medium,large")
    parser.add_argument("--encoders", default=",".join(ENCODERS))
    parser.add_argument("--seeds", default="101,211,307")
    parser.add_argument("--training-workers", type=int, default=1)
    parser.add_argument("--allow-incomplete-smoke", action="store_true")
    parser.add_argument("--moe-experts", type=int, default=4)
    parser.add_argument("--dropout", type=float, default=0.10)
    parser.add_argument("--epochs", type=int, default=100)
    parser.add_argument("--patience", type=int, default=15)
    parser.add_argument("--minimum-improvement", type=float, default=1.0e-5)
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--learning-rate", type=float, default=5.0e-4)
    parser.add_argument("--weight-decay", type=float, default=1.0e-5)
    parser.add_argument("--gradient-clip-norm", type=float, default=1.0)
    parser.add_argument("--hand-clip", type=float, default=2_000.0)
    parser.add_argument("--tail-clip", type=float, default=2_000.0)
    parser.add_argument("--match-clip", type=float, default=2_000.0)
    parser.add_argument("--mean-loss-weight", type=float, default=1.0)
    parser.add_argument("--quantile-loss-weight", type=float, default=1.0)
    parser.add_argument("--match-ranking-weight", type=float, default=0.5)
    parser.add_argument("--match-q20-ranking-weight", type=float, default=0.25)
    parser.add_argument("--ranking-margin", type=float, default=100.0)
    parser.add_argument("--ranking-temperature", type=float, default=0.25)
    parser.add_argument("--outcome-loss-weight", type=float, default=2.0)
    parser.add_argument("--outcome-pairwise-weight", type=float, default=0.5)
    parser.add_argument("--outcome-pairwise-temperature", type=float, default=1.0)
    parser.add_argument("--response-loss-weight", type=float, default=1.0)
    parser.add_argument("--response-size-weight", type=float, default=0.25)
    parser.add_argument("--device", default="cuda")
    args = parser.parse_args(argv)
    try:
        scales = _csv_values(
            args.scales, choices=tuple(MODEL_SCALES), field="scales"
        )
        encoders = _csv_values(args.encoders, choices=ENCODERS, field="encoders")
        seeds = _seeds(args.seeds)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if not 1 <= args.training_workers <= 3:
        raise SystemExit("training-workers must be in [1, 3]")
    if not args.allow_incomplete_smoke and (
        len(seeds) < 3
        or len(scales) < 2
        or len(encoders) < 2
        or not _is_cuda_device(args.device)
    ):
        raise SystemExit(
            "formal v4 scaling requires three seeds, two scales, two encoders, "
            "and --device cuda"
        )
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
                f"[v4-scaling] {row['slug']} completed={row['completed']} "
                f"key={row.get('selection_key')}",
                flush=True,
            )
    rows.sort(key=lambda row: (row["scale"], row["encoder"], row["seed"]))
    configurations, best = summarize_runs(rows, required_seeds=seeds)
    formal = formal_selection_allowed(
        rows,
        configurations,
        best,
        allow_incomplete_smoke=args.allow_incomplete_smoke,
    )
    summary = {
        "schema": SUMMARY_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "command": [sys.executable, *sys.argv],
        "role_manifest": str(args.role_manifest.resolve()),
        "ledger": str(args.ledger.resolve()),
        "requested": {
            "scales": scales,
            "encoders": encoders,
            "seeds": seeds,
            "configurations": len(scales) * len(encoders),
            "device": str(args.device),
        },
        "selection_key_order": list(SELECTION_KEY_ORDER),
        "selection_method": SELECTION_METHOD,
        "scaling_tool_sha256": _sha256(Path(__file__).resolve()),
        "runs": rows,
        "configurations": configurations,
        "selection_eligible": formal,
        "selected_configuration": best if formal else None,
        "provisional_best_configuration": best,
        "model_calibration_opened": False,
        "policy_roles_opened": False,
        "source_collection_complete": bool(
            rows and all(row.get("source_collection_complete") is True for row in rows)
        ),
        "incomplete_smoke": args.allow_incomplete_smoke,
        "deployment_policy_value": False,
        "strength_evidence": False,
        "native_tcp_evaluated": False,
    }
    (root / "scaling_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({
        "out_dir": str(root),
        "runs": len(rows),
        "completed": sum(row.get("completed") is True for row in rows),
        "selection_eligible": formal,
        "selected_configuration": summary["selected_configuration"],
        "provisional_best_configuration": best,
        "strength_evidence": False,
    }, indent=2, sort_keys=True))
    return 0 if all(row.get("completed") is True for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
