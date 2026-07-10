#!/usr/bin/env python3
"""Run reproducible multi-scale opponent-model architecture experiments."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import sys
import time
from typing import Any


TOOLS = Path(__file__).resolve().parent
TRAINER = TOOLS / "train_opponent_multitask_net.py"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from audit_oppmodel_dataset import audit  # noqa: E402
from opp_multitask_runtime import OpponentMultiTaskRuntime  # noqa: E402


DEFAULT_CONFIGS = (
    "small_gru@gru:128:64:48:32:32:64",
    "small_moe@gru_moe:128:64:48:32:32:64",
    "small_set@deep_set:128:64:48:32:32:64",
    "small_tx@transformer:128:64:48:32:32:64",
    "medium_gru@gru:256:128:96:64:64:128",
    "medium_moe@gru_moe:256:128:96:64:64:128",
    "medium_set@deep_set:256:128:96:64:64:128",
    "medium_tx@transformer:256:128:96:64:64:128",
    "large_gru@gru:512:256:192:128:128:256",
    "large_moe@gru_moe:512:256:192:128:128:256",
    "large_set@deep_set:512:256:192:128:128:256",
    "large_tx@transformer:512:256:192:128:128:256",
    "xlarge_gru@gru:1024:512:256:256:256:512",
    "xlarge_moe@gru_moe:1024:512:256:256:256:512",
    "xlarge_set@deep_set:1024:512:256:256:256:512",
    "xlarge_tx@transformer:1024:512:256:256:256:512",
)


def _parse_config(
    raw: str, *, cross_transformer_heads: int = 4,
    cross_moe_experts: int = 4,
) -> dict[str, Any]:
    parts = raw.split(":")
    if len(parts) not in {6, 7}:
        raise SystemExit(
            f"invalid config {raw!r}; expected "
            "name:hidden:latent:gru:aggregate_cross:sequence_cross:head"
        )
    name_spec = parts[0]
    if "@" in name_spec:
        name, encoder = name_spec.rsplit("@", 1)
    else:
        name, encoder = name_spec, "gru"
    if encoder not in {"gru", "gru_moe", "deep_set", "transformer"}:
        raise SystemExit(f"invalid cross-hand encoder in {raw!r}: {encoder}")
    values = [int(value) for value in parts[1:]]
    if len(values) == 5:
        values.insert(4, values[3])
    if not name or any(value <= 0 for value in values):
        raise SystemExit(f"invalid config {raw!r}")
    config = dict(zip(
        (
            "name", "hidden", "latent", "gru_hidden", "cross_hidden",
            "cross_sequence_hidden", "head_hidden",
        ),
        (name, *values),
    ))
    config["cross_sequence_encoder"] = encoder
    config["cross_transformer_heads"] = int(cross_transformer_heads)
    config["cross_moe_experts"] = int(cross_moe_experts)
    if config["cross_transformer_heads"] <= 0:
        raise SystemExit("cross-transformer-heads must be positive")
    if (
        encoder == "transformer"
        and config["cross_sequence_hidden"] % config["cross_transformer_heads"]
    ):
        raise SystemExit(
            f"transformer sequence hidden size must be divisible by heads in {raw!r}"
        )
    if encoder == "gru_moe" and config["cross_moe_experts"] < 2:
        raise SystemExit("cross-moe-experts must be at least two")
    return config


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _training_recipe(args: argparse.Namespace) -> dict[str, float]:
    return {
        "match_ranking_weight": float(args.match_ranking_weight),
        "ranking_margin": float(args.ranking_margin),
        "ranking_temperature": float(args.ranking_temperature),
        "direction_score_weight": float(args.direction_score_weight),
    }


def _benchmark_runtime(path: Path, *, repeats: int) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    meta = payload.get("meta") or {}
    model_meta = meta.get("model") or {}
    runtime = OpponentMultiTaskRuntime(payload)
    state = [0.01] * int(meta.get("state_dim", 48))
    profile = [0.02] * int(meta.get("profile_dim", 12))
    history = [
        [0.03] * int(meta.get("hist_feat_dim", 15))
        for _ in range(int(model_meta.get("max_hist", 16)))
    ]
    cross_hand = [0.04] * int(meta.get("cross_hand_dim", 20))
    cross_sequence = [
        [0.05] * int(meta.get("cross_hand_sequence_dim", 16))
        for _ in range(int(model_meta.get("max_cross_hands", 32)))
    ]
    hero_action = [0.0] * int(meta.get("hero_action_dim", 10))
    if hero_action:
        hero_action[0] = 1.0

    def predict() -> None:
        values = runtime.predict_values(
            state, profile, history, cross_hand, 0, cross_sequence
        )
        response = runtime.predict_response(
            state, profile, history, cross_hand, hero_action, cross_sequence
        )
        if not values or not response:
            raise SystemExit(f"stdlib runtime benchmark failed for {path}")

    predict()
    timings = []
    for _ in range(max(1, int(repeats))):
        start = time.perf_counter()
        predict()
        timings.append((time.perf_counter() - start) * 1000.0)
    return {
        "repeats": len(timings),
        "mean_value_response_ms": statistics.fmean(timings),
        "max_value_response_ms": max(timings),
    }


def _model_matches(
    path: Path,
    *,
    config: dict[str, Any],
    seed: int,
    data_paths: dict[str, Path],
    args: argparse.Namespace,
) -> bool:
    if not path.exists():
        return False
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))["meta"]
        model = meta["model"]
        training = meta["training"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return False
    for key in (
        "hidden", "latent", "gru_hidden", "cross_hidden",
        "cross_sequence_hidden", "cross_transformer_heads",
        "cross_moe_experts", "head_hidden",
    ):
        if int(model.get(key, -1)) != int(config[key]):
            return False
    if model.get("cross_sequence_encoder", "gru") != config["cross_sequence_encoder"]:
        return False
    if int(training.get("seed", -1)) != seed:
        return False
    if training.get("trainer_sha256") != _sha256(TRAINER):
        return False
    for key, expected in _training_recipe(args).items():
        if float(training.get(key, float("nan"))) != expected:
            return False
    if meta.get("response_encoder") != "separate_public_v1":
        return False
    if meta.get("format") != "opp_multitask_gru_v2":
        return False
    if int(meta.get("rule_action_dim", -1)) != 6:
        return False
    manifests = training.get("data") or {}
    for name, data_path in data_paths.items():
        manifest = manifests.get(name) or {}
        if manifest.get("sha256") != _sha256(data_path):
            return False
    return True


def _run_training(
    *,
    config: dict[str, Any],
    seed: int,
    data_paths: dict[str, Path],
    output: Path,
    log_path: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    if args.resume and _model_matches(
        output, config=config, seed=seed, data_paths=data_paths, args=args
    ):
        return json.loads(output.read_text(encoding="utf-8"))
    command = [
        sys.executable,
        str(TRAINER),
        "--value-train", str(data_paths["value_train"]),
        "--value-val", str(data_paths["value_val"]),
        "--behavior-train", str(data_paths["behavior_train"]),
        "--behavior-val", str(data_paths["behavior_val"]),
        "--out", str(output),
        "--hidden", str(config["hidden"]),
        "--latent", str(config["latent"]),
        "--gru-hidden", str(config["gru_hidden"]),
        "--cross-hidden", str(config["cross_hidden"]),
        "--cross-sequence-hidden", str(config["cross_sequence_hidden"]),
        "--cross-sequence-encoder", config["cross_sequence_encoder"],
        "--cross-transformer-heads", str(config["cross_transformer_heads"]),
        "--cross-moe-experts", str(config["cross_moe_experts"]),
        "--match-ranking-weight", str(args.match_ranking_weight),
        "--ranking-margin", str(args.ranking_margin),
        "--ranking-temperature", str(args.ranking_temperature),
        "--direction-score-weight", str(args.direction_score_weight),
        "--head-hidden", str(config["head_hidden"]),
        "--epochs", str(args.epochs),
        "--patience", str(args.patience),
        "--batch-size", str(args.batch_size),
        "--device", args.device,
        "--seed", str(seed),
    ]
    if not args.allow_missing_cross_hand_sequence:
        command.append("--require-cross-hand-sequence")
    if "value_held_out" in data_paths:
        command.extend([
            "--value-held-out", str(data_paths["value_held_out"]),
            "--behavior-held-out", str(data_paths["behavior_held_out"]),
        ])
    if "value_calibration" in data_paths:
        command.extend([
            "--value-calibration", str(data_paths["value_calibration"]),
            "--behavior-calibration", str(data_paths["behavior_calibration"]),
        ])
    with log_path.open("w", encoding="utf-8") as log:
        completed = subprocess.run(
            command,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
    if completed.returncode != 0:
        raise SystemExit(
            f"training failed for {config['name']} seed={seed}; see {log_path}"
        )
    return json.loads(output.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--config", action="append", default=[])
    parser.add_argument("--seeds", default="101,211,307")
    parser.add_argument("--epochs", type=int, default=80)
    parser.add_argument("--patience", type=int, default=12)
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--cross-transformer-heads", type=int, default=4)
    parser.add_argument("--cross-moe-experts", type=int, default=4)
    parser.add_argument("--match-ranking-weight", type=float, default=0.5)
    parser.add_argument("--ranking-margin", type=float, default=100.0)
    parser.add_argument("--ranking-temperature", type=float, default=0.1)
    parser.add_argument("--direction-score-weight", type=float, default=0.5)
    parser.add_argument("--runtime-benchmark-repeats", type=int, default=3)
    parser.add_argument("--max-stdlib-runtime-ms", type=float, default=5000.0)
    parser.add_argument("--min-value-train", type=int, default=500)
    parser.add_argument("--min-behavior-train", type=int, default=2000)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--allow-missing-cross-hand-sequence", action="store_true")
    parser.add_argument("--allow-missing-calibration", action="store_true")
    args = parser.parse_args()
    if args.runtime_benchmark_repeats <= 0:
        raise SystemExit("runtime-benchmark-repeats must be positive")
    if args.max_stdlib_runtime_ms <= 0:
        raise SystemExit("max-stdlib-runtime-ms must be positive")
    if args.match_ranking_weight < 0 or args.direction_score_weight < 0:
        raise SystemExit("ranking and direction score weights must be non-negative")
    if args.ranking_margin < 0 or args.ranking_temperature <= 0:
        raise SystemExit("ranking margin must be non-negative and temperature positive")

    data_dir = args.data_dir.resolve()
    out_dir = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    audit_report = audit(
        data_dir,
        min_value_rows=args.min_value_train,
        min_behavior_rows=args.min_behavior_train,
        require_cross_hand_sequence=not args.allow_missing_cross_hand_sequence,
    )
    (out_dir / "dataset_audit.json").write_text(
        json.dumps(audit_report, indent=2, sort_keys=True), encoding="utf-8"
    )
    if not audit_report["passed"]:
        raise SystemExit("dataset audit failed; see dataset_audit.json")

    configs = [
        _parse_config(
            raw,
            cross_transformer_heads=args.cross_transformer_heads,
            cross_moe_experts=args.cross_moe_experts,
        )
        for raw in (args.config or DEFAULT_CONFIGS)
    ]
    seeds = [int(value) for value in args.seeds.split(",") if value.strip()]
    if not seeds:
        raise SystemExit("at least one seed is required")
    selection_data = {
        "value_train": data_dir / "cf_train.jsonl",
        "value_val": data_dir / "cf_val.jsonl",
        "behavior_train": data_dir / "opponent_actions_train.jsonl",
        "behavior_val": data_dir / "opponent_actions_val.jsonl",
    }
    held_out_data = {
        "value_held_out": data_dir / "cf_held_out.jsonl",
        "behavior_held_out": data_dir / "opponent_actions_held_out.jsonl",
    }
    calibration_paths = {
        "value_calibration": data_dir / "cf_calibration.jsonl",
        "behavior_calibration": data_dir / "opponent_actions_calibration.jsonl",
    }
    calibration_exists = [path.exists() for path in calibration_paths.values()]
    if any(calibration_exists) and not all(calibration_exists):
        raise SystemExit("calibration split is incomplete")
    if not all(calibration_exists) and not args.allow_missing_calibration:
        raise SystemExit(
            "calibration split is required; freeze the collection first or use "
            "--allow-missing-calibration for a legacy diagnostic"
        )
    if all(calibration_exists):
        held_out_data.update(calibration_paths)
    experiments = []
    for config in configs:
        for seed in seeds:
            stem = f"{config['name']}_seed{seed}"
            output = out_dir / f"{stem}.json"
            payload = _run_training(
                config=config,
                seed=seed,
                data_paths=selection_data,
                output=output,
                log_path=out_dir / f"{stem}.log",
                args=args,
            )
            experiments.append({
                "config": config,
                "seed": seed,
                "model": str(output),
                "model_sha256": _sha256(output),
                "parameters": payload["meta"]["model"]["parameters"],
                "validation": payload["meta"]["validation"],
                "stdlib_runtime": _benchmark_runtime(
                    output, repeats=args.runtime_benchmark_repeats
                ),
            })

    config_summaries = []
    for config in configs:
        rows = [row for row in experiments if row["config"]["name"] == config["name"]]
        scores = [float(row["validation"]["selection_score"]) for row in rows]
        max_member_runtime = max(
            row["stdlib_runtime"]["max_value_response_ms"] for row in rows
        )
        estimated_ensemble_runtime = sum(
            row["stdlib_runtime"]["max_value_response_ms"] for row in rows
        )
        config_summaries.append({
            "config": config,
            "seeds": len(rows),
            "median_validation_score": statistics.median(scores),
            "mean_validation_score": statistics.mean(scores),
            "stdev_validation_score": statistics.pstdev(scores),
            "parameters": rows[0]["parameters"],
            "median_stdlib_runtime_ms": statistics.median(
                row["stdlib_runtime"]["mean_value_response_ms"]
                for row in rows
            ),
            "max_member_stdlib_runtime_ms": max_member_runtime,
            "estimated_ensemble_stdlib_runtime_ms": estimated_ensemble_runtime,
            "runtime_eligible": (
                estimated_ensemble_runtime <= args.max_stdlib_runtime_ms
            ),
        })
    candidate_summary_path = out_dir / "architecture_candidates.json"
    candidate_summary_path.write_text(
        json.dumps({
            "schema_version": 1,
            "selection_used_held_out": False,
            "max_stdlib_runtime_ms": args.max_stdlib_runtime_ms,
            "runtime_benchmark_repeats": args.runtime_benchmark_repeats,
            "training_recipe": _training_recipe(args),
            "dataset_audit": str(out_dir / "dataset_audit.json"),
            "experiments": experiments,
            "config_summaries": config_summaries,
        }, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    eligible_summaries = [
        row for row in config_summaries
        if row["runtime_eligible"]
    ]
    if not eligible_summaries:
        raise SystemExit(
            "no architecture satisfies the stdlib runtime budget; "
            "see architecture_candidates.json"
        )
    eligible_summaries.sort(key=lambda row: (
        row["median_validation_score"],
        row["stdev_validation_score"],
        row["parameters"],
    ))
    config_summaries.sort(key=lambda row: (
        not row["runtime_eligible"],
        row["median_validation_score"],
        row["stdev_validation_score"],
        row["parameters"],
    ))
    selected_config = eligible_summaries[0]["config"]
    selected_rows = [
        row for row in experiments
        if row["config"]["name"] == selected_config["name"]
    ]
    selected_median = eligible_summaries[0]["median_validation_score"]
    selected_row = min(
        selected_rows,
        key=lambda row: abs(float(row["validation"]["selection_score"]) - selected_median),
    )
    selected_seed = int(selected_row["seed"])

    # The architecture and complete seed ensemble are frozen before held-out
    # paths are passed to the trainer. Every deterministic rerun must reproduce
    # its selection-only validation score.
    final_members = []
    for row in selected_rows:
        seed = int(row["seed"])
        final_path = out_dir / f"selected_{selected_config['name']}_seed{seed}.json"
        final_payload = _run_training(
            config=selected_config,
            seed=seed,
            data_paths={**selection_data, **held_out_data},
            output=final_path,
            log_path=out_dir / f"selected_{selected_config['name']}_seed{seed}.log",
            args=args,
        )
        final_validation = float(final_payload["meta"]["validation"]["selection_score"])
        original_validation = float(row["validation"]["selection_score"])
        if abs(final_validation - original_validation) > 1e-8:
            raise SystemExit(
                f"deterministic final rerun changed validation score for seed {seed}"
            )
        final_members.append({
            "seed": seed,
            "model": str(final_path),
            "model_sha256": _sha256(final_path),
            "validation": final_payload["meta"]["validation"],
            "held_out": final_payload["meta"]["held_out"],
            "stdlib_runtime": _benchmark_runtime(
                final_path, repeats=args.runtime_benchmark_repeats
            ),
        })
    representative = next(
        member for member in final_members if member["seed"] == selected_seed
    )
    final_ensemble_runtime_ms = sum(
        member["stdlib_runtime"]["max_value_response_ms"]
        for member in final_members
    )
    if final_ensemble_runtime_ms > args.max_stdlib_runtime_ms:
        raise SystemExit(
            "selected final ensemble exceeds the stdlib runtime budget"
        )
    ensemble_manifest = {
        "format": "opp_multitask_ensemble_v1",
        "selection_used_held_out": False,
        "max_stdlib_runtime_ms": args.max_stdlib_runtime_ms,
        "training_recipe": _training_recipe(args),
        "estimated_ensemble_stdlib_runtime_ms": final_ensemble_runtime_ms,
        "config": selected_config,
        "std_multiplier": 1.0,
        "members": [
            {
                "seed": member["seed"],
                "model": member["model"],
                "sha256": member["model_sha256"],
            }
            for member in final_members
        ],
    }
    ensemble_manifest_path = out_dir / "selected_ensemble.json"
    ensemble_manifest_path.write_text(
        json.dumps(ensemble_manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    summary = {
        "schema_version": 3,
        "selection_used_held_out": False,
        "max_stdlib_runtime_ms": args.max_stdlib_runtime_ms,
        "runtime_benchmark_repeats": args.runtime_benchmark_repeats,
        "training_recipe": _training_recipe(args),
        "dataset_audit": str(out_dir / "dataset_audit.json"),
        "architecture_candidates": str(candidate_summary_path),
        "experiments": experiments,
        "config_summaries": config_summaries,
        "selected": {
            "config": selected_config,
            "seed": selected_seed,
            "model": representative["model"],
            "model_sha256": representative["model_sha256"],
            "validation": representative["validation"],
            "held_out": representative["held_out"],
            "ensemble_manifest": str(ensemble_manifest_path),
            "members": final_members,
        },
    }
    summary_path = out_dir / "sweep_summary.json"
    summary_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps(summary["selected"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
