"""Shared helpers for MCP tool implementations.

UI injection, logging adapters, checkpoint gates, and validation utilities.
"""

import difflib
import json
import logging
import re
import time
from pathlib import Path

log = logging.getLogger("pok.tools")

from bot_namespace import (
    NATIONAL_ENTRYPOINT,
    bot_name as active_bot_name,
    parse_bot_version,
)
from evolution_core import (
    BaseUI,
    get_active_bots,
    get_bot_dir,
    load_ratings,
    write_pipeline_checkpoint,
    read_pipeline_checkpoint,
)
from evolution_infra import _target_rel, read_locked_json

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


# ──────────────────────────────────────────────
# UI Injection — Dashboard Integration
# ──────────────────────────────────────────────

_injected_ui = None


def inject_ui(ui):
    """Inject a real WebUI instance so tool events broadcast to Dashboard via SSE."""
    global _injected_ui
    _injected_ui = ui


def _get_ui():
    """Get UI instance: injected WebUI (Dashboard mode) or silent ToolUI (CLI mode)."""
    return _injected_ui if _injected_ui else ToolUI()


def _set_pipeline_status(msg, is_working=True):
    """Update WebUI status message for pipeline stage visibility."""
    _get_ui().set_status(msg, is_working=is_working)


# ──────────────────────────────────────────────
# Logging UI Adapter (CLI fallback)
# ──────────────────────────────────────────────

class ToolUI(BaseUI):
    """Silent UI adapter for CLI mode — captures output for tool results only."""

    def __init__(self):
        self.messages = []
        self.costs = []

    def log_history(self, msg, status="info"):
        self.messages.append(f"[{status}] {msg}")

    def set_status(self, msg, is_working=False):
        self.messages.append(f"[status] {msg}")

    def log_io(self, msg, stream_type="default", role=""):
        pass

    def clear_io(self):
        pass

    def update_eval_table(self, ratings, active_bots):
        pass

    def update_daemon_status(self, stats, ratings):
        pass

    def set_header(self, msg):
        pass

    def update_cost(self, role, cost_usd, usage):
        if cost_usd is not None:
            self.costs.append({"role": role, "cost_usd": cost_usd})

    def update_metrics(self, metrics):
        pass

    def get_output(self):
        return "\n".join(self.messages[-20:])


# ──────────────────────────────────────────────
# Common Helpers
# ──────────────────────────────────────────────

def _ratings_summary(ratings, n=10):
    """Get top N bots as a compact summary, sorted by unified strength."""
    strength_scores = load_strength_scores()
    h2h_winrates = load_h2h_avg_winrates()
    sorted_bots = sorted(
        [(name, p) for name, p in ratings.items()],
        key=lambda x: strength_scores.get(x[0], 0.0), reverse=True,
    )[:n]
    return [
        {
            "name": name,
            "r": round(p.r, 1),
            "rd": round(p.rd, 1),
            "leaderboard_score": round(strength_scores.get(name, 0.0), 4),
            "h2h_avg_wr": round(h2h_winrates.get(name, 0.0), 4),
        }
        for name, p in sorted_bots
    ]


def _json_tool_result(data):
    return {"content": [{"type": "text", "text": json.dumps(data, indent=2, ensure_ascii=False)}]}


def _read_json(path, default):
    result = read_locked_json(path, default)
    if result is default and Path(path).exists():
        log.warning("_read_json: corrupt JSON in %s, returning default", path)
    return result


def _resolve_version_args(args):
    """Get version/source_v from args, falling back to active pipeline checkpoint.

    Prevents KeyError death spiral when the orchestrator LLM calls a tool
    without providing version/source_v parameters.
    """
    v = args.get("version") or args.get("next_v")
    source_v = args.get("source_v")
    if v is None or source_v is None:
        ckpt = read_pipeline_checkpoint()
        if ckpt:
            v = v or ckpt.get("next_v")
            source_v = source_v or ckpt.get("source_v")
    return v, source_v


def _matching_checkpoint(version, source_v=None):
    ckpt = read_pipeline_checkpoint()
    if not ckpt or ckpt.get("next_v") != version:
        return None
    if source_v is not None and ckpt.get("source_v") != source_v:
        return None
    return ckpt


def _critic_result_to_preserve(checkpoint):
    """Return the current Critic result a replacement gate will preserve."""

    if not isinstance(checkpoint, dict):
        return None
    existing = (checkpoint.get("gate_results") or {}).get("critic")
    if not isinstance(existing, dict):
        return None
    try:
        has_score = float(existing.get("score", 0) or 0) > 0
    except (TypeError, ValueError):
        has_score = False
    return existing if has_score else None


