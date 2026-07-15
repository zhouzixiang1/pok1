"""Read-only status plus explicit, authenticated operator controls."""

import asyncio
import hashlib
import json
import logging
import math
import os
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parents[3]
WEB_DIR = PROJECT_ROOT / "web"
RESULTS_DIR = WEB_DIR / "core" / "results"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(WEB_DIR / "core"))

from server.state import app_state, run_evolution_task
from server.operator_control import CONTROL_TOKEN_HEADER, require_operator_mutation
from blocking_runtime import run_blocking_isolated

router = APIRouter(prefix="/api/control", tags=["control"])
_log = logging.getLogger("pok.control")
_RUNTIME_LIFECYCLE_LOCK = asyncio.Lock()
_LifecycleResult = TypeVar("_LifecycleResult")


async def _run_lifecycle_operation(
    factory: Callable[[], Awaitable[_LifecycleResult]],
) -> _LifecycleResult:
    """Serialize start/stop/config and drain a transaction on cancellation.

    ``run_blocking_isolated`` cannot stop a Python worker thread after request
    cancellation.  Shielding and draining the complete lifecycle operation
    keeps the mutex held until its write/rollback outcome is known, so another
    request cannot observe and mutate a half-transaction.
    """

    async with _RUNTIME_LIFECYCLE_LOCK:
        operation = asyncio.create_task(factory())
        cancellation: asyncio.CancelledError | None = None
        while not operation.done():
            try:
                await asyncio.shield(operation)
            except asyncio.CancelledError as exc:
                cancellation = exc
        if cancellation is not None:
            try:
                operation.result()
            except BaseException as exc:
                _log.error(
                    "Cancelled lifecycle request completed with %s: %s",
                    type(exc).__name__,
                    str(exc)[:240],
                )
            raise cancellation
        return operation.result()

# This is deliberately an explicit HTTP capability registry, not a projection
# of the Orchestrator's MCP ``all_tools`` registry.  Adding an MCP tool can
# therefore never make it remotely callable through the dashboard.
_CONTROL_CAPABILITIES = (
    {"id": "read_status", "method": "GET", "path": "/api/control/status", "mutation": False},
    {"id": "read_health", "method": "GET", "path": "/api/control/health", "mutation": False},
    {"id": "read_config", "method": "GET", "path": "/api/control/config", "mutation": False},
    {"id": "read_decisions", "method": "GET", "path": "/api/control/decisions", "mutation": False},
    {
        "id": "read_orchestrator_session",
        "method": "GET",
        "path": "/api/control/orchestrator/session",
        "mutation": False,
    },
    {
        "id": "start_evolution",
        "method": "POST",
        "path": "/api/control/start",
        "mutation": True,
        "requires_epoch": True,
    },
    {
        "id": "stop_evolution",
        "method": "POST",
        "path": "/api/control/stop",
        "mutation": True,
        "requires_epoch": False,
    },
    {
        "id": "update_config",
        "method": "PUT",
        "path": "/api/control/config",
        "mutation": True,
        "requires_epoch": True,
    },
)


def _epoch_access_state(operation: str) -> dict:
    """Return canonical launch state, failing closed on authority errors."""

    try:
        from epoch_authority import require_policy_epoch_initialized

        return require_policy_epoch_initialized(operation)
    except Exception as exc:
        return getattr(exc, "state", None) or {
            "evaluation_epoch": "national_tcp_policy_v1",
            "state": "epoch_authority_unavailable",
            "initialized": False,
            "operator_action": "inspect_epoch_authority",
            "operator_command": None,
        }


def _require_initialized_epoch(operation: str) -> dict:
    """Translate the canonical epoch launch guard into an HTTP 409.

    This helper intentionally does not persist an event on denial because the
    canonical event ledger still belongs to the retired epoch.
    """

    state = _epoch_access_state(operation)
    if not state.get("initialized"):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "policy_epoch_not_initialized",
                "operation": operation,
                "epoch": state,
            },
        ) from None
    return state


def _read_pid_info(path: Path) -> dict:
    # Reuse the lifecycle module's exact, read-only identity proof.  A
    # heartbeat alone cannot turn a forged PID record into a live daemon.
    from daemon_management import daemon_pid_record_health

    return daemon_pid_record_health(path)


