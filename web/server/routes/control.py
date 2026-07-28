"""Read-only status plus explicit, authenticated operator controls."""

import asyncio
import copy
import hashlib
import json
import logging
import math
import os
import sys
import threading
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

from server.state import MAX_DAEMON_PAIRS, app_state, run_evolution_task
from server.operator_control import CONTROL_TOKEN_HEADER, require_operator_mutation
from blocking_runtime import run_blocking_isolated

router = APIRouter(prefix="/api/control", tags=["control"])
_log = logging.getLogger("pok.control")
_RUNTIME_LIFECYCLE_LOCK = asyncio.Lock()
_LifecycleResult = TypeVar("_LifecycleResult")

_OBSERVER_CACHE_TTL_SEC = 15.0
_OBSERVER_ERROR_TTL_SEC = 0.2
# The observer builder (strict_epoch_projection + git scans, with the
# content-coherence bracket in _sync_evolution_fields that samples the strict
# projection up to three times to prove epoch/handoff/transition identity did
# not move) takes ~76s.  A same-key follower therefore cannot absorb that build
# inside the short HTTP retry window; instead it cooperatively awaits the
# in-flight build's shared result (see _OBSERVER_FOLLOWER_AWAIT_TIMEOUT_SEC).
# _OBSERVER_HTTP_RETRY_DELAY_SEC only needs to cover the narrow changed-key
# handoff race, where the in-flight build belongs to a superseded authority.
_OBSERVER_HTTP_RETRY_DELAY_SEC = 0.15
# A same-key follower waits up to this long for the single in-flight build to
# resolve, instead of failing fast with 503.  It is bounded comfortably above
# the measured ~76s build but well under the 300s nginx proxy_read_timeout, so a
# legitimate build finishes and populates the cache while a genuinely stuck one
# still surfaces as a retryable 503.
_OBSERVER_FOLLOWER_AWAIT_TIMEOUT_SEC = 90.0


class _ObserverProjectionUnavailable(RuntimeError):
    """A coherent observer projection is temporarily being refreshed."""

    def __init__(self, reason: str):
        self.reason = str(reason)
        super().__init__(self.reason)


class _ObserverSingleflightCache:
    """Bound duplicate GET projections without weakening fresh launch reads.

    ``stale_while_revalidate_sec`` is observer-only.  It may reuse a prior
    projection after the short fresh TTL only while the caller's exact local
    content key is unchanged.  One daemon refresh then runs in the background.
    A changed key never consumes stale bytes, and launch/mutation paths do not
    call this cache at all.
    """

    def __init__(
        self,
        ttl_sec: float = _OBSERVER_CACHE_TTL_SEC,
        *,
        stale_while_revalidate_sec: float = 0.0,
    ):
        self.ttl_sec = float(ttl_sec)
        self.stale_while_revalidate_sec = max(
            0.0,
            float(stale_while_revalidate_sec),
        )
        self._condition = threading.Condition()
        self._value: dict[str, Any] | None = None
        self._expires_at = 0.0
        self._error: str | None = None
        self._error_expires_at = 0.0
        self._inflight = False
        self._inflight_generation: int | None = None
        self._pending_builder: Callable[[], dict[str, Any]] | None = None
        self._generation = 0
        self._key: object = None

    def _start_pending_refresh_locked(self) -> None:
        """Hand the single refresh slot to the newest invalidated authority."""

        if self._inflight or self._pending_builder is None:
            return
        builder = self._pending_builder
        self._pending_builder = None
        generation = self._generation
        self._inflight = True
        self._inflight_generation = generation
        threading.Thread(
            target=self._complete_background_refresh,
            args=(builder,),
            kwargs={"generation": generation},
            name="pok-control-observer-refresh",
            daemon=True,
        ).start()

    def _complete_background_refresh(
        self,
        builder: Callable[[], dict[str, Any]],
        *,
        generation: int,
    ) -> None:
        try:
            built = builder()
            if not isinstance(built, dict):
                raise TypeError("observer snapshot builder did not return an object")
            frozen = copy.deepcopy(built)
        except BaseException as exc:
            with self._condition:
                if self._inflight_generation == generation:
                    self._inflight = False
                    self._inflight_generation = None
                if generation == self._generation:
                    if isinstance(exc, _ObserverProjectionUnavailable):
                        self._error = None
                        self._error_expires_at = 0.0
                    else:
                        self._error = (
                            f"observer_projection_failed:{type(exc).__name__}:"
                            f"{str(exc)[:160]}"
                        )
                        self._error_expires_at = (
                            time.monotonic() + _OBSERVER_ERROR_TTL_SEC
                        )
                    self._pending_builder = None
                else:
                    self._start_pending_refresh_locked()
                self._condition.notify_all()
            return
        with self._condition:
            if self._inflight_generation == generation:
                self._inflight = False
                self._inflight_generation = None
            if generation == self._generation:
                self._value = frozen
                self._expires_at = time.monotonic() + self.ttl_sec
                self._error = None
                self._error_expires_at = 0.0
                self._pending_builder = None
            else:
                self._start_pending_refresh_locked()
            self._condition.notify_all()

    def invalidate(self) -> None:
        with self._condition:
            self._generation += 1
            self._value = None
            self._expires_at = 0.0
            self._error = None
            self._error_expires_at = 0.0
            self._pending_builder = None
            self._key = None
            self._condition.notify_all()

    def get(
        self,
        builder: Callable[[], dict[str, Any]],
        *,
        key: object = None,
    ) -> dict[str, Any]:
        cached_value: dict[str, Any] | None = None
        while True:
            with self._condition:
                if self._key != key:
                    if self._inflight:
                        # The in-flight result belongs to the preceding local
                        # authority.  Invalidate it and hand the one refresh
                        # slot to the latest key after that builder exits.  A
                        # zero-stale cache (health) must be just as nonblocking
                        # as status: waiting here can pin an HTTP request behind
                        # a many-second signature/Git projection.
                        self._generation += 1
                        self._key = key
                        self._value = None
                        self._expires_at = 0.0
                        self._error = None
                        self._error_expires_at = 0.0
                        self._pending_builder = builder
                        raise _ObserverProjectionUnavailable(
                            "observer_projection_authority_changed_during_refresh"
                        )
                    self._generation += 1
                    self._key = key
                    self._value = None
                    self._expires_at = 0.0
                    self._error = None
                    self._error_expires_at = 0.0
                now = time.monotonic()
                if self._value is not None and now < self._expires_at:
                    cached_value = self._value
                    break
                stale_until = (
                    self._expires_at + self.stale_while_revalidate_sec
                )
                if self._value is not None and now < stale_until:
                    cached_value = self._value
                    if (
                        not self._inflight
                        and not (
                            self._error is not None
                            and now < self._error_expires_at
                        )
                    ):
                        self._inflight = True
                        generation = self._generation
                        self._inflight_generation = generation
                        threading.Thread(
                            target=self._complete_background_refresh,
                            args=(builder,),
                            kwargs={"generation": generation},
                            name="pok-control-observer-refresh",
                            daemon=True,
                        ).start()
                    break
                if self._error is not None and now < self._error_expires_at:
                    raise RuntimeError(self._error)
                if self._inflight:
                    inflight_generation = self._inflight_generation
                    if inflight_generation != self._generation:
                        # The in-flight build belongs to a superseded authority
                        # (the key moved and the superseding build has not yet
                        # started).  Fail closed rather than await the wrong
                        # authority's bytes.
                        self._pending_builder = builder
                        raise _ObserverProjectionUnavailable(
                            "observer_projection_authority_changed_during_refresh"
                        )
                    # Same-key follower: cooperatively await the single in-flight
                    # build instead of failing fast.  This runs on the follower's
                    # own off-loop worker thread (one isolated
                    # ThreadPoolExecutor(max_workers=1) per HTTP request), so the
                    # ASGI event loop is never blocked.  The wait is bounded: a
                    # build that cannot complete within the follower window still
                    # surfaces as a retryable 503.
                    self._pending_builder = builder
                    self._condition.wait(
                        timeout=_OBSERVER_FOLLOWER_AWAIT_TIMEOUT_SEC
                    )
                    now = time.monotonic()
                    if self._inflight:
                        # The build never resolved within the follower window.
                        raise _ObserverProjectionUnavailable(
                            "observer_projection_refresh_in_progress"
                        )
                    if (
                        inflight_generation == self._generation
                        and self._value is not None
                        and now < self._expires_at
                    ):
                        # The awaited build completed and is still fresh under
                        # the same authority (generation unchanged).  Serve it.
                        cached_value = self._value
                        break
                    # The authority moved (key/generation changed) before this
                    # follower observed the result, or the result was evicted.
                    # Fail closed: never serve a value that does not match the
                    # caller's requested key/generation.
                    raise _ObserverProjectionUnavailable(
                        "observer_projection_authority_changed_during_refresh"
                    )
                self._inflight = True
                generation = self._generation
                self._inflight_generation = generation
                break
        if cached_value is not None:
            # Copy outside the condition: status payloads are bounded, but a
            # deep copy must not serialize unrelated cache readers.
            return copy.deepcopy(cached_value)
        try:
            built = builder()
            if not isinstance(built, dict):
                raise TypeError("observer snapshot builder did not return an object")
            frozen = copy.deepcopy(built)
        except BaseException as exc:
            with self._condition:
                if self._inflight_generation == generation:
                    self._inflight = False
                    self._inflight_generation = None
                if generation == self._generation:
                    if isinstance(exc, _ObserverProjectionUnavailable):
                        self._error = None
                        self._error_expires_at = 0.0
                    else:
                        self._error = (
                            f"observer_projection_failed:{type(exc).__name__}:"
                            f"{str(exc)[:160]}"
                        )
                        self._error_expires_at = (
                            time.monotonic() + _OBSERVER_ERROR_TTL_SEC
                        )
                    self._pending_builder = None
                else:
                    self._start_pending_refresh_locked()
                self._condition.notify_all()
            raise
        with self._condition:
            if self._inflight_generation == generation:
                self._inflight = False
                self._inflight_generation = None
            still_current = generation == self._generation
            if still_current:
                self._value = frozen
                self._expires_at = time.monotonic() + self.ttl_sec
                self._error = None
                self._error_expires_at = 0.0
                self._pending_builder = None
            else:
                self._start_pending_refresh_locked()
            self._condition.notify_all()
        if not still_current:
            raise _ObserverProjectionUnavailable(
                "observer_projection_invalidated_during_build"
            )
        return copy.deepcopy(frozen)