def _previous_critic_result(checkpoint):
    """Reconstruct the exact previous-Critic value used by its provider call."""

    current = _critic_result_to_preserve(checkpoint)
    if str((checkpoint or {}).get("stage") or "") == "reviewed":
        # Pre-dispatch (and crash-before-projection) state: the current gate is
        # what _record_gate will preserve as the new result's prev_critic.
        return current
    if not isinstance(current, dict):
        return None
    previous = current.get("prev_critic")
    return previous if isinstance(previous, dict) else None


def _record_gate(version, source_v, gate_name, gate_data, stage=None,
                 master_plan=None, reviewer_feedback=None, generation_attempt=None,
                 infra_failure=None, clear_infra_failure=False,
                 infra_failure_owner=None,
                 expected_infra_failure_digest=None, record_gate=True):
    ckpt = _matching_checkpoint(version, source_v)
    if not ckpt:
        log.warning("_record_gate: no matching checkpoint for v%s/v%s, gate '%s' dropped", version, source_v, gate_name)
        try:
            from system_log import log_system_event
            log_system_event(
                "pipeline.gate_record_dropped", "error",
                f"Gate '{gate_name}' dropped because no matching checkpoint exists for v{version}/source v{source_v}",
                {"version": version, "source_v": source_v, "gate": gate_name},
            )
        except Exception:
            pass
        return False
    current_stage = ckpt.get("stage", "")
    # Preserve previous critic result when overwriting with a new one
    if gate_name == "critic":
        existing_critic = _critic_result_to_preserve(ckpt)
        if existing_critic is not None:
            gate_data = {**gate_data, "prev_critic": existing_critic}
    # Use provided generation_attempt or preserve existing
    if generation_attempt is None:
        generation_attempt = ckpt.get("generation_attempt", 0)
    gate_artifact_hash = ""
    if isinstance(gate_data, dict):
        gate_artifact_hash = str(gate_data.get("code_fingerprint") or "")
        if not gate_artifact_hash:
            identity = gate_data.get("certification_identity")
            if isinstance(identity, dict):
                gate_artifact_hash = str(identity.get("candidate_hash") or "")
    recorded = write_pipeline_checkpoint(
        version,
        source_v,
        stage or current_stage,
        master_plan=master_plan if master_plan is not None else ckpt.get("master_plan"),
        reviewer_feedback=(
            reviewer_feedback
            if reviewer_feedback is not None
            else ckpt.get("reviewer_feedback", "")
        ),
        generation_attempt=generation_attempt,
        gate_results={gate_name: gate_data} if record_gate else None,
        direction_audit=ckpt.get("direction_audit"),
        infra_failure=infra_failure,
        clear_infra_failure=clear_infra_failure,
        infra_failure_owner=infra_failure_owner,
        expected_infra_failure_digest=expected_infra_failure_digest,
        repair_baseline_artifact_hash=(gate_artifact_hash or None),
    )
    if not recorded:
        log.warning(
            "_record_gate: checkpoint rejected gate '%s' for v%s/v%s at stage '%s'",
            gate_name, version, source_v, stage or current_stage,
        )
        try:
            from system_log import log_system_event
            log_system_event(
                "pipeline.gate_record_rejected",
                "error",
                f"Gate '{gate_name}' was rejected by checkpoint state machine for v{version}/source v{source_v}",
                {
                    "version": version,
                    "source_v": source_v,
                    "gate": gate_name,
                    "requested_stage": stage or current_stage,
                    "current_stage": current_stage,
                },
            )
        except Exception:
            pass
    return bool(recorded)


def _owned_infrastructure_failure(checkpoint, owner_tool):
    """Return ``(failure, error)`` for a tool-owned recovery overlay."""
    failure = checkpoint.get("infra_failure") if isinstance(checkpoint, dict) else None
    if not isinstance(failure, dict):
        return None, None
    from pipeline_infrastructure import validate_infrastructure_failure

    errors = validate_infrastructure_failure(failure)
    if errors:
        return failure, "invalid infrastructure overlay: " + "; ".join(errors[:5])
    if failure.get("owner_tool") != owner_tool:
        return failure, (
            f"infrastructure recovery is owned by {failure.get('owner_tool')}; "
            f"{owner_tool} cannot consume it"
        )
    return failure, None


