"""LLM query primitive and JSON output parsing.

Provides run_claude_query() for all sub-agent LLM calls, and parse_json_output()
for extracting structured data from LLM responses.
"""

import asyncio
import contextlib
import contextvars
import fnmatch
import hashlib
import inspect
import json
import logging
import math
import os
import re
import shlex
import threading
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from claude_agent_sdk import (
    query as claude_query,
    ClaudeAgentOptions,
    AssistantMessage,
    UserMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
    ThinkingBlock,
    ClaudeSDKError,
)
from bot_namespace import ACTIVE_BOT_PREFIX
from llm_availability import LLMAvailabilityBlocked, LLMAvailabilityTrace
from llm_failure import is_shutdown_cancel_error, is_success_error_result
import llm_role_observability as _ro  # role-IO logging/observability helpers

log = logging.getLogger("pok.infra")
_shutdown_manager = None
_shutdown_manager_owner = None
_SHUTDOWN_MANAGER_LOCK = threading.Lock()
_LLM_BILLING_RESULTS = contextvars.ContextVar("llm_billing_results", default=None)
_STRICT_PROVIDER_RESULTS = contextvars.ContextVar(
    "strict_provider_results", default=None
)
_LLM_TOTAL_DEADLINE = contextvars.ContextVar(
    "llm_total_deadline", default=None
)
# The per-attempt stream processor (_process_stream) and the bounded
# signature-retry execution path (_run_stream_with_signature_retry family +
# billing/sleep helpers) live in llm_query_retry (companion module).  They are
# re-exported below as thin delegate shells so ``from llm_query import <name>``
# imports and test monkeypatches on ``llm_query.<name>`` keep resolving.  The
# companion routes every parent-symbol reference through a lazy ``_lq`` alias so
# patches applied to this module's namespace take effect at call time.
import llm_query_retry as _qr  # noqa: E402

# Owned provider-attempt lifecycle + terminal-abandon result cache live in
# llm_provider_attempt (companion module).  These names are re-exported here
# for backward compatibility so existing imports and monkeypatches on
# ``llm_query.<name>`` keep working.  The ContextVar object below is the SAME
# object as ``llm_provider_attempt._LLM_PROVIDER_ATTEMPT`` (identity contract).
import llm_provider_attempt as _pa  # noqa: E402
_LLM_PROVIDER_ATTEMPT = _pa._LLM_PROVIDER_ATTEMPT
_PROVIDER_CLEANUP_LOCK = _pa._PROVIDER_CLEANUP_LOCK
_UNRESOLVED_PROVIDER_ATTEMPTS = _pa._UNRESOLVED_PROVIDER_ATTEMPTS

# Rendered-prompt receipt issuing, dispatch-scope validation, and the
# system-owned role-contract suffix live in llm_role_dispatch (companion
# module).  The dataclasses, validators, and suffix renderers are re-exported
# below as thin delegate shells so ``from llm_query import <name>`` and
# ``monkeypatch.setattr(llm_query, "<name>", ...)`` keep resolving.
import llm_role_dispatch as _rd  # noqa: E402


class LLMRoleContractError(RuntimeError):
    """Raised before provider dispatch when an active role drifts from policy."""


def _strict_provider_failure_error(
    error: BaseException,
    *,
    shutdown_requested: bool,
) -> BaseException | str:
    """Normalize controlled process termination to attempt-neutral evidence."""

    return (
        "asyncio.CancelledError"
        if is_shutdown_cancel_error(error) and shutdown_requested
        else error
    )


@dataclass(frozen=True)
class LLMRoleContract:
    """One provider-facing capability and evidence contract.

    ``role_pattern`` is matched against the concrete runtime label (which may
    carry a version, Worker id, or retry suffix).  The remaining fields are
    system-owned: prompt templates may explain them but cannot broaden them.
    """

    role_id: str
    role_pattern: object
    provider_path: str
    renderer: str
    producer_file: str
    producer_name: str
    template_paths: tuple
    evidence_provenance_kind: str
    required_evidence_fields: tuple
    scope_policy: str
    allowed_tool_sets: tuple
    provider_read_scope: str
    provider_write_scope: str
    evidence_policy: str
    history_policy: str
    allowed_models: tuple = ("sonnet",)
    allowed_mcp_servers: frozenset = frozenset()
    requires_read_scope: bool = False
    requires_write_scope: bool = False
    allows_context_files: bool = False
    allows_evidence_snapshot: bool = False
    allows_strict_authority: bool = False
    allows_exact_bash_commands: bool = False
    requires_frozen_evidence_guard: bool = False
    fixed_read_files: tuple = ()
    fixed_bash_commands: tuple = ()


def _llm_role_contract(
    role_id,
    pattern,
    *,
    provider_path="subagent_sdk",
    renderer,
    producer_file,
    producer_name,
    template_paths=(),
    evidence_kind,
    required_evidence_fields=(),
    scope_policy="none",
    tools=((),),
    read_scope="none_provider_filesystem",
    write_scope="none",
    evidence_policy="system_bound_prompt_only",
    history_policy="forbidden",
    models=("sonnet",),
    mcp_servers=(),
    requires_read_scope=False,
    requires_write_scope=False,
    allows_context_files=False,
    allows_evidence_snapshot=False,
    allows_strict_authority=False,
    allows_exact_bash_commands=False,
    requires_frozen_evidence_guard=False,
    fixed_read_files=(),
    fixed_bash_commands=(),
):
    return LLMRoleContract(
        role_id=str(role_id),
        role_pattern=re.compile(pattern, re.IGNORECASE),
        provider_path=str(provider_path),
        renderer=str(renderer),
        producer_file=str(producer_file),
        producer_name=str(producer_name),
        template_paths=tuple(str(path) for path in template_paths),
        evidence_provenance_kind=str(evidence_kind),
        required_evidence_fields=tuple(
            str(field) for field in required_evidence_fields
        ),
        scope_policy=str(scope_policy),
        allowed_tool_sets=tuple(frozenset(group) for group in tools),
        provider_read_scope=str(read_scope),
        provider_write_scope=str(write_scope),
        evidence_policy=str(evidence_policy),
        history_policy=str(history_policy),
        allowed_models=tuple(str(model) for model in models),
        allowed_mcp_servers=frozenset(str(name) for name in mcp_servers),
        requires_read_scope=bool(requires_read_scope),
        requires_write_scope=bool(requires_write_scope),
        allows_context_files=bool(allows_context_files),
        allows_evidence_snapshot=bool(allows_evidence_snapshot),
        allows_strict_authority=bool(allows_strict_authority),
        allows_exact_bash_commands=bool(allows_exact_bash_commands),
        requires_frozen_evidence_guard=bool(requires_frozen_evidence_guard),
        fixed_read_files=tuple(str(path) for path in fixed_read_files),
        fixed_bash_commands=tuple(str(command) for command in fixed_bash_commands),
    )