_OBSERVER_STATUS_CACHE = _ObserverSingleflightCache(
    stale_while_revalidate_sec=60.0,
)
_OBSERVER_HEALTH_CACHE = _ObserverSingleflightCache()


def _invalidate_observer_projection_cache() -> None:
    _OBSERVER_STATUS_CACHE.invalidate()
    _OBSERVER_HEALTH_CACHE.invalidate()


def _observer_path_token(path: str | Path) -> tuple:
    """Return a no-follow local content token without opening the payload."""

    candidate = Path(path)
    try:
        value = candidate.lstat()
    except (FileNotFoundError, OSError):
        return (str(candidate), None)
    return (
        str(candidate),
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
    )


def _observer_authority_content_key() -> tuple:
    """Cheap local invalidation key for bounded stale observer snapshots.

    Remote publication freshness is deliberately absent: refreshing that proof
    is the slow operation this observer hides.  Local tags, checkpoint/reset,
    abandon/reap ledgers, handoff rows, published artifact directories and
    certificates are all included.  Any local authority movement therefore
    rejects stale bytes immediately; launch still performs a new complete
    remote proof through ``_fresh_control_status_snapshot``.
    """

    import evolution_infra as infra
    from epoch_authority import RUNTIME_RECONCILIATION_CLAIM_FILENAME
    from system_strict_bootstrap import POLICY_EPOCH_RESET_RECEIPT_FILENAME

    try:
        namespace = infra.version_namespace_authority()
        namespace_token: tuple = (
            "namespace",
            namespace.high_water,
            namespace.paired_versions,
            namespace.paired_commits,
            namespace.unpaired_completion_versions,
            namespace.unpaired_high_water_versions,
        )
        published_versions = tuple(namespace.paired_versions)
    except Exception as exc:
        namespace_token = (
            "namespace_unavailable",
            type(exc).__name__,
        )
        published_versions = ()

    results_paths = (
        infra.PIPELINE_STATE_FILE,
        infra.ABANDONED_VERSIONS_FILE,
        infra.REAPED_BOTS_FILE,
        Path(infra.RESULTS_DIR) / POLICY_EPOCH_RESET_RECEIPT_FILENAME,
        Path(infra.RESULTS_DIR) / RUNTIME_RECONCILIATION_CLAIM_FILENAME,
        Path(infra.RESULTS_DIR) / "stability_observation.json",
        infra.POST_PUBLICATION_HANDOFF_DIR,
    )
    from bot_namespace import bot_name

    publication_paths: list[Path] = []
    project_root = Path(infra.PROJECT_ROOT)
    for version in published_versions:
        bot_dir = Path(infra.BOTS_DIR) / bot_name(version)
        publication_paths.extend((
            bot_dir,
            bot_dir / ".completed",
            bot_dir / "national_bot.py",
            bot_dir / "precompute.py",
            bot_dir / "policy.py",
            bot_dir / "national_runtime_manifest.json",
            bot_dir / "policy_epoch_receipt.json",
            project_root / "official_certificates" / f"{bot_name(version)}.json",
        ))
    return (
        namespace_token,
        tuple(_observer_path_token(path) for path in results_paths),
        tuple(_observer_path_token(path) for path in publication_paths),
    )


