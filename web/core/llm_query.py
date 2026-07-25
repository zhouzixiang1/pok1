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
_LLM_PROVIDER_ATTEMPT = contextvars.ContextVar(
    "llm_provider_attempt", default=None
)
_PROVIDER_CLEANUP_LOCK = threading.Lock()
_UNRESOLVED_PROVIDER_ATTEMPTS = {}


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
        producer_file="web/core/agent_master_validation.py",
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
        producer_file="web/core/agent_master_validation.py",
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
        producer_file="web/core/agent_master_validation.py",
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


@dataclass(frozen=True)
class LLMRendererReceipt:
    role_id: str
    runtime_role: str
    producer_file: str
    producer_name: str
    producer_file_sha256: str
    producer_function_sha256: str
    template_digests: tuple
    rendered_prompt_sha256: str
    rendered_prompt_chars: int
    receipt_digest: str
    producer: object
    _authority: object


@dataclass(frozen=True)
class LLMEvidenceReceipt:
    role_id: str
    provenance_kind: str
    provenance_json: str
    provenance_sha256: str
    renderer_receipt_digest: str
    receipt_digest: str
    _authority: object


@dataclass(frozen=True)
class LLMMCPReceipt:
    role_id: str
    config_json: str
    config_sha256: str
    receipt_digest: str
    _authority: object


@dataclass(frozen=True)
class LLMDispatchReceipt:
    schema: str
    role_id: str
    runtime_role: str
    model: str
    renderer: LLMRendererReceipt
    evidence: LLMEvidenceReceipt
    mcp: LLMMCPReceipt
    receipt_digest: str
    _authority: object


@dataclass(frozen=True)
class LLMRenderedMaterial:
    """Replay result: provider text and its causally derived provenance."""

    text: str
    evidence_kind: str
    evidence_provenance: dict


class RenderedLLMPrompt(str):
    """String-compatible but sealed output of a replayable renderer."""

    def __new__(
        cls,
        *,
        role_id,
        runtime_role,
        text,
        renderer_inputs_json,
        dispatch_receipt,
        producer,
        _authority,
    ):
        instance = str.__new__(cls, str(text))
        object.__setattr__(instance, "role_id", str(role_id))
        object.__setattr__(instance, "runtime_role", str(runtime_role))
        object.__setattr__(instance, "text", str(text))
        object.__setattr__(instance, "renderer_inputs_json", str(renderer_inputs_json))
        object.__setattr__(instance, "dispatch_receipt", dispatch_receipt)
        object.__setattr__(instance, "producer", producer)
        object.__setattr__(instance, "_authority", _authority)
        object.__setattr__(instance, "_sealed", True)
        return instance

    def __setattr__(self, name, value):
        if getattr(self, "_sealed", False):
            raise AttributeError("RenderedLLMPrompt is immutable")
        object.__setattr__(self, name, value)


@dataclass(frozen=True)
class FrozenLLMCapability:
    role_id: str
    model: str
    selected_tools: tuple
    read_dirs: tuple
    read_files: tuple
    write_dirs: tuple
    write_files: tuple
    evidence_dir: str | None
    context_files: tuple
    exact_bash_commands: tuple
    strict_authority_json: str | None
    strict_authority_sha256: str | None
    _authority: object


def _receipt_digest(payload):
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _normalize_receipt_value(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise LLMRoleContractError("non-finite evidence provenance number")
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_normalize_receipt_value(item) for item in value]
    if isinstance(value, dict):
        normalized = {}
        for key in sorted(value, key=lambda item: str(item)):
            if not isinstance(key, str) or not key:
                raise LLMRoleContractError(
                    "evidence provenance requires non-empty string keys"
                )
            normalized[key] = _normalize_receipt_value(value[key])
        return normalized
    raise LLMRoleContractError(
        f"unsupported evidence provenance value: {type(value).__name__}"
    )


def _canonical_strict_authority_json(strict_authority):
    if strict_authority is None:
        return None
    if type(strict_authority) is not dict:
        raise LLMRoleContractError("strict-authority descriptor must be a plain object")
    try:
        normalized = _normalize_receipt_value(strict_authority)
    except LLMRoleContractError as exc:
        raise LLMRoleContractError(
            f"strict-authority descriptor is not canonically serializable: {exc}"
        ) from exc
    if not isinstance(normalized, dict):
        raise LLMRoleContractError("strict-authority descriptor must be an object")
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _assert_strict_authority_unchanged(strict_authority, frozen_capability):
    expected = frozen_capability.strict_authority_json
    current = _canonical_strict_authority_json(strict_authority)
    if current != expected:
        raise LLMRoleContractError(
            f"{frozen_capability.role_id}: strict-authority descriptor changed "
            "after capability validation"
        )


def _project_strict_authority_state(owner, internal):
    """Publish internal effect results without re-admitting caller authority."""

    if owner is None or internal is None:
        return
    if type(owner) is not dict or type(internal) is not dict:
        raise LLMRoleContractError("strict-authority projection requires plain objects")
    projected = deepcopy(internal)
    owner.clear()
    owner.update(projected)


def _project_relative_path(path, *, require_file=False):
    raw = Path(str(path))
    absolute = raw if raw.is_absolute() else _LLM_PROJECT_ROOT / raw
    cursor = _LLM_PROJECT_ROOT
    try:
        relative_parts = absolute.absolute().relative_to(
            _LLM_PROJECT_ROOT.absolute()
        ).parts
    except ValueError as exc:
        raise LLMRoleContractError(f"path outside active project: {path}") from exc
    for part in relative_parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise LLMRoleContractError(f"symlinked LLM authority path: {path}")
    resolved = absolute.resolve(strict=False)
    try:
        relative = resolved.relative_to(_LLM_PROJECT_ROOT.resolve())
    except ValueError as exc:
        raise LLMRoleContractError(f"resolved path outside active project: {path}") from exc
    relative_text = relative.as_posix()
    if not relative_text or relative_text == "." or relative_text.startswith("archive/"):
        raise LLMRoleContractError(f"invalid active LLM authority path: {path}")
    if require_file and not resolved.is_file():
        raise LLMRoleContractError(f"required renderer source is not a file: {path}")
    return relative_text, resolved


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _producer_binding(contract, producer):
    if not callable(producer):
        raise LLMRoleContractError(f"{contract.role_id}: renderer producer is not callable")
    source_file = inspect.getsourcefile(producer)
    if not source_file:
        raise LLMRoleContractError(f"{contract.role_id}: renderer producer has no source file")
    relative, resolved = _project_relative_path(source_file, require_file=True)
    if relative != contract.producer_file:
        raise LLMRoleContractError(
            f"{contract.role_id}: producer file {relative!r} is not "
            f"{contract.producer_file!r}"
        )
    producer_name = str(getattr(producer, "__name__", ""))
    if producer_name != contract.producer_name:
        raise LLMRoleContractError(
            f"{contract.role_id}: producer {producer_name!r} is not "
            f"{contract.producer_name!r}"
        )
    try:
        function_source = inspect.getsource(producer).encode("utf-8")
    except (OSError, TypeError) as exc:
        raise LLMRoleContractError(
            f"{contract.role_id}: renderer producer source unavailable"
        ) from exc
    templates = []
    for template_path in contract.template_paths:
        relative_template, resolved_template = _project_relative_path(
            template_path,
            require_file=True,
        )
        if relative_template != template_path:
            raise LLMRoleContractError(
                f"{contract.role_id}: non-canonical template path {template_path}"
            )
        templates.append((template_path, _sha256_file(resolved_template)))
    return {
        "producer_file": relative,
        "producer_name": producer_name,
        "producer_file_sha256": _sha256_file(resolved),
        "producer_function_sha256": hashlib.sha256(function_source).hexdigest(),
        "template_digests": tuple(templates),
    }


def _type_identity(value):
    if isinstance(value, type):
        return f"{value.__module__}.{value.__qualname__}"
    return str(value)


def _active_mcp_config_payload(mcp_servers):
    selected = dict(mcp_servers or {})
    if not selected:
        return {}
    if set(selected) != {"evolution"}:
        raise LLMRoleContractError(
            f"unregistered MCP server objects: {sorted(selected)}"
        )
    from tools import evolution_server, mcp_tools

    if selected["evolution"] is not evolution_server:
        raise LLMRoleContractError(
            "Orchestrator evolution MCP must be the system-owned server object"
        )
    if not isinstance(evolution_server, dict) or set(evolution_server) != {
        "type", "name", "instance",
    }:
        raise LLMRoleContractError("system evolution MCP config shape drift")
    instance = evolution_server.get("instance")
    tools_payload = []
    for tool in mcp_tools:
        handler = getattr(tool, "handler", None)
        handler_file = inspect.getsourcefile(handler) if callable(handler) else None
        if not handler_file:
            raise LLMRoleContractError("evolution MCP tool handler source unavailable")
        handler_relative, handler_resolved = _project_relative_path(
            handler_file,
            require_file=True,
        )
        schema = getattr(tool, "input_schema", {}) or {}
        tools_payload.append({
            "name": str(getattr(tool, "name", "")),
            "description_sha256": hashlib.sha256(
                str(getattr(tool, "description", "")).encode("utf-8")
            ).hexdigest(),
            "input_schema": {
                str(key): _type_identity(value)
                for key, value in sorted(schema.items())
            },
            "handler_file": handler_relative,
            "handler_name": str(getattr(handler, "__name__", "")),
            "handler_file_sha256": _sha256_file(handler_resolved),
        })
    return {
        "type": evolution_server.get("type"),
        "name": evolution_server.get("name"),
        "instance_name": str(getattr(instance, "name", "")),
        "instance_version": str(getattr(instance, "version", "")),
        "tools": tools_payload,
    }


