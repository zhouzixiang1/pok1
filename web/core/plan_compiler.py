"""Deterministic Master-plan compilation.

Master should decide strategy, not hand-carry arbitrarily long worker prompts
through every checkpoint and orchestrator turn. This compiler keeps the public
plan shape backward-compatible while moving oversized task context into a
target-bot-local brief file that workers can read explicitly.
"""

from __future__ import annotations

import copy
import re
import shutil
from pathlib import Path
from typing import Any

from output_schema import (
    LEGACY_CONSUMER_MIGRATION_CHECKS,
    LEGACY_CONSUMER_MIGRATION_FILES,
    LEGACY_CONSUMER_MIGRATION_FOCUS_ID,
    RuntimeContract,
    WORKER_PROMPT_MAX_CHARS,
    WORKER_PROMPT_MIN_CHARS,
    runtime_contract_worker_prompt_terms,
)


SOFT_WORKER_PROMPT_CHARS = 6_000
HARD_WORKER_PROMPT_CHARS = 10_000
TASK_CONTEXT_CHARS = 12_000
SYSTEM_OWNED_CONTRACT_HEADER = (
    "System-owned worker contract binding (derived from the structured "
    "runtime_contract/reference card):"
)
SYSTEM_OWNED_CONTRACT_BEGIN = "[[SYSTEM_OWNED_WORKER_CONTRACT:BEGIN]]"
SYSTEM_OWNED_CONTRACT_END = "[[SYSTEM_OWNED_WORKER_CONTRACT:END]]"
_SYSTEM_OWNED_CONTRACT_RE = re.compile(
    r"\n\n"
    + re.escape(SYSTEM_OWNED_CONTRACT_BEGIN)
    + r"\n.*?\n"
    + re.escape(SYSTEM_OWNED_CONTRACT_END),
    re.DOTALL,
)
SYSTEM_OWNED_MIGRATION_BEGIN = "[[SYSTEM_OWNED_LEGACY_MIGRATION:BEGIN]]"
SYSTEM_OWNED_MIGRATION_END = "[[SYSTEM_OWNED_LEGACY_MIGRATION:END]]"


def _safe_worker_id(value: Any, fallback: int) -> str:
    text = str(value if value is not None else fallback)
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("._-")
    return text or str(fallback)


def _trim_context(text: str, max_chars: int = TASK_CONTEXT_CHARS) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    head = max_chars // 2
    tail = max_chars - head - 80
    return (
        text[:head]
        + "\n\n...[TASK CONTEXT TRIMMED BY PLAN COMPILER]...\n\n"
        + text[-tail:],
        True,
    )


def _relative_to_project(path: Path, project_root: Path | None) -> str:
    if project_root is None:
        return str(path)
    try:
        return str(path.relative_to(project_root))
    except ValueError:
        return str(path)


def _reset_task_context_dir(context_dir: Path) -> None:
    if context_dir.exists():
        shutil.rmtree(context_dir)


def _compiled_prompt_validation_terms(
    plan: dict[str, Any],
    task: dict[str, Any],
) -> tuple[str, ...]:
    """Keep hard-gate terms visible after an oversized prompt is externalized."""
    terms: list[str] = []
    raw_contract = task.get("runtime_contract")
    if isinstance(raw_contract, dict):
        try:
            contract = RuntimeContract.model_validate(raw_contract)
        except Exception:
            contract = None
        if contract is not None:
            terms.extend(runtime_contract_worker_prompt_terms(contract))

    policy = plan.get("architecture_policy")
    focus = policy.get("selected_focus") if isinstance(policy, dict) else None
    if isinstance(focus, dict) and str(task.get("architecture_focus_id") or "") == str(
        focus.get("focus_id") or ""
    ):
        terms.extend(str(term) for term in focus.get("required_terms") or [] if str(term))
    return tuple(dict.fromkeys(terms))


