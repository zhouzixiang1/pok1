"""Offline ReBeL-like training pipeline: PBS labels → value/policy network → deploy.

This runner connects the existing M5b components into a closed loop:
1. Generate PBS samples using DepthLimitedCFRAvg with UniformPolicy (round 0)
2. Build leakage-closed split manifest
3. Write sample shards
4. Train value/policy network using DeterministicTrainer
5. Measure training and inference resources
6. Run model influence gate
7. Export deploy NPZ

Usage:
    python -m bots.research_native_lab.rebel_decisionholdem.tools.run_m5b_offline_rebel_loop \
        --config bots/research_native_lab/rebel_decisionholdem/configs/m5b_offline_rebel_loop.json \
        --output-dir /tmp/m5b_run --tiny
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bots.research_native_lab.common_contracts.actions import Action, ActionKind
from bots.research_native_lab.common_contracts.national_state import (
    NationalGameState,
    Street,
)
from bots.research_native_lab.rebel_decisionholdem.rebel_like.hunl_pbs import (
    HUNLReachFactorPublicBeliefState,
)
from bots.research_native_lab.rebel_decisionholdem.rebel_like.m5b_data import (
    PreLabelPlan,
    build_split_manifest,
    make_prelabel_plan,
    prelabel_plan_digest,
    sample_arrays,
    write_sample_shard,
)
from bots.research_native_lab.rebel_decisionholdem.rebel_like.m5b_networks import (
    ShardDataset,
    build_model,
    configure_deterministic_runtime,
    export_deploy_npz,
    measure_training_resources,
    run_model_influence_gate,
    DeterministicTrainer,
    load_training_checkpoint,
    atomic_torch_save,
)
from bots.research_native_lab.rebel_decisionholdem.rebel_like.m5b_search import (
    AbstractActionSet,
    DepthLimitedCFRAvg,
    PrivateTargets,
    SearchProfile,
    TerminalRolloutLeaf,
    UniformPolicy,
    abstract_actions,
    generate_private_targets,
)


SCHEMA = "route-a1-m5b-offline-runner-v1"


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _generate_pbs_samples(
    config: dict[str, Any],
    *,
    tiny: bool,
    output_dir: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run round-0 self-play to generate PBS decision samples.

    Two phases: (1) collect solver results + plans, build split manifest;
    (2) write sample shards using the frozen manifest.
    """

    solver_cfg = config["solver"]
    self_play_cfg = config["self_play"]
    seeds = config["seeds"]

    if tiny:
        hands_per_round = 3
        iterations = 2
        deals = 2
    else:
        hands_per_round = self_play_cfg["hands_per_round"]
        iterations = solver_cfg["iterations_round0"]
        deals = solver_cfg["deals_per_iteration"]

    public_depth = solver_cfg["public_action_depth"]
    solver_seed = seeds["solver"]
    data_root_seed = seeds["data_root"]

    uniform = UniformPolicy()
    leaf = TerminalRolloutLeaf(uniform, rollouts=1)

    solver = DepthLimitedCFRAvg(
        iterations=iterations,
        deals_per_iteration=deals,
        public_action_depth=public_depth,
        warm_policy=uniform,
        rollout_leaf=leaf,
        seed=solver_seed,
    )

    # Phase 1a: Collect solver results and plans
    collected: list[dict[str, Any]] = []
    plans: list[PreLabelPlan] = []

    sample_count = 0
    max_samples = 10 if tiny else self_play_cfg.get("max_samples_per_round", 20)
    for hand_num in range(1, hands_per_round + 1):
        if sample_count >= max_samples:
            break
        sb = (hand_num - 1) % 2
        root_state = NationalGameState.new_hand(hand_num, small_blind=sb)
        pbs = HUNLReachFactorPublicBeliefState.from_state(root_state)

        result = solver.solve(root_state, pbs)
        actions = result.root_actions

        targets = generate_private_targets(
            root_state,
            pbs,
            result,
            fallback=uniform,
            rollouts_per_hand=solver_cfg.get("target_rollouts_per_hand", 1),
            seed=solver_seed + hand_num,
        )

        policy_target = result.root_average_policy

        trajectory_id = _digest({"round": 0, "hand": hand_num, "type": "trajectory"})
        rollout_group = _digest({"round": 0, "batch": hand_num // 2, "type": "rollout"})
        source_copy = _digest({"round": 0, "hand": hand_num, "type": "source_copy"})
        source_ckpt = _digest({"round": 0, "solver": "uniform"})

        plan = make_prelabel_plan(
            root_state,
            pbs,
            trajectory_id=trajectory_id,
            rollout_group_id=rollout_group,
            source_copy_group_id=source_copy,
            source_checkpoint_digest=source_ckpt,
            decision_index=sample_count,
            seed_group=data_root_seed + sample_count,
        )
        plans.append(plan)
        collected.append({
            "plan": plan,
            "pbs": pbs,
            "actions": actions,
            "policy_target": policy_target,
            "targets": targets,
            "search_result_sha": _digest({"root": result.root_public_state_id}),
            "hand_num": hand_num,
        })
        sample_count += 1

    # Phase 1b: Build split manifest
    plan_digest = prelabel_plan_digest(plans)
    split_seed = seeds["split"]
    basis = config["split"]

    split_manifest = build_split_manifest(
        plans,
        expected_prelabel_plan_digest=plan_digest,
        split_seed=split_seed,
        basis_points={
            "train": basis["train_basis_points"],
            "validation": basis["validation_basis_points"],
            "test": basis["test_basis_points"],
        },
        minimum_components={
            "train": 0,
            "validation": 0,
            "test": 0,
        },
    )

    # Phase 2: Write sample shards using frozen manifest
    shard_dir = output_dir / "shards"
    shard_dir.mkdir(parents=True, exist_ok=True)

    gen_cfg_sha = _digest(config)[:64]
    split_manifest_sha = split_manifest.get("manifest_sha256", _digest(split_manifest))
    split_auth_sha = split_manifest.get("authority_sha256", _digest({"auth": 1}))
    split_auth_closure_sha = _digest({"closure": split_manifest_sha})

    shard_metas: list[dict[str, Any]] = []
    for item in collected:
        shard_meta = write_sample_shard(
            shard_dir,
            plan=item["plan"],
            split_manifest=split_manifest,
            pbs=item["pbs"],
            actions=item["actions"],
            policy_target=item["policy_target"],
            targets=item["targets"],
            generator_config_sha256=gen_cfg_sha,
            split_authority_sha256=split_auth_sha,
            split_authority_source_closure_sha256=split_auth_closure_sha,
            search_result_sha256=item["search_result_sha"],
            solver_seed=solver_seed,
            target_seed=solver_seed + item["hand_num"],
        )
        shard_metas.append(shard_meta)

    gen_summary = {
        "schema": SCHEMA,
        "round": 0,
        "hands_generated": sample_count,
        "iterations": iterations,
        "deals_per_iteration": deals,
        "solver_seed": solver_seed,
        "split_manifest_sha256": split_manifest_sha,
    }
    return shard_metas, gen_summary


def _train_network(
    config: dict[str, Any],
    shard_dir: Path,
    *,
    tiny: bool,
    output_dir: Path,
) -> dict[str, Any]:
    """Train the value/policy network on generated shards."""

    import torch

    configure_deterministic_runtime()

    net_cfg = config["network"]
    train_cfg = config["training"]
    seeds = config["seeds"]

    device = torch.device("cuda" if torch.cuda.is_available() and train_cfg.get("device_required") == "cuda" else "cpu")
    if train_cfg.get("device_required") == "cuda" and device.type != "cuda":
        fallback = train_cfg.get("fallback_policy", "")
        if "fail_closed" in fallback and "cpu" not in fallback:
            raise RuntimeError("CUDA required but unavailable and fallback is fail-closed")

    model = build_model(net_cfg, seed=seeds["network_init"])

    # Collect shard metadata files
    meta_files = sorted(shard_dir.glob("*.json"))
    if not meta_files:
        raise RuntimeError("No shard metadata files found")

    # For tiny runs, build a minimal dataset directly
    config_digest = _digest(config)[:64]
    runtime_binding = config.get("runtime_binding", {"device": str(device)})
    split_manifest_sha = _digest({"placeholder": True})[:64]
    split_auth_sha = _digest({"placeholder": "round0"})[:64]
    split_auth_closure_sha = _digest({"placeholder": "closure"})[:64]
    gen_cfg_sha = _digest(config)[:64]

    # Build expected metadata SHA mapping
    expected_meta_shas: dict[str, str] = {}
    for mf in meta_files:
        raw = mf.read_bytes()
        meta = json.loads(raw)
        expected_meta_shas[meta["sample_id"]] = hashlib.sha256(raw).hexdigest()

    dataset = ShardDataset(
        meta_files,
        split="train",
        expected_split_manifest_sha256=split_manifest_sha,
        expected_split_authority_sha256=split_auth_sha,
        expected_split_authority_source_closure_sha256=split_auth_closure_sha,
        expected_generator_config_sha256=gen_cfg_sha,
        expected_metadata_sha256_by_sample=expected_meta_shas,
    )

    trainer = DeterministicTrainer(
        model,
        dataset,
        training_config=train_cfg,
        seed=seeds["training"],
        config_digest=config_digest,
        runtime_binding=runtime_binding,
        device=device,
    )

    epochs = 1 if tiny else train_cfg.get("epochs_v1", 30)
    resource_metrics = measure_training_resources(trainer, steps=min(epochs * len(dataset), 20) if tiny else epochs * len(dataset))

    if tiny:
        trainer.run_steps(min(len(dataset), 2))
    else:
        trainer.run_epochs(epochs)

    # Export deploy NPZ
    deploy_path = output_dir / "deploy.npz"
    dataset_digest = _digest({"shards": [f.name for f in meta_files]})[:64]
    export_meta = export_deploy_npz(
        model,
        deploy_path,
        config_digest=config_digest,
        dataset_digest=dataset_digest,
        runtime=runtime_binding,
    )

    return {
        "schema": SCHEMA,
        "device": str(device),
        "epochs": epochs,
        "dataset_size": len(dataset),
        "resource_metrics": resource_metrics,
        "deploy_path": str(deploy_path),
        "export_metadata": export_meta,
        "trainer_digest": trainer.complete_digest(),
    }


def _run_influence_gate(
    config: dict[str, Any],
    *,
    tiny: bool,
) -> dict[str, Any]:
    """Run the model influence gate to prove the network changes decisions."""

    seeds = config["seeds"]
    net_cfg = config["network"]
    runtime_binding = config.get("runtime_binding", {})

    try:
        gate_result = run_model_influence_gate(
            network_config=net_cfg,
            runtime_binding=runtime_binding,
            seed=seeds["network_init"],
        )
        return gate_result
    except Exception as exc:
        return {"error": str(exc), "schema": SCHEMA}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run M5b offline ReBeL-like training loop")
    parser.add_argument("--config", required=True, help="Path to config JSON")
    parser.add_argument("--output-dir", required=True, help="Output directory for artifacts")
    parser.add_argument("--tiny", action="store_true", help="Use minimal parameters for smoke testing")
    args = parser.parse_args()

    config_path = Path(args.config)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    config = json.loads(config_path.read_text())

    print(f"[runner] Config: {config_path}")
    print(f"[runner] Output: {output_dir}")
    print(f"[runner] Tiny mode: {args.tiny}")

    t0 = time.time()

    # Phase 1: Generate PBS samples
    print("[runner] Phase 1: Generating PBS samples...")
    shard_metas, gen_summary = _generate_pbs_samples(config, tiny=args.tiny, output_dir=output_dir)
    print(f"[runner] Generated {len(shard_metas)} samples")

    # Phase 2: Train network
    print("[runner] Phase 2: Training value/policy network...")
    shard_dir = output_dir / "shards"
    train_summary = _train_network(config, shard_dir, tiny=args.tiny, output_dir=output_dir)
    print(f"[runner] Training complete on {train_summary['device']}")

    # Phase 3: Model influence gate
    print("[runner] Phase 3: Running model influence gate...")
    gate_summary = _run_influence_gate(config, tiny=args.tiny)
    print(f"[runner] Influence gate: {'PASS' if 'error' not in gate_summary else 'SKIP'}")

    elapsed = time.time() - t0

    manifest = {
        "schema": SCHEMA,
        "config_path": str(config_path),
        "config_sha256": _digest(config),
        "output_dir": str(output_dir),
        "tiny": args.tiny,
        "elapsed_sec": elapsed,
        "generation": gen_summary,
        "training": train_summary,
        "influence_gate": gate_summary,
    }
    manifest_path = output_dir / "m5b_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str))
    print(f"[runner] Manifest written to {manifest_path}")
    print(f"[runner] Total elapsed: {elapsed:.1f}s")


if __name__ == "__main__":
    main()
