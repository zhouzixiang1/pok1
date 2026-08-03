"""Pipeline stage routing + checkpoint recovery-route resolution.

Extracted from orchestrator.py as a single business responsibility: decode
the persisted pipeline checkpoint into the deterministic next-tool route
(repeated-tool guard, recovery-route resolution, deterministic-route handler
binding, checkpoint observation, structured-event progress tail, and
actionable-stage stall/handoff identity).

Members moved here (all re-exported by orchestrator.py):

* ``_CORRECTIVE_RETRY_STAGES_BY_TOOL``  -- stages that legitimately re-enter a
  previously used pipeline tool (corrective gate re-entry).
* ``_DETERMINISTIC_RECOVERY_TOOLS`` / ``_DETERMINISTIC_ROUTES_WITH_LLM``  --
  the deterministic checkpoint-recovery tool set and its LLM-required subset.
* ``_ORCH_EXTERNAL_PROGRESS_EVENT_TYPES``  -- structured-event types that count
  as current-generation external progress.
* ``_as_positive_int``, ``_has_recorded_gate_failure``,
  ``_has_corrective_retry_history``, ``_read_checkpoint_for_repeated_tool_guard``,
  ``_route_allows_tool``, ``_classify_allowed_repeated_pipeline_tool``,
  ``_deterministic_route_requires_llm``, ``_resolve_recovery_route``,
  ``_checkpoint_master_plan_arg``, ``_checkpoint_reviewer_feedback``,
  ``_checkpoint_commit_strategy``, ``_deterministic_route_handler_and_args``,
  ``_coerce_event_ts``, ``_pipeline_checkpoint_observation``,
  ``_read_active_pipeline_checkpoint``, ``_read_structured_events_tail``,
  ``_event_matches_active_generation``,
  ``_latest_orchestrator_external_progress``,
  ``_detect_actionable_stage_stall``, ``_checkpoint_actionable_identity``,
  ``_checkpoint_stream_owned_route_identity``.

IMPORTANT -- shared-symbol access model
---------------------------------------
Symbols referenced by these bodies that live in ``orchestrator`` (``log``,
``log_system_event``, ``_NOISY_TOOLS``, ``ORCH_EXTERNAL_PROGRESS_TAIL_BYTES``,
``ORCH_ACTIONABLE_STAGE_TIMEOUT``) are written as ``_o.<name>`` so they
resolve against the live ``orchestrator`` module attribute, matching the
pattern proven by ``orchestrator_branch_guard`` /
``orchestrator_post_generation``.  This lets the test suite's
``monkeypatch.setattr(orchestrator, "_pipeline_checkpoint_observation", ...)``
etc. (which replaces the *re-exported* attribute on ``orchestrator``)
continue to drive every bare-global call site in ``orchestrator_loop`` /
``_run_one_cycle`` unchanged.

``FIRST_STRICT_POLICY_VERSION`` and ``ARCHIVED_VERSION_HIGH_WATER`` are
imported directly from ``bot_namespace`` (they are immutable constants, not
monkeypatched) exactly as orchestrator.py already imports them.
"""

from __future__ import annotations

import json
import os
import time

import orchestrator as _o
from bot_namespace import ARCHIVED_VERSION_HIGH_WATER, FIRST_STRICT_POLICY_VERSION


_CORRECTIVE_RETRY_STAGES_BY_TOOL = {
    "execute_workers": {
        "quality_failed",
        "reviewed",
        "critic_checked",
        "precommit_failed",
        "official_failed",
        "repair_planned",
        "rework_running",
    },
    "run_quality_gates": {"workers_done"},
    "run_review": {"quality_passed"},
    "run_critic": {"reviewed", "critic_checked"},
    "run_precommit_eval": {"critic_checked"},
}


def _as_positive_int(value) -> int:
    try:
        return max(0, int(value or 0))
    except Exception:
        return 0


def _has_recorded_gate_failure(checkpoint) -> bool:
    gate_results = checkpoint.get("gate_results") if isinstance(checkpoint, dict) else None
    if not isinstance(gate_results, dict):
        return False
    for gate in gate_results.values():
        if not isinstance(gate, dict):
            continue
        if gate.get("passed") is False or gate.get("ok") is False or gate.get("success") is False:
            return True
    return False


