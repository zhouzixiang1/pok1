"""Authoritative pipeline state machine helpers.

Stages remain persisted as strings for backward compatibility. Centralizing the
stage order, route policy, and generic-abandon guard prevents the orchestrator
prompt, MCP tools, and recovery code from drifting into contradictory rules.
"""

from failure_classification import classify_precommit_gate


STAGE_ORDER = [
    "selected",
    "preparing",
    "prepared",
    "crossover_running",
    "direction_audited",
    "master_planned",
    "workers_done",
    "quality_failed",
    "quality_passed",
    "reviewed",
    "critic_checked",
    "precommit_failed",
    "repair_planned",
    "rework_running",
    "verified",
    "archived",
]

STAGE_GATE_ALLOWLIST = {
    "selected": set(),
    "preparing": set(),
    "prepared": set(),
    "crossover_running": set(),
    "direction_audited": set(),
    "master_planned": set(),
    "workers_done": set(),
    "quality_failed": {"quality"},
    "quality_passed": {"quality"},
    "reviewed": {"quality", "review"},
    "critic_checked": {"quality", "review", "critic"},
    "precommit_failed": {"quality", "review", "critic", "precommit_eval"},
    "repair_planned": {"quality", "review", "critic", "precommit_eval"},
    "rework_running": {"quality", "review", "critic", "precommit_eval"},
    "verified": {"quality", "review", "critic", "precommit_eval"},
    "archived": {"quality", "review", "critic", "precommit_eval"},
}

NEXT_TOOL_BY_STAGE = {
    "selected": "prepare_next_gen or run_crossover",
    "preparing": "prepare_next_gen",
    "prepared": "run_direction_audit",
    "crossover_running": "run_crossover",
    "direction_audited": "run_master",
    "master_planned": "execute_workers",
    "workers_done": "run_quality_gates",
    "quality_failed": "execute_workers",
    "quality_passed": "run_review",
    "reviewed": "run_critic",
    "critic_checked": "run_precommit_eval",
    "precommit_failed": "execute_workers",
    "repair_planned": "execute_workers",
    "rework_running": "execute_workers",
    "verified": "commit_bot",
    "archived": "run_archivist",
    "timed_out": "prepare_next_gen",
    "infra_timed_out": "run_precommit_eval",
}

_STAGE_RANK = {stage: idx for idx, stage in enumerate(STAGE_ORDER)}


def validate_stage_transition(current_stage, proposed_stage):
    """Validate that a pipeline stage transition is legal.

    Returns ``(is_valid, reason)``. Unknown stages remain allowed for backward
    compatibility with old checkpoints, but known backward transitions are
    blocked unless they are explicit retry/recovery paths.
    """
    if proposed_stage is None or current_stage is None:
        return True, "no_guard"
    if proposed_stage == current_stage:
        return True, "same_stage"
    if proposed_stage == "timed_out":
        return True, "timeout_override"
    if proposed_stage == "infra_timed_out":
        return True, "infra_timeout_override"
    if proposed_stage in {"selected", "preparing", "prepared"}:
        return True, "fresh_prepare_restart"

    retry_sources = {
        "workers_done",
        "quality_failed",
        "quality_passed",
        "reviewed",
        "critic_checked",
        "precommit_failed",
        "verified",
    }
    if proposed_stage == "master_planned" and current_stage in retry_sources:
        return True, "retry_reset"
    if proposed_stage == "repair_planned" and current_stage in retry_sources:
        return True, "rework_planned"
    if proposed_stage == "repair_planned" and current_stage == "rework_running":
        return True, "rework_retry_planned"
    if proposed_stage == "rework_running" and current_stage in retry_sources | {"repair_planned"}:
        return True, "rework_running"
    if proposed_stage == "workers_done" and current_stage == "quality_passed":
        return True, "review_rework_done"
    if proposed_stage == "workers_done" and current_stage == "reviewed":
        return True, "review_rework_done"
    if proposed_stage == "workers_done" and current_stage == "critic_checked":
        return True, "critic_rework_done"
    if proposed_stage == "workers_done" and current_stage == "precommit_failed":
        return True, "precommit_rework_done"
    if proposed_stage == "workers_done" and current_stage in {"repair_planned", "rework_running"}:
        return True, "rework_done"
    if proposed_stage == "workers_done" and current_stage == "verified":
        return True, "verified_rework_reset"
    if current_stage == "critic_checked" and proposed_stage == "reviewed":
        return True, "review_recheck"

    if current_stage in STAGE_ORDER and proposed_stage in STAGE_ORDER:
        current_idx = _STAGE_RANK[current_stage]
        proposed_idx = _STAGE_RANK[proposed_stage]
        if proposed_idx > current_idx:
            return True, "forward_progression"
        return False, f"backward_transition: {current_stage} -> {proposed_stage}"

    return True, "unknown_stage"


