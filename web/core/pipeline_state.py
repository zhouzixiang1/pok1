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
    "quality_passed": {"quality", "review"},
    "reviewed": {"quality", "review", "critic"},
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


HEAD_DRIFT_RESUME_POLICY = {
    "selected": {
        "allowed_tools": ("prepare_next_gen", "run_crossover"),
        "resume_kind": "selected",
        "warning_suffix": "selected",
        "requires_target": False,
        "requires_contract_unchanged": False,
        "branch_alias_allowed": True,
    },
    "prepared": {
        "allowed_tools": ("run_direction_audit",),
        "resume_kind": "pre_master",
        "warning_suffix": "pre_master",
        "requires_target": True,
        "requires_contract_unchanged": False,
        "branch_alias_allowed": True,
    },
    "crossover_running": {
        "allowed_tools": ("run_crossover",),
        "resume_kind": "crossover",
        "warning_suffix": "crossover",
        "requires_target": True,
        "requires_contract_unchanged": False,
        "branch_alias_allowed": True,
    },
    "direction_audited": {
        "allowed_tools": ("run_literature_probe", "run_master"),
        "resume_kind": "pre_master",
        "warning_suffix": "pre_master",
        "requires_target": True,
        "requires_contract_unchanged": True,
        "branch_alias_allowed": True,
    },
    "master_planned": {
        "allowed_tools": ("execute_workers",),
        "resume_kind": "initial_workers",
        "warning_suffix": "initial_workers",
        "requires_target": True,
        # The persisted master_plan already contains the exact worker prompts
        # for the first mutating step. If HEAD changed before or during planning,
        # execute_workers can still run from that saved plan; downstream quality
        # and precommit gates validate the resulting candidate on the current
        # codebase before it can be committed.
        "requires_contract_unchanged": False,
        "branch_alias_allowed": True,
    },
    "workers_done": {
        "allowed_tools": ("run_quality_gates",),
        "resume_kind": "gate",
        "warning_suffix": "gate",
        "requires_target": True,
        "requires_contract_unchanged": False,
        "branch_alias_allowed": True,
    },
    "quality_failed": {
        "allowed_tools": ("execute_workers",),
        "resume_kind": "repair",
        "warning_suffix": "repair",
        "requires_target": True,
        "requires_contract_unchanged": True,
        "branch_alias_allowed": True,
    },
    "quality_passed": {
        "allowed_tools": ("run_review", "execute_workers"),
        "resume_kind": "post_quality",
        "warning_suffix": "post_quality",
        "requires_target": True,
        "requires_contract_unchanged": True,
        "branch_alias_allowed": True,
    },
    "reviewed": {
        "allowed_tools": ("run_critic", "execute_workers"),
        "resume_kind": "post_quality",
        "warning_suffix": "post_quality",
        "requires_target": True,
        "requires_contract_unchanged": True,
        "branch_alias_allowed": True,
    },
    "critic_checked": {
        "allowed_tools": ("run_precommit_eval", "execute_workers"),
        "resume_kind": "post_quality",
        "warning_suffix": "post_quality",
        "requires_target": True,
        "requires_contract_unchanged": True,
        "branch_alias_allowed": True,
    },
    "precommit_failed": {
        "allowed_tools": ("execute_workers",),
        "resume_kind": "repair",
        "warning_suffix": "repair",
        "requires_target": True,
        "requires_contract_unchanged": True,
        "branch_alias_allowed": True,
    },
    "repair_planned": {
        "allowed_tools": ("execute_workers",),
        "resume_kind": "repair",
        "warning_suffix": "repair",
        "requires_target": True,
        "requires_contract_unchanged": True,
        "branch_alias_allowed": True,
    },
    "rework_running": {
        "allowed_tools": ("execute_workers",),
        "resume_kind": "repair",
        "warning_suffix": "repair",
        "requires_target": True,
        "requires_contract_unchanged": True,
        "branch_alias_allowed": True,
    },
    "verified": {
        "allowed_tools": ("commit_bot",),
        "resume_kind": "post_quality",
        "warning_suffix": "post_quality",
        "requires_target": True,
        "requires_contract_unchanged": True,
        "branch_alias_allowed": False,
    },
}


def head_drift_resume_policy(stage: str | None) -> dict | None:
    """Return the authoritative HEAD-drift recovery policy for a stage."""
    policy = HEAD_DRIFT_RESUME_POLICY.get(stage)
    return dict(policy) if policy else None


def head_drift_resume_stages() -> set[str]:
    return set(HEAD_DRIFT_RESUME_POLICY)


def head_drift_allowed_tools(stage: str | None) -> set[str]:
    policy = head_drift_resume_policy(stage)
    if not policy:
        return set()
    return set(policy.get("allowed_tools") or ())


def stage_requires_target_for_head_resume(stage: str | None) -> bool:
    policy = head_drift_resume_policy(stage)
    return True if not policy else bool(policy.get("requires_target", True))