def bind_system_owned_legacy_consumer_migration(
    plan: dict[str, Any],
    *,
    policy: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Restore the immutable four-consumer migration after weak-model output.

    The planner may choose implementation details, but the migration focus,
    checks, writable ABI surface, and typed RuntimeContract are policy facts. A
    model omission is therefore normalized here instead of consuming repeated
    quality-repair turns. The policy and runtime-contract ledgers bind the result.
    """
    bound = copy.deepcopy(plan)
    active_policy = policy or (
        bound.get("architecture_policy") if isinstance(bound, dict) else None
    )
    focus = (
        active_policy.get("selected_focus")
        if isinstance(active_policy, dict)
        else None
    )
    meta: dict[str, Any] = {
        "bound": False,
        "focus_id": "",
        "worker_id": None,
        "bundle_digest": None,
        "dropped_worker_ids": [],
    }
    if (
        not isinstance(focus, dict)
        or focus.get("focus_id") != LEGACY_CONSUMER_MIGRATION_FOCUS_ID
    ):
        return bound, meta
    tasks = bound.get("tasks") if isinstance(bound, dict) else None
    if not isinstance(tasks, list) or not tasks:
        return bound, meta

    bundle = active_policy.get("legacy_consumer_migration_bundle") or {}
    if (
        set(bundle.get("required_checks") or [])
        != set(LEGACY_CONSUMER_MIGRATION_CHECKS)
        or set(bundle.get("consumer_files") or [])
        != set(LEGACY_CONSUMER_MIGRATION_FILES)
    ):
        # A malformed policy is repository/checkpoint drift, not something a
        # plan compiler may guess around. The downstream identity gate remains
        # fail-closed.
        return bound, meta

    selected_index = next(
        (
            index
            for index, task in enumerate(tasks)
            if isinstance(task, dict)
            and str(task.get("architecture_focus_id") or "")
            == LEGACY_CONSUMER_MIGRATION_FOCUS_ID
        ),
        0,
    )
    selected = tasks[selected_index]
    if not isinstance(selected, dict):
        return bound, meta

    dropped_worker_ids = [
        task.get("worker_id", index + 1)
        for index, task in enumerate(tasks)
        if index != selected_index and isinstance(task, dict)
    ]
    # The universal migration is a generation-wide isolation boundary, not just
    # one specially labelled task among otherwise unconstrained strategy work.
    # Collapse weak-model output to the single system-owned task before schema
    # validation so an untyped river/state-learning task cannot execute beside
    # the migration or widen the generation's writable scope.
    bound["tasks"] = [selected]
    bound.pop("selected_proposal_id", None)
    bound.pop("proposal_binding", None)

    selected["role"] = "Algorithmic Runtime Migration Architect"
    selected["difficulty"] = "hard"
    selected["skill_layer"] = "runtime_architecture"
    selected["architecture_focus_id"] = LEGACY_CONSUMER_MIGRATION_FOCUS_ID
    selected["target_files"] = list(LEGACY_CONSUMER_MIGRATION_FILES[:3])
    selected["files_allowed"] = list(LEGACY_CONSUMER_MIGRATION_FILES[3:])
    selected["read_only_dependencies"] = ["national_bot.py"]
    selected["prohibited_files"] = []
    selected["checks_required"] = list(dict.fromkeys([
        *(active_policy.get("plan_required_floor_checks") or []),
        *LEGACY_CONSUMER_MIGRATION_CHECKS,
    ]))
    from runtime_architecture_policy import (
        legacy_consumer_migration_runtime_contract,
    )

    selected["runtime_contract"] = legacy_consumer_migration_runtime_contract()
    selected["merge_policy"] = "system_owned_universal_migration"
    selected["expected_diff_shape"] = (
        "One coherent producer-to-consumer migration across exactly strategy.py, "
        "opponent.py, simulation.py, and donk_probe.py."
    )
    selected["behavior_hypothesis"] = (
        "Each of the four wrapper-owned runtime dimensions changes a repeatable "
        "specific final sanitized wire action through an exact live source path."
    )
    # Do not retain model-authored prompt/instruction prose here. It may contain
    # the exact ordinary innovation that this focus must postpone, and appending
    # a contradictory guardrail would still hand both instructions to Worker.
    selected.pop("instruction", None)
    migration_prompt = (
        f"{SYSTEM_OWNED_MIGRATION_BEGIN}\n"
        "System-owned universal legacy-consumer migration; implement all four "
        "obligations in one coherent decision graph. Consume terminal_response "
        "and showdown_range from opponent_runtime with confidence/adaptation "
        "weight, and consume hand_runtime.can_donk plus "
        "hand_runtime.can_delayed_probe from the official semantic transcripts. "
        "Each producer must reach a final sanitized wire action counterfactual; "
        "telemetry, dead reads, or intermediate-only changes do not pass. Edit "
        "strategy.py, opponent.py, simulation.py, and donk_probe.py; treat "
        "national_bot.py as read-only. Preserve bounded connection memory and "
        "confidence from opponent_runtime; publish a legal baseline within the "
        "decision budget/deadline and retain the legal fallback. Do not add "
        "state_learning innovation until the complete migration bundle passes.\n"
        f"{SYSTEM_OWNED_MIGRATION_END}"
    )
    selected["worker_prompt"] = migration_prompt

    meta.update({
        "bound": True,
        "focus_id": LEGACY_CONSUMER_MIGRATION_FOCUS_ID,
        "worker_id": selected.get("worker_id"),
        "bundle_digest": bundle.get("bundle_digest"),
        "required_checks": list(LEGACY_CONSUMER_MIGRATION_CHECKS),
        "consumer_files": list(LEGACY_CONSUMER_MIGRATION_FILES),
        "dropped_worker_ids": dropped_worker_ids,
    })
    return bound, meta


def bind_system_owned_worker_contract_terms(
    plan: dict[str, Any],
    *,
    max_prompt_chars: int = WORKER_PROMPT_MAX_CHARS,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Bind machine-derived contract anchors into worker prompts.

    Master owns the strategy choice and the concrete implementation brief.  It
    should not also be a lossy serializer for literal terms that are already
    determined by ``runtime_contract`` and the selected reference card.  This
    pass renders one canonical system block from those structured sources
    before the hard schema gate runs.  Re-running it replaces that block, so a
    later system architecture policy can add focus terms without duplicating
    stale blocks.

    The pass is deliberately fail-closed.  It never repairs an invalid runtime
    contract, never invents a reference card, and never truncates model-authored
    text to make room.  Invalid contracts and prompt overflows therefore remain
    visible to the existing schema validators.
    """
    bound = copy.deepcopy(plan)
    tasks = bound.get("tasks", []) if isinstance(bound, dict) else []
    meta: dict[str, Any] = {
        "bound": False,
        "bound_tasks": [],
        "invalid_contract_tasks": [],
        "invalid_prompt_tasks": [],
        "overflow_tasks": [],
        "max_prompt_chars": max_prompt_chars,
    }
    if not isinstance(tasks, list):
        return bound, meta

    for idx, task in enumerate(tasks):
        if not isinstance(task, dict):
            continue
        raw_contract = task.get("runtime_contract")
        if not isinstance(raw_contract, dict):
            continue
        worker_id = task.get("worker_id", idx + 1)
        try:
            RuntimeContract.model_validate(raw_contract)
        except Exception as exc:
            meta["invalid_contract_tasks"].append({
                "worker_id": worker_id,
                "error": f"{type(exc).__name__}: {str(exc)[:500]}",
            })
            continue

        raw_prompt = task.get("worker_prompt")
        if not isinstance(raw_prompt, str) or len(raw_prompt) < WORKER_PROMPT_MIN_CHARS:
            meta["invalid_prompt_tasks"].append({
                "worker_id": worker_id,
                "reason": (
                    "worker_prompt_not_string"
                    if not isinstance(raw_prompt, str)
                    else "worker_prompt_below_min_chars"
                ),
                "original_chars": len(raw_prompt) if isinstance(raw_prompt, str) else None,
            })
            continue

        terms = _compiled_prompt_validation_terms(bound, task)
        if not terms:
            continue
        prompt, replaced_blocks = _SYSTEM_OWNED_CONTRACT_RE.subn("", raw_prompt)
        prompt_lower = prompt.lower()
        missing_terms = tuple(
            term for term in terms if str(term).lower() not in prompt_lower
        )

        binding_block = (
            f"\n\n{SYSTEM_OWNED_CONTRACT_BEGIN}\n"
            f"{SYSTEM_OWNED_CONTRACT_HEADER}\n"
            "- Required literal execution anchors: "
            + " | ".join(str(term) for term in terms)
            + ".\n"
            "- These anchors name executable obligations already selected in the "
            "structured contract. Implement their behavior and control evidence; "
            "do not treat them as labels.\n"
            f"{SYSTEM_OWNED_CONTRACT_END}"
        )
        bound_prompt = prompt + binding_block
        if len(bound_prompt) > max_prompt_chars:
            meta["overflow_tasks"].append({
                "worker_id": worker_id,
                "original_chars": len(prompt),
                "required_chars": len(bound_prompt),
                "required_terms": list(terms),
            })
            continue

        task["worker_prompt"] = bound_prompt
        if bound_prompt != raw_prompt:
            meta["bound"] = True
            meta["bound_tasks"].append({
                "worker_id": worker_id,
                "original_chars": len(raw_prompt),
                "bound_chars": len(bound_prompt),
                "added_terms": list(missing_terms),
                "bound_terms": list(terms),
                "replaced_blocks": replaced_blocks,
            })

    return bound, meta


def compile_master_plan(
    plan: dict[str, Any],
    *,
    next_v: int,
    target_dir: Path,
    project_root: Path | None = None,
    hard_prompt_chars: int = HARD_WORKER_PROMPT_CHARS,
    context_chars: int = TASK_CONTEXT_CHARS,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return a compiled copy of a Master plan plus compiler metadata."""
    compiled, migration_binding = bind_system_owned_legacy_consumer_migration(
        plan
    )
    compiled, contract_binding = bind_system_owned_worker_contract_terms(compiled)
    tasks = compiled.get("tasks", []) if isinstance(compiled, dict) else []
    meta = {
        "compiled": bool(migration_binding.get("bound")),
        "compiled_tasks": [],
        "hard_prompt_chars": hard_prompt_chars,
        "context_chars": context_chars,
        "contract_binding": contract_binding,
        "migration_binding": migration_binding,
    }
    context_dir = Path(target_dir) / ".task_context"
    has_precompiled_task = bool(
        isinstance(tasks, list)
        and any(
            isinstance(task, dict)
            and task.get("worker_prompt_compiled") is True
            and str(task.get("task_brief_file") or "").strip()
            for task in tasks
        )
    )
    meta["preserved_compiled_context"] = has_precompiled_task
    if not has_precompiled_task:
        _reset_task_context_dir(context_dir)
    if not isinstance(tasks, list):
        return compiled, meta

    for idx, task in enumerate(tasks):
        if not isinstance(task, dict):
            continue
        prompt = str(task.get("worker_prompt", ""))
        if len(prompt) <= hard_prompt_chars:
            continue

        worker_id = _safe_worker_id(task.get("worker_id"), idx + 1)
        context_dir.mkdir(parents=True, exist_ok=True)
        context_path = context_dir / f"w{worker_id}.md"
        context_text, trimmed = _trim_context(prompt, context_chars)
        context_path.write_text(
            "# Compiled Worker Task Context\n\n"
            f"- next_v: {next_v}\n"
            f"- worker_id: {task.get('worker_id', idx + 1)}\n"
            f"- role: {task.get('role', '')}\n"
            f"- target_files: {task.get('target_files', [])}\n"
            f"- trimmed: {trimmed}\n\n"
            "## Original Worker Prompt\n\n"
            + context_text,
            encoding="utf-8",
        )
        rel_context = _relative_to_project(context_path, project_root)
        targets = ", ".join(map(str, task.get("target_files", []))) or "declared target files"
        validation_terms = _compiled_prompt_validation_terms(compiled, task)
        validation_anchor = ""
        if validation_terms:
            validation_anchor = (
                " Preserve these literal hard-contract terms while following the brief: "
                + ", ".join(validation_terms)
                + "."
            )
        task["task_brief_file"] = rel_context
        task["worker_prompt_compiled"] = True
        task["worker_prompt_original_chars"] = len(prompt)
        task["worker_prompt"] = (
            f"Read <task_brief_file>{rel_context}</task_brief_file> FIRST and follow it exactly. "
            f"Implement only Worker {task.get('worker_id', idx + 1)} ({task.get('role', 'worker')}) "
            f"for v{next_v}. Target files: {targets}. Do not broaden scope. "
            "Run the checks named in the task context and report the exact files changed."
            + validation_anchor
        )
        meta["compiled"] = True
        meta["compiled_tasks"].append({
            "worker_id": task.get("worker_id", idx + 1),
            "target_files": task.get("target_files", []),
            "brief_file": rel_context,
            "original_chars": len(prompt),
            "compiled_chars": len(task["worker_prompt"]),
            "context_trimmed": trimmed,
            "validation_terms": list(validation_terms),
        })

    if meta["compiled"]:
        compiled["plan_compiler"] = meta
    return compiled, meta