# This is the sole active role registry.  Order is significant where a more
# specific label (proposal critic, CoT audit, crossover compatibility) shares a
# prefix with an implementation role.
ACTIVE_LLM_ROLE_CONTRACTS = (
    _llm_role_contract(
        "orchestrator",
        r"^ORCHESTRATOR$",
        provider_path="orchestrator_sdk",
        renderer="prompts/orchestrator.md::_build_context",
        producer_file="web/core/orchestrator.py",
        producer_name="_render_orchestrator_provider_prompt",
        template_paths=("web/core/prompts/orchestrator.md",),
        evidence_kind="checkpoint_context_projection",
        required_evidence_fields=("context_digest", "dry_run"),
        scope_policy="orchestrator_mcp_only",
        tools=((),),
        read_scope="typed_evolution_mcp_projections_only",
        write_scope="typed_fenced_evolution_mcp_effects_only",
        evidence_policy="checkpoint_bound_typed_mcp_only",
        history_policy="fresh_provider_session_from_checkpoint_projection_only",
        mcp_servers=("evolution",),
    ),
    _llm_role_contract(
        "master_proposal_critic",
        r"^MASTER PROPOSAL CRITIC(?:\s|$)",
        renderer="agent_master.py::_run_master_proposal_ensemble/critic_renderer",
        producer_file="web/core/agent_master_prompts.py",
        producer_name="_render_master_proposal_critic_provider_prompt",
        evidence_kind="frozen_proposal_packet",
        required_evidence_fields=(
            "proposal_packet_digest", "proposal_name", "criteria_digest",
            "planning_context_digest", "lens_digest", "evidence_mode", "schema_retry",
            "invocation_id",
        ),
        evidence_policy="frozen_proposal_packet_in_prompt",
        history_policy="frozen_generation_context_only",
        allows_strict_authority=True,
    ),
    _llm_role_contract(
        "master_proposal",
        r"^MASTER PROPOSAL(?:\s|$)",
        renderer="agent_master.py::_run_master_proposal_ensemble/proposal_renderer",
        producer_file="web/core/agent_master_prompts.py",
        producer_name="_render_master_proposal_provider_prompt",
        evidence_kind="master_planning_context",
        required_evidence_fields=(
            "planning_context_digest", "direction", "source_v", "next_v",
            "source_symbol_index_digest", "directive_digest",
            "protocol_bootstrap_prepared_only", "singleton_no_strength",
            "evidence_mode", "repair_kind",
            "projection_hints", "allowed_primaries", "invocation_id",
        ),
        scope_policy="canonical_candidates",
        tools=(("Read",),),
        read_scope="explicit_candidate_and_generation_snapshot_dirs",
        evidence_policy="generation_snapshot_plus_content_bound_candidate",
        history_policy="frozen_generation_context_only",
        requires_read_scope=True,
        allows_evidence_snapshot=True,
        allows_strict_authority=True,
        requires_frozen_evidence_guard=True,
    ),
    _llm_role_contract(
        "master_final",
        r"^MASTER(?:\s+\(TRY\s+\d+\))?$",
        renderer="prompts/master_prompt.md+master_context_contract.py",
        producer_file="web/core/agent_master_prompts.py",
        producer_name="_render_master_final_provider_prompt",
        template_paths=("web/core/prompts/master_prompt.md",),
        evidence_kind="compiled_master_context",
        required_evidence_fields=(
            "master_context_digest", "proposal_packet_digest", "source_v", "next_v",
            "template_values_digest", "schema_repair_digest",
            "final_output_guard_digest", "invocation_id",
        ),
        scope_policy="none",
        tools=((),),
        read_scope="none",
        evidence_policy="generation_snapshot_plus_compiled_proposal_packet",
        history_policy="frozen_generation_context_only",
        allows_strict_authority=True,
        # Keep the decision-changing-role evidence hook even with zero tools.
        # It is a fail-closed tripwire if capability drift ever reintroduces a
        # filesystem tool; the current role still receives no read authority.
        requires_frozen_evidence_guard=True,
    ),
    _llm_role_contract(
        "worker_cot_audit",
        r"^WORKER_COT_CHECK_",
        renderer="prompts/worker_cot_check.md::_run_worker_cot_check",
        producer_file="web/core/audit_agents.py",
        producer_name="_render_worker_cot_provider_prompt",
        template_paths=("web/core/prompts/worker_cot_check.md",),
        evidence_kind="worker_output_diff",
        required_evidence_fields=(
            "task_digest", "diff_digest", "worker_output_digest",
            "worker_role_digest", "worker_task_digest", "diff_metadata_digest",
            "worker_output_binding_digest", "worker_effect_id",
            "worker_lease_epoch", "worker_dispatch_receipt_digest",
        ),
        evidence_policy="system_bound_worker_output_and_diff_metadata",
        history_policy="current_bound_diff_only",
    ),
    _llm_role_contract(
        "worker",
        r"^WORKER(?:\s|$)",
        renderer="prompts/worker_prompt.md+prompts/worker_profile_national_native.md",
        producer_file="web/core/agent_workers.py",
        producer_name="_render_worker_provider_prompt",
        template_paths=(
            "web/core/prompts/worker_prompt.md",
            "web/core/prompts/worker_profile_national_native.md",
        ),
        evidence_kind="compiled_worker_task",
        required_evidence_fields=(
            "task", "next_v", "candidate_path", "allowed_files",
            "renderer_inputs_digest",
        ),
        scope_policy="worker_candidate",
        tools=(("Bash", "Read", "Edit"),),
        read_scope="explicit_target_candidate_and_system_context_files",
        write_scope="exact_compiled_task_files",
        evidence_policy="checkpoint_compiled_task_plus_candidate_bytes",
        history_policy="forbidden",
        requires_read_scope=True,
        requires_write_scope=True,
        requires_frozen_evidence_guard=True,
    ),
    _llm_role_contract(
        "debug_agent",
        r"^DEBUG AGENT(?:\s|$)",
        renderer="prompts/debug_worker_prompt.md::_run_debug_agent",
        producer_file="web/core/agent_workers.py",
        producer_name="_render_debug_provider_prompt",
        template_paths=("web/core/prompts/debug_worker_prompt.md",),
        evidence_kind="worker_gate_failure",
        required_evidence_fields=(
            "candidate_path", "error_digest", "changed_diff_digest",
            "target_file", "next_v",
        ),
        scope_policy="debug_candidate",
        tools=(("Read",),),
        read_scope="explicit_target_candidate_only",
        evidence_policy="bound_candidate_and_system_gate_failure",
        requires_read_scope=True,
        requires_frozen_evidence_guard=True,
    ),
    _llm_role_contract(
        "lead_code_reviewer",
        r"^LEAD CODE REVIEWER$",
        renderer="prompts/reviewer_prompt.md::run_review",
        producer_file="web/core/tool_gates.py",
        producer_name="_render_reviewer_provider_prompt",
        template_paths=("web/core/prompts/reviewer_prompt.md",),
        evidence_kind="review_candidate_pair",
        required_evidence_fields=(
            "source_v", "next_v", "review_prompt_digest",
            "review_semantic_contract_digest", "review_authority_slot",
        ),
        scope_policy="canonical_candidates",
        tools=(("Read",), ("Bash", "Read")),
        read_scope="explicit_source_and_target_candidate_dirs",
        evidence_policy="content_bound_candidate_and_system_gate_input",
        history_policy="bound_parent_candidate_only",
        requires_read_scope=True,
        allows_strict_authority=True,
        requires_frozen_evidence_guard=True,
    ),
    _llm_role_contract(
        "strategy_critic",
        r"^STRATEGY CRITIC$",
        renderer="prompts/critic_prompt.md::_run_critic",
        producer_file="web/core/agent_review.py",
        producer_name="_render_critic_provider_prompt",
        template_paths=("web/core/prompts/critic_prompt.md",),
        evidence_kind="critic_plan_candidate_snapshot",
        required_evidence_fields=(
            "source_v", "next_v", "master_plan_digest", "code_evidence_digest",
            "h2h_snapshot_digest", "previous_critic_digest", "invocation_id",
        ),
        scope_policy="canonical_candidates",
        tools=(("Read",),),
        read_scope="explicit_source_target_and_generation_snapshot_dirs",
        evidence_policy="generation_snapshot_plus_content_bound_candidate",
        history_policy="frozen_generation_context_only",
        requires_read_scope=True,
        allows_evidence_snapshot=True,
        allows_strict_authority=True,
        requires_frozen_evidence_guard=True,
    ),
    _llm_role_contract(
        "crossover_compatibility",
        r"^CROSSOVER_COMPAT_",
        renderer="prompts/crossover_compatibility.md::_run_crossover_compatibility_audit",
        producer_file="web/core/audit_agents.py",
        producer_name="_render_crossover_compat_provider_prompt",
        template_paths=("web/core/prompts/crossover_compatibility.md",),
        evidence_kind="crossover_parent_compatibility",
        required_evidence_fields=(
            "parent_a_v", "parent_b_v", "parent_code_digest",
            "rating_context_digest", "h2h_context_digest",
            "architecture_context_digest", "parent_snapshot_receipt_digest",
        ),
        evidence_policy="frozen_parent_artifacts_plus_generation_snapshot",
        history_policy="bound_parent_identity_only",
    ),
    _llm_role_contract(
        "crossover",
        r"^CROSSOVER(?:\s|$)",
        renderer="prompts/crossover_prompt.md::_run_crossover",
        producer_file="web/core/agent_review.py",
        producer_name="_render_crossover_provider_prompt",
        template_paths=("web/core/prompts/crossover_prompt.md",),
        evidence_kind="crossover_frozen_parents",
        required_evidence_fields=(
            "parent_a_v", "parent_b_v", "target_v", "parent_artifacts",
            "compatibility_receipt_digest", "renderer_inputs_digest",
        ),
        scope_policy="crossover_workspace",
        tools=(("Bash", "Read", "Edit"),),
        read_scope="frozen_parent_snapshots_and_target_candidate",
        write_scope="exact_target_policy_file",
        evidence_policy="frozen_parent_snapshots_plus_target_candidate",
        history_policy="bound_parent_identity_only",
        requires_read_scope=True,
        requires_write_scope=True,
        allows_evidence_snapshot=True,
        requires_frozen_evidence_guard=True,
    ),
    _llm_role_contract(
        "direction_auditor",
        r"^DIRECTION AUDITOR$",
        renderer="prompts/direction_auditor_prompt.md::_run_direction_audit",
        producer_file="web/core/direction_auditor.py",
        producer_name="_render_direction_provider_prompt",
        template_paths=("web/core/prompts/direction_auditor_prompt.md",),
        evidence_kind="annotated_completion_direction_history",
        required_evidence_fields=("source_v", "generation_history_digest"),
        evidence_policy="annotated_strict_completion_commits_in_prompt",
        history_policy="annotated_strict_completion_tags_only",
    ),
    _llm_role_contract(
        "literature_probe",
        r"^LITERATURE_PROBE(?:\s|$)",
        renderer="prompts/literature_probe_prompt.md::run_literature_probe",
        producer_file="web/core/tool_planning.py",
        producer_name="_render_literature_provider_prompt",
        template_paths=("web/core/prompts/literature_probe_prompt.md",),
        evidence_kind="governed_literature_brief",
        required_evidence_fields=("source_v", "next_v", "brief_digest"),
        tools=(("WebSearch",),),
        read_scope="governed_public_web_search_only",
        evidence_policy="cited_public_sources_no_strength_weight",
    ),
    _llm_role_contract(
        "cycle_archivist",
        r"^CYCLE ARCHIVIST$",
        renderer="prompts/cycle_archivist.md::run_cycle_archivist_analysis",
        producer_file="web/core/cycle_archivist.py",
        producer_name="_render_cycle_archivist_provider_prompt",
        template_paths=("web/core/prompts/cycle_archivist.md",),
        evidence_kind="content_bound_cycle_snapshot",
        required_evidence_fields=("version", "source_v", "snapshot_digest"),
        evidence_policy="post_commit_content_bound_snapshot_in_prompt",
        history_policy="bound_subject_snapshot_only",
    ),
    _llm_role_contract(
        "master_plan_audit",
        r"^MASTER_PLAN_AUDIT$",
        renderer="prompts/master_plan_audit.md::_run_master_plan_audit",
        producer_file="web/core/audit_agents.py",
        producer_name="_render_master_plan_audit_provider_prompt",
        template_paths=("web/core/prompts/master_plan_audit.md",),
        evidence_kind="compiled_plan_completion_history",
        required_evidence_fields=(
            "source_v", "next_v", "plan_digest", "history_digest",
            "direction_audit_digest", "h2h_snapshot_digest",
        ),
        evidence_policy="compiled_plan_plus_annotated_completion_commits",
        history_policy="annotated_strict_completion_tags_only",
    ),
    _llm_role_contract(
        "degeneration_diagnosis",
        r"^DEGENERATION_DIAGNOSIS$",
        renderer="prompts/degeneration_diagnosis.md::_run_degeneration_diagnosis",
        producer_file="web/core/audit_agents.py",
        producer_name="_render_degeneration_provider_prompt",
        template_paths=("web/core/prompts/degeneration_diagnosis.md",),
        evidence_kind="frozen_degeneration_window",
        required_evidence_fields=(
            "source_v", "history_digest", "rating_digest",
            "strategy_changes_digest",
        ),
        evidence_policy="annotated_completion_commits_plus_frozen_rating_tail",
        history_policy="annotated_strict_completion_tags_only",
    ),
    _llm_role_contract(
        "combined_analyst",
        r"^COMBINED ANALYST$",
        renderer="prompts/combined_analyst.md::_run_combined_analysis",
        producer_file="web/core/combined_analyst.py",
        producer_name="_render_combined_provider_prompt",
        template_paths=("web/core/prompts/combined_analyst.md",),
        evidence_kind="immutable_generation_evaluation_bundle",
        required_evidence_fields=("source_v", "frozen_bundle_digest"),
        evidence_policy="generation_immutable_evaluation_snapshot_in_prompt",
        history_policy="frozen_generation_rating_tail_only",
    ),
    _llm_role_contract(
        "official_platform_analysis",
        r"^OFFICIAL PLATFORM COMPLIANCE ANALYST$",
        renderer="prompts/official_platform_analysis.md::build_official_analysis_prompt",
        producer_file="web/core/official_llm_analysis.py",
        producer_name="_render_official_provider_prompt",
        template_paths=("web/core/prompts/official_platform_analysis.md",),
        evidence_kind="compact_official_compliance_evidence",
        required_evidence_fields=("evidence_id", "compact_evidence_digest"),
        evidence_policy="compact_content_bound_official_evidence_in_prompt",
        history_policy="forbidden",
    ),
    _llm_role_contract(
        "operator_sdk_probe",
        r"^OPERATOR SDK PROBE$",
        renderer="operator_sdk_probe.py::build_probe_prompt",
        producer_file="web/core/operator_sdk_probe.py",
        producer_name="_render_operator_probe_provider_prompt",
        evidence_kind="operator_exact_file_probe",
        required_evidence_fields=("repo_root", "local_evidence_digest"),
        scope_policy="operator_exact_files",
        tools=(("Read", "Bash"),),
        read_scope="exact_system_oracle_and_runtime_files",
        evidence_policy="system_collected_exact_file_hashes_and_tool_receipts",
        requires_read_scope=True,
        allows_exact_bash_commands=True,
        requires_frozen_evidence_guard=True,
        fixed_read_files=(
            "docs/official-raise-boundary-oracle-2026-07-11.md",
            "docs/official-terminal-settlement-oracle-2026-07-11.md",
            "docs/official-allin-runout-wire-oracle-2026-07-19.md",
            "sever/server/transport.py",
        ),
        fixed_bash_commands=(
            "sha256sum docs/official-raise-boundary-oracle-2026-07-11.md docs/official-terminal-settlement-oracle-2026-07-11.md docs/official-allin-runout-wire-oracle-2026-07-19.md sever/server/transport.py",
            "rg -n 'writer.write\\(payload\\)|invalid_server_message_delimiter|take_client_action|idle_flush_sec' sever/server/transport.py",
        ),
    ),
)