def is_rework_reset_transition(current_stage: str | None, proposed_stage: str | None) -> bool:
    """True when stage movement implies bot code is being regenerated."""
    if not current_stage or not proposed_stage:
        return False
    if current_stage not in _STAGE_RANK or proposed_stage not in _STAGE_RANK:
        return False
    if proposed_stage in {"repair_planned", "rework_running"} and current_stage in {
        "workers_done",
        "quality_failed",
        "quality_passed",
        "reviewed",
        "critic_checked",
        "precommit_failed",
        "verified",
    }:
        return True
    old_rank = _STAGE_RANK[current_stage]
    new_rank = _STAGE_RANK[proposed_stage]
    return new_rank < old_rank and new_rank <= _STAGE_RANK["workers_done"]


def route_policy(checkpoint: dict | None) -> dict:
    """Return the single authoritative route decision for a checkpoint.

    The orchestrator prompt, compact/resume context, and MCP refusal payloads
    should describe the next step from this function rather than maintaining
    separate stage-hint tables.
    """
    if not checkpoint:
        return {
            "stage": None,
            "next_tool": None,
            "allowed_tools": [],
            "intent": "none",
            "directive": "No active checkpoint. Start or select a generation before calling pipeline tools.",
        }

    stage = checkpoint.get("stage")
    next_v = checkpoint.get("next_v")
    source_v = checkpoint.get("source_v")
    parent2_v = checkpoint.get("parent2_v")
    gate_results = checkpoint.get("gate_results") or {}
    failure_class = None

    if stage == "selected":
        next_tool = "run_crossover" if parent2_v is not None else "prepare_next_gen"
        intent = "crossover_prepare" if parent2_v is not None else "prepare"
    elif stage == "critic_checked":
        gate = gate_results.get("precommit_eval")
        failure_class = classify_precommit_gate(gate)
        if failure_class in {"regression", "failed_unknown"}:
            next_tool = "execute_workers"
            intent = "precommit_rework"
        else:
            next_tool = "run_precommit_eval"
            intent = "precommit_eval"
    else:
        next_tool = NEXT_TOOL_BY_STAGE.get(stage)
        if stage in {"quality_failed", "repair_planned", "rework_running"}:
            intent = "quality_rework"
        elif stage == "precommit_failed":
            intent = "precommit_rework"
        elif stage in {"quality_passed", "reviewed"}:
            intent = "gate"
        elif stage == "master_planned":
            intent = "initial_workers"
        elif stage == "workers_done":
            intent = "quality"
        else:
            intent = "pipeline"

    directive_map = {
        "prepare_next_gen": "Call prepare_next_gen with the checkpoint source/target.",
        "run_crossover": "Call run_crossover with the checkpoint parents/target.",
        "run_direction_audit": "Call run_direction_audit before planning workers.",
        "run_master": "Call run_master to produce worker tasks.",
        "execute_workers": "Call execute_workers with the checkpoint task plan and exact failure feedback when present.",
        "run_quality_gates": "Call run_quality_gates; it owns compile, national, decision, size, and scope validation.",
        "run_review": "Call run_review. Do not rerun workers unless the reviewer returns a code rejection.",
        "run_critic": "Call run_critic; critic is advisory and precommit remains final.",
        "run_precommit_eval": "Call run_precommit_eval unless the precommit gate already recorded a regression.",
        "commit_bot": "Call commit_bot only after all gates are passed.",
        "run_archivist": "Call run_archivist to finish post-commit cleanup.",
    }
    directive = directive_map.get(next_tool, "Inspect checkpoint context and continue with the matching MCP pipeline tool.")
    if stage == "quality_failed":
        directive = (
            "Quality failed. Call execute_workers using the exact quality-gate failures; "
            "do not call run_master. The rework will be tracked as repair_planned/rework_running."
        )
    elif stage == "precommit_failed":
        directive = (
            "Precommit failed. Call execute_workers with the exact precommit blockers; "
            "do not retry precommit on unchanged code."
        )
    elif stage in {"repair_planned", "rework_running"}:
        directive = (
            "Rework is already planned/running. Continue execute_workers with the saved task plan; "
            "do not restart planning or crossover."
        )
    elif stage == "selected" and parent2_v is not None:
        directive = "Crossover selected. Call run_crossover; do not call prepare_next_gen."
    elif stage == "master_planned":
        directive = "Master plan is saved. Call execute_workers with the saved tasks."

    return {
        "stage": stage,
        "next_v": next_v,
        "source_v": source_v,
        "parent2_v": parent2_v,
        "next_tool": next_tool,
        "allowed_tools": [next_tool] if next_tool else [],
        "intent": intent,
        "failure_class": failure_class,
        "directive": directive,
    }