def _prepare_official_profile_refresh(checkpoint, tool_name):
    """Cancel an attached EXE job before profile-driven gate revalidation."""
    if not isinstance(checkpoint, dict) or checkpoint.get("stage") != "official_certifying":
        return {"ok": True, "needed": False}
    from pipeline_state import route_policy

    route = route_policy(checkpoint)
    if route.get("intent") not in {"quality_profile_refresh", "precommit_profile_refresh"}:
        return {"ok": True, "needed": False}
    if route.get("next_tool") != tool_name:
        return {
            "ok": False,
            "needed": True,
            "error": (
                f"official profile refresh is owned by {route.get('next_tool')}; "
                f"{tool_name} cannot run first"
            ),
            "route": route,
        }
    attachment = checkpoint.get("official_job") or {}
    job_id = str(attachment.get("job_id") or "")
    if not job_id:
        return {"ok": True, "needed": True, "job_state": "missing_attachment"}
    try:
        from official_certification_job import cancel_job

        cancelled = cancel_job(job_id, reason=f"workflow_profile_refresh:{tool_name}")
    except Exception as exc:
        return {
            "ok": False,
            "needed": True,
            "error": f"official job cancellation failed: {type(exc).__name__}: {str(exc)[:240]}",
        }
    state = str(cancelled.get("state") or "")
    if state not in {"cancelled", "completed", "failed", "missing"}:
        return {
            "ok": False,
            "needed": True,
            "error": f"official job did not reach a terminal state before profile refresh: {state}",
            "job": cancelled,
        }
    return {"ok": True, "needed": True, "job_state": state, "job": cancelled}


async def _execute_exhausted_infrastructure_failure(
    version,
    source_v,
    *,
    owner_tool,
):
    """Execute centralized abandonment for an exhausted owned overlay."""
    checkpoint = _matching_checkpoint(version, source_v)
    failure, error = _owned_infrastructure_failure(checkpoint, owner_tool)
    if error:
        return {
            "state_blocked": True,
            "failure_class": "infrastructure",
            "error": error,
            "infra_failure": failure,
        }
    if not failure or not failure.get("exhausted"):
        return None
    from tool_bot_management import _do_abandon_generation

    abandon_result = await _do_abandon_generation(
        reason=f"infrastructure_exhausted:{failure.get('component')}",
        expected_workflow_run_id=checkpoint.get("workflow_run_id"),
        expected_next_v=checkpoint.get("next_v"),
        expected_source_v=checkpoint.get("source_v"),
        expected_checkpoint_revision=checkpoint.get("checkpoint_revision"),
        expected_checkpoint_stage=checkpoint.get("stage"),
    )
    return {
        "failure_class": "infrastructure",
        "action": "abandon_generation",
        "infra_failure": failure,
        "abandon_result": abandon_result,
        "abandoned": bool(abandon_result.get("abandoned")),
    }


async def _record_infrastructure_failure(
    version,
    source_v,
    *,
    owner_tool,
    resume_stage,
    component,
    code,
    attempt_key,
    issues,
    max_attempts=3,
    metadata=None,
    cumulative_attempt_field=None,
    master_plan=None,
    reviewer_feedback=None,
):
    """Advance one identity-bound retry and abandon when its budget expires."""
    from pipeline_infrastructure import build_infrastructure_failure
    from pipeline_infrastructure import infrastructure_failure_digest

    overlay = None
    recorded = False
    for _cas_attempt in range(5):
        checkpoint = _matching_checkpoint(version, source_v)
        if not checkpoint:
            return {
                "state_blocked": True,
                "failure_class": "infrastructure",
                "error": "no matching checkpoint for infrastructure failure",
            }
        existing, error = _owned_infrastructure_failure(checkpoint, owner_tool)
        if error:
            return {
                "state_blocked": True,
                "failure_class": "infrastructure",
                "error": error,
                "infra_failure": existing,
            }
        overlay_metadata = dict(metadata or {})
        cumulative_attempt = None
        if cumulative_attempt_field:
            previous_metadata = (
                existing.get("metadata")
                if isinstance(existing, dict)
                and isinstance(existing.get("metadata"), dict)
                else {}
            )
            try:
                previous_cumulative = int(
                    previous_metadata.get(cumulative_attempt_field) or 0
                )
            except (TypeError, ValueError):
                previous_cumulative = 0
            cumulative_attempt = min(max_attempts, previous_cumulative + 1)
            overlay_metadata[cumulative_attempt_field] = cumulative_attempt
        overlay = build_infrastructure_failure(
            existing,
            component=component,
            code=code,
            owner_tool=owner_tool,
            resume_stage=resume_stage,
            attempt_key=attempt_key,
            issues=list(issues or []),
            max_attempts=max_attempts,
            metadata=overlay_metadata,
        )
        if cumulative_attempt is not None:
            # Some owners intentionally share one retry budget across several
            # probe components. Generic identity attempts reset when component,
            # code, or attempt_key changes; overwrite only the attempt ledger
            # with this owner-scoped monotonic counter while preserving the
            # component as diagnostic detail.
            overlay["attempt"] = cumulative_attempt
            overlay["max_attempts"] = max_attempts
            overlay["exhausted"] = cumulative_attempt >= max_attempts
            overlay["retryable"] = not overlay["exhausted"]
            overlay["action"] = (
                "abandon_generation" if overlay["exhausted"] else "retry_same_tool"
            )
            if isinstance(existing, dict) and existing.get("first_seen_at"):
                overlay["first_seen_at"] = existing["first_seen_at"]
            overlay["identity_digest"] = infrastructure_failure_digest(overlay)
        recorded = _record_gate(
            version,
            source_v,
            owner_tool,
            {},
            stage=resume_stage,
            master_plan=master_plan,
            reviewer_feedback=reviewer_feedback,
            infra_failure=overlay,
            expected_infra_failure_digest=infrastructure_failure_digest(existing),
            record_gate=False,
        )
        if recorded:
            break
    if not recorded or overlay is None:
        return {
            "state_blocked": True,
            "failure_class": "infrastructure",
            "error": "infrastructure overlay compare-and-swap did not converge",
            "checkpoint_recorded": False,
        }
    result = {
        "failure_class": "infrastructure",
        "action": overlay["action"],
        "infra_failure": overlay,
        "checkpoint_recorded": bool(recorded),
        "abandoned": False,
    }
    if overlay["exhausted"] and recorded:
        from tool_bot_management import _do_abandon_generation

        abandon_checkpoint = _matching_checkpoint(version, source_v)
        if not isinstance(abandon_checkpoint, dict):
            result["abandon_result"] = {
                "abandoned": False,
                "reason": "forced_abandon_checkpoint_identity_unavailable",
            }
            return result
        abandon_result = await _do_abandon_generation(
            reason=f"infrastructure_exhausted:{component}",
            expected_workflow_run_id=abandon_checkpoint.get("workflow_run_id"),
            expected_next_v=abandon_checkpoint.get("next_v"),
            expected_source_v=abandon_checkpoint.get("source_v"),
            expected_checkpoint_revision=abandon_checkpoint.get(
                "checkpoint_revision"
            ),
            expected_checkpoint_stage=abandon_checkpoint.get("stage"),
        )
        result["abandon_result"] = abandon_result
        result["abandoned"] = bool(abandon_result.get("abandoned"))
    return result