def _read_pipeline_health(status: dict) -> dict:
    """Project pipeline health only from canonical strict epoch status.

    The old implementation reopened ``pipeline_state.json`` and rendered raw
    fields even when that file belonged to the retired epoch.  The status
    projection has already validated the strict checkpoint envelope; health
    must not invent a second checkpoint reader or expose unbound state.
    """

    authority = "strict_epoch_projection"
    if status.get("status_sync_error"):
        return {
            "exists": False,
            "stage": None,
            "authority": authority,
            "error": "canonical_epoch_projection_unavailable",
        }

    active = status.get("active_generation")
    handoff = status.get("post_publication_handoff")
    if isinstance(handoff, dict) and handoff.get("status") != "none":
        conflict = isinstance(active, dict)
        blocked = bool(handoff.get("blocked")) or conflict
        issues = list(handoff.get("issues") or [])
        if conflict:
            issues.append("active_generation_and_handoff_overlap")
        return {
            "exists": True,
            "stage": "post_publication_handoff",
            "authority": "post_publication_handoff_journal",
            "epoch_state": status.get("epoch_state"),
            "blocked": blocked,
            "issues": list(dict.fromkeys(issues)),
            "next_v": handoff.get("version"),
            "source_v": handoff.get("source_v"),
            "workflow_run_id": handoff.get("workflow_run_id"),
            "checkpoint_revision": handoff.get("record_revision"),
            "handoff_identity_digest": handoff.get("identity_digest"),
            "handoff_projection_digest": handoff.get("projection_digest"),
            "publication_id": handoff.get("publication_id"),
            "route": None if blocked else {
                "stage": "post_publication_handoff",
                "next_tool": "run_archivist",
                "next_v": handoff.get("version"),
                "source_v": handoff.get("source_v"),
                "parent2_v": None,
                "allowed_tools": ["run_archivist"],
                "intent": "post_publication_handoff",
                "directive": (
                    "Resume the exact durable Archivist handoff before "
                    "preparing another generation."
                ),
                "identity_digest": handoff.get("identity_digest"),
            },
        }
    ignored = status.get("ignored_checkpoint")
    if not isinstance(active, dict):
        return {
            "exists": False,
            "stage": None,
            "authority": authority,
            "epoch_state": status.get("epoch_state"),
            "blocked": not bool(status.get("epoch_initialized")),
            "ignored_checkpoint": ignored if isinstance(ignored, dict) else None,
        }

    # Only an initialized projection with a validated active generation may
    # reopen the live checkpoint for recovery diagnostics.  Revalidate the
    # bytes read now so a concurrent replacement cannot inherit the earlier
    # projection's authority.
    if not status.get("epoch_initialized"):
        return {
            "exists": False,
            "stage": None,
            "authority": authority,
            "epoch_state": status.get("epoch_state"),
            "blocked": True,
            "error": "active_generation_without_initialized_epoch",
        }
    try:
        from checkpoint_schema import (
            checkpoint_epoch_errors,
            live_policy_epoch_reset_receipt_errors,
        )
        from evolution_infra import PROJECT_ROOT as CORE_PROJECT_ROOT
        from evolution_infra import read_pipeline_checkpoint
        from pipeline_recovery import checkpoint_recovery_diagnostics
        from pipeline_state import route_policy

        checkpoint = read_pipeline_checkpoint()
        issues = checkpoint_epoch_errors(checkpoint)
        if not issues:
            issues.extend(
                live_policy_epoch_reset_receipt_errors(
                    checkpoint,
                    project_root=CORE_PROJECT_ROOT,
                )
            )
        checkpoint_obj = checkpoint if isinstance(checkpoint, dict) else {}
        observed_generation_attempt = int(
            checkpoint_obj.get("generation_attempt") or 0
        )
        observed_run_id = checkpoint_obj.get("run_id") or (
            f"{checkpoint_obj.get('next_v')}#{observed_generation_attempt}"
            if checkpoint_obj.get("next_v") is not None
            else None
        )
        identity_fields = (
            "next_v",
            "source_v",
            "stage",
            "run_id",
            "workflow_run_id",
            "checkpoint_revision",
        )
        expected_identity = {
            "next_v": active.get("next_v"),
            "source_v": active.get("source_v"),
            "stage": active.get("stage"),
            "run_id": active.get("run_id"),
            "workflow_run_id": active.get("workflow_run_id"),
            "checkpoint_revision": active.get("checkpoint_revision"),
        }
        observed_identity = {
            "next_v": checkpoint_obj.get("next_v"),
            "source_v": checkpoint_obj.get("source_v"),
            "stage": checkpoint_obj.get("stage"),
            "run_id": observed_run_id,
            "workflow_run_id": checkpoint_obj.get("workflow_run_id"),
            "checkpoint_revision": checkpoint_obj.get("checkpoint_revision"),
        }
        identity_mismatches = [
            field
            for field in identity_fields
            if observed_identity[field] != expected_identity[field]
        ]
        if (
            type(expected_identity["checkpoint_revision"]) is not int
            or expected_identity["checkpoint_revision"] <= 0
            or type(observed_identity["checkpoint_revision"]) is not int
            or observed_identity["checkpoint_revision"] <= 0
        ):
            identity_mismatches.append("checkpoint_revision_invalid")
        if (
            not isinstance(expected_identity["run_id"], str)
            or not expected_identity["run_id"].strip()
            or not isinstance(observed_identity["run_id"], str)
            or not observed_identity["run_id"].strip()
        ):
            identity_mismatches.append("run_id_invalid")
        identity_mismatches = list(dict.fromkeys(identity_mismatches))
        if issues or identity_mismatches:
            return {
                "exists": True,
                "stage": active.get("stage"),
                "authority": authority,
                "epoch_state": status.get("epoch_state"),
                "error": "strict_checkpoint_revalidation_failed",
                "issues": list(dict.fromkeys(map(str, issues))),
                "identity_changed": bool(identity_mismatches),
                "identity_mismatches": identity_mismatches,
                "expected_identity": expected_identity,
                "observed_identity": observed_identity,
            }
        recovery = checkpoint_recovery_diagnostics(checkpoint)
        route = route_policy(checkpoint)
        if not isinstance(route, dict):
            raise RuntimeError("canonical route is not an object")
    except Exception as exc:
        return {
            "exists": True,
            "stage": active.get("stage"),
            "authority": authority,
            "epoch_state": status.get("epoch_state"),
            "error": f"strict_checkpoint_diagnostic_failed:{type(exc).__name__}",
        }

    attempt = active.get("attempt") if isinstance(active.get("attempt"), dict) else {}
    snapshot = {
        "exists": True,
        "authority": authority,
        "epoch_state": status.get("epoch_state"),
        "run_id": active.get("run_id"),
        "workflow_run_id": active.get("workflow_run_id"),
        "checkpoint_revision": active.get("checkpoint_revision"),
        "stage": active.get("stage"),
        "next_v": active.get("next_v"),
        "source_v": active.get("source_v"),
        "generation_attempt": attempt.get("generation"),
        "audit_attempt": attempt.get("audit"),
        "precommit_attempt": attempt.get("precommit"),
        "ignored_checkpoint": None,
        "recovery": recovery,
        "route": route,
    }
    now = time.time()
    for source_key, target_key in (
        ("last_stage_change_ts", "last_stage_age_sec"),
        ("last_update_ts", "last_update_age_sec"),
    ):
        value = checkpoint.get(source_key)
        if value is not None:
            try:
                snapshot[target_key] = round(now - float(value), 2)
            except (TypeError, ValueError):
                snapshot[target_key] = None
    return snapshot


