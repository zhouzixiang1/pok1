"""Pipeline state endpoints — checkpoint, worker failures, and agent activity.

All endpoints here are read-only projections of the current strict national
TCP epoch.  They reuse ``load_strict_pipeline_checkpoint`` as the sole
checkpoint authority and never reopen ``pipeline_state.json`` or any write
path.  See ``docs/evolution-continuous-delivery-runbook.md`` for the contract
that the dashboard must not mix the three checkpoint shapes
(``/api/pipeline/checkpoint``, ``/api/control/health.pipeline`` and
``/api/evolution/state``).
"""

import copy
from collections import OrderedDict
import io
import json
import math
import os
import stat
import threading
import time
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, Query

from server.routes._helpers import (
    load_strict_pipeline_checkpoint,
    load_strict_strength_snapshot,
    read_strict_worker_failures,
)
from evaluation_bundle import (
    evaluation_cycle_lock,
    load_current_strict_evaluation_bundle,
)
from blocking_runtime import run_blocking_isolated


def _daemon_health_snapshot() -> dict:
    """Re-export the control-plane daemon snapshot so tests can patch it here.

    Imported lazily to avoid pulling the full control router at module import
    time; the function object is stable once ``server.routes.control`` is
    loaded by the app (conftest imports ``server.app`` first).
    """
    from server.routes.control import _daemon_health_snapshot as _snapshot

    return _snapshot()

PROJECT_ROOT = Path(__file__).resolve().parents[3]
RESULTS_DIR = PROJECT_ROOT / "web" / "core" / "results"
PIPELINE_STATE_FILE = RESULTS_DIR / "pipeline_state.json"
WORKER_FAILURES_FILE = RESULTS_DIR / "worker_failures.jsonl"

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])


class _BoundedObserverCache:
    """Tiny process-local singleflight cache for read-only dashboard views.

    The request key always includes cheap file identity while the stored entry
    also records the durable workflow/revision (or evaluation-cycle) identity
    returned by the validated builder.  This cache never participates in a
    mutation or launch decision; it only prevents several browser tabs from
    reopening the same multi-megabyte authority at once.
    """

    def __init__(self, *, ttl_sec: float = 0.75, max_entries: int = 4):
        self.ttl_sec = float(ttl_sec)
        self.max_entries = max(1, int(max_entries))
        self._condition = threading.Condition()
        self._entries: OrderedDict[tuple[object, object], tuple[float, dict]] = OrderedDict()
        self._request_index: dict[object, tuple[object, object]] = {}
        self._inflight: set[object] = set()

    def clear(self) -> None:
        with self._condition:
            self._entries.clear()
            self._request_index.clear()
            self._inflight.clear()
            self._condition.notify_all()

    def get(
        self,
        request_key: object,
        builder: Callable[[], tuple[object, dict]],
    ) -> dict:
        while True:
            with self._condition:
                now = time.monotonic()
                cache_key = self._request_index.get(request_key)
                cached = self._entries.get(cache_key) if cache_key is not None else None
                if cached is not None:
                    expires_at, value = cached
                    if now < expires_at:
                        self._entries.move_to_end(cache_key)
                        return copy.deepcopy(value)
                    self._entries.pop(cache_key, None)
                    self._request_index.pop(request_key, None)
                if request_key in self._inflight:
                    self._condition.wait()
                    continue
                self._inflight.add(request_key)
                break
        try:
            authority_key, built = builder()
            if not isinstance(built, dict):
                raise TypeError("observer builder returned a non-object")
            frozen = copy.deepcopy(built)
        except BaseException:
            with self._condition:
                self._inflight.discard(request_key)
                self._condition.notify_all()
            raise
        with self._condition:
            previous_key = self._request_index.get(request_key)
            if previous_key is not None:
                self._entries.pop(previous_key, None)
            cache_key = (request_key, authority_key)
            self._entries[cache_key] = (
                time.monotonic() + self.ttl_sec,
                frozen,
            )
            self._request_index[request_key] = cache_key
            self._entries.move_to_end(cache_key)
            while len(self._entries) > self.max_entries:
                evicted_key, _entry = self._entries.popitem(last=False)
                evicted_request_key = evicted_key[0]
                if self._request_index.get(evicted_request_key) == evicted_key:
                    self._request_index.pop(evicted_request_key, None)
            self._inflight.discard(request_key)
            self._condition.notify_all()
        return copy.deepcopy(frozen)


_AGENTS_OBSERVER_CACHE = _BoundedObserverCache()
_STRENGTH_OBSERVER_CACHE = _BoundedObserverCache(ttl_sec=5.0, max_entries=4)


def _path_stat_token(path: Path) -> tuple:
    """Return a cheap no-content observer invalidation token."""

    try:
        value = path.stat(follow_symlinks=False)
    except (FileNotFoundError, OSError):
        return (str(path), None)
    return (
        str(path),
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        stat.S_IFMT(value.st_mode),
    )


@router.get("/checkpoint")
async def pipeline_checkpoint():
    """Return current pipeline checkpoint (stage of in-progress generation)."""
    return await run_blocking_isolated(
        load_strict_pipeline_checkpoint,
        RESULTS_DIR,
        PIPELINE_STATE_FILE,
        thread_name_prefix="pipeline-checkpoint-observer",
    )


@router.get("/failures")
async def pipeline_failures(limit: int = Query(10, le=50)):
    """Return failures explicitly bound to the current strict workflow."""
    return await run_blocking_isolated(
        read_strict_worker_failures,
        WORKER_FAILURES_FILE,
        results_dir=RESULTS_DIR,
        checkpoint_path=PIPELINE_STATE_FILE,
        limit=limit,
        thread_name_prefix="pipeline-failures-observer",
    )


# ── Structured agent activity projection ────────────────────────────────────
#
# ``/agents`` is a typed, epoch-bound view of Master / Scouts / Workers /
# Reviewer / Critic / Orchestrator activity for the current strict workflow.
# It is derived only from the validated checkpoint and the already-filtered
# worker failures reader; it never reopens raw files or invents a second
# checkpoint interpretation.  Gate "completeness" is projected from the same
# gate_result fields the frontend uses (``criticAdvisoryComplete`` et al.),
# so the dashboard and this endpoint agree without either side re-deriving
# authority from a loose summary.


def _quality_complete(checkpoint: dict) -> bool:
    """Use the production quality gate, including profile/native receipts."""

    try:
        from tool_helpers import _quality_gate_ok

        return bool(_quality_gate_ok(checkpoint))
    except Exception:
        return False


def _review_complete(checkpoint: dict) -> bool:
    """Use the production Reviewer gate, including strict bootstrap receipt."""

    try:
        from tool_helpers import _review_gate_ok

        return bool(_review_gate_ok(checkpoint))
    except Exception:
        return False


def _critic_advisory_complete(checkpoint: dict) -> bool:
    """Use the production Critic execution gate; its verdict stays advisory."""

    try:
        from tool_helpers import _critic_gate_ok

        return bool(_critic_gate_ok(checkpoint))
    except Exception:
        return False


def _precommit_complete(checkpoint: dict) -> bool:
    """Use pipeline_state's active-profile/native-template reuse predicate."""

    try:
        from pipeline_state import _precommit_gate_matches_active_workflow

        return bool(_precommit_gate_matches_active_workflow(
            checkpoint.get("gate_results") or {}
        ))
    except Exception:
        return False


