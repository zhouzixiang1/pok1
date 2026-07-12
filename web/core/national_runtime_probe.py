"""Sandboxed dynamic evidence for national-native runtime contracts."""

from __future__ import annotations

import copy
from collections import OrderedDict
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import threading
from typing import Any

from managed_bot_executor import (
    ExecutorRuntime,
    IsolationUnavailable,
    ManagedExecutorError,
    launch_isolated_worker,
)
from national_runtime_probe_scenarios import (
    LINE_SCENARIO_PAIRS,
    RUNTIME_PROBE_SCENARIO_DIGEST,
    RUNTIME_PROBE_SCENARIO_VERSION,
    SHOWDOWN_RANGE_SCENARIO_IDS,
    TERMINAL_RESPONSE_SCENARIO_IDS,
)
from national_native import NATIONAL_DECISION_RUNTIME_VERSION


RUNTIME_PROBE_SCHEMA_VERSION = 10
RUNTIME_PROBE_ORCHESTRATOR_VERSION = 10
MIGRATION_EVIDENCE_REPEATABILITY_SCHEMA_VERSION = 1
RUNTIME_PROBE_TIMEOUT_SEC = 45.0
RUNTIME_PROBE_REPEATS = 2
RUNTIME_PROBE_MAX_IMPORT_MS = 2_500.0
RUNTIME_PROBE_MAX_ENTRIES = 65_536
RUNTIME_PROBE_MAX_BYTES = 8 * 1024 * 1024
RUNTIME_PROBE_MAX_OUTPUT_BYTES = 1024 * 1024
RUNTIME_PROBE_CACHE_MAX_ENTRIES = 128
_RUNTIME_PROBE_CACHE: OrderedDict[str, dict[str, Any]] = OrderedDict()
_RUNTIME_PROBE_CACHE_LOCK = threading.Lock()

_LEGAL_SANITIZED_WIRE_ACTIONS = frozenset({"fold", "call", "check", "allin"})
_LINE_PAIR_BY_DIMENSION = {
    str(item["dimension"]): dict(item) for item in LINE_SCENARIO_PAIRS
}
_MIGRATION_DIMENSION_SPECS = {
    "terminal_response": {
        "payload": "terminal_response",
        "left_label": "terminal_folder",
        "right_label": "terminal_caller",
        "scenario_ids": frozenset(TERMINAL_RESPONSE_SCENARIO_IDS),
    },
    "showdown_range": {
        "payload": "showdown_range",
        "left_label": "tight_showdown",
        "right_label": "loose_showdown",
        "scenario_ids": frozenset(SHOWDOWN_RANGE_SCENARIO_IDS),
    },
    "donk": {
        "payload": "semantic_lines",
        "left_label": "positive",
        "right_label": "negative",
        "scenario_ids": frozenset({_LINE_PAIR_BY_DIMENSION["donk"]["positive"]}),
    },
    "delayed_probe": {
        "payload": "semantic_lines",
        "left_label": "positive",
        "right_label": "negative",
        "scenario_ids": frozenset({
            _LINE_PAIR_BY_DIMENSION["delayed_probe"]["positive"]
        }),
    },
}


def runtime_probe_limits() -> dict[str, Any]:
    executor_path = Path(__file__).with_name("managed_bot_executor.py")
    socket_path = Path(__file__).with_name("managed_bot_socket.py")
    return {
        "timeout_sec": RUNTIME_PROBE_TIMEOUT_SEC,
        "repeats": RUNTIME_PROBE_REPEATS,
        "max_import_ms": RUNTIME_PROBE_MAX_IMPORT_MS,
        "max_entries": RUNTIME_PROBE_MAX_ENTRIES,
        "max_bytes": RUNTIME_PROBE_MAX_BYTES,
        "max_output_bytes": RUNTIME_PROBE_MAX_OUTPUT_BYTES,
        "sandbox": "central-managed-executor-minimal-ro-inputs-seccomp-v1",
        "managed_executor_sha256": hashlib.sha256(executor_path.read_bytes()).hexdigest(),
        "managed_socket_sha256": hashlib.sha256(socket_path.read_bytes()).hexdigest(),
    }