def _daemon_health_snapshot() -> dict:
    data = _read_pid_info(RESULTS_DIR / ".daemon_pid")
    try:
        from elo_daemon import HEARTBEAT_STALE_SEC
    except Exception:
        HEARTBEAT_STALE_SEC = 120
    configured = bool(app_state.get_config().get("daemon_enabled"))
    data["configured"] = configured
    data["heartbeat_stale_sec"] = HEARTBEAT_STALE_SEC
    data["heartbeat_age_sec"] = None
    data["heartbeat_stale"] = False
    if not configured:
        data["heartbeat_status"] = "not_applicable"
        if data.get("alive"):
            data["health_error"] = "daemon_running_while_disabled"
        elif not data.get("exists"):
            # ``--no-daemon`` is a complete, supported runtime mode.  In
            # that mode an absent PID record proves the desired process
            # state; the lifecycle reader's fail-closed missing-file error is
            # only applicable when a daemon is configured.
            data["health_error"] = None
        return data
    if not data.get("alive"):
        data["heartbeat_status"] = "invalid" if data.get("exists") else "missing"
        if not data.get("health_error"):
            data["health_error"] = (
                "daemon_process_not_alive"
                if data.get("exists")
                else "daemon_pid_file_missing"
            )
        return data

    raw_heartbeat = data.get("last_heartbeat")
    if raw_heartbeat is None:
        data["heartbeat_status"] = "missing"
        data["health_error"] = "daemon_heartbeat_missing"
        return data
    try:
        if isinstance(raw_heartbeat, bool):
            raise ValueError("boolean heartbeat")
        heartbeat = float(raw_heartbeat)
        if not math.isfinite(heartbeat):
            raise ValueError("non-finite heartbeat")
    except (TypeError, ValueError, OverflowError):
        data["heartbeat_status"] = "invalid"
        data["health_error"] = "daemon_heartbeat_invalid"
        return data
    age = time.time() - heartbeat
    data["heartbeat_age_sec"] = round(age, 2)
    if age < -5.0:
        data["heartbeat_status"] = "future"
        data["health_error"] = "daemon_heartbeat_from_future"
    elif age > float(HEARTBEAT_STALE_SEC):
        data["heartbeat_status"] = "stale"
        data["heartbeat_stale"] = True
        data["health_error"] = "daemon_heartbeat_stale"
    else:
        data["heartbeat_status"] = "fresh"
        data["health_error"] = None
    return data


