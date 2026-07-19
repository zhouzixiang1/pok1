"""Pipeline state endpoints — checkpoint, worker failures, and agent activity.

All endpoints here are read-only projections of the current strict national
TCP epoch.  They reuse ``load_strict_pipeline_checkpoint`` as the sole
checkpoint authority and never reopen ``pipeline_state.json`` or any write
path.  See ``docs/evolution-continuous-delivery-runbook.md`` for the contract
that the dashboard must not mix the three checkpoint shapes
(``/api/pipeline/checkpoint``, ``/api/control/health.pipeline`` and
``/api/evolution/state``).
"""

from pathlib import Path

from fastapi import APIRouter, Query

from server.routes._helpers import (
    _jsonl_bytes,
    load_strict_pipeline_checkpoint,
    load_strict_strength_snapshot,
    read_strict_worker_failures,
)
from evaluation_bundle import load_current_strict_evaluation_bundle


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


@router.get("/checkpoint")
async def pipeline_checkpoint():
    """Return current pipeline checkpoint (stage of in-progress generation)."""
    return load_strict_pipeline_checkpoint(RESULTS_DIR, PIPELINE_STATE_FILE)


@router.get("/failures")
async def pipeline_failures(limit: int = Query(10, le=50)):
    """Return failures explicitly bound to the current strict workflow."""
    return read_strict_worker_failures(
        WORKER_FAILURES_FILE,
        results_dir=RESULTS_DIR,
        checkpoint_path=PIPELINE_STATE_FILE,
        limit=limit,
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


def _gate_field(gate: dict | None, name: str, default=None):
    if not isinstance(gate, dict):
        return default
    return gate.get(name, default)


def _quality_complete(gate: dict | None) -> bool:
    """Mirror ``tool_helpers._quality_gate_ok`` field check (read-only).

    The system-level workflow-profile match is intentionally not re-derived
    here: that would require importing the active workflow profile state and
    duplicate the backend trust boundary.  The projection reports the gate's
    own ``all_passed``/``critical_scenarios_passed`` verdicts only and lets
    the dashboard show ``workflow_profile`` fields verbatim.
    """
    return (
        _gate_field(gate, "all_passed") is True
        and _gate_field(gate, "critical_scenarios_passed") is True
    )


def _review_complete(gate: dict | None) -> bool:
    """Mirror ``tool_helpers._review_gate_ok`` field check (read-only)."""
    return (
        _gate_field(gate, "approved") is True
        and not _gate_field(gate, "llm_failed")
        and not _gate_field(gate, "parse_failed")
        and _gate_field(gate, "llm_invoked") is True
        and _gate_field(gate, "reviewer_llm_executed") is True
        and _gate_field(gate, "schema_valid") is True
    )


def _critic_advisory_complete(gate: dict | None) -> bool:
    """Mirror ``tool_helpers._critic_gate_ok`` field check (read-only).

    Critic is advisory-only; completion means a schema-valid LLM execution
    happened, not that it approved the strategy.  The advisory verdict lives
    in ``advisory_approved`` / ``advisory_score``.
    """
    return (
        _gate_field(gate, "approved") is True
        and not _gate_field(gate, "llm_failed")
        and not _gate_field(gate, "parse_failed")
        and _gate_field(gate, "llm_invoked") is True
        and _gate_field(gate, "critic_llm_executed") is True
        and _gate_field(gate, "schema_valid") is True
    )


def _gate_view(checkpoint: dict, name: str, *, complete_fn) -> dict | None:
    gate_results = checkpoint.get("gate_results")
    if not isinstance(gate_results, dict):
        return None
    gate = gate_results.get(name)
    if not isinstance(gate, dict) or not gate:
        return None
    view = {
        "name": name,
        "present": True,
        "complete": bool(complete_fn(gate)),
        "fields": dict(gate),
    }
    return view


def _worker_task_view(task: dict) -> dict:
    """Project one ``master_plan.tasks`` entry without re-deriving its schema."""
    if not isinstance(task, dict):
        return {}
    return {
        "worker_id": task.get("worker_id"),
        "role": task.get("role"),
        "target_files": list(task.get("target_files") or []),
        "difficulty": task.get("difficulty"),
        "skill_layer": task.get("skill_layer"),
        "behavior_hypothesis": task.get("behavior_hypothesis"),
        "expected_diff_shape": task.get("expected_diff_shape"),
        "merge_policy": task.get("merge_policy"),
    }


def _master_view(checkpoint: dict) -> dict:
    plan = checkpoint.get("master_plan")
    tasks = []
    analysis = None
    if isinstance(plan, dict):
        raw_tasks = plan.get("tasks")
        if isinstance(raw_tasks, list):
            tasks = [_worker_task_view(t) for t in raw_tasks if isinstance(t, dict)]
        analysis = plan.get("analysis")
    return {
        "stage_reached": checkpoint.get("stage") in {
            "direction_audited",
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
        },
        "plan_present": isinstance(plan, dict),
        "analysis": analysis if isinstance(analysis, str) else None,
        "tasks": tasks,
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
            "reviewer_feedback": checkpoint.get("reviewer_feedback") or None,
            "infra_failure": checkpoint.get("infra_failure") or None,
        },
        "master": _master_view(checkpoint),
        "direction_audit": checkpoint.get("direction_audit") or None,
        "gates": {
            "quality": _gate_view(checkpoint, "quality", complete_fn=_quality_complete),
            "review": _gate_view(checkpoint, "review", complete_fn=_review_complete),
            "critic": _gate_view(checkpoint, "critic", complete_fn=_critic_advisory_complete),
            "precommit_eval": _gate_view(
                checkpoint,
                "precommit_eval",
                complete_fn=lambda g: _gate_field(g, "passed") is True,
            ),
            "official_full": _gate_view(
                checkpoint,
                "official_full",
                complete_fn=lambda g: _gate_field(g, "passed") is True,
            ),
        },
        "gate_keys_present": sorted(
            key for key, value in gate_results.items()
            if isinstance(value, dict) and value
        ),
        "worker_failures": [
            {
                "worker_id": row.get("worker_id"),
                "role": row.get("role"),
                "error": row.get("error"),
                "failure_type": row.get("failure_type"),
                "category": row.get("category"),
                "gen": row.get("gen"),
            }
            for row in failures
            if isinstance(row, dict)
        ],
    }