def _has_corrective_retry_history(checkpoint) -> bool:
    if not isinstance(checkpoint, dict):
        return False
    return bool(
        _o._as_positive_int(checkpoint.get("audit_attempt")) > 0
        or _o._as_positive_int(checkpoint.get("generation_attempt")) > 0
        or _o._as_positive_int(checkpoint.get("precommit_attempt")) > 0
        or _o._as_positive_int(checkpoint.get("worker_failure_count")) > 0
        or checkpoint.get("reviewer_feedback")
        or checkpoint.get("audit_context")
        or _o._has_recorded_gate_failure(checkpoint)
    )


def _read_checkpoint_for_repeated_tool_guard():
    try:
        from evolution_infra import read_pipeline_checkpoint
        return read_pipeline_checkpoint() or {}
    except Exception:
        return {}


def _route_allows_tool(checkpoint, tool_name: str) -> bool:
    try:
        from pipeline_state import route_policy
        route = route_policy(checkpoint)
    except Exception:
        return True
    return route.get("next_tool") == tool_name


def _classify_allowed_repeated_pipeline_tool(tool_name: str, tool_input=None):
    """Return an info payload when a repeated MCP tool call is valid state flow.

    The outer Orchestrator stream sees only "this is the 2nd run_master call".
    Whether that is wasteful or correct depends on the persisted checkpoint:
    Master validation/audit rejection, quality repair, review repair, and
    precommit repair all intentionally re-enter a previously used pipeline tool
    in the same cycle. Keep redundant-call warnings for repeats that are not on
    one of those explicit state-machine routes.
    """
    if tool_name in _o._NOISY_TOOLS:
        return None

    checkpoint = _o._read_checkpoint_for_repeated_tool_guard()
    if not checkpoint:
        return None
    stage = checkpoint.get("stage")

    if tool_name == "run_master":
        master_plan = checkpoint.get("master_plan")
        has_plan = bool(master_plan) if not isinstance(master_plan, list) else bool(master_plan)
        audit_context = checkpoint.get("audit_context")
        if (
            stage == "direction_audited"
            and not has_plan
            and _o._route_allows_tool(checkpoint, "run_master")
            and (
                _o._as_positive_int(checkpoint.get("audit_attempt")) > 0
                or bool(audit_context)
            )
        ):
            return {
                "reason": "corrective_master_replan",
                "stage": stage,
                "audit_attempt": _o._as_positive_int(checkpoint.get("audit_attempt")),
                "next_v": checkpoint.get("next_v"),
                "source_v": checkpoint.get("source_v"),
            }
        return None

    allowed_stages = _CORRECTIVE_RETRY_STAGES_BY_TOOL.get(tool_name)
    if not allowed_stages or stage not in allowed_stages:
        return None
    if not _o._route_allows_tool(checkpoint, tool_name):
        return None
    if not _o._has_corrective_retry_history(checkpoint):
        return None

    return {
        "reason": "corrective_gate_reentry",
        "stage": stage,
        "next_v": checkpoint.get("next_v"),
        "source_v": checkpoint.get("source_v"),
        "generation_attempt": _o._as_positive_int(checkpoint.get("generation_attempt")),
        "precommit_attempt": _o._as_positive_int(checkpoint.get("precommit_attempt")),
        "worker_failure_count": _o._as_positive_int(checkpoint.get("worker_failure_count")),
    }


_DETERMINISTIC_RECOVERY_TOOLS = frozenset({
    "abandon_generation",
    "execute_workers",
    "prepare_next_gen",
    "run_crossover",
    "run_quality_gates",
    "run_review",
    "run_critic",
    "run_precommit_eval",
    "commit_bot",
    "run_archivist",
})

_DETERMINISTIC_ROUTES_WITH_LLM = frozenset({
    "run_crossover",
    "run_direction_audit",
    "run_master",
    "execute_workers",
    "run_review",
    "run_critic",
    "run_archivist",
})


