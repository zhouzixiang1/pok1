"""Sandboxed dynamic evidence for national-native runtime contracts."""

from __future__ import annotations

import copy
from collections import OrderedDict
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from typing import Any

from national_runtime_probe_scenarios import (
    RUNTIME_PROBE_SCENARIO_DIGEST,
    RUNTIME_PROBE_SCENARIO_VERSION,
)
from national_native import NATIONAL_DECISION_RUNTIME_VERSION


RUNTIME_PROBE_SCHEMA_VERSION = 7
RUNTIME_PROBE_ORCHESTRATOR_VERSION = 7
RUNTIME_PROBE_TIMEOUT_SEC = 45.0
RUNTIME_PROBE_REPEATS = 2
RUNTIME_PROBE_MAX_IMPORT_MS = 2_500.0
RUNTIME_PROBE_MAX_ENTRIES = 65_536
RUNTIME_PROBE_MAX_BYTES = 8 * 1024 * 1024
RUNTIME_PROBE_MAX_OUTPUT_BYTES = 1024 * 1024
RUNTIME_PROBE_CACHE_MAX_ENTRIES = 128
_RUNTIME_PROBE_CACHE: OrderedDict[str, dict[str, Any]] = OrderedDict()
_RUNTIME_PROBE_CACHE_LOCK = threading.Lock()


def runtime_probe_limits() -> dict[str, Any]:
    return {
        "timeout_sec": RUNTIME_PROBE_TIMEOUT_SEC,
        "repeats": RUNTIME_PROBE_REPEATS,
        "max_import_ms": RUNTIME_PROBE_MAX_IMPORT_MS,
        "max_entries": RUNTIME_PROBE_MAX_ENTRIES,
        "max_bytes": RUNTIME_PROBE_MAX_BYTES,
        "max_output_bytes": RUNTIME_PROBE_MAX_OUTPUT_BYTES,
        "sandbox": "bubblewrap-ro-root-unshare-net-pid",
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
    bwrap = shutil.which("bwrap")
    if not bwrap:
        return {
            "ok": False,
            "failure_class": "probe_infra",
            "issues": ["runtime_probe_bwrap_unavailable"],
        }
    worker = _probe_worker_path()
    with tempfile.TemporaryDirectory(prefix="pok_runtime_probe_") as output_dir:
        output_root = Path(output_dir)
        report_host = output_root / "report.json"
        phase_host = output_root / "phase.txt"
        stdout_host = output_root / "stdout.log"
        stderr_host = output_root / "stderr.log"
        command = [
            bwrap,
            "--die-with-parent",
            "--new-session",
            "--unshare-net",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--ro-bind", "/", "/",
            "--dev", "/dev",
            "--proc", "/proc",
            "--tmpfs", "/tmp",
            "--dir", "/tmp/probe_out",
            "--bind", str(output_root), "/tmp/probe_out",
            "--ro-bind", str(root), str(root),
            "--chdir", str(root),
            "--clearenv",
            "--setenv", "PATH", str(Path(sys.executable).parent),
            "--setenv", "LANG", "C.UTF-8",
            "--setenv", "HOME", "/tmp",
            "--setenv", "PYTHONHASHSEED", "0",
            "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
            sys.executable,
            "-I",
            "-B",
            str(worker),
            str(root),
            "/tmp/probe_out/report.json",
            json.dumps(spec, sort_keys=True, separators=(",", ":")),
        ]
        with stdout_host.open("wb") as stdout_fp, stderr_host.open("wb") as stderr_fp:
            try:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_fp,
                    stderr=stderr_fp,
                    start_new_session=True,
                )
            except OSError as exc:
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
        if stdout_bytes > RUNTIME_PROBE_MAX_OUTPUT_BYTES or stderr_bytes > RUNTIME_PROBE_MAX_OUTPUT_BYTES:
            return {
                "ok": False,
                "failure_class": "candidate_contract",
                "issues": ["runtime_probe_output_limit_exceeded"],
                "process_returncode": returncode,
            }
        if returncode != 0 or not report_host.is_file():
            stderr_text = stderr_host.read_text(encoding="utf-8", errors="replace")[:2000]
            return {
                "ok": False,
                "failure_class": (
                    "probe_infra"
                    if returncode in {125, 126, 127} or stderr_text.startswith("bwrap:")
                    else "candidate_contract"
                ),
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
            }
    first = dict(runs[0])
    issues = list(first.get("issues") or [])
    if before != after:
        issues.append("runtime_probe_mutated_candidate")
    reference = _repeatability_view(runs[0])
    if any(_repeatability_view(run) != reference for run in runs[1:]):
        issues.append("runtime_probe_non_repeatable")
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