def _health_summary(status: dict) -> dict:
    task = app_state.task_snapshot()
    daemon = _daemon_health_snapshot()
    pipeline = _read_pipeline_health(status)
    issues = []
    task_active = bool(task.get("present") and task.get("done") is False)
    if not status.get("running"):
        issues.append("evolution_not_running")
    if status.get("running") and not task_active:
        issues.append("orchestrator_task_not_active")
    if task_active and task.get("shutdown_requested"):
        issues.append("orchestrator_stop_in_progress")
    if daemon.get("configured") != bool(status.get("daemon_enabled")):
        issues.append("daemon_configuration_projection_mismatch")
    if status.get("running") and status.get("daemon_enabled"):
        if not daemon.get("alive"):
            issues.append("daemon_dead")
        elif daemon.get("heartbeat_status") != "fresh":
            issues.append(
                f"daemon_heartbeat_{daemon.get('heartbeat_status') or 'unavailable'}"
            )
    if not status.get("daemon_enabled") and daemon.get("alive"):
        issues.append("daemon_running_while_disabled")
    if daemon.get("health_error"):
        issues.append(f"daemon_health_error:{daemon['health_error']}")
    if status.get("active_generation") and not pipeline.get("exists"):
        issues.append("active_generation_without_checkpoint")
    handoff = status.get("post_publication_handoff")
    if isinstance(handoff, dict) and handoff.get("blocked") is True:
        issues.append("post_publication_handoff_blocked")
    if pipeline.get("error"):
        issues.append("pipeline_checkpoint_unreadable")
    if pipeline.get("blocked") is True:
        issues.append("pipeline_blocked")
    recovery = pipeline.get("recovery") if isinstance(pipeline.get("recovery"), dict) else {}
    for issue in recovery.get("issues") or []:
        issues.append(f"pipeline_{issue}")
    stability = status.get("stability_observation")
    verification = (
        stability.get("verification")
        if isinstance(stability, dict)
        and isinstance(stability.get("verification"), dict)
        else {}
    )
    fresh_until = verification.get("fresh_until")
    verification_fresh = bool(
        verification.get("state") == "fresh"
        and isinstance(fresh_until, (int, float))
        and not isinstance(fresh_until, bool)
        and math.isfinite(float(fresh_until))
        and float(fresh_until) > time.time()
    )
    if (
        status.get("running")
        and status.get("epoch_initialized")
        and (
            not isinstance(stability, dict)
            or not stability.get("continuity_valid")
            or not verification_fresh
        )
    ):
        issues.append("stability_observation_unavailable")

    if not status.get("running"):
        overall = "stopped"
    elif issues:
        overall = "degraded"
    else:
        overall = "healthy"
    return {
        "overall": overall,
        "issues": issues,
        "status": status,
        "running": status.get("running"),
        "active_generation": status.get("active_generation"),
        "task": task,
        "daemon": daemon,
        "pipeline": pipeline,
        "checked_at": time.time(),
    }