def _deterministic_route_requires_llm(checkpoint, next_tool: str) -> bool:
    if next_tool not in _DETERMINISTIC_ROUTES_WITH_LLM:
        return False
    try:
        from system_strict_bootstrap import is_declared_native_bootstrap

        system_migration = is_declared_native_bootstrap(checkpoint)
    except Exception:
        system_migration = False
    if not system_migration:
        return True
    stage = str((checkpoint or {}).get("stage") or "")
    # These exact first-migration stages are content-bound system verifiers.
    if next_tool == "run_direction_audit" and stage == "prepared":
        return False
    if next_tool == "execute_workers" and stage == "master_planned" and not (
        (checkpoint or {}).get("reviewer_feedback")
    ):
        return False
    # Master (three proposals + two anonymous ballots), Review, and Critic are
    # mandatory LLM governance stages even for the deterministic first Worker
    # migration.  Only Direction's exact receipt and the initial Worker
    # blueprint are system-executable while the provider is paused.
    return True


def _resolve_recovery_route(checkpoint):
    """Return deterministic checkpoint route only for known recovery-safe tools."""
    if not checkpoint:
        return None
    try:
        from pipeline_state import route_policy
        route = route_policy(checkpoint)
    except Exception:
        return None
    next_tool = route.get("next_tool")
    if next_tool in {"run_direction_audit", "run_master"}:
        try:
            from system_strict_bootstrap import system_recovery_eligible

            if not system_recovery_eligible(checkpoint, next_tool):
                return None
        except Exception:
            return None
    elif next_tool not in _DETERMINISTIC_RECOVERY_TOOLS:
        return None
    return {
        "next_tool": next_tool,
        "directive": route.get("directive"),
        "next_v": checkpoint.get("next_v"),
        "source_v": checkpoint.get("source_v"),
        "stage": checkpoint.get("stage"),
        "parent2_v": checkpoint.get("parent2_v"),
        "route": route,
    }


def _checkpoint_master_plan_arg(checkpoint):
    """Return the saved plan context for deterministic review/critic routes."""
    if not isinstance(checkpoint, dict):
        return []
    master_plan = checkpoint.get("master_plan")
    if isinstance(master_plan, (dict, list)):
        return master_plan
    return []


def _checkpoint_reviewer_feedback(checkpoint):
    if not isinstance(checkpoint, dict):
        return ""
    feedback = checkpoint.get("reviewer_feedback")
    if isinstance(feedback, str) and feedback.strip():
        return feedback
    gate = (checkpoint.get("gate_results") or {}).get("review") or {}
    feedback = gate.get("feedback")
    return feedback if isinstance(feedback, str) else ""


def _checkpoint_commit_strategy(checkpoint):
    if not isinstance(checkpoint, dict):
        return ""
    master_plan = checkpoint.get("master_plan") or {}
    if isinstance(master_plan, dict) and master_plan.get("strategy"):
        return str(master_plan.get("strategy"))
    return "crossover" if checkpoint.get("parent2_v") is not None else "master"