def _official_full_complete(checkpoint: dict) -> bool:
    """Reopen the signed certificate and its formal content-bound profile."""

    gates = checkpoint.get("gate_results")
    gate = gates.get("official_full") if isinstance(gates, dict) else None
    status = gate.get("status") if isinstance(gate, dict) else None
    version = checkpoint.get("next_v")
    if (
        not isinstance(gate, dict)
        or gate.get("passed") is not True
        or not isinstance(status, dict)
        or type(version) is not int
    ):
        return False
    digest = gate.get("certificate_digest")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(char not in "0123456789abcdef" for char in digest)
        or status.get("certificate_digest") != digest
    ):
        return False
    try:
        from bot_namespace import bot_name
        from official_certification import (
            official_certification_profile_projection,
            official_full_certified,
        )

        candidate = PROJECT_ROOT / "bots" / bot_name(version)
        profile = official_certification_profile_projection(status, candidate)
        return bool(
            official_full_certified(status, candidate)
            and isinstance(profile, dict)
            and profile
            and profile.get("strength_evidence_weight") == 0
            and profile.get("strategy_evidence_weight") == 0
            and (profile.get("formal_summary") or {}).get("rounds_run") == 8
            and (profile.get("formal_summary") or {}).get("passed_rounds") == 8
        )
    except Exception:
        return False


_GATE_FIELD_ALLOWLIST: dict[str, tuple[str, ...]] = {
    # Browser copy needs only these scalar verdict/identity hints.  Full test
    # receipts, subprocess output, status payloads and certificate bodies stay
    # behind the server-side authority helper and are never serialized.
    "quality": (
        "all_passed",
        "critical_scenarios_passed",
        "decision_pass_rate",
        "code_fingerprint",
        "workflow_profile_digest",
    ),
    "review": (
        "approved",
        "schema_valid",
        "llm_invoked",
        "reviewer_llm_executed",
        "llm_failed",
        "parse_failed",
        "quality_score",
        "receipt_digest",
    ),
    "critic": (
        "approved",
        "schema_valid",
        "llm_invoked",
        "critic_llm_executed",
        "llm_failed",
        "parse_failed",
        "advisory_approved",
        "advisory_score",
        "receipt_digest",
    ),
    "precommit_eval": (
        "passed",
        "attempt",
        "native_matches",
        "hands_per_match",
        "receipt_digest",
        "candidate_artifact_hash",
    ),
    "official_full": (
        "passed",
        "certificate_digest",
        "certification_profile",
        "opponent_authority",
        "strength_evidence_weight",
        "strategy_evidence_weight",
        "reused_existing_certificate",
    ),
}
_REPAIR_STAGES = frozenset({"repair_planned", "rework_running"})
_OFFICIAL_CERTIFICATION_STAGES = frozenset({
    "official_bootstrap_required",
    "official_certifying",
    "official_failed",
    "official_inconclusive",
})
_MAX_AGENT_TEXT = 256
_MAX_MASTER_ANALYSIS = 1_024
_MAX_AGENT_TASKS = 8
_MAX_TASK_TARGETS = 8
_MAX_AGENT_FAILURES = 10
_MAX_AGENT_RESPONSE_BYTES = 64 * 1024


def _bounded_text(value: object, *, limit: int = _MAX_AGENT_TEXT) -> str | None:
    if not isinstance(value, str):
        return None
    return value[: max(0, int(limit))]


def _bounded_scalar(value: object) -> object | None:
    """Keep JSON scalars only; nested receipts have no browser authority."""

    if value is None or isinstance(value, bool):
        return value
    if type(value) is int:
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value[:_MAX_AGENT_TEXT]
    return None


def _gate_summary(name: str, gate: dict) -> dict[str, object]:
    allowed = _GATE_FIELD_ALLOWLIST.get(name, ())
    return {
        key: _bounded_scalar(gate.get(key))
        for key in allowed
        if key in gate and _bounded_scalar(gate.get(key)) is not None
    }


def _gate_view(checkpoint: dict, name: str, *, complete_fn) -> dict | None:
    gate_results = checkpoint.get("gate_results")
    if not isinstance(gate_results, dict):
        return None
    gate = gate_results.get(name)
    if not isinstance(gate, dict) or not gate:
        return None
    historical_invalidated = checkpoint.get("stage") in _REPAIR_STAGES
    view = {
        "name": name,
        "present": True,
        # Once repair owns the candidate, every earlier gate belongs to the
        # pre-repair bytes.  Keeping it green would falsely claim current
        # compliance, so the observer explicitly invalidates it until rerun.
        "authority_state": (
            "historical_invalidated" if historical_invalidated else "current"
        ),
        "complete": False if historical_invalidated else bool(complete_fn(checkpoint)),
        "fields": _gate_summary(name, gate),
    }
    return view


def _worker_task_view(task: dict) -> dict:
    """Project one ``master_plan.tasks`` entry without re-deriving its schema."""
    if not isinstance(task, dict):
        return {}
    return {
        "worker_id": task.get("worker_id"),
        "role": _bounded_text(task.get("role")),
        "target_files": [
            value[:_MAX_AGENT_TEXT]
            for value in list(task.get("target_files") or [])[:_MAX_TASK_TARGETS]
            if isinstance(value, str)
        ],
        "difficulty": _bounded_text(task.get("difficulty")),
        "skill_layer": _bounded_text(task.get("skill_layer")),
        "behavior_hypothesis": _bounded_text(task.get("behavior_hypothesis")),
        "expected_diff_shape": _bounded_text(task.get("expected_diff_shape")),
        "merge_policy": _bounded_text(task.get("merge_policy")),
    }


def _master_view(checkpoint: dict) -> dict:
    plan = checkpoint.get("master_plan")
    plan_present = isinstance(plan, dict)
    tasks = []
    analysis = None
    if isinstance(plan, dict):
        raw_tasks = plan.get("tasks")
        if isinstance(raw_tasks, list):
            tasks = [
                _worker_task_view(t)
                for t in raw_tasks[:_MAX_AGENT_TASKS]
                if isinstance(t, dict)
            ]
        analysis = plan.get("analysis")
    stage = checkpoint.get("stage")
    completed_stages = {
        "master_planned",
        "workers_done",
        "quality_failed",
        "quality_rejected",
        "quality_passed",
        "review_rejected",
        "reviewed",
        "critic_rejected",
        "critic_checked",
        "precommit_failed",
        "repair_planned",
        "rework_running",
        "verified",
        "official_bootstrap_required",
        "official_certifying",
        "official_failed",
        "official_inconclusive",
        "publishing",
        "archived",
    }
    return {
        # Timeout leases replace the raw stage but do not erase a previously
        # committed plan.  Presence of the checkpoint-owned plan is therefore
        # the durable Master high-water; it must not regress to not_reached.
        "started": bool(
            plan_present or stage == "direction_audited" or stage in completed_stages
        ),
        "completed": plan_present,
        "plan_present": plan_present,
        "analysis": _bounded_text(analysis, limit=_MAX_MASTER_ANALYSIS),
        "tasks": tasks,
        "task_total": (
            len(plan.get("tasks"))
            if isinstance(plan, dict) and isinstance(plan.get("tasks"), list)
            else 0
        ),
        "tasks_truncated": (
            isinstance(plan, dict)
            and isinstance(plan.get("tasks"), list)
            and len(plan["tasks"]) > _MAX_AGENT_TASKS
        ),
    }


def _bounded_mapping(
    value: object,
    *,
    allowed: tuple[str, ...],
) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return {
        key: scalar
        for key in allowed
        if key in value
        and (scalar := _bounded_scalar(value.get(key))) is not None
    }


