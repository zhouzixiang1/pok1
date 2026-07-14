"""Path ownership rules for running evolution inside a shared worktree."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from bot_namespace import ACTIVE_BOT_PREFIX, bot_relpath, parse_bot_version

CRITICAL_PREFIXES = ()
NON_CONTRACT_PREFIXES = (
    # Original national-platform documents and Windows reference assets. They
    # are important references, but changing them does not alter a running
    # local evaluation unless the Python server/engine code changes too.
    "sever/国赛平台/",
)
# No broad source directory is a contract path. Keep this list file-scoped so a
# dashboard/doc/logging change can drift without interrupting a running
# generation. Add a file here only when changing it can alter bot selection,
# candidate mutation, national legality, or gate/precommit outcomes.
# The Botzone/local JSON engines are physically archived and have zero active
# evaluation authority. Keep the names for checkpoint-schema compatibility,
# but never bind archive bytes into a national evaluation contract.
CRITICAL_NATIONAL_PLATFORM_EXACT = frozenset({
    # National TCP platform logic that native gates and precommit execute.
    "sever/engine/deck.py",
    "sever/engine/evaluator.py",
    "sever/engine/game.py",
    "sever/engine/thp_recorder.py",
    "sever/engine/validator.py",
    "sever/server/protocol.py",
    "sever/server/tcp_server.py",
    "sever/tests/test_national_platform_alignment.py",
})

# System-owned recovery bytes are executable control-plane inputs, not
# candidate-owned strategy data.  Keep every blueprint file exact-scoped: a
# broad bootstrap_assets/ prefix would make unrelated future experiments
# restart an in-flight generation, while omitting one file would let the first
# strict candidate change without evaluation-contract drift.
CRITICAL_SYSTEM_BOOTSTRAP_EXACT = frozenset({
    "scripts/reset_national_tcp_policy_epoch.py",
    "web/core/system_strict_bootstrap.py",
    "web/core/strict_authority_workflow.py",
    "web/core/bootstrap_assets/strict_v1/manifest.json",
    "web/core/bootstrap_assets/strict_v1/policy.py",
    "web/core/bootstrap_assets/strict_v1/prepared_policy.py",
})

# The first strict bot cannot execute the quarantined v142 strategy as an
# evaluation opponent.  These checked-in bytes and their materializer define
# the sole non-rating control allowed while the strict publication pool is
# empty, so they remain exact-scoped from quality through publication.
CRITICAL_FIRST_STRICT_CONTROL_EXACT = frozenset({
    "web/core/first_strict_execution_journal.py",
    "web/core/first_strict_control.py",
    "web/core/bootstrap_assets/first_strict_control_v1/manifest.json",
    "web/core/bootstrap_assets/first_strict_control_v1/policy.py",
})

# Availability classification and durable pause/resume authority decide
# whether an LLM activity is retried or parked across process restarts.  The
# mutable pause record itself lives below results/ and remains runtime data.
CRITICAL_LLM_CONTROL_EXACT = frozenset({
    "web/core/llm_availability.py",
    "web/core/llm_availability_store.py",
})

CRITICAL_EVALUATION_GATE_EXACT = frozenset({
    # Gate and precommit logic whose output defines whether a bot is publishable.
    "web/core/bot_artifact.py",
    "web/core/candidate_sandbox.py",
    "web/core/candidate_hygiene.py",
    "web/core/code_verification.py",
    "web/core/national_decision_tester.py",
    "web/core/elo_daemon.py",
    "web/core/eval_stats.py",
    "web/core/evaluation_bundle.py",
    "web/core/evaluation_data_identity.py",
    "web/core/gate_execution.py",
    "web/core/blocking_runtime.py",
    "web/core/managed_bot_executor.py",
    "web/core/managed_bot_socket.py",
    "web/core/national_bot_launcher.py",
    "web/core/national_capability_contract.py",
    "web/core/national_game_runtime.py",
    "web/core/national_native.py",
    "web/core/national_runtime_telemetry.py",
    "sever/server/transport.py",
    "web/core/national_epoch_registry.py",
    "web/core/national_runtime_probe.py",
    "web/core/national_runtime_probe_scenarios.py",
    "web/core/national_runtime_probe_worker.py",
    "web/core/national_runtime_authority.py",
    "web/core/precommit_eval_contract.py",
    "web/core/rating_snapshot.py",
    "web/core/strength_order.py",
    "web/core/official_attribution.py",
    "web/core/official_certificate_signing.py",
    "web/core/official_certification.py",
    "web/core/official_certification_job.py",
    "web/core/official_certifier_allowed_signers",
    "web/core/official_certifier_trust_policy.json",
    "web/core/official_bot_sandbox.py",
    "web/core/official_bootstrap.py",
    "web/core/official_bootstrap_control.json",
    "web/core/official_execution_profile.py",
    "web/core/official_execution_profile.json",
    "web/core/official_eligibility.py",
    "web/core/official_evidence.py",
    "web/core/official_evidence_archive.py",
    "web/core/official_role_policy.json",
    "web/core/official_job_envelope.py",
    "web/core/official_llm_analysis.py",
    "web/core/official_platform_harness.py",
    "web/core/official_platform_resource.py",
    "web/core/official_verdict_ledger.py",
    "web/core/official_wire_probe.py",
    "web/core/publication_transaction.py",
    "web/core/runtime_architecture_policy.py",
    "web/core/strategy_reference_pack.py",
    "web/core/runtime_capacity.py",
    "web/core/tool_commit.py",
    "web/core/tool_eval.py",
    "web/core/tool_gates.py",
    "web/core/worker_boundary.py",
    "web/core/prompts/official_platform_analysis.md",
    "scripts/official_certify.py",
}).union(
    CRITICAL_SYSTEM_BOOTSTRAP_EXACT,
    CRITICAL_FIRST_STRICT_CONTROL_EXACT,
)

CRITICAL_GENERATION_EXACT = frozenset({
    # Evolution decisions, worker boundaries, recovery, and publish safety.
    "web/core/agent_master.py",
    "web/core/agent_review.py",
    "web/core/agent_workers.py",
    "web/core/audit_agents.py",
    "web/core/bot_action_stats.py",
    "web/core/bot_namespace.py",
    "web/core/combined_analyst.py",
    "web/core/cycle_archivist.py",
    "web/core/crossover_projection.py",
    "web/core/crossover_synthesis.py",
    "web/core/direction_auditor.py",
    "web/core/evidence_snapshot.py",
    "web/core/evaluation_contract.py",
    "web/core/epoch_authority.py",
    "web/core/evolution_infra.py",
    "web/core/evolution_scope.py",
    "web/core/failure_classification.py",
    "web/core/gate_execution.py",
    "web/core/generation_scheduler.py",
    "web/core/llm_failure.py",
    "web/core/llm_query.py",
    "web/core/national_runtime_authority.py",
    "web/core/official_bootstrap.py",
    "web/core/official_bootstrap_control.json",
    "web/core/orchestrator.py",
    "web/core/orchestrator_cost_policy.py",
    "web/core/orchestrator_context.py",
    "web/core/output_schema.py",
    "web/core/checkpoint_schema.py",
    "web/core/pipeline_infrastructure.py",
    "web/core/pipeline_intents.py",
    "web/core/pipeline_recovery.py",
    "web/core/pipeline_schema.py",
    "web/core/pipeline_state.py",
    "web/core/plan_compiler.py",
    "web/core/publish_reconcile.py",
    "web/core/publication_transaction.py",
    "web/core/repo_state.py",
    "web/core/research_governance.py",
    "web/core/runtime_architecture_policy.py",
    "web/core/strategy_reference_pack.py",
    "web/core/skill_library.py",
    "web/core/tool_bot_management.py",
    "web/core/tool_helpers.py",
    "web/core/tool_pipeline.py",
    "web/core/tool_planning.py",
    "web/core/tool_runtime_guard.py",
    "web/core/tools.py",
    "web/core/workflow_profiles.py",
    "web/core/workflow_kernel.py",
    "web/core/worker_workflow.py",
}).union(
    CRITICAL_SYSTEM_BOOTSTRAP_EXACT,
    CRITICAL_FIRST_STRICT_CONTROL_EXACT,
    CRITICAL_LLM_CONTROL_EXACT,
)

CRITICAL_PROMPT_EXACT = frozenset({
    # Active LLM prompts. Prompt edits change future generation behavior.
    "web/core/prompts/cycle_archivist.md",
    "web/core/prompts/combined_analyst.md",
    "web/core/prompts/critic_prompt.md",
    "web/core/prompts/crossover_compatibility.md",
    "web/core/prompts/crossover_prompt.md",
    "web/core/prompts/debug_worker_prompt.md",
    "web/core/prompts/degeneration_diagnosis.md",
    "web/core/prompts/direction_auditor_prompt.md",
    "web/core/prompts/literature_probe_prompt.md",
    "web/core/prompts/master_plan_audit.md",
    "web/core/prompts/master_prompt.md",
    "web/core/prompts/orchestrator.md",
    "web/core/prompts/reviewer_prompt.md",
    "web/core/prompts/worker_cot_check.md",
    "web/core/prompts/worker_prompt.md",
    "web/core/prompts/worker_profile_national_native.md",
})

CRITICAL_EXACT = frozenset().union(
    CRITICAL_NATIONAL_PLATFORM_EXACT,
    CRITICAL_EVALUATION_GATE_EXACT,
    CRITICAL_GENERATION_EXACT,
    CRITICAL_PROMPT_EXACT,
)
RUNTIME_PREFIXES = (
    "web/core/results/",
    "web/logs/",
    "web/frontend/dist/",
    "web/server/static/",
    "results/",
    "ladder_results/",
)

_ACTIVE_BOT_RE = re.compile(rf"^bots/{re.escape(ACTIVE_BOT_PREFIX)}(?P<version>\d+)(?:/|$)")


def normalize_repo_path(path: str) -> str:
    """Normalize a git porcelain path to a slash-separated relative path."""
    path = (path or "").strip().strip('"').replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def status_entry_paths(entry: str) -> list[str]:
    """Extract one or two paths from a porcelain v1 status entry."""
    raw = (entry or "").rstrip()
    if not raw or raw.startswith("## "):
        return []
    payload = raw[3:] if len(raw) > 3 else raw
    if " -> " in payload:
        left, right = payload.split(" -> ", 1)
        paths = [left, right]
    else:
        paths = [payload]
    return [p for p in (normalize_repo_path(path) for path in paths) if p]


def is_runtime_path(path: str) -> bool:
    path = normalize_repo_path(path)
    return any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in RUNTIME_PREFIXES)


def is_non_contract_path(path: str) -> bool:
    path = normalize_repo_path(path)
    return any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in NON_CONTRACT_PREFIXES)


def active_bot_version(path: str) -> int | None:
    match = _ACTIVE_BOT_RE.match(normalize_repo_path(path))
    if not match:
        return None
    return parse_bot_version(f"{ACTIVE_BOT_PREFIX}{match.group('version')}")


def is_candidate_bot_path(path: str, candidate_v: int | None) -> bool:
    if candidate_v is None:
        return False
    return normalize_repo_path(path).startswith(bot_relpath(candidate_v) + "/")


def _normalize_bot_versions(values: Any) -> set[int] | None:
    if values is None:
        return None
    versions: set[int] = set()
    for value in values if isinstance(values, (list, tuple, set, frozenset)) else [values]:
        try:
            if isinstance(value, bool):
                continue
            versions.add(int(value))
        except Exception:
            continue
    return versions


def is_foreign_active_bot_path(
    path: str,
    candidate_v: int | None,
    contract_bot_versions: Any = None,
) -> bool:
    version = active_bot_version(path)
    if version is None:
        return False
    if candidate_v is not None and version == int(candidate_v):
        return False
    allowed_versions = _normalize_bot_versions(contract_bot_versions)
    if allowed_versions is None:
        return True
    return version in allowed_versions


def is_critical_evolution_path(path: str) -> bool:
    path = normalize_repo_path(path)
    if is_runtime_path(path) or is_non_contract_path(path):
        return False
    return path in CRITICAL_EXACT or any(path.startswith(prefix) for prefix in CRITICAL_PREFIXES)


def _is_contract_path(path: str, contract: dict[str, Any] | None) -> bool:
    if not isinstance(contract, dict):
        return False
    path = normalize_repo_path(path)
    if not path:
        return False
    runtime_prefixes = contract.get("runtime_prefixes") or RUNTIME_PREFIXES
    if any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in runtime_prefixes):
        return False
    non_contract_prefixes = contract.get("non_contract_prefixes") or NON_CONTRACT_PREFIXES
    if any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in non_contract_prefixes):
        return False
    if path in set(contract.get("path_exact") or []):
        return True
    return any(path.startswith(prefix) for prefix in contract.get("path_prefixes") or [])


def classify_path(
    path: str,
    candidate_v: int | None,
    contract_bot_versions: Any = None,
    evaluation_contract: dict[str, Any] | None = None,
) -> str:
    """Classify a repo path for in-place evolution ownership checks."""
    path = normalize_repo_path(path)
    if not path:
        return "empty"
    if is_runtime_path(path):
        return "runtime"
    if is_non_contract_path(path):
        return "external"
    if is_candidate_bot_path(path, candidate_v):
        return "candidate"
    if is_foreign_active_bot_path(path, candidate_v, contract_bot_versions):
        return "foreign_active_bot"
    if evaluation_contract is not None:
        return "critical" if _is_contract_path(path, evaluation_contract) else "external"
    if is_critical_evolution_path(path):
        return "critical"
    return "external"


def classify_status_entries(
    entries: list[str] | tuple[str, ...] | None,
    candidate_v: int | None,
    contract_bot_versions: Any = None,
    evaluation_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify porcelain status entries into blocking and ignored groups."""
    groups: dict[str, list[str]] = {
        "candidate_entries": [],
        "critical_entries": [],
        "foreign_bot_entries": [],
        "runtime_entries": [],
        "external_entries": [],
        "unknown_entries": [],
    }
    entry_classes: list[dict[str, Any]] = []
    for entry in entries or []:
        paths = status_entry_paths(str(entry))
        if not paths:
            groups["unknown_entries"].append(str(entry))
            continue
        classes = {
            classify_path(
                path,
                candidate_v,
                contract_bot_versions,
                evaluation_contract=evaluation_contract,
            )
            for path in paths
        }
        item = {"entry": str(entry), "paths": paths, "classes": sorted(classes)}
        entry_classes.append(item)
        if "critical" in classes:
            groups["critical_entries"].append(str(entry))
        elif "foreign_active_bot" in classes:
            groups["foreign_bot_entries"].append(str(entry))
        elif "candidate" in classes:
            groups["candidate_entries"].append(str(entry))
        elif "runtime" in classes:
            groups["runtime_entries"].append(str(entry))
        elif "external" in classes:
            groups["external_entries"].append(str(entry))
        else:
            groups["unknown_entries"].append(str(entry))

    blocking_entries = groups["critical_entries"] + groups["foreign_bot_entries"]
    ignored_entries = groups["runtime_entries"] + groups["external_entries"]
    return {
        **groups,
        "entry_classes": entry_classes,
        "blocking_entries": blocking_entries,
        "ignored_entries": ignored_entries,
        "blocking_count": len(blocking_entries),
        "ignored_count": len(ignored_entries),
    }


def classify_paths(
    paths: list[str] | tuple[str, ...] | set[str],
    candidate_v: int | None,
    contract_bot_versions: Any = None,
    evaluation_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    entries = [f"?? {normalize_repo_path(path)}" for path in sorted(paths)]
    return classify_status_entries(
        entries,
        candidate_v,
        contract_bot_versions,
        evaluation_contract=evaluation_contract,
    )


def changed_paths_between_heads(root: str | Path, old_head: str, new_head: str) -> list[str] | None:
    """Return changed paths between two git heads, or None if git cannot answer."""
    if not old_head or not new_head or old_head == new_head:
        return []
    try:
        proc = subprocess.run(
            ["git", "diff", "--name-only", f"{old_head}..{new_head}"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return [normalize_repo_path(line) for line in (proc.stdout or "").splitlines() if line.strip()]