def _deterministic_route_handler_and_args(next_tool, checkpoint, next_v, source_v, parent2_v):
    """Return the MCP handler and canonical args for a deterministic checkpoint route."""
    if next_tool == "run_direction_audit":
        args = {"source_v": source_v, "next_v": next_v}
        from tool_planning import run_direction_audit
        return run_direction_audit.handler, args
    if next_tool == "run_master":
        args = {"source_v": source_v, "next_v": next_v}
        from tool_planning import run_master
        return run_master.handler, args
    if next_tool == "execute_workers":
        args = {"next_v": next_v, "source_v": source_v}
        reviewer_feedback = _o._checkpoint_reviewer_feedback(checkpoint)
        if reviewer_feedback:
            args["reviewer_feedback"] = reviewer_feedback
        from tool_planning import execute_workers
        return execute_workers.handler, args
    if next_tool == "prepare_next_gen":
        args = {"source_v": source_v, "next_v": next_v}
        from tool_gates import prepare_next_gen
        return prepare_next_gen.handler, args
    if next_tool == "abandon_generation":
        from tool_bot_management import abandon_generation
        return abandon_generation.handler, {}
    if next_tool == "run_crossover":
        args = {
            "parent_a": source_v,
            "parent_b": parent2_v,
            "target_v": next_v,
        }
        from tool_commit import run_crossover
        return run_crossover.handler, args
    if next_tool == "run_quality_gates":
        args = {"version": next_v, "source_v": source_v}
        from tool_gates import run_quality_gates
        return run_quality_gates.handler, args
    if next_tool == "run_review":
        args = {
            "version": next_v,
            "source_v": source_v,
            "plan": _o._checkpoint_master_plan_arg(checkpoint),
        }
        from tool_gates import run_review
        return run_review.handler, args
    if next_tool == "run_critic":
        args = {
            "version": next_v,
            "source_v": source_v,
            "plan": _o._checkpoint_master_plan_arg(checkpoint),
            "reviewer_feedback": _o._checkpoint_reviewer_feedback(checkpoint),
            "force_advance": False,
        }
        from tool_gates import run_critic
        return run_critic.handler, args
    if next_tool == "run_precommit_eval":
        args = {"version": next_v, "source_v": source_v}
        from tool_eval import run_precommit_eval
        return run_precommit_eval.handler, args
    if next_tool == "commit_bot":
        args = {
            "version": next_v,
            "source_v": source_v,
            "strategy": _o._checkpoint_commit_strategy(checkpoint),
            "review_approved": True,
        }
        from tool_commit import commit_bot
        return commit_bot.handler, args
    if next_tool == "run_archivist":
        args = {"version": next_v, "source_v": source_v}
        from tool_commit import run_archivist
        return run_archivist.handler, args
    return None, None


def _coerce_event_ts(value) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return 0.0


def _pipeline_checkpoint_observation():
    """Read checkpoint bytes while preserving absent-vs-invalid authority.

    Resolves the checkpoint path through ``pipeline_state_path()`` so the
    existence/observation flags target the SAME slot-aware file as the
    override-aware ``read_pipeline_checkpoint()`` read.  Previously the
    existence flags used the hard-coded primary ``PIPELINE_STATE_FILE`` while
    the read honored the active slot override — a split-brain in which a draft
    task's absent draft slot was reported as a "claimed but unreadable" primary
    checkpoint.
    """

    try:
        from evolution_core import read_pipeline_checkpoint
        from evolution_infra import pipeline_state_path
    except Exception as exc:
        return {
            "checkpoint": None,
            "path_exists": None,
            "error": f"checkpoint_import_failed:{type(exc).__name__}",
        }
    _checkpoint_file = pipeline_state_path()
    path_exists_before = os.path.lexists(_checkpoint_file)
    try:
        checkpoint = read_pipeline_checkpoint()
    except Exception as exc:
        return {
            "checkpoint": None,
            "path_exists": os.path.lexists(_checkpoint_file),
            "path_existed_before": path_exists_before,
            "error": f"checkpoint_read_failed:{type(exc).__name__}",
        }
    path_exists = os.path.lexists(_checkpoint_file)
    if checkpoint is None:
        return {
            "checkpoint": None,
            "path_exists": path_exists,
            "path_existed_before": path_exists_before,
            "error": (
                "checkpoint_disappeared_during_read"
                if path_exists_before and not path_exists
                else "checkpoint_unreadable_or_invalid"
                if path_exists
                else None
            ),
        }
    if not isinstance(checkpoint, dict):
        return {
            "checkpoint": None,
            "path_exists": path_exists,
            "path_existed_before": path_exists_before,
            "error": "checkpoint_projection_not_object",
        }
    identity_issues = []
    _IDENTITY_FLOORS = {
        "next_v": FIRST_STRICT_POLICY_VERSION,
        "source_v": ARCHIVED_VERSION_HIGH_WATER,
        "checkpoint_revision": 1,
    }
    for field, floor in _IDENTITY_FLOORS.items():
        value = checkpoint.get(field)
        if type(value) is not int or value < floor:
            identity_issues.append(field)
    for field in ("stage", "workflow_run_id"):
        value = checkpoint.get(field)
        if not isinstance(value, str) or not value.strip():
            identity_issues.append(field)
    if identity_issues:
        return {
            "checkpoint": None,
            "path_exists": path_exists,
            "path_existed_before": path_exists_before,
            "error": (
                "checkpoint_projection_identity_invalid:"
                + ",".join(identity_issues)
            ),
        }
    return {
        "checkpoint": checkpoint,
        "path_exists": path_exists,
        "path_existed_before": path_exists_before,
        "error": None,
    }