def _gate_payload(version, source_v, passed, **extra):
    return {
        "version": version,
        "source_v": source_v,
        "passed": bool(passed),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        **extra,
    }


def _state_blocked(message, version, source_v=None, checkpoint=None):
    # Compact gate summary instead of full gate_results (saves ~600+ tokens)
    gate_summary = {}
    if checkpoint:
        for name, gate in (checkpoint.get("gate_results") or {}).items():
            gate_summary[name] = {"passed": gate.get("passed")}
            if gate.get("score") is not None:
                gate_summary[name]["score"] = gate.get("score")
    return _json_tool_result({
        "error": f"STATE BLOCKED: {message}",
        "version": version,
        "source_v": source_v,
        "checkpoint_stage": checkpoint.get("stage") if checkpoint else None,
        "gate_summary": gate_summary,
    })


def _checkpoint_gate(checkpoint, gate_name):
    if not checkpoint:
        return {}
    return (checkpoint.get("gate_results", {}) or {}).get(gate_name, {}) or {}


def _active_workflow_profile_info():
    from workflow_profiles import get_workflow_profile

    profile = get_workflow_profile()
    return (
        getattr(profile, "profile_id", ""),
        getattr(profile, "national_execution_mode", "native_tcp"),
    )


def _gate_matches_active_workflow(checkpoint, gate):
    """Reject cached gate results produced under another workflow profile.

    Profile drift is especially dangerous for the national-native migration:
    a candidate that passed the old adapter-backed national gate must not be
    allowed to proceed as if it had passed the direct TCP native gate.
    """
    active_profile_id, active_execution_mode = _active_workflow_profile_info()
    if not active_profile_id:
        return True

    checkpoint_profile_id = str((checkpoint or {}).get("workflow_profile_id") or "")
    if checkpoint_profile_id and checkpoint_profile_id != active_profile_id:
        return False

    gate_profile_id = str(gate.get("workflow_profile_id") or gate.get("profile_id") or "")
    if gate_profile_id and gate_profile_id != active_profile_id:
        return False
    if not gate_profile_id and active_profile_id != "default":
        return False

    gate_execution_mode = str(gate.get("national_execution_mode") or "")
    if active_execution_mode == "native_tcp":
        return (
            gate_execution_mode == "native_tcp"
            and gate.get("national_native_contract_ok") is True
        )
    if gate_execution_mode and gate_execution_mode != active_execution_mode:
        return False
    return True


def _quality_gate_ok(checkpoint):
    quality = _checkpoint_gate(checkpoint, "quality")
    return (
        quality.get("all_passed") is True
        and quality.get("critical_scenarios_passed") is True
        and _gate_matches_active_workflow(checkpoint, quality)
    )


