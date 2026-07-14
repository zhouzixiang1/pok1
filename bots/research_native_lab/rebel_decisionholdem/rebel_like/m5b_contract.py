"""Frozen contracts for Route A1 M5b real-HUNL self-play learning.

M5b is deliberately offline.  It owns its solver, labels, networks and data;
it may import the Common national state and the frozen A1 M5a PBS contract,
but never an A2 blueprint or any Route B artifact.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

from ...common_contracts.constants import CONTRACT_VERSION
from .hunl_pbs import (
    HUNL_COMBO_ORDER,
    HUNL_COMBO_REGISTRY_SHA256,
    HUNL_PBS_SCHEMA,
    MAX_REBEL_ACTIONS,
)


M5B_CONFIG_SCHEMA = "route-a1-m5b-offline-rebel-loop-config-v1"
M5B_SAMPLE_SCHEMA = "route-a1-m5b-hunl-search-sample-v1"
M5B_DATASET_SCHEMA = "route-a1-m5b-content-addressed-dataset-v1"
M5B_MODEL_SCHEMA = "route-a1-m5b-value-policy-network-v1"
M5B_RUN_SCHEMA = "route-a1-m5b-offline-loop-run-v1"
M5B_ACTION_SCHEMA = "route-a1-m5b-nine-slot-common-action-abstraction-v1"
M5B_SPLIT_SCHEMA = "route-a1-m5b-public-family-seed-group-split-v1"
M5B_SOLVER_NAME = "alternating-linear-external-sampling-cfr-avg-v1"
M5B_VALUE_SEMANTICS = "posterior-normalized-private-continuation-v1"
M5B_CFV_SEMANTICS = "opponent-reach-unnormalized-omit-own-reach-v1"
M5B_Q_SEMANTICS = "posterior-normalized-forced-root-action-v1"

ACTION_SLOTS = (
    "fold",
    "check",
    "call",
    "raise_min",
    "raise_half_pot",
    "raise_pot",
    "raise_150pct_pot",
    "raise_exact_offtree",
    "allin",
)

# These are historical, already-published bytes.  M5b checks them before every
# generation/training run rather than silently inheriting a mutated M5a.
FROZEN_M5A = {
    "manifest_path": "manifests/milestone_m5a.json",
    "manifest_sha256": "4edede3c8a3cdef7d24176cb5e4b1ae9d5a8160ae315e99fd1fd028ffc7dd497",
    "artifact_path": "artifacts/m5a_exact_label_fixture.json",
    "artifact_sha256": "ae4e8eca65d2c99429f0a7f064abfac9f468347903ab3dd131959865c7ff8797",
    "artifact_body_sha256": "3c07efb96f256c2466fcd140a2967032ca1d0cf2edf474c540733d179f39387f",
    "source_snapshot_sha256": "941d18e36b39cb674c2cdc9da672858a0e013f077a5b3597e34ad069bdcb99ae",
}

FORBIDDEN_RUNTIME_PREFIXES = (
    "engine",
    "sever.bot_adapter",
    "bots.research_native_lab.rebel_decisionholdem.decisionholdem_like",
)


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _integer(value: object, *, minimum: int, label: str) -> int:
    if type(value) is not int or int(value) < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return int(value)


def _finite(value: object, *, minimum: float, label: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        raise ValueError(f"{label} must be finite")
    result = float(value)
    if result < minimum:
        raise ValueError(f"{label} must be >= {minimum}")
    return result


def route_root() -> Path:
    return Path(__file__).resolve().parents[1]


def verify_frozen_m5a(root: Path | None = None) -> dict[str, str]:
    root = route_root() if root is None else Path(root).resolve()
    observed: dict[str, str] = {}
    for name in ("manifest", "artifact"):
        path = root / str(FROZEN_M5A[f"{name}_path"])
        if not path.is_file():
            raise FileNotFoundError(f"frozen M5a {name} is missing: {path}")
        digest = sha256_file(path)
        expected = str(FROZEN_M5A[f"{name}_sha256"])
        if digest != expected:
            raise ValueError(
                f"frozen M5a {name} drifted: expected {expected}, got {digest}"
            )
        observed[name] = digest
    manifest = json.loads(
        (root / str(FROZEN_M5A["manifest_path"])).read_text(encoding="utf-8")
    )
    artifact = manifest.get("artifact")
    if not isinstance(artifact, dict):
        raise ValueError("frozen M5a manifest lost its artifact binding")
    for field in ("body_sha256", "source_snapshot_sha256"):
        if artifact.get(field) != FROZEN_M5A[f"artifact_{field}" if field == "body_sha256" else field]:
            raise ValueError(f"frozen M5a {field} binding drifted")
    return observed


def validate_config(payload: object) -> dict[str, Any]:
    """Strictly validate the preregistered M5b generation/training config."""

    if not isinstance(payload, dict):
        raise ValueError("M5b config must be an object")
    required = {
        "schema",
        "stage",
        "common_contract_version",
        "hunl_pbs_schema",
        "hunl_combo_order",
        "hunl_combo_registry_sha256",
        "historical_m5a",
        "independence",
        "action_abstraction",
        "solver",
        "self_play",
        "split",
        "network",
        "training",
        "metrics",
        "seeds",
        "runtime_binding",
        "resource_measurement",
        "pre_generation_gates",
        "claim_boundary",
    }
    if set(payload) != required:
        raise ValueError("M5b config top-level fields differ")
    if payload["schema"] != M5B_CONFIG_SCHEMA:
        raise ValueError("M5b config schema differs")
    if payload["stage"] != "M5b offline real-HUNL ReBeL-like closed loop":
        raise ValueError("M5b stage differs")
    if payload["common_contract_version"] != CONTRACT_VERSION:
        raise ValueError("M5b Common contract differs")
    if payload["hunl_pbs_schema"] != HUNL_PBS_SCHEMA:
        raise ValueError("M5b PBS schema differs")
    if payload["hunl_combo_order"] != HUNL_COMBO_ORDER:
        raise ValueError("M5b combo order differs")
    if payload["hunl_combo_registry_sha256"] != HUNL_COMBO_REGISTRY_SHA256:
        raise ValueError("M5b combo registry differs")
    if payload["historical_m5a"] != FROZEN_M5A:
        raise ValueError("M5b historical M5a binding differs")

    independence = payload["independence"]
    if independence != {
        "a1_solver_owned": True,
        "a1_labels_owned": True,
        "a2_blueprint_allowed_as_behavior": False,
        "a2_artifact_allowed_as_labels": False,
        "route_b_model_or_data_allowed": False,
        "top_level_engine_allowed": False,
        "legacy_adapter_allowed": False,
    }:
        raise ValueError("M5b independence boundary differs")

    abstraction = payload["action_abstraction"]
    if abstraction != {
        "schema": M5B_ACTION_SCHEMA,
        "max_actions": MAX_REBEL_ACTIONS,
        "slots": list(ACTION_SLOTS),
        "raise_semantics": "common_raise_to_stage_total_v1",
        "exact_offtree_injection": True,
        "nearest_translation": False,
    }:
        raise ValueError("M5b action abstraction differs")

    solver = payload["solver"]
    solver_fields = {
        "algorithm",
        "alternating_updates",
        "linear_weighting",
        "iterations_round0",
        "iterations_round1",
        "deals_per_iteration",
        "public_action_depth",
        "leaf_rollouts",
        "target_rollouts_per_hand",
        "record_root_q",
        "value_semantics",
        "cfv_semantics",
        "q_semantics",
    }
    if not isinstance(solver, dict) or set(solver) != solver_fields:
        raise ValueError("M5b solver fields differ")
    if (
        solver["algorithm"] != M5B_SOLVER_NAME
        or solver["alternating_updates"] is not True
        or solver["linear_weighting"] is not True
        or solver["record_root_q"] is not True
        or solver["value_semantics"] != M5B_VALUE_SEMANTICS
        or solver["cfv_semantics"] != M5B_CFV_SEMANTICS
        or solver["q_semantics"] != M5B_Q_SEMANTICS
    ):
        raise ValueError("M5b solver identity/semantics differ")
    for field, minimum in (
        ("iterations_round0", 2),
        ("iterations_round1", 2),
        ("deals_per_iteration", 1),
        ("public_action_depth", 1),
        ("leaf_rollouts", 1),
        ("target_rollouts_per_hand", 1),
    ):
        _integer(solver[field], minimum=minimum, label=f"solver.{field}")

    self_play = payload["self_play"]
    if not isinstance(self_play, dict) or set(self_play) != {
        "rounds",
        "hands_per_round",
        "max_recorded_roots_per_hand",
        "max_samples_per_round",
        "exploration_epsilon",
        "round0_behavior",
        "round0_leaf",
        "round1_behavior",
        "round1_leaf",
    }:
        raise ValueError("M5b self-play fields differ")
    if (
        self_play["rounds"] != 2
        or self_play["round0_behavior"] != "a1_uniform_policy_v0"
        or self_play["round0_leaf"] != "a1_terminal_rollout_v0"
        or self_play["round1_behavior"] != "a1_value_policy_net_v1"
        or self_play["round1_leaf"] != "a1_value_net_v1"
    ):
        raise ValueError("M5b two-round bootstrap contract differs")
    for field in ("hands_per_round", "max_recorded_roots_per_hand", "max_samples_per_round"):
        _integer(self_play[field], minimum=1, label=f"self_play.{field}")
    epsilon = _finite(
        self_play["exploration_epsilon"], minimum=0.0, label="exploration epsilon"
    )
    if epsilon > 0.25:
        raise ValueError("exploration epsilon is unexpectedly large")

    split = payload["split"]
    if not isinstance(split, dict) or set(split) != {
        "schema",
        "group_key",
        "route_domain_salt",
        "union_find_edge_namespaces",
        "duplicate_pbs_union_required",
        "three_way_disjoint_cluster_digests_required",
        "label_generation_after_split_only",
        "test_once_policy",
        "minimum_components",
        "train_basis_points",
        "validation_basis_points",
        "test_basis_points",
    }:
        raise ValueError("M5b split fields differ")
    if (
        split["schema"] != M5B_SPLIT_SCHEMA
        or split["group_key"]
        != "complete_public_family_suit_isomorphism_union_closure_v1"
        or split["route_domain_salt"] != "route-a1-m5b-only-v1"
        or split["union_find_edge_namespaces"]
        != [
            "same_canonical_public_family",
            "same_trajectory",
            "same_rollout_group",
            "same_augmentation_parent",
            "same_source_sample_checkpoint_identity",
            "duplicate_mathematical_pbs",
        ]
        or split["duplicate_pbs_union_required"] is not True
        or split["three_way_disjoint_cluster_digests_required"] is not True
        or split["label_generation_after_split_only"] is not True
        or split["test_once_policy"]
        != "sealed_after_candidate_threshold_and_model_freeze_v1"
    ):
        raise ValueError("M5b split identity differs")
    if split["minimum_components"] != {
        "train": 4,
        "validation": 2,
        "test": 2,
    }:
        raise ValueError("M5b minimum split component contract differs")
    basis = [
        _integer(split[name], minimum=1, label=f"split.{name}")
        for name in ("train_basis_points", "validation_basis_points", "test_basis_points")
    ]
    if sum(basis) != 10_000:
        raise ValueError("M5b split basis points must sum to 10,000")

    network = payload["network"]
    if not isinstance(network, dict) or set(network) != {
        "schema",
        "combo_embedding_dim",
        "global_hidden_dim",
        "trunk_hidden_dim",
        "layers",
        "activation",
        "normalization",
        "value_outputs",
        "policy_outputs",
    }:
        raise ValueError("M5b network fields differ")
    if (
        network["schema"] != M5B_MODEL_SCHEMA
        or network["activation"] != "gelu"
        or network["normalization"] != "layer_norm"
        or network["value_outputs"] != [2, 1326]
        or network["policy_outputs"] != [1326, 9]
    ):
        raise ValueError("M5b network output/architecture identity differs")
    for field in ("combo_embedding_dim", "global_hidden_dim", "trunk_hidden_dim", "layers"):
        _integer(network[field], minimum=1, label=f"network.{field}")

    training = payload["training"]
    if not isinstance(training, dict) or set(training) != {
        "device_required",
        "optimizer",
        "learning_rate",
        "weight_decay",
        "batch_size",
        "epochs_v1",
        "epochs_v2",
        "value_loss",
        "policy_loss",
        "policy_brier_aux_weight",
        "gradient_clip_norm",
        "checkpoint_every_epochs",
        "resume_semantics",
        "resume_probes",
        "scheduler",
        "mixed_precision_scaler",
        "deterministic_algorithms",
        "tf32_matmul",
        "tf32_cudnn",
        "dataloader_workers",
        "fallback_policy",
        "checkpoint_state",
    }:
        raise ValueError("M5b training fields differ")
    if (
        training["device_required"] != "cuda"
        or training["optimizer"] != "adam"
        or training["value_loss"] != "pointwise_huber_weighted"
        or training["policy_loss"]
        != "actor_projected_marginal_weighted_masked_cross_entropy_primary_kl_report_brier_aux_v1"
        or training["resume_semantics"]
        != "same_hardware_stack_non_epoch_full_state_bit_exact_v1"
        or training["resume_probes"] != 2
        or training["scheduler"] != "none"
        or training["mixed_precision_scaler"] != "disabled"
        or training["deterministic_algorithms"] is not True
        or training["tf32_matmul"] is not False
        or training["tf32_cudnn"] is not False
        or training["dataloader_workers"] != 0
        or training["fallback_policy"]
        != "cpu_deterministic_canonical_or_fail_closed_v1"
        or training["checkpoint_state"]
        != [
            "model",
            "optimizer",
            "scheduler",
            "scaler",
            "epoch",
            "batch_cursor",
            "global_step",
            "cpu_rng",
            "cuda_rng_all",
            "numpy_rng",
            "python_rng",
            "loader_generator_rng",
            "sampler_permutation",
            "config_dataset_runtime_digests",
            "loss_history",
        ]
    ):
        raise ValueError("M5b training identity differs")
    for field in ("batch_size", "epochs_v1", "epochs_v2", "checkpoint_every_epochs"):
        _integer(training[field], minimum=1, label=f"training.{field}")
    for field in (
        "learning_rate",
        "weight_decay",
        "policy_brier_aux_weight",
        "gradient_clip_norm",
    ):
        _finite(training[field], minimum=0.0, label=f"training.{field}")

    metrics = payload["metrics"]
    if not isinstance(metrics, dict) or set(metrics) != {
        "report",
        "scale_selection_uses",
        "forbidden_scale_selection",
        "finite_required",
        "minimum_legal_policy_mass",
        "maximum_weighted_zero_sum_abs_chips",
        "minimum_nonzero_value_std_chips",
        "minimum_q_action_separation_chips",
        "eligible_baselines",
        "policy_reference_upper_bound",
        "strongest_baseline_selection",
        "candidate_freeze",
        "cluster_bootstrap",
        "formal_relative_improvement_fraction",
        "formal_validation_metrics",
    }:
        raise ValueError("M5b metric fields differ")
    expected_reports = [
        "value_mae",
        "value_rmse",
        "weighted_zero_sum",
        "policy_cross_entropy",
        "policy_kl",
        "legal_policy_mass",
        "value_calibration",
        "constant_zero_baseline",
        "train_mean_baseline",
        "simple_showdown_equity_baseline",
        "street_public_family_mean_baseline",
        "range_blind_pot_equity_baseline",
        "nearest_public_family_baseline",
        "legal_uniform_policy_baseline",
        "train_action_marginal_policy_baseline",
    ]
    if (
        metrics["report"] != expected_reports
        or metrics["scale_selection_uses"]
        != ["training_loss", "validation_preregistered_metrics"]
        or metrics["forbidden_scale_selection"]
        != ["tcp_chips", "official_exe_chips", "test_metric_tuning"]
        or metrics["finite_required"] is not True
        or metrics["eligible_baselines"]
        != [
            "constant_zero",
            "train_mean",
            "simple_showdown_equity_broadcast",
            "street_public_family_mean",
            "range_blind_pot_equity",
            "nearest_public_family",
            "legal_uniform_policy",
            "train_action_marginal_policy",
        ]
        or metrics["policy_reference_upper_bound"]
        != "source_cfr_average_policy_table_not_eligible_baseline_v1"
        or metrics["strongest_baseline_selection"]
        != "validation_metric_then_frozen_before_candidate_test_v1"
        or metrics["candidate_freeze"]
        != "model_digest_threshold_margin_and_baseline_digest_before_test_once_v1"
        or metrics["formal_relative_improvement_fraction"] != 0.1
        or metrics["formal_validation_metrics"]
        != [
            "public_family_macro_value_mae",
            "public_family_macro_value_rmse",
            "private_hand_p90_absolute_error",
            "value_calibration_error",
            "actor_policy_cross_entropy",
            "actor_policy_kl",
        ]
    ):
        raise ValueError("M5b metric/reporting contract differs")
    bootstrap = metrics["cluster_bootstrap"]
    if not isinstance(bootstrap, dict) or set(bootstrap) != {
        "cluster_unit",
        "replicates",
        "confidence",
        "required_margin_mae_chips",
        "required_margin_policy_ce",
    }:
        raise ValueError("M5b cluster bootstrap fields differ")
    if bootstrap["cluster_unit"] != "union_find_split_cluster_v1":
        raise ValueError("M5b cluster bootstrap unit differs")
    _integer(bootstrap["replicates"], minimum=100, label="bootstrap replicates")
    confidence = _finite(bootstrap["confidence"], minimum=0.5, label="bootstrap confidence")
    if confidence >= 1.0:
        raise ValueError("bootstrap confidence must be below one")
    _finite(bootstrap["required_margin_mae_chips"], minimum=0.0, label="MAE margin")
    _finite(bootstrap["required_margin_policy_ce"], minimum=0.0, label="CE margin")
    _finite(metrics["minimum_legal_policy_mass"], minimum=0.99, label="legal mass")
    _finite(metrics["maximum_weighted_zero_sum_abs_chips"], minimum=0.0, label="zero sum")
    _finite(metrics["minimum_nonzero_value_std_chips"], minimum=0.0, label="value std")
    _finite(metrics["minimum_q_action_separation_chips"], minimum=0.0, label="Q separation")

    seeds = payload["seeds"]
    if not isinstance(seeds, dict) or set(seeds) != {
        "data_root",
        "solver",
        "policy_sampling",
        "chance",
        "split",
        "network_init",
        "training",
    }:
        raise ValueError("M5b seed fields differ")
    for name, value in seeds.items():
        _integer(value, minimum=0, label=f"seeds.{name}")
    if len(set(seeds.values())) != len(seeds):
        raise ValueError("M5b seed domains must be distinct")

    runtime = payload["runtime_binding"]
    if runtime != {
        "torch": "2.12.0+cu132",
        "cuda": "13.2",
        "cudnn": 92000,
        "driver": "595.71.05",
        "gpu_name": "NVIDIA GeForce RTX 4060 Laptop GPU",
        "gpu_uuid": "GPU-a1d260bf-f2f0-a451-d745-4c2989d07575",
        "gpu_total_memory_bytes": 8186822656,
        "deterministic_algorithms": True,
        "cublas_workspace_config": ":4096:8",
        "tf32_matmul": False,
        "tf32_cudnn": False,
    }:
        raise ValueError("M5b frozen runtime binding differs")

    resources = payload["resource_measurement"]
    if resources != {
        "schema": "route-a1-m5b-resource-measurement-contract-v1",
        "formal_receipt_bindings": [
            "config_sha256",
            "dataset_sha256",
            "model_tensor_sha256",
            "runtime_sha256",
        ],
        "gpu_seconds_semantics": (
            "single_cuda_device_synchronized_wall_envelope_not_utilization_time_v1"
        ),
        "rss_semantics": "linux_process_lifetime_high_water_rusage_v1",
        "training_fields": [
            "steps_per_second",
            "samples_per_second",
            "peak_cuda_allocated_bytes",
            "peak_cuda_reserved_bytes",
            "process_peak_rss_after_bytes",
            "gpu_seconds",
        ],
        "inference_fields": [
            "latency_median_ms",
            "latency_p95_ms",
            "public_states_per_second_at_median",
            "peak_cuda_allocated_bytes",
            "peak_cuda_reserved_bytes",
            "process_peak_rss_after_bytes",
            "gpu_seconds",
        ],
    }:
        raise ValueError("M5b resource measurement contract differs")

    gates = payload["pre_generation_gates"]
    if gates != {
        "must_pass_before_large_labels": True,
        "toy_exact_differential": True,
        "hunl_physical_combo_micro_oracles": [
            "fold",
            "showdown",
            "river_one_decision",
            "turn_allin_runout",
        ],
        "pbs_oracles": [
            "card_removal",
            "actor_bayes",
            "chance_conditioning",
            "exact_offtree_injection",
            "zero_evidence_rejection",
            "legal_masks",
            "projected_marginal_weighted_zero_sum",
        ],
        "sampled_deal_public_only_byte_identity": True,
        "model_influence_variants": [
            "normal",
            "zeroed",
            "shuffled",
            "value_policy_namespace_swapped",
        ],
        "model_influence_observables": [
            "leaf_values",
            "root_policy",
            "sampled_root_action",
        ],
    }:
        raise ValueError("M5b pre-generation gates differ")

    boundary = payload["claim_boundary"]
    if boundary != {
        "real_hunl_offline_labels": True,
        "value_policy_cuda_training": True,
        "iterative_offline_backfeed": True,
        "online_tcp_search": False,
        "official_exe_certified": False,
        "strength_claimed": False,
        "submission_bot_claimed": False,
    }:
        raise ValueError("M5b claim boundary differs")
    return json.loads(json.dumps(payload))


def load_config(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate M5b config key: {key}")
            result[key] = value
        return result

    payload = json.loads(
        raw,
        object_pairs_hook=reject_duplicates,
        parse_constant=lambda token: (_ for _ in ()).throw(
            ValueError(f"non-finite M5b config value: {token}")
        ),
    )
    validated = validate_config(payload)
    return validated


def config_digest(config: Mapping[str, object]) -> str:
    return sha256_bytes(canonical_bytes(validate_config(dict(config))))