def _canonical_digest(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


RUNTIME_PROBE_LIMITS_DIGEST = _canonical_digest(runtime_probe_limits())


def _trusted_file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


RUNTIME_PROBE_WORKER_DIGEST = _trusted_file_digest(
    Path(__file__).with_name("national_runtime_probe_worker.py")
)
RUNTIME_PROBE_IDENTITY_DIGEST = _canonical_digest({
    "schema_version": RUNTIME_PROBE_SCHEMA_VERSION,
    "orchestrator_version": RUNTIME_PROBE_ORCHESTRATOR_VERSION,
    "worker_digest": RUNTIME_PROBE_WORKER_DIGEST,
    "scenario_digest": RUNTIME_PROBE_SCENARIO_DIGEST,
    "limits_digest": RUNTIME_PROBE_LIMITS_DIGEST,
})


def clear_runtime_probe_cache() -> None:
    with _RUNTIME_PROBE_CACHE_LOCK:
        _RUNTIME_PROBE_CACHE.clear()


def _runtime_probe_cache_key(
    spec: dict[str, Any],
    *,
    timeout_sec: float,
    repeats: int,
) -> str:
    return _canonical_digest({
        "probe_identity_digest": RUNTIME_PROBE_IDENTITY_DIGEST,
        "spec_digest": spec.get("spec_digest"),
        "code_fingerprint": spec.get("code_fingerprint"),
        "timeout_sec": float(timeout_sec),
        "repeats": max(2, int(repeats)),
    })


def _runtime_probe_cache_get(key: str) -> dict[str, Any] | None:
    with _RUNTIME_PROBE_CACHE_LOCK:
        result = _RUNTIME_PROBE_CACHE.pop(key, None)
        if result is None:
            return None
        _RUNTIME_PROBE_CACHE[key] = result
        cached = copy.deepcopy(result)
    cached["cache_hit"] = True
    return cached


def _runtime_probe_cache_put(key: str, result: dict[str, Any]) -> None:
    stored = copy.deepcopy(result)
    stored["cache_hit"] = False
    with _RUNTIME_PROBE_CACHE_LOCK:
        _RUNTIME_PROBE_CACHE.pop(key, None)
        _RUNTIME_PROBE_CACHE[key] = stored
        while len(_RUNTIME_PROBE_CACHE) > RUNTIME_PROBE_CACHE_MAX_ENTRIES:
            _RUNTIME_PROBE_CACHE.popitem(last=False)


def _bot_code_fingerprint(root: Path) -> str:
    # Probe cache/provenance must follow the complete decision artifact.  A
    # nested packed table or model can change runtime evidence even when every
    # top-level Python loader remains byte-identical.
    from bot_artifact import hash_path

    return hash_path(root)


def _artifact_requests(static_artifacts: list[dict[str, Any]]) -> list[dict[str, str]]:
    requests: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in static_artifacts or []:
        location = str(item.get("location") or "")
        owner_file = location.split(":", 1)[0]
        name = str(item.get("name") or "")
        key = (owner_file, name)
        if not owner_file.endswith(".py") or not name or key in seen:
            continue
        seen.add(key)
        requests.append({
            "owner_file": owner_file,
            "name": name,
            "kind": str(item.get("kind") or ""),
        })
    return requests


def build_runtime_probe_spec(
    bot_dir: str | Path,
    *,
    static_artifacts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    root = Path(bot_dir).resolve()
    payload = {
        "schema_version": RUNTIME_PROBE_SCHEMA_VERSION,
        "orchestrator_version": RUNTIME_PROBE_ORCHESTRATOR_VERSION,
        "scenario_version": RUNTIME_PROBE_SCENARIO_VERSION,
        "scenario_digest": RUNTIME_PROBE_SCENARIO_DIGEST,
        "limits_digest": RUNTIME_PROBE_LIMITS_DIGEST,
        "worker_digest": RUNTIME_PROBE_WORKER_DIGEST,
        "probe_identity_digest": RUNTIME_PROBE_IDENTITY_DIGEST,
        "expected_decision_runtime_version": NATIONAL_DECISION_RUNTIME_VERSION,
        "code_fingerprint": _bot_code_fingerprint(root),
        "artifacts": _artifact_requests(static_artifacts or []),
    }
    payload["spec_digest"] = _canonical_digest(payload)
    return payload


def _probe_worker_path() -> Path:
    return Path(__file__).with_name("national_runtime_probe_worker.py").resolve()


def _kill_process_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            process.kill()
        except ProcessLookupError:
            pass


def _run_once(root: Path, spec: dict[str, Any], timeout_sec: float) -> dict[str, Any]:
    try:
        runtime = ExecutorRuntime.discover()
    except (IsolationUnavailable, ManagedExecutorError) as exc:
        return {
            "ok": False,
            "failure_class": "probe_infra",
            "issues": [f"runtime_probe_isolation_unavailable:{type(exc).__name__}:{str(exc)[:180]}"],
        }
    worker = _probe_worker_path()
    try:
        if _bot_code_fingerprint(root) != str(spec.get("code_fingerprint") or ""):
            return {
                "ok": False,
                "failure_class": "candidate_contract",
                "issues": ["runtime_probe_candidate_changed_before_launch"],
            }
    except Exception as exc:
        return {
            "ok": False,
            "failure_class": "candidate_contract",
            "issues": [f"runtime_probe_candidate_rehash_failed:{type(exc).__name__}:{str(exc)[:180]}"],
        }
    scenario = Path(__file__).with_name("national_runtime_probe_scenarios.py").resolve()
    with (
        tempfile.TemporaryDirectory(prefix="pok_runtime_probe_work_") as work_dir,
        tempfile.TemporaryDirectory(prefix="pok_runtime_probe_out_") as output_dir,
    ):
        work_root = Path(work_dir)
        output_root = Path(output_dir)
        shutil.copyfile(worker, work_root / "worker.py")
        shutil.copyfile(scenario, work_root / "national_runtime_probe_scenarios.py")
        (work_root / "spec.json").write_text(
            json.dumps(spec, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        (work_root / "worker.py").chmod(0o444)
        (work_root / "national_runtime_probe_scenarios.py").chmod(0o444)
        (work_root / "spec.json").chmod(0o444)
        report_host = output_root / "report.json"
        phase_host = output_root / "phase.txt"
        stdout_host = output_root / "stdout.log"
        stderr_host = output_root / "stderr.log"
        command = [
            str(runtime.python),
            "-I",
            "-B",
            "/work/worker.py",
            "/inputs/bot",
            "/output/report.json",
            "/work/spec.json",
        ]
        with stdout_host.open("wb") as stdout_fp, stderr_host.open("wb") as stderr_fp:
            try:
                managed = launch_isolated_worker(
                    work_root,
                    command,
                    environment={"PYTHONHASHSEED": "0"},
                    readonly_inputs={"bot": root},
                    output_files={
                        "report.json": report_host,
                        "phase.txt": phase_host,
                    },
                    runtime=runtime,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_fp,
                    stderr=stderr_fp,
                    start_new_session=True,
                )
                process = managed.process
            except (OSError, ManagedExecutorError) as exc:
                return {
                    "ok": False,
                    "failure_class": "probe_infra",
                    "issues": [f"runtime_probe_launch_error:{type(exc).__name__}:{str(exc)[:180]}"],
                }
            try:
                returncode = process.wait(timeout=max(1.0, float(timeout_sec)))
            except subprocess.TimeoutExpired:
                _kill_process_group(process)
                process.wait(timeout=5.0)
                phase = (
                    phase_host.read_text(encoding="utf-8", errors="replace")[:120]
                    if phase_host.is_file()
                    else "unknown"
                )
                return {
                    "ok": False,
                    "failure_class": "candidate_contract",
                    "issues": [f"runtime_probe_candidate_timeout:phase={phase}"],
                }
        stdout_bytes = stdout_host.stat().st_size
        stderr_bytes = stderr_host.stat().st_size
        stderr_text = (
            stderr_host.read_text(encoding="utf-8", errors="replace")[:2000]
            if stderr_bytes
            else ""
        )
        try:
            if _bot_code_fingerprint(root) != str(spec.get("code_fingerprint") or ""):
                return {
                    "ok": False,
                    "failure_class": "candidate_contract",
                    "issues": ["runtime_probe_candidate_changed_during_probe"],
                    "process_returncode": returncode,
                }
        except Exception as exc:
            return {
                "ok": False,
                "failure_class": "candidate_contract",
                "issues": [f"runtime_probe_candidate_posthash_failed:{type(exc).__name__}:{str(exc)[:180]}"],
                "process_returncode": returncode,
            }
        if stdout_bytes > RUNTIME_PROBE_MAX_OUTPUT_BYTES or stderr_bytes > RUNTIME_PROBE_MAX_OUTPUT_BYTES:
            return {
                "ok": False,
                "failure_class": (
                    "probe_infra"
                    if returncode in {125, 126, 127}
                    or stderr_text.startswith(("bwrap:", "prlimit:"))
                    else "candidate_contract"
                ),
                "issues": ["runtime_probe_output_limit_exceeded"],
                "process_returncode": returncode,
            }
        if returncode != 0 or not report_host.is_file():
            return {
                "ok": False,
                "failure_class": "candidate_contract",
                "issues": [f"runtime_probe_worker_failed:rc={returncode}"],
                "worker_stdout": stdout_host.read_text(encoding="utf-8", errors="replace")[:2000],
                "worker_stderr": stderr_text,
            }
        try:
            result = json.loads(report_host.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {
                "ok": False,
                "failure_class": "probe_infra",
                "issues": [f"runtime_probe_report_invalid:{type(exc).__name__}:{str(exc)[:180]}"],
            }
        if not isinstance(result, dict):
            return {
                "ok": False,
                "failure_class": "probe_infra",
                "issues": ["runtime_probe_report_not_object"],
            }
        result["process_returncode"] = returncode
        result["managed_isolation"] = asdict(managed.isolation)
        if result.get("failure_class") not in {"probe_infra", "candidate_contract", "none"}:
            result["failure_class"] = "candidate_contract" if result.get("issues") else "none"
        if stdout_bytes:
            result["worker_stdout"] = stdout_host.read_text(encoding="utf-8", errors="replace")[:2000]
        if stderr_bytes:
            result["worker_stderr"] = stderr_host.read_text(encoding="utf-8", errors="replace")[:2000]
        return result


def _repeatability_view(result: dict[str, Any]) -> dict[str, Any]:
    def stable_action(action: Any) -> Any:
        if not isinstance(action, dict):
            return action
        cleaned = copy.deepcopy(action)
        # Deadline scheduling may report worker_done versus latest_safe while
        # preserving the same sanitized action and trusted capability facts.
        # Source labels are diagnostic timing outcomes, not deterministic
        # strategy evidence.
        cleaned.pop("source", None)
        metrics = cleaned.get("runtime_metrics") or {}
        cleaned["runtime_metrics"] = {
            "runtime_version": metrics.get("runtime_version"),
            "socket_fallback_action": metrics.get("socket_fallback_action"),
            "refinement_observed": bool(metrics.get("refinement_messages")),
            "refinement_changed_action": bool(metrics.get("refinement_action_changes")),
            "trusted_refinement_steps_ge_8": int(
                metrics.get("trusted_refinement_steps") or 0
            ) >= 8,
            "refinement_iterator_exhausted": bool(
                metrics.get("refinement_iterator_exhausted")
            ),
            "timed_out": metrics.get("timed_out"),
            "worker_terminated": metrics.get("worker_terminated"),
            "completed": metrics.get("completed"),
        }
        return cleaned

    def stable_action_tree(value: Any) -> Any:
        if isinstance(value, dict):
            if "runtime_metrics" in value and ("wire" in value or "internal" in value):
                return stable_action(value)
            return {key: stable_action_tree(item) for key, item in value.items()}
        if isinstance(value, list):
            return [stable_action_tree(item) for item in value]
        return value

    artifacts = [
        {
            key: row.get(key)
            for key in (
                "owner_file",
                "name",
                "entries",
                "deep_bytes",
                "observed_key_shape",
                "observed_build_phase",
                "consumer_reads",
                "consumer_scenarios",
                "lookup_key_fingerprints",
                "lookup_key_varies_across_consumer_scenarios",
                "value_affects_final_wire",
                "action_influence_scenarios",
                "counterfactual_scenarios",
                "fallback_ok",
                "fallback_scenarios",
                "issues",
                "ok",
            )
        }
        for row in result.get("artifacts") or []
        if isinstance(row, dict)
    ]
    for artifact in artifacts:
        for field in (
            "consumer_scenarios",
            "counterfactual_scenarios",
            "fallback_scenarios",
        ):
            for scenario in artifact.get(field) or []:
                if isinstance(scenario, dict) and "action" in scenario:
                    scenario["action"] = stable_action(scenario["action"])
    strategy = stable_action_tree(
        copy.deepcopy(result.get("strategy_influence") or {})
    )
    decision_runtime = copy.deepcopy(result.get("decision_runtime") or {})
    decision_runtime.pop("baseline_samples_ms", None)
    decision_runtime.pop("fallback_ready_samples_ms", None)
    decision_runtime.pop("refinement_evidence", None)
    scaling = decision_runtime.get("budget_scaling") or {}
    short = scaling.get("short") or {}
    long = scaling.get("long") or {}
    decision_runtime["budget_scaling"] = {
        "probe_kind": str(scaling.get("probe_kind") or ""),
        "short_budget": scaling.get("short_budget") or {},
        "long_budget": scaling.get("long_budget") or {},
        "short_has_work": int(short.get("trusted_steps") or 0) > 0,
        "long_has_real_work": int(long.get("trusted_steps") or 0) >= 8,
        "long_scales_or_completes": (
            int(long.get("trusted_steps") or 0) > int(short.get("trusted_steps") or 0)
            or (
                bool(short.get("iterator_exhausted"))
                and bool(long.get("iterator_exhausted"))
            )
        ),
        "short_changed_action": bool(short.get("action_changes")),
        "long_changed_action": bool(long.get("action_changes")),
        "short_wire": short.get("wire"),
        "long_wire": long.get("wire"),
        "worker_seed_equal": (
            short.get("worker_seed") is not None
            and short.get("worker_seed") == long.get("worker_seed")
        ),
    }
    for action in (decision_runtime.get("timeout_recovery") or {}).values():
        if not isinstance(action, dict):
            continue
        cleaned_action = stable_action(action)
        action.clear()
        action.update(cleaned_action)
    return {
        "schema_version": result.get("schema_version"),
        "worker_version": result.get("worker_version"),
        "scenario_version": result.get("scenario_version"),
        "scenario_digest": result.get("scenario_digest"),
        "spec_digest": result.get("spec_digest"),
        "code_fingerprint": result.get("code_fingerprint"),
        "issues": result.get("issues") or [],
        "artifacts": artifacts,
        "tracker": result.get("tracker") or {},
        "hand_context": result.get("hand_context") or {},
        "strategy_influence": strategy,
        "decision_runtime": decision_runtime,
    }


def _is_legal_sanitized_wire(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if value in _LEGAL_SANITIZED_WIRE_ACTIONS:
        return True
    if not value.startswith("raise "):
        return False
    amount = value[6:]
    return bool(amount and amount.isascii() and amount.isdigit() and amount[0] != "0")


def _migration_baseline_evidence(
    result: dict[str, Any],
    dimension: str,
) -> list[dict[str, str]]:
    """Extract deterministic final-wire migration evidence from one run.

    Only the baseline tier is authoritative here. It fixes refinement candidates
    at zero in the worker, so wall-clock deadline/refinement scheduling cannot
    make a migration capability appear or disappear between otherwise equal
    runs. Short/long tiers remain useful diagnostics outside this contract.
    """

    spec = _MIGRATION_DIMENSION_SPECS.get(dimension)
    if spec is None:
        return []
    influence = result.get("strategy_influence") or {}
    dimensions = influence.get("dimensions") or {}
    payload = dimensions.get(spec["payload"]) or {}
    rows = payload.get("rows") or []
    evidence: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        scenario_id = str(row.get("scenario_id") or "")
        if scenario_id not in spec["scenario_ids"]:
            continue
        if dimension in _LINE_PAIR_BY_DIMENSION:
            pair = _LINE_PAIR_BY_DIMENSION[dimension]
            if (
                row.get("dimension") != dimension
                or row.get("control_kind") != "same_scenario_flag_false"
                or row.get("flag") != pair["flag"]
                or scenario_id != pair["positive"]
            ):
                continue
        tiers = row.get("tiers") or {}
        baseline = tiers.get("baseline") if isinstance(tiers, dict) else None
        if not isinstance(baseline, dict) or baseline.get("changed") is not True:
            continue
        left_label = str(spec["left_label"])
        right_label = str(spec["right_label"])
        left = baseline.get(left_label)
        right = baseline.get(right_label)
        if not isinstance(left, dict) or not isinstance(right, dict):
            continue
        if "error" in left or "error" in right:
            continue
        left_wire = left.get("wire")
        right_wire = right.get("wire")
        if (
            not _is_legal_sanitized_wire(left_wire)
            or not _is_legal_sanitized_wire(right_wire)
            or left_wire == right_wire
        ):
            continue
        row_evidence = {
            "dimension": dimension,
            "scenario_id": scenario_id,
            "tier": "baseline",
            "left_label": left_label,
            "right_label": right_label,
            "left_wire": str(left_wire),
            "right_wire": str(right_wire),
        }
        if dimension in _LINE_PAIR_BY_DIMENSION:
            row_evidence["control_kind"] = "same_scenario_flag_false"
            row_evidence["flag"] = str(_LINE_PAIR_BY_DIMENSION[dimension]["flag"])
        evidence.append(row_evidence)
    return sorted(
        evidence,
        key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
    )


def _migration_evidence_repeatability(
    runs: list[dict[str, Any]],
    *,
    before_fingerprint: str,
    after_fingerprint: str,
) -> dict[str, Any]:
    """Aggregate per-dimension evidence without coupling it to full-probe jitter."""

    candidate_fingerprint_unchanged = bool(
        before_fingerprint == after_fingerprint
        and runs
        and all(
            str(run.get("code_fingerprint") or "") == before_fingerprint
            for run in runs
        )
    )
    runs_eligible = bool(
        len(runs) >= 2
        and all(
            str(run.get("failure_class") or "")
            not in {"probe_infra", "internal_infrastructure"}
            and not any(
                str(issue).startswith("runtime_probe_candidate_timeout:")
                for issue in run.get("issues") or []
            )
            for run in runs
        )
    )
    dimensions: dict[str, dict[str, Any]] = {}
    for dimension in _MIGRATION_DIMENSION_SPECS:
        observations = [
            _migration_baseline_evidence(run, dimension) for run in runs
        ]
        reference = observations[0] if observations else []
        observations_identical = bool(
            len(observations) >= 2
            and all(observation == reference for observation in observations[1:])
        )
        evidence_present = bool(reference)
        stable = bool(
            candidate_fingerprint_unchanged
            and runs_eligible
            and observations_identical
            and evidence_present
        )
        dimensions[dimension] = {
            "stable": stable,
            "authority_tier": "baseline",
            "evidence_present": evidence_present,
            "observations_identical": observations_identical,
            "evidence": copy.deepcopy(reference) if stable else [],
            "observation_digests": [
                _canonical_digest(observation) for observation in observations
            ],
        }
    return {
        "schema_version": MIGRATION_EVIDENCE_REPEATABILITY_SCHEMA_VERSION,
        "candidate_fingerprint_unchanged": candidate_fingerprint_unchanged,
        "run_count": len(runs),
        "runs_eligible": runs_eligible,
        "dimensions": dimensions,
    }


def run_national_runtime_probe(
    bot_dir: str | Path,
    *,
    static_artifacts: list[dict[str, Any]] | None = None,
    timeout_sec: float = RUNTIME_PROBE_TIMEOUT_SEC,
    repeats: int = RUNTIME_PROBE_REPEATS,
) -> dict[str, Any]:
    """Run two fresh sandboxed probes and require deterministic evidence."""
    root = Path(bot_dir).resolve()
    spec = build_runtime_probe_spec(root, static_artifacts=static_artifacts)
    before = _bot_code_fingerprint(root)
    cache_key = _runtime_probe_cache_key(
        spec,
        timeout_sec=timeout_sec,
        repeats=repeats,
    )
    cached = _runtime_probe_cache_get(cache_key)
    if cached is not None:
        return cached
    runs = []
    for _ in range(max(2, int(repeats))):
        run = _run_once(root, spec, timeout_sec)
        runs.append(run)
        # Dynamic dimensions remain useful shadow evidence when unrelated
        # advisory checks fail, so ordinary candidate-contract results still
        # repeat. Launch failures and candidate timeouts return immediately
        # instead of burning another complete sandbox timeout.
        non_repeatable_failure = run.get("failure_class") == "probe_infra" or any(
            str(issue).startswith("runtime_probe_candidate_timeout:")
            for issue in run.get("issues") or []
        )
        if non_repeatable_failure:
            break
    after = _bot_code_fingerprint(root)
    migration_repeatability = _migration_evidence_repeatability(
        runs,
        before_fingerprint=before,
        after_fingerprint=after,
    )
    for run in runs:
        if run.get("failure_class") == "probe_infra" or any(
            str(issue).startswith("runtime_probe_candidate_timeout:")
            for issue in run.get("issues") or []
        ):
            return {
                **run,
                "schema_version": RUNTIME_PROBE_SCHEMA_VERSION,
                "orchestrator_version": RUNTIME_PROBE_ORCHESTRATOR_VERSION,
                "scenario_version": RUNTIME_PROBE_SCENARIO_VERSION,
                "scenario_digest": RUNTIME_PROBE_SCENARIO_DIGEST,
                "limits_digest": RUNTIME_PROBE_LIMITS_DIGEST,
                "worker_digest": RUNTIME_PROBE_WORKER_DIGEST,
                "probe_identity_digest": RUNTIME_PROBE_IDENTITY_DIGEST,
                "spec_digest": spec["spec_digest"],
                "code_fingerprint": before,
                "repeat_count": len(runs),
                "repeatability_ok": False,
                "evidence_integrity_ok": False,
                "migration_evidence_repeatability": migration_repeatability,
            }
    first = dict(runs[0])
    issues = list(first.get("issues") or [])
    if before != after:
        issues.append("runtime_probe_mutated_candidate")
    reference = _repeatability_view(runs[0])
    repeatability_ok = len(runs) >= 2 and not any(
        _repeatability_view(run) != reference for run in runs[1:]
    )
    if not repeatability_ok:
        issues.append("runtime_probe_non_repeatable")
    evidence_integrity_ok = bool(repeatability_ok and before == after)
    first.update({
        "schema_version": RUNTIME_PROBE_SCHEMA_VERSION,
        "orchestrator_version": RUNTIME_PROBE_ORCHESTRATOR_VERSION,
        "scenario_version": RUNTIME_PROBE_SCENARIO_VERSION,
        "scenario_digest": RUNTIME_PROBE_SCENARIO_DIGEST,
        "limits_digest": RUNTIME_PROBE_LIMITS_DIGEST,
        "worker_digest": RUNTIME_PROBE_WORKER_DIGEST,
        "probe_identity_digest": RUNTIME_PROBE_IDENTITY_DIGEST,
        "spec_digest": spec["spec_digest"],
        "code_fingerprint": before,
        "repeat_count": len(runs),
        "repeatability_ok": repeatability_ok,
        "evidence_integrity_ok": evidence_integrity_ok,
        "migration_evidence_repeatability": migration_repeatability,
        "cache_key": cache_key,
        "cache_hit": False,
        "issues": list(dict.fromkeys(issues)),
    })
    first["ok"] = not first["issues"]
    first["failure_class"] = "none" if first["ok"] else "candidate_contract"
    if before == after:
        _runtime_probe_cache_put(cache_key, first)
    return first


def validate_dynamic_precompute_contract(
    probe: dict[str, Any],
    *,
    name: str,
    owner_file: str,
    build_phase: str,
    max_build_ms: int,
    max_entries: int,
    max_bytes: int,
    key_shape: str,
    fallback: str,
    require_action_influence: bool = False,
    require_key_variation: bool = False,
) -> list[str]:
    rows = [
        row for row in probe.get("artifacts") or []
        if row.get("name") == name and row.get("owner_file") == owner_file
    ]
    if not rows:
        return [f"dynamic_precompute_missing:{owner_file}:{name}"]
    row = rows[0]
    errors = [str(item) for item in row.get("issues") or []]
    comparisons = (
        ("entries", row.get("entries"), int(max_entries), "maximum"),
        ("deep_bytes", row.get("deep_bytes"), int(max_bytes), "maximum"),
        ("import_elapsed_ms", row.get("import_elapsed_ms"), int(max_build_ms), "maximum"),
    )
    for field, actual, declared, _kind in comparisons:
        if actual is None or float(actual) > float(declared):
            errors.append(f"dynamic_precompute_{field}:{actual!r}>declared:{declared}")
    if row.get("observed_build_phase") != build_phase:
        errors.append(
            f"dynamic_precompute_build_phase:{row.get('observed_build_phase')!r}!={build_phase!r}"
        )
    if row.get("observed_key_shape") != key_shape:
        errors.append(
            f"dynamic_precompute_key_shape:{row.get('observed_key_shape')!r}!={key_shape!r}"
        )
    if int(row.get("consumer_reads") or 0) < 1:
        errors.append("dynamic_precompute_consumer_reads_zero")
    if fallback != "legal_baseline" or row.get("fallback_ok") is not True:
        errors.append("dynamic_precompute_legal_baseline_fallback_failed")
    if require_action_influence and row.get("value_affects_final_wire") is not True:
        errors.append("dynamic_precompute_value_no_final_wire_influence")
    if (
        require_key_variation
        and row.get("lookup_key_varies_across_consumer_scenarios") is not True
    ):
        errors.append("dynamic_precompute_lookup_key_static")
    return list(dict.fromkeys(errors))
