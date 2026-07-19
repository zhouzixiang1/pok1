"""Fail-closed operator observation for uninterrupted evolution delivery.

This module records whether one long-running evolution process has published
ten consecutive strict-policy generations without a repair, abandonment,
process restart, or evaluation-contract change.  The record is operational
acceptance evidence only.  It is deliberately stored below ``results/`` and
must never be injected into planning prompts, ratings, selection, or candidate
policy context.
"""

from __future__ import annotations

import fcntl
import copy
import hashlib
import json
import math
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from bot_namespace import EVALUATION_EPOCH, FIRST_STRICT_POLICY_VERSION, bot_tag
from evaluation_contract import build_evaluation_contract
from evolution_infra import (
    PROJECT_ROOT,
    RESULTS_DIR,
    _git,
    _git_command_succeeds,
    _fsync_directory,
    locked_file,
)


SCHEMA_VERSION = 1
KIND = "national-tcp-uninterrupted-evolution-observation"
TARGET_GENERATIONS = 10
STATE_FILE = RESULTS_DIR / "stability_observation.json"
LOCK_FILE = RESULTS_DIR / ".stability_observation.lock"
_PROCESS_BOOT_ID = uuid.uuid4().hex
_PROCESS_PID = os.getpid()
try:
    _PROCESS_START_TICKS = int(
        (Path("/proc") / str(_PROCESS_PID) / "stat").read_text(
            encoding="utf-8"
        ).split()[21]
    )
except Exception:
    _PROCESS_START_TICKS = 0
_MAX_RESET_HISTORY = 20
STABILITY_VERIFICATION_TTL_SEC = 30.0
STABILITY_VERIFICATION_RETRY_SEC = 5.0
# A running orchestrator refreshes before the last verified result expires.
# The public health projection nevertheless becomes stale at the exact TTL if
# a verifier cannot finish, so this is availability maintenance, never a
# relaxation of the delivery observation contract.
STABILITY_VERIFICATION_PREFETCH_LEAD_SEC = 10.0
MAX_DAEMON_PAIRS = 8

_RUNTIME_CONFIG_LOCK = threading.RLock()
_BOUND_RUNTIME_CONFIG: dict[str, Any] | None = None

_PROJECTION_CACHE_LOCK = threading.Lock()
_PROJECTION_CACHE_GENERATION = 0
_PROJECTION_CACHE_VALUE: dict[str, Any] | None = None
_PROJECTION_CACHE_CHECKED_AT: float | None = None
_PROJECTION_CACHE_FRESH_UNTIL: float | None = None
_PROJECTION_CACHE_ERROR: str | None = None
_PROJECTION_CACHE_RETRY_AFTER = 0.0
_PROJECTION_CACHE_INFLIGHT = False
_PROJECTION_CACHE_AUTHORITY: dict[str, Any] | None = None


class StabilityObservationError(RuntimeError):
    """Raised when an observation cannot be recorded without overstating it."""


def _now() -> float:
    return time.time()


def _canonical_digest(payload: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in payload.items() if key != "state_digest"}
    encoded = json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_hex(value: Any, length: int) -> bool:
    text = str(value or "")
    return len(text) == length and all(char in "0123456789abcdef" for char in text)


def _validate_runtime_configuration(config: dict[str, Any]) -> dict[str, Any]:
    enabled = config.get("daemon_enabled")
    workers = config.get("daemon_workers")
    pairs = config.get("daemon_pairs")
    if not isinstance(enabled, bool):
        raise StabilityObservationError("runtime_config_daemon_enabled_invalid")
    if (
        not isinstance(workers, int)
        or isinstance(workers, bool)
        or not 1 <= workers <= 12
    ):
        raise StabilityObservationError("runtime_config_daemon_workers_invalid")
    if (
        not isinstance(pairs, int)
        or isinstance(pairs, bool)
        or not 1 <= pairs <= MAX_DAEMON_PAIRS
    ):
        raise StabilityObservationError("runtime_config_daemon_pairs_invalid")
    return {
        "daemon_enabled": enabled,
        "daemon_workers": workers,
        "daemon_pairs": pairs,
    }


def _runtime_configuration_from_disk() -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "daemon_enabled": os.environ.get("DAEMON_DISABLED") != "1",
        "daemon_workers": max(1, min(12, int((os.cpu_count() or 1) * 28 / 32))),
        "daemon_pairs": 5,
    }
    path = STATE_FILE.parent / "app_config.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        payload = {}
    except Exception as exc:
        raise StabilityObservationError(
            f"runtime_config_unavailable:{type(exc).__name__}"
        ) from exc
    if isinstance(payload, dict):
        for key in defaults:
            if key in payload:
                defaults[key] = payload[key]
    return _validate_runtime_configuration(defaults)


def bind_runtime_configuration(config: dict[str, Any]) -> dict[str, Any]:
    """Bind the effective daemon configuration used by this runtime process."""

    global _BOUND_RUNTIME_CONFIG
    value = _validate_runtime_configuration(dict(config))
    with _RUNTIME_CONFIG_LOCK:
        changed = _BOUND_RUNTIME_CONFIG != value
        _BOUND_RUNTIME_CONFIG = value
    if changed:
        invalidate_stability_projection_cache()
    return runtime_configuration_identity()


def runtime_configuration_identity() -> dict[str, Any]:
    with _RUNTIME_CONFIG_LOCK:
        value = (
            dict(_BOUND_RUNTIME_CONFIG)
            if _BOUND_RUNTIME_CONFIG is not None
            else _runtime_configuration_from_disk()
        )
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return {
        "config": value,
        "digest": hashlib.sha256(encoded).hexdigest(),
    }


def clear_runtime_configuration_binding() -> None:
    """Clear the process binding for isolated tests and controlled relaunch."""

    global _BOUND_RUNTIME_CONFIG
    with _RUNTIME_CONFIG_LOCK:
        _BOUND_RUNTIME_CONFIG = None
    invalidate_stability_projection_cache()


def _current_identity() -> dict[str, str]:
    """Return the process/epoch/infrastructure identity for this observation."""

    if _PROCESS_START_TICKS <= 0:
        raise StabilityObservationError("runtime_process_start_identity_unavailable")
    contract = build_evaluation_contract(
        PROJECT_ROOT,
        include_hash=True,
        national_execution_mode="native_tcp",
    )
    contract_hash = str(contract.get("hash") or "")
    if len(contract_hash) != 64:
        raise StabilityObservationError("evaluation_contract_hash_unavailable")
    repository_head = str(_git("rev-parse", "--verify", "HEAD") or "")
    repository_branch = str(_git("rev-parse", "--abbrev-ref", "HEAD") or "")
    if not _is_hex(repository_head, 40):
        raise StabilityObservationError("repository_head_unavailable")
    if (
        not repository_branch
        or repository_branch == "HEAD"
        or any(char.isspace() or ord(char) < 32 for char in repository_branch)
    ):
        raise StabilityObservationError("repository_branch_unavailable")
    runtime_config = runtime_configuration_identity()
    return {
        "evaluation_epoch": EVALUATION_EPOCH,
        "process_boot_id": _PROCESS_BOOT_ID,
        "process_pid": str(_PROCESS_PID),
        "process_start_ticks": str(_PROCESS_START_TICKS),
        "infrastructure_contract_hash": contract_hash,
        "runtime_config_digest": str(runtime_config["digest"]),
        "repository_head": repository_head,
        "repository_branch": repository_branch,
    }


