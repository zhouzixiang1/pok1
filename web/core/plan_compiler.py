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
    POLICY_CONTEXT_SCHEMA_VERSION,
    POLICY_CONTEXT_TOP_LEVEL_FIELDS,
    POLICY_ENTRYPOINTS,
    POLICY_INTENT_KINDS,
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
SYSTEM_OWNED_CONTRACT_MAX_CHARS = 2_048
_SYSTEM_OWNED_CONTRACT_RE = re.compile(
    r"\n\n"
    + re.escape(SYSTEM_OWNED_CONTRACT_BEGIN)
    + r"\n"
    + re.escape(SYSTEM_OWNED_CONTRACT_HEADER)
    + r"\n.*?\n"
    + re.escape(SYSTEM_OWNED_CONTRACT_END)
    + r"\Z",
    re.DOTALL,
)
SELECTED_PROPOSAL_BEGIN = "[[SELECTED_PROPOSAL_CONTRACT:BEGIN]]"
SELECTED_PROPOSAL_END = "[[SELECTED_PROPOSAL_CONTRACT:END]]"
_SELECTED_PROPOSAL_RE = re.compile(
    re.escape(SELECTED_PROPOSAL_BEGIN)
    + r"\n.*?\n"
    + re.escape(SELECTED_PROPOSAL_END),
    re.DOTALL,
)