def _active_workflow_profile_info() -> tuple[str, str]:
    try:
        from workflow_profiles import get_workflow_profile
        profile = get_workflow_profile()
        return (
            getattr(profile, "profile_id", ""),
            getattr(profile, "national_execution_mode", "adapter"),
        )
    except Exception:
        return "", "adapter"


def _gate_matches_active_workflow(gate: dict | None, *, require_native_contract: bool = False) -> bool:
    active_profile_id, active_execution_mode = _active_workflow_profile_info()
    if not active_profile_id:
        return True
    gate = gate or {}
    gate_profile_id = str(gate.get("workflow_profile_id") or gate.get("profile_id") or "")
    gate_execution_mode = str(gate.get("national_execution_mode") or "")
    if active_profile_id == "default":
        return (
            gate_profile_id in {"", "default"}
            and gate_execution_mode in {"", active_execution_mode}
        )
    if gate_profile_id != active_profile_id:
        return False
    if gate_execution_mode != active_execution_mode:
        return False
    if (
        require_native_contract
        and active_execution_mode == "native_tcp"
        and gate.get("national_native_contract_ok") is not True
    ):
        return False
    return True


def _quality_gate_matches_active_workflow(gate_results: dict) -> bool:
    quality = (gate_results or {}).get("quality") or {}
    return (
        quality.get("all_passed") is True
        and quality.get("critical_scenarios_passed") is True
        and _gate_matches_active_workflow(quality, require_native_contract=True)
    )


def _precommit_gate_matches_active_workflow(gate_results: dict) -> bool:
    precommit = (gate_results or {}).get("precommit_eval") or {}
    return precommit.get("passed") is True and _gate_matches_active_workflow(precommit)


def _critic_gate_passed(gate_results: dict) -> bool:
    critic = (gate_results or {}).get("critic") or {}
    if not critic:
        return False
    if critic.get("approved") is not True:
        return False
    if critic.get("raw_approved") is False or critic.get("advisory_approved") is False:
        return False
    score = critic.get("score", critic.get("advisory_score"))
    if score is None:
        return True
    try:
        return float(score) >= 6.0
    except (TypeError, ValueError):
        return False


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
    if current_stage == "master_planned" and proposed_stage == "direction_audited":
        return True, "master_plan_rejected_replan"
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
    profile_refresh_needed = False

    if (
        stage in {"quality_passed", "reviewed", "critic_checked", "precommit_failed", "verified"}
        and "quality" in gate_results
        and not _quality_gate_matches_active_workflow(gate_results)
    ):
        next_tool = "run_quality_gates"
        intent = "quality_profile_refresh"
        profile_refresh_needed = True
    elif (
        stage == "verified"
        and not _precommit_gate_matches_active_workflow(gate_results)
    ):
        next_tool = "run_precommit_eval"
        intent = "precommit_profile_refresh"
        profile_refresh_needed = True
    elif stage == "selected":
        next_tool = "run_crossover" if parent2_v is not None else "prepare_next_gen"
        intent = "crossover_prepare" if parent2_v is not None else "prepare"
    elif (
        stage in {"reviewed", "critic_checked"}
        and "critic" in gate_results
        and not _critic_gate_passed(gate_results)
    ):
        next_tool = "execute_workers"
        intent = "critic_rework"
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
        "run_critic": "Call run_critic; critic is a hard strategy gate before precommit.",
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
    elif intent == "critic_rework":
        directive = (
            "Critic rejected this candidate. Call execute_workers with the exact "
            "critic feedback stored in reviewer_feedback; do not call run_precommit_eval "
            "or commit_bot on unchanged code."
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
    elif profile_refresh_needed and next_tool == "run_quality_gates":
        directive = (
            "Cached quality gate was produced under a different workflow profile "
            "or national execution mode. Call run_quality_gates to revalidate "
            "the current candidate under the active workflow."
        )
    elif profile_refresh_needed and next_tool == "run_precommit_eval":
        directive = (
            "Cached precommit gate was produced under a different workflow "
            "profile or national execution mode. Call run_precommit_eval to "
            "revalidate under the active workflow."
        )

    allowed_tools = []
    if next_tool:
        allowed_tools.append(next_tool)
    for tool_name in sorted(head_drift_allowed_tools(stage)):
        if tool_name not in allowed_tools:
            allowed_tools.append(tool_name)

    return {
        "stage": stage,
        "next_v": next_v,
        "source_v": source_v,
        "parent2_v": parent2_v,
        "next_tool": next_tool,
        "allowed_tools": allowed_tools,
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
        if "critic" in (checkpoint.get("gate_results") or {}) and not _critic_gate_passed(checkpoint.get("gate_results") or {}):
            next_tool = "execute_workers"
            explanation = "Critic rejected this code; rework the bot with exact critic feedback"
        elif failure_class in {"regression", "failed_unknown"}:
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