def active_llm_role_contracts():
    """Return the immutable active registry for tests and diagnostics."""

    return ACTIVE_LLM_ROLE_CONTRACTS


def resolve_llm_role_contract(role_name):
    normalized = str(role_name or "").strip()
    for contract in ACTIVE_LLM_ROLE_CONTRACTS:
        if contract.role_pattern.search(normalized):
            return contract
    raise LLMRoleContractError(
        f"unregistered active LLM role: {normalized or '<empty>'}"
    )


_LLM_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_LLM_RECEIPT_AUTHORITY = object()
_LLM_RECEIPT_SCHEMA = "national_tcp_llm_dispatch_receipt_v2"


# --- Rendered-prompt receipts and dispatch-scope validation ----------------
# Extracted to llm_role_dispatch.py as a single business responsibility: issue
# digest-sealed renderer/evidence/MCP receipts, validate rendered prompt
# integrity and complete dispatch scope before the provider call, and render
# the system-owned role-contract suffix.  The role-contract registry itself
# stays in this module (see LLMRoleContract / ACTIVE_LLM_ROLE_CONTRACTS above);
# the receipt-authority singleton (_LLM_RECEIPT_AUTHORITY), the schema tag
# (_LLM_RECEIPT_SCHEMA), and _LLM_PROJECT_ROOT also stay here so identity
# checks remain centralized.  llm_role_dispatch reaches them via ``_lq.``.
#
# Each moved symbol below is re-exported as a thin delegate so external
# ``from llm_query import <name>`` imports and test monkeypatches on
# ``llm_query.<name>`` continue to work; the real bodies live in
# llm_role_dispatch.py.

LLMRendererReceipt = _rd.LLMRendererReceipt
LLMEvidenceReceipt = _rd.LLMEvidenceReceipt
LLMMCPReceipt = _rd.LLMMCPReceipt
LLMDispatchReceipt = _rd.LLMDispatchReceipt
LLMRenderedMaterial = _rd.LLMRenderedMaterial
RenderedLLMPrompt = _rd.RenderedLLMPrompt
FrozenLLMCapability = _rd.FrozenLLMCapability


def _receipt_digest(*args, **kwargs):
    """Delegate to llm_role_dispatch."""
    return _rd._receipt_digest(*args, **kwargs)


def _normalize_receipt_value(*args, **kwargs):
    """Delegate to llm_role_dispatch."""
    return _rd._normalize_receipt_value(*args, **kwargs)


def _canonical_strict_authority_json(*args, **kwargs):
    """Delegate to llm_role_dispatch."""
    return _rd._canonical_strict_authority_json(*args, **kwargs)


def _assert_strict_authority_unchanged(*args, **kwargs):
    """Delegate to llm_role_dispatch."""
    return _rd._assert_strict_authority_unchanged(*args, **kwargs)


def _project_strict_authority_state(*args, **kwargs):
    """Delegate to llm_role_dispatch."""
    return _rd._project_strict_authority_state(*args, **kwargs)


def _project_relative_path(*args, **kwargs):
    """Delegate to llm_role_dispatch."""
    return _rd._project_relative_path(*args, **kwargs)


def _sha256_file(*args, **kwargs):
    """Delegate to llm_role_dispatch."""
    return _rd._sha256_file(*args, **kwargs)


def _producer_binding(*args, **kwargs):
    """Delegate to llm_role_dispatch."""
    return _rd._producer_binding(*args, **kwargs)


def _type_identity(*args, **kwargs):
    """Delegate to llm_role_dispatch."""
    return _rd._type_identity(*args, **kwargs)


def _active_mcp_config_payload(*args, **kwargs):
    """Delegate to llm_role_dispatch."""
    return _rd._active_mcp_config_payload(*args, **kwargs)


def _issue_llm_dispatch_receipt(*args, **kwargs):
    """Delegate to llm_role_dispatch."""
    return _rd._issue_llm_dispatch_receipt(*args, **kwargs)


def render_llm_prompt(*args, **kwargs):
    """Delegate to llm_role_dispatch."""
    return _rd.render_llm_prompt(*args, **kwargs)


def _validate_rendered_llm_prompt(*args, **kwargs):
    """Delegate to llm_role_dispatch."""
    return _rd._validate_rendered_llm_prompt(*args, **kwargs)


def _validate_llm_dispatch_receipt(*args, **kwargs):
    """Delegate to llm_role_dispatch."""
    return _rd._validate_llm_dispatch_receipt(*args, **kwargs)


def _llm_selected_tools(*args, **kwargs):
    """Delegate to llm_role_dispatch."""
    return _rd._llm_selected_tools(*args, **kwargs)


def _llm_selected_tool_set(*args, **kwargs):
    """Delegate to llm_role_dispatch."""
    return _rd._llm_selected_tool_set(*args, **kwargs)


def _llm_selected_mcp_servers(*args, **kwargs):
    """Delegate to llm_role_dispatch."""
    return _rd._llm_selected_mcp_servers(*args, **kwargs)


def _raw_scope_entries(*args, **kwargs):
    """Delegate to llm_role_dispatch."""
    return _rd._raw_scope_entries(*args, **kwargs)


def _canonical_scope_paths(*args, **kwargs):
    """Delegate to llm_role_dispatch."""
    return _rd._canonical_scope_paths(*args, **kwargs)


def _scope_evidence_provenance(*args, **kwargs):
    """Delegate to llm_role_dispatch."""
    return _rd._scope_evidence_provenance(*args, **kwargs)


def _validate_role_scope(*args, **kwargs):
    """Delegate to llm_role_dispatch."""
    return _rd._validate_role_scope(*args, **kwargs)


def validate_llm_role_dispatch(*args, **kwargs):
    """Delegate to llm_role_dispatch."""
    return _rd.validate_llm_role_dispatch(*args, **kwargs)


def render_llm_role_contract_suffix(*args, **kwargs):
    """Delegate to llm_role_dispatch."""
    return _rd.render_llm_role_contract_suffix(*args, **kwargs)