def _observer_cache_key(*, health: bool = False) -> tuple:
    """Cheap process-local invalidation key; durable state still uses TTL."""

    from epoch_authority import strict_epoch_projection

    state = app_state.to_dict()
    task = app_state.task_snapshot()
    key = (
        strict_epoch_projection,
        _sync_evolution_fields,
        state.get("running"),
        state.get("daemon_enabled"),
        state.get("daemon_workers"),
        state.get("daemon_pairs"),
        task.get("present"),
        task.get("done"),
        task.get("shutdown_requested"),
        task.get("owner_id"),
        task.get("lifecycle_revision"),
        _observer_authority_content_key(),
    )
    if health:
        return (*key, _daemon_health_snapshot, _read_pipeline_health)
    return key


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
        "id": "abandon_active_generation",
        "method": "POST",
        "path": "/api/control/abandon",
        "mutation": True,
        # An abandon only makes sense with an initialized epoch that owns the
        # active checkpoint; do not advertise it as a fallback when the epoch
        # authority itself is unavailable.
        "requires_epoch": True,
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
            "blocked": True,
            "error": "canonical_epoch_projection_unavailable",
        }

    active = status.get("active_generation")
    handoff = status.get("post_publication_handoff")
    if isinstance(handoff, dict) and handoff.get("status") != "none":
        conflict = isinstance(active, dict)
        operator_action = status.get("operator_action")
        operator_blocked = bool(operator_action)
        ignored_checkpoint = status.get("ignored_checkpoint")
        ignored_blocked = isinstance(ignored_checkpoint, dict)
        epoch_blocked = not bool(status.get("epoch_initialized"))
        handoff_state = str(handoff.get("state") or "")
        owner_scope = str(handoff.get("owner_scope") or "unknown")
        task = app_state.task_snapshot()
        runtime_owner_id = app_state.runtime_owner_id()
        current_runtime_owner = bool(
            status.get("running") is True
            and task.get("present") is True
            and task.get("done") is False
            and isinstance(runtime_owner_id, str)
            and bool(runtime_owner_id)
            and task.get("owner_id") == runtime_owner_id
        )
        owner_blocked = False
        owner_issue = None
        if handoff_state == "running":
            if owner_scope == "foreign_process":
                owner_blocked = True
                owner_issue = "post_publication_handoff_foreign_owner_active"
            elif owner_scope == "current_process":
                if not current_runtime_owner:
                    owner_blocked = True
                    owner_issue = (
                        "post_publication_handoff_current_owner_unbound"
                    )
            else:
                owner_blocked = True
                owner_issue = "post_publication_handoff_owner_unknown"
        elif handoff_state == "pending" and owner_scope != "none":
            owner_blocked = True
            owner_issue = "post_publication_handoff_owner_scope_invalid"
        blocked = (
            bool(handoff.get("blocked"))
            or conflict
            or operator_blocked
            or ignored_blocked
            or epoch_blocked
            or owner_blocked
        )
        issues = list(handoff.get("issues") or [])
        if conflict:
            issues.append("active_generation_and_handoff_overlap")
        if operator_blocked:
            issues.append("operator_action_required")
        if ignored_blocked:
            issues.append("ignored_checkpoint_requires_recovery")
        if epoch_blocked:
            issues.append("policy_epoch_not_initialized")
        if owner_issue:
            issues.append(owner_issue)
        return {
            "exists": True,
            "stage": "post_publication_handoff",
            "authority": "post_publication_handoff_journal",
            "epoch_state": status.get("epoch_state"),
            "blocked": blocked,
            "operator_action_required": operator_blocked,
            "operator_action": operator_action,
            "ignored_checkpoint": (
                ignored_checkpoint if ignored_blocked else None
            ),
            "issues": list(dict.fromkeys(issues)),
            "next_v": handoff.get("version"),
            "source_v": handoff.get("source_v"),
            "workflow_run_id": handoff.get("workflow_run_id"),
            "checkpoint_revision": handoff.get("record_revision"),
            "handoff_identity_digest": handoff.get("identity_digest"),
            "handoff_projection_digest": handoff.get("projection_digest"),
            "publication_id": handoff.get("publication_id"),
            "handoff_owner_scope": owner_scope,
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
        ignored_present = isinstance(ignored, dict)
        operator_action = status.get("operator_action")
        operator_blocked = bool(operator_action)
        blocked = (
            not bool(status.get("epoch_initialized"))
            or ignored_present
            or operator_blocked
        )
        issues = []
        if ignored_present:
            issues.append("ignored_checkpoint_requires_recovery")
        if operator_blocked:
            issues.append("operator_action_required")
        return {
            "exists": False,
            "stage": None,
            "authority": authority,
            "epoch_state": status.get("epoch_state"),
            "blocked": blocked,
            "issues": issues,
            "operator_action_required": operator_blocked,
            "operator_action": operator_action,
            "ignored_checkpoint": ignored if isinstance(ignored, dict) else None,
            "scheduler_boundary": None if blocked else {
                "authority": "outer_scheduler",
                "state": "ready_to_prepare",
                "provider_action": "end_stream",
                "scheduler_action": "prepare_generation",
                "next_v": status.get("next_v"),
                # Parent/source selection belongs to prepare_generation and is
                # not derivable from current_v once a strict pool exists.
                "source_v": None,
            },
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
            "parent2_v",
            "stage",
            "run_id",
            "workflow_run_id",
            "checkpoint_revision",
        )
        expected_identity = {
            "next_v": active.get("next_v"),
            "source_v": active.get("source_v"),
            "parent2_v": active.get("parent2_v"),
            "stage": active.get("stage"),
            "run_id": active.get("run_id"),
            "workflow_run_id": active.get("workflow_run_id"),
            "checkpoint_revision": active.get("checkpoint_revision"),
        }
        observed_identity = {
            "next_v": checkpoint_obj.get("next_v"),
            "source_v": checkpoint_obj.get("source_v"),
            "parent2_v": checkpoint_obj.get("parent2_v"),
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
                "blocked": True,
                "error": "strict_checkpoint_revalidation_failed",
                "issues": list(dict.fromkeys(map(str, issues))),
                "identity_changed": bool(identity_mismatches),
                "identity_mismatches": identity_mismatches,
                "expected_identity": expected_identity,
                "observed_identity": observed_identity,
            }
        recovery = checkpoint_recovery_diagnostics(checkpoint)
        recovery_blocked = recovery.get("recoverable") is not True
        operator_action = status.get("operator_action")
        operator_blocked = bool(operator_action)
        route = None
        if not recovery_blocked and not operator_blocked:
            route = route_policy(checkpoint)
            if not isinstance(route, dict):
                raise RuntimeError("canonical route is not an object")
    except Exception as exc:
        return {
            "exists": True,
            "stage": active.get("stage"),
            "authority": authority,
            "epoch_state": status.get("epoch_state"),
            "blocked": True,
            "error": f"strict_checkpoint_diagnostic_failed:{type(exc).__name__}",
        }

    attempt = active.get("attempt") if isinstance(active.get("attempt"), dict) else {}
    route_operator_reconcile = bool(
        isinstance(route, dict)
        and route.get("intent") == "operator_reconcile_checkpoint"
    )
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
        "parent2_v": active.get("parent2_v"),
        "generation_attempt": attempt.get("generation"),
        "audit_attempt": attempt.get("audit"),
        "precommit_attempt": attempt.get("precommit"),
        "ignored_checkpoint": None,
        "recovery": recovery,
        "recovery_blocked": recovery_blocked or route_operator_reconcile,
        "blocked": False,
        "operator_action_required": False,
    }
    if recovery_blocked or operator_blocked or route_operator_reconcile:
        snapshot["blocked"] = True
        snapshot["operator_action_required"] = operator_blocked
        snapshot["operator_action"] = operator_action
        snapshot["route"] = None
        blocked_issues = list(recovery.get("issues") or [])
        if recovery_blocked and not blocked_issues:
            blocked_issues.append("checkpoint_recovery_not_proven")
        if operator_blocked:
            blocked_issues.append("operator_action_required")
        if route_operator_reconcile:
            blocked_issues.extend(str(item) for item in (route.get("issues") or []))
            blocked_issues.append("terminal_gate_outcome_requires_operator_reconciliation")
            snapshot["error"] = "terminal_gate_outcome_invalid"
        snapshot["issues"] = list(dict.fromkeys(map(str, blocked_issues)))
        snapshot["route"] = route
    else:
        snapshot["route"] = route
        if route.get("intent") == "terminal_gate_abandon":
            outcome = checkpoint.get("terminal_gate_outcome") or {}
            if (
                isinstance(outcome, dict)
                and outcome.get("receipt_digest")
                == route.get("terminal_gate_outcome_digest")
            ):
                snapshot["admission_blocked"] = True
                snapshot["terminalization_pending"] = True
                snapshot["gate_outcome"] = {
                    key: outcome.get(key)
                    for key in (
                        "schema_version",
                        "kind",
                        "gate_name",
                        "terminal_stage",
                        "reason_code",
                        "failure_class",
                        "disposition",
                        "receipt_digest",
                    )
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
    if pipeline.get("admission_blocked") is True:
        issues.append("pipeline_admission_blocked_terminalization_pending")
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


def _dynamic_first_strict_operator_transition(epoch: dict) -> dict | None:
    """Read the durable bootstrap job through certification's strict reader."""

    try:
        from server.routes.certification import (
            operator_transition_for_epoch_projection,
        )

        return operator_transition_for_epoch_projection(epoch)
    except Exception:
        return None


def _operator_transition_matches_active(
    transition: dict | None,
    active: dict | None,
    base_transition: dict | None,
) -> bool:
    """Accept a dynamic transition only for the exact stable checkpoint."""

    if not all(isinstance(value, dict) for value in (
        transition,
        active,
        base_transition,
    )):
        return False
    assert isinstance(transition, dict)
    assert isinstance(active, dict)
    assert isinstance(base_transition, dict)
    digest = transition.get("transition_digest")
    try:
        encoded = json.dumps(
            {key: value for key, value in transition.items() if key != "transition_digest"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        return False
    if (
        not isinstance(digest, str)
        or hashlib.sha256(encoded).hexdigest() != digest
        or transition.get("schema_version") != 1
        or transition.get("kind")
        != "first-strict-official-operator-transition"
        or transition.get("state") not in {
            "bootstrap_required",
            "bootstrap_running",
            "bootstrap_failed",
            "ready_to_finalize",
        }
        or transition.get("certification_profile") != "first_strict_control_v1"
        or transition.get("opponent_authority") != "system_control"
        or transition.get("strength_evidence_weight") != 0
        or transition.get("strategy_evidence_weight") != 0
        or transition.get("evaluation_epoch") != "national_tcp_policy_v1"
        or transition.get("workflow_run_id") != active.get("workflow_run_id")
        or transition.get("candidate_version") != active.get("next_v")
        or transition.get("source_v") != active.get("source_v")
        or transition.get("checkpoint_stage") != active.get("stage")
        or transition.get("checkpoint_revision")
        != active.get("checkpoint_revision")
        or transition.get("candidate_hash")
        != base_transition.get("candidate_hash")
        or transition.get("parked_request_digest")
        != base_transition.get("parked_request_digest")
    ):
        return False
    def hex64(value: object) -> bool:
        return bool(
            isinstance(value, str)
            and len(value) == 64
            and all(char in "0123456789abcdef" for char in value)
        )
    state = transition["state"]
    if state == "bootstrap_required":
        return transition.get("job_id") is None and transition.get("certificate_digest") is None
    if state == "bootstrap_failed":
        return (
            transition.get("certificate_digest") is None
            and (
                transition.get("job_id") is None
                or hex64(transition.get("job_id"))
            )
        )
    if not hex64(transition.get("job_id")):
        return False
    if state == "ready_to_finalize":
        return hex64(transition.get("certificate_digest"))
    return transition.get("certificate_digest") is None


def _epoch_transition_identity(epoch: dict) -> tuple:
    active = epoch.get("active_generation")
    active = active if isinstance(active, dict) else {}
    return (
        epoch.get("evaluation_epoch"),
        epoch.get("state"),
        epoch.get("initialized"),
        epoch.get("reset_receipt_digest"),
        *(active.get(field) for field in (
            "next_v",
            "source_v",
            "parent2_v",
            "stage",
            "run_id",
            "workflow_run_id",
            "checkpoint_revision",
        )),
    )


def _refined_operator_transition(
    epoch: dict,
    *,
    resample: Callable[[], dict],
) -> dict | None:
    """Return a dynamic exact-job transition or the epoch-owned baseline."""

    base = epoch.get("operator_transition")
    active = epoch.get("active_generation")
    if (
        not isinstance(active, dict)
        or active.get("stage") != "official_bootstrap_required"
    ):
        return base if isinstance(base, dict) else None
    candidate = _dynamic_first_strict_operator_transition(epoch)
    if not _operator_transition_matches_active(candidate, active, base):
        return base if isinstance(base, dict) else None
    after = resample()
    if _epoch_transition_identity(after) != _epoch_transition_identity(epoch):
        return base if isinstance(base, dict) else None
    return candidate


def _sync_evolution_fields(
    state: dict,
    *,
    ledger_fresh: bool = True,
) -> dict:
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

        def load_epoch() -> dict[str, Any]:
            return strict_epoch_projection(ledger_fresh=ledger_fresh)

        epoch, handoff, stable_sample = stable_epoch_handoff_sample(
            load_epoch,
            lambda value: post_publication_handoff_projection(
                enabled=bool(value.get("initialized"))
            ),
        )
        if not stable_sample:
            raise RuntimeError(
                "canonical_epoch_changed_during_handoff_projection"
            )
        dynamic_transition = _refined_operator_transition(
            epoch,
            resample=load_epoch,
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
        state["strict_published_bot_identities"] = epoch[
            "strict_published_bot_identities"
        ]
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
        state["operator_transition"] = dynamic_transition
        state["ignored_checkpoint"] = epoch["ignored_checkpoint"]
        state["unpublished_candidate_versions"] = unpublished_candidate_versions(
            ledger_fresh=ledger_fresh,
        )
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
            "strict_published_bot_identities": [],
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
    daemon_pairs: int | None = Field(default=None, ge=1, le=MAX_DAEMON_PAIRS)

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
    await run_blocking_isolated(
        _require_initialized_epoch,
        "control_config_update",
        thread_name_prefix="control-config-epoch-authority",
    )
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
    _invalidate_observer_projection_cache()
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
        _invalidate_observer_projection_cache()
        raise HTTPException(
            status_code=500,
            detail={
                "code": "runtime_configuration_transaction_failed",
                "failure": f"{type(exc).__name__}:{str(exc)[:240]}",
                "rollback_errors": rollback_errors,
            },
        ) from None
    _invalidate_observer_projection_cache()
    return result


@router.put("/config")
async def set_config(req: ConfigRequest, request: Request):
    require_operator_mutation(request, operation="control_config_update")
    return await _run_lifecycle_operation(
        lambda: _set_config_transaction(req)
    )


def _fresh_control_status_snapshot() -> dict[str, Any]:
    """Return a fresh authority sample for launch and mutation admission."""

    return _sync_evolution_fields(
        app_state.to_dict(),
        ledger_fresh=True,
    )


def _observer_control_status_snapshot() -> dict[str, Any]:
    """Return a content-bound cached-ledger sample for read-only HTTP views."""

    return _sync_evolution_fields(
        app_state.to_dict(),
        ledger_fresh=False,
    )


def _control_status_snapshot() -> dict[str, Any]:
    """Short-lived singleflight snapshot for read-only HTTP observers."""

    return _OBSERVER_STATUS_CACHE.get(
        _observer_control_status_snapshot,
        key=_observer_cache_key(),
    )


async def _run_control_observer_http_snapshot(
    builder: Callable[[], dict[str, Any]],
    *,
    thread_name_prefix: str,
) -> dict[str, Any]:
    """Absorb a narrow refresh race, then expose coherent unavailability.

    Local checkpoint/tag movement is expected while evolution runs.  It must
    invalidate the preceding observer projection, but it is not an internal
    server error.  A second off-loop sample absorbs refreshes which complete
    immediately; a still-running refresh is reported as retryable 503 without
    ever returning the old authority under the new content key.
    """

    failure: _ObserverProjectionUnavailable | None = None
    for attempt in range(2):
        try:
            return await run_blocking_isolated(
                builder,
                thread_name_prefix=thread_name_prefix,
            )
        except _ObserverProjectionUnavailable as exc:
            failure = exc
            if attempt == 0:
                await asyncio.sleep(_OBSERVER_HTTP_RETRY_DELAY_SEC)
    assert failure is not None
    raise HTTPException(
        status_code=503,
        detail={
            "code": "observer_projection_refreshing",
            "reason": failure.reason,
            "retryable": True,
            "authority": "strict_epoch_projection",
        },
        headers={"Retry-After": "1"},
    ) from None


def control_observer_epoch_projection() -> dict[str, Any]:
    """Return the epoch slice of the shared read-only control observation.

    Dashboard readers which only need publication/stream identity must share
    the same content-keyed singleflight as ``/control/status``. Reopening the
    complete strict projection for every SSE event also reopens each signed
    official verdict ledger and can stall the ASGI loop while certification
    holds those locks. Mutation and launch code deliberately do not call this
    helper; their fresh barriers remain in ``_fresh_control_status_snapshot``.
    """

    status = _control_status_snapshot()
    initialized = status.get("epoch_initialized") is True
    state = str(status.get("epoch_state") or "epoch_authority_unavailable")
    return {
        "evaluation_epoch": str(
            status.get("evaluation_epoch") or "national_tcp_policy_v1"
        ),
        # Strict-style fields are consumed by existing projection helpers.
        "state": state,
        "initialized": initialized,
        "reset_receipt_valid": status.get("reset_receipt_valid") is True,
        "reset_receipt_digest": status.get("reset_receipt_digest"),
        "version_authority_high_water": status.get(
            "version_authority_high_water"
        ),
        "active_bots": list(status.get("active_bots") or []),
        "strict_published_bot_identities": list(
            status.get("strict_published_bot_identities") or []
        ),
        # Public data-stream aliases remain backwards compatible.
        "epoch_state": state,
        "epoch_initialized": initialized,
        "epoch_reset_receipt_digest": status.get("reset_receipt_digest"),
        "stream_authority_digest": status.get("stream_authority_digest"),
    }


def _control_launch_authority_snapshot() -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the canonical status and live-revalidated launch barrier."""

    # Launch is a mutation boundary: it must never consume observer cache.
    status = _fresh_control_status_snapshot()
    return status, _read_pipeline_health(status)


def _runtime_launch_barrier_snapshot() -> dict[str, Any]:
    """Build one content-bound launch decision shared by HTTP and lifespan."""

    status, pipeline = _control_launch_authority_snapshot()
    recovery = (
        pipeline.get("recovery")
        if isinstance(pipeline.get("recovery"), dict)
        else {}
    )
    active = status.get("active_generation")
    handoff = status.get("post_publication_handoff")
    handoff = handoff if isinstance(handoff, dict) else {"status": "none"}
    scheduler = pipeline.get("scheduler_boundary")
    scheduler = scheduler if isinstance(scheduler, dict) else None
    route = pipeline.get("route")
    route = route if isinstance(route, dict) else None

    denial_code = None
    issues: list[str] = []
    if status.get("epoch_initialized") is not True:
        denial_code = "policy_epoch_not_initialized"
        issues.extend(status.get("reset_receipt_issues") or [])
    elif status.get("operator_action"):
        denial_code = "operator_action_required"
        issues.append("operator_action_required")
    elif (
        pipeline.get("blocked") is True
        or recovery.get("recoverable") is False
        or pipeline.get("error")
    ):
        denial_code = "pipeline_recovery_blocked"
        issues.extend(pipeline.get("issues") or [])
        issues.extend(recovery.get("issues") or [])
        if pipeline.get("error"):
            issues.append(str(pipeline["error"]))
    elif isinstance(active, dict):
        if pipeline.get("exists") is not True or route is None:
            denial_code = "pipeline_launch_boundary_invalid"
            issues.append("active_generation_route_not_proven")
    elif handoff.get("status") != "none":
        if pipeline.get("exists") is not True or route is None:
            denial_code = "pipeline_launch_boundary_invalid"
            issues.append("post_publication_handoff_route_not_proven")
    elif (
        scheduler is None
        or scheduler.get("authority") != "outer_scheduler"
        or scheduler.get("state") != "ready_to_prepare"
        or scheduler.get("provider_action") != "end_stream"
        or scheduler.get("scheduler_action") != "prepare_generation"
        or scheduler.get("next_v") != status.get("next_v")
        or scheduler.get("source_v") is not None
    ):
        denial_code = "pipeline_launch_boundary_invalid"
        issues.append("scheduler_boundary_not_proven")

    fence_material = {
        "schema_version": 1,
        "evaluation_epoch": status.get("evaluation_epoch"),
        "stream_authority_digest": status.get("stream_authority_digest"),
        "epoch_state": status.get("epoch_state"),
        "epoch_initialized": status.get("epoch_initialized"),
        "operator_action": status.get("operator_action"),
        "ignored_checkpoint": status.get("ignored_checkpoint"),
        "active_generation": active,
        "post_publication_handoff": {
            key: handoff.get(key)
            for key in (
                "status",
                "state",
                "blocked",
                "version",
                "source_v",
                "workflow_run_id",
                "identity_digest",
                "projection_digest",
                "record_revision",
                "owner_scope",
            )
        },
        "pipeline": {
            key: pipeline.get(key)
            for key in (
                "exists",
                "authority",
                "blocked",
                "error",
                "stage",
                "next_v",
                "source_v",
                "parent2_v",
                "run_id",
                "workflow_run_id",
                "checkpoint_revision",
                "handoff_identity_digest",
                "handoff_projection_digest",
                "handoff_owner_scope",
                "admission_blocked",
                "terminalization_pending",
                "gate_outcome",
            )
        },
        "recovery": {
            "active": recovery.get("active"),
            "recoverable": recovery.get("recoverable"),
            "issues": list(recovery.get("issues") or []),
        },
        "route": route,
        "scheduler_boundary": scheduler,
    }
    try:
        fence_bytes = json.dumps(
            fence_material,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        denial_code = denial_code or "pipeline_launch_boundary_invalid"
        issues.append("launch_fence_not_canonical_json")
        fence_bytes = b"invalid-launch-fence"
    fence_digest = hashlib.sha256(fence_bytes).hexdigest()
    return {
        "allowed": denial_code is None,
        "denial_code": denial_code,
        "issues": list(dict.fromkeys(map(str, issues))),
        "fence_digest": fence_digest,
        "status": status,
        "pipeline": pipeline,
    }


async def _reserve_runtime_launch_owner() -> dict[str, Any]:
    """Fence launch authority across the atomic runtime-owner reservation."""

    before = await run_blocking_isolated(
        _runtime_launch_barrier_snapshot,
        thread_name_prefix="runtime-launch-before-owner",
    )
    if before.get("allowed") is not True:
        return {"acquired": False, "reason": "barrier", "barrier": before}
    owner_id = app_state.begin_runtime_owner()
    if owner_id is None:
        return {
            "acquired": False,
            "reason": "already_owned",
            "barrier": before,
        }
    _invalidate_observer_projection_cache()
    try:
        after = await run_blocking_isolated(
            _runtime_launch_barrier_snapshot,
            thread_name_prefix="runtime-launch-after-owner",
        )
    except BaseException:
        # The reservation exists before the second authority sample starts.
        # Cancellation and ordinary sampling failures must release that exact
        # owner before propagating; otherwise AppState remains running with no
        # task and every later launch is permanently rejected as already owned.
        app_state.abort_runtime_owner(owner_id)
        _invalidate_observer_projection_cache()
        raise
    if (
        after.get("allowed") is not True
        or after.get("fence_digest") != before.get("fence_digest")
    ):
        app_state.abort_runtime_owner(owner_id)
        _invalidate_observer_projection_cache()
        if after.get("allowed") is True:
            after = {
                **after,
                "allowed": False,
                "denial_code": "launch_authority_changed",
                "issues": [
                    *list(after.get("issues") or []),
                    "launch_authority_changed_during_owner_reservation",
                ],
            }
        return {
            "acquired": False,
            "reason": "authority_changed",
            "barrier": after,
        }
    return {
        "acquired": True,
        "reason": "acquired",
        "owner_id": owner_id,
        "barrier": after,
    }


@router.get("/status")
async def control_status():
    return await _run_control_observer_http_snapshot(
        _control_status_snapshot,
        thread_name_prefix="control-status-snapshot",
    )


def _fresh_control_health_snapshot() -> dict[str, Any]:
    # Reuse the exact coalesced observer status authority. This does not alter
    # the launch barrier, which calls _fresh_control_status_snapshot directly.
    status = _control_status_snapshot()
    return _health_summary(status)


def _control_health_snapshot() -> dict[str, Any]:
    return _OBSERVER_HEALTH_CACHE.get(
        _fresh_control_health_snapshot,
        key=_observer_cache_key(health=True),
    )


@router.get("/health")
async def control_health():
    """Return a single read-only health snapshot for observers/supervisors."""
    return await _run_control_observer_http_snapshot(
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
    await run_blocking_isolated(
        _require_initialized_epoch,
        "control_start_evolution",
        thread_name_prefix="control-start-epoch-authority",
    )
    reservation = await _reserve_runtime_launch_owner()
    if reservation.get("acquired") is not True:
        if reservation.get("reason") == "already_owned":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "evolution_runtime_already_owned",
                    "task": app_state.task_snapshot(),
                },
            )
        barrier = reservation.get("barrier") or {}
        status = barrier.get("status") or {}
        pipeline = barrier.get("pipeline") or {}
        raise HTTPException(
            status_code=409,
            detail={
                "code": barrier.get("denial_code")
                or "pipeline_launch_boundary_invalid",
                "operation": "control_start_evolution",
                "operator_action": status.get("operator_action"),
                "operator_command": status.get("operator_command"),
                "epoch_state": status.get("epoch_state"),
                "stage": pipeline.get("stage"),
                "issues": list(barrier.get("issues") or []),
                "fence_digest": barrier.get("fence_digest"),
            },
        )
    owner_id = reservation.get("owner_id")
    task: asyncio.Task | None = None
    llm_shutdown_manager_bound = False
    try:
        # Enter the cleanup boundary immediately after owner acquisition.  Even
        # imports, broadcaster maintenance, and config reads are fallible; none
        # may strand a running owner with no attached task.
        from server.app import register_lifespan_runtime_owner, web_ui

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

        from shutdown_manager import ShutdownManager

        shutdown_mgr = ShutdownManager(grace_period=15.0)
        app_state.set_shutdown_mgr(shutdown_mgr, owner_id=owner_id)
        try:
            from llm_query import set_shutdown_manager
            llm_shutdown_manager_bound = bool(
                set_shutdown_manager(
                    shutdown_mgr,
                    owner_id=owner_id,
                )
            )
            if not llm_shutdown_manager_bound:
                raise RuntimeError(
                    "LLM shutdown manager owner fencing conflict"
                )
        except Exception:
            raise

        from orchestrator import orchestrator_loop
        task = asyncio.create_task(run_evolution_task(orchestrator_loop(
            web_ui, shutdown_mgr=shutdown_mgr, no_daemon=not config["daemon_enabled"],
            daemon_workers=config["daemon_workers"], daemon_pairs=config["daemon_pairs"]),
            owner_id=owner_id,
        ))
        app_state.set_task(task, owner_id=owner_id)
        _invalidate_observer_projection_cache()
        register_lifespan_runtime_owner(owner_id)
    except BaseException:
        if task is not None:
            task.cancel()
            try:
                await task
            except BaseException:
                pass
        if llm_shutdown_manager_bound:
            try:
                from llm_query import set_shutdown_manager
                set_shutdown_manager(None, owner_id=owner_id)
            except Exception:
                pass
        app_state.abort_runtime_owner(owner_id)
        _invalidate_observer_projection_cache()
        raise

    return {"status": "started", "mode": "orchestrator"}


@router.post("/start")
async def start_evolution(request: Request):
    require_operator_mutation(request, operation="control_start_evolution")
    return await _run_lifecycle_operation(_start_evolution_transaction)


async def _stop_evolution_transaction() -> dict[str, str]:
    _invalidate_observer_projection_cache()
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
        _invalidate_observer_projection_cache()
        raise HTTPException(
            status_code=500,
            detail={
                "code": "evolution_stop_incomplete",
                "failure": f"{type(exc).__name__}:{str(exc)[:240]}",
            },
        ) from None
    _invalidate_observer_projection_cache()
    return {"status": "stopped"}


@router.post("/stop")
async def stop_evolution(request: Request):
    require_operator_mutation(request, operation="control_stop_evolution")
    return await _run_lifecycle_operation(_stop_evolution_transaction)


class AbandonRequest(BaseModel):
    """Optional body for ``POST /api/control/abandon``.

    The abandon target is always the canonical active checkpoint; callers do
    not select a version. ``reason`` is an opaque operator annotation that
    travels into the durable abandon receipt (it defaults to the same
    ``abandon_generation`` reason the MCP tool uses when an operator drives
    cleanup manually).
    """

    model_config = {"extra": "ignore"}
    reason: str | None = Field(default=None, max_length=200)


def _abandonable_stage_block(checkpoint: dict | None, reason: str) -> dict | None:
    """Return the canonical generic-abandon refusal payload, or None if clear.

    Mirrors the exact guard ``_do_abandon_generation`` applies internally via
    ``_generic_abandon_stage_block`` so the HTTP layer can surface the same
    typed 409 (``stage_not_disposable``) before invoking irreversible
    publication-authority code.
    """

    from pipeline_state import generic_abandon_block

    return generic_abandon_block(checkpoint, reason=reason)


async def _abandon_generation_transaction(reason: str) -> dict[str, Any]:
    """Stop the live orchestrator inside the lifecycle lock, then abandon.

    Why Option B (stop-then-abandon within the lock) is the safe choice:

    ``_do_abandon_generation`` is publication-authority code: it acquires the
    bot publication lock, fences the actor journal, revalidates the checkpoint
    CAS, quarantines the candidate, and clears the checkpoint by exact CAS.
    Running that against a checkpoint a live orchestrator is concurrently
    mutating would race the loop and could strand the workflow between an
    abandoned candidate and a half-cleared checkpoint.  ``/api/control/stop``
    already establishes the contract for tearing the task down gracefully
    inside ``_RUNTIME_LIFECYCLE_LOCK``; reusing it here serializes the
    abandon against any concurrent start/stop/config mutation and ensures the
    orchestrator is quiescent before irreversible cleanup runs.

    The service is left stopped; the operator restarts it via
    ``/api/control/start`` once they have inspected the abandon receipt.
    """

    # Invalidate the observer projection cache up-front so the snapshot a
    # browser polls after this returns reflects the post-stop state rather
    # than the pre-stop running projection.
    _invalidate_observer_projection_cache()

    # 1) Stop the orchestrator task (graceful, mirroring stop_evolution).
    app_state.request_shutdown()
    task = app_state.stop_running()
    if task is not None and not task.done():
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
        _invalidate_observer_projection_cache()
        raise HTTPException(
            status_code=500,
            detail={
                "code": "evolution_task_stop_timeout",
                "operation": "control_abandon_generation",
                "task": app_state.task_snapshot(),
            },
        )

    # 2) Read the canonical checkpoint from evolution_infra (the same reader
    #    the MCP abandon tool uses) and apply the disposable-stage guard. An
    #    absent or terminal-stage checkpoint has nothing to abandon.
    from evolution_infra import PIPELINE_STATE_FILE, read_pipeline_checkpoint

    checkpoint: dict | None = None
    try:
        if os.path.lexists(PIPELINE_STATE_FILE):
            checkpoint = read_pipeline_checkpoint()
    except Exception as exc:
        _invalidate_observer_projection_cache()
        raise HTTPException(
            status_code=500,
            detail={
                "code": "checkpoint_read_failed",
                "operation": "control_abandon_generation",
                "failure": f"{type(exc).__name__}:{str(exc)[:240]}",
            },
        ) from None

    if not isinstance(checkpoint, dict):
        _invalidate_observer_projection_cache()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "no_active_generation_to_abandon",
                "operation": "control_abandon_generation",
                "checkpoint_present": False,
                "directive": (
                    "No active pipeline checkpoint to abandon. The generation "
                    "either completed, was already abandoned, or never started."
                ),
            },
        ) from None

    stage = checkpoint.get("stage")
    block = _abandonable_stage_block(checkpoint, reason)
    if block is not None:
        _invalidate_observer_projection_cache()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "stage_not_disposable",
                "operation": "control_abandon_generation",
                "stage": stage,
                "next_v": checkpoint.get("next_v"),
                "source_v": checkpoint.get("source_v"),
                "block": block,
                "directive": block.get("directive"),
            },
        ) from None

    # 3) Invoke the canonical abandon transaction (the same code the
    #    orchestrator dispatches for ``abandon_generation``). Do not
    #    reimplement abandon logic here. ``_do_abandon_generation`` reads the
    #    checkpoint itself, revalidates the CAS under the publication lock,
    #    fences the actor journal, quarantines the candidate, clears the
    #    checkpoint by exact CAS, and writes the terminal receipt. It is a
    #    coroutine that manages its own locks, so it runs on the event loop
    #    rather than through ``run_blocking_isolated`` (which is reserved for
    #    blocking sync infrastructure calls and would return a coroutine
    #    object instead of awaiting it).
    #
    # A forced (non-generic) reason requires the caller to supply the
    # checkpoint CAS identity (expected_workflow_run_id / _next_v /
    # _source_v / _checkpoint_revision / _checkpoint_stage) so the canonical
    # transaction can prove it is abandoning exactly the candidate the
    # operator observed. The orchestrator's own worker-terminal-abandon path
    # (orchestrator.py:4271-4279) and cycle-timeout path (:3307-3316) do this
    # via ``expected_abandon_identity(checkpoint)``; mirror that exactly.
    from tool_bot_management import (
        _do_abandon_generation,
        expected_abandon_identity,
    )

    try:
        identity = expected_abandon_identity(checkpoint)
    except Exception as exc:
        _invalidate_observer_projection_cache()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "abandon_identity_incomplete",
                "operation": "control_abandon_generation",
                "reason": f"{type(exc).__name__}:{str(exc)[:240]}",
                "stage": stage,
                "next_v": checkpoint.get("next_v"),
            },
        ) from None

    try:
        result = await _do_abandon_generation(reason=reason, **identity)
    except HTTPException:
        _invalidate_observer_projection_cache()
        raise
    except Exception as exc:
        _invalidate_observer_projection_cache()
        # The canonical abandon surfaces most revalidation failures as a
        # dict result rather than an exception (e.g. CAS mismatch after the
        # workflow fence). Only true unexpected errors land here; expose the
        # typed reason without leaking internal tracebacks.
        raise HTTPException(
            status_code=500,
            detail={
                "code": "abandon_transaction_failed",
                "operation": "control_abandon_generation",
                "failure": f"{type(exc).__name__}:{str(exc)[:240]}",
                "stage": stage,
                "next_v": checkpoint.get("next_v"),
            },
        ) from None

    if not isinstance(result, dict):
        _invalidate_observer_projection_cache()
        raise HTTPException(
            status_code=500,
            detail={
                "code": "abandon_transaction_invalid_result",
                "operation": "control_abandon_generation",
                "stage": stage,
                "next_v": checkpoint.get("next_v"),
            },
        ) from None

    # 4) Translate the canonical abandon result into the HTTP contract. A
    #    ``abandoned: False`` result is a typed refusal from inside the
    #    canonical transaction (CAS mismatch, workflow fence failure, rate
    #    limit, ...); surface it as a 409 so operators can distinguish a
    #    refused-but-fenced outcome from a server error.
    _invalidate_observer_projection_cache()
    if result.get("abandoned") is not True:
        canonical_reason = str(result.get("reason") or "abandon_refused")
        if canonical_reason == "expected_checkpoint_identity_mismatch":
            http_code = "checkpoint_cas_mismatch"
        else:
            http_code = "abandon_refused"
        raise HTTPException(
            status_code=409,
            detail={
                "code": http_code,
                "operation": "control_abandon_generation",
                "canonical_reason": canonical_reason,
                "stage": stage,
                "next_v": checkpoint.get("next_v"),
                "result": result,
            },
        ) from None

    return {
        "status": "abandoned",
        "operation": "control_abandon_generation",
        "transaction_id": result.get("abandon_transaction_id"),
        "abandoned_v": result.get("abandoned_v"),
        "reason": result.get("reason") or reason,
        "cleared_checkpoint": result.get("cleared_checkpoint"),
        "removed_directory": result.get("removed_directory"),
        "abandon_receipt_digest": result.get("abandon_receipt_digest"),
        "finalize_receipt_digest": result.get("finalize_receipt_digest"),
        "workflow_fenced": result.get("workflow_fenced"),
        "workflow_run_id": result.get("workflow_run_id"),
        "runtime_stopped": True,
        "directive": (
            "Generation abandoned and runtime left stopped. Restart evolution "
            "via POST /api/control/start after inspecting the abandon receipt."
        ),
    }


@router.post("/abandon")
async def abandon_generation(request: Request):
    """Abandon the currently stuck generation.

    Operator-facing escape hatch for a generation stuck at an abandonable
    stage (e.g. ``workers_done`` / ``rework_running``) where the documented
    auto-abandon paths do not fire. Stops the live orchestrator inside the
    runtime lifecycle lock, then runs the canonical abandon transaction.
    The runtime is left stopped for the operator to restart.
    """
    require_operator_mutation(request, operation="control_abandon_generation")
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    try:
        req = AbandonRequest(**body)
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "abandon_request_invalid",
                "operation": "control_abandon_generation",
                "failure": f"{type(exc).__name__}:{str(exc)[:200]}",
            },
        ) from None
    reason = req.reason or "abandon_generation"
    return await _run_lifecycle_operation(
        lambda: _abandon_generation_transaction(reason)
    )


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
    epoch = await run_blocking_isolated(
        _epoch_access_state,
        "control_tools_catalog",
        thread_name_prefix="control-tools-epoch-authority",
    )
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
    epoch = await run_blocking_isolated(
        _epoch_access_state,
        "control_orchestrator_session_read",
        thread_name_prefix="control-session-epoch-authority",
    )
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
