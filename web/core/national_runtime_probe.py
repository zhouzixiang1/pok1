"""Sandboxed dynamic evidence for the current typed national policy ABI."""

from __future__ import annotations

import copy
from collections import OrderedDict
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
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
    snapshot_managed_bot_sources,
)
from bot_namespace import STRICT_ARTIFACT_FILES
from national_native import NATIONAL_DECISION_RUNTIME_VERSION
from national_runtime_authority import (
    current_system_native_runtime_identity,
    system_native_runtime_identity_structure_issues,
)
from national_runtime_probe_scenarios import (
    DECISION_SCENARIOS,
    LINE_SCENARIO_PAIRS,
    RUNTIME_PROBE_SCENARIO_DIGEST,
    RUNTIME_PROBE_SCENARIO_VERSION,
)


RUNTIME_PROBE_SCHEMA_VERSION = 18
# The probe cache is also a quality-evidence cache.  Its identity must change
# when the system-owned wire client changes, even when the candidate's five
# artifact files have not.  Otherwise a changed name/stream/decision path
# could inherit a result that was exercised against an older native template.
RUNTIME_PROBE_ORCHESTRATOR_VERSION = 19
RUNTIME_PROBE_WORKER_VERSION = 19
RUNTIME_PROBE_REPEATABILITY_SCHEMA_VERSION = 3
# Only this bounded, redacted semantic projection participates in repeated-run
# equality.  A candidate's deadline-dependent refinement trace is evidence of
# bounded work, but it is not a stable final action contract.
RUNTIME_PROBE_REPEATABILITY_VIEW_CONTRACT = (
    "stable-safety-semantics-per-scenario-refinement-v3"
)
RUNTIME_PROBE_MAX_REPEAT_VIEW_DIGESTS = 8
RUNTIME_PROBE_MAX_REPEAT_DIFF_PATHS = 64
RUNTIME_PROBE_REPEATABILITY_DIGEST_ALGORITHM = "sha256-canonical-json-v1"
RUNTIME_PROBE_REPEATABILITY_REDACTION = {
    "repeat_views": "digest-and-json-pointer-only",
    "candidate_source": "omitted",
    "raw_context": "omitted",
    "worker_stdout_stderr": "omitted",
}
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_JSON_POINTER_RE = re.compile(r"/[A-Za-z0-9_./~\-]*\Z")
_CANONICAL_WIRE_RE = re.compile(r"(?:fold|call|check|allin|raise [1-9]\d*)\Z")
_LINE_ACTIVITY_DIMENSIONS = frozenset(
    str(pair["dimension"]) for pair in LINE_SCENARIO_PAIRS
)
_COUNTERFACTUAL_ACTIVITY_DIMENSIONS = frozenset({
    "action_profile",
    "terminal_response",
    "showdown_range",
})
_MATCH_CONTROL_ACTIVITY_ROWS = frozenset({
    "strict_win",
    "equality_boundary",
    "malformed_proof",
})
_OFFICIAL_TRANSCRIPT_SCENARIO_IDS = frozenset(
    str(scenario["id"]) for scenario in DECISION_SCENARIOS
)
RUNTIME_PROBE_TIMEOUT_SEC = 45.0
RUNTIME_PROBE_REPEATS = 2
RUNTIME_PROBE_MAX_IMPORT_MS = 2_500.0
#: Extra attempts granted to a watchdog-timed-out probe run before the
#: timeout is accepted as a real failure. Under sustained CPU load
#: (saturator streams + native matches) a single slow run is a load
#: artifact; see run_national_runtime_probe.
RUNTIME_PROBE_TIMEOUT_EXTRA_ATTEMPTS = 2
_TIMEOUT_RUN_EXTRA_ATTEMPTS = RUNTIME_PROBE_TIMEOUT_EXTRA_ATTEMPTS
RUNTIME_PROBE_MAX_OUTPUT_BYTES = 1024 * 1024
RUNTIME_PROBE_CACHE_MAX_ENTRIES = 128

_RUNTIME_PROBE_CACHE: OrderedDict[str, dict[str, Any]] = OrderedDict()
_RUNTIME_PROBE_CACHE_LOCK = threading.Lock()


def _canonical_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def _trusted_file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _current_native_template_identity() -> dict[str, Any]:
    """Return the exact system-owned runtime identity used by this process.

    This deliberately uses the same authority as the static native-contract
    gate.  The result binds both ``NATIVE_BOT_TEMPLATE`` and
    ``NATIVE_PRECOMPUTE_TEMPLATE`` rather than a version label that either
    decision-changing system template could forget to bump.
    """

    identity = current_system_native_runtime_identity()
    if system_native_runtime_identity_structure_issues(identity):
        raise RuntimeError("native_runtime_identity_invalid")
    return dict(identity)


RUNTIME_PROBE_NATIVE_TEMPLATE_IDENTITY = _current_native_template_identity()
RUNTIME_PROBE_NATIVE_TEMPLATE_DIGEST = _canonical_digest(
    RUNTIME_PROBE_NATIVE_TEMPLATE_IDENTITY
)


def runtime_probe_native_template_evidence() -> dict[str, Any]:
    """Fields that a reusable quality/precommit receipt must carry.

    Callers compare the canonical two-file system runtime object and its
    digest.  A missing field, malformed value, or a change to either the raw
    TCP entrypoint or system precompute is therefore stale rather than a
    compatibility fallback.
    """

    return {
        # Receipts are handed to independent quality/precommit/commit callers.
        # Do not let a caller mutating its nested ``artifacts`` projection
        # mutate this process's expected schema-2 authority in place.
        "native_runtime_template_identity": copy.deepcopy(
            RUNTIME_PROBE_NATIVE_TEMPLATE_IDENTITY
        ),
        "native_runtime_template_digest": RUNTIME_PROBE_NATIVE_TEMPLATE_DIGEST,
    }


def runtime_probe_native_template_evidence_matches(
    evidence: dict[str, Any] | None,
) -> bool:
    """Whether persisted evidence is for this exact loaded native template."""

    return isinstance(evidence, dict) and all(
        evidence.get(key) == value
        for key, value in runtime_probe_native_template_evidence().items()
    )


def runtime_probe_limits() -> dict[str, Any]:
    executor_path = Path(__file__).with_name("managed_bot_executor.py")
    socket_path = Path(__file__).with_name("managed_bot_socket.py")
    return {
        "timeout_sec": RUNTIME_PROBE_TIMEOUT_SEC,
        "repeats": RUNTIME_PROBE_REPEATS,
        "max_import_ms": RUNTIME_PROBE_MAX_IMPORT_MS,
        "max_output_bytes": RUNTIME_PROBE_MAX_OUTPUT_BYTES,
        "sandbox": "central-managed-executor-sealed-five-file-source-seccomp-v2",
        "managed_executor_sha256": hashlib.sha256(
            executor_path.read_bytes()
        ).hexdigest(),
        "managed_socket_sha256": hashlib.sha256(
            socket_path.read_bytes()
        ).hexdigest(),
    }