def bind_llm_role_provider_prompt(*args, **kwargs):
    """Delegate to llm_role_dispatch."""
    return _rd.bind_llm_role_provider_prompt(*args, **kwargs)


# --- Guard hooks and shell parsing (extracted to llm_query_guards.py) ---
# Re-export for backward compatibility. Existing `from llm_query import
# _make_subagent_*` imports continue to work.
from llm_query_guards import (  # noqa: F401
    _cancelled_event,
    _format_runtime_path_contract,
    _iter_shell_write_redirect_targets,
    _make_exact_bash_allowlist_guard,
    _make_master_evidence_read_guard,
    _make_subagent_cost_guard,
    _make_subagent_llm_availability_guard,
    _make_subagent_read_scope_guard,
    _make_subagent_readonly_guard,
    _make_subagent_write_guard,
    _master_live_evidence_read_violation,
    _merge_allowed_read_scopes,
    _merge_hooks,
    _normalize_allowed_read_scope,
    _normalize_allowed_write_scope,
    _project_root_for_guard,
    _read_path_violation,
    _readonly_guard_recovery_hint,
    _record_llm_tool_trace_event,
    _role_requires_frozen_evidence_guard,
    _subagent_bash_cost_detector,
    _subagent_bash_is_mutation,
    _subagent_bash_mutation_detector,
    _subagent_bash_write_scope_violation,
    _subagent_read_scope_violation,
    _subagent_readonly_mutation_violation,
    capture_llm_tool_trace,
    llm_cancel_scope,
)




# --- Role-IO observability, timeout policy, and thinking options -----------
# Extracted to llm_role_observability.py as a single business responsibility.
# Constants and functions remain reachable through this module for backward
# compatibility (tests monkeypatch them on llm_query.<name>); the delegate
# bodies in llm_role_observability read these via ``_lq.<NAME>`` so the test
# patches take effect.
_ROLE_IO_ROTATION_LOCK = _ro._ROLE_IO_ROTATION_LOCK

#: Cap a single role-IO log at 20MB before rotating to one backup (``.1``).
#: Historical role logs grew without an upper bound; this is the structural cap
#: (lowered here because role-IO files are append-heavy and per-role).
_ROLE_IO_MAX_BYTES = _ro._ROLE_IO_MAX_BYTES


_LLM_FIRST_ACTIVITY_WARN_SEC = _ro._LLM_FIRST_ACTIVITY_WARN_SEC

_LLM_PROGRESS_INTERVAL_SEC = _ro._LLM_PROGRESS_INTERVAL_SEC

_LLM_SILENCE_WARN_SEC = _ro._LLM_SILENCE_WARN_SEC

_ROLE_TIMEOUT_DEFAULTS = _ro._ROLE_TIMEOUT_DEFAULTS


# --- Extended-thinking configuration ---------------------------------------
# GLM-5.2 via the Anthropic-compatible endpoint:
#
#   * ``thinking.type=adaptive`` — KNOWN BUG: GLM emits 16k-19k+ thinking
#     tokens without ever producing visible output, exhausting the timeout
#     ceiling. Do NOT use ``adaptive``.
#   * ``thinking.type=enabled`` + ``budget_tokens`` — reliable: reason then
#     answer. GLM treats budget as a SOFT TARGET (not a hard cap), so the model
#     may exceed it when deep reasoning is warranted. A large budget (64000)
#     gives GLM full freedom to reason deeply.
#   * ``effort=max`` — GLM's strongest reasoning depth. Confirmed NOT a
#     death-loop: thinking tokens grow linearly and the model eventually emits
#     visible text. It is simply SLOW, requiring role timeouts of 1800-3600s
#     (see _ROLE_TIMEOUT_DEFAULTS and deploy/tencent-cloud/env.runtime). The
#     earlier "infinite loop" diagnosis was a misattribution caused by killing
#     the stream at 900s while GLM was still productively reasoning.
#
# All three are environment-overridable via POK_LLM_THINKING_MODE,
# POK_LLM_THINKING_BUDGET, and POK_LLM_EFFORT.
def _llm_thinking_options() -> dict:
    return _ro._llm_thinking_options()


def _role_timeout_policy(role_name: str) -> dict:
    """Return hard stream timeout policy for a role.

    Values <=0 disable that timeout. Environment overrides are intentionally
    role-scoped so slow backends can be tuned without changing code.
    """
    return _ro._role_timeout_policy(role_name)


# These three exception types are defined in llm_provider_attempt and
# re-exported here as aliases so existing imports, ``isinstance`` checks, and
# ``pytest.raises`` calls preserve class identity.
LLMStreamNextTimeout = _pa.LLMStreamNextTimeout
LLMProviderCleanupError = _pa.LLMProviderCleanupError
LLMProviderCleanupBlocked = _pa.LLMProviderCleanupBlocked


class LLMRoleTimeout(asyncio.TimeoutError):
    """Raised when a role exceeds first-activity, idle, or total timeout."""

    def __init__(
        self,
        role_name,
        timeout_kind,
        timeout_sec,
        *,
        pending_stream_task=None,
    ):
        self.role_name = role_name
        self.timeout_kind = timeout_kind
        self.timeout_sec = timeout_sec
        self.pending_stream_task = pending_stream_task
        super().__init__(
            f"{role_name}: LLM {timeout_kind} timeout after {timeout_sec:.1f}s"
        )


def set_shutdown_manager(shutdown_mgr, *, owner_id: str | None = None) -> bool:
    """Bind or clear the process shutdown state with optional owner fencing.

    Web/runtime launchers pass their exact AppState owner id.  A late cleanup
    from owner A therefore cannot clear or replace owner B's manager after B
    has acquired the process.  ``orchestrator_loop`` repeats the bind without
    an owner; that is accepted only when it supplies the already-bound object,
    preserving the outer launcher's owner identity.  Standalone CLI callers
    remain an explicitly unowned, single-process boundary.
    """

    global _shutdown_manager, _shutdown_manager_owner
    normalized_owner = (
        owner_id
        if isinstance(owner_id, str) and owner_id.strip()
        else None
    )
    with _SHUTDOWN_MANAGER_LOCK:
        if _shutdown_manager_owner is not None:
            if normalized_owner != _shutdown_manager_owner:
                if (
                    normalized_owner is None
                    and shutdown_mgr is _shutdown_manager
                ):
                    # Idempotent inner Orchestrator bind of the exact manager
                    # already owned by the outer Web launch transaction.
                    return True
                return False
        if shutdown_mgr is None:
            if (
                normalized_owner is not None
                and normalized_owner != _shutdown_manager_owner
            ):
                return False
            _shutdown_manager = None
            _shutdown_manager_owner = None
            return True
        _shutdown_manager = shutdown_mgr
        _shutdown_manager_owner = normalized_owner
        return True


def _is_shutdown_requested() -> bool:
    try:
        with _SHUTDOWN_MANAGER_LOCK:
            manager = _shutdown_manager
        return bool(manager and manager.is_shutting_down)
    except Exception:
        return False


def is_operator_shutdown_requested() -> bool:
    """Return the owner-fenced process shutdown edge for activity reducers.

    This is deliberately read-only.  Durable domains may use it only to
    classify a contemporaneous ``CancelledError``; it is not evidence that an
    arbitrary provider exception or an old lease was operator-interrupted.
    """

    return _is_shutdown_requested()


def _emit_llm_event(category, severity, message, **fields):
    """Emit an LLM lifecycle event without letting logging affect execution."""
    return _ro._emit_llm_event(category, severity, message, **fields)


def _role_log_metadata(log_file_path):
    return _ro._role_log_metadata(log_file_path)


def _role_log_basename(log_file_path):
    """Return a short relative path for metrics logging (e.g. v1/.../master_io.txt)."""
    return _ro._role_log_basename(log_file_path)


def _tools_metadata(tools):
    return _ro._tools_metadata(tools)


def _usage_metadata(usage):
    return _ro._usage_metadata(usage)


def _llm_failure_severity(exc: Exception) -> str:
    """Classify known noisy SDK/business failures without hiding hard failures."""
    return _ro._llm_failure_severity(exc)


def _append_role_io(log_file_path, text):
    """Append text to a role-IO log file with fcntl locking + 20MB rotation.

    Replaces the bare ``with open(path, "a") as lf: lf.write(...)`` pattern that
    had no locking and no size cap.

      - fcntl LOCK_EX via ``evolution_infra.locked_file`` → cross-process +
        cross-thread safe when orchestrator roles append concurrently.
      - Before writing: if a non-strict file exceeds ``_ROLE_IO_MAX_BYTES``
        (20MB), copy its locked bytes to ``.1`` (single overwrite backup), then
        truncate the still-locked live inode. Strict invocation logs never
        rotate because their complete bytes are immutable evidence.
      - Each appended chunk is prefixed with ``[<run_id>] `` (or ``[-]`` when
        no run_id is resolvable) so role-IO lines join app.log + events.jsonl
        on the same correlation key (RC6).

    Never raises — logging must not crash the pipeline. Returns silently on any
    error (the underlying stream processing / return value is unaffected).
    """
    return _ro._append_role_io(log_file_path, text)


def extract_result_error(message) -> str:
    """Extract diagnostic error text from a ResultMessage.

    Uses correct SDK attributes:
    - message.errors: list[str]|None — error messages from the SDK
    - message.api_error_status: int|None — HTTP status code (429, 500, etc.)

    Falls back to 'Unknown SDK error' if no error info is available.
    """
    _err_list = getattr(message, 'errors', None) or []
    _status = getattr(message, 'api_error_status', None)
    if _err_list:
        return '; '.join(str(e) for e in _err_list)
    if _status:
        return f'API error {_status}'
    return 'Unknown SDK error'


