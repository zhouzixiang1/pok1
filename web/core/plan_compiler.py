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

from output_schema import RuntimeContract, runtime_contract_worker_prompt_terms


SOFT_WORKER_PROMPT_CHARS = 6_000
HARD_WORKER_PROMPT_CHARS = 10_000
TASK_CONTEXT_CHARS = 12_000


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
    compiled = copy.deepcopy(plan)
    tasks = compiled.get("tasks", []) if isinstance(compiled, dict) else []
    meta = {
        "compiled": False,
        "compiled_tasks": [],
        "hard_prompt_chars": hard_prompt_chars,
        "context_chars": context_chars,
    }
    context_dir = Path(target_dir) / ".task_context"
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
