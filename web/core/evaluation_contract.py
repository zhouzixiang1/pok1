"""Evaluation contract model for evolution git drift decisions.

This module is the single place that defines which repository paths can change
the meaning of an evolution evaluation. Runtime guards, checkpoint recovery and
publish reconciliation should depend on this contract instead of open-coding
HEAD-drift exceptions.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Iterable

from bot_namespace import (
    ACTIVE_BOT_PREFIX,
    FIRST_STRICT_POLICY_VERSION,
    bot_relpath,
    parse_bot_version,
)
from evolution_scope import (
    CRITICAL_EVALUATION_GATE_EXACT,
    CRITICAL_EXACT,
    CRITICAL_GENERATION_EXACT,
    CRITICAL_NATIONAL_PLATFORM_EXACT,
    CRITICAL_PREFIXES,
    CRITICAL_PROMPT_EXACT,
    CRITICAL_FIRST_STRICT_CONTROL_EXACT,
    CRITICAL_LLM_CONTROL_EXACT,
    CRITICAL_SYSTEM_BOOTSTRAP_EXACT,
    NON_CONTRACT_PREFIXES,
    RUNTIME_PREFIXES,
    changed_paths_between_heads,
    normalize_repo_path,
)

CONTRACT_VERSION = 40
_BOT_NAME_RE = re.compile(rf"^{re.escape(ACTIVE_BOT_PREFIX)}(?P<version>\d+)$")
_BOT_PATH_RE = re.compile(rf"^bots/{re.escape(ACTIVE_BOT_PREFIX)}(?P<version>\d+)(?:/|$)")

# Guarding the guard itself is non-negotiable: if these files changed on disk,
# the running process may be making drift decisions with older in-memory code.
ALWAYS_CRITICAL_EXACT = frozenset({
    "docs/official-raise-boundary-oracle-2026-07-11.md",
    "docs/official-terminal-settlement-oracle-2026-07-11.md",
    "web/core/blocking_runtime.py",
    "web/core/bootstrap_contract_recovery.py",
    "web/core/bot_artifact.py",
    "web/core/bot_namespace.py",
    "web/core/candidate_sandbox.py",
    "web/core/checkpoint_schema.py",
    "web/core/daemon_management.py",
    "web/core/evaluation_contract.py",
    "web/core/evaluation_data_identity.py",
    "web/core/epoch_authority.py",
    "web/core/evolution_infra.py",
    "web/core/evolution_core.py",
    "web/core/evolution_scope.py",
    "web/core/gate_outcome.py",
    "web/core/generation_evidence.py",
    "web/core/managed_bot_executor.py",
    "web/core/managed_bot_socket.py",
    "web/core/national_epoch_registry.py",
    "web/core/national_bot_launcher.py",
    "web/core/national_game_runtime.py",
    "web/core/national_runtime_telemetry.py",
    "sever/server/transport.py",
    "web/core/national_runtime_authority.py",
    "web/core/official_attribution.py",
    "web/core/official_certificate_signing.py",
    "web/core/official_certification.py",
    "web/core/official_certification_job.py",
    "web/core/official_certifier_allowed_signers",
    "web/core/official_certifier_trust_policy.json",
    "web/core/official_bot_sandbox.py",
    "web/core/official_bootstrap.py",
    "web/core/official_bootstrap_control.json",
    "web/core/first_strict_control.py",
    "web/core/bootstrap_assets/first_strict_control_v1/manifest.json",
    "web/core/bootstrap_assets/first_strict_control_v1/policy.py",
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
    "scripts/official_certify.py",
    "scripts/abandon_parked_bootstrap_contract_change.py",
    "scripts/reconcile_national_policy_epoch.py",
    "scripts/reconcile_terminal_gate.py",
    "web/core/orchestrator.py",
    "web/core/orchestrator_context.py",
    "web/core/orchestrator_cost_policy.py",
    "web/core/pipeline_recovery.py",
    "web/core/pipeline_infrastructure.py",
    "web/core/pipeline_state.py",
    "web/core/terminal_gate_reconcile.py",
    "web/core/post_publication_handoff.py",
    "web/core/publish_reconcile.py",
    "web/core/publication_transaction.py",
    "web/core/repo_state.py",
    "web/core/stability_observation.py",
    "web/core/tool_runtime_guard.py",
    "web/core/tool_bot_management.py",
    "web/core/tool_pipeline.py",
    "web/core/tools.py",
    "web/core/runtime_capacity.py",
    "web/core/workflow_profiles.py",
    "web/core/workflow_kernel.py",
    "web/core/worker_workflow.py",
}).union(
    # The deterministic first-strict migration remains relevant through commit:
    # publication revalidates its worker/review/critic receipts against these
    # exact checked-in bytes.  LLM availability modules are restart authority,
    # so drift must not be reconciled under older in-memory pause semantics.
    CRITICAL_SYSTEM_BOOTSTRAP_EXACT,
    CRITICAL_FIRST_STRICT_CONTROL_EXACT,
    CRITICAL_LLM_CONTROL_EXACT,
)

EVALUATION_RUNTIME_EXACT = frozenset().union(
    ALWAYS_CRITICAL_EXACT,
    CRITICAL_NATIONAL_PLATFORM_EXACT,
    CRITICAL_EVALUATION_GATE_EXACT,
)

FULL_PIPELINE_EXACT = frozenset().union(
    EVALUATION_RUNTIME_EXACT,
    CRITICAL_GENERATION_EXACT,
    CRITICAL_PROMPT_EXACT,
)

PREPARE_STAGE_EXACT = frozenset().union(
    ALWAYS_CRITICAL_EXACT,
    {
        "web/core/agent_review.py",  # owns crossover implementation helpers
        "web/core/crossover_projection.py",
        "web/core/crossover_synthesis.py",
        "web/core/audit_agents.py",
        "web/core/generation_scheduler.py",
        "web/core/llm_failure.py",
        "web/core/llm_query.py",
        "web/core/output_schema.py",
        "web/core/tool_bot_management.py",
        "web/core/tool_commit.py",  # owns run_crossover preparation
        "web/core/tool_gates.py",  # owns prepare_next_gen
        "web/core/tool_helpers.py",
        "web/core/tool_planning.py",
        "web/core/prompts/crossover_compatibility.md",
        "web/core/prompts/crossover_prompt.md",
    },
)

DIRECTION_STAGE_EXACT = frozenset().union(
    ALWAYS_CRITICAL_EXACT,
    {
        "web/core/direction_auditor.py",
        "web/core/llm_failure.py",
        "web/core/llm_query.py",
        "web/core/output_schema.py",
        "web/core/research_governance.py",
        "web/core/tool_helpers.py",
        "web/core/tool_planning.py",
        "web/core/prompts/direction_auditor_prompt.md",
        "web/core/prompts/literature_probe_prompt.md",
    },
)

MASTER_STAGE_EXACT = frozenset().union(
    ALWAYS_CRITICAL_EXACT,
    {
        "web/core/agent_master.py",
        "web/core/audit_agents.py",
        "web/core/bot_action_stats.py",
        "web/core/combined_analyst.py",
        "web/core/cycle_archivist.py",
        "web/core/evidence_snapshot.py",
        "web/core/llm_failure.py",
        "web/core/llm_query.py",
        "web/core/master_context_contract.py",
        "web/core/output_schema.py",
        "web/core/strategy_reference_pack.py",
        "web/core/poker_assets.py",
        "web/core/plan_compiler.py",
        "web/core/research_governance.py",
        "web/core/replay_spotlight.py",
        "web/core/skill_library.py",
        "web/core/tool_helpers.py",
        "web/core/tool_planning.py",
        "web/core/prompts/combined_analyst.md",
        "web/core/prompts/degeneration_diagnosis.md",
        "web/core/prompts/cycle_archivist.md",
        "web/core/prompts/literature_probe_prompt.md",
        "web/core/prompts/master_plan_audit.md",
        "web/core/prompts/master_prompt.md",
    },
)

WORKER_REPAIR_STAGE_EXACT = frozenset().union(
    ALWAYS_CRITICAL_EXACT,
    {
        "web/core/agent_workers.py",
        "web/core/failure_classification.py",
        "web/core/llm_failure.py",
        "web/core/llm_query.py",
        "web/core/output_schema.py",
        "web/core/strategy_reference_pack.py",
        "web/core/poker_assets.py",
        "web/core/plan_compiler.py",
        "web/core/tool_helpers.py",
        "web/core/tool_planning.py",
        "web/core/prompts/debug_worker_prompt.md",
        "web/core/prompts/worker_cot_check.md",
        "web/core/prompts/worker_profile_national_native.md",
        "web/core/prompts/worker_prompt.md",
    },
)

QUALITY_STAGE_EXACT = frozenset().union(
    ALWAYS_CRITICAL_EXACT,
    CRITICAL_NATIONAL_PLATFORM_EXACT,
    {
        "web/core/candidate_hygiene.py",
        "web/core/candidate_sandbox.py",
        "web/core/code_verification.py",
        "web/core/national_decision_tester.py",
        "web/core/eval_stats.py",
        "web/core/gate_execution.py",
        "web/core/national_capability_contract.py",
        "web/core/national_native.py",
        "web/core/national_runtime_probe.py",
        "web/core/national_runtime_probe_scenarios.py",
        "web/core/national_runtime_probe_worker.py",
        "web/core/runtime_architecture_policy.py",
        "web/core/strategy_reference_pack.py",
        "web/core/tool_gates.py",
        "web/core/worker_boundary.py",
    },
)

REVIEW_STAGE_EXACT = frozenset().union(
    ALWAYS_CRITICAL_EXACT,
    {
        "web/core/agent_review.py",
        "web/core/llm_failure.py",
        "web/core/llm_query.py",
        "web/core/output_schema.py",
        "web/core/tool_gates.py",
        "web/core/prompts/reviewer_prompt.md",
    },
)

CRITIC_STAGE_EXACT = frozenset().union(
    ALWAYS_CRITICAL_EXACT,
    {
        "web/core/agent_review.py",
        "web/core/audit_agents.py",
        "web/core/llm_failure.py",
        "web/core/llm_query.py",
        "web/core/output_schema.py",
        "web/core/tool_gates.py",
        "web/core/prompts/critic_prompt.md",
    },
)

PRECOMMIT_STAGE_EXACT = frozenset().union(
    ALWAYS_CRITICAL_EXACT,
    CRITICAL_NATIONAL_PLATFORM_EXACT,
    {
        "web/core/elo_daemon.py",
        "web/core/eval_stats.py",
        "web/core/gate_execution.py",
        "web/core/national_capability_contract.py",
        "web/core/national_native.py",
        "web/core/national_runtime_probe.py",
        "web/core/national_runtime_probe_scenarios.py",
        "web/core/national_runtime_probe_worker.py",
        "web/core/precommit_eval_contract.py",
        "web/core/rating_snapshot.py",
        "web/core/runtime_architecture_policy.py",
        "web/core/strategy_reference_pack.py",
        "web/core/strength_order.py",
        "web/core/tool_eval.py",
    },
)

COMMIT_STAGE_EXACT = frozenset().union(
    ALWAYS_CRITICAL_EXACT,
    {
        "web/core/candidate_hygiene.py",
        "web/core/gate_execution.py",
        "web/core/official_attribution.py",
        "web/core/official_certificate_signing.py",
        "web/core/official_certification_job.py",
        "web/core/official_evidence_archive.py",
        "web/core/tool_commit.py",
        "web/core/prompts/official_platform_analysis.md",
    },
)

_STAGE_EXACT = {
    "selected": PREPARE_STAGE_EXACT,
    "preparing": PREPARE_STAGE_EXACT,
    "prepared": DIRECTION_STAGE_EXACT,
    "crossover_running": PREPARE_STAGE_EXACT,
    "direction_audited": MASTER_STAGE_EXACT,
    "master_planned": WORKER_REPAIR_STAGE_EXACT,
    "quality_failed": WORKER_REPAIR_STAGE_EXACT,
    "quality_rejected": ALWAYS_CRITICAL_EXACT,
    "precommit_failed": WORKER_REPAIR_STAGE_EXACT,
    "repair_planned": WORKER_REPAIR_STAGE_EXACT,
    "rework_running": WORKER_REPAIR_STAGE_EXACT,
    "workers_done": QUALITY_STAGE_EXACT,
    "quality_passed": REVIEW_STAGE_EXACT,
    "review_rejected": ALWAYS_CRITICAL_EXACT,
    "reviewed": CRITIC_STAGE_EXACT,
    "critic_rejected": ALWAYS_CRITICAL_EXACT,
    "critic_checked": PRECOMMIT_STAGE_EXACT,
    "verified": COMMIT_STAGE_EXACT,
    "official_bootstrap_required": COMMIT_STAGE_EXACT,
    "official_certifying": COMMIT_STAGE_EXACT,
    "publishing": COMMIT_STAGE_EXACT,
}

NATIVE_TCP_EXCLUDED_EXACT = frozenset()


def _active_national_execution_mode(explicit: str | None = None) -> str:
    if explicit:
        mode = str(explicit)
    else:
        from workflow_profiles import get_workflow_profile

        mode = str(
            getattr(get_workflow_profile(), "national_execution_mode", "") or ""
        )
    if mode != "native_tcp":
        raise ValueError(
            f"invalid national execution mode {mode!r}; only native_tcp is active"
        )
    return mode


def _stage_exact_for_mode(stage_exact: frozenset[str], national_execution_mode: str) -> frozenset[str]:
    if national_execution_mode == "native_tcp":
        return frozenset(path for path in stage_exact if path not in NATIVE_TCP_EXCLUDED_EXACT)
    return stage_exact


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def bot_version_from_name(value: Any) -> int | None:
    text = str(value or "").strip()
    match = _BOT_NAME_RE.match(text)
    if not match:
        return None
    return parse_bot_version(text)


def bot_version_from_path(path: str) -> int | None:
    match = _BOT_PATH_RE.match(normalize_repo_path(path))
    if not match:
        return None
    return _as_int(match.group("version"))


def _iter_values(value: Any) -> Iterable[Any]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _iter_values(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _iter_values(item)
    else:
        yield value


def _extract_opponent_versions(checkpoint: dict[str, Any] | None) -> set[int]:
    versions: set[int] = set()
    if not isinstance(checkpoint, dict):
        return versions
    gate_results = checkpoint.get("gate_results")
    for value in _iter_values(gate_results if isinstance(gate_results, dict) else {}):
        version = bot_version_from_name(value)
        if version is not None:
            versions.add(version)
    official_job = checkpoint.get("official_job")
    for value in _iter_values(official_job if isinstance(official_job, dict) else {}):
        version = bot_version_from_name(value)
        if version is not None:
            versions.add(version)
    return versions


def contract_bot_versions(
    *,
    candidate_v: int | None = None,
    source_v: int | None = None,
    checkpoint: dict[str, Any] | None = None,
    extra_versions: Iterable[int | str] | None = None,
) -> list[int]:
    """Return bot versions that are part of the active evaluation contract."""
    versions: set[int] = set()
    for value in (candidate_v, source_v):
        version = _as_int(value)
        if version is not None:
            versions.add(version)
    if isinstance(checkpoint, dict):
        for key in ("next_v", "source_v", "parent2_v"):
            version = _as_int(checkpoint.get(key))
            if version is not None:
                versions.add(version)
        versions.update(_extract_opponent_versions(checkpoint))
    for value in extra_versions or ():
        version = _as_int(value)
        if version is not None:
            versions.add(version)
    # Retired pre-epoch bots are numeric/tag namespace continuity only.  They
    # must never become filesystem inputs merely because an active checkpoint
    # (notably fresh v143) carries source_v=142.  Normal strict parents begin at
    # FIRST_STRICT_POLICY_VERSION and remain included below.
    return sorted(
        version
        for version in versions
        if version >= FIRST_STRICT_POLICY_VERSION
    )


def _contract_stage(checkpoint: dict[str, Any] | None, stage: str | None = None) -> str:
    if stage:
        return str(stage)
    if isinstance(checkpoint, dict):
        checkpoint_stage = checkpoint.get("stage")
        if checkpoint_stage:
            return str(checkpoint_stage)
    return ""


def critical_exact_for_stage(
    stage: str | None = None,
    *,
    national_execution_mode: str | None = None,
) -> frozenset[str]:
    """Return exact files that can still affect the current checkpoint.

    Without a checkpoint stage we keep the original conservative full-pipeline
    contract. With a checkpoint stage, only the guard itself and the files
    needed by the stage's next deterministic/LLM tool are contract-critical.
    """
    stage = str(stage or "")
    execution_mode = _active_national_execution_mode(national_execution_mode)
    if stage in _STAGE_EXACT:
        return _stage_exact_for_mode(_STAGE_EXACT[stage], execution_mode)
    return _stage_exact_for_mode(FULL_PIPELINE_EXACT, execution_mode)


def build_evaluation_contract(
    root: str | Path,
    *,
    candidate_v: int | None = None,
    source_v: int | None = None,
    checkpoint: dict[str, Any] | None = None,
    extra_versions: Iterable[int | str] | None = None,
    stage: str | None = None,
    national_execution_mode: str | None = None,
    include_hash: bool = False,
) -> dict[str, Any]:
    """Build a serializable description of evaluation-sensitive paths."""
    contract_stage = _contract_stage(checkpoint, stage)
    execution_mode = _active_national_execution_mode(national_execution_mode)
    bot_versions = contract_bot_versions(
        candidate_v=candidate_v,
        source_v=source_v,
        checkpoint=checkpoint,
        extra_versions=extra_versions,
    )
    prefixes = list(CRITICAL_PREFIXES) + [bot_relpath(version) + "/" for version in bot_versions]
    exact = sorted({
        *critical_exact_for_stage(
        contract_stage,
        national_execution_mode=execution_mode,
        ),
        *(f"official_certificates/{ACTIVE_BOT_PREFIX}{version}.json" for version in bot_versions),
    })
    contract = {
        "version": CONTRACT_VERSION,
        "stage": contract_stage,
        "national_execution_mode": execution_mode,
        "path_prefixes": sorted(set(prefixes)),
        "path_exact": exact,
        "bot_versions": bot_versions,
        "runtime_prefixes": list(RUNTIME_PREFIXES),
        "non_contract_prefixes": list(NON_CONTRACT_PREFIXES),
    }
    if include_hash:
        contract["hash"] = evaluation_contract_hash(root, contract)
    return contract


def _is_runtime_path(path: str, runtime_prefixes: Iterable[str]) -> bool:
    path = normalize_repo_path(path)
    return any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in runtime_prefixes)


def is_contract_path(path: str, contract: dict[str, Any]) -> bool:
    path = normalize_repo_path(path)
    if not path or _is_runtime_path(path, contract.get("runtime_prefixes") or RUNTIME_PREFIXES):
        return False
    # Explicit exact-file contracts override broad convenience exclusions such
    # as docs/. The two byte-pinned official oracle documents live there and
    # must remain restart-critical even though ordinary notes are neutral.
    if path in set(contract.get("path_exact") or []):
        return True
    non_contract_prefixes = contract.get("non_contract_prefixes") or NON_CONTRACT_PREFIXES
    if any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in non_contract_prefixes):
        return False
    return any(path.startswith(prefix) for prefix in contract.get("path_prefixes") or [])


def classify_contract_paths(paths: Iterable[str], contract: dict[str, Any]) -> dict[str, Any]:
    contract_paths: list[str] = []
    external_paths: list[str] = []
    for raw in paths:
        path = normalize_repo_path(str(raw))
        if not path:
            continue
        if is_contract_path(path, contract):
            contract_paths.append(path)
        else:
            external_paths.append(path)
    return {
        "contract_paths": sorted(set(contract_paths)),
        "external_paths": sorted(set(external_paths)),
        "contract_count": len(set(contract_paths)),
        "external_count": len(set(external_paths)),
    }


def _git_ls_files(root: Path, pathspecs: list[str]) -> list[str]:
    if not pathspecs:
        return []
    proc = subprocess.run(
        ["git", "ls-files", "-z", "--", *pathspecs],
        cwd=str(root),
        capture_output=True,
        text=False,
        timeout=30,
    )
    if proc.returncode != 0:
        return []
    return [
        normalize_repo_path(item.decode("utf-8", errors="replace"))
        for item in proc.stdout.split(b"\0")
        if item
    ]


def _iter_contract_files(root: Path, contract: dict[str, Any]) -> list[str]:
    pathspecs = list(contract.get("path_exact") or []) + list(contract.get("path_prefixes") or [])
    files = {
        rel for rel in _git_ls_files(root, pathspecs)
        if is_contract_path(rel, contract)
    }
    for prefix in contract.get("path_prefixes") or []:
        if not prefix.startswith(f"bots/{ACTIVE_BOT_PREFIX}"):
            continue
        base = root / prefix.rstrip("/")
        if not base.exists():
            continue
        for current_root, dirnames, filenames in os.walk(base):
            dirnames[:] = [
                name for name in dirnames
                if name not in {"__pycache__", ".pytest_cache", ".mypy_cache"}
            ]
            for filename in filenames:
                if filename.endswith((".pyc", ".pyo")):
                    continue
                path = Path(current_root) / filename
                try:
                    rel = normalize_repo_path(str(path.relative_to(root)))
                except ValueError:
                    continue
                if is_contract_path(rel, contract):
                    files.add(rel)
    return sorted(files)


def evaluation_contract_hash(root: str | Path, contract: dict[str, Any]) -> str:
    """Hash the current on-disk content of contract files."""
    root_path = Path(root)
    digest = hashlib.sha256()
    digest.update(f"contract-v{contract.get('version', CONTRACT_VERSION)}\n".encode())
    for rel in _iter_contract_files(root_path, contract):
        path = root_path / rel
        if not path.is_file():
            continue
        digest.update(rel.encode("utf-8", errors="replace") + b"\0")
        try:
            with open(path, "rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as exc:
            digest.update(f"ERROR:{type(exc).__name__}:{exc}".encode("utf-8", errors="replace"))
        digest.update(b"\0")
    return digest.hexdigest()


def evaluate_head_drift(
    root: str | Path,
    baseline_head: str,
    current_head: str,
    *,
    candidate_v: int | None = None,
    source_v: int | None = None,
    checkpoint: dict[str, Any] | None = None,
    extra_versions: Iterable[int | str] | None = None,
    stage: str | None = None,
    national_execution_mode: str | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Return whether a HEAD change leaves the evaluation contract untouched."""
    if not baseline_head or not current_head or baseline_head == current_head:
        return False, {}
    changed_paths = changed_paths_between_heads(root, baseline_head, current_head)
    if changed_paths is None:
        return False, {
            "head_drift_paths_available": False,
            "evaluation_contract_unchanged": False,
        }
    contract = build_evaluation_contract(
        root,
        candidate_v=candidate_v,
        source_v=source_v,
        checkpoint=checkpoint,
        extra_versions=extra_versions,
        stage=stage,
        national_execution_mode=national_execution_mode,
        include_hash=False,
    )
    scope = classify_contract_paths(changed_paths, contract)
    allowed = not scope["contract_paths"]
    return allowed, {
        "head_drift_paths_available": True,
        "evaluation_contract_unchanged": allowed,
        "evaluation_contract": contract,
        "head_changed_paths": changed_paths[:80],
        "head_contract_paths": scope["contract_paths"][:40],
        "head_external_paths": scope["external_paths"][:40],
        # Compatibility fields for existing guard/recovery logs and tests.
        "head_blocking_entries": [f"?? {path}" for path in scope["contract_paths"][:40]],
        "head_ignored_entries": [f"?? {path}" for path in scope["external_paths"][:40]],
    }