def _build_agents_projection(checkpoint: dict | None, failures: list[dict]) -> dict:
    if not isinstance(checkpoint, dict) or not checkpoint:
        return {
            "available": False,
            "reason": "no_strict_workflow",
            "evaluation_epoch": "national_tcp_policy_v1",
        }

    stage = checkpoint.get("stage")
    gate_results = checkpoint.get("gate_results") if isinstance(checkpoint.get("gate_results"), dict) else {}
    return {
        "available": True,
        "evaluation_epoch": checkpoint.get("evaluation_epoch", "national_tcp_policy_v1"),
        "workflow_run_id": checkpoint.get("workflow_run_id"),
        "run_id": checkpoint.get("run_id"),
        "next_v": checkpoint.get("next_v"),
        "source_v": checkpoint.get("source_v"),
        "parent2_v": checkpoint.get("parent2_v"),
        "checkpoint_revision": checkpoint.get("checkpoint_revision"),
        "stage": stage,
        "attempts": {
            "generation": int(checkpoint.get("generation_attempt") or 0),
            "audit": int(checkpoint.get("audit_attempt") or 0),
            "precommit": int(checkpoint.get("precommit_attempt") or 0),
        },
        "rework_counts": {
            "worker_failure": int(checkpoint.get("worker_failure_count") or 0),
            "precommit": int(checkpoint.get("precommit_rework_count") or 0),
            "official": int(checkpoint.get("official_rework_count") or 0),
        },
        "orchestrator": {
            # The orchestrator's live status comes from /api/control/health
            # (paired status/health observation).  Here we only expose the
            # checkpoint-bound scheduler hints so the dashboard cannot mix the
            # two authority shapes.
            "stage": stage,
            "reviewer_feedback": _bounded_text(checkpoint.get("reviewer_feedback")),
            "infra_failure": _bounded_mapping(
                checkpoint.get("infra_failure"),
                allowed=(
                    "schema_version",
                    "failure_class",
                    "component",
                    "code",
                    "operation",
                    "owner_tool",
                    "resume_stage",
                    "attempt",
                    "max_attempts",
                    "reason",
                    "retryable",
                    "exhausted",
                    "action",
                    "identity_digest",
                ),
            ),
            # Consumers must not poll the durable official-job endpoint during
            # ordinary planning/worker/quality stages.  This flag is derived
            # solely from the exact checkpoint stage rather than from a stale
            # gate result or browser-side stage guess.
            "official_jobs_polling_supported": stage in _OFFICIAL_CERTIFICATION_STAGES,
        },
        "master": _master_view(checkpoint),
        "direction_audit": _bounded_mapping(
            checkpoint.get("direction_audit"),
            allowed=(
                "schema_version",
                "status",
                "approved",
                "direction",
                "reason",
                "attempt",
                "receipt_digest",
            ),
        ),
        "gates": {
            "quality": _gate_view(checkpoint, "quality", complete_fn=_quality_complete),
            "review": _gate_view(checkpoint, "review", complete_fn=_review_complete),
            "critic": _gate_view(checkpoint, "critic", complete_fn=_critic_advisory_complete),
            "precommit_eval": _gate_view(checkpoint, "precommit_eval", complete_fn=_precommit_complete),
            "official_full": _gate_view(
                checkpoint,
                "official_full",
                complete_fn=_official_full_complete,
            ),
        },
        "gate_keys_present": sorted(
            key for key, value in gate_results.items()
            if key in _GATE_FIELD_ALLOWLIST and isinstance(value, dict) and value
        ),
        "worker_failures": [
            {
                "worker_id": row.get("worker_id"),
                "role": _bounded_text(row.get("role")),
                "error": _bounded_text(row.get("error")),
                "failure_type": _bounded_text(row.get("failure_type")),
                "category": _bounded_text(row.get("category")),
                "gen": row.get("gen"),
                "timestamp": _bounded_scalar(row.get("timestamp")),
                # JSONL rows are an append-only audit of failures that happened
                # during this workflow.  They do not prove that the failure is
                # still the current blocker; current recovery/disposition comes
                # from health.pipeline and the checkpoint infra overlay.
                "record_state": "historical",
                "current_blocker": False,
            }
            for row in failures[:_MAX_AGENT_FAILURES]
            if isinstance(row, dict)
        ],
        "worker_failures_truncated": len(failures) > _MAX_AGENT_FAILURES,
        "observer_limits": {
            "max_tasks": _MAX_AGENT_TASKS,
            "max_target_files_per_task": _MAX_TASK_TARGETS,
            "max_worker_failures": _MAX_AGENT_FAILURES,
            "max_response_bytes": _MAX_AGENT_RESPONSE_BYTES,
        },
    }


def _agents_request_key() -> tuple:
    return (
        _path_stat_token(PIPELINE_STATE_FILE),
        _path_stat_token(WORKER_FAILURES_FILE),
        str(RESULTS_DIR),
        id(load_strict_pipeline_checkpoint),
        id(read_strict_worker_failures),
        id(_quality_complete),
        id(_review_complete),
        id(_critic_advisory_complete),
        id(_precommit_complete),
        id(_official_full_complete),
    )