RUNTIME_PROBE_LIMITS_DIGEST = _canonical_digest(runtime_probe_limits())
RUNTIME_PROBE_WORKER_DIGEST = _trusted_file_digest(
    Path(__file__).with_name("national_runtime_probe_worker.py")
)


def _runtime_probe_identity_payload(
    native_template_identity: dict[str, Any],
    native_template_digest: str,
) -> dict[str, Any]:
    """Return the complete immutable subject of the probe identity digest."""

    return {
        "schema_version": RUNTIME_PROBE_SCHEMA_VERSION,
        "orchestrator_version": RUNTIME_PROBE_ORCHESTRATOR_VERSION,
        "worker_version": RUNTIME_PROBE_WORKER_VERSION,
        "worker_digest": RUNTIME_PROBE_WORKER_DIGEST,
        "scenario_digest": RUNTIME_PROBE_SCENARIO_DIGEST,
        "limits_digest": RUNTIME_PROBE_LIMITS_DIGEST,
        "native_runtime_template_identity": native_template_identity,
        "native_runtime_template_digest": native_template_digest,
        "policy_abi": "decision_context_v1_typed_intent_v1",
        "repeatability": {
            "schema_version": RUNTIME_PROBE_REPEATABILITY_SCHEMA_VERSION,
            "view_contract": RUNTIME_PROBE_REPEATABILITY_VIEW_CONTRACT,
            "max_view_digests": RUNTIME_PROBE_MAX_REPEAT_VIEW_DIGESTS,
            "max_diff_paths": RUNTIME_PROBE_MAX_REPEAT_DIFF_PATHS,
        },
    }


RUNTIME_PROBE_IDENTITY_DIGEST = _canonical_digest(
    _runtime_probe_identity_payload(
        RUNTIME_PROBE_NATIVE_TEMPLATE_IDENTITY,
        RUNTIME_PROBE_NATIVE_TEMPLATE_DIGEST,
    )
)


def clear_runtime_probe_cache() -> None:
    with _RUNTIME_PROBE_CACHE_LOCK:
        _RUNTIME_PROBE_CACHE.clear()


def _bot_code_fingerprint(root: Path) -> str:
    from bot_artifact import hash_path

    return hash_path(root)


def build_runtime_probe_spec(bot_dir: str | Path) -> dict[str, Any]:
    root = Path(bot_dir).resolve()
    payload = {
        "schema_version": RUNTIME_PROBE_SCHEMA_VERSION,
        "orchestrator_version": RUNTIME_PROBE_ORCHESTRATOR_VERSION,
        "expected_worker_version": RUNTIME_PROBE_WORKER_VERSION,
        "scenario_version": RUNTIME_PROBE_SCENARIO_VERSION,
        "scenario_digest": RUNTIME_PROBE_SCENARIO_DIGEST,
        "limits_digest": RUNTIME_PROBE_LIMITS_DIGEST,
        "worker_digest": RUNTIME_PROBE_WORKER_DIGEST,
        "probe_identity_digest": RUNTIME_PROBE_IDENTITY_DIGEST,
        **runtime_probe_native_template_evidence(),
        "policy_abi": "decision_context_v1_typed_intent_v1",
        "expected_decision_runtime_version": NATIONAL_DECISION_RUNTIME_VERSION,
        "max_import_ms": RUNTIME_PROBE_MAX_IMPORT_MS,
        "code_fingerprint": _bot_code_fingerprint(root),
    }
    payload["spec_digest"] = _canonical_digest(payload)
    return payload


def _cache_key(
    spec: dict[str, Any],
    *,
    timeout_sec: float,
    repeats: int,
) -> str:
    return _canonical_digest({
        "probe_identity_digest": RUNTIME_PROBE_IDENTITY_DIGEST,
        "spec_digest": spec["spec_digest"],
        "timeout_sec": float(timeout_sec),
        "repeats": max(2, int(repeats)),
    })


def _cache_get(key: str) -> dict[str, Any] | None:
    with _RUNTIME_PROBE_CACHE_LOCK:
        value = _RUNTIME_PROBE_CACHE.pop(key, None)
        if value is None:
            return None
        _RUNTIME_PROBE_CACHE[key] = value
        result = copy.deepcopy(value)
    result["cache_hit"] = True
    return result


