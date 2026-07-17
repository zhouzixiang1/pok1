"""Sandboxed dynamic evidence for the current typed national policy ABI."""

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
    snapshot_managed_bot_sources,
)
from bot_namespace import STRICT_ARTIFACT_FILES
from national_native import NATIONAL_DECISION_RUNTIME_VERSION
from national_runtime_authority import (
    current_system_native_runtime_identity,
    system_native_runtime_identity_structure_issues,
)
from national_runtime_probe_scenarios import (
    RUNTIME_PROBE_SCENARIO_DIGEST,
    RUNTIME_PROBE_SCENARIO_VERSION,
)


RUNTIME_PROBE_SCHEMA_VERSION = 16
# The probe cache is also a quality-evidence cache.  Its identity must change
# when the system-owned wire client changes, even when the candidate's five
# artifact files have not.  Otherwise a changed name/stream/decision path
# could inherit a result that was exercised against an older native template.
RUNTIME_PROBE_ORCHESTRATOR_VERSION = 17
RUNTIME_PROBE_WORKER_VERSION = 18
RUNTIME_PROBE_TIMEOUT_SEC = 45.0
RUNTIME_PROBE_REPEATS = 2
RUNTIME_PROBE_MAX_IMPORT_MS = 2_500.0
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
    return {
        "runtime_version": value.get("runtime_version"),
        "socket_fallback_decision": value.get("socket_fallback_decision"),
        "baseline_published": bool(value.get("baseline_published")),
        "policy_baseline_decision": value.get("policy_baseline_decision"),
        "timed_out": bool(value.get("timed_out")),
        "worker_terminated": bool(value.get("worker_terminated")),
    }


def _repeatability_view(result: dict[str, Any]) -> dict[str, Any]:
    decisions = []
    for row in result.get("official_transcript_decisions") or []:
        if not isinstance(row, dict):
            continue
        decisions.append({
            "id": row.get("id"),
            "ok": row.get("ok"),
            "issues": row.get("issues") or [],
            "context_digest": row.get("context_digest"),
            "decision": row.get("decision"),
            "wire": row.get("wire"),
            "setup_wire": row.get("setup_wire") or [],
            "runtime": _stable_runtime(row.get("runtime") or {}),
        })
    scaling = result.get("budget_scaled_refinement") or {}
    scaling_view = {
        "probe_kind": scaling.get("probe_kind"),
        "scenario": scaling.get("scenario"),
        "ok": scaling.get("ok"),
        "active": scaling.get("active"),
        "system_issues": scaling.get("system_issues") or [],
        "capability_issues": scaling.get("capability_issues") or [],
        "worker_seed_equal": scaling.get("worker_seed_equal"),
        "bounded_work": scaling.get("bounded_work"),
        "scaled_or_exhausted": scaling.get("scaled_or_exhausted"),
        "changes_sanitized_decision": scaling.get(
            "changes_sanitized_decision"
        ),
        "short": {
            key: (scaling.get("short") or {}).get(key)
            for key in (
                "iterator_exhausted",
                "action_changes",
                "refinement_messages",
                "baseline_published",
                "baseline_target_met",
                "worker_seed",
                "decision",
                "wire",
            )
        },
        "long": {
            key: (scaling.get("long") or {}).get(key)
            for key in (
                "iterator_exhausted",
                "action_changes",
                "refinement_messages",
                "baseline_published",
                "baseline_target_met",
                "worker_seed",
                "decision",
                "wire",
            )
        },
    }
    return {
        "schema_version": result.get("schema_version"),
        "worker_version": result.get("worker_version"),
        "scenario_version": result.get("scenario_version"),
        "scenario_digest": result.get("scenario_digest"),
        "spec_digest": result.get("spec_digest"),
        "code_fingerprint": result.get("code_fingerprint"),
        "native_runtime_template_identity": result.get(
            "native_runtime_template_identity"
        ),
        "native_runtime_template_digest": result.get(
            "native_runtime_template_digest"
        ),
        "issues": result.get("issues") or [],
        "official_transcript_decisions": decisions,
        "line_reachability": result.get("line_reachability") or {},
        "persistent_memory": result.get("persistent_memory") or {},
        "policy_entrypoints": result.get("policy_entrypoints") or {},
        "policy_counterfactuals": result.get("policy_counterfactuals") or {},
        "match_control_consumer": result.get("match_control_consumer") or {},
        "budget_scaled_refinement": scaling_view,
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
    for _ in range(max(2, int(repeats))):
        observed = _run_once(root, spec, timeout_sec)
        runs.append(observed)
        if observed.get("failure_class") == "probe_infra" or any(
            str(issue).startswith("runtime_probe_candidate_timeout:")
            for issue in observed.get("issues") or []
        ):
            break
    after = _bot_code_fingerprint(root)

    first = copy.deepcopy(runs[0])
    issues = list(first.get("issues") or [])
    for run_index, observed in enumerate(runs, start=1):
        issues.extend(
            issue if run_index == 1 else f"{issue}:repeat={run_index}"
            for issue in _identity_issues(observed, spec)
        )
    if before != after:
        issues.append("runtime_probe_mutated_candidate")
    reference = _repeatability_view(runs[0])
    repeatability_ok = bool(
        len(runs) >= 2
        and all(_repeatability_view(run) == reference for run in runs[1:])
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
        "evidence_integrity_ok": bool(repeatability_ok and before == after),
        "managed_isolation_digest": _canonical_digest(
            first.get("managed_isolation") or {}
        ),
        "cache_key": key,
        "cache_hit": False,
        "issues": list(dict.fromkeys(map(str, issues))),
    })
    first["ok"] = not first["issues"]
    if first.get("failure_class") != "probe_infra":
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
]