def _review_gate_ok(checkpoint):
    review = _checkpoint_gate(checkpoint, "review")
    if (
        review.get("approved") is not True
        or review.get("llm_failed")
        or review.get("parse_failed")
        or review.get("llm_invoked") is not True
        or review.get("reviewer_llm_executed") is not True
        or review.get("schema_valid") is not True
    ):
        return False
    try:
        from system_strict_bootstrap import is_declared_native_bootstrap

        declared = is_declared_native_bootstrap(checkpoint)
    except Exception:
        declared = False
    if declared:
        try:
            from system_strict_bootstrap import validate_system_gate_receipt

            return not validate_system_gate_receipt(
                checkpoint,
                gate_name="review",
            )
        except Exception:
            return False
    return True


def _critic_gate_ok(checkpoint):
    critic = _checkpoint_gate(checkpoint, "critic")
    if (
        critic.get("approved") is not True
        or critic.get("llm_failed")
        or critic.get("parse_failed")
        or critic.get("llm_invoked") is not True
        or critic.get("critic_llm_executed") is not True
        or critic.get("schema_valid") is not True
    ):
        return False
    try:
        from system_strict_bootstrap import is_declared_native_bootstrap

        declared = is_declared_native_bootstrap(checkpoint)
    except Exception:
        declared = False
    if declared:
        try:
            from system_strict_bootstrap import validate_system_gate_receipt

            return not validate_system_gate_receipt(
                checkpoint,
                gate_name="critic",
            )
        except Exception:
            return False
    # The verdict remains advisory; successful schema-valid execution does not.
    return True


def _bot_entry(bot_name):
    """Return only the strict national TCP policy-artifact entrypoint."""
    version = parse_bot_version(str(bot_name))
    if version is None:
        return PROJECT_ROOT / "bots" / str(bot_name) / NATIONAL_ENTRYPOINT
    return PROJECT_ROOT / "bots" / active_bot_name(version) / NATIONAL_ENTRYPOINT


def _load_h2h_data():
    from evaluation_data_identity import ensure_evaluation_data_identity
    import evolution_infra

    results_dir = evolution_infra.RESULTS_DIR
    ensure_evaluation_data_identity(results_dir)
    return _read_json(results_dir / "head_to_head.json", {})


def _h2h_stats(bot_name, opponent, h2h):
    for key, value in h2h.items():
        parts = key.split(" vs ")
        if len(parts) != 2 or bot_name not in parts or opponent not in parts:
            continue
        a, b = parts
        games = value.get("games", 0)
        if games <= 0:
            return None
        bot_wins = value.get("a_wins", 0) if bot_name == a else value.get("b_wins", 0)
        opp_wins = value.get("b_wins", 0) if bot_name == a else value.get("a_wins", 0)
        draws = value.get("draws", 0)
        return {
            "wins": bot_wins,
            "losses": opp_wins,
            "draws": draws,
            "games": games,
            "win_rate": (bot_wins + 0.5 * draws) / games,
        }
    return None


def compute_h2h_avg_winrate(bot_name, h2h_data):
    """Equal-weighted average win rate across all H2H opponents.

    Draws count as half a win, matching the Glicko update semantics.
    """
    from rating_snapshot import h2h_winrate_for_bot
    return h2h_winrate_for_bot(bot_name, h2h_data)


def _batch_compute_h2h_winrates(h2h_data, active_bots):
    """Compute H2H avg win rates for all active bots in a single pass over h2h_data.

    Returns dict mapping bot_name -> list of per-opponent win rates (for averaging).
    """
    bot_rates = {name: [] for name in active_bots}
    for key, value in h2h_data.items():
        parts = key.split(" vs ")
        if len(parts) != 2:
            continue
        a, b = parts
        games = value.get("games", 0)
        if games <= 0:
            continue
        if a in bot_rates:
            bot_rates[a].append((value.get("a_wins", 0) + 0.5 * value.get("draws", 0)) / games)
        if b in bot_rates:
            bot_rates[b].append((value.get("b_wins", 0) + 0.5 * value.get("draws", 0)) / games)
    return bot_rates


def _match_history_file():
    import evolution_infra
    return evolution_infra.MATCH_HISTORY_FILE


def _rating_rows_for_active():
    import evolution_infra
    from evaluation_bundle import validated_evaluation_identity_digest
    from rating_snapshot import build_strength_rows
    h2h_data = _load_h2h_data()
    bot_stats_data = _read_json(evolution_infra.RESULTS_DIR / "bot_stats.json", {})
    ratings = load_ratings()
    active = list(get_active_bots())
    evaluation_identity = validated_evaluation_identity_digest(
        evolution_infra.RESULTS_DIR
    )
    return build_strength_rows(
        ratings,
        bot_stats_data,
        h2h_data,
        active_bots=active,
        match_history_path=_match_history_file(),
        expected_evaluation_identity_digest=evaluation_identity,
    )


def load_strength_scores():
    """Load unified leaderboard strength scores for active bots."""
    rows = _rating_rows_for_active()
    if rows:
        return {row["name"]: row["leaderboard_score"] for row in rows}
    return {name: 0.5 for name in get_active_bots()}