def _read_active_pipeline_checkpoint():
    observation = _o._pipeline_checkpoint_observation()
    checkpoint = observation.get("checkpoint")
    return checkpoint if isinstance(checkpoint, dict) else None


def _read_structured_events_tail(max_bytes=None):
    """Read a bounded tail of the canonical structured-event ledger."""
    try:
        from event_bus import _events_file
        path = _events_file()
    except Exception:
        return []
    if path is None or not path.exists():
        return []
    limit = max(4096, int(max_bytes or _o.ORCH_EXTERNAL_PROGRESS_TAIL_BYTES))
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            start = max(0, size - limit)
            f.seek(start)
            if start > 0:
                f.readline()
            payload = f.read()
    except Exception:
        return []
    try:
        return payload.decode("utf-8", errors="replace").splitlines()
    except Exception:
        return []


def _event_matches_active_generation(event_data, checkpoint):
    if not checkpoint:
        return False
    expected_workflow_run_id = str(checkpoint.get("workflow_run_id") or "").strip()
    event_workflow_run_id = str(event_data.get("workflow_run_id") or "").strip()
    if expected_workflow_run_id:
        return event_workflow_run_id == expected_workflow_run_id

    expected_run_id = str(checkpoint.get("run_id") or "").strip()
    event_run_id = str(event_data.get("run_id") or "").strip()
    if expected_run_id:
        return event_run_id == expected_run_id

    expected_v_text = str(checkpoint.get("next_v") or "").strip()
    if not expected_v_text:
        return False
    for key in ("version", "next_v", "candidate_v", "target_v"):
        if str(event_data.get(key) or "").strip() == expected_v_text:
            return True

    log_file = str(event_data.get("log_file") or "")
    return f"/v{expected_v_text}/logs/" in log_file


_ORCH_EXTERNAL_PROGRESS_EVENT_TYPES = frozenset({
    "pipeline.llm_role_first_activity",
    "pipeline.llm_role_first_activity_delayed",
    "pipeline.llm_role_progress",
    "pipeline.master_checkpoint_heartbeat",
    "pipeline.orchestrator_native_match_extension_granted",
})


def _latest_orchestrator_external_progress(since_ts):
    """Return current-generation tool/sub-role progress newer than since_ts.

    The orchestrator main stream is silent while a local MCP tool executes.
    That silence is not an SDK stall if the active checkpoint or a sub-role log
    shows current-generation progress. Background daemon events are deliberately
    ignored so ratings or async queues cannot mask a stuck generation.
    """
    since = _o._coerce_event_ts(since_ts)
    checkpoint = _o._read_active_pipeline_checkpoint()
    best = None

    if checkpoint:
        from pipeline_state import pipeline_runtime_activity_ts

        checkpoint_ts = max(
            _o._coerce_event_ts(checkpoint.get("last_update_ts")),
            _o._coerce_event_ts(checkpoint.get("last_stage_change_ts")),
            pipeline_runtime_activity_ts(checkpoint),
        )
        if checkpoint_ts > since:
            best = {
                "ts": checkpoint_ts,
                "source": "checkpoint",
                "event_type": "pipeline.checkpoint_progress",
                "next_v": checkpoint.get("next_v"),
                "stage": checkpoint.get("stage"),
            }

    if not checkpoint:
        return best

    for line in _o._read_structured_events_tail():
        try:
            event = json.loads(line)
        except Exception:
            continue
        event_type = str(event.get("type") or "")
        if event_type not in _ORCH_EXTERNAL_PROGRESS_EVENT_TYPES:
            continue
        ts = _o._coerce_event_ts(event.get("ts"))
        if ts <= since:
            continue
        data = event.get("data") or {}
        if not isinstance(data, dict):
            data = {}
        emitter_proc = str(data.get("emitter_proc") or data.get("proc") or "")
        if emitter_proc and emitter_proc not in {"web", "orchestrator"}:
            continue
        if not _o._event_matches_active_generation(data, checkpoint):
            continue
        if best is None or ts > best["ts"]:
            best = {
                "ts": ts,
                "source": "system_event",
                "event_type": event_type,
                "message": str(event.get("message") or "")[:240],
                "next_v": checkpoint.get("next_v"),
                "stage": data.get("stage") or checkpoint.get("stage"),
                "role": data.get("role"),
                "log_file": data.get("log_file"),
            }
    return best