def _is_rate_limited(output: str) -> bool:
    # Long responses are never rate-limit errors — avoid false positives
    # when LLM discusses "rate limit" or "overloaded" in normal output.
    # NOTE: 429 "Request rejected" is handled separately by _is_quota_exceeded()
    # to avoid triggering the 529 exponential-backoff retry loop.
    if len(output) > 2000:
        return False
    return (
        "overloaded" in output.lower()
        or "该模型当前访问量过大" in output
        or "rate limit" in output.lower()
        or re.search(r'(?:status["\s:=]+529|HTTP/\d\.?\d?\s+529|error.*529)', output, re.IGNORECASE) is not None
    )


def _is_quota_exceeded(output: str) -> bool:
    """Detect 429 quota exhaustion (distinct from 529 overloaded).

    Matches the GLM API error pattern:
        "Request rejected (429) · [1308][已达到 5 小时的使用上限...]"
    """
    if len(output) > 2000:
        return False
    return (
        "Request rejected (429)" in output
        or ("已达到" in output and "使用上限" in output)
    )


def _trim_to_budget(text: str, max_chars: int, tail: bool = False) -> str:
    """Trim text to max_chars. If tail=True, keep the LAST max_chars (most recent content)."""
    if len(text) <= max_chars:
        return text
    note = "\n...[TRIMMED]\n"
    if tail:
        return note + text[-(max_chars - len(note)):]
    return text[:max_chars - len(note)] + note


def _new_provider_attempt(transport):
    """Delegate to llm_provider_attempt."""
    return _pa._new_provider_attempt(transport)


_CANONICAL_ABANDON_RESULT_FIELDS = _pa._CANONICAL_ABANDON_RESULT_FIELDS
TERMINAL_ABANDON_RESULT_OWNER_TOOLS = _pa.TERMINAL_ABANDON_RESULT_OWNER_TOOLS
_EVOLUTION_PROVIDER_TOOL_PREFIX = _pa._EVOLUTION_PROVIDER_TOOL_PREFIX


def _normalized_provider_tool_name(name: object) -> str:
    """Delegate to llm_provider_attempt."""
    return _pa._normalized_provider_tool_name(name)


def _canonical_provider_tool_args(args: object) -> str | None:
    """Delegate to llm_provider_attempt."""
    return _pa._canonical_provider_tool_args(args)


def register_current_provider_evolution_tool_use(
    tool_use_id: str,
    raw_name: object,
    args: object,
) -> bool:
    """Delegate to llm_provider_attempt."""
    return _pa.register_current_provider_evolution_tool_use(
        tool_use_id, raw_name, args
    )


def settle_current_provider_evolution_tool_use(tool_use_id: str) -> None:
    """Delegate to llm_provider_attempt."""
    _pa.settle_current_provider_evolution_tool_use(tool_use_id)


def _single_canonical_abandon_result(value):
    """Delegate to llm_provider_attempt."""
    return _pa._single_canonical_abandon_result(value)


def cache_verified_provider_terminal_abandon(
    owner_tool: str,
    baseline_checkpoint: dict,
    raw_result,
    args: object,
):
    """Delegate to llm_provider_attempt."""
    return _pa.cache_verified_provider_terminal_abandon(
        owner_tool, baseline_checkpoint, raw_result, args
    )


def current_provider_verified_terminal_abandon():
    """Delegate to llm_provider_attempt."""
    return _pa.current_provider_verified_terminal_abandon()


def _capture_owned_provider_process(attempt):
    """Delegate to llm_provider_attempt."""
    return _pa._capture_owned_provider_process(attempt)


def _register_unresolved_provider_attempt(attempt, reason, *tasks):
    """Delegate to llm_provider_attempt."""
    return _pa._register_unresolved_provider_attempt(attempt, reason, *tasks)


def _provider_attempt_exit_confirmed(attempt):
    """Delegate to llm_provider_attempt."""
    return _pa._provider_attempt_exit_confirmed(attempt)


def _resolve_provider_attempt_if_stopped(attempt):
    """Delegate to llm_provider_attempt."""
    return _pa._resolve_provider_attempt_if_stopped(attempt)


def _assert_no_unresolved_provider_attempts():
    """Delegate to llm_provider_attempt."""
    return _pa._assert_no_unresolved_provider_attempts()


def _track_pending_stream_task(task, reason):
    """Delegate to llm_provider_attempt."""
    return _pa._track_pending_stream_task(task, reason)


def _provider_stream_cancel_grace():
    """Delegate to llm_provider_attempt."""
    return _pa._provider_stream_cancel_grace()


async def cancel_provider_stream_task_bounded(
    task,
    reason,
    *,
    attempt=None,
    grace=None,
):
    """Delegate to llm_provider_attempt."""
    return await _pa.cancel_provider_stream_task_bounded(
        task, reason, attempt=attempt, grace=grace
    )


async def _await_stream_next_bounded(stream_iter, timeout):
    """Delegate to llm_provider_attempt."""
    return await _pa._await_stream_next_bounded(stream_iter, timeout)


async def await_provider_stream_next_bounded(stream_iter, timeout):
    """Delegate to llm_provider_attempt."""
    return await _pa.await_provider_stream_next_bounded(stream_iter, timeout)


async def _process_stream(query_gen, log_file_path, ui, role_name):
    """Delegate to llm_query_retry.

    Kept as a thin shell so tests that monkeypatch
    ``llm_query._process_stream`` continue to route through this module
    boundary.
    """

    return await _qr._process_stream(query_gen, log_file_path, ui, role_name)


# The bounded signature-retry helpers (the ``_SIGNATURE_MAX_ATTEMPTS``
# constant, ``_merge_billing_usage`` / ``_record_completed_billing_attempt``
# billing, and the deadline-aware ``_signature_retry_sleep`` /
# ``_raise_signature_retry_total_timeout``) live in llm_query_retry.  The
# constant is re-exported by value; the functions are thin delegate shells so
# ``llm_query.<name>`` imports and monkeypatches keep resolving.
_SIGNATURE_MAX_ATTEMPTS = _qr._SIGNATURE_MAX_ATTEMPTS


def _merge_billing_usage(total, usage):
    """Delegate to llm_query_retry."""
    return _qr._merge_billing_usage(total, usage)


def _record_completed_billing_attempt(
    *,
    role_name,
    ui,
    billing_results,
    fallback_cost,
    fallback_usage,
    attempt,
    billing_call_id,
):
    """Delegate to llm_query_retry."""
    return _qr._record_completed_billing_attempt(
        role_name=role_name,
        ui=ui,
        billing_results=billing_results,
        fallback_cost=fallback_cost,
        fallback_usage=fallback_usage,
        attempt=attempt,
        billing_call_id=billing_call_id,
    )


def _raise_signature_retry_total_timeout(role_name, log_file_path):
    """Delegate to llm_query_retry."""
    return _qr._raise_signature_retry_total_timeout(role_name, log_file_path)


async def _signature_retry_sleep(delay, role_name, log_file_path):
    """Delegate to llm_query_retry."""
    return await _qr._signature_retry_sleep(delay, role_name, log_file_path)


def _consume_task_result(task):
    """Delegate to llm_provider_attempt."""
    return _pa._consume_task_result(task)


async def _bounded_aclose(query_gen, role_name, log_file_path):
    """Delegate to llm_provider_attempt."""
    return await _pa._bounded_aclose(query_gen, role_name, log_file_path)


def _new_owned_sdk_transport(full_prompt, options):
    """Delegate to llm_provider_attempt.

    Kept as a thin shell so tests that monkeypatch
    ``llm_query._new_owned_sdk_transport`` continue to drive transport
    construction through this module boundary.
    """

    return _pa._new_owned_sdk_transport(full_prompt, options)


def create_owned_provider_attempt(full_prompt, options):
    """Delegate to llm_provider_attempt."""
    return _pa.create_owned_provider_attempt(full_prompt, options)


def owned_provider_attempt_transport(attempt):
    """Delegate to llm_provider_attempt."""
    return _pa.owned_provider_attempt_transport(attempt)


@contextlib.contextmanager
def owned_provider_attempt_scope(attempt):
    """Delegate to llm_provider_attempt."""
    with _pa.owned_provider_attempt_scope(attempt) as value:
        yield value


def activate_owned_provider_attempt(attempt):
    """Delegate to llm_provider_attempt."""
    return _pa.activate_owned_provider_attempt(attempt)


def reset_owned_provider_attempt(token):
    """Delegate to llm_provider_attempt."""
    return _pa.reset_owned_provider_attempt(token)


def mark_owned_provider_attempt_unresolved(attempt, reason, task=None):
    """Delegate to llm_provider_attempt."""
    return _pa.mark_owned_provider_attempt_unresolved(attempt, reason, task)


def owned_provider_attempt_exit_confirmed(attempt):
    """Delegate to llm_provider_attempt."""
    return _pa.owned_provider_attempt_exit_confirmed(attempt)


def _refresh_transport_exit_confirmation(attempt):
    """Delegate to llm_provider_attempt."""
    return _pa._refresh_transport_exit_confirmation(attempt)


async def _bounded_owned_transport_close(attempt, role_name, log_file_path):
    """Delegate to llm_provider_attempt."""
    return await _pa._bounded_owned_transport_close(
        attempt, role_name, log_file_path
    )


async def _await_provider_attempt_tasks(attempt):
    """Delegate to llm_provider_attempt."""
    return await _pa._await_provider_attempt_tasks(attempt)


async def _perform_owned_provider_attempt_cleanup(
    query_gen,
    attempt,
    role_name,
    log_file_path,
):
    """Delegate to llm_provider_attempt."""
    return await _pa._perform_owned_provider_attempt_cleanup(
        query_gen, attempt, role_name, log_file_path
    )