def _issue_llm_dispatch_receipt(
    role_name,
    rendered_prompt,
    *,
    producer,
    evidence_kind,
    evidence_provenance,
    mcp_servers=(),
    model="sonnet",
):
    """Issue one sealed receipt from the real renderer and evidence producer.

    Callers provide the callable and the structured source payload, never a
    renderer-name string.  The issuer selects the expected source/template from
    the active role registry and content-binds their current bytes.
    """

    contract = resolve_llm_role_contract(role_name)
    if str(evidence_kind) != contract.evidence_provenance_kind:
        raise LLMRoleContractError(
            f"{contract.role_id}: evidence kind {evidence_kind!r} is not "
            f"{contract.evidence_provenance_kind!r}"
        )
    prompt_text = str(rendered_prompt or "")
    producer_binding = _producer_binding(contract, producer)
    renderer_payload = {
        "role_id": contract.role_id,
        "runtime_role": str(role_name),
        **producer_binding,
        "rendered_prompt_sha256": hashlib.sha256(
            prompt_text.encode("utf-8")
        ).hexdigest(),
        "rendered_prompt_chars": len(prompt_text),
    }
    renderer_receipt = LLMRendererReceipt(
        **renderer_payload,
        receipt_digest=_receipt_digest(renderer_payload),
        producer=producer,
        _authority=_LLM_RECEIPT_AUTHORITY,
    )

    normalized_provenance = _normalize_receipt_value(evidence_provenance)
    if not isinstance(normalized_provenance, dict):
        raise LLMRoleContractError(
            f"{contract.role_id}: evidence provenance must be an object"
        )
    missing = [
        field for field in contract.required_evidence_fields
        if field not in normalized_provenance
    ]
    if missing:
        raise LLMRoleContractError(
            f"{contract.role_id}: evidence provenance fields missing: {missing}"
        )
    provenance_json = json.dumps(
        normalized_provenance,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    evidence_payload = {
        "role_id": contract.role_id,
        "provenance_kind": contract.evidence_provenance_kind,
        "provenance_sha256": hashlib.sha256(
            provenance_json.encode("utf-8")
        ).hexdigest(),
        "renderer_receipt_digest": renderer_receipt.receipt_digest,
    }
    evidence_receipt = LLMEvidenceReceipt(
        **evidence_payload,
        provenance_json=provenance_json,
        receipt_digest=_receipt_digest(evidence_payload),
        _authority=_LLM_RECEIPT_AUTHORITY,
    )

    mcp_payload_value = _active_mcp_config_payload(mcp_servers)
    mcp_json = json.dumps(
        mcp_payload_value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    mcp_payload = {
        "role_id": contract.role_id,
        "config_sha256": hashlib.sha256(mcp_json.encode("utf-8")).hexdigest(),
    }
    mcp_receipt = LLMMCPReceipt(
        **mcp_payload,
        config_json=mcp_json,
        receipt_digest=_receipt_digest(mcp_payload),
        _authority=_LLM_RECEIPT_AUTHORITY,
    )
    dispatch_payload = {
        "schema": _LLM_RECEIPT_SCHEMA,
        "role_id": contract.role_id,
        "runtime_role": str(role_name),
        "model": str(model),
        "renderer_receipt_digest": renderer_receipt.receipt_digest,
        "evidence_receipt_digest": evidence_receipt.receipt_digest,
        "mcp_receipt_digest": mcp_receipt.receipt_digest,
    }
    return LLMDispatchReceipt(
        schema=_LLM_RECEIPT_SCHEMA,
        role_id=contract.role_id,
        runtime_role=str(role_name),
        model=str(model),
        renderer=renderer_receipt,
        evidence=evidence_receipt,
        mcp=mcp_receipt,
        receipt_digest=_receipt_digest(dispatch_payload),
        _authority=_LLM_RECEIPT_AUTHORITY,
    )


def render_llm_prompt(
    role_name,
    *,
    producer,
    renderer_inputs,
    mcp_servers=(),
    model="sonnet",
):
    """Replayably render and seal one active provider prompt.

    The provider boundary never accepts a text string plus a claimed renderer.
    This wrapper canonicalizes the renderer inputs, calls the registered
    production renderer itself, then signs the exact output. Validation invokes
    the same callable again from the stored inputs, so replacing or independently
    constructing ``text`` fails even when the correct producer is named.
    """

    contract = resolve_llm_role_contract(role_name)
    if str(model) not in contract.allowed_models:
        raise LLMRoleContractError(
            f"{contract.role_id}: model {model!r} outside "
            f"{list(contract.allowed_models)!r}"
        )
    normalized_inputs = _normalize_receipt_value(renderer_inputs)
    if not isinstance(normalized_inputs, dict):
        raise LLMRoleContractError(
            f"{contract.role_id}: renderer inputs must be an object"
        )
    renderer_inputs_json = json.dumps(
        normalized_inputs,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    replay_inputs = json.loads(renderer_inputs_json)
    try:
        material = producer(replay_inputs)
    except Exception as exc:
        raise LLMRoleContractError(
            f"{contract.role_id}: production renderer failed: "
            f"{type(exc).__name__}"
        ) from exc
    if (
        not isinstance(material, LLMRenderedMaterial)
        or not isinstance(material.text, str)
        or not material.text
        or not isinstance(material.evidence_provenance, dict)
    ):
        raise LLMRoleContractError(
            f"{contract.role_id}: production renderer returned no typed material"
        )
    dispatch_receipt = _issue_llm_dispatch_receipt(
        role_name,
        material.text,
        producer=producer,
        evidence_kind=material.evidence_kind,
        evidence_provenance=material.evidence_provenance,
        mcp_servers=mcp_servers,
        model=model,
    )
    return RenderedLLMPrompt(
        role_id=contract.role_id,
        runtime_role=str(role_name),
        text=material.text,
        renderer_inputs_json=renderer_inputs_json,
        dispatch_receipt=dispatch_receipt,
        producer=producer,
        _authority=_LLM_RECEIPT_AUTHORITY,
    )


def _validate_rendered_llm_prompt(
    rendered, contract, role_name, mcp_servers, model="sonnet"
):
    if not isinstance(rendered, RenderedLLMPrompt):
        raise LLMRoleContractError(
            f"{contract.role_id}: sealed RenderedLLMPrompt required"
        )
    if (
        rendered._authority is not _LLM_RECEIPT_AUTHORITY
        or rendered.role_id != contract.role_id
        or rendered.runtime_role != str(role_name)
    ):
        raise LLMRoleContractError(
            f"{contract.role_id}: rendered prompt authority/subject mismatch"
        )
    if rendered.producer is not rendered.dispatch_receipt.renderer.producer:
        raise LLMRoleContractError(
            f"{contract.role_id}: rendered prompt producer receipt mismatch"
        )
    try:
        replay_inputs = json.loads(rendered.renderer_inputs_json)
        replayed = rendered.producer(replay_inputs)
    except Exception as exc:
        raise LLMRoleContractError(
            f"{contract.role_id}: renderer replay failed: {type(exc).__name__}"
        ) from exc
    if not isinstance(replayed, LLMRenderedMaterial) or replayed.text != rendered.text:
        raise LLMRoleContractError(
            f"{contract.role_id}: rendered prompt replay mismatch"
        )
    replayed_provenance_json = json.dumps(
        _normalize_receipt_value(replayed.evidence_provenance),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if (
        replayed.evidence_kind
        != rendered.dispatch_receipt.evidence.provenance_kind
        or replayed_provenance_json
        != rendered.dispatch_receipt.evidence.provenance_json
    ):
        raise LLMRoleContractError(
            f"{contract.role_id}: rendered evidence provenance replay mismatch"
        )
    _validate_llm_dispatch_receipt(
        rendered.dispatch_receipt,
        contract,
        role_name,
        rendered.text,
        mcp_servers,
        model,
    )
    return rendered


def _validate_llm_dispatch_receipt(
    receipt,
    contract,
    role_name,
    rendered_prompt,
    mcp_servers,
    model,
):
    if not isinstance(receipt, LLMDispatchReceipt):
        raise LLMRoleContractError(f"{contract.role_id}: typed dispatch receipt required")
    if receipt._authority is not _LLM_RECEIPT_AUTHORITY:
        raise LLMRoleContractError(f"{contract.role_id}: dispatch receipt authority invalid")
    if (
        receipt.schema != _LLM_RECEIPT_SCHEMA
        or receipt.role_id != contract.role_id
        or receipt.runtime_role != str(role_name)
        or receipt.model != str(model)
        or receipt.model not in contract.allowed_models
    ):
        raise LLMRoleContractError(f"{contract.role_id}: dispatch receipt subject mismatch")
    renderer = receipt.renderer
    if renderer._authority is not _LLM_RECEIPT_AUTHORITY:
        raise LLMRoleContractError(f"{contract.role_id}: renderer receipt authority invalid")
    binding = _producer_binding(contract, renderer.producer)
    prompt_text = str(rendered_prompt or "")
    renderer_payload = {
        "role_id": contract.role_id,
        "runtime_role": str(role_name),
        **binding,
        "rendered_prompt_sha256": hashlib.sha256(
            prompt_text.encode("utf-8")
        ).hexdigest(),
        "rendered_prompt_chars": len(prompt_text),
    }
    if any(
        getattr(renderer, key) != value for key, value in renderer_payload.items()
    ) or renderer.receipt_digest != _receipt_digest(renderer_payload):
        raise LLMRoleContractError(f"{contract.role_id}: renderer receipt drift")
    evidence = receipt.evidence
    if evidence._authority is not _LLM_RECEIPT_AUTHORITY:
        raise LLMRoleContractError(f"{contract.role_id}: evidence receipt authority invalid")
    try:
        provenance = json.loads(evidence.provenance_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise LLMRoleContractError(
            f"{contract.role_id}: evidence provenance receipt invalid"
        ) from exc
    missing = [
        field for field in contract.required_evidence_fields
        if field not in provenance
    ]
    evidence_payload = {
        "role_id": contract.role_id,
        "provenance_kind": contract.evidence_provenance_kind,
        "provenance_sha256": hashlib.sha256(
            evidence.provenance_json.encode("utf-8")
        ).hexdigest(),
        "renderer_receipt_digest": renderer.receipt_digest,
    }
    if (
        missing
        or evidence.provenance_kind != contract.evidence_provenance_kind
        or any(getattr(evidence, key) != value for key, value in evidence_payload.items())
        or evidence.receipt_digest != _receipt_digest(evidence_payload)
    ):
        raise LLMRoleContractError(f"{contract.role_id}: evidence receipt drift")
    mcp = receipt.mcp
    current_mcp_json = json.dumps(
        _active_mcp_config_payload(mcp_servers),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    mcp_payload = {
        "role_id": contract.role_id,
        "config_sha256": hashlib.sha256(
            current_mcp_json.encode("utf-8")
        ).hexdigest(),
    }
    if (
        mcp._authority is not _LLM_RECEIPT_AUTHORITY
        or mcp.config_json != current_mcp_json
        or any(getattr(mcp, key) != value for key, value in mcp_payload.items())
        or mcp.receipt_digest != _receipt_digest(mcp_payload)
    ):
        raise LLMRoleContractError(f"{contract.role_id}: MCP config receipt drift")
    dispatch_payload = {
        "schema": _LLM_RECEIPT_SCHEMA,
        "role_id": contract.role_id,
        "runtime_role": str(role_name),
        "model": str(model),
        "renderer_receipt_digest": renderer.receipt_digest,
        "evidence_receipt_digest": evidence.receipt_digest,
        "mcp_receipt_digest": mcp.receipt_digest,
    }
    if receipt.receipt_digest != _receipt_digest(dispatch_payload):
        raise LLMRoleContractError(f"{contract.role_id}: dispatch receipt digest drift")
    return receipt


def _llm_selected_tools(tools):
    if tools is None:
        return ()
    if not isinstance(tools, (list, tuple, set, frozenset)):
        raise LLMRoleContractError(
            "active LLM roles require an explicit built-in tool-name sequence"
        )
    names = tuple(str(item) for item in tools)
    if len(names) != len(set(names)):
        raise LLMRoleContractError("duplicate built-in tool grant")
    if isinstance(tools, (set, frozenset)):
        names = tuple(sorted(names))
    return names


def _llm_selected_tool_set(tools):
    return frozenset(_llm_selected_tools(tools))


def _llm_selected_mcp_servers(mcp_servers):
    if isinstance(mcp_servers, dict):
        return frozenset(str(name) for name in mcp_servers)
    return frozenset(str(name) for name in (mcp_servers or ()))


_BOT_DIR_SCOPE_RE = re.compile(rf"^bots/{re.escape(ACTIVE_BOT_PREFIX)}(?P<version>\d+)$")
_EVIDENCE_SCOPE_RE = re.compile(
    r"^web/core/results/v(?P<version>\d+)/evidence_snapshot$"
)
_BOOTSTRAP_EVIDENCE_SCOPE_RE = re.compile(
    rf"^bots/{re.escape(ACTIVE_BOT_PREFIX)}(?P<version>\d+)/\.protocol_bootstrap_no_strength_evidence$"
)
_WORKER_WORKSPACE_SCOPE_RE = re.compile(
    r"^web/core/results/workflow/artifacts/workspaces/(?P<digest>[0-9a-f]{64})$"
)
_WORKFLOW_ARTIFACT_SCOPE_RE = re.compile(
    r"^web/core/results/workflow/artifacts/(?P<digest>[0-9a-f]{64})$"
)
_CROSSOVER_WORKSPACE_SCOPE_RE = re.compile(
    r"^web/core/results/crossover_workspaces/"
    r"v(?P<version>\d+)-attempt-(?P<attempt>\d+)-[A-Za-z0-9_-]+$"
)


def _raw_scope_entries(raw, *, default_kind):
    dirs = []
    files = []
    if raw is None:
        return dirs, files
    if isinstance(raw, dict):
        allowed_keys = {"dirs", "directories", "files", "paths"}
        unknown = set(raw) - allowed_keys
        if unknown:
            raise LLMRoleContractError(f"unknown LLM scope keys: {sorted(unknown)}")
        dirs.extend(raw.get("dirs") or raw.get("directories") or ())
        files.extend(raw.get("files") or raw.get("paths") or ())
    elif isinstance(raw, (list, tuple, set, frozenset)):
        (dirs if default_kind == "dirs" else files).extend(raw)
    else:
        (dirs if default_kind == "dirs" else files).append(raw)
    return dirs, files


def _canonical_scope_paths(values):
    result = []
    for value in values:
        relative, _resolved = _project_relative_path(value)
        if relative in result:
            raise LLMRoleContractError(f"duplicate LLM authority path: {relative}")
        result.append(relative)
    return result


def _scope_evidence_provenance(dispatch_receipt):
    if not isinstance(dispatch_receipt, LLMDispatchReceipt):
        return {}
    try:
        value = json.loads(dispatch_receipt.evidence.provenance_json)
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _validate_role_scope(
    contract,
    role_name,
    *,
    allowed_read_dirs,
    allowed_write_dir,
    allowed_evidence_snapshot_dir,
    context_files,
    exact_bash_commands,
    dispatch_receipt,
    selected_tools,
    strict_authority_json,
    model,
):
    read_dirs_raw, read_files_raw = _raw_scope_entries(
        allowed_read_dirs,
        default_kind="dirs",
    )
    write_dirs_raw, write_files_raw = _raw_scope_entries(
        allowed_write_dir,
        default_kind="dirs",
    )
    read_dirs = _canonical_scope_paths(read_dirs_raw)
    read_files = _canonical_scope_paths(read_files_raw)
    write_dirs = _canonical_scope_paths(write_dirs_raw)
    write_files = _canonical_scope_paths(write_files_raw)
    context = _canonical_scope_paths(context_files or ())
    evidence = None
    if allowed_evidence_snapshot_dir is not None:
        evidence, _resolved = _project_relative_path(
            allowed_evidence_snapshot_dir
        )
    selected_bash = tuple(str(item).strip() for item in (exact_bash_commands or ()))
    provenance = _scope_evidence_provenance(dispatch_receipt)

    if context:
        # Active production renderers inject their compiled context directly
        # before signing the prompt. No provider role accepts caller-selected
        # context files.
        raise LLMRoleContractError(
            f"{contract.role_id}: external context files are forbidden"
        )

    policy = contract.scope_policy
    if policy in {"none", "orchestrator_mcp_only"}:
        if any((read_dirs, read_files, write_dirs, write_files, evidence)):
            raise LLMRoleContractError(
                f"{contract.role_id}: filesystem authority is forbidden"
            )
    elif policy == "canonical_candidates":
        if (
            read_files or write_dirs or write_files
            or not 1 <= len(read_dirs) <= 2
            or any(_BOT_DIR_SCOPE_RE.fullmatch(path) is None for path in read_dirs)
        ):
            raise LLMRoleContractError(
                f"{contract.role_id}: only one/two canonical "
                f"{ACTIVE_BOT_PREFIX}<N> read dirs are allowed"
            )
        if evidence is not None and not (
            _EVIDENCE_SCOPE_RE.fullmatch(evidence)
            or _BOOTSTRAP_EVIDENCE_SCOPE_RE.fullmatch(evidence)
        ):
            raise LLMRoleContractError(
                f"{contract.role_id}: evidence path is not a generation snapshot"
            )
        subject_versions = {
            int(value)
            for key, value in provenance.items()
            if key in {"source_v", "next_v"}
            and isinstance(value, int)
        }
        read_versions = {
            int(_BOT_DIR_SCOPE_RE.fullmatch(path).group("version"))
            for path in read_dirs
        }
        if subject_versions and not read_versions.issubset(subject_versions):
            raise LLMRoleContractError(
                f"{contract.role_id}: candidate read scope is outside receipt subject versions"
            )
        if evidence is not None:
            match = _EVIDENCE_SCOPE_RE.fullmatch(evidence) or (
                _BOOTSTRAP_EVIDENCE_SCOPE_RE.fullmatch(evidence)
            )
            if subject_versions and int(match.group("version")) not in subject_versions:
                raise LLMRoleContractError(
                    f"{contract.role_id}: evidence snapshot version is outside receipt subject"
                )
    elif policy in {"worker_candidate", "debug_candidate"}:
        if (
            read_files or write_dirs or len(read_dirs) != 1
            or _WORKER_WORKSPACE_SCOPE_RE.fullmatch(read_dirs[0]) is None
            or evidence is not None
        ):
            raise LLMRoleContractError(
                f"{contract.role_id}: exact lease-isolated Worker workspace required"
            )
        candidate = read_dirs[0]
        candidate_from_receipt = provenance.get("candidate_path")
        if candidate_from_receipt is not None:
            candidate_relative, _ = _project_relative_path(candidate_from_receipt)
            if candidate_relative != candidate:
                raise LLMRoleContractError(
                    f"{contract.role_id}: candidate scope differs from evidence receipt"
                )
        if policy == "debug_candidate":
            if write_files:
                raise LLMRoleContractError("debug agent cannot receive write scope")
        else:
            expected_write = f"{candidate}/policy.py"
            if write_files != [expected_write]:
                raise LLMRoleContractError(
                    "Worker write scope must be the compiled candidate policy.py"
                )
            if provenance.get("allowed_files") != ["policy.py"]:
                raise LLMRoleContractError(
                    "Worker evidence receipt must bind allowed_files=['policy.py']"
                )
            task = provenance.get("task")
            next_v = provenance.get("next_v")
            if not isinstance(task, dict) or not isinstance(next_v, int):
                raise LLMRoleContractError("Worker compiled task provenance invalid")
            from worker_boundary import allowed_files_for_task

            if allowed_files_for_task(task, next_v) != ["policy.py"]:
                raise LLMRoleContractError(
                    "Worker compiled task does not authorize policy.py"
                )
    elif policy == "crossover_workspace":
        artifact_dirs = [
            path for path in read_dirs
            if _WORKFLOW_ARTIFACT_SCOPE_RE.fullmatch(path)
        ]
        workspace_dirs = [
            path for path in read_dirs
            if _CROSSOVER_WORKSPACE_SCOPE_RE.fullmatch(path)
        ]
        if (
            read_files or write_dirs or len(read_dirs) != 3
            or len(artifact_dirs) != 2 or len(workspace_dirs) != 1
        ):
            raise LLMRoleContractError(
                "Crossover requires two immutable parent artifacts and one target workspace"
            )
        workspace = workspace_dirs[0]
        if write_files != [f"{workspace}/policy.py"]:
            raise LLMRoleContractError(
                "Crossover write scope must be the exact target policy.py"
            )
        role_match = re.search(r"→v(?P<version>\d+)", str(role_name))
        workspace_match = _CROSSOVER_WORKSPACE_SCOPE_RE.fullmatch(workspace)
        if (
            role_match is None
            or int(role_match.group("version"))
            != int(workspace_match.group("version"))
            or provenance.get("target_v") != int(workspace_match.group("version"))
        ):
            raise LLMRoleContractError("Crossover target version scope mismatch")
        parent_artifacts = provenance.get("parent_artifacts")
        if sorted(parent_artifacts or ()) != sorted(
            path.rsplit("/", 1)[-1] for path in artifact_dirs
        ):
            raise LLMRoleContractError(
                "Crossover parent artifact scope differs from evidence receipt"
            )
        if evidence is not None:
            evidence_match = _EVIDENCE_SCOPE_RE.fullmatch(evidence)
            if (
                evidence_match is None
                or int(evidence_match.group("version"))
                != provenance.get("target_v")
            ):
                raise LLMRoleContractError("Crossover evidence snapshot version mismatch")
    elif policy == "operator_exact_files":
        if (
            read_dirs or write_dirs or write_files or evidence is not None
            or set(read_files) != set(contract.fixed_read_files)
            or len(read_files) != len(contract.fixed_read_files)
            or selected_bash != contract.fixed_bash_commands
        ):
            raise LLMRoleContractError(
                "Operator probe scope/config differs from the fixed oracle contract"
            )
        repo_root = provenance.get("repo_root")
        if repo_root is None or Path(str(repo_root)).resolve() != _LLM_PROJECT_ROOT.resolve():
            raise LLMRoleContractError("Operator probe repo root receipt mismatch")
    else:
        raise LLMRoleContractError(
            f"{contract.role_id}: unknown scope policy {policy!r}"
        )

    if policy != "operator_exact_files" and selected_bash:
        raise LLMRoleContractError(
            f"{contract.role_id}: exact Bash allowlist is not registered"
        )
    def absolute_paths(paths):
        return tuple(
            str((_LLM_PROJECT_ROOT / path).resolve(strict=False)) for path in paths
        )

    return FrozenLLMCapability(
        role_id=contract.role_id,
        model=str(model),
        selected_tools=tuple(selected_tools),
        read_dirs=absolute_paths(read_dirs),
        read_files=absolute_paths(read_files),
        write_dirs=absolute_paths(write_dirs),
        write_files=absolute_paths(write_files),
        evidence_dir=(
            str((_LLM_PROJECT_ROOT / evidence).resolve(strict=False))
            if evidence is not None else None
        ),
        context_files=absolute_paths(context),
        exact_bash_commands=selected_bash,
        strict_authority_json=strict_authority_json,
        strict_authority_sha256=(
            hashlib.sha256(strict_authority_json.encode("utf-8")).hexdigest()
            if strict_authority_json is not None else None
        ),
        _authority=_LLM_RECEIPT_AUTHORITY,
    )


def validate_llm_role_dispatch(
    role_name,
    *,
    tools,
    rendered_prompt,
    provider_path="subagent_sdk",
    mcp_servers=(),
    context_files=(),
    allowed_read_dirs=None,
    allowed_write_dir=None,
    allowed_evidence_snapshot_dir=None,
    strict_authority=None,
    exact_bash_commands=None,
    model="sonnet",
):
    """Fail closed if a real provider dispatch exceeds its registered scope."""

    contract = resolve_llm_role_contract(role_name)
    if str(model) not in contract.allowed_models:
        raise LLMRoleContractError(
            f"{contract.role_id}: model {model!r} outside "
            f"{list(contract.allowed_models)!r}"
        )
    selected_tool_names = _llm_selected_tools(tools)
    selected_tools = frozenset(selected_tool_names)
    selected_mcp = _llm_selected_mcp_servers(mcp_servers)
    rendered_prompt = _validate_rendered_llm_prompt(
        rendered_prompt,
        contract,
        role_name,
        mcp_servers,
        model,
    )
    dispatch_receipt = rendered_prompt.dispatch_receipt
    if str(provider_path) != contract.provider_path:
        raise LLMRoleContractError(
            f"{contract.role_id}: provider path {provider_path!r} is not "
            f"{contract.provider_path!r}"
        )
    if selected_tools not in contract.allowed_tool_sets:
        allowed = [sorted(group) for group in contract.allowed_tool_sets]
        raise LLMRoleContractError(
            f"{contract.role_id}: tools {sorted(selected_tools)!r} outside {allowed!r}"
        )
    if selected_mcp != contract.allowed_mcp_servers:
        raise LLMRoleContractError(
            f"{contract.role_id}: MCP servers {sorted(selected_mcp)!r} outside "
            f"{sorted(contract.allowed_mcp_servers)!r}"
        )
    if contract.requires_read_scope and not any((
        allowed_read_dirs,
        allowed_write_dir,
        context_files,
    )):
        raise LLMRoleContractError(
            f"{contract.role_id}: explicit filesystem read scope is required"
        )
    if contract.requires_write_scope and allowed_write_dir is None:
        raise LLMRoleContractError(
            f"{contract.role_id}: exact write scope is required"
        )
    if not contract.requires_write_scope and allowed_write_dir is not None:
        raise LLMRoleContractError(
            f"{contract.role_id}: filesystem write scope is forbidden"
        )
    if context_files and not contract.allows_context_files:
        raise LLMRoleContractError(
            f"{contract.role_id}: context-file prompt injection is not registered"
        )
    if (
        allowed_evidence_snapshot_dir is not None
        and not contract.allows_evidence_snapshot
    ):
        raise LLMRoleContractError(
            f"{contract.role_id}: filesystem evidence snapshot is not registered"
        )
    if strict_authority is not None and not contract.allows_strict_authority:
        raise LLMRoleContractError(
            f"{contract.role_id}: strict-authority call binding is not registered"
        )
    if (
        exact_bash_commands is not None
        and not contract.allows_exact_bash_commands
    ):
        raise LLMRoleContractError(
            f"{contract.role_id}: exact Bash command grants are not registered"
        )
    if contract.allows_exact_bash_commands and exact_bash_commands is None:
        raise LLMRoleContractError(
            f"{contract.role_id}: exact Bash command allowlist is required"
        )
    strict_authority_json = _canonical_strict_authority_json(strict_authority)
    frozen_capability = _validate_role_scope(
        contract,
        role_name,
        allowed_read_dirs=allowed_read_dirs,
        allowed_write_dir=allowed_write_dir,
        allowed_evidence_snapshot_dir=allowed_evidence_snapshot_dir,
        context_files=context_files,
        exact_bash_commands=exact_bash_commands,
        dispatch_receipt=dispatch_receipt,
        selected_tools=selected_tool_names,
        strict_authority_json=strict_authority_json,
        model=str(model),
    )
    return contract, dispatch_receipt, frozen_capability


def render_llm_role_contract_suffix(
    contract,
    role_name,
    *,
    tools,
    mcp_servers=(),
    rendered_provider_prefix="",
    dispatch_receipt=None,
    frozen_capability=None,
):
    """Render the final, system-owned provider instruction for one dispatch."""

    provider_prefix = str(rendered_provider_prefix or "")
    capability_payload = {
        "model": frozen_capability.model,
        "selected_tools": list(frozen_capability.selected_tools),
        "read_dirs": list(frozen_capability.read_dirs),
        "read_files": list(frozen_capability.read_files),
        "write_dirs": list(frozen_capability.write_dirs),
        "write_files": list(frozen_capability.write_files),
        "evidence_dir": frozen_capability.evidence_dir,
        "context_files": list(frozen_capability.context_files),
        "exact_bash_commands": list(frozen_capability.exact_bash_commands),
        "strict_authority_sha256": frozen_capability.strict_authority_sha256,
    }
    payload = {
        "schema": "national_tcp_llm_role_contract_v2",
        "role_id": contract.role_id,
        "runtime_role": str(role_name),
        "provider_path": contract.provider_path,
        "model": frozen_capability.model,
        "renderer": contract.renderer,
        "renderer_receipt_digest": dispatch_receipt.renderer.receipt_digest,
        "renderer_producer_file": dispatch_receipt.renderer.producer_file,
        "renderer_producer_file_sha256": (
            dispatch_receipt.renderer.producer_file_sha256
        ),
        "renderer_producer_function_sha256": (
            dispatch_receipt.renderer.producer_function_sha256
        ),
        "renderer_template_digests": dict(
            dispatch_receipt.renderer.template_digests
        ),
        "evidence_provenance_kind": (
            dispatch_receipt.evidence.provenance_kind
        ),
        "evidence_provenance_sha256": (
            dispatch_receipt.evidence.provenance_sha256
        ),
        "evidence_receipt_digest": dispatch_receipt.evidence.receipt_digest,
        "mcp_config_sha256": dispatch_receipt.mcp.config_sha256,
        "mcp_receipt_digest": dispatch_receipt.mcp.receipt_digest,
        "dispatch_receipt_digest": dispatch_receipt.receipt_digest,
        "frozen_capability_sha256": _receipt_digest(capability_payload),
        "frozen_capability": capability_payload,
        "selected_builtin_tools": list(frozen_capability.selected_tools),
        "selected_mcp_servers": sorted(_llm_selected_mcp_servers(mcp_servers)),
        "provider_read_scope": contract.provider_read_scope,
        "provider_write_scope": contract.provider_write_scope,
        "evidence_policy": contract.evidence_policy,
        "history_policy": contract.history_policy,
        "rendered_provider_prefix_chars": len(provider_prefix),
        "rendered_provider_prefix_sha256": hashlib.sha256(
            provider_prefix.encode("utf-8")
        ).hexdigest(),
        "strength_authority": "zero",
        "certification_authority": "zero",
        "rating_authority": "zero",
        "historical_memory_authority": "zero",
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    payload["contract_digest"] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    proposal_emission_gate = ""
    if contract.role_id == "master_proposal":
        repair_attempt = str(role_name).endswith(
            (" SCHEMA RETRY", " DISTINCTNESS RETRY")
        )
        proposal_emission_gate = (
            "\n\n# SYSTEM-OWNED MASTER PROPOSAL EMISSION GATE (LAST)\n"
            + (
                "This is the sole repair attempt (attempt 2 of 2). "
                if repair_attempt
                else "This is the initial attempt (attempt 1 of at most 2). "
            )
            + "The only admissible Scout completion is one raw JSON object "
            "matching the rendered FINAL SCOUT OUTPUT CONTRACT. Do not return "
            "Markdown fences, analysis, an acknowledgement, a summary, or "
            "trailing commentary. Apply the closed schema and any system-owned "
            "repair instruction in the rendered prefix, then emit the complete "
            "object now. "
            + (
                "A malformed or duplicate repair object is rejected; there is "
                "no third attempt."
                if repair_attempt
                else "If this object is rejected, only the system may authorize "
                "the single bounded repair attempt; do not self-retry or emit a "
                "second object."
            )
        )
    return (
        "\n\n# SYSTEM-OWNED ACTIVE LLM ROLE CONTRACT (FINAL)\n"
        + json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
        + "\n\nThe rendered template and every attached context block are "
        "subordinate to this final contract. Do not read, infer from, search, "
        "or request archive/legacy content, mutable live result files, unbound "
        "replays, free-standing lessons/experience, generic Git history, or any "
        "path/tool not listed above. Historical input is allowed only when the "
        "history_policy explicitly names its system-bound form. This response "
        "may perform only its registered advisory or scoped implementation "
        "function; it is never strength evidence, a rating/certification result, "
        "persistent memory, or authority to override deterministic gates.\n"
        + proposal_emission_gate
        + "\n"
    )


def bind_llm_role_provider_prompt(
    rendered_prompt,
    role_name,
    *,
    tools,
    provider_path="subagent_sdk",
    mcp_servers=(),
    context_files=(),
    allowed_read_dirs=None,
    allowed_write_dir=None,
    allowed_evidence_snapshot_dir=None,
    strict_authority=None,
    exact_bash_commands=None,
    max_chars=None,
    provider_prefix=None,
    frozen_capability=None,
    model="sonnet",
):
    """Validate a dispatch and place its role contract last in provider input."""

    if frozen_capability is not None:
        contract = resolve_llm_role_contract(role_name)
        if (
            not isinstance(frozen_capability, FrozenLLMCapability)
            or frozen_capability._authority is not _LLM_RECEIPT_AUTHORITY
            or frozen_capability.role_id != contract.role_id
        ):
            raise LLMRoleContractError(
                f"{contract.role_id}: frozen capability authority invalid"
            )
        allowed_read_dirs = {
            "dirs": frozen_capability.read_dirs,
            "files": frozen_capability.read_files,
        }
        allowed_write_dir = {
            "dirs": frozen_capability.write_dirs,
            "files": frozen_capability.write_files,
        } if (frozen_capability.write_dirs or frozen_capability.write_files) else None
        allowed_evidence_snapshot_dir = frozen_capability.evidence_dir
        context_files = frozen_capability.context_files
        exact_bash_commands = frozen_capability.exact_bash_commands or None
        tools = frozen_capability.selected_tools
        model = frozen_capability.model
    contract, dispatch_receipt, validated_capability = validate_llm_role_dispatch(
        role_name,
        tools=tools,
        rendered_prompt=rendered_prompt,
        provider_path=provider_path,
        mcp_servers=mcp_servers,
        context_files=context_files,
        allowed_read_dirs=allowed_read_dirs,
        allowed_write_dir=allowed_write_dir,
        allowed_evidence_snapshot_dir=allowed_evidence_snapshot_dir,
        strict_authority=strict_authority,
        exact_bash_commands=exact_bash_commands,
        model=model,
    )
    if frozen_capability is not None and validated_capability != frozen_capability:
        raise LLMRoleContractError(
            f"{contract.role_id}: frozen capability replay mismatch"
        )
    # The sealed renderer bytes are immutable evidence.  Do not even strip
    # trailing whitespace here: doing so would make the provider prefix differ
    # from the receipt while appearing visually identical in logs.
    base = str(
        rendered_prompt.text if provider_prefix is None else provider_prefix
    )
    if rendered_prompt.text not in base:
        raise LLMRoleContractError(
            f"{contract.role_id}: provider prefix does not contain the sealed "
            "renderer output"
        )
    suffix = render_llm_role_contract_suffix(
        contract,
        role_name,
        tools=tools,
        mcp_servers=mcp_servers,
        rendered_provider_prefix=base,
        dispatch_receipt=dispatch_receipt,
        frozen_capability=validated_capability,
    )
    if max_chars is not None:
        if len(base) + len(suffix) > int(max_chars):
            raise LLMRoleContractError(
                f"{contract.role_id}: sealed provider prompt exceeds the "
                "provider budget; renderer output cannot be truncated"
            )
    return base + suffix, contract


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




# Serialize role-IO log rotation across threads/processes. Without this lock, two concurrent
# appenders can both observe the file over the size cap and race the rename
# (one wins, the other's rename throws FileNotFoundError — swallowed by the
# except, benign but loses the backup). The lock makes the rotate-then-append
# atomic. A threading.Lock suffices within one process; cross-process safety
# for the append itself is provided by fcntl (locked_file below).
_ROLE_IO_ROTATION_LOCK = threading.Lock()

#: Cap a single role-IO log at 20MB before rotating to one backup (``.1``).
#: Historical role logs grew without an upper bound; this is the structural cap
#: (lowered here because role-IO files are append-heavy and per-role).
_ROLE_IO_MAX_BYTES = 20 * 1024 * 1024


_LLM_FIRST_ACTIVITY_WARN_SEC = float(
    os.environ.get("POK_LLM_FIRST_ACTIVITY_WARN_SEC", "60")
)

_LLM_PROGRESS_INTERVAL_SEC = float(
    os.environ.get("POK_LLM_PROGRESS_INTERVAL_SEC", "120")
)

_LLM_SILENCE_WARN_SEC = float(
    os.environ.get("POK_LLM_SILENCE_WARN_SEC", "240")
)

_ROLE_TIMEOUT_DEFAULTS = {
    # Fallback for analysis/probe roles such as MATCH ANALYST, COMBINED
    # ANALYST and literature-probe roles. These can be
    # slower than gate roles on GLM-backed Claude-compatible endpoints, but must
    # still have a hard ceiling so the pipeline cannot wait forever.
    "DEFAULT": (240.0, 360.0, 900.0),
    # The final Master is a zero-tool selector/compiler over the frozen three-
    # proposal/two-ballot packet.  Its prompt and output schema are necessarily
    # large. Live v148 emitted only system/thinking telemetry through the old
    # 240s first-substantive boundary and was killed at 277.5s including owned
    # cleanup, even though every Scout/ballot input was already durable. Give
    # this zero-tool selector/compiler 360s for first activity and later
    # productive-message silence. This is independent from both proposal
    # Scouts and ordinary Master roles and remains bounded by the same 900s
    # total ceiling.
    "MASTER_FINAL": (360.0, 360.0, 900.0),
    # Proposal Scouts are Read-capable mechanism designers.  Live strict runs
    # routinely complete between 155s and 236s after one or more bounded Read
    # round-trips. Live singleton-successor evidence also showed a valid
    # counterfactual role still computing at the former 240s boundary. Keep the
    # pre-output gate at 120s, but give an already productive Scout 360s of
    # silence. Successful roles are now journaled separately, so this larger
    # per-role bound no longer multiplies into whole-ensemble redispatch.
    # System/thinking telemetry remains nonproductive and the 900s total
    # ceiling is unchanged.
    "MASTER_PROPOSAL": (120.0, 360.0, 900.0),
    # Master is the highest leverage failure point: it plans, reads evidence,
    # and can otherwise burn the whole orchestrator cycle before any code exists.
    "MASTER": (120.0, 240.0, 900.0),
    # Review/Critic can be slow on GLM-backed Claude-compatible endpoints.
    # They still have ceilings, but defaults must be long enough to avoid
    # repeated 600s retries that keep the generation stuck at quality_passed.
    "REVIEW": (180.0, 360.0, 1200.0),
    "CRITIC": (180.0, 360.0, 900.0),
    # Crossover synthesizes a whole child bot from two parents and routinely
    # exceeds the generic analysis/probe budget on GLM-backed Claude-compatible
    # endpoints. Keep the idle ceiling, but give total wall-clock enough room so
    # a live stream is not killed and restarted at ~15 minutes.
    "CROSSOVER": (240.0, 420.0, 2400.0),
    # Workers already have an outer WORKER_TIMEOUT. Live v147 showed legitimate
    # Read/tool reasoning repeatedly crossing the generic 180s mid-loop stall
    # ceiling: four provider streams were restarted from the same frozen prompt
    # before any Edit could land. Give a productive Worker the full 360s idle
    # window while retaining the 180s no-first-output gate and 1000s total cap.
    "WORKER": (180.0, 360.0, 1000.0),
}


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
    mode = os.environ.get("POK_LLM_THINKING_MODE", "enabled").strip().lower()
    if mode == "disabled":
        return {"thinking": {"type": "disabled"}}
    if mode == "adaptive":
        return {"thinking": {"type": "adaptive"}}
    # default / "enabled": deep reasoning with strong effort. GLM-5.2 treats
    # budget_tokens as a soft target (not a hard cap), so a large budget (default
    # 64000) lets the model reason as deeply as it needs and still converge.
    # effort=max selects GLM's strongest reasoning depth, producing the highest
    # quality strategy output. Both are now the defaults after confirming that:
    # (1) GLM does NOT enter a death-loop at effort=max — thinking tokens grow
    # linearly and the model eventually emits visible text; it is simply slow,
    # requiring higher role timeouts (see _ROLE_TIMEOUT_DEFAULTS / env.runtime).
    # (2) The earlier "infinite loop" diagnosis was a misattribution: the
    # stream was killed by insufficient timeouts (900s) while GLM was still
    # productively reasoning at 27k-66k thinking tokens.
    budget = int(os.environ.get("POK_LLM_THINKING_BUDGET", "64000"))
    options: dict = {"thinking": {"type": "enabled", "budget_tokens": budget}}
    effort = os.environ.get("POK_LLM_EFFORT", "max").strip().lower()
    if effort:
        options["effort"] = effort
    return options


def _role_timeout_policy(role_name: str) -> dict:
    """Return hard stream timeout policy for a role.

    Values <=0 disable that timeout. Environment overrides are intentionally
    role-scoped so slow backends can be tuned without changing code.
    """
    role = str(role_name or "").upper()
    key = ""
    if re.fullmatch(r"MASTER(?:\s+\(TRY\s+\d+\))?", role):
        key = "MASTER_FINAL"
    elif re.fullmatch(
        r"MASTER PROPOSAL (?:MECHANISM|COUNTERFACTUAL|COMPUTE_MEMORY)"
        r"(?: (?:SCHEMA|DISTINCTNESS) RETRY)?",
        role,
    ):
        key = "MASTER_PROPOSAL"
    elif "MASTER" in role:
        key = "MASTER"
    elif "REVIEW" in role:
        key = "REVIEW"
    elif "CRITIC" in role:
        key = "CRITIC"
    elif "CROSSOVER" in role:
        key = "CROSSOVER"
    elif "WORKER" in role:
        key = "WORKER"
    defaults = _ROLE_TIMEOUT_DEFAULTS.get(key or "DEFAULT", (0.0, 0.0, 0.0))

    def _env(name, default):
        names = [name]
        # Preserve existing operator overrides while giving the zero-tool final
        # compiler its own more-specific namespace.  MASTER_FINAL wins when
        # both are present; legacy MASTER remains a safe fallback.
        if key in {"MASTER_FINAL", "MASTER_PROPOSAL"} and (
            f"POK_LLM_{key}_" in name
        ):
            names.append(name.replace(
                f"POK_LLM_{key}_", "POK_LLM_MASTER_", 1
            ))
        for candidate in names:
            if candidate not in os.environ:
                continue
            try:
                parsed = float(os.environ[candidate])
                if math.isfinite(parsed):
                    return parsed
            except Exception:
                pass
            # A malformed/non-finite role-specific override must not mask a
            # valid legacy operator override. Continue through the ordered
            # fallback chain and use the compiled default only if none parses.
            continue
        return float(default)

    prefix = f"POK_LLM_{key}_" if key else "POK_LLM_DEFAULT_"
    first_activity = _env(prefix + "FIRST_ACTIVITY_TIMEOUT", defaults[0])
    idle = _env(prefix + "IDLE_TIMEOUT", defaults[1])
    total = _env(prefix + "TOTAL_TIMEOUT", defaults[2])
    # B3 (2026-07-09): a shorter stall ceiling enforced AFTER the first
    # substantive model output, i.e. once the stream has entered the
    # tool/thinking loop. Backends like the deepseek-v4-pro endpoint behind
    # cc-switch intermittently stall mid-tool-loop (a tool_use is emitted but
    # its tool_result never returns, or the model stops streaming mid-think).
    # The full idle_timeout (240-420s) is appropriate for the FIRST real
    # output but is too long to wait once we are already in the loop: every
    # mid-loop stall costs the full idle budget before the role retry can
    # restart. Default to ~55% of idle (clamped to [60, 180]s) so a stall is
    # caught well before the full idle ceiling while still tolerating legit
    # slow tool/think deltas. 0 disables (falls back to idle_timeout).
    stall_default = (
        360.0
        if key in {"MASTER_PROPOSAL", "MASTER_FINAL", "WORKER"}
        else 0.0
    )
    if idle > 0 and key not in {
        "MASTER_FINAL",
        "MASTER_PROPOSAL",
        "WORKER",
    }:
        stall_default = max(60.0, min(180.0, idle * 0.55))
    stall = _env(prefix + "STALL_TIMEOUT", stall_default)
    return {
        "policy_key": key or "DEFAULT",
        "first_activity_timeout": first_activity,
        "idle_timeout": idle,
        "stall_timeout": stall,
        "total_timeout": total,
    }


class LLMStreamNextTimeout(asyncio.TimeoutError):
    """One SDK ``__anext__`` exceeded its deadline.

    ``pending_task`` is retained only when cancellation did not complete during
    the bounded grace period.  The attempt owner must then close its exact SDK
    transport and prove both task and child-process exit before another provider
    call may start.
    """

    def __init__(self, pending_task=None):
        self.pending_task = pending_task
        super().__init__("SDK stream __anext__ timed out")


class LLMProviderCleanupError(ConnectionError):
    """The SDK stream required exceptional transport-level cleanup."""

    def __init__(self, message, *, provider_exit_confirmed=False, attempt_id=None):
        self.provider_exit_confirmed = bool(provider_exit_confirmed)
        self.attempt_id = attempt_id
        super().__init__(str(message))


class LLMProviderCleanupBlocked(LLMProviderCleanupError):
    """A prior provider attempt has not yet proven task/process termination."""


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
    try:
        import event_bus
        event_bus.emit(category, severity, message, **fields)
    except Exception:
        pass


def _role_log_metadata(log_file_path):
    path = str(log_file_path or "")
    meta = {"log_file": path}
    match = re.search(
        r"/v(\d+)/logs/(?:[^/]+/)*([^/]+)_io\.txt$",
        path,
    )
    if match:
        meta["version"] = int(match.group(1))
        meta["role_log"] = match.group(2)
    return meta


def _role_log_basename(log_file_path):
    """Return a short relative path for metrics logging (e.g. v1/.../master_io.txt)."""
    path = str(log_file_path or "")
    match = re.search(r"/(v\d+/logs/.+)$", path)
    if match:
        return match.group(1)
    return path.rsplit("/", 1)[-1] if path else None


def _tools_metadata(tools):
    if tools is None:
        return {"tools": []}
    if isinstance(tools, (list, tuple)):
        return {"tools": [str(t) for t in tools]}
    return {"tools": [type(tools).__name__]}


def _usage_metadata(usage):
    if not usage:
        return {}
    try:
        data = usage if isinstance(usage, dict) else usage.model_dump()
    except Exception:
        try:
            data = dict(usage)
        except Exception:
            data = {}
    summary = {}
    for key in (
        "input_tokens", "output_tokens", "cache_creation_input_tokens",
        "cache_read_input_tokens",
    ):
        if key in data:
            summary[key] = data.get(key)
    return summary


def _llm_failure_severity(exc: Exception) -> str:
    """Classify known noisy SDK/business failures without hiding hard failures."""
    if is_success_error_result(exc):
        return "info"
    return "error"


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
    try:
        log_file_path = os.fspath(log_file_path)
        # Resolve the current run_id for the correlation prefix. event_bus reads
        # the live checkpoint as fallback, so this works even in long-lived
        # worker threads that are not pinned to one generation.
        try:
            from event_bus import capture_context
            _ctx = capture_context() or {}
            _rid = _ctx.get("run_id") or "-"
        except Exception:
            _rid = "-"
        chunk = f"[{_rid}] {text}" if not text.startswith("\n") else f"\n[{_rid}] " + text.lstrip("\n")
        from evolution_infra import locked_file
        with _ROLE_IO_ROTATION_LOCK:
            with locked_file(log_file_path, "a+", encoding="utf-8") as lf:
                lf.seek(0, os.SEEK_END)
                strict_log = f"{os.sep}strict_invocations{os.sep}" in (
                    os.path.abspath(log_file_path)
                )
                if lf.tell() > _ROLE_IO_MAX_BYTES and not strict_log:
                    lf.seek(0)
                    previous = lf.read()
                    with locked_file(
                        log_file_path + ".1",
                        "w",
                        encoding="utf-8",
                    ) as rotated:
                        rotated.write(previous)
                        rotated.flush()
                        os.fsync(rotated.fileno())
                    lf.seek(0)
                    lf.truncate()
                lf.seek(0, os.SEEK_END)
                lf.write(chunk)
                lf.flush()
    except Exception:
        pass


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
    return {
        "attempt_id": uuid.uuid4().hex,
        "transport": transport,
        "owned_process": None,
        "pending_tasks": set(),
        "cleanup_reasons": [],
        "cleanup_task": None,
        "transport_close_attempted": False,
        "transport_close_confirmed": False,
    }


_CANONICAL_ABANDON_RESULT_FIELDS = frozenset({
    "abandoned",
    "cleared_checkpoint",
    "workflow_run_id",
    "abandon_transaction_id",
    "abandon_receipt_digest",
    "finalize_receipt_digest",
    "abandon_checkpoint_identity",
})
TERMINAL_ABANDON_RESULT_OWNER_TOOLS = frozenset({
    "abandon_generation",
    "prepare_next_gen",
    "run_crossover",
    "run_direction_audit",
    "run_literature_probe",
    "run_master",
    "execute_workers",
    "run_quality_gates",
    "run_review",
    "run_critic",
    "run_precommit_eval",
    "commit_bot",
})
_EVOLUTION_PROVIDER_TOOL_PREFIX = "mcp__evolution__"


def _normalized_provider_tool_name(name: object) -> str:
    return str(name or "").rsplit("__", 1)[-1]


def _canonical_provider_tool_args(args: object) -> str | None:
    if not isinstance(args, dict):
        return None
    try:
        return json.dumps(
            args,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        return None


def register_current_provider_evolution_tool_use(
    tool_use_id: str,
    raw_name: object,
    args: object,
) -> bool:
    """Bind one observed Evolution MCP ToolUse to the active SDK attempt.

    An in-process MCP handler receives only name+arguments, not the provider's
    ToolUse id.  The SDK is allowed to invoke that handler before the outer
    stream yields its corresponding message, so a handler may have retained a
    *provisional* same-attempt proof.  It becomes consumable only here, when
    exactly one un-settled ToolUse has the exact normalized owner and canonical
    arguments.  A duplicate exact registration makes attribution ambiguous and
    invalidates the cache.  Other UserMessage content is deliberately not a
    registration capability.
    """

    attempt = _LLM_PROVIDER_ATTEMPT.get()
    name = str(raw_name or "")
    canonical_args = _canonical_provider_tool_args(args)
    identifier = str(tool_use_id or "")
    if (
        not isinstance(attempt, dict)
        or not name.startswith(_EVOLUTION_PROVIDER_TOOL_PREFIX)
        or not identifier
        or canonical_args is None
    ):
        return False
    entry = {
        "tool_use_id": identifier,
        "owner_tool": _normalized_provider_tool_name(name),
        "arguments": canonical_args,
        "settled": False,
    }
    with _PROVIDER_CLEANUP_LOCK:
        registrations = attempt.setdefault("registered_evolution_tool_uses", {})
        if not isinstance(registrations, dict) or identifier in registrations:
            return False
        registrations[identifier] = entry
        provisional = attempt.get("provisional_verified_terminal_abandon")
        bound = attempt.get("verified_terminal_abandon")
        if isinstance(provisional, dict):
            matches = [
                value
                for value in registrations.values()
                if isinstance(value, dict)
                and value.get("settled") is not True
                and value.get("owner_tool") == provisional.get("owner_tool")
                and value.get("arguments") == provisional.get("arguments")
            ]
            if len(matches) == 1:
                candidate_id = str(matches[0].get("tool_use_id") or "")
                if candidate_id:
                    record = deepcopy(provisional)
                    record["tool_use_id"] = candidate_id
                    attempt.pop("provisional_verified_terminal_abandon", None)
                    attempt["verified_terminal_abandon"] = record
                else:
                    attempt.pop("provisional_verified_terminal_abandon", None)
                    attempt["verified_terminal_abandon_conflict"] = True
            elif len(matches) > 1:
                attempt.pop("provisional_verified_terminal_abandon", None)
                attempt["verified_terminal_abandon_conflict"] = True
        elif isinstance(bound, dict):
            # The handler did not receive a ToolUse id.  A later duplicate
            # exact owner+arguments registration would make the existing bind
            # speculative, so retain neither candidate.
            if (
                bound.get("owner_tool") == entry["owner_tool"]
                and bound.get("arguments") == entry["arguments"]
                and str(bound.get("tool_use_id") or "") != identifier
            ):
                attempt.pop("verified_terminal_abandon", None)
                attempt["verified_terminal_abandon_conflict"] = True
    return True


def settle_current_provider_evolution_tool_use(tool_use_id: str) -> None:
    """Mark one stream-observed Evolution ToolUse settled after its SDK result."""

    attempt = _LLM_PROVIDER_ATTEMPT.get()
    identifier = str(tool_use_id or "")
    if not isinstance(attempt, dict) or not identifier:
        return
    with _PROVIDER_CLEANUP_LOCK:
        registrations = attempt.get("registered_evolution_tool_uses")
        entry = registrations.get(identifier) if isinstance(registrations, dict) else None
        if isinstance(entry, dict):
            entry["settled"] = True


def _single_canonical_abandon_result(value):
    """Extract exactly one terminal-abandon payload from a tool return shape.

    The SDK can carry a local MCP return as a JSON string, a text content
    block, or an already-decoded nested mapping.  This helper deliberately
    accepts only one complete payload: duplicated flattened/nested terminal
    objects remain ambiguous and are not cacheable.
    """

    matches = []

    def collect(candidate):
        if isinstance(candidate, dict):
            if _CANONICAL_ABANDON_RESULT_FIELDS.issubset(candidate):
                matches.append(candidate)
            for key in ("abandon_result", "result", "content", "text"):
                if key in candidate:
                    collect(candidate.get(key))
            return
        if isinstance(candidate, list):
            for item in candidate:
                collect(item)
            return
        if isinstance(candidate, str):
            try:
                collect(json.loads(candidate))
            except (TypeError, json.JSONDecodeError):
                pass

    collect(value)
    if len(matches) != 1:
        return None
    try:
        return deepcopy(matches[0])
    except Exception:
        return None


def cache_verified_provider_terminal_abandon(
    owner_tool: str,
    baseline_checkpoint: dict,
    raw_result,
    args: object,
):
    """Cache one already-reproved terminal result for the active SDK attempt.

    This is a narrow transport-loss bridge, not durable recovery authority.
    A guarded mutating MCP handler calls it only after returning its actual
    result.  The cache exists solely in the active provider-attempt mapping.
    If the SDK handler runs before the stream exposes its ToolUse, this
    function retains an unconsumable provisional record; only a later unique
    exact registration can attach the provider ToolUse id.  A process restart,
    a different attempt, a missing registration, or a second candidate all
    remain fail-closed in the Orchestrator.
    """

    attempt = _LLM_PROVIDER_ATTEMPT.get()
    owner = str(owner_tool or "")
    canonical_args = _canonical_provider_tool_args(args)
    if (
        not isinstance(attempt, dict)
        or not isinstance(baseline_checkpoint, dict)
        or owner not in TERMINAL_ABANDON_RESULT_OWNER_TOOLS
        or canonical_args is None
    ):
        return None
    terminal_result = _single_canonical_abandon_result(raw_result)
    if terminal_result is None:
        return None
    try:
        from tool_bot_management import validate_completed_abandon_handoff

        terminal_proof = validate_completed_abandon_handoff(
            deepcopy(baseline_checkpoint),
            terminal_result,
        )
        record = {
            "owner_tool": owner,
            "arguments": canonical_args,
            "terminal_result": terminal_result,
            "terminal_proof": deepcopy(terminal_proof),
        }
        # Canonical JSON makes later SDK/cache equality checks independent of
        # dictionary insertion order and prevents a caller from mutating our
        # retained object after this function returns.
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except Exception:
        return None
    with _PROVIDER_CLEANUP_LOCK:
        if (
            attempt.get("verified_terminal_abandon") is not None
            or attempt.get("provisional_verified_terminal_abandon") is not None
            or attempt.get("verified_terminal_abandon_conflict") is True
        ):
            # Two terminal results in one provider attempt are ambiguous even
            # when their fields happen to look similar.  Do not retain either.
            attempt.pop("verified_terminal_abandon", None)
            attempt.pop("provisional_verified_terminal_abandon", None)
            attempt["verified_terminal_abandon_conflict"] = True
            return None
        registrations = attempt.get("registered_evolution_tool_uses")
        all_matches = (
            [
                value
                for value in registrations.values()
                if isinstance(value, dict)
                and value.get("owner_tool") == owner
                and value.get("arguments") == canonical_args
            ]
            if isinstance(registrations, dict)
            else []
        )
        candidates = [
            value for value in all_matches if value.get("settled") is not True
        ]
        # Same-name or same-argument concurrent calls cannot be inferred from
        # the handler alone.  Preserve normal SDK delivery, but never cache
        # ambiguity.  Zero matches is the documented handler-before-stream
        # race: keep an unconsumable record until one exact registration binds.
        # A settled historical exact registration is also ambiguous: the
        # handler lacks a provider id, so it must not speculate that a later
        # same-name/same-argument ToolUse is its owner.
        if len(candidates) > 1 or len(all_matches) != len(candidates):
            attempt["verified_terminal_abandon_conflict"] = True
            return None
        if len(candidates) == 1:
            tool_use_id = str(candidates[0].get("tool_use_id") or "")
            if not tool_use_id:
                attempt["verified_terminal_abandon_conflict"] = True
                return None
            record["tool_use_id"] = tool_use_id
            attempt["verified_terminal_abandon"] = record
            return deepcopy(record)
        attempt["provisional_verified_terminal_abandon"] = record
    # A provisional record intentionally has no ToolUse id and is neither a
    # successful cache return nor visible through
    # ``current_provider_verified_terminal_abandon``.  It can become authority
    # only through a later unique exact registration above.
    return None


def current_provider_verified_terminal_abandon():
    """Return the active attempt's one in-memory verified terminal record.

    Callers must still bind it to a pending SDK ToolUse and revalidate it
    against their own immutable pre-call checkpoint snapshot.
    """

    attempt = _LLM_PROVIDER_ATTEMPT.get()
    if not isinstance(attempt, dict):
        return None
    with _PROVIDER_CLEANUP_LOCK:
        if attempt.get("verified_terminal_abandon_conflict") is True:
            return None
        record = attempt.get("verified_terminal_abandon")
        if not isinstance(record, dict):
            return None
        try:
            return deepcopy(record)
        except Exception:
            return None


def _capture_owned_provider_process(attempt):
    if not isinstance(attempt, dict):
        return None
    transport = attempt.get("transport")
    process = getattr(transport, "_process", None)
    if process is not None and attempt.get("owned_process") is None:
        attempt["owned_process"] = process
    return attempt.get("owned_process")


def _register_unresolved_provider_attempt(attempt, reason, *tasks):
    if not isinstance(attempt, dict):
        return
    _capture_owned_provider_process(attempt)
    for task in tasks:
        if isinstance(task, asyncio.Task):
            attempt.setdefault("pending_tasks", set()).add(task)
            task.add_done_callback(_consume_task_result)
    reasons = attempt.setdefault("cleanup_reasons", [])
    reason = str(reason or "provider_cleanup_unresolved")
    if reason not in reasons:
        reasons.append(reason)
    with _PROVIDER_CLEANUP_LOCK:
        _UNRESOLVED_PROVIDER_ATTEMPTS[attempt["attempt_id"]] = attempt


def _provider_attempt_exit_confirmed(attempt):
    if (
        not isinstance(attempt, dict)
        or not attempt.get("transport_close_attempted")
        or not attempt.get("transport_close_confirmed")
    ):
        return False
    if any(
        isinstance(task, asyncio.Task) and not task.done()
        for task in attempt.get("pending_tasks") or ()
    ):
        return False
    cleanup_task = attempt.get("cleanup_task")
    if isinstance(cleanup_task, asyncio.Task) and not cleanup_task.done():
        try:
            current_task = asyncio.current_task()
        except RuntimeError:
            current_task = None
        if cleanup_task is not current_task:
            return False
    owned_process = _capture_owned_provider_process(attempt)
    if owned_process is not None and getattr(owned_process, "returncode", None) is None:
        return False
    transport_process = getattr(attempt.get("transport"), "_process", None)
    if (
        transport_process is not None
        and getattr(transport_process, "returncode", None) is None
    ):
        return False
    return True


def _resolve_provider_attempt_if_stopped(attempt):
    if isinstance(attempt, dict) and attempt.get("transport_close_attempted"):
        owned_process = _capture_owned_provider_process(attempt)
        transport_process = getattr(attempt.get("transport"), "_process", None)
        if (
            (owned_process is None or getattr(owned_process, "returncode", None) is not None)
            and (
                transport_process is None
                or getattr(transport_process, "returncode", None) is not None
            )
        ):
            attempt["transport_close_confirmed"] = True
    if not _provider_attempt_exit_confirmed(attempt):
        return False
    with _PROVIDER_CLEANUP_LOCK:
        _UNRESOLVED_PROVIDER_ATTEMPTS.pop(attempt.get("attempt_id"), None)
    return True


def _assert_no_unresolved_provider_attempts():
    blocked = []
    with _PROVIDER_CLEANUP_LOCK:
        attempts = list(_UNRESOLVED_PROVIDER_ATTEMPTS.values())
    for attempt in attempts:
        if _resolve_provider_attempt_if_stopped(attempt):
            continue
        blocked.append(attempt)
    if blocked:
        details = ", ".join(
            f"{item.get('attempt_id')}:{'|'.join(item.get('cleanup_reasons') or [])}"
            for item in blocked[:3]
        )
        raise LLMProviderCleanupBlocked(
            "prior SDK provider cleanup is unresolved; refusing a new provider "
            f"dispatch ({details})",
            provider_exit_confirmed=False,
            attempt_id=blocked[0].get("attempt_id"),
        )


def _track_pending_stream_task(task, reason):
    attempt = _LLM_PROVIDER_ATTEMPT.get()
    if isinstance(attempt, dict):
        _register_unresolved_provider_attempt(attempt, reason, task)
    else:
        task.add_done_callback(_consume_task_result)


def _provider_stream_cancel_grace():
    try:
        grace = max(
            0.0,
            min(
                5.0,
                float(os.environ.get("POK_LLM_NEXT_CANCEL_GRACE", "1")),
            ),
        )
    except (TypeError, ValueError):
        grace = 1.0
    total_scope = _LLM_TOTAL_DEADLINE.get()
    total_deadline = (
        (total_scope or {}).get("deadline")
        if isinstance(total_scope, dict)
        else None
    )
    if total_deadline is not None:
        grace = min(grace, max(0.0, float(total_deadline) - time.time()))
    return grace


async def cancel_provider_stream_task_bounded(
    task,
    reason,
    *,
    attempt=None,
    grace=None,
):
    """Cancel one owned stream task without waiting beyond a fixed grace.

    A task which ignores cancellation is retained by its exact provider
    attempt.  The transport-level cleanup boundary then owns termination and
    future provider dispatch remains blocked until task and process exit are
    both proven.
    """

    if not isinstance(task, asyncio.Task):
        raise TypeError("provider stream cancellation requires an asyncio.Task")
    if task.done():
        _consume_task_result(task)
        return True
    task.cancel()
    if grace is None:
        grace = _provider_stream_cancel_grace()
    else:
        try:
            grace = max(0.0, min(30.0, float(grace)))
        except (TypeError, ValueError):
            grace = _provider_stream_cancel_grace()
    try:
        if grace > 0:
            await asyncio.wait({task}, timeout=grace)
    except BaseException:
        if not task.done():
            if isinstance(attempt, dict):
                _register_unresolved_provider_attempt(attempt, reason, task)
            else:
                _track_pending_stream_task(task, reason)
        raise
    if task.done():
        _consume_task_result(task)
        return True
    if isinstance(attempt, dict):
        _register_unresolved_provider_attempt(attempt, reason, task)
    else:
        _track_pending_stream_task(task, reason)
    return False


async def _await_stream_next_bounded(stream_iter, timeout):
    """Await one SDK message without unbounded ``wait_for`` cancellation.

    ``asyncio.wait_for`` waits for a cancellation-resistant awaitable to finish
    cancelling, so its wall-clock can exceed the timeout indefinitely.  Race a
    task against the timeout, give SDK cleanup a small bounded grace, then let
    the caller raise its typed role timeout.
    """

    task = asyncio.create_task(stream_iter.__anext__())
    try:
        done, _pending = await asyncio.wait({task}, timeout=timeout)
        if task in done:
            return task.result()
        cancelled = await cancel_provider_stream_task_bounded(
            task,
            "stream_next_cancellation_unconfirmed",
        )
        if not cancelled:
            raise LLMStreamNextTimeout(task)
        raise LLMStreamNextTimeout()
    except LLMStreamNextTimeout:
        raise
    except BaseException:
        if not task.done():
            await cancel_provider_stream_task_bounded(
                task,
                "stream_next_parent_cancellation_unconfirmed",
            )
        raise


async def await_provider_stream_next_bounded(stream_iter, timeout):
    """Public owned-provider boundary used by both role stream runtimes."""

    return await _await_stream_next_bounded(stream_iter, timeout)


async def _process_stream(query_gen, log_file_path, ui, role_name):
    """Process a streaming LLM query, returning (texts, cost_usd, usage).

    Handles TextBlock, ThinkingBlock, ToolUseBlock, UserMessage ToolResultBlock,
    and ResultMessage.
    Writes to log file and emits UI events as they arrive.
    """
    texts = []
    cost_usd = None
    usage = None
    availability_trace = LLMAvailabilityTrace()
    stream_started_at = time.time()
    # Metrics: track first-token and first-text latencies for call analytics.
    first_productive_at = None  # first AssistantMessage/ToolUse/etc
    first_text_at = None        # first TextBlock with non-empty text
    # Metrics: capture ResultMessage diagnostic fields for llm_call_metrics.jsonl.
    result_diag = {}            # subtype, is_error, num_turns, stop_reason, etc.
    assistant_message_count = 0
    first_activity_logged = False
    # B2 (2026-07-09): a SystemMessage (e.g. subtype=init, thinking_tokens) is
    # emitted by the SDK/proxy purely to acknowledge the request or carry
    # billing telemetry — it is not model output. Letting it satisfy the
    # first-activity gate flips the wait budget from first_activity_timeout to
    # idle_timeout (e.g. 240s → 420s for CROSSOVER). When a backend (here the
    # GLM proxy behind cc-switch) stalls right after init, that extra slack
    # turns a hard stall into a ~420s dead wait per attempt. Track substantive
    # activity (AssistantMessage/ToolUse/UserMessage/ResultMessage) separately
    # and keep enforcing first_activity_timeout until real output arrives.
    substantive_activity_logged = False
    message_count = 0
    last_progress_at = stream_started_at
    last_message_at = stream_started_at
    last_silence_event_at = stream_started_at
    stream_done = False
    text_chars = 0
    thinking_chars = 0
    tool_use_count = 0
    tool_result_count = 0
    system_message_count = 0
    thinking_tokens_estimate = 0
    thinking_tokens_delta_total = 0
    unknown_message_count = 0
    timeout_policy = _role_timeout_policy(role_name)
    total_timeout = float(timeout_policy.get("total_timeout") or 0)
    first_activity_timeout = float(timeout_policy.get("first_activity_timeout") or 0)
    idle_timeout = float(timeout_policy.get("idle_timeout") or 0)
    # B3: shorter stall ceiling once substantive output has started (tool/think
    # loop). 0 means "do not enforce a separate stall ceiling; use idle_timeout".
    stall_timeout = float(timeout_policy.get("stall_timeout") or 0)
    total_scope = _LLM_TOTAL_DEADLINE.get()
    scoped_deadline = (
        float(total_scope.get("deadline"))
        if isinstance(total_scope, dict) and total_scope.get("deadline") is not None
        else None
    )
    scoped_started_at = (
        float(total_scope.get("started_at"))
        if isinstance(total_scope, dict) and total_scope.get("started_at") is not None
        else stream_started_at
    )
    attempt_total_deadline = (
        stream_started_at + total_timeout if total_timeout > 0 else None
    )
    total_deadline = attempt_total_deadline
    if scoped_deadline is not None and (
        total_deadline is None or scoped_deadline < total_deadline
    ):
        total_deadline = scoped_deadline

    def _tool_result_text(content):
        if isinstance(content, str):
            return content
        if content is None:
            return ""
        try:
            return json.dumps(content, ensure_ascii=False, default=str)
        except Exception:
            return str(content)

    def _record_tool_result(
        content,
        is_error=None,
        source="ToolResultBlock",
        tool_use_id=None,
    ):
        nonlocal tool_result_count
        tool_result_count += 1
        result_text = _tool_result_text(content)
        if not result_text:
            result_text = "[empty tool result]"
        result_preview = result_text[:3000]
        header = f"[TOOL_RESULT source={source} is_error={bool(is_error)}]"
        _append_role_io(log_file_path, f"\n{header} {result_preview}\n")
        ui.log_io(result_preview, "tool_result", role_name)
        _record_llm_tool_trace_event({
            "event": "tool_result",
            "tool_use_id": str(tool_use_id or ""),
            "is_error": bool(is_error),
            "source": str(source),
            "content_chars": len(result_text),
            "content_sha256": hashlib.sha256(result_text.encode("utf-8")).hexdigest(),
            "content_preview": result_text[:3000],
        })

    def _mark_first_activity(kind, substantive=True):
        nonlocal first_activity_logged, substantive_activity_logged
        # substantive output (assistant/tool/user/result) upgrades the gate so
        # the wait loop may switch to the idle_timeout budget. System-only
        # messages record the first-activity milestone for observability but do
        # NOT lift the (shorter) first_activity_timeout ceiling — see B2.
        if substantive:
            substantive_activity_logged = True
        if first_activity_logged:
            return
        first_activity_logged = True
        elapsed = time.time() - stream_started_at
        delayed = elapsed >= _LLM_FIRST_ACTIVITY_WARN_SEC
        category = (
            "pipeline.llm_role_first_activity_delayed"
            if delayed else
            "pipeline.llm_role_first_activity"
        )
        severity = "warn" if delayed else "info"
        _emit_llm_event(
            category, severity,
            f"{role_name}: first LLM stream activity after {elapsed:.1f}s",
            role=role_name,
            elapsed_sec=round(elapsed, 2),
            first_activity_warn_sec=_LLM_FIRST_ACTIVITY_WARN_SEC,
            activity_kind=kind,
            substantive=substantive,
            **_role_log_metadata(log_file_path),
        )

    def _emit_progress():
        nonlocal last_progress_at
        if _LLM_PROGRESS_INTERVAL_SEC <= 0:
            return
        now = time.time()
        if now - last_progress_at < _LLM_PROGRESS_INTERVAL_SEC:
            return
        elapsed = now - stream_started_at
        last_progress_at = now
        _emit_llm_event(
            "pipeline.llm_role_progress", "info",
            f"{role_name}: LLM stream active for {elapsed:.1f}s",
            role=role_name,
            elapsed_sec=round(elapsed, 2),
            messages_seen=message_count,
            system_messages_seen=system_message_count,
            unknown_messages_seen=unknown_message_count,
            text_chars=text_chars,
            thinking_chars=thinking_chars,
            thinking_tokens_estimate=thinking_tokens_estimate,
            thinking_tokens_delta_total=thinking_tokens_delta_total,
            tool_use_count=tool_use_count,
            tool_result_count=tool_result_count,
            progress_interval_sec=_LLM_PROGRESS_INTERVAL_SEC,
            **_role_log_metadata(log_file_path),
        )

    async def _silence_watchdog():
        nonlocal last_silence_event_at
        if _LLM_SILENCE_WARN_SEC <= 0:
            return
        sleep_for = max(0.01, min(_LLM_SILENCE_WARN_SEC / 2.0, 30.0))
        while not stream_done:
            await asyncio.sleep(sleep_for)
            if stream_done:
                return
            now = time.time()
            silent_for = now - last_message_at
            since_last_event = now - last_silence_event_at
            if silent_for < _LLM_SILENCE_WARN_SEC:
                continue
            if since_last_event < _LLM_SILENCE_WARN_SEC:
                continue
            last_silence_event_at = now
            _emit_llm_event(
                "pipeline.llm_role_stream_silent", "warn",
                f"{role_name}: no productive LLM stream messages for {silent_for:.1f}s",
                role=role_name,
                elapsed_sec=round(now - stream_started_at, 2),
                silent_for_sec=round(silent_for, 2),
                silence_warn_sec=_LLM_SILENCE_WARN_SEC,
                messages_seen=message_count,
                system_messages_seen=system_message_count,
                unknown_messages_seen=unknown_message_count,
                text_chars=text_chars,
                thinking_chars=thinking_chars,
                thinking_tokens_estimate=thinking_tokens_estimate,
                thinking_tokens_delta_total=thinking_tokens_delta_total,
                tool_use_count=tool_use_count,
                tool_result_count=tool_result_count,
                **_role_log_metadata(log_file_path),
            )

    def _should_log_sparse_count(count):
        return count == 1 or count in {5, 10, 20, 50} or count % 100 == 0

    def _timeout_limit(effective_kind, wait_timeout):
        if effective_kind == "total":
            return total_timeout
        if effective_kind == "first_activity":
            return first_activity_timeout
        if effective_kind == "idle":
            return idle_timeout
        if effective_kind == "stall":
            return stall_timeout
        return wait_timeout or 0

    def _raise_role_timeout(
        timeout_kind,
        wait_timeout,
        *,
        pending_stream_task=None,
    ):
        attempt_elapsed = time.time() - stream_started_at
        effective_kind = timeout_kind or "stream"
        elapsed = (
            time.time() - scoped_started_at
            if effective_kind == "total"
            else attempt_elapsed
        )
        effective_limit = _timeout_limit(effective_kind, wait_timeout)
        _emit_llm_event(
            f"pipeline.llm_role_{effective_kind}_timeout",
            "error",
            f"{role_name}: LLM {effective_kind} timeout after {effective_limit:.1f}s",
            role=role_name,
            elapsed_sec=round(elapsed, 2),
            attempt_elapsed_sec=round(attempt_elapsed, 2),
            timeout_sec=round(effective_limit, 2),
            messages_seen=message_count,
            system_messages_seen=system_message_count,
            unknown_messages_seen=unknown_message_count,
            text_chars=text_chars,
            thinking_chars=thinking_chars,
            thinking_tokens_estimate=thinking_tokens_estimate,
            thinking_tokens_delta_total=thinking_tokens_delta_total,
            tool_use_count=tool_use_count,
            tool_result_count=tool_result_count,
            **timeout_policy,
            **_role_log_metadata(log_file_path),
        )
        raise LLMRoleTimeout(
            role_name,
            effective_kind,
            effective_limit,
            pending_stream_task=pending_stream_task,
        )

    try:
        watchdog_task = asyncio.create_task(_silence_watchdog())
        stream_iter = query_gen.__aiter__()
        while True:
            wait_timeout = None
            timeout_kind = None
            now = time.time()
            # B2: keep the (shorter) first_activity_timeout budget until we see
            # substantive model output, not just SDK/proxy bookkeeping
            # (SystemMessage init/thinking_tokens). This prevents a stalled
            # backend from degrading into the longer idle_timeout dead-wait.
            if not substantive_activity_logged and first_activity_timeout > 0:
                wait_timeout = max(
                    0.0,
                    first_activity_timeout - (now - stream_started_at),
                )
                timeout_kind = "first_activity"
            elif substantive_activity_logged:
                # B3: once we are inside the tool/think loop, a mid-loop stall
                # (tool_use emitted but tool_result never returns, or the model
                # stops streaming mid-think) should be caught at the shorter
                # stall_timeout rather than burning the full idle_timeout
                # before the role retry can restart. stall_timeout<=0 disables
                # this layer and falls back to idle_timeout.
                idle_budget = (idle_timeout - (now - last_message_at)) if idle_timeout > 0 else None
                stall_budget = (stall_timeout - (now - last_message_at)) if stall_timeout > 0 else None
                if stall_budget is not None and (idle_budget is None or stall_budget <= idle_budget):
                    wait_timeout = max(0.0, stall_budget)
                    timeout_kind = "stall"
                elif idle_budget is not None:
                    wait_timeout = max(0.0, idle_budget)
                    timeout_kind = "idle"
            if total_deadline is not None:
                remaining_total = max(0.0, total_deadline - now)
                if wait_timeout is None or remaining_total < wait_timeout:
                    wait_timeout = remaining_total
                    timeout_kind = "total"
            if wait_timeout is not None and wait_timeout <= 0:
                _raise_role_timeout(timeout_kind, wait_timeout)
            try:
                if wait_timeout is None:
                    message = await stream_iter.__anext__()
                else:
                    message = await _await_stream_next_bounded(
                        stream_iter,
                        max(0.001, wait_timeout),
                    )
            except StopAsyncIteration:
                break
            except LLMStreamNextTimeout as exc:
                _raise_role_timeout(
                    timeout_kind,
                    wait_timeout,
                    pending_stream_task=exc.pending_task,
                )
            message_count += 1
            productive_message = False
            if isinstance(message, AssistantMessage):
                productive_message = True
                if first_productive_at is None:
                    first_productive_at = time.time()
                assistant_message_count += 1
                _mark_first_activity("assistant")
                for block in message.content:
                    if isinstance(block, TextBlock):
                        text = block.text
                        if text and first_text_at is None:
                            first_text_at = time.time()
                        availability_trace.observe_text(text)
                        text_chars += len(text or "")
                        texts.append(text)
                        _append_role_io(log_file_path, text + "\n")
                        ui.log_io(text, "claude", role_name)
                    elif isinstance(block, ThinkingBlock):
                        thinking = block.thinking or "[thinking...]"
                        thinking_chars += len(thinking or "")
                        _append_role_io(log_file_path, f"\n[THINKING] {thinking[:2000]}\n")
                        ui.log_io(thinking, "thinking", role_name)
                    elif isinstance(block, ToolUseBlock):
                        tool_use_count += 1
                        args_str = json.dumps(block.input, ensure_ascii=False, indent=2)[:2000]
                        _append_role_io(log_file_path, f"\n[TOOL_CALL] {block.name}\n[ARGS] {args_str}\n")
                        ui.log_io(f"\n[tool: {block.name}]", "tool", role_name)
                        ui.emit_tool_call(block.name, block.input, role_name)
                        _record_llm_tool_trace_event({
                            "event": "tool_use",
                            "tool_use_id": str(getattr(block, "id", "") or ""),
                            "tool_name": str(block.name),
                            "tool_input": dict(block.input or {}),
                        })
                    elif isinstance(block, ToolResultBlock):
                        _record_tool_result(
                            block.content,
                            getattr(block, "is_error", None),
                            tool_use_id=getattr(block, "tool_use_id", None),
                        )
                _emit_progress()
            elif isinstance(message, UserMessage):
                productive_message = True
                _mark_first_activity("user")
                saw_tool_result_block = False
                if isinstance(message.content, list):
                    for block in message.content:
                        if isinstance(block, ToolResultBlock):
                            saw_tool_result_block = True
                            _record_tool_result(
                                block.content,
                                getattr(block, "is_error", None),
                                tool_use_id=getattr(block, "tool_use_id", None),
                            )
                tool_use_result = getattr(message, "tool_use_result", None)
                if tool_use_result is not None and not saw_tool_result_block:
                    _record_tool_result(
                        tool_use_result,
                        None,
                        source="UserMessage.tool_use_result",
                        tool_use_id=(
                            tool_use_result.get("tool_use_id")
                            if isinstance(tool_use_result, dict)
                            else None
                        ),
                    )
                _emit_progress()
            elif isinstance(message, SystemMessage):
                # SDK/proxy init and thinking-token telemetry can arrive tens
                # of times per second while the actual model/tool loop is
                # wedged.  It is observable bookkeeping, not progress: never
                # refresh ``last_message_at`` and never emit the progress event
                # consumed by the parent orchestrator's liveness extension.
                productive_message = False
                system_message_count += 1
                subtype = getattr(message, "subtype", None) or "unknown"
                data = getattr(message, "data", None)
                if not isinstance(data, dict):
                    data = {}
                if subtype == "thinking_tokens":
                    try:
                        estimate = int(data.get("estimated_tokens") or 0)
                    except (TypeError, ValueError):
                        estimate = 0
                    try:
                        delta = int(data.get("estimated_tokens_delta") or 0)
                    except (TypeError, ValueError):
                        delta = 0
                    thinking_tokens_estimate = max(
                        thinking_tokens_estimate,
                        estimate,
                    )
                    thinking_tokens_delta_total += max(0, delta)
                # B2: SystemMessages (init / thinking_tokens) are SDK/proxy
                # bookkeeping, not model output — do NOT let them satisfy the
                # substantive first-activity gate, otherwise a backend that
                # stalls right after init slips into the longer idle_timeout.
                _mark_first_activity(f"system:{subtype}", substantive=False)
                if _should_log_sparse_count(system_message_count):
                    _append_role_io(
                        log_file_path,
                        f"\n[SYSTEM_MESSAGE subtype={subtype} "
                        f"count={system_message_count} "
                        f"thinking_tokens={thinking_tokens_estimate} "
                        f"thinking_delta_total={thinking_tokens_delta_total}]\n",
                    )
            elif isinstance(message, ResultMessage):
                productive_message = True
                _mark_first_activity("result")
                availability_trace.observe_result(message)
                cost_usd = message.total_cost_usd
                usage = message.usage
                # Capture ALL ResultMessage diagnostic fields for metrics logging.
                # Previously only extracted on error path; now always captured
                # so stop_reason/num_turns/duration are available for success too.
                result_diag = {
                    "subtype": getattr(message, "subtype", None),
                    "is_error": bool(getattr(message, "is_error", False)),
                    "num_turns": getattr(message, "num_turns", None),
                    "stop_reason": getattr(message, "stop_reason", None),
                    "terminal_reason": getattr(message, "terminal_reason", None),
                    "duration_ms": getattr(message, "duration_ms", None),
                    "duration_api_ms": getattr(message, "duration_api_ms", None),
                    "session_id": getattr(message, "session_id", None),
                    "uuid": getattr(message, "uuid", None),
                    "result_text": getattr(message, "result", None),
                    "api_error_status": getattr(message, "api_error_status", None),
                    "errors": getattr(message, "errors", None),
                    "model_usage": None,
                }
                # model_usage may be a dict of ModelUsage dataclasses; convert.
                _mu = getattr(message, "model_usage", None)
                if isinstance(_mu, dict):
                    try:
                        result_diag["model_usage"] = {
                            k: (v if isinstance(v, dict) else
                                (v.model_dump() if hasattr(v, "model_dump") else
                                 dict(v) if hasattr(v, "__iter__") and not isinstance(v, str) else str(v)))
                            for k, v in _mu.items()
                        }
                    except Exception:
                        result_diag["model_usage"] = None
                billing_results = _LLM_BILLING_RESULTS.get()
                if isinstance(billing_results, list):
                    billing_results.append(message)
                strict_provider_capture = _STRICT_PROVIDER_RESULTS.get()
                if isinstance(strict_provider_capture, dict):
                    # The strict authority workflow consumes the SDK object in
                    # the parent process.  Logs/cost projections are not an
                    # execution authority and cannot synthesize this entry.
                    from strict_authority_workflow import _observe_provider_result

                    _observe_provider_result(
                        message,
                        invocation_id=str(
                            strict_provider_capture.get("invocation_id") or ""
                        ),
                        effect_id=str(
                            strict_provider_capture.get("effect_id") or ""
                        ),
                    )
                    strict_provider_capture.setdefault("results", []).append(message)
                _emit_progress()
                # A1 (v125 retry-storm fix): capture ResultMessage diagnostic fields.
                # Previously this branch read ONLY cost/usage, discarding subtype /
                # is_error / num_turns / stop_reason. That made every Master-failure
                # mode (missing-return / NO_FENCE / empty-output) collapse to the SAME
                # undifferentiated "malformed JSON" symptom downstream, which caused
                # multiple rounds of mis-attribution (v125 wasted several analysis
                # cycles before the real root cause was found). Log the diagnostics so
                # future failures are classifiable. Return signature is UNCHANGED (3-tuple)
                # — this is pure observation and must not alter retry/circuit behavior.
                try:
                    _subtype = getattr(message, "subtype", None)
                    _is_err = bool(getattr(message, "is_error", False))
                    if _is_err or (_subtype and _subtype != "success"):
                        _num_turns = getattr(message, "num_turns", None)
                        _stop_reason = getattr(message, "stop_reason", None)
                        _diag = {
                            "role": role_name,
                            "subtype": _subtype,
                            "is_error": _is_err,
                            "num_turns": _num_turns,
                            "stop_reason": _stop_reason,
                        }
                        _append_role_io(
                            log_file_path,
                            "\n[RESULT_DIAG] "
                            + json.dumps(_diag, ensure_ascii=False, default=str)
                            + "\n",
                        )
                        if ui:
                            ui.log_history(
                                f"{role_name}: ResultMessage non-success "
                                f"(subtype={_subtype}, is_error={_is_err}, "
                                f"num_turns={_num_turns}, stop_reason={_stop_reason})",
                                "warn",
                            )
                            try:
                                import event_bus
                                event_bus.warn(
                                    "pipeline.llm_result_non_success",
                                    f"{role_name} ResultMessage non-success (subtype={_subtype})",
                                    role=role_name,
                                    subtype=_subtype,
                                    is_error=_is_err,
                                    num_turns=_num_turns,
                                    stop_reason=_stop_reason,
                                )
                            except Exception:
                                pass
                except Exception:
                    pass
                availability_block = availability_trace.blocked(role=role_name)
                if availability_block is not None:
                    raise availability_block
            else:
                unknown_message_count += 1
                message_type = type(message).__name__
                message_module = type(message).__module__
                if _should_log_sparse_count(unknown_message_count):
                    _append_role_io(
                        log_file_path,
                        f"\n[UNKNOWN_SDK_MESSAGE] {message_module}.{message_type}: "
                        f"{repr(message)[:1000]}\n",
                    )
                    _emit_llm_event(
                        "pipeline.llm_role_unknown_message",
                        "warn",
                        f"{role_name}: unknown SDK stream message {message_type}",
                        role=role_name,
                        elapsed_sec=round(time.time() - stream_started_at, 2),
                        message_type=message_type,
                        message_module=message_module,
                        messages_seen=message_count,
                        system_messages_seen=system_message_count,
                        unknown_messages_seen=unknown_message_count,
                        text_chars=text_chars,
                        thinking_chars=thinking_chars,
                        thinking_tokens_estimate=thinking_tokens_estimate,
                        thinking_tokens_delta_total=thinking_tokens_delta_total,
                        tool_use_count=tool_use_count,
                        tool_result_count=tool_result_count,
                        **_role_log_metadata(log_file_path),
                    )
            if productive_message:
                last_message_at = time.time()
    except LLMAvailabilityBlocked as e:
        issue = e.issue
        _emit_llm_event(
            "pipeline.llm_role_availability_blocked", "error",
            f"{role_name}: LLM availability blocked ({issue.category})",
            role=role_name,
            elapsed_sec=round(time.time() - stream_started_at, 2),
            messages_seen=message_count,
            availability_category=issue.category,
            availability_issue=issue.as_dict(),
            **_role_log_metadata(log_file_path),
        )
        ui.log_io(f"[LLM UNAVAILABLE] {issue.summary}", "error", role_name)
        raise
    except ClaudeSDKError as e:
        # GLM 429 配额耗尽检测：与签名重试循环相同的检测逻辑。这覆盖那些
        # 绕过签名重试循环、直接在外层抛出的 429（例如 availability block
        # 路径，或 SDK 在建立流之前就拒绝的情况）。
        try:
            if _is_quota_exceeded(str(e)):
                from rate_limiter import rate_limiter
                rate_limiter.parse_429(str(e))
                _emit_llm_event(
                    "pipeline.llm_quota_exceeded_detected", "error",
                    (
                        f"{role_name}: GLM 429 quota exhaustion detected "
                        f"(outer handler); rate_limiter will block pipeline "
                        f"until reset"
                    ),
                    role=role_name,
                    elapsed_sec=round(time.time() - stream_started_at, 2),
                    messages_seen=message_count,
                    exception_type=type(e).__name__,
                    reset_time=(
                        rate_limiter.reset_time_str()
                        if rate_limiter.is_blocked() else None
                    ),
                    **_role_log_metadata(log_file_path),
                )
                if ui:
                    ui.log_history(
                        f"{role_name}: GLM API 配额耗尽 (429)。"
                        + (
                            f" 将暂停进化直到 {rate_limiter.reset_time_str()} 自动恢复。"
                            if rate_limiter.is_blocked()
                            else " 未检测到重置时间。"
                        ),
                        "error",
                    )
        except Exception:
            pass
        availability_block = availability_trace.blocked(
            role=role_name,
            exception=e,
        )
        if availability_block is not None:
            issue = availability_block.issue
            _emit_llm_event(
                "pipeline.llm_role_availability_blocked", "error",
                f"{role_name}: LLM availability blocked ({issue.category})",
                role=role_name,
                elapsed_sec=round(time.time() - stream_started_at, 2),
                messages_seen=message_count,
                exception_type=type(e).__name__,
                availability_category=issue.category,
                availability_issue=issue.as_dict(),
                **_role_log_metadata(log_file_path),
            )
            ui.log_io(f"[LLM UNAVAILABLE] {issue.summary}", "error", role_name)
            raise availability_block from e
        _emit_llm_event(
            "pipeline.llm_role_stream_sdk_error", "warn",
            f"{role_name}: SDK stream error: {str(e)[:180]}",
            role=role_name,
            elapsed_sec=round(time.time() - stream_started_at, 2),
            messages_seen=message_count,
            exception_type=type(e).__name__,
            error=str(e)[:500],
            **_role_log_metadata(log_file_path),
        )
        ui.log_io(f"[ERROR] {e}", "error", role_name)
        raise   # propagate so callers distinguish a hard SDK error from an empty-but-valid reply
    except LLMRoleTimeout:
        # This is our own role-policy deadline, not evidence that the provider
        # transport failed.  Preserve the existing typed timeout contract.
        raise
    except Exception as e:
        availability_block = availability_trace.blocked(
            role=role_name,
            exception=e,
        )
        if availability_block is not None:
            issue = availability_block.issue
            _emit_llm_event(
                "pipeline.llm_role_availability_blocked", "error",
                f"{role_name}: LLM availability blocked ({issue.category})",
                role=role_name,
                elapsed_sec=round(time.time() - stream_started_at, 2),
                messages_seen=message_count,
                exception_type=type(e).__name__,
                availability_category=issue.category,
                availability_issue=issue.as_dict(),
                **_role_log_metadata(log_file_path),
            )
            ui.log_io(f"[LLM UNAVAILABLE] {issue.summary}", "error", role_name)
            raise availability_block from e
        raise
    except asyncio.CancelledError:
        _category, _severity, _cancel_fields = _cancelled_event(
            "pipeline.llm_role_stream_cancelled",
            "pipeline.llm_role_stream_parent_timeout_cancelled",
        )
        _scope = _cancel_fields.get("cancel_scope")
        _timeout = _cancel_fields.get("timeout_sec")
        if _cancel_fields.get("cancel_reason") == "parent_timeout":
            _msg = (
                f"{role_name}: LLM stream cancelled by parent timeout"
                f" ({_scope}, {_timeout:g}s)"
                if isinstance(_timeout, (int, float))
                else f"{role_name}: LLM stream cancelled by parent timeout ({_scope})"
            )
        else:
            _msg = f"{role_name}: LLM stream cancelled"
        _emit_llm_event(
            _category, _severity,
            _msg,
            role=role_name,
            elapsed_sec=round(time.time() - stream_started_at, 2),
            messages_seen=message_count,
            **_cancel_fields,
            **_role_log_metadata(log_file_path),
        )
        ui.log_io(f"\n[{role_name} CANCELLED]", "error", role_name)
        raise
    finally:
        stream_done = True
        if 'watchdog_task' in locals():
            watchdog_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watchdog_task
    availability_block = availability_trace.blocked(role=role_name)
    if availability_block is not None:
        raise availability_block
    # Build timing metrics for call analytics (llm_call_metrics.jsonl).
    stream_end_at = time.time()
    stream_metrics = {
        "stream_elapsed_sec": round(stream_end_at - stream_started_at, 2),
        "first_token_latency_sec": (
            round(first_productive_at - stream_started_at, 2)
            if first_productive_at is not None else None
        ),
        "first_text_latency_sec": (
            round(first_text_at - stream_started_at, 2)
            if first_text_at is not None else None
        ),
        "thinking_tokens_estimated": thinking_tokens_estimate,
        "thinking_tokens_delta_total": thinking_tokens_delta_total,
        "text_block_count": len(texts),
        "thinking_chars": thinking_chars,
        "tool_use_count": tool_use_count,
        "tool_result_count": tool_result_count,
        "message_count": message_count,
        "assistant_message_count": assistant_message_count,
        "result_diag": result_diag,
    }
    return texts, cost_usd, usage, stream_metrics


# claude_agent_sdk 0.2.91 intermittently raises ClaudeSDKError "Missing required
# field in assistant message: 'signature'" mid-stream. It is transient (a fresh
# query usually succeeds) but frequent enough that 3 retries occasionally exhaust,
# stalling Master/analyst. Bumped to 5 with slightly longer backoff so a brief
# SDK-side storm still resolves without surfacing a failure to the caller.
_SIGNATURE_MAX_ATTEMPTS = 5


def _merge_billing_usage(total, usage):
    if not isinstance(usage, dict):
        return total
    merged = dict(total or {})
    for key, value in usage.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            previous = merged.get(key, 0)
            if not isinstance(previous, (int, float)) or isinstance(previous, bool):
                previous = 0
            merged[key] = previous + value
        elif key not in merged:
            # Keep non-numeric metadata from the first result. It is not summed,
            # but callers do not lose fields such as service tier/model detail.
            merged[key] = value
    return merged


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
    """Record each SDK Result exactly once and return newly billed totals."""

    from orchestrator_cost_policy import (
        assert_operator_cost_limit_available,
        current_generation_cost_scope,
        record_generation_cost,
        sdk_result_event_id,
    )

    results = list(billing_results or [])
    if not results and (fallback_cost is not None or fallback_usage is not None):
        results = [None]
    billed_cost = 0.0
    billed_usage = None
    for result_index, result in enumerate(results):
        if result is None:
            cost_usd = fallback_cost
            usage = fallback_usage
            event_id = (
                f"llm-result-fallback:{billing_call_id}:"
                f"{int(attempt)}:{int(result_index)}"
            )
        else:
            cost_usd = getattr(result, "total_cost_usd", None)
            usage = getattr(result, "usage", None)
            event_id = sdk_result_event_id(
                result,
                source="llm_query",
                attempt=attempt,
            )
        status = record_generation_cost(
            role_name,
            cost_usd,
            usage,
            source="llm_query_attempt",
            event_id=event_id,
        )
        accepted = bool(
            not status.get("active")
            or status.get("recorded")
            or status.get("pending_only")
        )
        if accepted:
            if cost_usd is not None:
                billed_cost += float(cost_usd)
            billed_usage = _merge_billing_usage(billed_usage, usage)
            if ui:
                ui.update_cost(role_name, float(cost_usd or 0.0), usage)
        elif ui and status.get("active") and not status.get("accounting_ok"):
            # A pending write-ahead entry is already included in durable status;
            # refresh the projection without incrementing a replay twice.
            scope = current_generation_cost_scope()
            begin_cost = getattr(ui, "begin_generation_cost", None)
            if scope is not None and callable(begin_cost):
                begin_cost(
                    scope.generation_id,
                    status.get("spent_usd", 0.0),
                    scope.receipt(
                        spent_before_usd=float(status.get("spent_usd") or 0.0),
                        ledger_errors=tuple(status.get("accounting_errors") or ()),
                    ),
                )
        assert_operator_cost_limit_available()
    return billed_cost, billed_usage


def _raise_signature_retry_total_timeout(role_name, log_file_path):
    scope = _LLM_TOTAL_DEADLINE.get()
    timeout_sec = float((scope or {}).get("timeout_sec") or 0)
    started_at = float((scope or {}).get("started_at") or time.time())
    elapsed = max(0.0, time.time() - started_at)
    _emit_llm_event(
        "pipeline.llm_role_total_timeout",
        "error",
        f"{role_name}: LLM total timeout after {timeout_sec:.1f}s",
        role=role_name,
        elapsed_sec=round(elapsed, 2),
        timeout_sec=round(timeout_sec, 2),
        retry_phase="sdk_signature_backoff",
        **_role_log_metadata(log_file_path),
    )
    raise LLMRoleTimeout(role_name, "total", timeout_sec)


async def _signature_retry_sleep(delay, role_name, log_file_path):
    """Sleep without letting retry backoff cross the call-wide total deadline."""

    scope = _LLM_TOTAL_DEADLINE.get()
    deadline = (scope or {}).get("deadline") if isinstance(scope, dict) else None
    if deadline is None:
        await asyncio.sleep(delay)
        return
    remaining = float(deadline) - time.time()
    if remaining <= 0:
        _raise_signature_retry_total_timeout(role_name, log_file_path)
    if float(delay) >= remaining:
        await asyncio.sleep(max(0.0, remaining))
        _raise_signature_retry_total_timeout(role_name, log_file_path)
    await asyncio.sleep(delay)


def _consume_task_result(task):
    with contextlib.suppress(BaseException):
        task.result()


async def _bounded_aclose(query_gen, role_name, log_file_path):
    """Close one SDK generator or raise a typed infrastructure failure.

    A completed close task is not automatically success: async generators raise
    ``RuntimeError`` when ``aclose()`` races an active ``__anext__``.  Suppressing
    that exception previously reported cleanup success while the CLI subprocess
    and billed provider request could continue in the background.
    """

    try:
        close_timeout = max(
            0.1,
            min(30.0, float(os.environ.get("POK_LLM_ACLOSE_TIMEOUT", "15"))),
        )
    except (TypeError, ValueError):
        close_timeout = 15.0
    close_task = asyncio.create_task(query_gen.aclose())
    done, _pending = await asyncio.wait({close_task}, timeout=close_timeout)
    if close_task in done:
        try:
            close_task.result()
        except BaseException as exc:
            raise LLMProviderCleanupError(
                "SDK stream aclose failed: "
                f"{type(exc).__name__}: {str(exc)[:300]}"
            ) from exc
        return True
    close_task.cancel()
    done, _pending = await asyncio.wait({close_task}, timeout=1.0)
    pending_task = close_task if close_task not in done else None
    if pending_task is not None:
        _track_pending_stream_task(
            pending_task,
            "stream_aclose_cancellation_unconfirmed",
        )
    else:
        _consume_task_result(close_task)
    _emit_llm_event(
        "pipeline.llm_role_stream_close_timeout",
        "error",
        f"{role_name}: SDK stream cleanup exceeded {close_timeout:.1f}s",
        role=role_name,
        timeout_sec=round(close_timeout, 2),
        **_role_log_metadata(log_file_path),
    )
    raise LLMProviderCleanupError(
        f"SDK stream aclose exceeded {close_timeout:.1f}s",
        provider_exit_confirmed=False,
    )


def _new_owned_sdk_transport(full_prompt, options):
    """Create the exact SDK subprocess transport owned by one query attempt."""

    try:
        from claude_agent_sdk._internal.transport.subprocess_cli import (
            SubprocessCLITransport,
        )
    except Exception as exc:
        raise LLMProviderCleanupError(
            "SDK owned subprocess transport is unavailable: "
            f"{type(exc).__name__}: {str(exc)[:300]}"
        ) from exc
    return SubprocessCLITransport(prompt=full_prompt, options=options)


def create_owned_provider_attempt(full_prompt, options):
    """Create one dispatch-ready provider attempt with exact transport ownership."""

    _assert_no_unresolved_provider_attempts()
    return _new_provider_attempt(_new_owned_sdk_transport(full_prompt, options))


def owned_provider_attempt_transport(attempt):
    """Return only the transport bound to ``attempt``."""

    if not isinstance(attempt, dict) or attempt.get("transport") is None:
        raise LLMProviderCleanupError("owned provider attempt has no transport")
    return attempt["transport"]


@contextlib.contextmanager
def owned_provider_attempt_scope(attempt):
    """Bind pending SDK tasks to one provider attempt for this async context."""

    token = activate_owned_provider_attempt(attempt)
    try:
        yield attempt
    finally:
        reset_owned_provider_attempt(token)


def activate_owned_provider_attempt(attempt):
    """Activate one attempt and return the exact ContextVar reset token."""

    if not isinstance(attempt, dict):
        raise TypeError("owned provider attempt must be a mapping")
    return _LLM_PROVIDER_ATTEMPT.set(attempt)


def reset_owned_provider_attempt(token):
    """Reset a token produced by :func:`activate_owned_provider_attempt`."""

    _LLM_PROVIDER_ATTEMPT.reset(token)


def mark_owned_provider_attempt_unresolved(attempt, reason, task=None):
    """Mark an anomalous attempt and optionally retain its unfinished task."""

    tasks = (task,) if isinstance(task, asyncio.Task) else ()
    _register_unresolved_provider_attempt(attempt, reason, *tasks)


def owned_provider_attempt_exit_confirmed(attempt):
    """Return true only after this attempt's exact process and tasks exited."""

    return _resolve_provider_attempt_if_stopped(attempt)


def _refresh_transport_exit_confirmation(attempt):
    if not isinstance(attempt, dict) or not attempt.get("transport_close_attempted"):
        return False
    owned_process = _capture_owned_provider_process(attempt)
    transport_process = getattr(attempt.get("transport"), "_process", None)
    owned_stopped = (
        owned_process is None
        or getattr(owned_process, "returncode", None) is not None
    )
    transport_stopped = (
        transport_process is None
        or getattr(transport_process, "returncode", None) is not None
    )
    if owned_stopped and transport_stopped:
        attempt["transport_close_confirmed"] = True
    return bool(attempt.get("transport_close_confirmed"))


async def _bounded_owned_transport_close(attempt, role_name, log_file_path):
    """Close only this attempt's SDK-owned transport and prove process exit."""

    transport = attempt.get("transport") if isinstance(attempt, dict) else None
    if transport is None or not callable(getattr(transport, "close", None)):
        raise LLMProviderCleanupError(
            "SDK provider transport has no owned close API",
            attempt_id=(attempt or {}).get("attempt_id"),
        )
    _capture_owned_provider_process(attempt)
    attempt["transport_close_attempted"] = True
    try:
        close_timeout = max(
            0.1,
            min(
                30.0,
                float(os.environ.get("POK_LLM_TRANSPORT_CLOSE_TIMEOUT", "15")),
            ),
        )
    except (TypeError, ValueError):
        close_timeout = 15.0
    close_task = asyncio.create_task(transport.close())
    done, _pending = await asyncio.wait({close_task}, timeout=close_timeout)
    if close_task not in done:
        close_task.cancel()
        done, _pending = await asyncio.wait({close_task}, timeout=1.0)
    if close_task not in done:
        _register_unresolved_provider_attempt(
            attempt,
            "owned_transport_close_cancellation_unconfirmed",
            close_task,
        )
        raise LLMProviderCleanupError(
            f"owned SDK transport close exceeded {close_timeout:.1f}s",
            provider_exit_confirmed=False,
            attempt_id=attempt.get("attempt_id"),
        )
    try:
        close_task.result()
    except BaseException as exc:
        _register_unresolved_provider_attempt(
            attempt,
            f"owned_transport_close_failed:{type(exc).__name__}",
        )
        _refresh_transport_exit_confirmation(attempt)
        raise LLMProviderCleanupError(
            "owned SDK transport close failed: "
            f"{type(exc).__name__}: {str(exc)[:300]}",
            provider_exit_confirmed=_provider_attempt_exit_confirmed(attempt),
            attempt_id=attempt.get("attempt_id"),
        ) from exc
    _refresh_transport_exit_confirmation(attempt)
    if not attempt.get("transport_close_confirmed"):
        _register_unresolved_provider_attempt(
            attempt,
            "owned_transport_process_exit_unconfirmed",
        )
        raise LLMProviderCleanupError(
            "owned SDK transport returned from close without process-exit proof",
            provider_exit_confirmed=False,
            attempt_id=attempt.get("attempt_id"),
        )
    return True


async def _await_provider_attempt_tasks(attempt):
    tasks = {
        task
        for task in (attempt.get("pending_tasks") or ())
        if isinstance(task, asyncio.Task) and not task.done()
    }
    if not tasks:
        return True
    try:
        timeout = max(
            0.1,
            min(
                10.0,
                float(os.environ.get("POK_LLM_STREAM_TASK_EXIT_TIMEOUT", "5")),
            ),
        )
    except (TypeError, ValueError):
        timeout = 5.0
    done, pending = await asyncio.wait(tasks, timeout=timeout)
    for task in done:
        _consume_task_result(task)
    if pending:
        _register_unresolved_provider_attempt(
            attempt,
            "stream_tasks_remain_after_transport_close",
            *pending,
        )
        return False
    return True


async def _perform_owned_provider_attempt_cleanup(
    query_gen,
    attempt,
    role_name,
    log_file_path,
):
    """Close a query and fail explicitly if exceptional cleanup was required."""

    # Preserve the exact process object before ``aclose`` can clear the
    # transport's pointer.  Exceptional cleanup must prove that this same
    # child exited; a later ``None`` transport pointer is not, by itself,
    # process-exit evidence.
    _capture_owned_provider_process(attempt)
    exceptional = bool(attempt.get("cleanup_reasons"))
    cleanup_errors = []
    if exceptional:
        try:
            await _bounded_owned_transport_close(
                attempt,
                role_name,
                log_file_path,
            )
        except LLMProviderCleanupError as exc:
            cleanup_errors.append(str(exc))
        if not await _await_provider_attempt_tasks(attempt):
            cleanup_errors.append("SDK stream task exit remains unconfirmed")
        if not any(
            isinstance(task, asyncio.Task) and not task.done()
            for task in attempt.get("pending_tasks") or ()
        ):
            try:
                await _bounded_aclose(query_gen, role_name, log_file_path)
            except LLMProviderCleanupError as exc:
                cleanup_errors.append(str(exc))
    else:
        try:
            await _bounded_aclose(query_gen, role_name, log_file_path)
        except LLMProviderCleanupError as exc:
            exceptional = True
            cleanup_errors.append(str(exc))
            _register_unresolved_provider_attempt(
                attempt,
                "stream_aclose_failed",
            )
            try:
                await _bounded_owned_transport_close(
                    attempt,
                    role_name,
                    log_file_path,
                )
            except LLMProviderCleanupError as transport_exc:
                cleanup_errors.append(str(transport_exc))
            await _await_provider_attempt_tasks(attempt)
        else:
            # Another owner (for example the cycle-level timeout boundary) may
            # mark the attempt while this normal ``aclose`` is in flight.  A
            # successful generator close then counts as the transport close,
            # but only the captured original process can prove termination.
            if attempt.get("cleanup_reasons"):
                exceptional = True
                attempt["transport_close_attempted"] = True
                _refresh_transport_exit_confirmation(attempt)

    if not exceptional:
        return True
    _refresh_transport_exit_confirmation(attempt)
    confirmed = _resolve_provider_attempt_if_stopped(attempt)
    cleanup_reasons = set(attempt.get("cleanup_reasons") or ())
    pending_tasks = [
        task
        for task in attempt.get("pending_tasks") or ()
        if isinstance(task, asyncio.Task) and not task.done()
    ]
    # A parent cancellation is not a provider failure once cleanup has proven
    # that the exact child and every owned SDK task exited.  Preserve the
    # original CancelledError so the existing shutdown/control path can classify
    # it as a clean stop.  Every timeout reason, mixed reason, cleanup error, or
    # unconfirmed exit remains fail-closed below.
    if (
        cleanup_reasons
        == {"stream_next_parent_cancellation_unconfirmed"}
        and confirmed
        and not pending_tasks
        and not cleanup_errors
    ):
        _emit_llm_event(
            "pipeline.llm_role_provider_cleanup_completed_after_parent_cancel",
            "info",
            f"{role_name}: provider cleanup completed after parent cancellation",
            role=role_name,
            attempt_id=attempt.get("attempt_id"),
            provider_exit_confirmed=True,
            cleanup_reasons=sorted(cleanup_reasons),
            **_role_log_metadata(log_file_path),
        )
        return True
    message = (
        "SDK provider stream required exceptional cleanup; "
        f"process_exit_confirmed={confirmed}; reasons="
        + "|".join(attempt.get("cleanup_reasons") or [])
    )
    if cleanup_errors:
        message += "; errors=" + "; ".join(cleanup_errors[:4])
    _emit_llm_event(
        "pipeline.llm_role_provider_cleanup_failure",
        "error",
        f"{role_name}: {message}",
        role=role_name,
        attempt_id=attempt.get("attempt_id"),
        provider_exit_confirmed=confirmed,
        cleanup_reasons=list(attempt.get("cleanup_reasons") or []),
        cleanup_errors=cleanup_errors[:4],
        **_role_log_metadata(log_file_path),
    )
    raise LLMProviderCleanupError(
        message,
        provider_exit_confirmed=confirmed,
        attempt_id=attempt.get("attempt_id"),
    )


async def _cleanup_owned_provider_attempt(
    query_gen,
    attempt,
    role_name,
    log_file_path,
):
    """Run the exact attempt cleanup once, even with multiple timeout owners."""

    if not isinstance(attempt, dict):
        raise LLMProviderCleanupError("invalid owned provider cleanup attempt")
    cleanup_task = attempt.get("cleanup_task")
    if not isinstance(cleanup_task, asyncio.Task):
        cleanup_task = asyncio.create_task(
            _perform_owned_provider_attempt_cleanup(
                query_gen,
                attempt,
                role_name,
                log_file_path,
            )
        )
        attempt["cleanup_task"] = cleanup_task
        cleanup_task.add_done_callback(_consume_task_result)
    return await asyncio.shield(cleanup_task)


async def cleanup_owned_provider_attempt(
    query_gen,
    attempt,
    role_name,
    log_file_path,
):
    """Public idempotent cleanup boundary for an owned provider attempt."""

    return await _cleanup_owned_provider_attempt(
        query_gen,
        attempt,
        role_name,
        log_file_path,
    )


async def _run_stream_with_signature_retry(
    full_prompt, options, log_file_path, ui, role_name
):
    """Run bounded SDK retries under one role-wide total wall-clock budget."""

    policy = _role_timeout_policy(role_name)
    total_timeout = float(policy.get("total_timeout") or 0)
    started_at = time.time()
    token = _LLM_TOTAL_DEADLINE.set({
        "started_at": started_at,
        "deadline": (
            started_at + total_timeout if total_timeout > 0 else None
        ),
        "timeout_sec": total_timeout,
    })
    try:
        return await _run_stream_with_signature_retry_attempts(
            full_prompt, options, log_file_path, ui, role_name
        )
    finally:
        _LLM_TOTAL_DEADLINE.reset(token)


async def _run_stream_with_signature_retry_attempts(
    full_prompt, options, log_file_path, ui, role_name
):
    """Run one streaming query with retries on transient SDK signature errors.

    Extracted so the 529/429 retry paths reuse the same handling as the initial query.
    Returns (texts_list, cost_usd, usage).
    """
    last_sdk_err = None
    total_cost = 0.0
    total_usage = None
    billing_call_id = uuid.uuid4().hex
    _assert_no_unresolved_provider_attempts()
    for sdk_attempt in range(_SIGNATURE_MAX_ATTEMPTS):
        _assert_no_unresolved_provider_attempts()
        owned_transport = _new_owned_sdk_transport(full_prompt, options)
        provider_attempt = _new_provider_attempt(owned_transport)
        provider_token = _LLM_PROVIDER_ATTEMPT.set(provider_attempt)
        try:
            query_gen = claude_query(
                prompt=full_prompt,
                options=options,
                transport=owned_transport,
            )
        except BaseException:
            _LLM_PROVIDER_ATTEMPT.reset(provider_token)
            raise
        billing_results = []
        billing_token = _LLM_BILLING_RESULTS.set(billing_results)
        _attempt_start = time.time()
        try:
            texts, cost_usd, usage, stream_metrics = await _process_stream(
                query_gen, log_file_path, ui, role_name
            )
            attempt_cost, attempt_usage = _record_completed_billing_attempt(
                role_name=role_name,
                ui=ui,
                billing_results=billing_results,
                fallback_cost=cost_usd,
                fallback_usage=usage,
                attempt=sdk_attempt,
                billing_call_id=billing_call_id,
            )
            # Record per-attempt call metrics for offline timing/token analysis.
            try:
                from llm_call_metrics import record_llm_call_metrics
                _um = _usage_metadata(usage) if usage else {}
                _rd = stream_metrics.get("result_diag") or {}
                record_llm_call_metrics(
                    call_id=billing_call_id,
                    attempt=sdk_attempt,
                    max_attempts=_SIGNATURE_MAX_ATTEMPTS,
                    role=role_name,
                    model=getattr(options, "model", None),
                    total_elapsed_sec=time.time() - _attempt_start,
                    first_token_latency_sec=stream_metrics.get("first_token_latency_sec"),
                    first_text_latency_sec=stream_metrics.get("first_text_latency_sec"),
                    stream_active_sec=stream_metrics.get("stream_elapsed_sec"),
                    input_tokens=_um.get("input_tokens"),
                    output_tokens=_um.get("output_tokens"),
                    cache_creation_input_tokens=_um.get("cache_creation_input_tokens"),
                    cache_read_input_tokens=_um.get("cache_read_input_tokens"),
                    thinking_tokens_estimated=stream_metrics.get("thinking_tokens_estimated"),
                    thinking_tokens_delta_total=stream_metrics.get("thinking_tokens_delta_total"),
                    cost_usd=attempt_cost,
                    success=True,
                    sdk_subtype=_rd.get("subtype"),
                    stop_reason=_rd.get("stop_reason"),
                    num_turns=_rd.get("num_turns"),
                    terminal_reason=_rd.get("terminal_reason"),
                    sdk_duration_ms=_rd.get("duration_ms"),
                    sdk_duration_api_ms=_rd.get("duration_api_ms"),
                    sdk_session_id=_rd.get("session_id"),
                    sdk_uuid=_rd.get("uuid"),
                    sdk_result_text=_rd.get("result_text"),
                    model_usage=_rd.get("model_usage"),
                    raw_usage=(usage if isinstance(usage, dict) else
                               (usage.model_dump() if usage and hasattr(usage, "model_dump") else None)),
                    api_error_status=_rd.get("api_error_status"),
                    text_block_count=stream_metrics.get("text_block_count"),
                    thinking_chars=stream_metrics.get("thinking_chars"),
                    tool_use_count=stream_metrics.get("tool_use_count"),
                    tool_result_count=stream_metrics.get("tool_result_count"),
                    message_count=stream_metrics.get("message_count"),
                    assistant_message_count=stream_metrics.get("assistant_message_count"),
                    log_file=_role_log_basename(log_file_path),
                )
            except Exception:
                pass
            total_cost += attempt_cost
            total_usage = _merge_billing_usage(total_usage, attempt_usage)
            if sdk_attempt > 0 and ui:
                ui.log_history(
                    f"{role_name}: SDK stream recovered after {sdk_attempt} signature retry/retries",
                    "info",
                )
            # Empty-output retry (root-cause fix for Master JSON collapse, 2026-06-19).
            # claude_agent_sdk 0.2.91's signature bug has TWO failure modes:
            #   (a) raises ClaudeSDKError mid-stream — caught above, retried.
            #   (b) stream "succeeds" with a ResultMessage (cost/usage present) but ZERO
            #       TextBlocks → _process_stream returns ([], cost, usage) WITHOUT raising.
            # Mode (b) escaped ALL retry layers (only ClaudeSDKError was caught), so the
            # empty output reached the caller, parse_json_output('') returned None, and
            # the agent logged "malformed JSON" → 3x retry exhaust → abandon_generation.
            # Measured impact: 140/540 (26%) of MASTER [COST] lines were in=0 out=0, and
            # 713 "Missing required field ... signature" errors appeared app-wide — this
            # is the true root cause of the v107-110/v116/v121/v125 "Master JSON collapse"
            # (previously mis-attributed to direction-audit constraints; that is only a
            # minor secondary factor for the real-output-but-rejected subset).
            # Fix: treat 0-TextBlock output as a signature-truncation variant and retry it
            # on the same backoff schedule. `continue` here runs the finally (aclose) then
            # the for-loop's next attempt. Retries exhausted → fall through to return
            # (caller sees empty output and handles it, same as today, but now rare).
            # Condition covers BOTH empty-output variants: 0 TextBlocks (texts=[]) AND
            # empty-string TextBlocks (texts=[""] — also out=0, another face of the SDK
            # signature-truncation bug where a TextBlock carries empty text). The plain
            # `not texts` check missed the texts=[""] case ([""] is truthy). `not any
            # (... .strip())` is True iff every text is empty/whitespace, catching both.
            if not any((t or "").strip() for t in texts) and sdk_attempt < _SIGNATURE_MAX_ATTEMPTS - 1:
                _backoff = min(5 * (2 ** sdk_attempt), 30)
                if ui:
                    ui.log_history(
                        f"{role_name}: SDK stream returned 0 TextBlocks (cost={cost_usd}) — "
                        f"signature-truncation variant, retrying in {_backoff}s "
                        f"(attempt {sdk_attempt+1}/{_SIGNATURE_MAX_ATTEMPTS})",
                        "warn",
                    )
                    try:
                        import event_bus
                        event_bus.warn(
                            "pipeline.llm_empty_output_retry",
                            f"{role_name} SDK stream returned 0 TextBlocks (signature-truncation variant)",
                            role=role_name, cost=cost_usd,
                            attempt=sdk_attempt + 1, max_attempts=_SIGNATURE_MAX_ATTEMPTS,
                        )
                    except Exception:
                        pass
                await _signature_retry_sleep(
                    _backoff, role_name, log_file_path
                )
                continue
            # A completed, non-error ResultMessage is success regardless of its
            # prose. Provider failures are raised by _process_stream before this
            # point; re-scanning model text would misread ordinary discussion of
            # quotas/overload as transport evidence.
            try:
                from api_concurrency import record_llm_outcome
                record_llm_outcome(success=True)
            except Exception:
                pass
            return texts, total_cost, total_usage
        except ClaudeSDKError as e:
            last_sdk_err = e
            err_str = str(e).lower()
            if ("signature" in err_str or "missing required field" in err_str) and \
                    sdk_attempt < _SIGNATURE_MAX_ATTEMPTS - 1:
                # Exponential-ish backoff: 5, 10, 20, 30s — short enough to not stall
                # the pipeline, long enough for a transient SDK state to clear.
                _backoff = min(5 * (2 ** sdk_attempt), 30)
                if ui:
                    ui.log_history(
                        f"{role_name}: SDK stream error (attempt {sdk_attempt+1}/{_SIGNATURE_MAX_ATTEMPTS}), "
                        f"retrying in {_backoff}s: {e}",
                        "warn",
                    )
                _emit_llm_event(
                    "pipeline.llm_role_signature_retry", "warn",
                    f"{role_name}: SDK signature stream error, retrying in {_backoff}s",
                    role=role_name,
                    sdk_attempt=sdk_attempt + 1,
                    max_attempts=_SIGNATURE_MAX_ATTEMPTS,
                    backoff_sec=_backoff,
                    exception_type=type(e).__name__,
                    error=str(e)[:500],
                    **_role_log_metadata(log_file_path),
                )
                await _signature_retry_sleep(
                    _backoff, role_name, log_file_path
                )
                continue
            # 自适应并发:非 signature 的 SDK error(可能含 503 熔断/overloaded/429)上报降并发
            try:
                _es = str(e).lower()
                if ("503" in _es or "overloaded" in _es or "熔断" in _es
                        or "所有供应商" in _es or "rate limit" in _es or "429" in _es):
                    from api_concurrency import record_llm_outcome
                    record_llm_outcome(success=False, rate_limited=True)
            except Exception:
                pass
            # GLM 429 配额耗尽检测：解析重置时间戳到全局 rate_limiter。
            # rate_limiter.parse_429 只在 GLM 返回明确的 "限额将在 ... 重置"
            # 时间戳时设置阻塞；无重置证据的裸 429 返回 False，不阻塞（保持
            # 现有有限重试行为）。一旦 rate_limiter 被设置，orchestrator_loop
            # 的 is_blocked() 检查会暂停整个 pipeline 直到恢复窗口，所有后续
            # run_claude_query 入口也会等待。这是 "等待恢复窗口" 语义的核心。
            try:
                if _is_quota_exceeded(str(e)):
                    from rate_limiter import rate_limiter
                    rate_limiter.parse_429(str(e))
                    _emit_llm_event(
                        "pipeline.llm_quota_exceeded_detected", "error",
                        (
                            f"{role_name}: GLM 429 quota exhaustion detected; "
                            f"rate_limiter will block pipeline until reset"
                        ),
                        role=role_name,
                        sdk_attempt=sdk_attempt + 1,
                        max_attempts=_SIGNATURE_MAX_ATTEMPTS,
                        reset_time=(
                            rate_limiter.reset_time_str()
                            if rate_limiter.is_blocked() else None
                        ),
                        **_role_log_metadata(log_file_path),
                    )
                    if ui:
                        ui.log_history(
                            f"{role_name}: GLM API 配额耗尽 (429)。"
                            + (
                                f" 将暂停进化直到 {rate_limiter.reset_time_str()} 自动恢复。"
                                if rate_limiter.is_blocked()
                                else " 未检测到重置时间，将继续有限重试。"
                            ),
                            "error",
                        )
            except Exception:
                pass
            raise  # non-signature SDK error, or signature retries exhausted
        finally:
            _LLM_BILLING_RESULTS.reset(billing_token)
            try:
                # The transport is unique to this attempt. If generator cleanup
                # races a cancellation-resistant ``__anext__``, terminate only
                # that owned transport and prove process/task exit before retry.
                await _cleanup_owned_provider_attempt(
                    query_gen,
                    provider_attempt,
                    role_name,
                    log_file_path,
                )
            finally:
                _LLM_PROVIDER_ATTEMPT.reset(provider_token)
    if last_sdk_err is not None:
        raise last_sdk_err


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
            from llm_concurrency import get_global_llm_semaphore

            _global_llm_sem = get_global_llm_semaphore()
            _sem_wait_start = time.time()
            async with _global_llm_sem:
                lifecycle_fields["semaphore_wait_sec"] = round(time.time() - _sem_wait_start, 3)
                full_text, cost_usd, usage = await _run_stream_with_signature_retry(
                    full_prompt, options, log_file_path, ui, role_name)

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
    # Strategy 1: Find ALL ```json blocks, try from LAST to first.
    # Handles the case where the LLM references the prompt template before the actual plan.
    json_starts = list(re.finditer(r'```json\s*', output))
    for json_start in reversed(json_starts):
        after_start = output[json_start.end():]
        # Find all ``` positions after ```json
        close_positions = [m.start() for m in re.finditer(r'```', after_start)]
        # Try from the LAST ``` backward (most likely the actual closing)
        for pos in reversed(close_positions):
            candidate = after_start[:pos].strip()
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
        # Also try the full text after ```json (in case no closing ```)
        try:
            return json.loads(after_start.strip().rstrip('`').strip())
        except json.JSONDecodeError:
            pass

    # Strategy 1.5: Brace-matching from each ```json start.
    # Handles embedded ``` inside JSON string values (e.g., worker_prompt with code blocks).
    # Tracks string boundaries so ``` inside strings are ignored.
    for json_start in reversed(json_starts):
        after_start = output[json_start.end():]
        brace_pos = after_start.find('{')
        if brace_pos == -1:
            continue
        depth = 0
        in_string = False
        escape_next = False
        for i in range(brace_pos, len(after_start)):
            c = after_start[i]
            if escape_next:
                escape_next = False
                continue
            if c == '\\' and in_string:
                escape_next = True
                continue
            if c == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    candidate = after_start[brace_pos:i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        break  # brace match failed, try next ```json block

    # Strategy 2: Try the whole output as raw JSON
    try:
        return json.loads(output)
    except Exception:
        pass
    return None


def parse_json_output_with_mode(output):
    """Same parsing as parse_json_output, but returns a classifiable failure mode.

    Returns ``(data, failure_mode)`` where ``failure_mode`` is one of:
      - ``"OK"``          — parsed successfully (data is the dict)
      - ``"NO_JSON"``     — output empty/whitespace (no text to parse at all)
      - ``"NO_FENCE"``    — output has text but no JSON structure (no ```json
                            block and no ``{``); the model never emitted JSON
      - ``"PARSE_ERROR"`` — output looked like JSON (had a fence or brace) but
                            every parse strategy failed

    The mode lets callers (notably _run_master_analysis) log a CLASSIFIABLE
    reason instead of the undifferentiated "malformed JSON" that previously
    hid three distinct root causes (missing-return / NO_FENCE / empty-output).
    """
    if not output or not output.strip():
        return None, "NO_JSON"
    data = parse_json_output(output)
    if data is not None:
        return data, "OK"
    # parse_json_output exhausted every strategy — distinguish why.
    has_fence = "```json" in output
    has_brace = "{" in output
    if has_fence or has_brace:
        return None, "PARSE_ERROR"
    return None, "NO_FENCE"