@router.get("/agents")
async def pipeline_agents():
    """Return a structured agent-activity projection for the current workflow.

    Combines the validated strict checkpoint with the already epoch-filtered
    worker failures.  When no current strict workflow exists the endpoint
    returns ``{"available": False, ...}`` so the dashboard can render a
    fail-closed empty state instead of guessing agent state.
    """
    checkpoint = load_strict_pipeline_checkpoint(RESULTS_DIR, PIPELINE_STATE_FILE)
    failures = read_strict_worker_failures(
        WORKER_FAILURES_FILE,
        results_dir=RESULTS_DIR,
        checkpoint_path=PIPELINE_STATE_FILE,
        limit=50,
    )
    return _build_agents_projection(checkpoint, failures)


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


def _safe_pending_replay_files(pending_dir: Path) -> list[Path]:
    """Enumerate staged pending replay files without following symlinks.

    Mirrors the safety posture of ``elo_daemon._ensure_safe_replay_directory``:
    the pending directory itself must be a real directory, and each candidate
    must be a regular file (no symlinks).  Read-only; never unlink.
    """
    if not pending_dir.exists() or not pending_dir.is_dir() or pending_dir.is_symlink():
        return []
    files: list[Path] = []
    try:
        for child in sorted(pending_dir.iterdir()):
            if not child.is_file() or child.is_symlink():
                continue
            if child.suffix != ".json":
                continue
            files.append(child)
    except OSError:
        return []
    return files


def _staged_pending_view(pending_dir: Path) -> list[dict]:
    """Project staged (not-yet-committed) 70-hand matches as diagnostics.

    A staged file has passed daemon-side native validation but has not been
    admitted into the immutable cycle.  It is in-flight evidence only, never
    strength authority.
    """
    import json as _json

    views: list[dict] = []
    for path in _safe_pending_replay_files(pending_dir):
        try:
            with path.open("r", encoding="utf-8") as handle:
                data = _json.load(handle)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        views.append({
            "filename": path.name,
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
    return views


def _build_strength_jobs_projection(snapshot: dict, daemon_health: dict) -> dict:
    if not isinstance(snapshot, dict) or snapshot.get("available") is not True:
        reason = "evaluation_bundle_unavailable"
        if isinstance(snapshot, dict):
            reason = str(snapshot.get("reason") or reason)
        active_bots = list(snapshot.get("active_bots") or []) if isinstance(snapshot, dict) else []
        return {
            "available": False,
            "reason": reason,
            "evaluation_epoch": "national_tcp_policy_v1",
            "active_bots": active_bots,
            "daemon": daemon_health,
        }

    active_bots = list(snapshot.get("active_bots") or [])
    identity = str(snapshot.get("evaluation_identity_digest") or "")
    admitted = list(snapshot.get("match_history") or [])

    # Diagnostic: rows in the current bundle's raw match-history that did not
    # pass admission.  Loaded from the bundle's immutable bytes, not the live
    # top-level alias, so this never leaks retired-epoch rows.
    inadmissible: list[dict] = []
    try:
        bundle = load_current_strict_evaluation_bundle(RESULTS_DIR)
        if isinstance(bundle, dict) and bundle.get("available") is True:
            raw = (bundle.get("raw_append_logs") or {}).get("match_history", b"")
            admitted_ids = {row.get("id") for row in admitted if isinstance(row, dict)}
            for row in _jsonl_bytes(raw):
                if not isinstance(row, dict):
                    continue
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
    except Exception:
        inadmissible = []

    pending_dir = RESULTS_DIR / "match_replay" / ".pending"
    staged = _staged_pending_view(pending_dir)

    return {
        "available": True,
        "evaluation_epoch": "national_tcp_policy_v1",
        "evaluation_identity_digest": identity,
        "evaluation_manifest_digest": snapshot.get("evaluation_manifest_digest"),
        "epoch_reset_receipt_digest": snapshot.get("epoch_reset_receipt_digest"),
        "active_bots": active_bots,
        "daemon": daemon_health,
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
            for row in admitted
            if isinstance(row, dict)
        ],
        "staged_pending": staged,
        "inadmissible_diagnostics": inadmissible,
        "daemon_stats": snapshot.get("daemon_stats") or {},
    }


@router.get("/strength-jobs")
async def pipeline_strength_jobs():
    """Return a 70-hand background strength-job projection.

    Admitted samples come from ``load_strict_strength_snapshot`` (the same
    fail-closed authority the ratings endpoints use).  Staged-pending matches
    are enumerated read-only from ``results/match_replay/.pending``.
    Inadmissible rows are diagnostics only — they explain why a historical
    sample was not admitted, never contributing to strength.  Daemon health
    reuses the control-plane snapshot so configuration intent and live
    availability stay in one place.
    """
    snapshot = load_strict_strength_snapshot(RESULTS_DIR)
    try:
        daemon_health = _daemon_health_snapshot()
    except Exception:
        daemon_health = {"alive": False, "health_error": "daemon_health_unavailable"}
    return _build_strength_jobs_projection(snapshot, daemon_health)