def load_selection_scores():
    """Load confidence-discounted scores for evolution mechanics."""
    rows = _rating_rows_for_active()
    if rows:
        return {
            row["name"]: row.get("selection_score", row.get("leaderboard_score", 0.5))
            for row in rows
        }
    return {name: 0.5 for name in get_active_bots()}


def load_selection_order_keys():
    """Load lexicographic primary-result/secondary-chip ordering keys."""

    from strength_order import strength_order_key

    rows = _rating_rows_for_active()
    if rows:
        return {row["name"]: strength_order_key(row) for row in rows}
    return {name: (0.5, float("-inf")) for name in get_active_bots()}


def load_h2h_avg_winrates():
    """Load H2H avg win rates for all active bots from the unified snapshot.

    Returns dict mapping bot_name -> float (average win rate across H2H opponents).
    """
    rows = _rating_rows_for_active()
    result = {}
    for row in rows:
        bot_name = row["name"]
        if row.get("h2h_avg_wr") is not None:
            result[bot_name] = row["h2h_avg_wr"]
        else:
            result[bot_name] = row.get("win_rate") if row.get("win_rate") is not None else row.get("leaderboard_score", 0.5)
    return result


def _batch_compute_opponent_coverage(h2h_data, active_bots):
    """Compute opponent coverage for all active bots in a single pass."""
    active_set = set(active_bots)
    opponent_counts = {name: 0 for name in active_set}
    for key, value in h2h_data.items():
        parts = key.split(" vs ")
        if len(parts) != 2:
            continue
        a, b = parts
        if value.get("games", 0) > 0:
            if a in active_set and b in active_set:
                opponent_counts[a] += 1
                opponent_counts[b] += 1
    return opponent_counts


def strength_row_to_analysis_view(row):
    """Normalize a canonical strength row for analyst-facing consumers.

    ``rating_snapshot.build_strength_rows`` owns the canonical ``h2h_*``
    field names.  Older analyst code expects the more descriptive
    ``opponent_*`` aliases.  Keep that translation in one place so a frozen
    generation snapshot and the live dashboard path cannot silently disagree
    about coverage.
    """
    return {
        "h2h_avg_wr": (
            row.get("h2h_avg_wr")
            if row.get("h2h_avg_wr") is not None
            else 0.5
        ),
        "leaderboard_score": row.get("leaderboard_score", 0.5),
        "selection_score": row.get(
            "selection_score", row.get("leaderboard_score", 0.5)
        ),
        "selection_penalty": row.get("selection_penalty", 0.0),
        "primary_70_hand_match_score": row.get("primary_70_hand_match_score"),
        "secondary_net_chips_total": row.get("secondary_net_chips_total"),
        "secondary_net_chips_mean": row.get("secondary_net_chips_mean"),
        "strength_sample_count": row.get("strength_sample_count", 0),
        "strength_order_contract": row.get("strength_order_contract", []),
        "rank_basis": row.get("rank_basis", ""),
        "strength_confidence": row.get("strength_confidence", "low"),
        "strength_note": row.get("strength_note", ""),
        "h2h_source": row.get("h2h_source", "head_to_head"),
        "opponent_coverage": row.get("h2h_coverage", 0.0),
        "opponents_evaluated": row.get("h2h_opponents", 0),
        "opponents_total": row.get("h2h_opponents_total", 0),
        "h2h_games": row.get("h2h_games", 0),
    }


def load_h2h_avg_winrates_with_coverage():
    """Like load_h2h_avg_winrates but returns coverage metadata per bot."""
    rows = _rating_rows_for_active()
    result = {}
    for row in rows:
        bot_name = row["name"]
        result[bot_name] = strength_row_to_analysis_view(row)
    return result