def _stability_observation_digest(observation: Any) -> str:
    """Bind paired status/health reads to one exact stability projection."""

    try:
        encoded = json.dumps(
            observation,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        return ""
    return hashlib.sha256(encoded).hexdigest()


def _sync_evolution_fields(state: dict) -> dict:
    """Overlay cheap authoritative evolution fields for status reads.

    AppState is initialized from the latest completed tag, but the active
    pipeline may skip ahead because bare commits, abandoned versions, or a
    resume checkpoint reserve a later target. Keep /api/control/status aligned
    with the files the orchestrator actually uses, without mutating AppState
    from a read-only endpoint.
    """
    sampled_stream_authority_digest = None
    try:
        from epoch_authority import (
            epoch_stream_authority_digest,
            strict_epoch_projection,
            unpublished_candidate_versions,
        )
        from server.routes._helpers import (
            post_publication_handoff_projection,
            stable_epoch_handoff_sample,
        )

        epoch, handoff, stable_sample = stable_epoch_handoff_sample(
            strict_epoch_projection,
            lambda value: post_publication_handoff_projection(
                enabled=bool(value.get("initialized"))
            ),
        )
        if not stable_sample:
            raise RuntimeError(
                "canonical_epoch_changed_during_handoff_projection"
            )
        current_v = int(epoch["current_v"])
        next_v = int(epoch["next_v"])
        generation_count = int(epoch["strict_generation_count"])
        active_generation = epoch["active_generation"]

        state["current_v"] = current_v
        state["next_v"] = next_v
        state["generation_count"] = generation_count
        state["active_generation"] = active_generation
        state["evaluation_epoch"] = epoch["evaluation_epoch"]
        state["epoch_state"] = epoch["state"]
        state["epoch_initialized"] = bool(epoch["initialized"])
        state["version_authority_high_water"] = int(
            epoch["version_authority_high_water"]
        )
        state["strict_generation_count"] = generation_count
        state["strict_published_versions"] = epoch["strict_published_versions"]
        state["active_bots"] = epoch["active_bots"]
        state["reset_receipt_valid"] = bool(epoch["reset_receipt_valid"])
        state["reset_receipt_digest"] = epoch.get("reset_receipt_digest")
        sampled_stream_authority_digest = epoch_stream_authority_digest(epoch)
        state["stream_authority_digest"] = sampled_stream_authority_digest
        state["reset_receipt_issues"] = epoch["reset_receipt_issues"]
        state["operator_action"] = epoch["operator_action"]
        state["operator_command"] = epoch["operator_command"]
        state["runtime_reconciliation_claimed"] = bool(
            epoch.get("runtime_reconciliation_claimed")
        )
        state["runtime_reconciliation_kind"] = epoch.get(
            "runtime_reconciliation_kind"
        )
        state["runtime_reconciliation_claim_digest"] = epoch.get(
            "runtime_reconciliation_claim_digest"
        )
        state["runtime_reconciliation_claim_valid"] = bool(
            epoch.get("runtime_reconciliation_claim_valid")
        )
        state["runtime_reconciliation_claim_issues"] = list(
            epoch.get("runtime_reconciliation_claim_issues") or []
        )
        state["publication_recovery_ready"] = bool(
            epoch.get("publication_recovery_ready")
        )
        state["unpaired_completion_versions"] = list(
            epoch.get("unpaired_completion_versions") or []
        )
        state["unpaired_high_water_versions"] = list(
            epoch.get("unpaired_high_water_versions") or []
        )
        state["operator_transition"] = epoch.get("operator_transition")
        state["ignored_checkpoint"] = epoch["ignored_checkpoint"]
        state["unpublished_candidate_versions"] = unpublished_candidate_versions()
        state["post_publication_handoff"] = handoff
    except Exception as exc:
        # Never fall back to AppState's bootstrap counters: those values may
        # have been initialized from a retired checkpoint or abandoned bot
        # directory.  Return a complete, explicitly unavailable projection so
        # browser clients can render a recovery state without guessing fields
        # or accidentally presenting stale version authority.
        state.update({
            "current_v": 0,
            "next_v": 0,
            "generation_count": 0,
            "active_generation": None,
            "evaluation_epoch": "national_tcp_policy_v1",
            "epoch_state": "epoch_authority_unavailable",
            "epoch_initialized": False,
            "version_authority_high_water": 0,
            "strict_generation_count": 0,
            "strict_published_versions": [],
            "active_bots": [],
            "reset_receipt_valid": False,
            "reset_receipt_digest": None,
            "stream_authority_digest": None,
            "reset_receipt_issues": ["canonical_epoch_projection_unavailable"],
            "operator_action": "inspect_epoch_authority",
            "operator_command": None,
            "runtime_reconciliation_claimed": False,
            "runtime_reconciliation_kind": None,
            "runtime_reconciliation_claim_digest": None,
            "runtime_reconciliation_claim_valid": False,
            "runtime_reconciliation_claim_issues": [
                "canonical_epoch_projection_unavailable"
            ],
            "publication_recovery_ready": False,
            "unpaired_completion_versions": [],
            "unpaired_high_water_versions": [],
            "operator_transition": None,
            "ignored_checkpoint": None,
            "unpublished_candidate_versions": [],
        })
        state["status_sync_error"] = str(exc)[:200]
        from server.routes._helpers import post_publication_handoff_projection

        # Never open a journal after the epoch/checkpoint bracket failed.  A
        # disabled projection makes the unavailable authority explicit without
        # mixing a later handoff into the failed sample.
        state["post_publication_handoff"] = (
            post_publication_handoff_projection(enabled=False)
        )
    try:
        from stability_observation import stability_observation_cached_projection

        stability = stability_observation_cached_projection(
            expected_epoch_authority_digest=sampled_stream_authority_digest,
        )
        verification = stability.get("verification") if isinstance(
            stability, dict
        ) else None
        authority = verification.get("authority") if isinstance(
            verification, dict
        ) else None
        repository_head = (
            authority.get("repository_head")
            if isinstance(authority, dict)
            else None
        )
        repository_branch = (
            authority.get("repository_branch")
            if isinstance(authority, dict)
            else None
        )
        if (
            not isinstance(authority, dict)
            or authority.get("evaluation_epoch")
            != state.get("evaluation_epoch")
            or authority.get("epoch_stream_authority_digest")
            != sampled_stream_authority_digest
            or (
                bool(state.get("epoch_initialized"))
                and not isinstance(sampled_stream_authority_digest, str)
            )
            or not isinstance(repository_head, str)
            or len(repository_head) != 40
            or any(
                char not in "0123456789abcdef"
                for char in repository_head
            )
            or not isinstance(repository_branch, str)
            or not repository_branch
            or repository_branch == "HEAD"
        ):
            raise RuntimeError("stability_projection_authority_mismatch")
        state["stability_observation"] = stability
    except Exception as exc:
        state["stability_observation"] = {
            "schema_version": 1,
            "kind": "national-tcp-uninterrupted-evolution-observation",
            "authority": "operator_acceptance_only",
            "strategy_evidence_weight": 0,
            "strength_evidence_weight": 0,
            "status": "reset_required",
            "continuity_valid": False,
            "count": 0,
            "target": 10,
            "remaining": 10,
            "complete": False,
            "strength_cycle_ready": False,
            "strength_cycle": {
                "ready": False,
                "reason": "projection_failed",
            },
            "continuity_id": None,
            "last_reset_reason": "projection_failed",
            "identity_mismatches": [],
            "errors": [f"projection_failed:{type(exc).__name__}"],
            "verification": {
                "state": "failed",
                "checked_at": time.time(),
                "fresh_until": None,
                "error": f"projection_failed:{type(exc).__name__}",
                "authority": None,
            },
        }
    state["stability_observation_digest"] = _stability_observation_digest(
        state.get("stability_observation")
    )
    return state


class ConfigRequest(BaseModel):
    model_config = {"strict": True}
    daemon_enabled: bool | None = None
    daemon_workers: int | None = Field(default=None, ge=1, le=12)
    daemon_pairs: int | None = Field(default=None, ge=1, le=20)

    @property
    def safe_updates(self) -> dict:
        """Filter out None values."""
        result = {}
        if self.daemon_enabled is not None:
            result["daemon_enabled"] = self.daemon_enabled
        if self.daemon_workers is not None:
            result["daemon_workers"] = self.daemon_workers
        if self.daemon_pairs is not None:
            result["daemon_pairs"] = self.daemon_pairs
        return result


def _runtime_mutation_conflict() -> dict[str, Any] | None:
    task = app_state.task_snapshot()
    if app_state.to_dict().get("running"):
        return {"reason": "evolution_running", "task": task}
    if task.get("present") and task.get("done") is False:
        return {"reason": "evolution_task_still_active", "task": task}
    return None


def _require_runtime_stopped(operation: str) -> None:
    conflict = _runtime_mutation_conflict()
    if conflict is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "runtime_mutation_while_evolution_active",
                "operation": operation,
                **conflict,
            },
        )