def _daemon_process_identity() -> str:
    """Return a PID-reuse-safe identity for the live rating daemon."""

    pid_file = STATE_FILE.parent / ".daemon_pid"
    try:
        payload = json.loads(pid_file.read_text(encoding="utf-8"))
        pid = int(payload.get("pid")) if isinstance(payload, dict) else int(payload)
        ppid = int(payload.get("ppid") or 0) if isinstance(payload, dict) else 0
        expected_start_ticks = (
            int(payload.get("start_ticks") or 0)
            if isinstance(payload, dict)
            else 0
        )
        os.kill(pid, 0)
        # Linux proc start ticks disambiguate PID reuse.  The production runtime
        # is Linux-only because its mandatory isolation uses namespaces/seccomp.
        stat_fields = (Path("/proc") / str(pid) / "stat").read_text(
            encoding="utf-8"
        ).split()
        start_ticks = int(stat_fields[21])
        if expected_start_ticks <= 0 or start_ticks != expected_start_ticks:
            raise StabilityObservationError(
                "rating_daemon_start_identity_mismatch"
            )
    except Exception as exc:
        raise StabilityObservationError(
            f"rating_daemon_identity_unavailable:{type(exc).__name__}"
        ) from exc
    return hashlib.sha256(
        f"pid={pid};ppid={ppid};start_ticks={start_ticks}".encode("ascii")
    ).hexdigest()