def _read_agents_projection() -> tuple[object, dict]:
    """Read, validate and summarize one exact checkpoint off the event loop."""

    checkpoint = load_strict_pipeline_checkpoint(RESULTS_DIR, PIPELINE_STATE_FILE)
    failures = read_strict_worker_failures(
        WORKER_FAILURES_FILE,
        results_dir=RESULTS_DIR,
        checkpoint_path=PIPELINE_STATE_FILE,
        checkpoint_snapshot=checkpoint,
        limit=50,
    )
    projection = _build_agents_projection(checkpoint, failures)
    if isinstance(checkpoint, dict):
        authority_key: object = (
            checkpoint.get("workflow_run_id"),
            checkpoint.get("checkpoint_revision"),
            checkpoint.get("next_v"),
            checkpoint.get("source_v"),
            checkpoint.get("parent2_v"),
            checkpoint.get("stage"),
            checkpoint.get("run_id"),
        )
    else:
        authority_key = ("no_strict_workflow",)
    encoded = json.dumps(
        projection,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > _MAX_AGENT_RESPONSE_BYTES:
        # Never leak an accidentally expanded receipt just because an upstream
        # schema added a large field.  The observer becomes unavailable.
        projection = {
            "available": False,
            "reason": "agent_projection_size_budget_exceeded",
            "evaluation_epoch": "national_tcp_policy_v1",
        }
    return authority_key, projection


@router.get("/agents")
async def pipeline_agents():
    """Return a structured agent-activity projection for the current workflow.

    Combines the validated strict checkpoint with the already epoch-filtered
    worker failures.  When no current strict workflow exists the endpoint
    returns ``{"available": False, ...}`` so the dashboard can render a
    fail-closed empty state instead of guessing agent state.
    """
    request_key = _agents_request_key()
    return await run_blocking_isolated(
        _AGENTS_OBSERVER_CACHE.get,
        request_key,
        _read_agents_projection,
        thread_name_prefix="pipeline-agents-observer",
    )


# ── Background 70-hand strength-job projection ──────────────────────────────


def _match_rejection_reasons(
    row: dict,
    *,
    active_bots: set[str],
    evaluation_identity_digest: str,
) -> list[str]:
    """Mirror ``_filter_strict_match_rows`` as a diagnostic reason list.

    This never decides admission for strength — ``load_strict_strength_snapshot``
    already does that authoritatively.  It only explains *why* a historical
    row was not admitted, so the dashboard can render "not admissible" samples
    as diagnostics instead of silently dropping them.
    """
    from bot_namespace import EVALUATION_EPOCH

    reasons: list[str] = []
    samples = row.get("net_chips_bot0")
    if row.get("execution_mode") != "native_tcp":
        reasons.append("execution_mode_not_native_tcp")
    if row.get("evaluation_epoch") != EVALUATION_EPOCH:
        reasons.append("evaluation_epoch_mismatch")
    if row.get("evaluation_identity_digest") != evaluation_identity_digest:
        reasons.append("evaluation_identity_digest_mismatch")
    if row.get("bot0") not in active_bots:
        reasons.append("bot0_not_in_active_pool")
    if row.get("bot1") not in active_bots:
        reasons.append("bot1_not_in_active_pool")
    if row.get("bot0") == row.get("bot1"):
        reasons.append("self_match")
    if row.get("strength_sample_unit") != "70_hand_match":
        reasons.append("strength_sample_unit_not_70_hand_match")
    if row.get("hands_per_strength_sample") != 70:
        reasons.append("hands_per_strength_sample_not_70")
    if row.get("strength_admitted") is not True:
        reasons.append("strength_not_admitted")
    if row.get("strength_complete") is not True:
        reasons.append("strength_not_complete")
    if row.get("strength_compliance_passed") is not True:
        reasons.append("strength_compliance_not_passed")
    if not isinstance(row.get("id"), str):
        reasons.append("id_not_string")
    if not isinstance(samples, list):
        reasons.append("net_chips_not_list")
    elif row.get("strength_sample_count") != len(samples):
        reasons.append("strength_sample_count_mismatch")
    elif not samples:
        reasons.append("empty_samples")
    return reasons


_MAX_STAGED_REPLAY_BYTES = 2 * 1024 * 1024
_MAX_STAGED_REPLAY_FILES = 64
_MAX_STRENGTH_MANIFEST_BYTES = 256 * 1024
_MAX_STRENGTH_FILES = 80
_MAX_STRENGTH_DIRECTORY_ENTRIES = 256
_MAX_STRENGTH_TOTAL_READ_BYTES = 8 * 1024 * 1024
_MAX_STRENGTH_CPU_SECONDS = 0.75
_MAX_STRENGTH_WALL_SECONDS = 3.0
_MAX_STRENGTH_ROWS = 1_000
_DEFAULT_STRENGTH_PAGE_LIMIT = 50
_MAX_STRENGTH_PAGE_LIMIT = 100
_STRENGTH_CAPABILITIES = {
    # These describe what this endpoint can prove today.  The existing Elo
    # daemon publishes immutable cycles, but it does not expose the proposed
    # producer/consumer JobEnvelope lease lifecycle through this API.
    "durable_job_lifecycle": False,
    "queued_running_leases": False,
    "producer_consumer_dispatch": False,
}


class _ObserverBudgetExceeded(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _StrengthObserverBudget:
    """One global budget shared by bundle diagnostics and pending replays."""

    def __init__(self) -> None:
        self.started_wall = time.monotonic()
        thread_clock = getattr(time, "thread_time", time.process_time)
        self._thread_clock = thread_clock
        self.started_cpu = thread_clock()
        self.directory_entries = 0
        self.files_read = 0
        self.total_read_bytes = 0
        self.rows_seen = 0

    def check_time(self) -> None:
        if self._thread_clock() - self.started_cpu > _MAX_STRENGTH_CPU_SECONDS:
            raise _ObserverBudgetExceeded("strength_observer_cpu_budget_exceeded")
        if time.monotonic() - self.started_wall > _MAX_STRENGTH_WALL_SECONDS:
            raise _ObserverBudgetExceeded("strength_observer_wall_budget_exceeded")

    def observe_entry(self) -> None:
        self.directory_entries += 1
        if self.directory_entries > _MAX_STRENGTH_DIRECTORY_ENTRIES:
            raise _ObserverBudgetExceeded(
                "strength_observer_directory_entry_budget_exceeded"
            )
        self.check_time()

    def observe_file(self, size: int) -> None:
        self.files_read += 1
        if self.files_read > _MAX_STRENGTH_FILES:
            raise _ObserverBudgetExceeded("strength_observer_file_budget_exceeded")
        self.reserve_bytes(size)

    def reserve_bytes(self, size: int) -> None:
        """Reserve a known repeat read without counting a new unique file."""

        self.total_read_bytes += max(0, int(size))
        if self.total_read_bytes > _MAX_STRENGTH_TOTAL_READ_BYTES:
            raise _ObserverBudgetExceeded("strength_observer_byte_budget_exceeded")
        self.check_time()

    def observe_row(self) -> None:
        self.rows_seen += 1
        if self.rows_seen > _MAX_STRENGTH_ROWS:
            raise _ObserverBudgetExceeded("strength_observer_row_budget_exceeded")
        self.check_time()

    def projection(self, *, complete: bool, issues: list[str]) -> dict:
        return {
            "complete": bool(complete),
            "issues": list(dict.fromkeys(str(item)[:160] for item in issues)),
            "usage": {
                "directory_entries": self.directory_entries,
                "files_read": self.files_read,
                "total_read_bytes": self.total_read_bytes,
                "rows_seen": self.rows_seen,
            },
            "limits": {
                "directory_entries": _MAX_STRENGTH_DIRECTORY_ENTRIES,
                "files_read": _MAX_STRENGTH_FILES,
                "total_read_bytes": _MAX_STRENGTH_TOTAL_READ_BYTES,
                "cpu_seconds": _MAX_STRENGTH_CPU_SECONDS,
                "wall_seconds": _MAX_STRENGTH_WALL_SECONDS,
                "rows": _MAX_STRENGTH_ROWS,
            },
        }


def _is_sha256(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _validated_strength_reset_digest() -> str | None:
    """Return the immutable reset identity even before a two-Bot cycle exists."""

    try:
        from system_strict_bootstrap import load_policy_epoch_reset_receipt

        receipt, errors = load_policy_epoch_reset_receipt(RESULTS_DIR)
    except Exception:
        return None
    digest = receipt.get("receipt_digest") if isinstance(receipt, dict) else None
    return str(digest) if not errors and _is_sha256(digest) else None


def _strength_authority_binding(snapshot: object, bundle: object) -> dict:
    """Project the exact active-pool/reset tuple consumed by strength rows."""

    snapshot_dict = snapshot if isinstance(snapshot, dict) else {}
    bundle_dict = bundle if isinstance(bundle, dict) else {}
    snapshot_active = snapshot_dict.get("active_bots")
    bundle_active = bundle_dict.get("active_bots")
    active_value = snapshot_active if isinstance(snapshot_active, list) else bundle_active
    active_bots = [str(item) for item in active_value] if isinstance(active_value, list) else []
    receipt = bundle_dict.get("epoch_reset_receipt")
    snapshot_reset_digest = snapshot_dict.get("epoch_reset_receipt_digest")
    bundle_reset_digest = receipt.get("receipt_digest") if isinstance(receipt, dict) else None
    reset_sources_match = not (
        _is_sha256(snapshot_reset_digest)
        and _is_sha256(bundle_reset_digest)
        and snapshot_reset_digest != bundle_reset_digest
    )
    reset_digest = snapshot_reset_digest
    if not _is_sha256(reset_digest):
        reset_digest = bundle_reset_digest
    if not _is_sha256(reset_digest):
        reset_digest = _validated_strength_reset_digest()
    manifest = bundle_dict.get("manifest")
    snapshot_identity = snapshot_dict.get("evaluation_identity_digest")
    bundle_identity = manifest.get("evaluation_identity_digest") if isinstance(manifest, dict) else None
    identity_sources_match = not (
        _is_sha256(snapshot_identity)
        and _is_sha256(bundle_identity)
        and snapshot_identity != bundle_identity
    )
    identity = snapshot_identity
    if not _is_sha256(identity) and isinstance(manifest, dict):
        identity = bundle_identity
    snapshot_manifest_digest = snapshot_dict.get("evaluation_manifest_digest")
    bundle_manifest_digest = bundle_dict.get("manifest_digest")
    manifest_sources_match = not (
        _is_sha256(snapshot_manifest_digest)
        and _is_sha256(bundle_manifest_digest)
        and snapshot_manifest_digest != bundle_manifest_digest
    )
    manifest_digest = snapshot_manifest_digest
    if not _is_sha256(manifest_digest):
        manifest_digest = bundle_manifest_digest
    active_sources_match = not (
        isinstance(snapshot_active, list)
        and isinstance(bundle_active, list)
        and [str(item) for item in snapshot_active]
        != [str(item) for item in bundle_active]
    )
    try:
        from bot_namespace import FIRST_STRICT_POLICY_VERSION, parse_bot_version

        active_valid = (
            len(active_bots) == len(set(active_bots))
            and all(
                (version := parse_bot_version(name)) is not None
                and version >= FIRST_STRICT_POLICY_VERSION
                for name in active_bots
            )
        )
    except Exception:
        active_valid = False
    sources_match = (
        active_sources_match
        and reset_sources_match
        and identity_sources_match
        and manifest_sources_match
    )
    if not sources_match:
        reset_digest = None
        identity = None
        manifest_digest = None
    return {
        "evaluation_epoch": "national_tcp_policy_v1",
        "active_bots": active_bots,
        "epoch_reset_receipt_digest": reset_digest if _is_sha256(reset_digest) else None,
        "evaluation_identity_digest": identity if _is_sha256(identity) else None,
        "evaluation_manifest_digest": (
            manifest_digest if _is_sha256(manifest_digest) else None
        ),
        "complete": bool(_is_sha256(reset_digest) and active_valid and sources_match),
    }


def _strength_contract_fields(snapshot: object, bundle: object) -> dict:
    binding = _strength_authority_binding(snapshot, bundle)
    return {
        "active_bots": list(binding["active_bots"]),
        "epoch_reset_receipt_digest": binding["epoch_reset_receipt_digest"],
        "authority_binding": binding,
        "capabilities": dict(_STRENGTH_CAPABILITIES),
    }


def _read_preflight_regular_file(
    path: Path,
    *,
    budget: _StrengthObserverBudget,
    maximum_bytes: int,
    observe_file: bool = True,
) -> bytes:
    """No-follow stable read used before the production bundle loader.

    The loader performs the authoritative digest and semantic validation.  This
    earlier pass has one narrower purpose: reject work that cannot fit inside
    the dashboard observer budget *before* that loader reads every immutable
    payload and replay.
    """

    try:
        before = os.lstat(path)
    except OSError as exc:
        raise _ObserverBudgetExceeded(
            "strength_observer_bundle_preflight_failed"
        ) from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size < 0
        or before.st_size > maximum_bytes
    ):
        raise _ObserverBudgetExceeded(
            "strength_observer_bundle_preflight_failed"
        )
    if observe_file:
        budget.observe_file(before.st_size)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise _ObserverBudgetExceeded(
            "strength_observer_bundle_preflight_failed"
        ) from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino, opened.st_size)
            != (before.st_dev, before.st_ino, before.st_size)
        ):
            raise _ObserverBudgetExceeded(
                "strength_observer_bundle_preflight_failed"
            )
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            budget.check_time()
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise _ObserverBudgetExceeded(
                    "strength_observer_bundle_preflight_failed"
                )
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise _ObserverBudgetExceeded(
                "strength_observer_bundle_preflight_failed"
            )
        after = os.fstat(descriptor)
        live_after = os.lstat(path)
        stable = (
            after.st_nlink == 1
            and live_after.st_nlink == 1
            and (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            == (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            == (
                live_after.st_dev,
                live_after.st_ino,
                live_after.st_size,
                live_after.st_mtime_ns,
                live_after.st_ctime_ns,
            )
        )
        if not stable:
            raise _ObserverBudgetExceeded(
                "strength_observer_bundle_preflight_failed"
            )
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _observe_preflight_regular_file(
    path: Path,
    *,
    budget: _StrengthObserverBudget,
    expected_bytes: int,
) -> None:
    try:
        value = os.lstat(path)
    except OSError as exc:
        raise _ObserverBudgetExceeded(
            "strength_observer_bundle_preflight_failed"
        ) from exc
    if (
        not stat.S_ISREG(value.st_mode)
        or value.st_nlink != 1
        or value.st_size != expected_bytes
    ):
        raise _ObserverBudgetExceeded(
            "strength_observer_bundle_preflight_failed"
        )
    budget.observe_file(value.st_size)


def _preflight_strength_bundle_budget(
    results_dir: Path,
    budget: _StrengthObserverBudget,
) -> bool:
    """Bound manifest-declared bundle/replay work before authority loading.

    ``False`` means there is no cycle manifest, so the production loader has no
    immutable cycle payload to scan.  ``True`` means every manifest-declared
    file, append-log row, and admitted replay fits the same global observer
    budget.  Integrity is still revalidated by
    ``load_current_strict_evaluation_bundle`` afterwards.
    """

    manifest_path = results_dir / "evaluation_cycle_manifest.json"
    try:
        os.lstat(manifest_path)
    except FileNotFoundError:
        budget.check_time()
        return False
    except OSError as exc:
        raise _ObserverBudgetExceeded(
            "strength_observer_bundle_preflight_failed"
        ) from exc
    raw_manifest = _read_preflight_regular_file(
        manifest_path,
        budget=budget,
        maximum_bytes=_MAX_STRENGTH_MANIFEST_BYTES,
    )
    # The production authority reader reopens the manifest before and after
    # the immutable payloads to detect concurrent pointer movement.  Reserve
    # both reads now so its work cannot begin outside this budget.
    budget.reserve_bytes(len(raw_manifest))
    budget.reserve_bytes(len(raw_manifest))
    try:
        manifest = json.loads(raw_manifest.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise _ObserverBudgetExceeded(
            "strength_observer_bundle_preflight_failed"
        ) from exc
    if not isinstance(manifest, dict):
        raise _ObserverBudgetExceeded("strength_observer_bundle_preflight_failed")

    relative = Path(str(manifest.get("cycle_dir") or ""))
    if (
        relative.is_absolute()
        or len(relative.parts) != 2
        or relative.parts[0] != "evaluation_cycles"
        or not relative.parts[1]
        or len(relative.parts[1]) > 128
        or any(
            char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
            for char in relative.parts[1]
        )
    ):
        raise _ObserverBudgetExceeded("strength_observer_bundle_preflight_failed")
    cycle_directory = results_dir / relative
    try:
        cycle_stat = os.lstat(cycle_directory)
        resolved_parent = cycle_directory.resolve().parent
        expected_parent = (results_dir / "evaluation_cycles").resolve()
    except OSError as exc:
        raise _ObserverBudgetExceeded(
            "strength_observer_bundle_preflight_failed"
        ) from exc
    if (
        not stat.S_ISDIR(cycle_stat.st_mode)
        or cycle_directory.is_symlink()
        or resolved_parent != expected_parent
    ):
        raise _ObserverBudgetExceeded("strength_observer_bundle_preflight_failed")

    from evaluation_bundle import APPEND_LOGS, BUNDLE_FILES

    append_payloads: dict[str, bytes] = {}
    for contracts_key, filenames, size_key in (
        ("files", BUNDLE_FILES, "bytes"),
        ("append_logs", APPEND_LOGS, "committed_bytes"),
    ):
        contracts = manifest.get(contracts_key)
        if not isinstance(contracts, dict):
            raise _ObserverBudgetExceeded(
                "strength_observer_bundle_preflight_failed"
            )
        for role, expected_filename in filenames.items():
            contract = contracts.get(role)
            size = contract.get(size_key) if isinstance(contract, dict) else None
            if (
                not isinstance(contract, dict)
                or contract.get("filename") != expected_filename
                or type(size) is not int
                or size < 0
            ):
                raise _ObserverBudgetExceeded(
                    "strength_observer_bundle_preflight_failed"
                )
            budget.observe_entry()
            payload_path = cycle_directory / expected_filename
            _observe_preflight_regular_file(
                payload_path,
                budget=budget,
                expected_bytes=size,
            )
            if contracts_key == "append_logs":
                append_payloads[role] = _read_preflight_regular_file(
                    payload_path,
                    budget=budget,
                    maximum_bytes=size,
                    observe_file=False,
                )
                # Preflight has read this append log once; reserve the
                # production loader's later content-bound read as well.
                budget.reserve_bytes(size)

    replay_ids: set[str] = set()
    for role, payload in append_payloads.items():
        for raw_line in io.BytesIO(payload):
            budget.observe_row()
            if len(raw_line) > _MAX_STAGED_REPLAY_BYTES:
                raise _ObserverBudgetExceeded(
                    "strength_observer_jsonl_row_size_budget_exceeded"
                )
            if role != "match_history" or not raw_line.strip():
                continue
            try:
                row = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue
            replay_id = row.get("id") if isinstance(row, dict) else None
            if (
                not isinstance(row, dict)
                or row.get("strength_admitted") is not True
                or not isinstance(replay_id, str)
                or len(replay_id) > 255
                or replay_id.startswith(".")
                or Path(replay_id).name != replay_id
                or not replay_id.endswith(".json")
            ):
                continue
            replay_ids.add(replay_id)
    for replay_id in sorted(replay_ids):
        replay_path = results_dir / "match_replay" / replay_id
        try:
            replay_size = os.lstat(replay_path).st_size
        except OSError:
            # The authority loader will reject the missing replay without a
            # potentially unbounded content read.
            continue
        budget.observe_entry()
        _observe_preflight_regular_file(
            replay_path,
            budget=budget,
            expected_bytes=replay_size,
        )
        # The strict bundle reader validates admission and then recomputes the
        # H2H projection from the same replay.  Reserve that second read before
        # entering the loader.
        budget.reserve_bytes(replay_size)
    budget.check_time()
    return True


def _pending_diagnostic(filename: str | None, reason: str) -> dict:
    return {
        "id": None,
        "filename": filename,
        "timestamp": None,
        "bot0": None,
        "bot1": None,
        "strength_sample_count": None,
        "hands_per_strength_sample": None,
        "rejection_reasons": [reason],
    }


def _read_pending_bytes(
    directory_fd: int,
    name: str,
    *,
    budget: _StrengthObserverBudget | None = None,
) -> bytes:
    """Read one bounded regular file through a no-follow directory handle."""

    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    fd = os.open(name, flags, dir_fd=directory_fd)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise OSError("pending replay is not a regular file")
        if before.st_size <= 0 or before.st_size > _MAX_STAGED_REPLAY_BYTES:
            raise OSError("pending replay size is outside the observer cap")
        if budget is not None:
            budget.observe_file(before.st_size)
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                raise OSError("pending replay changed during read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(fd, 1):
            raise OSError("pending replay grew during read")
        after = os.fstat(fd)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise OSError("pending replay identity changed during read")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _staged_pending_view(
    pending_dir: Path,
    *,
    active_bots: set[str],
    evaluation_identity_digest: str,
    budget: _StrengthObserverBudget | None = None,
) -> tuple[list[dict], list[dict]]:
    """Project staged (not-yet-committed) 70-hand matches as diagnostics.

    A staged file has passed daemon-side native validation but has not been
    admitted into the immutable cycle.  It is in-flight evidence only, never
    strength authority.
    """
    views: list[dict] = []
    diagnostics: list[dict] = []
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        directory_fd = os.open(pending_dir, flags)
    except FileNotFoundError:
        return views, diagnostics
    except OSError:
        return views, [_pending_diagnostic(None, "staged_pending_directory_unsafe")]
    try:
        try:
            json_entries = []
            with os.scandir(directory_fd) as iterator:
                for entry in iterator:
                    if budget is not None:
                        budget.observe_entry()
                    if entry.name.endswith(".json"):
                        json_entries.append(entry)
                        if len(json_entries) > _MAX_STAGED_REPLAY_FILES:
                            raise _ObserverBudgetExceeded(
                                "strength_observer_file_budget_exceeded"
                            )
            json_entries.sort(key=lambda entry: entry.name)
        except OSError:
            return views, [
                _pending_diagnostic(None, "staged_pending_directory_unreadable")
            ]
        for entry in json_entries:
            name = entry.name
            try:
                if entry.is_symlink():
                    diagnostics.append(
                        _pending_diagnostic(name, "staged_pending_symlink_rejected")
                    )
                    continue
                raw = _read_pending_bytes(directory_fd, name, budget=budget)
                data = json.loads(raw.decode("utf-8"))
            except (OSError, UnicodeDecodeError, ValueError):
                diagnostics.append(
                    _pending_diagnostic(name, "staged_pending_payload_unreadable")
                )
                continue
            if budget is not None:
                budget.observe_row()
            if not isinstance(data, dict):
                diagnostics.append(
                    _pending_diagnostic(name, "staged_pending_payload_not_object")
                )
                continue
            reasons: list[str] = []
            if data.get("evaluation_identity_digest") != evaluation_identity_digest:
                reasons.append("evaluation_identity_digest_mismatch")
            if data.get("bot0") not in active_bots:
                reasons.append("bot0_not_in_active_pool")
            if data.get("bot1") not in active_bots:
                reasons.append("bot1_not_in_active_pool")
            try:
                from bot_artifact import hash_path
                from replay_analysis import validate_native_replay

                validation = validate_native_replay(
                    data,
                    expected_evaluation_identity_digest=evaluation_identity_digest,
                    expected_replay_id=name,
                )
                if not validation.accepted:
                    reasons.append(f"staged_replay_invalid:{validation.reason}")
                elif not reasons:
                    expected_hashes = {
                        label: hash_path(PROJECT_ROOT / "bots" / label)
                        for label in (str(data.get("bot0")), str(data.get("bot1")))
                    }
                    if dict(validation.artifact_hashes) != expected_hashes:
                        reasons.append("staged_replay_artifact_identity_drift")
            except Exception as exc:
                reasons.append(f"staged_replay_validation_error:{type(exc).__name__}")
            if budget is not None:
                budget.check_time()
            if reasons:
                diagnostics.append({
                    "id": data.get("id"),
                    "filename": name,
                    "timestamp": data.get("timestamp"),
                    "bot0": data.get("bot0"),
                    "bot1": data.get("bot1"),
                    "strength_sample_count": data.get("strength_sample_count"),
                    "hands_per_strength_sample": data.get("hands_per_strength_sample"),
                    "rejection_reasons": list(dict.fromkeys(reasons)),
                })
                continue
            views.append({
                "filename": name,
                "id": data.get("id"),
                "timestamp": data.get("timestamp"),
                "bot0": data.get("bot0"),
                "bot1": data.get("bot1"),
                "evaluation_identity_digest": data.get("evaluation_identity_digest"),
                "strength_sample_unit": data.get("strength_sample_unit"),
                "hands_per_strength_sample": data.get("hands_per_strength_sample"),
                "strength_sample_count": data.get("strength_sample_count"),
                "strength_admitted": data.get("strength_admitted"),
                "strength_complete": data.get("strength_complete"),
                "strength_compliance_passed": data.get("strength_compliance_passed"),
            })
    finally:
        os.close(directory_fd)
    return views, diagnostics


def _strength_diagnostic(reason: str) -> dict:
    return _pending_diagnostic(None, reason)


def _iter_bounded_jsonl(
    payload: bytes,
    *,
    budget: _StrengthObserverBudget,
    rows_preflighted: bool = False,
):
    for raw_line in io.BytesIO(payload):
        if not rows_preflighted:
            budget.observe_row()
        else:
            budget.check_time()
        if len(raw_line) > _MAX_STAGED_REPLAY_BYTES:
            raise _ObserverBudgetExceeded(
                "strength_observer_jsonl_row_size_budget_exceeded"
            )
        line = raw_line.strip()
        if not line:
            continue
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        if isinstance(value, dict):
            yield value


def _bounded_daemon_stats(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    result: dict[str, object] = {}
    for key in (
        "total_games",
        "total_matches",
        "total_periods",
        "n_bots",
        "ts",
        "scheduler",
    ):
        scalar = _bounded_scalar(value.get(key))
        if scalar is not None:
            result[key] = scalar
    pairs = value.get("pairs")
    if isinstance(pairs, dict):
        result["pairs"] = {
            str(key)[:256]: scalar
            for key, raw in list(pairs.items())[:64]
            if (scalar := _bounded_scalar(raw)) is not None
        }
        result["pairs_total"] = len(pairs)
        result["pairs_truncated"] = len(pairs) > 64
    return result


def _page_metadata(
    *,
    offset: int,
    limit: int,
    admitted_total: int,
    staged_total: int,
    inadmissible_total: int,
) -> dict:
    return {
        "offset": offset,
        "limit": limit,
        "admitted_total": admitted_total,
        "staged_pending_total": staged_total,
        "inadmissible_total": inadmissible_total,
        "admitted_has_more": offset + limit < admitted_total,
        "staged_pending_has_more": offset + limit < staged_total,
        "inadmissible_has_more": offset + limit < inadmissible_total,
    }


def _build_strength_jobs_projection(
    snapshot: dict,
    *,
    bundle: dict,
    offset: int,
    limit: int,
    budget: _StrengthObserverBudget,
    bundle_rows_preflighted: bool = False,
) -> dict:
    if not isinstance(snapshot, dict) or snapshot.get("available") is not True:
        reason = "evaluation_bundle_unavailable"
        if isinstance(snapshot, dict):
            reason = str(snapshot.get("reason") or reason)
        bundle_complete = isinstance(bundle, dict) and bundle.get("available") is True
        bundle_reason = (
            str(bundle.get("reason") or "invalid_shape")
            if isinstance(bundle, dict)
            else "invalid_shape"
        )
        observer_issues = [] if bundle_complete else [
            f"immutable_diagnostic_bundle_unavailable:{bundle_reason}"
        ]
        return {
            "available": False,
            "reason": reason,
            "evaluation_epoch": "national_tcp_policy_v1",
            **_strength_contract_fields(snapshot, bundle),
            "observer": budget.projection(
                complete=bundle_complete,
                issues=observer_issues,
            ),
        }

    active_bots = list(snapshot.get("active_bots") or [])
    identity = str(snapshot.get("evaluation_identity_digest") or "")
    admitted = list(snapshot.get("match_history") or [])

    # Diagnostic: rows in the current bundle's raw match-history that did not
    # pass admission.  Loaded from the bundle's immutable bytes, not the live
    # top-level alias, so this never leaks retired-epoch rows.
    inadmissible: list[dict] = []
    staged: list[dict] = []
    observer_issues: list[str] = []
    observer_complete = True
    try:
        budget.check_time()
        if isinstance(bundle, dict) and bundle.get("available") is True:
            raw = (bundle.get("raw_append_logs") or {}).get("match_history", b"")
            if not isinstance(raw, bytes):
                raise TypeError("match_history append log is not bytes")
            admitted_ids = {row.get("id") for row in admitted if isinstance(row, dict)}
            for row in _iter_bounded_jsonl(
                raw,
                budget=budget,
                rows_preflighted=bundle_rows_preflighted,
            ):
                if row.get("id") in admitted_ids:
                    continue
                reasons = _match_rejection_reasons(
                    row,
                    active_bots=set(active_bots),
                    evaluation_identity_digest=identity,
                )
                if not reasons:
                    continue
                inadmissible.append({
                    "id": row.get("id"),
                    "timestamp": row.get("timestamp"),
                    "bot0": row.get("bot0"),
                    "bot1": row.get("bot1"),
                    "strength_sample_count": row.get("strength_sample_count"),
                    "hands_per_strength_sample": row.get("hands_per_strength_sample"),
                    "rejection_reasons": reasons,
                })
        else:
            observer_complete = False
            reason = (
                str(bundle.get("reason") or "unavailable")
                if isinstance(bundle, dict)
                else "invalid_shape"
            )
            observer_issues.append(f"immutable_diagnostic_bundle_unavailable:{reason}")

        pending_dir = RESULTS_DIR / "match_replay" / ".pending"
        staged, staged_diagnostics = _staged_pending_view(
            pending_dir,
            active_bots=set(active_bots),
            evaluation_identity_digest=identity,
            budget=budget,
        )
        inadmissible.extend(staged_diagnostics)
        budget.check_time()
    except _ObserverBudgetExceeded as exc:
        # Partial scans are never rendered as if they were a complete queue.
        observer_complete = False
        observer_issues.append(exc.reason)
        staged = []
        inadmissible = [_strength_diagnostic(exc.reason)]
    except Exception as exc:
        observer_complete = False
        reason = f"strength_observer_scan_failed:{type(exc).__name__}"
        observer_issues.append(reason)
        staged = []
        inadmissible = [_strength_diagnostic(reason)]

    admitted_page = admitted[offset: offset + limit]
    staged_page = staged[offset: offset + limit]
    inadmissible_page = inadmissible[offset: offset + limit]

    return {
        "available": True,
        "evaluation_epoch": "national_tcp_policy_v1",
        "evaluation_identity_digest": identity,
        "evaluation_manifest_digest": snapshot.get("evaluation_manifest_digest"),
        **_strength_contract_fields(snapshot, bundle),
        "admitted_samples": [
            {
                "id": row.get("id"),
                "timestamp": row.get("timestamp"),
                "bot0": row.get("bot0"),
                "bot1": row.get("bot1"),
                "bot0_wins": row.get("bot0_wins"),
                "bot1_wins": row.get("bot1_wins"),
                "draws": row.get("draws"),
                "strength_sample_count": row.get("strength_sample_count"),
                "hands_per_strength_sample": row.get("hands_per_strength_sample"),
                "replay_sha256": row.get("replay_sha256"),
            }
            for row in admitted_page
            if isinstance(row, dict)
        ],
        "staged_pending": staged_page,
        "inadmissible_diagnostics": inadmissible_page,
        "pagination": _page_metadata(
            offset=offset,
            limit=limit,
            admitted_total=len(admitted),
            staged_total=len(staged),
            inadmissible_total=len(inadmissible),
        ),
        "observer": budget.projection(
            complete=observer_complete,
            issues=observer_issues,
        ),
        "daemon_stats": _bounded_daemon_stats(snapshot.get("daemon_stats")),
    }


def _strength_request_key() -> tuple:
    return (
        str(RESULTS_DIR),
        _path_stat_token(RESULTS_DIR / "evaluation_cycle_manifest.json"),
        _path_stat_token(RESULTS_DIR / "match_replay" / ".pending"),
        id(load_strict_strength_snapshot),
        id(load_current_strict_evaluation_bundle),
    )


def _read_strength_projection() -> tuple[object, dict]:
    # Keep the manifest/files observed by preflight frozen until the strict
    # loader and snapshot consumer finish.  Without one shared lock, a cycle
    # publisher could replace the pointer between the size reservation and the
    # heavy read, invalidating the observer budget.
    with evaluation_cycle_lock(RESULTS_DIR, exclusive=False):
        return _read_strength_projection_under_cycle_lock()


def _read_strength_projection_under_cycle_lock() -> tuple[object, dict]:
    budget = _StrengthObserverBudget()
    try:
        bundle_rows_preflighted = _preflight_strength_bundle_budget(
            RESULTS_DIR,
            budget,
        )
    except _ObserverBudgetExceeded as exc:
        projection = {
            "available": False,
            "reason": exc.reason,
            "evaluation_epoch": "national_tcp_policy_v1",
            **_strength_contract_fields({}, {}),
            "observer": budget.projection(complete=False, issues=[exc.reason]),
        }
        return ("budget_exceeded", exc.reason), projection
    except Exception:
        reason = "strength_observer_bundle_preflight_failed"
        projection = {
            "available": False,
            "reason": reason,
            "evaluation_epoch": "national_tcp_policy_v1",
            **_strength_contract_fields({}, {}),
            "observer": budget.projection(complete=False, issues=[reason]),
        }
        return ("preflight_failed", reason), projection
    try:
        bundle = load_current_strict_evaluation_bundle(RESULTS_DIR)
    except Exception as exc:
        bundle = {
            "available": False,
            "reason": f"evaluation_bundle_unavailable:{type(exc).__name__}",
        }
    try:
        if (
            not bundle_rows_preflighted
            and isinstance(bundle, dict)
            and bundle.get("available") is True
        ):
            for group_name in ("raw_files", "raw_append_logs"):
                group = bundle.get(group_name)
                if not isinstance(group, dict):
                    continue
                for payload in group.values():
                    if isinstance(payload, bytes):
                        budget.observe_file(len(payload))
        snapshot = load_strict_strength_snapshot(
            RESULTS_DIR,
            bundle_snapshot=bundle,
        )
    except _ObserverBudgetExceeded as exc:
        projection = {
            "available": False,
            "reason": exc.reason,
            "evaluation_epoch": "national_tcp_policy_v1",
            **_strength_contract_fields({}, bundle),
            "observer": budget.projection(complete=False, issues=[exc.reason]),
        }
        return ("budget_exceeded", exc.reason), projection
    try:
        budget.check_time()
    except _ObserverBudgetExceeded as exc:
        projection = {
            "available": False,
            "reason": exc.reason,
            "evaluation_epoch": "national_tcp_policy_v1",
            **_strength_contract_fields(snapshot, bundle),
            "observer": budget.projection(complete=False, issues=[exc.reason]),
        }
        return ("budget_exceeded", exc.reason), projection
    projection = _build_strength_jobs_projection(
        snapshot,
        bundle=bundle,
        offset=0,
        limit=_MAX_STRENGTH_ROWS + _MAX_STAGED_REPLAY_FILES + 1,
        budget=budget,
        bundle_rows_preflighted=bundle_rows_preflighted,
    )
    if isinstance(snapshot, dict) and snapshot.get("available") is True:
        authority_key: object = (
            snapshot.get("evaluation_identity_digest"),
            snapshot.get("evaluation_manifest_digest"),
            snapshot.get("epoch_reset_receipt_digest"),
            tuple(snapshot.get("active_bots") or []),
        )
    else:
        authority_key = (
            "unavailable",
            snapshot.get("reason") if isinstance(snapshot, dict) else None,
        )
    return authority_key, projection


def _paginate_strength_projection(
    projection: dict,
    *,
    offset: int,
    limit: int,
) -> dict:
    """Apply presentation paging after the identity-keyed heavy observation."""

    if projection.get("available") is not True:
        return projection
    admitted = list(projection.get("admitted_samples") or [])
    staged = list(projection.get("staged_pending") or [])
    inadmissible = list(projection.get("inadmissible_diagnostics") or [])
    projection["admitted_samples"] = admitted[offset: offset + limit]
    projection["staged_pending"] = staged[offset: offset + limit]
    projection["inadmissible_diagnostics"] = inadmissible[offset: offset + limit]
    projection["pagination"] = _page_metadata(
        offset=offset,
        limit=limit,
        admitted_total=len(admitted),
        staged_total=len(staged),
        inadmissible_total=len(inadmissible),
    )
    return projection


def _safe_daemon_health_snapshot() -> dict:
    try:
        value = _daemon_health_snapshot()
        return value if isinstance(value, dict) else {
            "alive": False,
            "health_error": "daemon_health_invalid",
        }
    except Exception:
        return {"alive": False, "health_error": "daemon_health_unavailable"}


@router.get("/strength-jobs")
async def pipeline_strength_jobs(
    # The observer itself admits at most _MAX_STRENGTH_ROWS immutable rows;
    # reject nonsensical pages in FastAPI validation before scheduling any
    # blocking bundle work.
    offset: int = Query(0, ge=0, le=_MAX_STRENGTH_ROWS),
    limit: int = Query(
        _DEFAULT_STRENGTH_PAGE_LIMIT,
        ge=1,
        le=_MAX_STRENGTH_PAGE_LIMIT,
    ),
):
    """Return a 70-hand background strength-job projection.

    Admitted samples come from ``load_strict_strength_snapshot`` (the same
    fail-closed authority the ratings endpoints use).  Staged-pending matches
    are enumerated read-only from ``results/match_replay/.pending``.
    Inadmissible rows are diagnostics only — they explain why a historical
    sample was not admitted, never contributing to strength.  Daemon health
    reuses the control-plane snapshot so configuration intent and live
    availability stay in one place.
    """
    request_key = _strength_request_key()
    observation = await run_blocking_isolated(
        _STRENGTH_OBSERVER_CACHE.get,
        request_key,
        _read_strength_projection,
        thread_name_prefix="pipeline-strength-observer",
    )
    evidence = _paginate_strength_projection(
        observation,
        offset=offset,
        limit=limit,
    )
    daemon_health = await run_blocking_isolated(
        _safe_daemon_health_snapshot,
        thread_name_prefix="pipeline-strength-daemon-health",
    )
    return {**evidence, "daemon": daemon_health}