def _bind_and_reset_stability(
    config: dict[str, Any],
    reason: str,
    details: dict[str, Any],
) -> None:
    from stability_observation import (
        bind_runtime_configuration,
        reset_stability_observation,
    )

    bind_runtime_configuration({
        key: config[key]
        for key in ("daemon_enabled", "daemon_workers", "daemon_pairs")
    })
    reset_stability_observation(reason, details=details)


def _apply_daemon_configuration(
    previous: dict[str, Any],
    current: dict[str, Any],
    *,
    daemon_was_alive: bool,
    transaction_state: dict[str, Any] | None = None,
) -> None:
    """Apply config to the actual daemon while the orchestrator is stopped."""

    from evolution_core import start_daemon, stop_daemon

    daemon_settings_changed = any(
        previous.get(key) != current.get(key)
        for key in ("daemon_enabled", "daemon_workers", "daemon_pairs")
    )
    if daemon_was_alive and daemon_settings_changed:
        if transaction_state is not None:
            transaction_state["daemon_mutation_started"] = True
        stop_daemon()
    # A stopped runtime stays stopped; enabling the configured daemon does not
    # launch an ownerless rating process before the orchestrator starts.
    if daemon_was_alive and current.get("daemon_enabled"):
        start_daemon(
            workers=int(current["daemon_workers"]),
            pairs=int(current["daemon_pairs"]),
        )


def _restore_daemon_configuration(
    previous: dict[str, Any],
    *,
    daemon_was_alive: bool,
) -> None:
    from evolution_core import start_daemon, stop_daemon

    if not daemon_was_alive:
        return
    # Stop any partially reconfigured process before reconstructing the exact
    # pre-transaction actual state.
    try:
        stop_daemon()
    except Exception:
        pass
    start_daemon(
        workers=int(previous["daemon_workers"]),
        pairs=int(previous["daemon_pairs"]),
    )


@router.get("/config")
async def get_config():
    return app_state.get_config()