def _cache_put(key: str, value: dict[str, Any]) -> None:
    stored = copy.deepcopy(value)
    stored["cache_hit"] = False
    with _RUNTIME_PROBE_CACHE_LOCK:
        _RUNTIME_PROBE_CACHE.pop(key, None)
        _RUNTIME_PROBE_CACHE[key] = stored
        while len(_RUNTIME_PROBE_CACHE) > RUNTIME_PROBE_CACHE_MAX_ENTRIES:
            _RUNTIME_PROBE_CACHE.popitem(last=False)


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
            "issues": [
                f"runtime_probe_isolation_unavailable:{type(exc).__name__}:"
                f"{str(exc)[:180]}"
            ],
        }
    try:
        source_snapshot = snapshot_managed_bot_sources(
            root,
            expected_artifact_hash=str(spec["code_fingerprint"]),
            required_artifact_files=tuple(sorted(STRICT_ARTIFACT_FILES)),
        )
    except Exception as exc:
        return {
            "ok": False,
            "failure_class": "candidate_contract",
            "issues": [
                f"runtime_probe_candidate_snapshot_failed:{type(exc).__name__}:"
                f"{str(exc)[:180]}"
            ],
        }

    worker = _probe_worker_path()
    scenario = Path(__file__).with_name(
        "national_runtime_probe_scenarios.py"
    ).resolve()
    with (
        tempfile.TemporaryDirectory(prefix="pok_typed_probe_work_") as work_dir,
        tempfile.TemporaryDirectory(prefix="pok_typed_probe_out_") as output_dir,
    ):
        work_root = Path(work_dir)
        output_root = Path(output_dir)
        shutil.copyfile(worker, work_root / "worker.py")
        shutil.copyfile(
            scenario,
            work_root / "national_runtime_probe_scenarios.py",
        )
        sealed_bot = work_root / "bot"
        sealed_bot.mkdir(mode=0o700)
        for name, payload in source_snapshot.files:
            target = sealed_bot / name
            with target.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            target.chmod(0o444)
        sealed_bot.chmod(0o555)
        (work_root / "spec.json").write_text(
            json.dumps(spec, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        for name in ("worker.py", "national_runtime_probe_scenarios.py", "spec.json"):
            (work_root / name).chmod(0o444)

        report_host = output_root / "report.json"
        phase_host = output_root / "phase.txt"
        stdout_host = output_root / "stdout.log"
        stderr_host = output_root / "stderr.log"
        command = [
            str(runtime.python),
            "-I",
            "-B",
            "/work/worker.py",
            "/work/bot",
            "/output/report.json",
            "/work/spec.json",
        ]
        with stdout_host.open("wb") as stdout_fp, stderr_host.open("wb") as stderr_fp:
            try:
                managed = launch_isolated_worker(
                    work_root,
                    command,
                    environment={"PYTHONHASHSEED": "0"},
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
                    "issues": [
                        f"runtime_probe_launch_error:{type(exc).__name__}:"
                        f"{str(exc)[:180]}"
                    ],
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

        stdout_size = stdout_host.stat().st_size
        stderr_size = stderr_host.stat().st_size
        stderr_text = (
            stderr_host.read_text(encoding="utf-8", errors="replace")[:2000]
            if stderr_size
            else ""
        )
        try:
            snapshot_managed_bot_sources(
                root,
                expected_artifact_hash=str(spec["code_fingerprint"]),
                required_artifact_files=tuple(sorted(STRICT_ARTIFACT_FILES)),
            )
        except Exception as exc:
            return {
                "ok": False,
                "failure_class": "candidate_contract",
                "issues": [
                    f"runtime_probe_candidate_posthash_failed:{type(exc).__name__}:"
                    f"{str(exc)[:180]}"
                ],
                "process_returncode": returncode,
            }
        if (
            stdout_size > RUNTIME_PROBE_MAX_OUTPUT_BYTES
            or stderr_size > RUNTIME_PROBE_MAX_OUTPUT_BYTES
        ):
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
                "worker_stdout": stdout_host.read_text(
                    encoding="utf-8", errors="replace"
                )[:2000],
                "worker_stderr": stderr_text,
            }
        try:
            result = json.loads(report_host.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {
                "ok": False,
                "failure_class": "probe_infra",
                "issues": [
                    f"runtime_probe_report_invalid:{type(exc).__name__}:"
                    f"{str(exc)[:180]}"
                ],
            }
        if not isinstance(result, dict):
            return {
                "ok": False,
                "failure_class": "probe_infra",
                "issues": ["runtime_probe_report_not_object"],
            }
        result["process_returncode"] = returncode
        result["managed_isolation"] = asdict(managed.isolation)
        output_pollution: list[str] = []
        if stdout_size:
            result["worker_stdout"] = stdout_host.read_text(
                encoding="utf-8", errors="replace"
            )[:2000]
            output_pollution.append(
                f"runtime_probe_candidate_stdout:{stdout_size}_bytes"
            )
        if stderr_size:
            result["worker_stderr"] = stderr_text
            output_pollution.append(
                f"runtime_probe_candidate_stderr:{stderr_size}_bytes"
            )
        if output_pollution:
            result["issues"] = list(dict.fromkeys([
                *(result.get("issues") or []),
                *output_pollution,
            ]))
            result["ok"] = False
            if result.get("failure_class") != "probe_infra":
                result["failure_class"] = "candidate_contract"
        return result


def _stable_runtime(value: dict[str, Any]) -> dict[str, Any]:
    """Project runtime facts that are safety semantics, not timing traces."""

    return {
        "runtime_version": value.get("runtime_version"),
        "socket_fallback_decision": value.get("socket_fallback_decision"),
        "baseline_published": bool(value.get("baseline_published")),
        "baseline_target_met": bool(value.get("baseline_target_met")),
        "policy_baseline_decision": value.get("policy_baseline_decision"),
        "timed_out": bool(value.get("timed_out")),
        "worker_terminated": bool(value.get("worker_terminated")),
        "completed": bool(value.get("completed")),
    }


def _refinement_active(value: dict[str, Any]) -> bool:
    """Whether a row's final action may have consumed variable refinement.

    The worker reports these counters after the system-owned runtime has
    applied the candidate iterator.  The counters themselves are deliberately
    not compared across repeats: a legal bounded iterator can stop at a
    different deadline boundary under ordinary host contention.
    """

    try:
        return bool(
            int(value.get("refinement_messages") or 0)
            or int(value.get("trusted_refinement_steps") or 0)
        )
    except (TypeError, ValueError, OverflowError):
        # A malformed system metric must not hide the final action.  Treat it
        # as inactive so the decision and wire stay in the compared view.
        return False


def _stable_policy_entrypoints(value: Any) -> dict[str, Any]:
    """Keep typed baseline/safety proof, omit deadline-variable yields."""

    entrypoints = value if isinstance(value, dict) else {}
    rows: list[dict[str, Any]] = []
    raw_rows = entrypoints.get("rows")
    if not isinstance(raw_rows, list):
        raw_rows = []
    for row in raw_rows:
        if not isinstance(row, dict):
            rows.append({"row_shape": type(row).__name__})
            continue
        baseline_work = row.get("baseline_work")
        baseline_work = baseline_work if isinstance(baseline_work, dict) else {}
        rows.append({
            "scenario": row.get("scenario"),
            "decision": row.get("decision"),
            "ok": row.get("ok"),
            "issue": row.get("issue"),
            "baseline_work": {
                "instrumented": baseline_work.get("instrumented"),
                "evaluator_calls": baseline_work.get("evaluator_calls"),
                "evaluator_call_cap": baseline_work.get("evaluator_call_cap"),
                "evaluator_calls_by_name": (
                    baseline_work.get("evaluator_calls_by_name") or {}
                ),
            },
        })
    return {
        "ok": entrypoints.get("ok"),
        "issues": entrypoints.get("issues") or [],
        "rows": rows,
    }


def _stable_action_fields(
    raw: dict[str, Any],
    *,
    actions: tuple[tuple[str, str, str], ...],
) -> dict[str, Any]:
    """Bind each final action to its exact scenario's refinement status.

    A dedicated budget probe is never authority to omit a wire from another
    scenario.  Every non-budget observation must carry its own system-owned
    ``*_refinement_active`` bit; when it is false the final typed decision and
    wire stay in the repeat view.
    """

    stable: dict[str, Any] = {}
    for activity_key, decision_key, wire_key in actions:
        active = raw.get(activity_key)
        stable[activity_key] = active
        if active is not True:
            stable[decision_key] = raw.get(decision_key)
            stable[wire_key] = raw.get(wire_key)
    return stable


def _stable_line_reachability(value: Any) -> dict[str, Any]:
    """Keep causal/context truth and per-scenario action evidence."""

    evidence = value if isinstance(value, dict) else {}
    dimensions: dict[str, Any] = {}
    raw_dimensions = evidence.get("dimensions")
    if not isinstance(raw_dimensions, dict):
        raw_dimensions = {}
    for name, raw in sorted(raw_dimensions.items(), key=lambda item: str(item[0])):
        if not isinstance(raw, dict):
            dimensions[str(name)] = {"shape": type(raw).__name__}
            continue
        stable = {
            key: raw.get(key)
            for key in (
                "ok",
                "flag",
                "positive_scenario",
                "negative_scenario",
                "mixed_identity_scenario",
                "mixing_class",
                "positive",
                "negative",
                "mixed_identity",
                "producer_reachable",
                "mixed_identity_context_digest",
                "positive_without_cards_digest",
                "mixed_without_cards_digest",
                "mixing_context_exact",
                "bounded_mixing",
                "stable_context_normalization_paths",
                "mixing_comparison_ignored_paths",
                "matched_control_context_digest",
                "positive_without_ablation_digest",
                "matched_without_ablation_digest",
                "ablation_paths",
                "context_ablation_exact",
                "consumer_wire_effect",
                "policy_changed",
                "causal_passed",
                "socket_validated",
            )
        }
        stable.update(_stable_action_fields(
            raw,
            actions=(
                (
                    "positive_refinement_active",
                    "positive_decision",
                    "positive_wire",
                ),
                (
                    "negative_refinement_active",
                    "negative_decision",
                    "negative_wire",
                ),
                (
                    "mixed_identity_refinement_active",
                    "mixed_identity_decision",
                    "mixed_identity_wire",
                ),
                (
                    "matched_control_refinement_active",
                    "matched_control_decision",
                    "matched_control_wire",
                ),
            ),
        ))
        dimensions[str(name)] = stable
    return {
        "ok": evidence.get("ok"),
        "issues": evidence.get("issues") or [],
        "system_issues": evidence.get("system_issues") or [],
        "candidate_issues": evidence.get("candidate_issues") or [],
        "dimensions": dimensions,
    }


def _stable_counterfactuals(value: Any) -> dict[str, Any]:
    """Keep causal behavior and per-profile final action evidence."""

    evidence = value if isinstance(value, dict) else {}
    dimensions: dict[str, Any] = {}
    raw_dimensions = evidence.get("dimensions")
    if not isinstance(raw_dimensions, dict):
        raw_dimensions = {}
    for name, raw in sorted(raw_dimensions.items(), key=lambda item: str(item[0])):
        if not isinstance(raw, dict):
            dimensions[str(name)] = {"shape": type(raw).__name__}
            continue
        stable = {
            key: raw.get(key)
            for key in (
                "scenario",
                "left_profile",
                "right_profile",
                "changed",
                "positive_wire_effect",
                "negative_control_stable",
                "negative_control_kind",
                "causal_passed",
                "socket_validated",
            )
        }
        stable.update(_stable_action_fields(
            raw,
            actions=(
                ("left_refinement_active", "left_decision", "left_wire"),
                ("right_refinement_active", "right_decision", "right_wire"),
                (
                    "negative_left_refinement_active",
                    "negative_left_decision",
                    "negative_left_wire",
                ),
                (
                    "negative_right_refinement_active",
                    "negative_right_decision",
                    "negative_right_wire",
                ),
            ),
        ))
        dimensions[str(name)] = stable
    return {
        "ok": evidence.get("ok"),
        "issues": evidence.get("issues") or [],
        "system_issues": evidence.get("system_issues") or [],
        "candidate_issues": evidence.get("candidate_issues") or [],
        "dimensions": dimensions,
    }


def _stable_match_control(value: Any) -> dict[str, Any]:
    """Compare lock-win safety with its exact scenario activity bit."""

    evidence = value if isinstance(value, dict) else {}
    rows: dict[str, Any] = {}
    raw_rows = evidence.get("rows")
    if not isinstance(raw_rows, dict):
        raw_rows = {}
    for name, raw in sorted(raw_rows.items(), key=lambda item: str(item[0])):
        if not isinstance(raw, dict):
            rows[str(name)] = {"shape": type(raw).__name__}
            continue
        stable = {
            key: raw.get(key)
            for key in (
                "expectation",
                "context_digest",
                "expected_system_issues",
                "observed_system_issues",
                "expectation_met",
            )
        }
        stable.update(_stable_action_fields(
            raw,
            actions=(("refinement_active", "decision", "wire"),),
        ))
        rows[str(name)] = stable
    return {
        "ok": evidence.get("ok"),
        "system_issues": evidence.get("system_issues") or [],
        "candidate_issues": evidence.get("candidate_issues") or [],
        "rows": rows,
        "socket_validated": evidence.get("socket_validated"),
        "causal_passed": evidence.get("causal_passed"),
        "strict_comparison": evidence.get("strict_comparison"),
    }


def _stable_budget_refinement(value: Any) -> dict[str, Any]:
    """Compare capability truth, while omitting variable budget consumption."""

    scaling = value if isinstance(value, dict) else {}
    def stratum(name: str) -> dict[str, Any]:
        row = scaling.get(name)
        row = row if isinstance(row, dict) else {}
        active = _refinement_active(row)
        stable = {
            key: row.get(key)
            for key in (
                "baseline_published",
                "baseline_target_met",
                "worker_seed",
            )
        }
        stable["refinement_active"] = active
        # With no active refinement, the final action is a baseline/fallback
        # safety contract.  Once refinement is active it is deadline-variable
        # and the capability booleans below remain the required comparison.
        if not active:
            stable["decision"] = row.get("decision")
            stable["wire"] = row.get("wire")
        return stable

    return {
        "probe_kind": scaling.get("probe_kind"),
        "scenario": scaling.get("scenario"),
        "ok": scaling.get("ok"),
        "active": bool(scaling.get("active")),
        "system_issues": scaling.get("system_issues") or [],
        "candidate_issues": scaling.get("candidate_issues") or [],
        "capability_issues": scaling.get("capability_issues") or [],
        "worker_seed_equal": scaling.get("worker_seed_equal"),
        "bounded_work": scaling.get("bounded_work"),
        "scaled_or_exhausted": scaling.get("scaled_or_exhausted"),
        "changes_sanitized_decision": scaling.get(
            "changes_sanitized_decision"
        ),
        "short": stratum("short"),
        "long": stratum("long"),
    }


def _repeatability_view(result: dict[str, Any]) -> dict[str, Any]:
    """Return the bounded semantic subject of repeatability comparison.

    This view intentionally is *not* persisted.  The returned result exposes
    only its digest and JSON-pointer differences, so a quality receipt cannot
    turn sandbox diagnostics, raw contexts, or candidate source into a second
    artifact channel.
    """

    decisions: list[dict[str, Any]] = []
    raw_decisions = result.get("official_transcript_decisions")
    if not isinstance(raw_decisions, list):
        raw_decisions = []
    for row in raw_decisions:
        if not isinstance(row, dict):
            decisions.append({"row_shape": type(row).__name__})
            continue
        runtime = row.get("runtime")
        runtime = runtime if isinstance(runtime, dict) else {}
        refinement_active = _refinement_active(runtime)
        decision = {
            "id": row.get("id"),
            "ok": row.get("ok"),
            "issues": row.get("issues") or [],
            "context_digest": row.get("context_digest"),
            "setup_wire": row.get("setup_wire") or [],
            "runtime": _stable_runtime(runtime),
            "refinement_active": refinement_active,
        }
        if not refinement_active:
            decision["decision"] = row.get("decision")
            decision["wire"] = row.get("wire")
        decisions.append(decision)
    return {
        "identity": {
            key: result.get(key)
            for key in (
                "schema_version",
                "orchestrator_version",
                "worker_version",
                "scenario_version",
                "scenario_digest",
                "limits_digest",
                "worker_digest",
                "probe_identity_digest",
                "native_runtime_template_identity",
                "native_runtime_template_digest",
                "policy_abi",
                "spec_digest",
                "code_fingerprint",
            )
        },
        "run": {
            "ok": result.get("ok"),
            "failure_class": result.get("failure_class"),
            "issues": result.get("issues") or [],
            "process_returncode": result.get("process_returncode"),
        },
        "official_transcript_decisions": decisions,
        "line_reachability": _stable_line_reachability(
            result.get("line_reachability")
        ),
        "persistent_memory": result.get("persistent_memory") or {},
        "policy_entrypoints": _stable_policy_entrypoints(
            result.get("policy_entrypoints")
        ),
        "policy_counterfactuals": _stable_counterfactuals(
            result.get("policy_counterfactuals")
        ),
        "match_control_consumer": _stable_match_control(
            result.get("match_control_consumer")
        ),
        "budget_scaled_refinement": _stable_budget_refinement(
            result.get("budget_scaled_refinement")
        ),
        "managed_isolation": result.get("managed_isolation") or {},
    }


def _identity_issues(result: dict[str, Any], spec: dict[str, Any]) -> list[str]:
    expected = {
        "schema_version": RUNTIME_PROBE_SCHEMA_VERSION,
        "orchestrator_version": RUNTIME_PROBE_ORCHESTRATOR_VERSION,
        "worker_version": RUNTIME_PROBE_WORKER_VERSION,
        "scenario_version": RUNTIME_PROBE_SCENARIO_VERSION,
        "scenario_digest": RUNTIME_PROBE_SCENARIO_DIGEST,
        "limits_digest": RUNTIME_PROBE_LIMITS_DIGEST,
        "worker_digest": RUNTIME_PROBE_WORKER_DIGEST,
        "probe_identity_digest": RUNTIME_PROBE_IDENTITY_DIGEST,
        **runtime_probe_native_template_evidence(),
        "policy_abi": "decision_context_v1_typed_intent_v1",
        "spec_digest": spec["spec_digest"],
        "code_fingerprint": spec["code_fingerprint"],
    }
    return [
        f"runtime_probe_{key}_mismatch"
        for key, value in expected.items()
        if result.get(key) != value
    ]


def _per_scenario_activity_issues(result: dict[str, Any]) -> list[str]:
    """Require the complete worker-owned action proof before comparison.

    Refinement can make one final action deadline-variable, but it never
    makes the row or its typed action/wire evidence optional.  The expected
    fixed dimension sets are part of the worker/orchestrator schema binding:
    accepting an empty or partial section would let two equally damaged
    reports erase the comparisons they are meant to prove.
    """

    issues: list[str] = []

    def valid_typed_intent(value: Any) -> bool:
        if type(value) is not dict:
            return False
        kind = value.get("kind")
        if kind not in {"fold", "pass", "allin", "raise"}:
            return False
        if kind == "raise":
            return (
                set(value) == {"kind", "raise_to"}
                and type(value.get("raise_to")) is int
                and value["raise_to"] > 0
            )
        return set(value) == {"kind"}

    def valid_wire(value: Any) -> bool:
        return (
            isinstance(value, str)
            and _CANONICAL_WIRE_RE.fullmatch(value) is not None
        )

    def validate_transcripts(value: Any) -> None:
        if not isinstance(value, list):
            issues.append("runtime_probe_official_transcript_list_invalid")
            return
        rows: dict[str, dict[str, Any]] = {}
        for index, raw in enumerate(value):
            if not isinstance(raw, dict):
                issues.append(
                    "runtime_probe_official_transcript_row_invalid:"
                    f"index={index}"
                )
                continue
            scenario_id = raw.get("id")
            if not isinstance(scenario_id, str):
                issues.append(
                    "runtime_probe_official_transcript_id_invalid:"
                    f"index={index}"
                )
                continue
            if scenario_id in rows:
                issues.append(
                    "runtime_probe_official_transcript_id_duplicate:"
                    f"{scenario_id}"
                )
                continue
            rows[scenario_id] = raw
        if set(rows) != _OFFICIAL_TRANSCRIPT_SCENARIO_IDS:
            issues.append("runtime_probe_official_transcript_id_set_mismatch")
        for scenario_id in sorted(_OFFICIAL_TRANSCRIPT_SCENARIO_IDS):
            raw = rows.get(scenario_id)
            if not isinstance(raw, dict):
                continue
            if raw.get("ok") is not True:
                issues.append(
                    "runtime_probe_official_transcript_not_passed:"
                    f"{scenario_id}"
                )
            row_issues = raw.get("issues")
            if not isinstance(row_issues, list):
                issues.append(
                    "runtime_probe_official_transcript_issues_invalid:"
                    f"{scenario_id}"
                )
            elif row_issues:
                issues.append(
                    "runtime_probe_official_transcript_issues_not_clear:"
                    f"{scenario_id}"
                )
            if not valid_typed_intent(raw.get("decision")):
                issues.append(
                    "runtime_probe_official_transcript_decision_invalid:"
                    f"{scenario_id}"
                )
            if not valid_wire(raw.get("wire")):
                issues.append(
                    "runtime_probe_official_transcript_wire_invalid:"
                    f"{scenario_id}"
                )
            runtime = raw.get("runtime")
            if not isinstance(runtime, dict):
                issues.append(
                    "runtime_probe_official_transcript_runtime_invalid:"
                    f"{scenario_id}"
                )
                continue
            for metric in (
                "refinement_messages",
                "trusted_refinement_steps",
            ):
                value = runtime.get(metric)
                if type(value) is not int or value < 0:
                    issues.append(
                        "runtime_probe_official_transcript_refinement_metric_invalid:"
                        f"{scenario_id}:{metric}"
                    )

    def validate_actions(
        raw: Any,
        *,
        label: str,
        actions: tuple[tuple[str, str, str], ...],
    ) -> None:
        if not isinstance(raw, dict):
            issues.append(
                "runtime_probe_per_scenario_refinement_row_invalid:" + label
            )
            return
        for activity_key, decision_key, wire_key in actions:
            # The worker owns these rows, so every declared observation must
            # carry its activity bit even if a broken run omitted the action.
            # Otherwise a pair of identically malformed reports could erase a
            # final-action comparison from the repeat view.
            if type(raw.get(activity_key)) is not bool:
                issues.append(
                    "runtime_probe_per_scenario_refinement_activity_invalid:"
                    f"{label}:{activity_key}"
                )
            if not valid_typed_intent(raw.get(decision_key)):
                issues.append(
                    "runtime_probe_per_scenario_refinement_decision_invalid:"
                    f"{label}:{decision_key}"
                )
            if not valid_wire(raw.get(wire_key)):
                issues.append(
                    "runtime_probe_per_scenario_refinement_wire_invalid:"
                    f"{label}:{wire_key}"
                )

    def validate_section(
        value: Any,
        *,
        label: str,
        rows_key: str,
        expected_rows: frozenset[str],
        actions: tuple[tuple[str, str, str], ...],
        issue_keys: tuple[str, ...],
    ) -> None:
        if not isinstance(value, dict):
            issues.append(
                "runtime_probe_per_scenario_refinement_section_invalid:" + label
            )
            return
        if value.get("ok") is not True:
            issues.append(
                "runtime_probe_per_scenario_refinement_section_not_passed:"
                + label
            )
        for key in issue_keys:
            section_issues = value.get(key)
            if not isinstance(section_issues, list):
                issues.append(
                    "runtime_probe_per_scenario_refinement_section_issues_invalid:"
                    f"{label}:{key}"
                )
            elif section_issues:
                issues.append(
                    "runtime_probe_per_scenario_refinement_section_issues_not_clear:"
                    f"{label}:{key}"
                )
        rows = value.get(rows_key)
        if not isinstance(rows, dict):
            issues.append(
                "runtime_probe_per_scenario_refinement_rows_invalid:"
                f"{label}:{rows_key}"
            )
            return
        if set(rows) != expected_rows:
            issues.append(
                "runtime_probe_per_scenario_refinement_row_set_mismatch:" + label
            )
        for name in sorted(expected_rows):
            validate_actions(
                rows.get(name),
                label=f"{label}:{name}",
                actions=actions,
            )

    validate_transcripts(result.get("official_transcript_decisions"))
    validate_section(
        result.get("line_reachability"),
        label="line",
        rows_key="dimensions",
        expected_rows=_LINE_ACTIVITY_DIMENSIONS,
        actions=(
            (
                "positive_refinement_active",
                "positive_decision",
                "positive_wire",
            ),
            (
                "negative_refinement_active",
                "negative_decision",
                "negative_wire",
            ),
            (
                "mixed_identity_refinement_active",
                "mixed_identity_decision",
                "mixed_identity_wire",
            ),
            (
                "matched_control_refinement_active",
                "matched_control_decision",
                "matched_control_wire",
            ),
        ),
        issue_keys=("issues", "system_issues", "candidate_issues"),
    )
    validate_section(
        result.get("policy_counterfactuals"),
        label="counterfactual",
        rows_key="dimensions",
        expected_rows=_COUNTERFACTUAL_ACTIVITY_DIMENSIONS,
        actions=(
            ("left_refinement_active", "left_decision", "left_wire"),
            ("right_refinement_active", "right_decision", "right_wire"),
            (
                "negative_left_refinement_active",
                "negative_left_decision",
                "negative_left_wire",
            ),
            (
                "negative_right_refinement_active",
                "negative_right_decision",
                "negative_right_wire",
            ),
        ),
        issue_keys=("issues", "system_issues", "candidate_issues"),
    )
    validate_section(
        result.get("match_control_consumer"),
        label="match_control",
        rows_key="rows",
        expected_rows=_MATCH_CONTROL_ACTIVITY_ROWS,
        actions=(("refinement_active", "decision", "wire"),),
        issue_keys=("system_issues", "candidate_issues"),
    )
    return list(dict.fromkeys(issues))


def _repeat_validation_issues(
    result: dict[str, Any],
    spec: dict[str, Any],
) -> list[str]:
    """Validate one execution before comparing it to another execution."""

    raw_issues = result.get("issues")
    if isinstance(raw_issues, list):
        issues = list(raw_issues)
    elif raw_issues is None:
        issues = []
    else:
        issues = ["runtime_probe_repeat_issues_shape_invalid"]
    issues.extend(_identity_issues(result, spec))
    issues.extend(_per_scenario_activity_issues(result))
    if result.get("ok") is not True:
        issues.append("runtime_probe_repeat_not_ok")
    if result.get("failure_class") != "none":
        issues.append(
            "runtime_probe_repeat_failure_class:"
            f"{str(result.get('failure_class'))[:80]}"
        )
    isolation = result.get("managed_isolation")
    if not isinstance(isolation, dict) or not isolation:
        issues.append("runtime_probe_repeat_managed_isolation_missing")
    if result.get("process_returncode") != 0:
        issues.append("runtime_probe_repeat_process_returncode_invalid")
    return list(dict.fromkeys(map(str, issues)))


def _json_pointer(path: str, token: str | int) -> str:
    escaped = str(token).replace("~", "~0").replace("/", "~1")
    return f"{path}/{escaped}" if path else f"/{escaped}"


def _differing_json_pointers(
    left: Any,
    right: Any,
    *,
    maximum: int,
) -> tuple[list[str], int]:
    """Return bounded JSON-pointer markers without retaining compared values."""

    pointers: list[str] = []
    count = 0

    def record(path: str) -> None:
        nonlocal count
        count += 1
        if len(pointers) < maximum:
            pointers.append(path or "/")

    def compare(first: Any, second: Any, path: str, depth: int) -> None:
        # Reports are JSON decoded and therefore acyclic.  The depth bound is
        # still a fail-closed guard for in-process test doubles or future host
        # callers: a too-deep shape gets a marker instead of recursive output.
        if depth > 64:
            record(path)
            return
        if type(first) is not type(second):
            record(path)
            return
        if isinstance(first, dict):
            keys = sorted(set(first) | set(second), key=str)
            for key in keys:
                child = _json_pointer(path, key)
                if key not in first or key not in second:
                    record(child)
                else:
                    compare(first[key], second[key], child, depth + 1)
            return
        if isinstance(first, (list, tuple)):
            maximum_length = max(len(first), len(second))
            for index in range(maximum_length):
                child = _json_pointer(path, index)
                if index >= len(first) or index >= len(second):
                    record(child)
                else:
                    compare(first[index], second[index], child, depth + 1)
            return
        if first != second:
            record(path)

    compare(left, right, "", 0)
    return pointers, count


def _repeatability_evidence(
    views: list[dict[str, Any]],
) -> dict[str, Any]:
    """Persist only bounded redacted repeat comparison evidence.

    Digests bind the full semantic projection, while JSON pointers identify
    which stable contract field differed.  Values are intentionally not
    recorded here: candidate source, raw context, stdout/stderr, and the full
    time-sensitive trace stay outside this receipt object.
    """

    digests = [_canonical_digest(view) for view in views]
    recorded_digests = [
        {"repeat": index, "sha256": digest}
        for index, digest in enumerate(
            digests[:RUNTIME_PROBE_MAX_REPEAT_VIEW_DIGESTS],
            start=1,
        )
    ]
    differences: list[dict[str, Any]] = []
    difference_count = 0
    if views:
        reference = views[0]
        for repeat_index, view in enumerate(views[1:], start=2):
            remaining = max(0, RUNTIME_PROBE_MAX_REPEAT_DIFF_PATHS - len(differences))
            paths, count = _differing_json_pointers(
                reference,
                view,
                maximum=remaining,
            )
            difference_count += count
            differences.extend({
                "repeat": repeat_index,
                "json_pointer": path,
            } for path in paths)
    return {
        "schema_version": RUNTIME_PROBE_REPEATABILITY_SCHEMA_VERSION,
        "view_contract": RUNTIME_PROBE_REPEATABILITY_VIEW_CONTRACT,
        "view_digest_algorithm": RUNTIME_PROBE_REPEATABILITY_DIGEST_ALGORITHM,
        "repeat_count": len(views),
        "view_digest_count": len(digests),
        "view_digests": recorded_digests,
        "view_digests_truncated": (
            len(digests) > RUNTIME_PROBE_MAX_REPEAT_VIEW_DIGESTS
        ),
        "differing_path_count": difference_count,
        "differing_paths": differences,
        "differing_paths_truncated": difference_count > len(differences),
        "redaction": dict(RUNTIME_PROBE_REPEATABILITY_REDACTION),
    }


def validate_runtime_probe_repeatability_evidence(
    probe: dict[str, Any] | None,
) -> list[str]:
    """Fail closed unless a persisted repeatability receipt is self-consistent.

    The host records only digest and JSON-pointer metadata, never the repeat
    views themselves.  Every acceptance boundary calls this validator rather
    than treating the three top-level success booleans as an authority.  It
    verifies structural integrity and fenced-writer bindings; it does not
    claim cryptographic protection from an actor able to replace every durable
    checkpoint field together.
    """

    if not isinstance(probe, dict):
        return ["runtime_probe_repeatability_probe_not_object"]
    evidence = probe.get("repeatability")
    if not isinstance(evidence, dict):
        return ["runtime_probe_repeatability_evidence_missing"]

    managed_isolation = probe.get("managed_isolation")
    if not isinstance(managed_isolation, dict) or not managed_isolation:
        issues = ["runtime_probe_repeatability_managed_isolation_missing"]
    else:
        issues = []
    managed_isolation_digest = probe.get("managed_isolation_digest")
    if (
        not isinstance(managed_isolation_digest, str)
        or _SHA256_RE.fullmatch(managed_isolation_digest) is None
    ):
        issues.append("runtime_probe_repeatability_managed_isolation_digest_invalid")
    elif isinstance(managed_isolation, dict) and managed_isolation:
        if managed_isolation_digest != _canonical_digest(managed_isolation):
            issues.append(
                "runtime_probe_repeatability_managed_isolation_digest_mismatch"
            )

    # Cache/reuse/commit/formal callers receive the persisted first run rather
    # than the transient ``runs`` list.  Reapply the same fixed worker-row
    # contract here so a hand-edited receipt cannot omit an action proof after
    # fresh execution completed.
    issues.extend(_per_scenario_activity_issues(probe))

    expected_fields = {
        "schema_version",
        "view_contract",
        "view_digest_algorithm",
        "repeat_count",
        "view_digest_count",
        "view_digests",
        "view_digests_truncated",
        "differing_path_count",
        "differing_paths",
        "differing_paths_truncated",
        "redaction",
    }
    if set(evidence) != expected_fields:
        issues.append("runtime_probe_repeatability_evidence_fields_invalid")
    if evidence.get("schema_version") != RUNTIME_PROBE_REPEATABILITY_SCHEMA_VERSION:
        issues.append("runtime_probe_repeatability_schema_mismatch")
    if evidence.get("view_contract") != RUNTIME_PROBE_REPEATABILITY_VIEW_CONTRACT:
        issues.append("runtime_probe_repeatability_contract_mismatch")
    if (
        evidence.get("view_digest_algorithm")
        != RUNTIME_PROBE_REPEATABILITY_DIGEST_ALGORITHM
    ):
        issues.append("runtime_probe_repeatability_digest_algorithm_mismatch")
    if evidence.get("redaction") != RUNTIME_PROBE_REPEATABILITY_REDACTION:
        issues.append("runtime_probe_repeatability_redaction_invalid")

    repeat_count = evidence.get("repeat_count")
    if type(repeat_count) is not int or repeat_count < 2:
        issues.append("runtime_probe_repeatability_repeat_count_invalid")
        repeat_count = 0
    view_digest_count = evidence.get("view_digest_count")
    if type(view_digest_count) is not int or view_digest_count != repeat_count:
        issues.append("runtime_probe_repeatability_view_digest_count_invalid")
    view_digests = evidence.get("view_digests")
    if not isinstance(view_digests, list):
        issues.append("runtime_probe_repeatability_view_digests_not_list")
        view_digests = []
    expected_recorded = min(repeat_count, RUNTIME_PROBE_MAX_REPEAT_VIEW_DIGESTS)
    if len(view_digests) != expected_recorded:
        issues.append("runtime_probe_repeatability_view_digests_length_invalid")
    expected_digest_truncation = repeat_count > RUNTIME_PROBE_MAX_REPEAT_VIEW_DIGESTS
    if evidence.get("view_digests_truncated") is not expected_digest_truncation:
        issues.append("runtime_probe_repeatability_view_digests_truncation_invalid")
    for index, item in enumerate(view_digests, start=1):
        if not isinstance(item, dict) or set(item) != {"repeat", "sha256"}:
            issues.append("runtime_probe_repeatability_view_digest_shape_invalid")
            continue
        if item.get("repeat") != index:
            issues.append("runtime_probe_repeatability_view_digest_order_invalid")
        digest = item.get("sha256")
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            issues.append("runtime_probe_repeatability_view_digest_invalid")

    differing_path_count = evidence.get("differing_path_count")
    if type(differing_path_count) is not int or differing_path_count < 0:
        issues.append("runtime_probe_repeatability_difference_count_invalid")
        differing_path_count = -1
    differing_paths = evidence.get("differing_paths")
    if not isinstance(differing_paths, list):
        issues.append("runtime_probe_repeatability_difference_paths_not_list")
        differing_paths = []
    if len(differing_paths) > RUNTIME_PROBE_MAX_REPEAT_DIFF_PATHS:
        issues.append("runtime_probe_repeatability_difference_paths_unbounded")
    if differing_path_count >= 0 and differing_path_count < len(differing_paths):
        issues.append("runtime_probe_repeatability_difference_count_underflow")
    expected_difference_truncation = differing_path_count > len(differing_paths)
    if evidence.get("differing_paths_truncated") is not expected_difference_truncation:
        issues.append("runtime_probe_repeatability_difference_truncation_invalid")
    for item in differing_paths:
        if not isinstance(item, dict) or set(item) != {"repeat", "json_pointer"}:
            issues.append("runtime_probe_repeatability_difference_path_shape_invalid")
            continue
        repeat = item.get("repeat")
        pointer = item.get("json_pointer")
        if type(repeat) is not int or not 2 <= repeat <= repeat_count:
            issues.append("runtime_probe_repeatability_difference_repeat_invalid")
        if (
            not isinstance(pointer, str)
            or len(pointer) > 512
            or _JSON_POINTER_RE.fullmatch(pointer) is None
        ):
            issues.append("runtime_probe_repeatability_difference_pointer_invalid")

    success_flag_names = ("ok", "repeatability_ok", "evidence_integrity_ok")
    for key in success_flag_names:
        if type(probe.get(key)) is not bool:
            issues.append(f"runtime_probe_repeatability_success_flag_invalid:{key}")
    if not all(probe.get(key) is True for key in success_flag_names):
        issues.append("runtime_probe_repeatability_not_passed")
    if differing_path_count != 0 or differing_paths:
        issues.append("runtime_probe_repeatability_pass_has_differences")
    if evidence.get("differing_paths_truncated") is not False:
        issues.append("runtime_probe_repeatability_pass_difference_truncated")
    if (
        all(probe.get(key) is True for key in success_flag_names)
        and len(view_digests) == expected_recorded
        and view_digests
        and all(
            isinstance(item, dict)
            and _SHA256_RE.fullmatch(str(item.get("sha256") or "")) is not None
            for item in view_digests
        )
    ):
        first_digest = _canonical_digest(_repeatability_view(probe))
        if view_digests[0].get("sha256") != first_digest:
            issues.append("runtime_probe_repeatability_first_view_digest_mismatch")
        if any(item.get("sha256") != first_digest for item in view_digests[1:]):
            issues.append("runtime_probe_repeatability_pass_view_digests_diverge")
    return list(dict.fromkeys(issues))


def run_national_runtime_probe(
    bot_dir: str | Path,
    *,
    timeout_sec: float = RUNTIME_PROBE_TIMEOUT_SEC,
    repeats: int = RUNTIME_PROBE_REPEATS,
) -> dict[str, Any]:
    """Run fresh isolated typed-policy probes and require stable evidence."""

    root = Path(bot_dir).resolve()
    spec = build_runtime_probe_spec(root)
    before = spec["code_fingerprint"]
    key = _cache_key(spec, timeout_sec=timeout_sec, repeats=repeats)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    runs: list[dict[str, Any]] = []
    # A watchdog-timeout run is a LOAD artifact, not a candidate verdict: the
    # probe worker runs under CPU contention (saturator streams + native
    # matches) and a single slow run used to break the repeat loop, leaving
    # len(runs) < 2 -> automatic runtime_probe_non_repeatable — a hard gate
    # failure that killed v183/v184 (and 18+ versions historically) on
    # otherwise-fine candidates. Retry a timed-out run (bounded) instead of
    # counting it as a repeatability sample; genuinely persistent timeouts
    # still surface honestly once the extra budget is spent.
    timeout_run_retries_left = _TIMEOUT_RUN_EXTRA_ATTEMPTS
    target_runs = max(2, int(repeats))
    while len(runs) < target_runs:
        observed = _run_once(root, spec, timeout_sec)
        if not isinstance(observed, dict):
            observed = {
                "ok": False,
                "failure_class": "probe_infra",
                "issues": ["runtime_probe_run_not_object"],
            }
        _is_timeout_run = any(
            str(issue).startswith("runtime_probe_candidate_timeout:")
            for issue in observed.get("issues") or []
        )
        if _is_timeout_run and timeout_run_retries_left > 0:
            timeout_run_retries_left -= 1
            continue
        runs.append(observed)
        if observed.get("failure_class") == "probe_infra" or _is_timeout_run:
            break
    after = _bot_code_fingerprint(root)

    first = copy.deepcopy(runs[0])
    issues: list[str] = []
    repeat_validation_ok = True
    for run_index, observed in enumerate(runs, start=1):
        repeat_issues = _repeat_validation_issues(observed, spec)
        repeat_validation_ok = repeat_validation_ok and not repeat_issues
        issues.extend(f"{issue}:repeat={run_index}" for issue in repeat_issues)
    if before != after:
        issues.append("runtime_probe_mutated_candidate")
    views = [_repeatability_view(run) for run in runs]
    repeatability = _repeatability_evidence(views)
    repeatability_ok = bool(
        len(runs) >= 2
        and repeat_validation_ok
        and repeatability["differing_path_count"] == 0
    )
    if not repeatability_ok:
        issues.append("runtime_probe_non_repeatable")

    first.update({
        "schema_version": RUNTIME_PROBE_SCHEMA_VERSION,
        "orchestrator_version": RUNTIME_PROBE_ORCHESTRATOR_VERSION,
        "scenario_version": RUNTIME_PROBE_SCENARIO_VERSION,
        "scenario_digest": RUNTIME_PROBE_SCENARIO_DIGEST,
        "limits_digest": RUNTIME_PROBE_LIMITS_DIGEST,
        "worker_digest": RUNTIME_PROBE_WORKER_DIGEST,
        "probe_identity_digest": RUNTIME_PROBE_IDENTITY_DIGEST,
        **runtime_probe_native_template_evidence(),
        "policy_abi": "decision_context_v1_typed_intent_v1",
        "spec_digest": spec["spec_digest"],
        "code_fingerprint": before,
        "repeat_count": len(runs),
        "repeatability_ok": repeatability_ok,
        "repeatability": repeatability,
        "evidence_integrity_ok": bool(repeatability_ok and before == after),
        "managed_isolation_digest": _canonical_digest(
            first.get("managed_isolation") or {}
        ),
        "cache_key": key,
        "cache_hit": False,
        "issues": list(dict.fromkeys(map(str, issues))),
    })
    first["ok"] = not first["issues"]
    if any(run.get("failure_class") == "probe_infra" for run in runs):
        first["failure_class"] = "probe_infra"
    else:
        first["failure_class"] = "none" if first["ok"] else "candidate_contract"
    if before == after:
        _cache_put(key, first)
    return first


__all__ = [
    "RUNTIME_PROBE_IDENTITY_DIGEST",
    "RUNTIME_PROBE_LIMITS_DIGEST",
    "RUNTIME_PROBE_NATIVE_TEMPLATE_DIGEST",
    "RUNTIME_PROBE_NATIVE_TEMPLATE_IDENTITY",
    "RUNTIME_PROBE_ORCHESTRATOR_VERSION",
    "RUNTIME_PROBE_REPEATABILITY_SCHEMA_VERSION",
    "RUNTIME_PROBE_SCHEMA_VERSION",
    "RUNTIME_PROBE_SCENARIO_DIGEST",
    "RUNTIME_PROBE_WORKER_DIGEST",
    "RUNTIME_PROBE_WORKER_VERSION",
    "_bot_code_fingerprint",
    "build_runtime_probe_spec",
    "clear_runtime_probe_cache",
    "run_national_runtime_probe",
    "runtime_probe_native_template_evidence",
    "runtime_probe_native_template_evidence_matches",
    "runtime_probe_limits",
    "validate_runtime_probe_repeatability_evidence",
]