async def _cleanup_owned_provider_attempt(
    query_gen,
    attempt,
    role_name,
    log_file_path,
):
    """Delegate to llm_provider_attempt."""
    return await _pa._cleanup_owned_provider_attempt(
        query_gen, attempt, role_name, log_file_path
    )


async def cleanup_owned_provider_attempt(
    query_gen,
    attempt,
    role_name,
    log_file_path,
):
    """Delegate to llm_provider_attempt."""
    return await _pa.cleanup_owned_provider_attempt(
        query_gen, attempt, role_name, log_file_path
    )


async def _run_stream_with_signature_retry(
    full_prompt, options, log_file_path, ui, role_name, *, semaphore=None
):
    """Delegate to llm_query_retry.

    Kept as a thin shell so tests that monkeypatch
    ``llm_query._run_stream_with_signature_retry`` (and the ``claude_query``,
    ``_process_stream``, ``asyncio.sleep`` it drives) continue to route through
    this module boundary; the companion reads those names via ``_lq``.

    ``semaphore`` must be forwarded: Phase 5a acquires the global LLM permit
    per attempt inside the companion. Dropping the kwarg here raises
    ``unexpected keyword argument 'semaphore'`` and aborts every role
    (including Combined analyst) before any provider stream starts.
    """

    return await _qr._run_stream_with_signature_retry(
        full_prompt, options, log_file_path, ui, role_name,
        semaphore=semaphore,
    )


async def _run_stream_with_signature_retry_attempts(
    full_prompt, options, log_file_path, ui, role_name, *, semaphore=None
):
    """Delegate to llm_query_retry."""
    return await _qr._run_stream_with_signature_retry_attempts(
        full_prompt, options, log_file_path, ui, role_name,
        semaphore=semaphore,
    )