async def _set_config_transaction(req: ConfigRequest) -> dict[str, Any]:
    updates = req.safe_updates
    if not updates:
        return app_state.get_config()
    _require_initialized_epoch("control_config_update")
    _require_runtime_stopped("control_config_update")
    previous_config = app_state.get_config()
    desired = {**previous_config, **updates}
    changed = {
        key: {"before": previous_config.get(key), "after": desired.get(key)}
        for key in updates
        if previous_config.get(key) != desired.get(key)
    }
    if not changed:
        return previous_config
    daemon_was_alive = bool(_daemon_health_snapshot().get("alive"))
    result: dict[str, Any] | None = None
    daemon_transaction_state: dict[str, Any] = {}
    try:
        result = await run_blocking_isolated(
            app_state.update_config,
            **updates,
            thread_name_prefix="control-config-write",
        )
        await run_blocking_isolated(
            _bind_and_reset_stability,
            result,
            "runtime_configuration_changed",
            {"changes": changed},
            thread_name_prefix="control-config-stability",
        )
        await run_blocking_isolated(
            _apply_daemon_configuration,
            previous_config,
            result,
            daemon_was_alive=daemon_was_alive,
            transaction_state=daemon_transaction_state,
            thread_name_prefix="control-config-daemon",
        )
    except Exception as exc:
        rollback_errors: list[str] = []
        try:
            await run_blocking_isolated(
                app_state.update_config,
                **{
                    key: previous_config[key]
                    for key in ("daemon_enabled", "daemon_workers", "daemon_pairs")
                },
                thread_name_prefix="control-config-rollback-write",
            )
        except Exception as rollback_exc:
            rollback_errors.append(
                f"config:{type(rollback_exc).__name__}:{str(rollback_exc)[:160]}"
            )
        if daemon_transaction_state.get("daemon_mutation_started") is True:
            try:
                await run_blocking_isolated(
                    _restore_daemon_configuration,
                    previous_config,
                    daemon_was_alive=daemon_was_alive,
                    thread_name_prefix="control-config-rollback-daemon",
                )
            except Exception as rollback_exc:
                rollback_errors.append(
                    f"daemon:{type(rollback_exc).__name__}:{str(rollback_exc)[:160]}"
                )
        try:
            await run_blocking_isolated(
                _bind_and_reset_stability,
                previous_config,
                "runtime_configuration_change_rolled_back",
                {
                    "changes": changed,
                    "failure": f"{type(exc).__name__}:{str(exc)[:200]}",
                },
                thread_name_prefix="control-config-rollback-stability",
            )
        except Exception as rollback_exc:
            rollback_errors.append(
                f"stability:{type(rollback_exc).__name__}:{str(rollback_exc)[:160]}"
            )
        raise HTTPException(
            status_code=500,
            detail={
                "code": "runtime_configuration_transaction_failed",
                "failure": f"{type(exc).__name__}:{str(exc)[:240]}",
                "rollback_errors": rollback_errors,
            },
        ) from None
    return result


@router.put("/config")
async def set_config(req: ConfigRequest, request: Request):
    require_operator_mutation(request, operation="control_config_update")
    return await _run_lifecycle_operation(
        lambda: _set_config_transaction(req)
    )


def _control_status_snapshot() -> dict[str, Any]:
    return _sync_evolution_fields(app_state.to_dict())


@router.get("/status")
async def control_status():
    return await run_blocking_isolated(
        _control_status_snapshot,
        thread_name_prefix="control-status-snapshot",
    )


def _control_health_snapshot() -> dict[str, Any]:
    status = _sync_evolution_fields(app_state.to_dict())
    return _health_summary(status)


@router.get("/health")
async def control_health():
    """Return a single read-only health snapshot for observers/supervisors."""
    return await run_blocking_isolated(
        _control_health_snapshot,
        thread_name_prefix="control-health-snapshot",
    )


@router.get("/decisions")
async def get_decisions(limit: int = 50):
    state = app_state.to_dict()
    decisions = state.get("decisions", [])
    if limit <= 0:
        return []
    return decisions[-limit:]


async def _start_evolution_transaction() -> dict[str, str]:
    _require_initialized_epoch("control_start_evolution")
    if not app_state.try_set_running(True):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "evolution_runtime_already_owned",
                "task": app_state.task_snapshot(),
            },
        )
    owner_id = app_state.runtime_owner_id()

    from server.app import web_ui
    web_ui._broadcaster.clear()
    config = app_state.get_config()

    try:
        await run_blocking_isolated(
            _bind_and_reset_stability,
            config,
            "orchestrator_restart",
            {"trigger": "control_start"},
            thread_name_prefix="control-start-stability",
        )
    except Exception as exc:
        app_state.abort_runtime_owner(owner_id)
        if "owner_process_still_alive" in str(exc):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "stability_observation_owner_active",
                    "message": (
                        "Another live runtime process owns the uninterrupted "
                        "evolution observation. Stop that runtime before starting."
                    ),
                },
            ) from None
        raise HTTPException(
            status_code=503,
            detail={
                "code": "stability_observation_reset_failed",
                "failure": f"{type(exc).__name__}:{str(exc)[:200]}",
            },
        ) from None

    task: asyncio.Task | None = None
    try:
        from shutdown_manager import ShutdownManager

        shutdown_mgr = ShutdownManager(grace_period=15.0)
        app_state.set_shutdown_mgr(shutdown_mgr, owner_id=owner_id)
        try:
            from llm_query import set_shutdown_manager
            set_shutdown_manager(shutdown_mgr)
        except Exception:
            pass

        from orchestrator import orchestrator_loop
        task = asyncio.create_task(run_evolution_task(orchestrator_loop(
            web_ui, shutdown_mgr=shutdown_mgr, no_daemon=not config["daemon_enabled"],
            daemon_workers=config["daemon_workers"], daemon_pairs=config["daemon_pairs"]),
            owner_id=owner_id,
        ))
        app_state.set_task(task, owner_id=owner_id)
    except BaseException:
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        app_state.abort_runtime_owner(owner_id)
        try:
            from llm_query import set_shutdown_manager
            set_shutdown_manager(None)
        except Exception:
            pass
        raise

    return {"status": "started", "mode": "orchestrator"}