def _generation_evidence_binding(
    version: int,
    source_v: int,
    publishing_checkpoint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Use the shared checkpoint-bound admitted/zero-weight producer."""

    if publishing_checkpoint is None:
        try:
            from post_publication_handoff import load_archive_snapshot

            snapshot = load_archive_snapshot(int(version))
            publishing_checkpoint = snapshot.get(
                "publishing_checkpoint_projection"
            )
        except Exception as exc:
            raise StabilityObservationError(
                f"generation_evidence_checkpoint_unavailable:{type(exc).__name__}"
            ) from exc
    try:
        from generation_evidence import build_generation_evidence_identity

        return build_generation_evidence_identity(
            publishing_checkpoint,
            version=int(version),
            source_v=int(source_v),
        )
    except Exception as exc:
        raise StabilityObservationError(
            f"generation_evidence_invalid:{type(exc).__name__}:{str(exc)[:240]}"
        ) from exc


def _strength_cycle_readiness(state: dict[str, Any]) -> dict[str, Any]:
    """Require the latest published bot to enter a valid native strength cycle."""

    rows = state.get("observations") or []
    if len(rows) < TARGET_GENERATIONS:
        return {"ready": False, "reason": "target_not_reached"}
    latest = rows[-1]
    from bot_namespace import bot_name
    from evaluation_bundle import load_current_strict_evaluation_bundle
    from rating_snapshot import _admitted_70_hand_history_sample

    bundle = load_current_strict_evaluation_bundle()
    if bundle.get("available") is not True:
        return {
            "ready": False,
            "reason": str(bundle.get("reason") or "current_cycle_unavailable"),
        }
    latest_name = bot_name(int(latest["version"]))
    if latest_name not in set(bundle.get("active_bots") or []):
        return {"ready": False, "reason": "latest_bot_missing_from_active_cycle"}
    manifest = bundle.get("manifest") or {}
    evaluation_identity = str(manifest.get("evaluation_identity_digest") or "")
    cycle_manifest_digest = str(bundle.get("manifest_digest") or "")
    if not _is_hex(evaluation_identity, 64) or not _is_hex(
        cycle_manifest_digest,
        64,
    ):
        return {"ready": False, "reason": "current_cycle_identity_invalid"}
    admitted_sample_id = ""
    for line in (bundle.get("raw_append_logs") or {}).get(
        "match_history", b""
    ).splitlines():
        try:
            sample = json.loads(line.decode("utf-8"))
        except Exception:
            continue
        if sample.get("evaluation_identity_digest") != evaluation_identity:
            continue
        if latest_name not in {sample.get("bot0"), sample.get("bot1")}:
            continue
        if _admitted_70_hand_history_sample(
            sample,
            expected_evaluation_identity_digest=evaluation_identity,
        ) is None:
            continue
        admitted_sample_id = str(sample.get("id") or _canonical_digest(sample))
        break
    if not admitted_sample_id:
        return {"ready": False, "reason": "latest_bot_has_no_admitted_70_hand_sample"}
    return {
        "ready": True,
        "reason": "latest_bot_admitted_to_current_native_cycle",
        "latest_bot": latest_name,
        "evaluation_identity_digest": evaluation_identity,
        "cycle_manifest_digest": cycle_manifest_digest,
        "cycle_save_num": manifest.get("save_num"),
        "admitted_sample_id": admitted_sample_id,
    }


def _state_errors(state: Any) -> list[str]:
    if not isinstance(state, dict):
        return ["state_not_object"]
    errors: list[str] = []
    if state.get("schema_version") != SCHEMA_VERSION:
        errors.append("schema_version_mismatch")
    if state.get("kind") != KIND:
        errors.append("kind_mismatch")
    if state.get("evaluation_epoch") != EVALUATION_EPOCH:
        errors.append("evaluation_epoch_mismatch")
    if state.get("target_generations") != TARGET_GENERATIONS:
        errors.append("target_generations_mismatch")
    if not str(state.get("continuity_id") or ""):
        errors.append("continuity_id_missing")
    if not str(state.get("process_boot_id") or ""):
        errors.append("process_boot_id_missing")
    try:
        process_pid = int(state.get("process_pid") or 0)
        process_start_ticks = int(state.get("process_start_ticks") or 0)
    except (TypeError, ValueError):
        process_pid = 0
        process_start_ticks = 0
    if process_pid <= 1:
        errors.append("process_pid_invalid")
    if process_start_ticks <= 0:
        errors.append("process_start_ticks_invalid")
    contract_hash = str(state.get("infrastructure_contract_hash") or "")
    if len(contract_hash) != 64:
        errors.append("infrastructure_contract_hash_invalid")
    if not _is_hex(state.get("runtime_config_digest"), 64):
        errors.append("runtime_config_digest_invalid")
    if not _is_hex(state.get("repository_head"), 40):
        errors.append("repository_head_invalid")
    repository_branch = str(state.get("repository_branch") or "")
    if (
        not repository_branch
        or repository_branch == "HEAD"
        or any(char.isspace() or ord(char) < 32 for char in repository_branch)
    ):
        errors.append("repository_branch_invalid")

    observations = state.get("observations")
    if not isinstance(observations, list):
        errors.append("observations_not_list")
        observations = []
    if len(observations) > TARGET_GENERATIONS:
        errors.append("observations_exceed_target")
    previous_version: int | None = None
    publication_ids: set[str] = set()
    for index, row in enumerate(observations):
        prefix = f"observation_{index}"
        if not isinstance(row, dict):
            errors.append(f"{prefix}_not_object")
            continue
        try:
            version = int(row.get("version"))
        except (TypeError, ValueError):
            errors.append(f"{prefix}_version_invalid")
            continue
        if version < FIRST_STRICT_POLICY_VERSION:
            errors.append(f"{prefix}_pre_epoch_version")
        if previous_version is not None and version != previous_version + 1:
            errors.append(f"{prefix}_version_not_consecutive")
        previous_version = version
        publication_id = str(row.get("publication_id") or "")
        if not _is_hex(publication_id, 64):
            errors.append(f"{prefix}_publication_id_invalid")
        elif publication_id in publication_ids:
            errors.append(f"{prefix}_publication_id_duplicate")
        publication_ids.add(publication_id)
        if row.get("completion_tag") != bot_tag(version):
            errors.append(f"{prefix}_completion_tag_mismatch")
        for key in (
            "commit_oid",
            "certificate_digest",
            "candidate_artifact_hash",
            "official_policy_id",
            "official_status_digest",
            "certificate_file_sha256",
            "certificate_attestation_digest",
            "workflow_run_id",
            "daemon_process_identity",
            "final_gate_ledger_digest",
            "workflow_profile_id",
        ):
            if not str(row.get(key) or ""):
                errors.append(f"{prefix}_{key}_missing")
        if not _is_hex(row.get("commit_oid"), 40):
            errors.append(f"{prefix}_commit_oid_invalid")
        for key in (
            "certificate_digest",
            "candidate_artifact_hash",
            "official_status_digest",
            "certificate_file_sha256",
            "certificate_attestation_digest",
            "daemon_process_identity",
            "final_gate_ledger_digest",
        ):
            if not _is_hex(row.get(key), 64):
                errors.append(f"{prefix}_{key}_invalid")
        if row.get("checkpoint_cleared") is not True:
            errors.append(f"{prefix}_checkpoint_not_cleared")
        if row.get("push_ok") is not True:
            errors.append(f"{prefix}_push_not_proven")
        if row.get("remote_publication_required") is not True:
            errors.append(f"{prefix}_remote_publication_not_required")
        if row.get("national_execution_mode") != "native_tcp":
            errors.append(f"{prefix}_execution_mode_invalid")
        remote = row.get("remote_publication")
        if not isinstance(remote, dict):
            errors.append(f"{prefix}_remote_publication_missing")
        else:
            if not _is_hex(remote.get("remote_main_oid"), 40):
                errors.append(f"{prefix}_remote_main_oid_invalid")
            elif str(remote.get("remote_main_oid")) != str(
                row.get("commit_oid") or ""
            ):
                errors.append(f"{prefix}_remote_main_not_publication_commit")
            for name in (bot_tag(version), f"national-high-water-v{version}"):
                ref = (remote.get("refs") or {}).get(name)
                if not isinstance(ref, dict):
                    errors.append(f"{prefix}_remote_ref_missing:{name}")
                    continue
                if not _is_hex(ref.get("object_oid"), 40):
                    errors.append(f"{prefix}_remote_ref_object_invalid:{name}")
                if str(ref.get("peeled_commit_oid") or "") != str(
                    row.get("commit_oid") or ""
                ):
                    errors.append(f"{prefix}_remote_ref_commit_mismatch:{name}")
        evidence = row.get("generation_evidence")
        if not isinstance(evidence, dict):
            errors.append(f"{prefix}_generation_evidence_missing")
        elif evidence.get("mode") in {
            "fresh_strict_v143_bootstrap",
            "singleton_strict_successor_bootstrap",
        }:
            expected_mode = (
                "fresh_strict_v143_bootstrap"
                if version == FIRST_STRICT_POLICY_VERSION
                else "singleton_strict_successor_bootstrap"
                if version > FIRST_STRICT_POLICY_VERSION
                else ""
            )
            if (
                evidence.get("mode") != expected_mode
                or evidence.get("strength_evidence_admitted") is not False
                or evidence.get("strength_evidence_weight") != 0
            ):
                errors.append(f"{prefix}_bootstrap_evidence_invalid")
        else:
            if (
                evidence.get("mode") != "frozen_native_evaluation"
                or evidence.get("strength_evidence_admitted") is not True
                or evidence.get("strength_evidence_weight") != 1
            ):
                errors.append(f"{prefix}_generation_evidence_kind_invalid")
            for key in (
                "generation_snapshot_manifest_digest",
                "evaluation_identity_digest",
                "cycle_manifest_digest",
                "selection_sha256",
                "match_history_index_sha256",
                "replay_spotlight_sha256",
            ):
                if len(str(evidence.get(key) or "")) != 64:
                    errors.append(f"{prefix}_generation_evidence_{key}_invalid")

    if observations and isinstance(observations[-1], dict) and str(
        state.get("repository_head") or ""
    ) != str(observations[-1].get("commit_oid") or ""):
        errors.append("repository_head_not_latest_publication_commit")

    reset_history = state.get("reset_history")
    if not isinstance(reset_history, list):
        errors.append("reset_history_not_list")
    elif len(reset_history) > _MAX_RESET_HISTORY:
        errors.append("reset_history_too_large")

    expected_digest = _canonical_digest(state)
    if state.get("state_digest") != expected_digest:
        errors.append("state_digest_mismatch")
    return errors


def _read_state_unlocked() -> tuple[dict[str, Any] | None, list[str]]:
    try:
        raw = STATE_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, ["state_missing"]
    except OSError as exc:
        return None, [f"state_read_failed:{type(exc).__name__}"]
    try:
        state = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, ["state_json_invalid"]
    errors = _state_errors(state)
    return (state if isinstance(state, dict) else None), errors


def _write_state_unlocked(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state["state_digest"] = _canonical_digest(state)
    tmp = STATE_FILE.with_name(f".{STATE_FILE.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "x", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, STATE_FILE)
        _fsync_directory(STATE_FILE.parent)
    finally:
        tmp.unlink(missing_ok=True)


def _new_state(
    identity: dict[str, str],
    *,
    reason: str,
    details: dict[str, Any] | None = None,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = _now()
    history: list[dict[str, Any]] = []
    if isinstance(previous, dict) and isinstance(previous.get("reset_history"), list):
        history = [
            dict(item)
            for item in previous["reset_history"][-(_MAX_RESET_HISTORY - 1):]
            if isinstance(item, dict)
        ]
    previous_rows = previous.get("observations") if isinstance(previous, dict) else []
    if not isinstance(previous_rows, list):
        previous_rows = []
    history.append({
        "at": now,
        "reason": str(reason)[:160],
        "details": dict(details or {}),
        "previous_count": len(previous_rows),
        "previous_last_version": (
            previous_rows[-1].get("version")
            if previous_rows and isinstance(previous_rows[-1], dict)
            else None
        ),
    })
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "evaluation_epoch": identity["evaluation_epoch"],
        "target_generations": TARGET_GENERATIONS,
        "continuity_id": uuid.uuid4().hex,
        "process_boot_id": identity["process_boot_id"],
        "process_pid": identity["process_pid"],
        "process_start_ticks": identity["process_start_ticks"],
        "infrastructure_contract_hash": identity[
            "infrastructure_contract_hash"
        ],
        "runtime_config_digest": identity["runtime_config_digest"],
        "repository_head": identity["repository_head"],
        "repository_branch": identity["repository_branch"],
        "started_at": now,
        "updated_at": now,
        "last_reset_reason": str(reason)[:160],
        "last_reset_details": dict(details or {}),
        "observations": [],
        "reset_history": history[-_MAX_RESET_HISTORY:],
    }


def _identity_mismatches(
    state: dict[str, Any],
    identity: dict[str, str],
    *,
    compare_process: bool = True,
) -> list[str]:
    keys = [
        "evaluation_epoch",
        "infrastructure_contract_hash",
        "runtime_config_digest",
        "repository_head",
        "repository_branch",
    ]
    if compare_process:
        keys.extend(("process_boot_id", "process_pid", "process_start_ticks"))
    return [
        key
        for key in keys
        if state.get(key) != identity.get(key)
    ]


def _owner_process_errors(state: dict[str, Any]) -> list[str]:
    """Prove that the process owning the streak is still the same process."""

    try:
        pid = int(state.get("process_pid") or 0)
        expected_start = int(state.get("process_start_ticks") or 0)
        os.kill(pid, 0)
        actual_start = int(
            (Path("/proc") / str(pid) / "stat").read_text(
                encoding="utf-8"
            ).split()[21]
        )
    except Exception as exc:
        return [f"owner_process_unavailable:{type(exc).__name__}"]
    if actual_start != expected_start:
        return ["owner_process_start_identity_mismatch"]
    return []


def _live_publication_errors(state: dict[str, Any]) -> list[str]:
    """Re-open current tag/tree/certificate proof for the bounded 10-row window."""

    from bot_artifact import validate_completion_tag
    from bot_namespace import bot_name

    errors: list[str] = []
    for row in state.get("observations") or []:
        version = int(row["version"])
        certificate_path = f"official_certificates/{bot_name(version)}.json"
        validation = validate_completion_tag(
            PROJECT_ROOT / "bots" / bot_name(version),
            expected_metadata={
                "official-certificate": str(row["certificate_digest"]),
                "official-candidate-hash": str(row["candidate_artifact_hash"]),
                "official-policy": str(row["official_policy_id"]),
            },
            certificate_path=certificate_path,
        )
        if not validation.get("valid"):
            errors.extend(
                f"v{version}:publication_proof:{issue}"
                for issue in (validation.get("issues") or ["invalid"])
            )
        identity = validation.get("identity") or {}
        if str(identity.get("commit_oid") or "") != str(row["commit_oid"]):
            errors.append(f"v{version}:publication_commit_oid_mismatch")
        certificate = PROJECT_ROOT / certificate_path
        try:
            payload = json.loads(certificate.read_text(encoding="utf-8"))
        except Exception as exc:
            errors.append(
                f"v{version}:certificate_unavailable:{type(exc).__name__}"
            )
        else:
            if payload.get("certificate_digest") != row["certificate_digest"]:
                errors.append(f"v{version}:certificate_digest_mismatch")
        try:
            current_evidence = _generation_evidence_binding(
                version,
                int(row["source_v"]),
            )
        except Exception as exc:
            errors.append(
                f"v{version}:generation_evidence_unavailable:{type(exc).__name__}"
            )
        else:
            if current_evidence != row.get("generation_evidence"):
                errors.append(f"v{version}:generation_evidence_identity_mismatch")
    return list(dict.fromkeys(errors))


def _remote_publication_errors(state: dict[str, Any]) -> list[str]:
    """Re-open origin refs without fetching or mutating the operator checkout."""

    rows = list(state.get("observations") or [])
    if not rows:
        return []
    wanted = ["refs/heads/main"]
    for row in rows:
        version = int(row["version"])
        for name in (bot_tag(version), f"national-high-water-v{version}"):
            wanted.extend((f"refs/tags/{name}", f"refs/tags/{name}^{{}}"))
    try:
        raw = _git("ls-remote", "origin", *wanted)
    except Exception as exc:
        return [f"remote_refs_unavailable:{type(exc).__name__}"]
    refs: dict[str, str] = {}
    for line in raw.splitlines():
        oid, separator, ref = line.partition("\t")
        if separator and _is_hex(oid, 40) and ref:
            refs[ref] = oid
    remote_main = refs.get("refs/heads/main", "")
    errors: list[str] = []
    if not _is_hex(remote_main, 40):
        errors.append("remote_main_missing")
    elif remote_main != str(state.get("repository_head") or ""):
        errors.append("remote_main_head_mismatch")
    for row in rows:
        version = int(row["version"])
        stored = (row.get("remote_publication") or {}).get("refs") or {}
        commit_oid = str(row.get("commit_oid") or "")
        for name in (bot_tag(version), f"national-high-water-v{version}"):
            expected = stored.get(name) or {}
            if refs.get(f"refs/tags/{name}") != expected.get("object_oid"):
                errors.append(f"v{version}:remote_tag_object_mismatch:{name}")
            if refs.get(f"refs/tags/{name}^{{}}") != commit_oid:
                errors.append(f"v{version}:remote_tag_peeled_mismatch:{name}")
        if remote_main and not _git_command_succeeds(
            "merge-base",
            "--is-ancestor",
            commit_oid,
            remote_main,
        ):
            errors.append(f"v{version}:publication_commit_not_on_remote_main")
    return list(dict.fromkeys(errors))


def _live_daemon_errors(state: dict[str, Any]) -> list[str]:
    rows = list(state.get("observations") or [])
    if not rows:
        return []
    try:
        current = _daemon_process_identity()
    except Exception as exc:
        return [f"rating_daemon_unavailable:{type(exc).__name__}"]
    if current != rows[-1].get("daemon_process_identity"):
        return ["rating_daemon_process_identity_mismatch"]
    return _daemon_heartbeat_errors()


def _daemon_heartbeat_errors() -> list[str]:
    try:
        payload = json.loads(
            (STATE_FILE.parent / ".daemon_pid").read_text(encoding="utf-8")
        )
        if not isinstance(payload, dict):
            raise ValueError("daemon heartbeat record is not an object")
        raw_heartbeat = payload.get("last_heartbeat")
        if isinstance(raw_heartbeat, bool):
            raise ValueError("daemon heartbeat is boolean")
        heartbeat = float(raw_heartbeat)
        if not math.isfinite(heartbeat):
            raise ValueError("daemon heartbeat is non-finite")
        from elo_daemon import HEARTBEAT_STALE_SEC

        age = _now() - heartbeat
    except Exception as exc:
        return [f"rating_daemon_heartbeat_unavailable:{type(exc).__name__}"]
    if age < -5.0:
        return ["rating_daemon_heartbeat_from_future"]
    if age > float(HEARTBEAT_STALE_SEC):
        return [f"rating_daemon_heartbeat_stale:{age:.1f}s"]
    return []


def _projection(
    state: dict[str, Any] | None,
    *,
    errors: list[str] | None = None,
    identity: dict[str, str] | None = None,
    compare_process: bool = True,
) -> dict[str, Any]:
    errors = list(errors or [])
    rows = state.get("observations") if isinstance(state, dict) else []
    if not isinstance(rows, list):
        rows = []
    count = len(rows) if not errors else 0
    mismatches = (
        _identity_mismatches(
            state,
            identity,
            compare_process=compare_process,
        )
        if isinstance(state, dict) and identity is not None and not errors
        else []
    )
    continuity_valid = not errors and not mismatches
    if not continuity_valid:
        count = 0
    strength_cycle = (
        _strength_cycle_readiness(state)
        if continuity_valid and count >= TARGET_GENERATIONS and state is not None
        else {"ready": False, "reason": "target_not_reached"}
    )
    complete = bool(
        continuity_valid
        and count >= TARGET_GENERATIONS
        and strength_cycle.get("ready")
    )
    status = (
        "complete"
        if complete
        else "awaiting_strength_cycle"
        if continuity_valid and count >= TARGET_GENERATIONS
        else "observing"
        if continuity_valid
        else "not_started"
        if errors == ["state_missing"]
        else "reset_required"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "authority": "operator_acceptance_only",
        "strategy_evidence_weight": 0,
        "strength_evidence_weight": 0,
        "status": status,
        "continuity_valid": continuity_valid,
        "count": count,
        "target": TARGET_GENERATIONS,
        "remaining": max(0, TARGET_GENERATIONS - count),
        "complete": complete,
        "strength_cycle_ready": bool(strength_cycle.get("ready")),
        "strength_cycle": strength_cycle,
        "continuity_id": state.get("continuity_id") if isinstance(state, dict) else None,
        "last_reset_reason": (
            state.get("last_reset_reason") if isinstance(state, dict) else None
        ),
        "last_reset_details": (
            state.get("last_reset_details") if isinstance(state, dict) else {}
        ),
        "started_at": state.get("started_at") if isinstance(state, dict) else None,
        "updated_at": state.get("updated_at") if isinstance(state, dict) else None,
        "observations": list(rows) if continuity_valid else [],
        "reset_history": (
            list(state.get("reset_history") or [])
            if isinstance(state, dict) and continuity_valid
            else []
        ),
        "recorded_contract_hash": (
            state.get("infrastructure_contract_hash")
            if isinstance(state, dict)
            else None
        ),
        "current_contract_hash": (
            identity.get("infrastructure_contract_hash") if identity else None
        ),
        "recorded_runtime_config_digest": (
            state.get("runtime_config_digest")
            if isinstance(state, dict)
            else None
        ),
        "current_runtime_config_digest": (
            identity.get("runtime_config_digest") if identity else None
        ),
        "recorded_repository_head": (
            state.get("repository_head") if isinstance(state, dict) else None
        ),
        "current_repository_head": (
            identity.get("repository_head") if identity else None
        ),
        "recorded_repository_branch": (
            state.get("repository_branch") if isinstance(state, dict) else None
        ),
        "current_repository_branch": (
            identity.get("repository_branch") if identity else None
        ),
        "identity_mismatches": mismatches,
        "errors": errors,
    }


def stability_observation_projection() -> dict[str, Any]:
    """Read the current observation without mutating runtime state."""

    try:
        identity = _current_identity()
    except Exception as exc:
        return _projection(
            None,
            errors=[f"current_identity_failed:{type(exc).__name__}"],
        )
    # Writers publish with atomic ``os.replace``.  A GET must not open/create a
    # lock sidecar, so the read path consumes either the complete old inode or
    # the complete new inode and remains strictly non-mutating.
    state, errors = _read_state_unlocked()
    if (
        state is not None
        and not errors
        and not _identity_mismatches(
            state,
            identity,
            compare_process=False,
        )
    ):
        errors.extend(_owner_process_errors(state))
    if (
        state is not None
        and not errors
        and not _identity_mismatches(
            state,
            identity,
            compare_process=False,
        )
    ):
        try:
            errors.extend(_live_publication_errors(state))
            errors.extend(_remote_publication_errors(state))
            errors.extend(_live_daemon_errors(state))
        except Exception as exc:
            errors.append(
                f"live_publication_validation_failed:{type(exc).__name__}"
            )
    return _projection(
        state,
        errors=errors,
        identity=identity,
        compare_process=False,
    )


def _verification_fail_closed_projection(
    source: dict[str, Any] | None,
    *,
    state: str,
    checked_at: float | None,
    fresh_until: float | None,
    error: str | None,
    authority: dict[str, Any] | None = None,
) -> dict[str, Any]:
    projection = (
        copy.deepcopy(source)
        if isinstance(source, dict)
        else _projection(None, errors=[f"verification_{state}"])
    )
    if state != "fresh":
        target = int(projection.get("target") or TARGET_GENERATIONS)
        projection.update({
            "continuity_valid": False,
            "count": 0,
            "target": target,
            "remaining": target,
            "complete": False,
            "strength_cycle_ready": False,
            "strength_cycle": {
                "ready": False,
                "reason": f"verification_{state}",
            },
            "observations": [],
        })
        projection["errors"] = list(dict.fromkeys([
            *(projection.get("errors") or []),
            error or f"verification_{state}",
        ]))
    projection["verification"] = {
        "state": state,
        "checked_at": checked_at,
        "fresh_until": fresh_until,
        "error": error,
        "authority": copy.deepcopy(authority),
    }
    return projection


def _stability_cache_authority(
    expected_epoch_authority_digest: str | None,
) -> dict[str, Any]:
    """Return the cheap local identity that may reuse one verified snapshot."""

    if expected_epoch_authority_digest is not None and not _is_hex(
        expected_epoch_authority_digest,
        64,
    ):
        raise StabilityObservationError("epoch_stream_authority_digest_invalid")
    repository_head = str(_git("rev-parse", "--verify", "HEAD") or "")
    repository_branch = str(_git("rev-parse", "--abbrev-ref", "HEAD") or "")
    if not _is_hex(repository_head, 40):
        raise StabilityObservationError("cache_repository_head_unavailable")
    if (
        not repository_branch
        or repository_branch == "HEAD"
        or any(char.isspace() or ord(char) < 32 for char in repository_branch)
    ):
        raise StabilityObservationError("cache_repository_branch_unavailable")
    return {
        "evaluation_epoch": EVALUATION_EPOCH,
        "epoch_stream_authority_digest": expected_epoch_authority_digest,
        "repository_head": repository_head,
        "repository_branch": repository_branch,
    }


def _stability_projection_refresh_worker(
    generation: int,
    authority: dict[str, Any],
) -> None:
    global _PROJECTION_CACHE_VALUE
    global _PROJECTION_CACHE_CHECKED_AT
    global _PROJECTION_CACHE_FRESH_UNTIL
    global _PROJECTION_CACHE_ERROR
    global _PROJECTION_CACHE_RETRY_AFTER
    global _PROJECTION_CACHE_INFLIGHT

    value: dict[str, Any] | None = None
    error: str | None = None
    try:
        value = stability_observation_projection()
        if not isinstance(value, dict):
            raise StabilityObservationError("projection_not_object")
        if (
            value.get("current_repository_head")
            != authority.get("repository_head")
            or value.get("current_repository_branch")
            != authority.get("repository_branch")
        ):
            raise StabilityObservationError(
                "projection_repository_authority_changed"
            )
        live_authority = _stability_cache_authority(
            authority.get("epoch_stream_authority_digest")
        )
        if live_authority != authority:
            raise StabilityObservationError(
                "projection_repository_authority_changed"
            )
    except BaseException as exc:
        # This verifier runs in a detached background thread.  Cancellation,
        # test fail-fast sentinels, and interpreter-level thread exits must not
        # strand the single-flight lease forever; project them as a failed
        # refresh so a later invalidation/retry can make progress.
        error = f"{type(exc).__name__}:{str(exc)[:240]}"
    checked_at = _now()
    with _PROJECTION_CACHE_LOCK:
        if (
            generation != _PROJECTION_CACHE_GENERATION
            or authority != _PROJECTION_CACHE_AUTHORITY
        ):
            # Invalidation never steals ownership from a live verifier.  Once
            # this obsolete worker exits, a later reader may launch the single
            # refresh for the current generation.
            _PROJECTION_CACHE_INFLIGHT = False
            return
        _PROJECTION_CACHE_INFLIGHT = False
        _PROJECTION_CACHE_CHECKED_AT = checked_at
        if error is None:
            _PROJECTION_CACHE_VALUE = copy.deepcopy(value)
            _PROJECTION_CACHE_FRESH_UNTIL = (
                checked_at + STABILITY_VERIFICATION_TTL_SEC
            )
            _PROJECTION_CACHE_ERROR = None
            _PROJECTION_CACHE_RETRY_AFTER = 0.0
        else:
            _PROJECTION_CACHE_VALUE = None
            _PROJECTION_CACHE_FRESH_UNTIL = None
            _PROJECTION_CACHE_ERROR = error
            _PROJECTION_CACHE_RETRY_AFTER = (
                checked_at + STABILITY_VERIFICATION_RETRY_SEC
            )


def invalidate_stability_projection_cache() -> None:
    """Invalidate without allowing a second remote verifier to overlap.

    A running verifier keeps ownership until it exits.  Clearing ``inflight``
    here used to let every state/config invalidation launch another 30-second
    ``ls-remote`` thread while the obsolete worker was still blocked.
    """

    global _PROJECTION_CACHE_GENERATION
    global _PROJECTION_CACHE_VALUE
    global _PROJECTION_CACHE_CHECKED_AT
    global _PROJECTION_CACHE_FRESH_UNTIL
    global _PROJECTION_CACHE_ERROR
    global _PROJECTION_CACHE_RETRY_AFTER
    global _PROJECTION_CACHE_AUTHORITY

    with _PROJECTION_CACHE_LOCK:
        _PROJECTION_CACHE_GENERATION += 1
        _PROJECTION_CACHE_VALUE = None
        _PROJECTION_CACHE_CHECKED_AT = None
        _PROJECTION_CACHE_FRESH_UNTIL = None
        _PROJECTION_CACHE_ERROR = None
        _PROJECTION_CACHE_RETRY_AFTER = 0.0
        _PROJECTION_CACHE_AUTHORITY = None
        # Deliberately preserve _PROJECTION_CACHE_INFLIGHT.  The obsolete
        # worker observes the generation mismatch and releases ownership; the
        # next status poll then launches exactly one refresh for this generation.


def stability_observation_cached_projection(
    *,
    expected_epoch_authority_digest: str | None = None,
    prefetch_lead_sec: float = 0.0,
) -> dict[str, Any]:
    """Return immediately and coalesce expensive remote verification.

    The first reader receives a fail-closed ``pending`` snapshot.  At most one
    daemon thread reopens tags, certificates, evidence, daemon identity and
    origin refs.  Expired snapshots become ``stale`` before refresh, so a prior
    green N/10 value never survives beyond the declared TTL.  A verified value
    is also scoped to the caller's stable epoch token and the current local
    branch/HEAD, so publication invalidates an old green value immediately.
    """

    global _PROJECTION_CACHE_INFLIGHT
    global _PROJECTION_CACHE_GENERATION
    global _PROJECTION_CACHE_VALUE
    global _PROJECTION_CACHE_CHECKED_AT
    global _PROJECTION_CACHE_FRESH_UNTIL
    global _PROJECTION_CACHE_ERROR
    global _PROJECTION_CACHE_RETRY_AFTER
    global _PROJECTION_CACHE_AUTHORITY

    try:
        authority = _stability_cache_authority(
            expected_epoch_authority_digest
        )
    except Exception as exc:
        return _verification_fail_closed_projection(
            None,
            state="failed",
            checked_at=None,
            fresh_until=None,
            error=f"cache_authority_unavailable:{type(exc).__name__}",
            authority=None,
        )

    try:
        prefetch_lead = float(prefetch_lead_sec)
    except (TypeError, ValueError) as exc:
        raise StabilityObservationError(
            "stability_verification_prefetch_lead_invalid"
        ) from exc
    if not math.isfinite(prefetch_lead) or prefetch_lead < 0:
        raise StabilityObservationError(
            "stability_verification_prefetch_lead_invalid"
        )
    # Never make the prefetch window longer than the verified lifetime.  The
    # cap keeps an accidental caller value from turning each reader into a
    # verifier launch loop.
    prefetch_lead = min(prefetch_lead, STABILITY_VERIFICATION_TTL_SEC)

    now = _now()
    launch = False
    generation = 0
    with _PROJECTION_CACHE_LOCK:
        if _PROJECTION_CACHE_AUTHORITY != authority:
            _PROJECTION_CACHE_GENERATION += 1
            _PROJECTION_CACHE_VALUE = None
            _PROJECTION_CACHE_CHECKED_AT = None
            _PROJECTION_CACHE_FRESH_UNTIL = None
            _PROJECTION_CACHE_ERROR = None
            _PROJECTION_CACHE_RETRY_AFTER = 0.0
            _PROJECTION_CACHE_AUTHORITY = copy.deepcopy(authority)
            # Do not steal the single worker slot.  A verifier for the prior
            # authority observes the generation mismatch and releases it.
        value = copy.deepcopy(_PROJECTION_CACHE_VALUE)
        checked_at = _PROJECTION_CACHE_CHECKED_AT
        fresh_until = _PROJECTION_CACHE_FRESH_UNTIL
        error = _PROJECTION_CACHE_ERROR
        fresh = (
            value is not None
            and isinstance(fresh_until, (int, float))
            and math.isfinite(float(fresh_until))
            and now < float(fresh_until)
        )
        if fresh:
            # A lifecycle-owned maintainer may ask for a second verifier
            # shortly before expiry.  Keep returning the still-bound fresh
            # projection while the single-flight worker runs; if it fails or
            # stalls, the original TTL remains the exact fail-closed boundary.
            if (
                prefetch_lead > 0
                and now >= float(fresh_until) - prefetch_lead
                and not _PROJECTION_CACHE_INFLIGHT
                and now >= _PROJECTION_CACHE_RETRY_AFTER
            ):
                _PROJECTION_CACHE_INFLIGHT = True
                launch = True
                generation = _PROJECTION_CACHE_GENERATION
            result = _verification_fail_closed_projection(
                value,
                state="fresh",
                checked_at=checked_at,
                fresh_until=fresh_until,
                error=None,
                authority=authority,
            )
        elif (
            not _PROJECTION_CACHE_INFLIGHT
            and now >= _PROJECTION_CACHE_RETRY_AFTER
        ):
            _PROJECTION_CACHE_INFLIGHT = True
            launch = True
            generation = _PROJECTION_CACHE_GENERATION

        if fresh:
            pass
        elif value is not None:
            result = _verification_fail_closed_projection(
                value,
                state="stale",
                checked_at=checked_at,
                fresh_until=fresh_until,
                error="stability_verification_expired",
                authority=authority,
            )
        elif error is not None:
            result = _verification_fail_closed_projection(
                None,
                state="failed",
                checked_at=checked_at,
                fresh_until=None,
                error=error,
                authority=authority,
            )
        else:
            result = _verification_fail_closed_projection(
                None,
                state="pending",
                checked_at=None,
                fresh_until=None,
                error=None,
                authority=authority,
            )

    if launch:
        threading.Thread(
            target=_stability_projection_refresh_worker,
            args=(generation, copy.deepcopy(authority)),
            name="stability-verification",
            daemon=True,
        ).start()
    return result


def initialize_stability_observation(
    reason: str = "runtime_process_start",
) -> dict[str, Any]:
    """Start or resume observation for the current process identity.

    A prior process can never resume a streak: its boot identity causes an
    atomic reset to zero.  Repeated initialization in the same process is
    idempotent.
    """

    identity = _current_identity()
    with locked_file(LOCK_FILE, "a+", lock_type=fcntl.LOCK_EX):
        state, errors = _read_state_unlocked()
        if state is not None and not errors and not _identity_mismatches(state, identity):
            return _projection(state, identity=identity)
        if state is not None and not errors:
            process_mismatches = _identity_mismatches(
                state,
                identity,
            )
            process_mismatches = [
                item
                for item in process_mismatches
                if item in {"process_boot_id", "process_pid", "process_start_ticks"}
            ]
            if process_mismatches and not _owner_process_errors(state):
                raise StabilityObservationError(
                    "stability_observation_owner_process_still_alive"
                )
        reset_reason = reason
        details: dict[str, Any] = {}
        if errors and errors != ["state_missing"]:
            reset_reason = "observation_state_invalid"
            details["errors"] = errors[:20]
        elif state is not None:
            details["identity_mismatches"] = _identity_mismatches(state, identity)
        state = _new_state(
            identity,
            reason=reset_reason,
            details=details,
            previous=state if not errors else None,
        )
        _write_state_unlocked(state)
    invalidate_stability_projection_cache()
    return _projection(state, identity=identity)


def reset_stability_observation(
    reason: str,
    *,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Atomically reset the uninterrupted-generation count to zero."""

    if not str(reason or "").strip():
        raise StabilityObservationError("reset_reason_required")
    identity = _current_identity()
    with locked_file(LOCK_FILE, "a+", lock_type=fcntl.LOCK_EX):
        previous, errors = _read_state_unlocked()
        if previous is not None and not errors:
            process_mismatches = _identity_mismatches(previous, identity)
            process_mismatches = [
                item
                for item in process_mismatches
                if item in {"process_boot_id", "process_pid", "process_start_ticks"}
            ]
            if process_mismatches and not _owner_process_errors(previous):
                raise StabilityObservationError(
                    "stability_observation_owner_process_still_alive"
                )
        if errors:
            previous = None
        state = _new_state(
            identity,
            reason=str(reason),
            details=details,
            previous=previous,
        )
        _write_state_unlocked(state)
    invalidate_stability_projection_cache()
    return _projection(state, identity=identity)


def _publication_head_transition_issues(
    *,
    previous_head: str | None,
    current_head: str,
    publication_commit: str,
    remote_main: str,
) -> list[str]:
    """Validate the sole permitted repository-HEAD movement.

    A published generation may advance the runtime checkout by exactly its
    content-bound publication commit.  Any other local/remote HEAD is fatal to
    the row.  More than one intervening commit is treated as infrastructure
    drift: the publication may become the first row of a new streak, but it
    cannot retain the earlier N/10 count.
    """

    issues: list[str] = []
    if not _is_hex(current_head, 40) or current_head != publication_commit:
        issues.append("publication_repository_head_mismatch")
    if not _is_hex(remote_main, 40) or remote_main != publication_commit:
        issues.append("publication_remote_main_head_mismatch")
    if previous_head is None or previous_head == current_head:
        return issues
    if not _is_hex(previous_head, 40) or not _git_command_succeeds(
        "merge-base",
        "--is-ancestor",
        previous_head,
        current_head,
    ):
        issues.append("publication_repository_head_not_descendant")
        return issues
    try:
        advance_count = int(
            _git("rev-list", "--count", f"{previous_head}..{current_head}")
        )
    except Exception as exc:
        issues.append(
            f"publication_repository_advance_unavailable:{type(exc).__name__}"
        )
    else:
        if advance_count != 1:
            issues.append("publication_repository_advance_not_single_commit")
    return list(dict.fromkeys(issues))


def record_published_generation(
    *,
    version: int,
    publication_result: dict[str, Any],
    publishing_checkpoint: dict[str, Any],
) -> dict[str, Any]:
    """Record one fully proven strict publication, idempotently.

    The caller must pass the frozen ``publishing`` checkpoint retained before
    publication clears it.  Missing remote, certificate, workflow, or
    checkpoint-clear proof is rejected instead of incrementing the count.
    """

    version = int(version)
    if version < FIRST_STRICT_POLICY_VERSION:
        raise StabilityObservationError("pre_epoch_publication")
    if not isinstance(publication_result, dict) or not isinstance(
        publishing_checkpoint, dict
    ):
        raise StabilityObservationError("publication_proof_missing")
    intent = publishing_checkpoint.get("publication_intent")
    if publishing_checkpoint.get("stage") != "publishing" or not isinstance(intent, dict):
        raise StabilityObservationError("publishing_checkpoint_invalid")
    required_result = {
        "committed": True,
        "push_ok": True,
        "checkpoint_cleared": True,
        "completed_sentinel_written": True,
    }
    for key, expected in required_result.items():
        if publication_result.get(key) is not expected:
            raise StabilityObservationError(f"publication_{key}_not_proven")
    if int(publication_result.get("version") or -1) != version:
        raise StabilityObservationError("publication_version_mismatch")

    publication_id = str(publication_result.get("publication_id") or "")
    if not _is_hex(publication_id, 64) or publication_id != str(intent.get("publication_id") or ""):
        raise StabilityObservationError("publication_id_mismatch")
    remote_proof = publication_result.get("remote_proof")
    if intent.get("remote_publication_required") is not True:
        raise StabilityObservationError("remote_publication_not_required")
    if not isinstance(remote_proof, dict) or remote_proof.get("valid") is not True:
        raise StabilityObservationError("remote_publication_proof_invalid")
    remote_refs = remote_proof.get("remote_refs")
    if not isinstance(remote_refs, dict):
        raise StabilityObservationError("remote_publication_refs_missing")
    remote_binding: dict[str, Any] = {
        "remote_main_oid": str(remote_proof.get("remote_main_oid") or ""),
        "refs": {},
    }
    commit_oid = str(publication_result.get("commit_oid") or "")
    for name in (bot_tag(version), f"national-high-water-v{version}"):
        object_oid = str(remote_refs.get(f"refs/tags/{name}") or "")
        peeled_oid = str(remote_refs.get(f"refs/tags/{name}^{{}}") or "")
        if not _is_hex(object_oid, 40) or peeled_oid != commit_oid:
            raise StabilityObservationError(
                f"remote_publication_ref_invalid:{name}"
            )
        remote_binding["refs"][name] = {
            "object_oid": object_oid,
            "peeled_commit_oid": peeled_oid,
        }
    if not _is_hex(remote_binding["remote_main_oid"], 40):
        raise StabilityObservationError("remote_publication_main_invalid")
    row = {
        "version": version,
        "completion_tag": bot_tag(version),
        "publication_id": publication_id,
        "commit_oid": commit_oid,
        "certificate_digest": str(intent.get("official_certificate_digest") or ""),
        "candidate_artifact_hash": str(intent.get("candidate_artifact_hash") or ""),
        "official_policy_id": str(intent.get("official_policy_id") or ""),
        "official_status_digest": str(intent.get("official_status_digest") or ""),
        "certificate_file_sha256": str(intent.get("certificate_file_sha256") or ""),
        "certificate_attestation_digest": str(
            intent.get("certificate_attestation_digest") or ""
        ),
        "workflow_run_id": str(publishing_checkpoint.get("workflow_run_id") or ""),
        "source_v": int(publishing_checkpoint.get("source_v")),
        "parent2_v": publishing_checkpoint.get("parent2_v"),
        "final_gate_ledger_digest": str(intent.get("final_gate_ledger_digest") or ""),
        "workflow_profile_id": str(publishing_checkpoint.get("workflow_profile_id") or ""),
        "national_execution_mode": str(
            publishing_checkpoint.get("national_execution_mode") or ""
        ),
        "generation_evidence": _generation_evidence_binding(
            version,
            int(publishing_checkpoint.get("source_v")),
            publishing_checkpoint,
        ),
        "daemon_process_identity": _daemon_process_identity(),
        "push_ok": True,
        "remote_publication_required": True,
        "remote_publication": remote_binding,
        "checkpoint_cleared": True,
        "completed_sentinel_written": True,
        "published_at": _now(),
    }
    for key in (
        "commit_oid",
        "certificate_digest",
        "candidate_artifact_hash",
        "official_policy_id",
        "official_status_digest",
        "certificate_file_sha256",
        "certificate_attestation_digest",
        "workflow_run_id",
        "daemon_process_identity",
        "final_gate_ledger_digest",
        "workflow_profile_id",
    ):
        if not row[key]:
            raise StabilityObservationError(f"publication_{key}_missing")
    if not _is_hex(row["commit_oid"], 40):
        raise StabilityObservationError("publication_commit_oid_invalid")
    for key in (
        "certificate_digest",
        "candidate_artifact_hash",
        "official_status_digest",
        "certificate_file_sha256",
        "certificate_attestation_digest",
        "daemon_process_identity",
        "final_gate_ledger_digest",
    ):
        if not _is_hex(row[key], 64):
            raise StabilityObservationError(f"publication_{key}_invalid")

    repair_fields = {
        key: publishing_checkpoint.get(key)
        for key in (
            "generation_attempt",
            "precommit_rework_count",
            "official_rework_count",
        )
        if int(publishing_checkpoint.get(key) or 0) > 0
    }
    if str(publishing_checkpoint.get("repair_baseline_artifact_hash") or ""):
        repair_fields["repair_baseline_artifact_hash"] = publishing_checkpoint.get(
            "repair_baseline_artifact_hash"
        )
    identity = _current_identity()
    preflight_head_issues = _publication_head_transition_issues(
        previous_head=None,
        current_head=str(identity.get("repository_head") or ""),
        publication_commit=commit_oid,
        remote_main=str(remote_binding.get("remote_main_oid") or ""),
    )
    if preflight_head_issues:
        raise StabilityObservationError(preflight_head_issues[0])
    with locked_file(LOCK_FILE, "a+", lock_type=fcntl.LOCK_EX):
        state, errors = _read_state_unlocked()
        prior_state = state if state is not None and not errors else None
        prior_publication_ids = {
            str(item.get("publication_id") or "")
            for item in (
                prior_state.get("observations")
                if isinstance(prior_state, dict)
                and isinstance(prior_state.get("observations"), list)
                else []
            )
            if isinstance(item, dict)
            and _is_hex(item.get("publication_id"), 64)
        }
        if state is not None and not errors:
            process_mismatches = [
                item
                for item in _identity_mismatches(state, identity)
                if item in {"process_boot_id", "process_pid", "process_start_ticks"}
            ]
            if process_mismatches and not _owner_process_errors(state):
                raise StabilityObservationError(
                    "stability_observation_owner_process_still_alive"
                )
        identity_mismatches = (
            _identity_mismatches(state, identity)
            if state is not None and not errors
            else []
        )
        non_head_mismatches = [
            item for item in identity_mismatches if item != "repository_head"
        ]
        state_reinitialized = bool(
            state is None or errors or non_head_mismatches
        )
        previous_head = (
            str(state.get("repository_head") or "")
            if state is not None and not errors
            else None
        )
        if state_reinitialized:
            state = _new_state(
                identity,
                reason="publication_observer_reinitialized",
                details={
                    "errors": errors[:20],
                    "identity_mismatches": non_head_mismatches,
                },
                previous=state if state is not None and not errors else None,
            )
        if publication_id in prior_publication_ids:
            if not state_reinitialized and not identity_mismatches:
                return _projection(state, identity=identity)
            # The publication predates this new continuity identity. Persist
            # the reset, but never reinterpret the replayed ID as generation
            # one after a branch/process/config/contract transition.
            if not state_reinitialized:
                state = _new_state(
                    identity,
                    reason="publication_identity_drift",
                    details={
                        "identity_mismatches": identity_mismatches,
                        "replayed_publication_id": publication_id,
                    },
                    previous=prior_state,
                )
            _write_state_unlocked(state)
            invalidate_stability_projection_cache()
            return _projection(state, identity=identity)
        rows = list(state.get("observations") or [])
        if not state_reinitialized:
            current_head = str(identity.get("repository_head") or "")
            if previous_head == current_head:
                raise StabilityObservationError(
                    "publication_repository_head_not_advanced"
                )
            head_transition_issues = _publication_head_transition_issues(
                previous_head=previous_head,
                current_head=current_head,
                publication_commit=commit_oid,
                remote_main=str(remote_binding.get("remote_main_oid") or ""),
            )
            drift_issues = [
                item
                for item in head_transition_issues
                if item not in {
                    "publication_repository_head_mismatch",
                    "publication_remote_main_head_mismatch",
                }
            ]
            if drift_issues:
                state = _new_state(
                    identity,
                    reason="repository_head_drift",
                    details={
                        "previous_repository_head": previous_head,
                        "current_repository_head": current_head,
                        "issues": drift_issues,
                    },
                    previous=state,
                )
                rows = []
            else:
                state["repository_head"] = current_head
        if repair_fields:
            state = _new_state(
                identity,
                reason="generation_repair_detected",
                details={"version": version, "repair_fields": repair_fields},
                previous=state,
            )
            rows = []
        if rows and rows[-1].get("daemon_process_identity") != row[
            "daemon_process_identity"
        ]:
            state = _new_state(
                identity,
                reason="rating_daemon_restart_detected",
                details={
                    "previous_daemon_identity": rows[-1].get(
                        "daemon_process_identity"
                    ),
                    "current_daemon_identity": row["daemon_process_identity"],
                },
                previous=state,
            )
            rows = []
        if rows:
            last_version = int(rows[-1]["version"])
            if version <= last_version:
                raise StabilityObservationError("publication_order_regressed")
            if version != last_version + 1:
                state = _new_state(
                    identity,
                    reason="publication_version_gap",
                    details={
                        "previous_version": last_version,
                        "published_version": version,
                    },
                    previous=state,
                )
                rows = []
        rows.append(row)
        state["observations"] = rows[-TARGET_GENERATIONS:]
        state["updated_at"] = _now()
        _write_state_unlocked(state)
    invalidate_stability_projection_cache()
    return _projection(state, identity=identity)


__all__ = [
    "KIND",
    "SCHEMA_VERSION",
    "STATE_FILE",
    "TARGET_GENERATIONS",
    "StabilityObservationError",
    "bind_runtime_configuration",
    "clear_runtime_configuration_binding",
    "initialize_stability_observation",
    "invalidate_stability_projection_cache",
    "record_published_generation",
    "reset_stability_observation",
    "runtime_configuration_identity",
    "stability_observation_cached_projection",
    "stability_observation_projection",
]