async def run_claude_query(
    prompt,
    context_files,
    ui,
    role_name,
    log_file_path,
    model="sonnet",
    tools=None,
    allowed_write_dir=None,
    allowed_evidence_snapshot_dir=None,
    allowed_read_dirs=None,
    strict_authority=None,
    exact_bash_commands=None,
):
    """Run a Claude query via the Agent SDK with cost tracking and typed streaming.

    tools: list of built-in tool names (e.g. ["Bash", "Read"]) or a ToolsPreset dict.
           When None, no built-in tools are exposed to the model.
    allowed_write_dir: A1 fix (2026-06-30): when set (a pathlib.Path / str), a
           PreToolUse guard hook is installed that BLOCKS this sub-agent's
           Bash/Edit/Write from mutating anything OUTSIDE this directory.
           Workers/crossover pass their target bot dir so a rogue worker prompt
           cannot edit web/core/*.py, other bot dirs, or pipeline state (the
           orchestrator-level guard does not cover sub-agents).
    allowed_read_dirs: the complete role-scoped filesystem read capability.
           A scalar/sequence grants exact directory roots; a mapping may use
           ``dirs`` and ``files`` for mixed directory/exact-file authority.
           Read and every statically provable Bash input must resolve inside
           this scope (plus the exact write scope, context files, and frozen
           evidence snapshot supplied by the system). Sensitive Git metadata,
           archived trees, unlisted bot versions, symlink aliases, and complex
           shell/Python readers remain denied even if a broad root is supplied.
    exact_bash_commands: optional complete-command allowlist.  When supplied,
           every Bash call must match one of these strings (apart from outer
           whitespace).  This is intended for read-only operator probes, not
           general Worker execution.
    dispatch_receipt: sealed renderer/evidence/MCP receipt issued from the real
           production callable and current template bytes. Caller-supplied
           renderer-name strings are not accepted.
    """
    call_started_at = time.time()
    from evolution_infra import (
        PROJECT_ROOT,
        MAX_PROMPT_CHARS,
        _BLOCKED_MCP_TOOLS,
        resolve_ui,
    )

    # Headless callers (official-platform certification, CLI probes, offline
    # analysis) intentionally have no dashboard. Normalize that boundary once
    # so stream, retry, cost, and error paths all receive the same UI contract.
    ui = resolve_ui(ui)
    # ClaudeAgentOptions.tools=None means "omit --tools", which lets the CLI
    # expose its default tool set.  That is unsafe under bypassPermissions and
    # contradicts this API's documented zero-tool default.  An explicit empty
    # list is serialized by the pinned SDK as ``--tools ''``.
    if tools is None:
        tools = []
    if exact_bash_commands is not None:
        exact_bash_commands = tuple(
            str(command).strip() for command in exact_bash_commands
        )
    rendered_prompt_receipt = prompt

    # Resolve and validate the concrete runtime label before any wait, budget,
    # provider-availability, or prompt-I/O side effect.  Unknown roles and
    # capability drift therefore cannot hide behind a provider pause or consume
    # an attempt before failing closed.
    role_contract, dispatch_receipt, frozen_capability = validate_llm_role_dispatch(
        role_name,
        tools=tools,
        rendered_prompt=rendered_prompt_receipt,
        provider_path="subagent_sdk",
        mcp_servers=(),
        context_files=context_files or (),
        allowed_read_dirs=allowed_read_dirs,
        allowed_write_dir=allowed_write_dir,
        allowed_evidence_snapshot_dir=allowed_evidence_snapshot_dir,
        strict_authority=strict_authority,
        exact_bash_commands=exact_bash_commands,
        model=model,
    )
    rendered_role_prompt = rendered_prompt_receipt.text
    prompt = rendered_role_prompt
    # From this point onward every prompt prefix, context read, and hook consumes
    # only the immutable normalized capability. Caller-owned lists/dicts may be
    # mutated while the provider quota wait is suspended without changing this
    # dispatch's authority.
    allowed_read_dirs = {
        "dirs": frozen_capability.read_dirs,
        "files": frozen_capability.read_files,
    }
    allowed_write_dir = (
        {
            "dirs": frozen_capability.write_dirs,
            "files": frozen_capability.write_files,
        }
        if (frozen_capability.write_dirs or frozen_capability.write_files)
        else None
    )
    allowed_evidence_snapshot_dir = frozen_capability.evidence_dir
    context_files = frozen_capability.context_files
    exact_bash_commands = frozen_capability.exact_bash_commands or None
    tools = list(frozen_capability.selected_tools)
    model = frozen_capability.model
    # The caller-owned strict descriptor is input only until this point.  All
    # provider-visible schema, receipt, replay, effect and completion work uses
    # this private deep copy, so a quota-wait/thread mutation cannot broaden or
    # redirect the call.  At function exit only resulting state is projected
    # back for the existing accept_role_result API.
    strict_authority_owner = strict_authority
    strict_authority = (
        json.loads(frozen_capability.strict_authority_json)
        if frozen_capability.strict_authority_json is not None
        else None
    )

    # Cost is monitor-only unless the operator enabled a finite positive hard
    # limit in the parent process.  This system-owned check is intentionally
    # outside prompts and MCP arguments, so an LLM cannot grant itself more
    # budget or disable enforcement.  It also prevents starting another billed
    # call after an earlier parallel/sub-agent call crossed the operator limit.
    from orchestrator_cost_policy import assert_operator_cost_limit_available
    assert_operator_cost_limit_available()

    # Every role shares the durable provider pause.  This process-local guard
    # prevents background analysts or a direct MCP call from bypassing the
    # Orchestrator/Worker pre-claim checks after a restart.
    from llm_availability_store import raise_if_llm_paused
    raise_if_llm_paused(role=str(role_name))

    # Pre-check: if already rate-limited, wait before making any API call
    from rate_limiter import rate_limiter
    if rate_limiter.is_blocked():
        _emit_llm_event(
            "pipeline.llm_role_rate_limited_wait", "warn",
            f"{role_name}: waiting for API quota reset",
            role=role_name,
            reset_time=rate_limiter.reset_time_str(),
            **_role_log_metadata(log_file_path),
        )
        if ui:
            ui.log_history(
                f"API 配额受限，等待至 {rate_limiter.reset_time_str()}...",
                "warn",
            )
        await rate_limiter.wait_until_reset()

    strict_schema_suffix = ""
    if strict_authority is not None:
        _assert_strict_authority_unchanged(
            strict_authority,
            frozen_capability,
        )
        from strict_authority_workflow import schema_retry_prompt

        strict_schema_suffix = schema_retry_prompt(strict_authority)
    prompt = (
        _format_runtime_path_contract(PROJECT_ROOT, allowed_write_dir)
        + (prompt or "")
        + strict_schema_suffix
    )

    # Build (path, content) pairs for context files
    context_parts = []
    context_chars = 0
    if context_files:
        for cf in context_files:
            if os.path.exists(cf):
                with open(cf, 'r') as f:
                    content = f.read()
                    context_chars += len(content)
                    context_parts.append((cf, content))

    # Assemble prompt with context files, smart-budgeting if needed
    if context_parts:
        ctx_section = "\n\n# Context Files:\n" + "".join(
            f"\n--- {p} ---\n{c}\n" for p, c in context_parts
        )
        full_prompt = prompt + ctx_section
        if len(full_prompt) > MAX_PROMPT_CHARS:
            # Compress context_files proportionally while keeping base prompt intact
            budget_for_files = MAX_PROMPT_CHARS - len(prompt) - 500
            if budget_for_files > 0:
                per_file = max(budget_for_files // len(context_parts), 500)
                ctx_section = "\n\n# Context Files:\n" + "".join(
                    f"\n--- {p} ---\n{_trim_to_budget(c, per_file)}\n"
                    for p, c in context_parts
                )
                full_prompt = prompt + ctx_section
            else:
                full_prompt = prompt + "\n\n[Context files omitted — prompt too long]"
            ui.log_history(f"Prompt budgeted to {len(full_prompt):,} chars (context compressed)", "warn")
    else:
        full_prompt = prompt
        if len(full_prompt) > MAX_PROMPT_CHARS:
            ui.log_history(
                f"Prompt too long ({len(full_prompt):,} chars); sealed prompt "
                "will fail closed instead of being truncated",
                "warn",
            )

    # The role/evidence contract must be the last provider-visible instruction,
    # after rendered templates, strict-schema repair text, and context files.
    # The binder reserves space for it rather than letting prompt trimming cut
    # away the system-owned authority boundary.
    full_prompt, role_contract = bind_llm_role_provider_prompt(
        rendered_prompt_receipt,
        role_name,
        tools=tools,
        provider_path="subagent_sdk",
        mcp_servers=(),
        context_files=context_files or (),
        allowed_read_dirs=allowed_read_dirs,
        allowed_write_dir=allowed_write_dir,
        allowed_evidence_snapshot_dir=allowed_evidence_snapshot_dir,
        strict_authority=strict_authority,
        exact_bash_commands=exact_bash_commands,
        max_chars=MAX_PROMPT_CHARS,
        provider_prefix=full_prompt,
        frozen_capability=frozen_capability,
        model=model,
    )

    ui.log_io(f"\n[{role_name} PROMPT]", "prompt", role_name)
    ui.log_io(prompt[:200] + "...\n[Context Attached]", "prompt", role_name)
    ui.log_io("\n[WAITING FOR CLAUDE...]\n", "prompt", role_name)

    # An accepted strict result is replayed from its completed durable effect;
    # it is not a second provider invocation.  Keep the original invocation log
    # byte-for-byte stable so embedded proposal/ballot evidence can reuse the
    # original log digest and packet identity after a process restart.
    if not (
        strict_authority is not None
        and strict_authority.get("replay_provider")
    ):
        _append_role_io(
            log_file_path,
            f"\n[{role_name} PROMPT]\n=============================\n"
            + full_prompt
            + "\n=============================\n[CLAUDE OUTPUT]\n",
        )

    # Install runtime hooks:
    # - cost guard for read-only but unbounded Bash (Master git-log stalls)
    # - write-scope guard for workers/crossover when allowed_write_dir is set
    _sub_hooks = None
    _cost_hooks = None
    _availability_hooks = None
    _exact_bash_hooks = None
    if tools and any(t == "Bash" for t in (tools if isinstance(tools, list) else [])):
        _cost_hooks = _make_subagent_cost_guard(role_name)
        _availability_hooks = _make_subagent_llm_availability_guard(role_name)
        if exact_bash_commands is not None:
            _exact_bash_hooks = _make_exact_bash_allowlist_guard(
                role_name,
                exact_bash_commands,
            )
            if not _exact_bash_hooks:
                raise RuntimeError(
                    "exact Bash allowlist hook is unavailable; refusing SDK dispatch"
                )
    elif exact_bash_commands is not None:
        raise ValueError("exact_bash_commands requires the Bash tool")
    _write_hooks = None
    _readonly_hooks = None
    _master_evidence_hooks = None
    _read_scope_hooks = None
    if allowed_write_dir is not None and tools and any(
        t in ("Bash", "Edit", "Write", "NotebookEdit") for t in (tools if isinstance(tools, list) else [])
    ):
        _write_hooks = _make_subagent_write_guard(allowed_write_dir)
        if not _write_hooks:
            raise RuntimeError(
                "write-scope guard hook is unavailable; refusing SDK dispatch"
            )
    elif allowed_write_dir is None and tools and any(
        t in ("Bash", "Edit", "Write", "NotebookEdit") for t in (tools if isinstance(tools, list) else [])
    ):
        _readonly_hooks = _make_subagent_readonly_guard(role_name)
        if not _readonly_hooks:
            raise RuntimeError(
                "read-only mutation guard hook is unavailable; refusing SDK dispatch"
            )
    if _role_requires_frozen_evidence_guard(role_name):
        allowed_results_dirs = []
        if allowed_write_dir is not None:
            write_scope = _normalize_allowed_write_scope(allowed_write_dir)
            allowed_results_dirs.extend(write_scope.get("dirs") or ())
            allowed_results_dirs.extend(write_scope.get("files") or ())
        if allowed_read_dirs is not None:
            read_scope = _normalize_allowed_read_scope(allowed_read_dirs)
            allowed_results_dirs.extend(read_scope.get("dirs") or ())
            allowed_results_dirs.extend(read_scope.get("files") or ())
        _master_evidence_hooks = _make_master_evidence_read_guard(
            role_name,
            allowed_evidence_snapshot_dir,
            allowed_results_dirs,
        )
        if not _master_evidence_hooks:
            raise RuntimeError(
                "frozen-evidence guard hook is unavailable; refusing SDK dispatch"
            )
    read_scope = _merge_allowed_read_scopes(
        allowed_read_dirs,
        (
            {"dirs": [allowed_evidence_snapshot_dir]}
            if allowed_evidence_snapshot_dir is not None
            else None
        ),
        _normalize_allowed_write_scope(allowed_write_dir),
        {"files": context_files or ()},
    )
    if tools and any(
        tool in {"Read", "Bash"}
        for tool in (tools if isinstance(tools, list) else [])
    ):
        _read_scope_hooks = _make_subagent_read_scope_guard(
            role_name,
            read_scope,
        )
        if not _read_scope_hooks:
            raise RuntimeError(
                "role-scoped filesystem read hook is unavailable; refusing SDK dispatch"
            )
    _sub_hooks = _merge_hooks(
        _cost_hooks,
        _write_hooks,
        _readonly_hooks,
        _read_scope_hooks,
        _master_evidence_hooks,
        _availability_hooks,
        _exact_bash_hooks,
    )
    options_kwargs = dict(
        model=model,
        permission_mode="bypassPermissions",
        cwd=str(PROJECT_ROOT),  # pok/ — workers use relative paths like bots/national_vN/
        mcp_servers={},
        strict_mcp_config=True,  # Direct sub-agents must not auto-start user/global MCP servers.
        tools=tools,
        disallowed_tools=_BLOCKED_MCP_TOOLS,
        # CLAUDE.md/AGENTS.md memory injection: setting_sources=["project"] makes
        # the CLI discover project-level CLAUDE.md from cwd. system_prompt preset
        # append ("") triggers --append-system-prompt '' instead of --system-prompt
        # '', so the CLI keeps its DEFAULT system prompt (which includes the
        # CLAUDE.md/AGENTS.md memory) and merely appends an empty string.  Without
        # these two fields, the SDK injects --system-prompt '' which OVERWRITES
        # the default system prompt and suppresses all memory injection — GLM
        # never sees the architecture contract (Strict candidate ABI, reachable_chain
        # semantics, namespace rules). Only "project" is loaded (not "user") to
        # avoid pulling ~/.claude/settings.json's CLAUDE_CODE_EFFORT_LEVEL which
        # would override the POK_LLM_EFFORT env var.
        setting_sources=["project"],
        system_prompt={"type": "preset", "append": ""},
        **_llm_thinking_options(),
    )
    if _sub_hooks:
        options_kwargs["hooks"] = _sub_hooks
    options = ClaudeAgentOptions(**options_kwargs)

    lifecycle_fields = {
        "role": role_name,
        "role_contract_id": role_contract.role_id,
        "role_contract_renderer": role_contract.renderer,
        "role_evidence_policy": role_contract.evidence_policy,
        "role_history_policy": role_contract.history_policy,
        "model": model,
        "prompt_chars": len(prompt or ""),
        "full_prompt_chars": len(full_prompt or ""),
        "context_file_count": len(context_parts),
        "context_chars": context_chars,
        "allowed_write_dir": str(allowed_write_dir) if allowed_write_dir is not None else None,
        "allowed_read_dir_count": len(read_scope.get("dirs") or ()),
        "allowed_read_file_count": len(read_scope.get("files") or ()),
        "exact_bash_command_count": (
            len(tuple(exact_bash_commands))
            if exact_bash_commands is not None
            else None
        ),
        **_tools_metadata(tools),
        **_role_timeout_policy(role_name),
        **_role_log_metadata(log_file_path),
        # Thinking config for metrics (filled before dispatch; semaphore_wait
        # is populated after acquire).
        "thinking_mode": os.environ.get("POK_LLM_THINKING_MODE", "enabled"),
        "thinking_budget": int(os.environ.get("POK_LLM_THINKING_BUDGET", "64000")),
        "effort": os.environ.get("POK_LLM_EFFORT", "max"),
        "global_concurrency": int(os.environ.get("POK_GLOBAL_LLM_CONCURRENCY", "2")),
        "semaphore_wait_sec": None,  # populated after acquire
    }

    # The strict bootstrap creates a one-attempt fenced effect only after the
    # complete runtime/path contract and context-budgeting have produced the
    # exact provider prompt, but before the SDK dispatch.  The mutable call
    # descriptor is returned to role code out-of-band (the public 3-tuple API
    # remains unchanged) and cannot be accepted until a real ResultMessage has
    # completed this effect.
    strict_provider_capture = None
    strict_provider_token = None
    if strict_authority is not None:
        _assert_strict_authority_unchanged(
            strict_authority,
            frozen_capability,
        )
        from strict_authority_workflow import dispatch_call

        dispatch_call(
            strict_authority,
            full_prompt=full_prompt,
            tools=tools,
            owner=f"llm_query:{os.getpid()}:{role_name}",
            actual_role=str(role_name),
            model=str(model),
            lease_seconds=(
                max(
                    60.0,
                    float(lifecycle_fields.get("total_timeout") or 0) + 60.0,
                )
                if float(lifecycle_fields.get("total_timeout") or 0) > 0
                else 3600.0
            ),
        )
        strict_provider_capture = {
            "invocation_id": strict_authority.get("invocation_id"),
            "effect_id": strict_authority.get("effect_id"),
            "results": [],
        }
        strict_provider_token = _STRICT_PROVIDER_RESULTS.set(
            strict_provider_capture
        )
    _emit_llm_event(
        "pipeline.llm_role_start", "info",
        f"{role_name}: LLM call started",
        startup_elapsed_sec=round(time.time() - call_started_at, 2),
        **lifecycle_fields,
    )

    # Initial query — retry transient SDK stream errors (signature field missing).
    # claude_agent_sdk 0.2.91 intermittently raises ClaudeSDKError "Missing required
    # field in assistant message: 'signature'"; a fresh query usually succeeds.
    # Without this retry, the error propagates and the calling tool either rejects
    # (critic) or skips (battle_exp), stalling the pipeline.
    try:
        if strict_authority is not None and strict_authority.get(
            "replay_provider"
        ):
            full_text = [str(strict_authority.get("replay_raw_output") or "")]
            cost_usd = strict_authority.get("replay_cost_usd")
            usage = strict_authority.get("replay_usage")
            _emit_llm_event(
                "pipeline.strict_llm_authority_replayed",
                "info",
                f"{role_name}: replayed accepted strict provider result",
                role=role_name,
                slot=strict_authority.get("slot"),
                effect_id=strict_authority.get("effect_id"),
                **_role_log_metadata(log_file_path),
            )
        else:
            # Global LLM concurrency limiter (producer-consumer): cap
            # simultaneous in-flight provider streams at GLOBAL_LLM_CONCURRENCY
            # (default 2).  This is the single chokepoint covering all 17+
            # run_claude_query call sites (Master Scouts/Critics/final, Workers,
            # Review, Critic, direction_audit, crossover, etc.).  FIFO ordering
            # prevents starvation; Master/Worker stages are temporally separated
            # by the linear pipeline stage machine so they rarely contend.
            #
            # The permit is acquired PER-ATTEMPT inside the signature-retry loop
            # (llm_query_retry._run_stream_with_signature_retry_attempts), NOT
            # around the whole retry loop.  This means signature-retry backoff
            # sleeps RELEASE the permit so other LLM work can fill the gap —
            # critical for keeping the pool fully utilized.
            #
            # Partitioned semaphore: consumer-lane roles (review/critic) get an
            # exclusive sub-pool so the publication critical path is never
            # starved by producer Scout bursts under multi-ahead.
            from llm_concurrency import get_llm_semaphore_for_role

            _role_sem = get_llm_semaphore_for_role(role_name)
            _sem_wait_start = time.time()
            lifecycle_fields["semaphore_wait_sec"] = round(time.time() - _sem_wait_start, 3)
            full_text, cost_usd, usage = await _run_stream_with_signature_retry(
                full_prompt, options, log_file_path, ui, role_name,
                semaphore=_role_sem,
            )

        streamed_output = "\n".join(full_text)
        output = streamed_output
        if strict_authority is not None and not strict_authority.get(
            "replay_provider"
        ):
            from strict_authority_workflow import canonical_provider_output

            output = canonical_provider_output(
                (strict_provider_capture or {}).get("results", [])
            )
            if output != streamed_output:
                _append_role_io(
                    log_file_path,
                    "\n[STRICT_TERMINAL_OUTPUT_AUTHORITY] "
                    f"stream_sha256={hashlib.sha256(streamed_output.encode('utf-8')).hexdigest()} "
                    f"terminal_sha256={hashlib.sha256(output.encode('utf-8')).hexdigest()}\n",
                )
                _emit_llm_event(
                    "pipeline.strict_llm_terminal_output_selected",
                    "info",
                    f"{role_name}: terminal SDK result selected over streaming aggregate",
                    role=role_name,
                    stream_output_chars=len(streamed_output),
                    terminal_output_chars=len(output),
                    stream_output_digest=hashlib.sha256(
                        streamed_output.encode("utf-8")
                    ).hexdigest(),
                    terminal_output_digest=hashlib.sha256(
                        output.encode("utf-8")
                    ).hexdigest(),
                    **_role_log_metadata(log_file_path),
                )

        # Every completed SDK Result (including an empty-output/signature retry)
        # was already recorded and UI-projected inside
        # _run_stream_with_signature_retry.  Re-check here only to cover a
        # concurrent sibling that crossed the operator threshold meanwhile.
        from orchestrator_cost_policy import assert_operator_cost_limit_available
        assert_operator_cost_limit_available()
        if strict_authority is not None and not strict_authority.get(
            "provider_completed"
        ):
            from strict_authority_workflow import complete_provider_call

            complete_provider_call(
                strict_authority,
                raw_output=output,
                provider_results=(strict_provider_capture or {}).get(
                    "results", []
                ),
            )
        _emit_llm_event(
            "pipeline.llm_role_done", "success",
            f"{role_name}: LLM call finished in {time.time() - call_started_at:.1f}s",
            elapsed_sec=round(time.time() - call_started_at, 2),
            cost_usd=round(cost_usd, 6) if cost_usd is not None else None,
            output_chars=len(output or ""),
            text_block_count=len(full_text or []),
            **_usage_metadata(usage),
            **lifecycle_fields,
        )
        return output, cost_usd, usage
    except LLMAvailabilityBlocked as e:
        if strict_authority is not None:
            from strict_authority_workflow import fail_provider_call

            fail_provider_call(strict_authority, e)
        persistence_error = None
        try:
            from llm_availability_store import persist_llm_pause

            pause_state = persist_llm_pause(e)
        except Exception as exc:
            pause_state = None
            persistence_error = f"{type(exc).__name__}: {str(exc)[:500]}"
        _emit_llm_event(
            "pipeline.llm_role_availability_pause_persisted"
            if pause_state
            else "pipeline.llm_role_availability_pause_persist_failed",
            "error",
            f"{role_name}: provider availability control activated",
            elapsed_sec=round(time.time() - call_started_at, 2),
            availability_issue=e.issue.as_dict(),
            pause_persisted=bool(pause_state),
            persistence_error=persistence_error,
            **lifecycle_fields,
        )
        raise
    except asyncio.CancelledError:
        if strict_authority is not None:
            from strict_authority_workflow import fail_provider_call

            fail_provider_call(strict_authority, "asyncio.CancelledError")
        is_shutdown = _is_shutdown_requested()
        _category, _severity, _cancel_fields = _cancelled_event(
            "pipeline.llm_role_cancelled",
            "pipeline.llm_role_parent_timeout_cancelled",
        )
        if is_shutdown:
            _category = "pipeline.llm_role_shutdown_cancelled"
            _severity = "info"
            _message = f"{role_name}: LLM call stopped during shutdown after {time.time() - call_started_at:.1f}s"
        elif _cancel_fields.get("cancel_reason") == "parent_timeout":
            _scope = _cancel_fields.get("cancel_scope")
            _timeout = _cancel_fields.get("timeout_sec")
            _message = (
                f"{role_name}: LLM call cancelled by parent timeout after {time.time() - call_started_at:.1f}s"
                f" ({_scope}, {_timeout:g}s)"
                if isinstance(_timeout, (int, float))
                else f"{role_name}: LLM call cancelled by parent timeout after {time.time() - call_started_at:.1f}s"
                f" ({_scope})"
            )
        else:
            _message = f"{role_name}: LLM call cancelled after {time.time() - call_started_at:.1f}s"
        _emit_llm_event(
            _category,
            _severity,
            _message,
            elapsed_sec=round(time.time() - call_started_at, 2),
            **_cancel_fields,
            **lifecycle_fields,
        )
        raise
    except Exception as e:
        shutdown_cancel_error = is_shutdown_cancel_error(e)
        shutdown_requested = bool(
            shutdown_cancel_error and _is_shutdown_requested()
        )
        if strict_authority is not None:
            from strict_authority_workflow import fail_provider_call

            fail_provider_call(
                strict_authority,
                _strict_provider_failure_error(
                    e,
                    shutdown_requested=shutdown_requested,
                ),
            )
        if shutdown_cancel_error:
            event_type = (
                "pipeline.llm_role_shutdown_cancelled"
                if shutdown_requested
                else "pipeline.llm_role_process_terminated"
            )
            _emit_llm_event(
                event_type,
                "info" if shutdown_requested else "warn",
                (
                    f"{role_name}: LLM call stopped during shutdown after {time.time() - call_started_at:.1f}s"
                    if shutdown_requested
                    else f"{role_name}: LLM process received SIGTERM after {time.time() - call_started_at:.1f}s"
                ),
                elapsed_sec=round(time.time() - call_started_at, 2),
                exception_type=type(e).__name__,
                error=str(e)[:1000],
                shutdown_requested=shutdown_requested,
                **lifecycle_fields,
            )
            raise asyncio.CancelledError(
                f"{role_name}: LLM process received SIGTERM"
            ) from e
        severity = _llm_failure_severity(e)
        _emit_llm_event(
            "pipeline.llm_role_failed", severity,
            f"{role_name}: LLM call failed after {time.time() - call_started_at:.1f}s: {str(e)[:180]}",
            elapsed_sec=round(time.time() - call_started_at, 2),
            exception_type=type(e).__name__,
            error=str(e)[:1000],
            **lifecycle_fields,
        )
        raise
    finally:
        if strict_provider_token is not None:
            _STRICT_PROVIDER_RESULTS.reset(strict_provider_token)
        _project_strict_authority_state(
            strict_authority_owner,
            strict_authority,
        )


def parse_json_output(output):
    """Delegate to llm_query_retry."""
    return _qr.parse_json_output(output)


def parse_json_output_with_mode(output):
    """Delegate to llm_query_retry."""
    return _qr.parse_json_output_with_mode(output)