@router.post("/start")
async def start_evolution(request: Request):
    require_operator_mutation(request, operation="control_start_evolution")
    return await _run_lifecycle_operation(_start_evolution_transaction)


async def _stop_evolution_transaction() -> dict[str, str]:
    app_state.request_shutdown()
    task = app_state.stop_running()
    if task and not task.done():
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=10)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
    if task is not None and not task.done():
        raise HTTPException(
            status_code=500,
            detail={
                "code": "evolution_task_stop_timeout",
                "task": app_state.task_snapshot(),
            },
        )
    try:
        from evolution_core import stop_daemon

        await run_blocking_isolated(
            stop_daemon,
            thread_name_prefix="control-stop-daemon",
        )
        await run_blocking_isolated(
            _bind_and_reset_stability,
            app_state.get_config(),
            "orchestrator_stopped",
            {"trigger": "control_stop"},
            thread_name_prefix="control-stop-stability",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "evolution_stop_incomplete",
                "failure": f"{type(exc).__name__}:{str(exc)[:240]}",
            },
        ) from None
    return {"status": "stopped"}


@router.post("/stop")
async def stop_evolution(request: Request):
    require_operator_mutation(request, operation="control_stop_evolution")
    return await _run_lifecycle_operation(_stop_evolution_transaction)


@router.post("/tool/{tool_name}", status_code=410)
async def retired_tool_executor(tool_name: str):
    """Permanently retire the old arbitrary MCP-over-HTTP dispatcher."""

    raise HTTPException(
        status_code=410,
        detail={
            "code": "control_tool_executor_retired",
            "tool": tool_name,
            "message": "Use explicit read-only APIs or operator control endpoints.",
        },
    )


@router.get("/tools")
async def list_tools():
    epoch = _epoch_access_state("control_tools_catalog")
    initialized = bool(epoch.get("initialized"))
    capabilities = []
    for definition in _CONTROL_CAPABILITIES:
        capability = dict(definition)
        requires_epoch = bool(capability.pop("requires_epoch", False))
        enabled = not requires_epoch or initialized
        capability["enabled"] = enabled
        capability["blocked_reason"] = (
            None if enabled else "policy_epoch_not_initialized"
        )
        capabilities.append(capability)
    enabled = [item["id"] for item in capabilities if item["enabled"]]
    blocked = [item["id"] for item in capabilities if not item["enabled"]]
    return {
        "capabilities": capabilities,
        # Transitional aliases are capability IDs, never MCP tool names.
        "tools": [item["id"] for item in capabilities],
        "enabled_tools": enabled,
        "blocked_tools": blocked,
        "epoch_initialized": initialized,
        "epoch_state": epoch.get("state"),
        "operator_action": epoch.get("operator_action"),
        "operator_auth_required": True,
        "operator_auth_modes": ["loopback_same_origin", "control_token"],
        "operator_token_configured": bool(os.environ.get("POK_CONTROL_TOKEN")),
        "operator_token_header": CONTROL_TOKEN_HEADER,
    }


# ── Orchestrator Session Management ──

# Compatibility path used only to identify retired debris in operator tests.
# The control plane must never read it as resumable provider-history authority.
ORCHESTRATOR_SESSION_FILE = (
    PROJECT_ROOT / "web" / "core" / "results" / "orchestrator_session.json"
)


@router.get("/orchestrator/session")
async def get_orchestrator_session():
    """Describe the non-resumable provider-history policy.

    Pipeline recovery is owned only by the validated checkpoint.  The opaque
    provider session id is neither persisted nor rendered as active authority.
    """
    epoch = _epoch_access_state("control_orchestrator_session_read")
    return {
        "session_id": None,
        "active": False,
        "blocked": not bool(epoch.get("initialized")),
        "resume_supported": False,
        "provider_history_persisted": False,
        "recovery_authority": "validated_checkpoint_only",
        "history_policy": "fresh_provider_session_from_checkpoint_projection_only",
        "epoch_state": epoch.get("state"),
        "operator_action": epoch.get("operator_action"),
    }


@router.delete("/orchestrator/session")
async def clear_orchestrator_session(request: Request):
    """Retired: provider sessions are never persisted or resumed."""
    require_operator_mutation(
        request,
        operation="control_orchestrator_session_clear",
    )
    raise HTTPException(
        status_code=410,
        detail={
            "code": "orchestrator_provider_session_resume_retired",
            "recovery_authority": "validated_checkpoint_only",
            "history_policy": (
                "fresh_provider_session_from_checkpoint_projection_only"
            ),
        },
    )