def _system_owned_contract_binding_block(terms: tuple[str, ...]) -> str:
    """Render the bounded deterministic block appended to one Worker prompt."""

    return (
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


def _safe_worker_id(value: Any, fallback: int) -> str:
    text = str(value if value is not None else fallback)
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("._-")
    return text or str(fallback)


def _trim_context(text: str, max_chars: int = TASK_CONTEXT_CHARS) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    proposal_blocks = _SELECTED_PROPOSAL_RE.findall(text)
    preserved = proposal_blocks[0] if len(proposal_blocks) == 1 else ""
    remaining = _SELECTED_PROPOSAL_RE.sub("", text) if preserved else text
    separator = "\n\n...[TASK CONTEXT TRIMMED BY PLAN COMPILER]...\n\n"
    reserve = len(preserved) + (2 if preserved else 0)
    available = max_chars - reserve
    if available <= len(separator) + 2:
        # The caller's schema/size gate will reject an overlarge selected
        # contract. Never silently cut its digest-bound counterfactual evidence.
        return preserved or text, True
    head = available // 2
    tail = available - head - len(separator)
    trimmed = remaining[:head] + separator + remaining[-tail:]
    if preserved:
        trimmed = trimmed.rstrip() + "\n\n" + preserved
    return trimmed, True


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
    proposal_binding = plan.get("proposal_binding")
    if isinstance(proposal_binding, dict):
        task_files = {
            Path(str(value)).name
            for key in ("target_files", "files_allowed")
            for value in (task.get(key) or [])
        }
        proposal_files = {
            Path(str(value)).name
            for value in (proposal_binding.get("target_files") or [])
        }
        if task_files.intersection(proposal_files):
            terms.extend((
                str(proposal_binding.get("contract_digest") or ""),
                str((proposal_binding.get("falsifier") or {}).get("test_name") or ""),
            ))
    return tuple(dict.fromkeys(terms))


def _compiled_selected_proposal_anchor(
    plan: dict[str, Any],
    selected_blocks: list[str],
) -> str:
    """Keep the immutable selected-proposal identity in a compiled stub.

    The full selected-proposal contract remains in the system-owned temporary
    task brief so the Worker has its complete context.  The plan itself,
    however, is later revalidated after a crash and the brief is deliberately
    not a durable candidate artifact.  Preserve the two identity terms and
    boundary markers in the compact prompt so receipt validation can bind the
    compiled form to the sealed proposal without trusting a transient path.
    """

    if len(selected_blocks) != 1:
        return ""
    binding = plan.get("proposal_binding")
    if not isinstance(binding, dict):
        return ""
    proposal_id = str(binding.get("selected_proposal_id") or "")
    contract_digest = str(binding.get("contract_digest") or "")
    if not proposal_id or not contract_digest:
        return ""
    selected_block = selected_blocks[0]
    required_terms = (
        SELECTED_PROPOSAL_BEGIN,
        SELECTED_PROPOSAL_END,
        f"proposal_id={proposal_id}",
        f"contract_digest={contract_digest}",
    )
    if any(term not in selected_block for term in required_terms):
        return ""
    return "\n\n".join((
        SELECTED_PROPOSAL_BEGIN,
        "# SYSTEM-BOUND SELECTED PROPOSAL IDENTITY ANCHOR",
        f"proposal_id={proposal_id}",
        f"contract_digest={contract_digest}",
        "The complete digest-bound proposal contract is in the compiler-owned "
        "task brief; do not substitute a different proposal.",
        SELECTED_PROPOSAL_END,
    ))


def bind_system_owned_policy_abi(
    plan: dict[str, Any],
    *,
    policy: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Bind every typed runtime task to the one national policy ABI.

    The model chooses poker behavior.  It cannot choose the transport boundary,
    context schema, entrypoints, or intent vocabulary.  This pass only fills the
    closed ABI section; it never rewrites task scope or preserves an older bot
    interface.
    """
    bound = copy.deepcopy(plan)
    meta: dict[str, Any] = {
        "bound": False,
        "schema_version": POLICY_CONTEXT_SCHEMA_VERSION,
        "bound_worker_ids": [],
    }
    tasks = bound.get("tasks") if isinstance(bound, dict) else None
    if not isinstance(tasks, list):
        return bound, meta
    abi = {
        "module": "policy.py",
        "context_schema_version": POLICY_CONTEXT_SCHEMA_VERSION,
        "context_fields": list(POLICY_CONTEXT_TOP_LEVEL_FIELDS),
        "entrypoints": list(POLICY_ENTRYPOINTS),
        "intent_kinds": list(POLICY_INTENT_KINDS),
        "raise_field": "raise_to",
        "pass_mapping": "socket_owner_call_or_check",
    }
    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            continue
        contract = task.get("runtime_contract")
        if not isinstance(contract, dict):
            continue
        if contract.get("policy_abi") != abi:
            contract["policy_abi"] = copy.deepcopy(abi)
            meta["bound"] = True
            meta["bound_worker_ids"].append(task.get("worker_id", index + 1))
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

        prompt, replaced_blocks = _SYSTEM_OWNED_CONTRACT_RE.subn("", raw_prompt)
        injected_system_markers = tuple(
            marker
            for marker in (
                SYSTEM_OWNED_CONTRACT_BEGIN,
                SYSTEM_OWNED_CONTRACT_END,
            )
            if marker in prompt
        )
        if injected_system_markers:
            meta["invalid_prompt_tasks"].append({
                "worker_id": worker_id,
                "reason": "worker_prompt_reserved_system_marker",
                "reserved_markers": list(injected_system_markers),
                "original_chars": len(raw_prompt),
            })
            continue
        terms = _compiled_prompt_validation_terms(bound, task)
        if not terms:
            continue
        prompt_lower = prompt.lower()
        missing_terms = tuple(
            term for term in terms if str(term).lower() not in prompt_lower
        )

        binding_block = _system_owned_contract_binding_block(terms)
        bound_prompt = prompt + binding_block
        if (
            len(binding_block) > SYSTEM_OWNED_CONTRACT_MAX_CHARS
            or len(bound_prompt) > max_prompt_chars
        ):
            meta["overflow_tasks"].append({
                "worker_id": worker_id,
                "original_chars": len(prompt),
                "required_chars": len(bound_prompt),
                "binding_chars": len(binding_block),
                "binding_max_chars": SYSTEM_OWNED_CONTRACT_MAX_CHARS,
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
    compiled, policy_abi_binding = bind_system_owned_policy_abi(
        plan
    )
    compiled, contract_binding = bind_system_owned_worker_contract_terms(compiled)
    tasks = compiled.get("tasks", []) if isinstance(compiled, dict) else []
    meta = {
        # ``compiled`` is the durable marker for prompt externalization only.
        # Migration/contract normalization has its own typed metadata below and
        # must not inject ``plan_compiler`` into an otherwise inline Master
        # result: the strict LLM authority deliberately rejects that lossy
        # post-role projection.
        "compiled": False,
        "compiled_tasks": [],
        "hard_prompt_chars": hard_prompt_chars,
        "context_chars": context_chars,
        "contract_binding": contract_binding,
        "policy_abi_binding": policy_abi_binding,
        "preserved_inline_tasks": [],
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
        context_path = context_dir / f"w{worker_id}.md"
        context_text, trimmed = _trim_context(prompt, context_chars)
        selected_blocks = _SELECTED_PROPOSAL_RE.findall(prompt)
        if (
            len(context_text) > context_chars
            or any(block not in context_text for block in selected_blocks)
        ):
            # Externalization is an optimization, not authority to truncate a
            # selected proposal. Keep the schema-valid inline prompt intact.
            meta["preserved_inline_tasks"].append({
                "worker_id": task.get("worker_id", idx + 1),
                "reason": "selected_proposal_contract_exceeds_context_budget",
                "original_chars": len(prompt),
                "selected_contract_chars": sum(map(len, selected_blocks)),
            })
            continue
        context_dir.mkdir(parents=True, exist_ok=True)
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
        selected_proposal_anchor = _compiled_selected_proposal_anchor(
            compiled,
            selected_blocks,
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
            + selected_proposal_anchor
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