def _select_precommit_opponents(version, source_v, max_top=2, max_weak=1):
    """Select 1 parent + up to 2 leaders + 1 weakness from one frozen cutoff.

    Generation preparation owns the evaluation cutoff.  Precommit may run much
    later, so this function must not reopen live ratings, H2H, match history, or
    rolling advisory archives.  Missing or invalid generation evidence fails
    closed by returning no opponents; ``run_precommit_eval`` then records the
    explicit ``no_opponents`` blocker.
    """
    candidate = active_bot_name(version)
    parent = active_bot_name(source_v)
    try:
        from evidence_snapshot import load_generation_evaluation_snapshot

        snapshot = load_generation_evaluation_snapshot(version)
    except Exception as exc:
        snapshot = {
            "available": False,
            "reason": f"snapshot_load_failed:{type(exc).__name__}",
        }
    if not snapshot.get("available"):
        try:
            from system_log import log_system_event

            log_system_event(
                "pipeline.precommit_opponent_snapshot_unavailable",
                "error",
                f"Frozen opponent evidence unavailable for {candidate}",
                {
                    "candidate": candidate,
                    "parent": parent,
                    "reason": snapshot.get("reason"),
                    "issues": list(snapshot.get("issues") or [])[:10],
                },
            )
        except Exception:
            pass
        return []

    selection = snapshot.get("selection") or {}
    rows = selection.get("rows") or []
    if not isinstance(rows, list):
        return []
    row_by_name = {
        str(row.get("name")): dict(row)
        for row in rows
        if isinstance(row, dict) and str(row.get("name") or "")
    }
    frozen_active = [str(name) for name in selection.get("active_bots") or []]
    if not frozen_active or set(frozen_active) != set(row_by_name):
        return []
    active = [
        name
        for name in frozen_active
        if name != candidate and _bot_entry(name).exists()
    ]
    active_set = set(active)
    h2h = snapshot.get("h2h") or {}
    snapshot_manifest = snapshot.get("manifest") or {}

    selected = []
    reasons = {}

    def add(name, reason):
        # A source may remain usable as immutable migration input after it has
        # been quarantined from execution.  Precommit opponents must come from
        # the current executable active pool; the first-strict transition adds
        # its typed system control in tool_eval instead.
        if (
            name == candidate
            or name not in active_set
            or name in selected
            or not _bot_entry(name).exists()
        ):
            return
        selected.append(name)
        reasons[name] = reason

    add(parent, "parent")
    if parent not in selected:
        return []

    from strength_order import strength_order_key

    strength_scores = {
        name: float(row.get("leaderboard_score", 0.0) or 0.0)
        for name, row in row_by_name.items()
    }
    selection_scores = {
        name: float(row.get("selection_score", strength_scores.get(name, 0.0)) or 0.0)
        for name, row in row_by_name.items()
    }
    top = sorted(
        active,
        key=lambda name: (
            strength_order_key(row_by_name.get(name, {})),
            parse_bot_version(name) or -1,
        ),
        reverse=True,
    )
    for name in top[:max_top]:
        add(name, "top_strength")

    source_name = parent
    weak = []
    for opp in active:
        stats = _h2h_stats(source_name, opp, h2h)
        if stats and stats["win_rate"] < 0.40:
            weak.append((stats["win_rate"], opp))
    for _, name in sorted(weak)[:max_weak]:
        add(name, "source_h2h_weakness")

    try:
        from system_log import log_system_event
        details = []
        for name in selected:
            cov = strength_row_to_analysis_view(row_by_name.get(name, {}))
            pair_stats = _h2h_stats(parent, name, h2h) if name != parent else None
            details.append({
                "name": name,
                "reason": reasons.get(name),
                "leaderboard_score": round(strength_scores.get(name, 0.0), 4),
                "selection_score": round(selection_scores.get(name, strength_scores.get(name, 0.0)), 4),
                "h2h_avg_wr": round(cov.get("h2h_avg_wr", 0.0), 4),
                "h2h_coverage": round(cov.get("opponent_coverage", 0.0), 4),
                "h2h_games": cov.get("h2h_games", 0),
                "strength_confidence": cov.get("strength_confidence", "low"),
                "h2h_source": cov.get("h2h_source", "generation_evidence_snapshot"),
                "pair_vs_parent": pair_stats,
            })
        log_system_event(
            "pipeline.precommit_opponents_selected",
            "info",
            f"Selected {len(selected)} precommit opponents for {candidate}",
            {
                "candidate": candidate,
                "parent": parent,
                "h2h_source": "generation_evidence_snapshot",
                "evidence_manifest_digest": snapshot_manifest.get("manifest_digest"),
                "evaluation_identity_digest": snapshot_manifest.get(
                    "evaluation_identity_digest"
                ),
                "opponents": details,
            },
        )
    except Exception:
        pass

    return [{"name": name, "reason": reasons[name]} for name in selected]




def _py_files_changed_between(source_dir, next_dir):
    if not next_dir.exists():
        return []
    rels = set()
    for base in (source_dir, next_dir):
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            rels.add(path.relative_to(base).as_posix())

    changed = []
    for rel in sorted(rels):
        src = source_dir / rel
        dst = next_dir / rel
        src_text = src.read_text() if src.exists() else ""
        dst_text = dst.read_text() if dst.exists() else ""
        if src_text != dst_text:
            changed.append(rel)
    return changed


_NUMERIC_LITERAL_RE = re.compile(
    r"(?<![A-Za-z_])[-+]?(?:\d+\.\d+|\d+|\.\d+)(?:[eE][-+]?\d+)?"
)


def _numbers_only_changed(before, after):
    return _NUMERIC_LITERAL_RE.sub("<NUM>", before) == _NUMERIC_LITERAL_RE.sub("<NUM>", after)


