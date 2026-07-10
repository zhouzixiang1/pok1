#!/usr/bin/env python3
"""Run reproducible small/medium/large opponent-model scaling experiments."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import sys
from typing import Any


TOOLS = Path(__file__).resolve().parent
TRAINER = TOOLS / "train_opponent_multitask_net.py"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from audit_oppmodel_dataset import audit  # noqa: E402


DEFAULT_CONFIGS = (
    "small:128:64:48:32:64",
    "medium:256:128:96:64:128",
    "large:512:256:192:128:256",
)


def _parse_config(raw: str) -> dict[str, Any]:
    parts = raw.split(":")
    if len(parts) != 6:
        raise SystemExit(
            f"invalid config {raw!r}; expected name:hidden:latent:gru:cross:head"
        )
    name = parts[0]
    values = [int(value) for value in parts[1:]]
    if not name or any(value <= 0 for value in values):
        raise SystemExit(f"invalid config {raw!r}")
    return dict(zip(
        ("name", "hidden", "latent", "gru_hidden", "cross_hidden", "head_hidden"),
        (name, *values),
    ))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _model_matches(
    path: Path,
    *,
    config: dict[str, Any],
    seed: int,
    data_paths: dict[str, Path],
) -> bool:
    if not path.exists():
        return False
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))["meta"]
        model = meta["model"]
        training = meta["training"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return False
    for key in ("hidden", "latent", "gru_hidden", "cross_hidden", "head_hidden"):
        if int(model.get(key, -1)) != int(config[key]):
            return False
    if int(training.get("seed", -1)) != seed:
        return False
    if meta.get("response_encoder") != "separate_public_v1":
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
        output, config=config, seed=seed, data_paths=data_paths
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
        "--head-hidden", str(config["head_hidden"]),
        "--epochs", str(args.epochs),
        "--patience", str(args.patience),
        "--batch-size", str(args.batch_size),
        "--device", args.device,
        "--seed", str(seed),
    ]
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
    parser.add_argument("--min-value-train", type=int, default=500)
    parser.add_argument("--min-behavior-train", type=int, default=2000)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    data_dir = args.data_dir.resolve()
    out_dir = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    audit_report = audit(
        data_dir,
        min_value_rows=args.min_value_train,
        min_behavior_rows=args.min_behavior_train,
    )
    (out_dir / "dataset_audit.json").write_text(
        json.dumps(audit_report, indent=2, sort_keys=True), encoding="utf-8"
    )
    if not audit_report["passed"]:
        raise SystemExit("dataset audit failed; see dataset_audit.json")

    configs = [_parse_config(raw) for raw in (args.config or DEFAULT_CONFIGS)]
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
    if all(path.exists() for path in calibration_paths.values()):
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
            })

    config_summaries = []
    for config in configs:
        rows = [row for row in experiments if row["config"]["name"] == config["name"]]
        scores = [float(row["validation"]["selection_score"]) for row in rows]
        config_summaries.append({
            "config": config,
            "seeds": len(rows),
            "median_validation_score": statistics.median(scores),
            "mean_validation_score": statistics.mean(scores),
            "stdev_validation_score": statistics.pstdev(scores),
            "parameters": rows[0]["parameters"],
        })
    config_summaries.sort(key=lambda row: (
        row["median_validation_score"],
        row["stdev_validation_score"],
        row["parameters"],
    ))
    selected_config = config_summaries[0]["config"]
    selected_rows = [
        row for row in experiments
        if row["config"]["name"] == selected_config["name"]
    ]
    selected_median = config_summaries[0]["median_validation_score"]
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
        })
    representative = next(
        member for member in final_members if member["seed"] == selected_seed
    )
    ensemble_manifest = {
        "format": "opp_multitask_ensemble_v1",
        "selection_used_held_out": False,
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
        "schema_version": 2,
        "selection_used_held_out": False,
        "dataset_audit": str(out_dir / "dataset_audit.json"),
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