def _detect_actionable_stage_stall(timeout_sec=None):
    """Return checkpoint route data when a deterministic next-tool stage is stale."""
    timeout = _o.ORCH_ACTIONABLE_STAGE_TIMEOUT if timeout_sec is None else float(timeout_sec)
    if timeout <= 0 and timeout_sec is None:
        return None
    try:
        from evolution_core import read_pipeline_checkpoint
        checkpoint = read_pipeline_checkpoint()
    except Exception:
        return None
    if not checkpoint:
        return None
    stage = checkpoint.get("stage")
    from pipeline_state import pipeline_runtime_activity_ts

    last_ts = max(
        float(checkpoint.get("last_stage_change_ts") or 0.0),
        float(checkpoint.get("last_update_ts") or 0.0),
        pipeline_runtime_activity_ts(checkpoint),
    )
    if last_ts <= 0:
        return None
    elapsed = time.time() - last_ts
    if elapsed < timeout:
        return None
    route = _o._resolve_recovery_route(checkpoint)
    if not route:
        return None
    return {
        "next_v": checkpoint.get("next_v"),
        "source_v": checkpoint.get("source_v"),
        "stage": stage,
        "elapsed_sec": round(elapsed, 1),
        "timeout_sec": timeout,
        "next_tool": route.get("next_tool"),
        "directive": route.get("directive"),
        "checkpoint_actionable_identity": _o._checkpoint_actionable_identity(
            checkpoint
        ),
        "stream_owned_route_identity": (
            _o._checkpoint_stream_owned_route_identity(
                checkpoint,
                resolved_route=route,
            )
        ),
    }


def _checkpoint_actionable_identity(checkpoint):
    """Return the persisted identity that fences a provider-cycle handoff.

    A checkpoint already actionable when a fresh provider session starts is
    the work that session must execute.  Only a different revision/stage
    produced after the session began authorizes disposing that stream.
    """

    if not isinstance(checkpoint, dict):
        return None
    return (
        checkpoint.get("workflow_run_id"),
        checkpoint.get("checkpoint_revision"),
        checkpoint.get("stage"),
        checkpoint.get("next_v"),
        checkpoint.get("source_v"),
    )


def _checkpoint_stream_owned_route_identity(checkpoint, *, resolved_route=None):
    """Return the semantic route owned by an in-flight provider tool call.

    Long-running tools may publish runtime heartbeats or same-stage retry
    metadata while they still own the call.  Those updates must not make the
    orchestrator's idle poller treat the stage that the fresh stream was
    launched to execute as abandoned.  The route tool and intent are included
    because authoritative recovery policy can expose different tools for the
    same persisted stage as its bound gate metadata advances.
    """

    if not isinstance(checkpoint, dict):
        return None
    route = resolved_route
    if route is None:
        route = _o._resolve_recovery_route(checkpoint)
    if not isinstance(route, dict):
        return None
    policy = route.get("route") or {}
    if not isinstance(policy, dict):
        policy = {}
    return (
        checkpoint.get("workflow_run_id"),
        checkpoint.get("stage"),
        checkpoint.get("next_v"),
        checkpoint.get("source_v"),
        route.get("next_tool"),
        policy.get("intent"),
    )