def normalize_worker_role(role):
    """Normalize a worker role string into a canonical category.

    Returns one of 'architect', 'tuner', 'other'. Case-insensitive substring
    matching so that all Tuner variants ('Tuner', 'HP Tuner', 'Hyperparameter
    Tuner', etc.) collapse to 'tuner', matching the planning-layer logic in
    tool_planning._validate_master_plan. Tuner is checked before Architect so
    that a mixed role string (e.g. 'Hyperparameter Tuner (Architect-assisted)')
    resolves to the stricter 'tuner' boundary rather than escaping it. Unknown/
    empty roles resolve to 'other' without raising, so callers can treat any
    LLM-emitted role safely.
    """
    role = str(role or "").lower()
    if "tuner" in role or "hyperparameter" in role or role == "hp tuner":
        return "tuner"
    if "architect" in role:
        return "architect"
    return "other"


def _validate_worker_boundaries(
    tasks,
    source_v,
    next_v,
    worker_snapshots=None,
    *,
    candidate_dir=None,
    source_artifact_inherited=True,
):
    """Validate that workers respected their role boundaries.

    Args:
        tasks: List of worker task dicts with role, target_files, etc.
        source_v: Source bot version number.
        next_v: Target bot version number.
        worker_snapshots: Optional dict mapping (task_idx, file_rel) -> file content
            before that worker ran. Enables accurate per-worker boundary checking
            when multiple workers share a target file.
    """
    # The one-time v142 -> v143 transition carries only a numeric high-water
    # identity.  Its historical bot path is not a fallback baseline and must
    # never be resolved, stat'ed, or read.  The strict system Worker always
    # supplies exact per-task snapshots; missing snapshots therefore fail
    # closed below instead of reopening numeric-only lineage bytes.
    source_dir = get_bot_dir(source_v) if source_artifact_inherited else None
    next_dir = Path(candidate_dir) if candidate_dir is not None else get_bot_dir(next_v)
    all_targets = set()
    errors = []

    for task in tasks:
        for target in task.get("target_files", []):
            rel = _target_rel(target, next_v)
            if rel:
                all_targets.add(rel)

    # Without per-worker snapshots, fall back to the historical whole-candidate
    # source diff. With snapshots, per-worker boundary checks have already
    # isolated each worker's actual writes. Re-running the whole-candidate diff
    # during in-place quality repair would falsely blame earlier sibling repair
    # edits on the current one.
    if not worker_snapshots and source_dir is None:
        errors.append({
            "type": "worker_boundary_baseline_missing",
            "message": (
                "Numeric-only lineage requires exact Worker preimage snapshots; "
                "historical source bytes are not an admissible fallback."
            ),
        })
    elif not worker_snapshots:
        changed_files = _py_files_changed_between(source_dir, next_dir)
        for rel in changed_files:
            if rel not in all_targets:
                errors.append({
                    "type": "target_file_violation",
                    "file": rel,
                    "message": "Worker modified a Python file outside declared target_files.",
                })

        # Check for new files created outside target_files
        if source_dir.exists() and next_dir.exists():
            source_files = {p.relative_to(source_dir).as_posix() for p in source_dir.rglob("*.py")}
            next_files = {p.relative_to(next_dir).as_posix() for p in next_dir.rglob("*.py")}
            new_files = next_files - source_files
            for rel in new_files:
                if rel not in all_targets:
                    errors.append({
                        "type": "new_file_violation",
                        "file": rel,
                        "message": "Worker created a new file outside declared target_files.",
                    })

    for task_idx, task in enumerate(tasks):
        role = str(task.get("role", ""))
        if normalize_worker_role(role) != "tuner":
            continue
        for target in task.get("target_files", []):
            rel = _target_rel(target, next_v)
            if not rel:
                continue
            # Use worker snapshot if available: this compares the file state BEFORE
            # this worker ran vs AFTER, isolating this worker's changes from those
            # of preceding workers (who may have modified the same shared file).
            # Falls back to source version for backward compatibility.
            if worker_snapshots and (task_idx, rel) in worker_snapshots:
                before = worker_snapshots[(task_idx, rel)]
            else:
                if source_dir is None:
                    errors.append({
                        "type": "worker_boundary_snapshot_missing",
                        "file": rel,
                        "message": (
                            "Numeric-only lineage has no readable source fallback "
                            "for this Worker target."
                        ),
                    })
                    continue
                src = source_dir / rel
                before = src.read_text() if src.exists() else ""
            dst = next_dir / rel
            after = dst.read_text() if dst.exists() else ""
            if before != after and not _numbers_only_changed(before, after):
                diff = "\n".join(difflib.unified_diff(
                    before.splitlines(),
                    after.splitlines(),
                    fromfile=f"v{source_v}/{rel}",
                    tofile=f"v{next_v}/{rel}",
                    lineterm="",
                ))
                errors.append({
                    "type": "hyperparameter_boundary_violation",
                    "file": rel,
                    "message": "Hyperparameter Tuner changed non-numeric text or structure.",
                    "diff_excerpt": diff[:1200],
                })

    return errors
