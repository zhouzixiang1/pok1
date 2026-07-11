"""Immutable stdlib runtime budget for protected v4 ensembles.

The benchmark deliberately models the largest native decision path: one value
forward, one calibrated 70-hand outcome forward, five opponent-response
forwards, and the shared win-first selector.  Wall time is diagnostic only;
formal eligibility is decided by the maximum per-decision process CPU time.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

import win_first_policy_v4 as win_first


RUNTIME_BUDGET_SCHEMA = "opponent_multitask_v4_runtime_budget_v1"
RUNTIME_BUDGET_METHOD = "stdlib_full_decision_process_cpu_max_v1"
RUNTIME_IDENTITY_SCHEMA = "opponent_multitask_v4_runtime_identity_v1"

# These are policy constants, not command-line tuning knobs.
MAX_BUNDLE_BYTES = 50_000_000
MAX_PRESELECTION_BUNDLE_BYTES = 49_000_000
MAX_FULL_DECISION_CPU_NS = 5_000_000_000
WARMUP_ROUNDS = 2
MEASURED_REPEATS = 7
SUBPROCESS_TIMEOUT_SECONDS = 60

STATE_DIM = 81
PROFILE_DIM = 12
CURRENT_HAND_ROWS = 16
CURRENT_HAND_DIM = 24
CROSS_HAND_ROWS = 32
CROSS_HAND_DIM = 16
STRATEGY_DIM = 66
VALUE_ACTIONS = 6
RESPONSE_ACTIONS = 5
HERO_ACTION_DIM = 10

RULE_LABEL_ID = 0
VALUE_ACTION_LEGAL_MASK = (1, 1, 1, 1, 1, 1)
RESPONSE_ACTION_LEGAL_MASK = (1, 1, 1, 1, 1)
INFERENCE_CALLS_PER_ROUND = {
    "predict_values": 1,
    "predict_match_outcomes": 1,
    "predict_response": 5,
    "win_first_selector": 1,
}

# A fixed valid policy forces the benchmark through the same protected v4
# scoring code without depending on whether policy_selection has opened yet.
BENCHMARK_POLICY = {
    "schema": win_first.POLICY_SCHEMA,
    "selection_priority": win_first.SELECTION_PRIORITY,
    "min_positive_probability_lcb": 0.5,
    "min_probability_uplift_lcb": 0.0,
    "chip_margin": 0.0,
    "hand_weight": 0.25,
    "tail_weight": 0.25,
    "match_weight": 0.5,
    "response_weight": 1.0,
    "min_hand_lcb": 0.0,
    "use_lower": True,
}

# ``-I`` intentionally refuses to put either the current working directory or
# a script's directory on sys.path. Pass the trusted worker as data, add only
# its resolved parent, and execute it as ``__main__``. This works in the lab
# tools directory and after the runtime is copied into a native candidate.
ISOLATED_WORKER_BOOTSTRAP = (
    "import runpy,sys;"
    "from pathlib import Path;"
    "worker=Path(sys.argv[1]).resolve(strict=True);"
    "sys.path.insert(0,str(worker.parent));"
    "sys.argv=[str(worker),*sys.argv[2:]];"
    "runpy.run_path(str(worker),run_name='__main__')"
)


def _canonical_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def runtime_budget_payload_sha256(payload: dict[str, Any]) -> str:
    unsigned = dict(payload)
    unsigned.pop("payload_sha256", None)
    return hashlib.sha256(_canonical_bytes(unsigned)).hexdigest()


def bundle_runtime_identity(payload: Any) -> dict[str, Any]:
    """Project policy-independent runtime identity from a v4 bundle."""
    if not isinstance(payload, dict):
        raise ValueError("v4 runtime identity requires a bundle object")
    calibration = payload.get("calibration")
    source = payload.get("source")
    export_contract = payload.get("export_contract")
    member_hashes = payload.get("member_payload_sha256")
    if (
        not isinstance(calibration, dict)
        or not isinstance(source, dict)
        or not isinstance(export_contract, dict)
        or not isinstance(member_hashes, list)
        or not member_hashes
    ):
        raise ValueError("v4 bundle runtime identity is incomplete")
    for index, digest in enumerate(member_hashes):
        _digest(digest, field=f"member_payload_sha256[{index}]")
    identity = {
        "schema": RUNTIME_IDENTITY_SCHEMA,
        "bundle_schema": payload.get("schema"),
        "bundle_format": payload.get("format"),
        "member_payload_sha256": list(member_hashes),
        "calibration": {
            key: calibration.get(key)
            for key in (
                "payload_sha256",
                "member_seed",
                "member_checkpoint_sha256",
                "outcome_calibration_payload_sha256",
                "calibration_projection_sha256",
                "role_manifest_sha256",
                "model_calibration_artifact_sha256",
                "model_calibration_opponents",
                "source_collection_complete",
                "outcome_aggregation",
                "uncertainty_std_weight",
                "outcome_uncertainty_std_weight",
            )
        },
        "source": {
            key: source.get(key)
            for key in (
                "run_id",
                "role_manifest_sha256",
                "ensemble_manifest_sha256",
                "calibration_artifact_manifest_sha256",
                "calibration_file_sha256",
                "calibration_report_sha256",
                "calibration_payload_sha256",
                "calibration_projection_sha256",
                "candidate_snapshot",
                "strategy_context_runtime_mode",
                "source_collection_complete",
                "source_completed_passes",
                "source_requested_passes",
            )
        },
        "export_contract": dict(export_contract),
    }
    # Reject values that cannot enter canonical evidence.
    _canonical_bytes(identity)
    return identity


def bundle_runtime_identity_sha256(payload: Any) -> str:
    return hashlib.sha256(_canonical_bytes(bundle_runtime_identity(payload))).hexdigest()


def _digest(value: Any, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field} must be a lowercase sha256 digest")
    return value


def _finite_tree(value: Any, *, field: str = "prediction") -> None:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            raise ValueError(f"{field} contains a non-finite number")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _finite_tree(item, field=f"{field}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _finite_tree(item, field=f"{field}[{index}]")
        return
    raise ValueError(f"{field} contains an unsupported value")


def _pattern_vector(size: int, *, offset: int) -> list[float]:
    return [((index * 37 + offset) % 101) / 100.0 for index in range(size)]


def benchmark_workload() -> dict[str, Any]:
    """Return a fresh copy of the immutable maximum-shape workload."""
    return {
        "state": _pattern_vector(STATE_DIM, offset=3),
        "profile": _pattern_vector(PROFILE_DIM, offset=7),
        "history": [
            _pattern_vector(CURRENT_HAND_DIM, offset=11 + row)
            for row in range(CURRENT_HAND_ROWS)
        ],
        "cross_sequence": [
            _pattern_vector(CROSS_HAND_DIM, offset=29 + row)
            for row in range(CROSS_HAND_ROWS)
        ],
        "rule_action": [
            1.0 if index == RULE_LABEL_ID else 0.0
            for index in range(VALUE_ACTIONS)
        ],
        "strategy_context": _pattern_vector(STRATEGY_DIM, offset=43),
    }


def workload_contract() -> dict[str, Any]:
    return {
        "state_dim": STATE_DIM,
        "profile_dim": PROFILE_DIM,
        "current_hand_shape": [CURRENT_HAND_ROWS, CURRENT_HAND_DIM],
        "cross_hand_shape": [CROSS_HAND_ROWS, CROSS_HAND_DIM],
        "strategy_dim": STRATEGY_DIM,
        "hero_action_dim": HERO_ACTION_DIM,
        "value_action_legal_mask": list(VALUE_ACTION_LEGAL_MASK),
        "response_action_legal_mask": list(RESPONSE_ACTION_LEGAL_MASK),
        "rule_label_id": RULE_LABEL_ID,
        "candidate_label_ids": list(range(1, VALUE_ACTIONS)),
        "inference_calls_per_round": dict(INFERENCE_CALLS_PER_ROUND),
    }


def _hero_action(label_id: int) -> list[float]:
    return [
        *[
            1.0 if index == label_id else 0.0
            for index in range(VALUE_ACTIONS)
        ],
        0.25,
        0.50,
        0.25,
        0.75,
    ]


def _response_signal(runtime: Any, response: dict[str, Any], action: int) -> float:
    signal = runtime.response_signal(
        response,
        action=action,
        pot=2_000.0,
        hero_stage_bet=500.0,
        hero_stack=15_000.0,
        opponent_stack=15_000.0,
    )
    signal = float(signal)
    if not math.isfinite(signal):
        raise ValueError("response signal is non-finite")
    return signal


def _selector_benchmark_predictions(
) -> tuple[dict[str, Any], dict[str, dict[str, list[float]]]]:
    """Return fixed predictions that exercise every candidate's full score.

    Eligibility must not turn a resource benchmark into a strength judgment.
    Real model predictions are still computed and checked for finite values,
    while this valid projection prevents either early eligibility exits or a
    model-dependent "no candidate" failure in the shared selector.
    """
    outcomes = win_first.aggregate_member_probabilities(
        [[0.1, 0.6, 0.65, 0.7, 0.75, 0.8]],
        uncertainty_std_weight=0.0,
    )
    lower = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]
    values = {
        field: {"lower": list(lower)}
        for field in (
            "delta_vs_rule",
            "tail_delta_vs_rule",
            "match_delta_vs_rule",
        )
    }
    return outcomes, values


def _full_decision(runtime: Any, workload: dict[str, Any]) -> None:
    value_inputs = {
        "state": workload["state"],
        "profile": workload["profile"],
        "history": workload["history"],
        "cross_sequence": workload["cross_sequence"],
        "rule_action": workload["rule_action"],
        "strategy_context": workload["strategy_context"],
    }
    response_inputs = {
        key: value_inputs[key]
        for key in ("state", "profile", "history", "cross_sequence")
    }

    values = runtime.predict_values(**value_inputs)
    _finite_tree(values, field="values")
    outcomes = runtime.predict_match_outcomes(**value_inputs)
    _finite_tree(outcomes, field="outcomes")

    actions = (0, 750, 2_000, 5_000, -2)
    candidates = []
    for label_id, action in zip(range(1, VALUE_ACTIONS), actions, strict=True):
        response = runtime.predict_response(
            **response_inputs,
            hero_action=_hero_action(label_id),
            legal_action_mask=list(RESPONSE_ACTION_LEGAL_MASK),
        )
        _finite_tree(response, field=f"response[{label_id}]")
        # Retain the real response-derived calculation in the timed path, but
        # keep selector eligibility independent from model predictions.
        _response_signal(runtime, response, action)
        candidates.append({
            "label_id": label_id,
            "action": action,
            "response_signal": label_id / 10.0,
        })

    selector_outcomes, selector_values = _selector_benchmark_predictions()
    selected = win_first.select_candidate(
        BENCHMARK_POLICY,
        selector_outcomes,
        selector_values,
        candidates,
        rule_label_id=RULE_LABEL_ID,
    )
    _finite_tree(selected, field="selected")
    if (
        not isinstance(selected, dict)
        or not any(
            selected.get("action") == candidate["action"]
            and selected.get("label_id") == candidate["label_id"]
            for candidate in candidates
        )
    ):
        raise ValueError("benchmark selector did not return a legal candidate")


def _artifact(
    *,
    bundle_bytes: int,
    bundle_sha256: str,
    runtime_identity_sha256: str,
    preselection_runtime_budget_payload_sha256: str | None = None,
    source_collection_complete: bool,
    cpu_ns: list[int],
    wall_ns: list[int],
    warmups_completed: int,
    errors: list[str],
) -> dict[str, Any]:
    measurements_complete = (
        warmups_completed == WARMUP_ROUNDS
        and len(cpu_ns) == MEASURED_REPEATS
        and len(wall_ns) == MEASURED_REPEATS
    )
    max_cpu_ns = max(cpu_ns) if cpu_ns else None
    max_wall_ns = max(wall_ns) if wall_ns else None
    budget_passed = bool(
        not errors
        and measurements_complete
        and bundle_bytes <= MAX_BUNDLE_BYTES
        and max_cpu_ns is not None
        and max_cpu_ns <= MAX_FULL_DECISION_CPU_NS
    )
    payload = {
        "schema": RUNTIME_BUDGET_SCHEMA,
        "method": RUNTIME_BUDGET_METHOD,
        "limits": {
            "max_bundle_bytes": MAX_BUNDLE_BYTES,
            "max_preselection_bundle_bytes": MAX_PRESELECTION_BUNDLE_BYTES,
            "max_full_decision_cpu_ns": MAX_FULL_DECISION_CPU_NS,
            "warmup_rounds": WARMUP_ROUNDS,
            "measured_repeats": MEASURED_REPEATS,
        },
        "clocks": {
            "eligibility": "time.process_time_ns",
            "wall_diagnostic_only": "time.perf_counter_ns",
        },
        "workload": workload_contract(),
        "bundle": {
            "bytes": bundle_bytes,
            "sha256": bundle_sha256,
        },
        "runtime_identity_sha256": runtime_identity_sha256,
        "preselection_runtime_budget_payload_sha256": (
            preselection_runtime_budget_payload_sha256
        ),
        "measurements": {
            "warmups_completed": warmups_completed,
            "repeats_completed": len(cpu_ns),
            "full_decision_cpu_ns": cpu_ns,
            "full_decision_wall_ns": wall_ns,
            "max_full_decision_cpu_ns": max_cpu_ns,
            "max_full_decision_wall_ns": max_wall_ns,
        },
        "source_collection_complete": source_collection_complete,
        "measurements_complete": measurements_complete,
        "runtime_budget_passed": budget_passed,
        "formal_runtime_budget_passed": bool(
            budget_passed and source_collection_complete
        ),
        "errors": errors,
        "deployment_policy_value": False,
        "strength_evidence": False,
    }
    payload["payload_sha256"] = runtime_budget_payload_sha256(payload)
    return payload


def measure_runtime_budget(
    runtime: Any,
    *,
    bundle_bytes: int,
    bundle_sha256: str,
    runtime_identity_sha256: str | None = None,
    preselection_runtime_budget_payload_sha256: str | None = None,
    source_collection_complete: bool,
) -> dict[str, Any]:
    """Measure one loaded v4 stdlib runtime under the immutable budget.

    Inference exceptions, non-finite predictions, and CPU overruns produce a
    self-hashed failing artifact instead of escaping as an accidental pass.
    """
    if isinstance(bundle_bytes, bool) or not isinstance(bundle_bytes, int):
        raise ValueError("bundle_bytes must be an integer")
    if bundle_bytes < 0:
        raise ValueError("bundle_bytes must be nonnegative")
    bundle_sha256 = _digest(bundle_sha256, field="bundle_sha256")
    runtime_identity_sha256 = _digest(
        bundle_sha256
        if runtime_identity_sha256 is None
        else runtime_identity_sha256,
        field="runtime_identity_sha256",
    )
    if preselection_runtime_budget_payload_sha256 is not None:
        preselection_runtime_budget_payload_sha256 = _digest(
            preselection_runtime_budget_payload_sha256,
            field="preselection_runtime_budget_payload_sha256",
        )
    if not isinstance(source_collection_complete, bool):
        raise ValueError("source_collection_complete must be boolean")

    cpu_ns: list[int] = []
    wall_ns: list[int] = []
    warmups_completed = 0
    errors: list[str] = []
    if bundle_bytes > MAX_BUNDLE_BYTES:
        errors.append(
            f"bundle_bytes {bundle_bytes} exceeds immutable limit "
            f"{MAX_BUNDLE_BYTES}"
        )
        return _artifact(
            bundle_bytes=bundle_bytes,
            bundle_sha256=bundle_sha256,
            runtime_identity_sha256=runtime_identity_sha256,
            preselection_runtime_budget_payload_sha256=(
                preselection_runtime_budget_payload_sha256
            ),
            source_collection_complete=source_collection_complete,
            cpu_ns=cpu_ns,
            wall_ns=wall_ns,
            warmups_completed=warmups_completed,
            errors=errors,
        )

    workload = benchmark_workload()
    try:
        for _ in range(WARMUP_ROUNDS):
            _full_decision(runtime, workload)
            warmups_completed += 1
        for _ in range(MEASURED_REPEATS):
            cpu_start = time.process_time_ns()
            wall_start = time.perf_counter_ns()
            _full_decision(runtime, workload)
            cpu_elapsed = time.process_time_ns() - cpu_start
            wall_elapsed = time.perf_counter_ns() - wall_start
            if cpu_elapsed < 0 or wall_elapsed < 0:
                raise ValueError("runtime benchmark clock moved backwards")
            cpu_ns.append(cpu_elapsed)
            wall_ns.append(wall_elapsed)
            if cpu_elapsed > MAX_FULL_DECISION_CPU_NS:
                errors.append(
                    f"full decision CPU time {cpu_elapsed} ns exceeds immutable "
                    f"limit {MAX_FULL_DECISION_CPU_NS} ns"
                )
                break
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")

    return _artifact(
        bundle_bytes=bundle_bytes,
        bundle_sha256=bundle_sha256,
        runtime_identity_sha256=runtime_identity_sha256,
        preselection_runtime_budget_payload_sha256=(
            preselection_runtime_budget_payload_sha256
        ),
        source_collection_complete=source_collection_complete,
        cpu_ns=cpu_ns,
        wall_ns=wall_ns,
        warmups_completed=warmups_completed,
        errors=errors,
    )


def measure_bundle_runtime_budget(
    bundle_path: str | Path,
    *,
    source_collection_complete: bool,
    preselection_runtime_budget_payload_sha256: str | None = None,
) -> dict[str, Any]:
    """Load and measure an on-disk v4 bundle with the pure stdlib runtime."""
    from opponent_multitask_ensemble_runtime_v4 import (
        OpponentMultiTaskEnsembleRuntimeV4,
    )

    path = Path(bundle_path)
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if len(raw) > MAX_BUNDLE_BYTES:
        return measure_runtime_budget(
            None,
            bundle_bytes=len(raw),
            bundle_sha256=digest,
            runtime_identity_sha256=digest,
            preselection_runtime_budget_payload_sha256=(
                preselection_runtime_budget_payload_sha256
            ),
            source_collection_complete=source_collection_complete,
        )
    identity_sha256 = digest
    try:
        payload = json.loads(raw)
        identity_sha256 = bundle_runtime_identity_sha256(payload)
    except Exception as exc:
        return _artifact(
            bundle_bytes=len(raw),
            bundle_sha256=digest,
            runtime_identity_sha256=identity_sha256,
            preselection_runtime_budget_payload_sha256=(
                preselection_runtime_budget_payload_sha256
            ),
            source_collection_complete=source_collection_complete,
            cpu_ns=[],
            wall_ns=[],
            warmups_completed=0,
            errors=[f"{type(exc).__name__}: {exc}"],
        )
    try:
        runtime = OpponentMultiTaskEnsembleRuntimeV4(payload)
    except Exception as exc:
        return _artifact(
            bundle_bytes=len(raw),
            bundle_sha256=digest,
            runtime_identity_sha256=identity_sha256,
            preselection_runtime_budget_payload_sha256=(
                preselection_runtime_budget_payload_sha256
            ),
            source_collection_complete=source_collection_complete,
            cpu_ns=[],
            wall_ns=[],
            warmups_completed=0,
            errors=[f"{type(exc).__name__}: {exc}"],
        )
    return measure_runtime_budget(
        runtime,
        bundle_bytes=len(raw),
        bundle_sha256=digest,
        runtime_identity_sha256=identity_sha256,
        preselection_runtime_budget_payload_sha256=(
            preselection_runtime_budget_payload_sha256
        ),
        source_collection_complete=source_collection_complete,
    )


def _failed_bundle_artifact(
    path: Path,
    *,
    source_collection_complete: bool,
    error: str,
    preselection_runtime_budget_payload_sha256: str | None = None,
) -> dict[str, Any]:
    raw = path.read_bytes()
    bundle_sha256 = hashlib.sha256(raw).hexdigest()
    try:
        identity_sha256 = bundle_runtime_identity_sha256(json.loads(raw))
    except Exception:
        identity_sha256 = bundle_sha256
    return _artifact(
        bundle_bytes=len(raw),
        bundle_sha256=bundle_sha256,
        runtime_identity_sha256=identity_sha256,
        preselection_runtime_budget_payload_sha256=(
            preselection_runtime_budget_payload_sha256
        ),
        source_collection_complete=source_collection_complete,
        cpu_ns=[],
        wall_ns=[],
        warmups_completed=0,
        errors=[error],
    )


def measure_bundle_runtime_budget_subprocess(
    bundle_path: str | Path,
    *,
    source_collection_complete: bool,
    preselection_runtime_budget_payload_sha256: str | None = None,
    worker_script: str | Path | None = None,
) -> dict[str, Any]:
    """Measure in a clean isolated stdlib child with a fixed hard timeout."""
    path = Path(bundle_path).resolve()
    if not isinstance(source_collection_complete, bool):
        raise ValueError("source_collection_complete must be boolean")
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment.update({
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONHASHSEED": "0",
    })
    trusted_worker = (
        Path(__file__).resolve()
        if worker_script is None
        else Path(worker_script).resolve()
    )
    command = [
        sys.executable,
        "-I",
        "-c",
        ISOLATED_WORKER_BOOTSTRAP,
        str(trusted_worker),
        "--worker-bundle",
        str(path),
        "--source-collection-complete",
        "1" if source_collection_complete else "0",
    ]
    if preselection_runtime_budget_payload_sha256 is not None:
        command.extend([
            "--preselection-runtime-budget-payload-sha256",
            _digest(
                preselection_runtime_budget_payload_sha256,
                field="preselection_runtime_budget_payload_sha256",
            ),
        ])
    try:
        completed = subprocess.run(
            command,
            cwd=path.parent,
            env=environment,
            capture_output=True,
            check=False,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return _failed_bundle_artifact(
            path,
            source_collection_complete=source_collection_complete,
            error=(
                "runtime budget subprocess exceeded immutable timeout "
                f"{SUBPROCESS_TIMEOUT_SECONDS}s"
            ),
            preselection_runtime_budget_payload_sha256=(
                preselection_runtime_budget_payload_sha256
            ),
        )
    if completed.returncode != 0:
        return _failed_bundle_artifact(
            path,
            source_collection_complete=source_collection_complete,
            error=(
                "runtime budget subprocess failed: "
                f"{completed.stderr[-1000:]}"
            ),
            preselection_runtime_budget_payload_sha256=(
                preselection_runtime_budget_payload_sha256
            ),
        )
    try:
        artifact = json.loads(completed.stdout)
        raw = path.read_bytes()
        payload = json.loads(raw)
        return validate_runtime_budget_artifact(
            artifact,
            bundle_bytes=len(raw),
            bundle_sha256=hashlib.sha256(raw).hexdigest(),
            runtime_identity_sha256=bundle_runtime_identity_sha256(payload),
            preselection_runtime_budget_payload_sha256=(
                preselection_runtime_budget_payload_sha256
            ),
            # Preserve a structurally valid, exactly bound failure artifact.
            # Selector/builder callers own the formal eligibility decision and
            # will revalidate with ``require_formal=True`` at that boundary.
            require_formal=False,
        )
    except Exception as exc:
        return _failed_bundle_artifact(
            path,
            source_collection_complete=source_collection_complete,
            error=f"invalid runtime budget subprocess result: {type(exc).__name__}: {exc}",
            preselection_runtime_budget_payload_sha256=(
                preselection_runtime_budget_payload_sha256
            ),
        )


def validate_runtime_budget_artifact(
    payload: Any,
    *,
    bundle_bytes: int | None = None,
    bundle_sha256: str | None = None,
    runtime_identity_sha256: str | None = None,
    preselection_runtime_budget_payload_sha256: str | None = None,
    require_formal: bool = False,
) -> dict[str, Any]:
    """Fail closed on tampering, weakened limits, or incomplete formal runs."""
    expected = {
        "schema",
        "method",
        "limits",
        "clocks",
        "workload",
        "bundle",
        "runtime_identity_sha256",
        "preselection_runtime_budget_payload_sha256",
        "measurements",
        "source_collection_complete",
        "measurements_complete",
        "runtime_budget_passed",
        "formal_runtime_budget_passed",
        "errors",
        "deployment_policy_value",
        "strength_evidence",
        "payload_sha256",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise ValueError("v4 runtime budget has unknown or missing fields")
    observed_hash = _digest(
        payload.get("payload_sha256"), field="runtime budget payload_sha256"
    )
    if runtime_budget_payload_sha256(payload) != observed_hash:
        raise ValueError("v4 runtime budget self-hash changed")
    if (
        payload.get("schema") != RUNTIME_BUDGET_SCHEMA
        or payload.get("method") != RUNTIME_BUDGET_METHOD
        or payload.get("limits") != {
            "max_bundle_bytes": MAX_BUNDLE_BYTES,
            "max_preselection_bundle_bytes": MAX_PRESELECTION_BUNDLE_BYTES,
            "max_full_decision_cpu_ns": MAX_FULL_DECISION_CPU_NS,
            "warmup_rounds": WARMUP_ROUNDS,
            "measured_repeats": MEASURED_REPEATS,
        }
        or payload.get("clocks") != {
            "eligibility": "time.process_time_ns",
            "wall_diagnostic_only": "time.perf_counter_ns",
        }
        or payload.get("workload") != workload_contract()
        or payload.get("deployment_policy_value") is not False
        or payload.get("strength_evidence") is not False
        or not isinstance(payload.get("source_collection_complete"), bool)
        or not isinstance(payload.get("measurements_complete"), bool)
        or not isinstance(payload.get("runtime_budget_passed"), bool)
        or not isinstance(payload.get("formal_runtime_budget_passed"), bool)
    ):
        raise ValueError("v4 runtime budget contract changed")

    bundle = payload.get("bundle")
    measurements = payload.get("measurements")
    errors = payload.get("errors")
    if (
        not isinstance(bundle, dict)
        or set(bundle) != {"bytes", "sha256"}
        or isinstance(bundle.get("bytes"), bool)
        or not isinstance(bundle.get("bytes"), int)
        or bundle["bytes"] < 0
        or not isinstance(measurements, dict)
        or set(measurements) != {
            "warmups_completed",
            "repeats_completed",
            "full_decision_cpu_ns",
            "full_decision_wall_ns",
            "max_full_decision_cpu_ns",
            "max_full_decision_wall_ns",
        }
        or not isinstance(errors, list)
        or any(not isinstance(error, str) or not error for error in errors)
    ):
        raise ValueError("v4 runtime budget result is malformed")
    observed_bundle_sha = _digest(bundle.get("sha256"), field="bundle.sha256")
    observed_identity_sha = _digest(
        payload.get("runtime_identity_sha256"),
        field="runtime_identity_sha256",
    )
    observed_preselection_sha = payload.get(
        "preselection_runtime_budget_payload_sha256"
    )
    if observed_preselection_sha is not None:
        observed_preselection_sha = _digest(
            observed_preselection_sha,
            field="preselection_runtime_budget_payload_sha256",
        )
    if bundle_bytes is not None and bundle["bytes"] != bundle_bytes:
        raise ValueError("v4 runtime budget bundle byte count changed")
    if bundle_sha256 is not None and observed_bundle_sha != _digest(
        bundle_sha256, field="expected bundle_sha256"
    ):
        raise ValueError("v4 runtime budget bundle hash changed")
    if (
        runtime_identity_sha256 is not None
        and observed_identity_sha
        != _digest(
            runtime_identity_sha256,
            field="expected runtime_identity_sha256",
        )
    ):
        raise ValueError("v4 runtime budget runtime identity changed")
    if (
        preselection_runtime_budget_payload_sha256 is not None
        and observed_preselection_sha
        != _digest(
            preselection_runtime_budget_payload_sha256,
            field="expected preselection runtime budget payload sha256",
        )
    ):
        raise ValueError("v4 runtime budget preselection binding changed")

    warmups = measurements.get("warmups_completed")
    repeats = measurements.get("repeats_completed")
    cpu_ns = measurements.get("full_decision_cpu_ns")
    wall_ns = measurements.get("full_decision_wall_ns")
    if (
        isinstance(warmups, bool)
        or not isinstance(warmups, int)
        or not 0 <= warmups <= WARMUP_ROUNDS
        or isinstance(repeats, bool)
        or not isinstance(repeats, int)
        or not isinstance(cpu_ns, list)
        or not isinstance(wall_ns, list)
        or repeats != len(cpu_ns)
        or repeats != len(wall_ns)
        or not 0 <= repeats <= MEASURED_REPEATS
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in [*cpu_ns, *wall_ns]
        )
        or measurements.get("max_full_decision_cpu_ns")
        != (max(cpu_ns) if cpu_ns else None)
        or measurements.get("max_full_decision_wall_ns")
        != (max(wall_ns) if wall_ns else None)
    ):
        raise ValueError("v4 runtime budget measurements are malformed")

    complete = (
        warmups == WARMUP_ROUNDS
        and repeats == MEASURED_REPEATS
    )
    max_cpu = max(cpu_ns) if cpu_ns else None
    passed = bool(
        not errors
        and complete
        and bundle["bytes"] <= MAX_BUNDLE_BYTES
        and max_cpu is not None
        and max_cpu <= MAX_FULL_DECISION_CPU_NS
    )
    formal = bool(passed and payload["source_collection_complete"])
    if (
        payload["measurements_complete"] is not complete
        or payload["runtime_budget_passed"] is not passed
        or payload["formal_runtime_budget_passed"] is not formal
    ):
        raise ValueError("v4 runtime budget eligibility is inconsistent")
    if require_formal and not formal:
        raise ValueError("v4 runtime budget is not formal-eligible")
    return dict(payload)


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-bundle", required=True, type=Path)
    parser.add_argument(
        "--source-collection-complete", choices=("0", "1"), required=True
    )
    parser.add_argument("--preselection-runtime-budget-payload-sha256")
    args = parser.parse_args(argv)
    artifact = measure_bundle_runtime_budget(
        args.worker_bundle,
        source_collection_complete=args.source_collection_complete == "1",
        preselection_runtime_budget_payload_sha256=(
            args.preselection_runtime_budget_payload_sha256
        ),
    )
    print(json.dumps(artifact, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