def next_tool_for_checkpoint(checkpoint: dict | None) -> str | None:
    if not checkpoint:
        return None
    return route_policy(checkpoint).get("next_tool")


def generic_abandon_block(checkpoint: dict | None, *,
                          reason: str = "abandon_generation",
                          max_precommit_retries: int = 3) -> dict | None:
    """Return a refusal payload when generic abandon is unsafe.

    Forced abandon callers pass a different reason and bypass this guard. For
    precommit regression failures, abandon is only allowed after the configured
    hard limit; before that, the state machine requires a worker rework using
    the exact precommit feedback.
    """
    if not checkpoint or reason != "abandon_generation":
        return None

    stage = checkpoint.get("stage")
    precommit_attempt = int(checkpoint.get("precommit_attempt") or 0)
    gate = (checkpoint.get("gate_results") or {}).get("precommit_eval")
    failure_class = classify_precommit_gate(gate)

    block = False
    route = route_policy(checkpoint)
    next_tool = route.get("next_tool")
    explanation = "This generation has passed earlier gates; continue the state machine"

    if stage in {"quality_passed", "reviewed", "repair_planned", "rework_running", "verified"}:
        block = True
    elif stage == "critic_checked":
        block = True
        if failure_class in {"regression", "failed_unknown"}:
            next_tool = "execute_workers"
            explanation = "Precommit already failed for this code; rework the bot with exact precommit feedback"
        else:
            next_tool = "run_precommit_eval"
    elif stage == "precommit_failed" and precommit_attempt < max_precommit_retries:
        block = True
        next_tool = "execute_workers"
        explanation = "Precommit failed below the hard limit; rework the bot before abandoning"

    if not block:
        return None

    next_v = checkpoint.get("next_v")
    source_v = checkpoint.get("source_v")
    directive = (
        f"Refusing generic abandon_generation for v{next_v} at stage '{stage}'. "
        f"{explanation} with {next_tool}."
    )
    return {
        "abandoned": False,
        "blocked": True,
        "reason": "forward_only_stage",
        "stage": stage,
        "next_v": next_v,
        "source_v": source_v,
        "next_tool": next_tool,
        "failure_class": failure_class,
        "directive": directive,
    }
