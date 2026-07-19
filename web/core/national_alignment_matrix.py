"""Executable cross-layer contract for ``national_tcp_policy_v1``.

The long-form alignment document is useful to operators, but it must not be the
only place where a rule's ownership and proof are recorded.  This module is the
machine-readable current view.  It deliberately validates *references*, not
runtime success: rows such as v143/v144 and the ten-generation observation are
current obligations whose ``runtime_pending`` state must not be rendered as a
completed production claim.

This module is stdlib-only and has no runtime side effects.  It is intentionally
not imported by the evolution daemon; the accompanying regression test is the
quality gate that makes drift fail before merge.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Iterable, Sequence


MATRIX_SCHEMA_VERSION = 14
CURRENT_STATUS = "current"
SUPERSEDED_STATUS = "superseded"
SOURCE_CONTRACT = "source_contract"
RUNTIME_PENDING = "runtime_pending"
HISTORICAL = "historical"


@dataclass(frozen=True)
class SourceRef:
    """A repository-relative source file with an optional required symbol/text."""

    path: str
    symbol: str = ""

    def display(self) -> str:
        return f"{self.path}::{self.symbol}" if self.symbol else self.path


@dataclass(frozen=True)
class PromptBinding:
    """The authoritative renderer registry entry and its checked-in templates."""

    role: str
    renderer: SourceRef
    templates: tuple[SourceRef, ...]


@dataclass(frozen=True)
class MatrixRow:
    """One causal, testable rule spanning the active national architecture."""

    rule_id: str
    coverage: tuple[str, ...]
    status: str
    evidence_state: str
    authority: tuple[SourceRef, ...]
    production_owners: tuple[SourceRef, ...]
    dynamic_gates: tuple[SourceRef, ...]
    prompts: tuple[PromptBinding, ...]
    producer_consumer: str
    positive_tests: tuple[str, ...]
    negative_tests: tuple[str, ...]
    fail_closed: str
    historical_reason: str = ""
    prompt_statement: str = ""
    # Exact terms that the source-owned overlay plus each role's rendered
    # template must expose for this rule.  The prose statement remains human
    # readable; this field prevents it from becoming unconsumed audit text.
    prompt_required_terms: tuple[str, ...] = ()


ROOT = Path(__file__).resolve().parents[2]
REQUIRED_COVERAGE = frozenset({
    "raw_tcp_delimiter_stream",
    "raw_tcp_name_handshake",
    "official_called_allin_runout",
    "raise_terminal_hand70",
    "strict_abi_context_fallback_deadline",
    "system_asset_boundary",
    "strict_connection_memory",
    "official_replay_harness",
    "quality_precommit_certification",
    "five_role_prompts",
    "evidence_history_isolation",
    "frontend_authoritative_status",
    "first_strict_v143_v144",
    "strict_v1_reviewer_dead_code",
    "immutable_rating_cycle",
    "stability_ten_generations",
    "master_strict_output_projection",
})
REQUIRED_PROMPT_ROLES = frozenset({
    "Master",
    "Worker",
    "Reviewer",
    "Critic",
    "Orchestrator",
})
_VALID_STATUSES = frozenset({CURRENT_STATUS, SUPERSEDED_STATUS})
_VALID_EVIDENCE_STATES = frozenset({SOURCE_CONTRACT, RUNTIME_PENDING, HISTORICAL})
_RULE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_]{2,80}$")
_RAW_NAME_HANDSHAKE_RULE_ID = "raw_tcp_name_handshake"
_RAW_NAME_HANDSHAKE_REQUIRED_POSITIVE_TESTS = frozenset({
    "web/tests/test_national_raw_tcp_handshake.py::"
    "test_generated_native_bot_replies_to_raw_name_before_slow_worker_import",
    "web/tests/test_national_native_strict_artifacts.py::"
    "test_real_system_native_pair_records_one_valid_name_worker_handshake",
})
_RAW_NAME_HANDSHAKE_REQUIRED_NEGATIVE_TESTS = frozenset({
    "web/tests/test_national_native_strict_artifacts.py::"
    "test_system_native_name_handshake_evidence_is_required_but_legacy_fixture_is_not",
    "web/tests/test_national_runtime_telemetry.py::"
    "test_name_handshake_telemetry_preserves_duplicate_failed_and_malformed_evidence",
    "web/tests/test_national_native_strict_artifacts.py::"
    "test_native_precommit_rejects_name_handshake_compliance_failure",
})
_RAW_NAME_HANDSHAKE_REQUIRED_TERMS = (
    "launch initiated",
    "not worker-ready",
    "import-complete",
    "first decision clock",
    "unfinished policy import",
)
_QUALITY_RUNTIME_IDENTITY_RULE_ID = "quality_native_precommit_certification"
_QUALITY_RUNTIME_IDENTITY_REQUIRED_OWNER_SYMBOLS = (
    "web/core/national_runtime_authority.py::current_system_native_runtime_identity",
    "web/core/national_runtime_probe.py::runtime_probe_native_template_evidence",
    "web/core/national_runtime_probe.py::"
    "validate_runtime_probe_repeatability_evidence",
    "web/core/tool_helpers.py::_quality_gate_ok",
    "web/core/system_strict_bootstrap.py::_quality_repeatability_errors",
    "web/core/official_certification_job.py::_live_normal_full_admission_issues",
)
_QUALITY_RUNTIME_IDENTITY_REQUIRED_POSITIVE_TESTS = (
    "web/tests/test_national_runtime_authority.py::"
    "test_system_runtime_identity_accepts_only_exact_current_bytes",
    "web/tests/test_native_runtime_quality_identity.py::"
    "test_native_quality_reuse_requires_exact_template_evidence",
    "web/tests/test_official_certification_job.py::"
    "test_live_normal_full_admission_rebinds_current_receipt",
    "web/tests/test_official_certification.py::"
    "test_test_runner_envelope_preserves_normal_full_quality_admission",
    "web/tests/test_official_commit_gate.py::"
    "test_commit_bot_keeps_quality_admission_failure_out_of_infrastructure_retry",
    "web/tests/test_national_runtime_probe.py::"
    "test_repeatability_allows_only_active_nonbudget_row_action_variation",
)
_QUALITY_RUNTIME_IDENTITY_REQUIRED_NEGATIVE_TESTS = (
    "web/tests/test_national_runtime_authority.py::"
    "test_system_runtime_identity_rejects_precompute_only_drift",
    "web/tests/test_native_runtime_quality_identity.py::"
    "test_commit_ledger_rejects_precommit_from_another_native_template",
    "web/tests/test_official_certification_job.py::"
    "test_stale_live_admission_never_creates_or_spawns_fresh_job",
    "web/tests/test_official_certification_job.py::"
    "test_stale_queued_job_becomes_terminal_before_queue_or_spawn",
    "web/tests/test_national_runtime_probe.py::"
    "test_repeatability_rejects_missing_required_activity_section",
    "web/tests/test_native_runtime_quality_identity.py::"
    "test_native_quality_reuse_rejects_tampered_repeatability_receipt",
    "web/tests/test_official_platform_harness.py::"
    "test_formal_quality_admission_rejects_missing_repeatability_receipt",
    "web/tests/test_system_control_contract.py::"
    "test_v143_bootstrap_requires_structural_dynamic_repeatability_receipt",
    "web/tests/test_precommit_eval_contract.py::"
    "test_verified_precommit_cache_rejects_missing_runtime_repeatability_receipt",
)
_QUALITY_RUNTIME_IDENTITY_REQUIRED_TERMS = (
    "composite runtime identity",
    "precompute-only drift",
    "combined_digest",
    "durable job",
    "stale admission",
    "repeatability receipt",
    "per-scenario transcript",
    "fenced writer",
)


def _ref(path: str, symbol: str = "") -> SourceRef:
    return SourceRef(path, symbol)


def _prompts(*roles: tuple[str, str, str, tuple[str, ...]]) -> tuple[PromptBinding, ...]:
    """Build compact prompt bindings without hiding their paths in prose."""

    return tuple(
        PromptBinding(
            role=role,
            renderer=_ref(renderer_path, renderer_symbol),
            templates=tuple(_ref(template) for template in templates),
        )
        for role, renderer_path, renderer_symbol, templates in roles
    )


_CORE_PROMPTS = _prompts(
    (
        "Master",
        "web/core/agent_master.py",
        "_render_master_final_provider_prompt",
        ("web/core/prompts/master_prompt.md",),
    ),
    (
        "Worker",
        "web/core/agent_workers.py",
        "_render_worker_provider_prompt",
        (
            "web/core/prompts/worker_prompt.md",
            "web/core/prompts/worker_profile_national_native.md",
        ),
    ),
    (
        "Reviewer",
        "web/core/tool_gates.py",
        "_render_reviewer_provider_prompt",
        ("web/core/prompts/reviewer_prompt.md",),
    ),
    (
        "Critic",
        "web/core/agent_review.py",
        "_render_critic_provider_prompt",
        ("web/core/prompts/critic_prompt.md",),
    ),
    (
        "Orchestrator",
        "web/core/orchestrator.py",
        "_render_orchestrator_provider_prompt",
        ("web/core/prompts/orchestrator.md",),
    ),
)


# Keep one row per causal contract.  Do not turn a green local test into a
# production-completion claim: runtime-pending rows remain explicit until the
# stopped checkout is recovered and its signed/publication evidence exists.
CURRENT_ALIGNMENT_ROWS: tuple[MatrixRow, ...] = (
    MatrixRow(
        rule_id="raw_tcp_delimiter_stream",
        coverage=("raw_tcp_delimiter_stream",),
        status=CURRENT_STATUS,
        evidence_state=SOURCE_CONTRACT,
        authority=(
            _ref("AGENTS.md", "TCP recv boundaries are not message boundaries"),
            _ref("docs/official-raise-boundary-oracle-2026-07-11.md"),
        ),
        production_owners=(
            _ref("sever/server/protocol.py", "split_server_messages"),
            _ref("sever/server/transport.py", "pop_client_action"),
            _ref("web/core/national_native.py", "NationalStreamDecoder"),
        ),
        dynamic_gates=(
            _ref("web/core/national_runtime_probe.py", "run_national_runtime_probe"),
            _ref("web/core/national_native.py", "check_native_stream_decoder"),
        ),
        prompts=_CORE_PROMPTS,
        prompt_statement=(
            "All five rendered roles must preserve delimiter-free raw TCP as a typed, "
            "quality-gated system contract: report parser defects to deterministic gates, "
            "never manufacture delimiters or relax the stream boundary."
        ),
        prompt_required_terms=("typed", "quality"),
        producer_consumer=(
            "raw TCP byte chunks → sever/server/protocol.py incremental tokenizer "
            "→ national_native.NationalStreamDecoder → typed decision_context"
        ),
        positive_tests=(
            "sever/tests/test_national_platform_alignment.py::"
            "test_server_message_tokenizer_handles_fragmented_and_sticky_raw_tokens",
        ),
        negative_tests=(
            "sever/tests/test_transport.py::test_transport_rejects_truncated_utf8_at_eof",
        ),
        fail_closed=(
            "Incomplete or malformed delimiter-free bytes stay buffered or become "
            "a protocol error; they never advance authoritative hand state."
        ),
    ),
    MatrixRow(
        rule_id=_RAW_NAME_HANDSHAKE_RULE_ID,
        coverage=("raw_tcp_name_handshake",),
        status=CURRENT_STATUS,
        evidence_state=SOURCE_CONTRACT,
        authority=(
            _ref("AGENTS.md", "Platform is TCP server; each AI is a client"),
            _ref("AGENTS.md", "single socket send path"),
            _ref(
                "web/core/strategy_reference_pack.py",
                "current_strict_runtime_prompt_overlay",
            ),
        ),
        production_owners=(
            _ref("web/core/national_native.py", "NATIVE_BOT_TEMPLATE"),
            _ref("web/core/national_native.py", "NativeNationalBot"),
            _ref("web/core/national_runtime_telemetry.py", "parse_native_bot_log"),
            _ref("web/core/tool_eval.py", "run_precommit_eval"),
        ),
        dynamic_gates=(
            _ref(
                "web/core/national_native.py",
                "_system_native_name_handshake_issues",
            ),
            _ref("web/core/national_native.py", "run_native_precommit"),
            _ref("web/core/tool_eval.py", "run_precommit_eval"),
        ),
        prompts=_CORE_PROMPTS,
        prompt_statement=(
            "All five rendered roles receive the source-owned rule: `name` starts "
            "the worker before preflop, but that is launch initiated—not worker-ready "
            "or import-complete—and the first decision clock includes unfinished policy "
            "import; no role may claim readiness, reset that clock, or waive the quality "
            "precommit gate."
        ),
        prompt_required_terms=("name", "quality"),
        producer_consumer=(
            "raw delimiter-free `name` frame → socket owner starts but does not await "
            "the system worker → exact raw team-name reply + system log → native "
            "precommit compliance result; launch initiated is not worker-ready or "
            "import-complete proof, and the first decision clock includes unfinished "
            "policy import"
        ),
        positive_tests=(
            "web/tests/test_national_raw_tcp_handshake.py::"
            "test_generated_native_bot_replies_to_raw_name_before_slow_worker_import",
            "web/tests/test_national_native_strict_artifacts.py::"
            "test_real_system_native_pair_records_one_valid_name_worker_handshake",
        ),
        negative_tests=(
            "web/tests/test_national_native_strict_artifacts.py::"
            "test_system_native_name_handshake_evidence_is_required_but_legacy_fixture_is_not",
            "web/tests/test_national_runtime_telemetry.py::"
            "test_name_handshake_telemetry_preserves_duplicate_failed_and_malformed_evidence",
            "web/tests/test_national_native_strict_artifacts.py::"
            "test_native_precommit_rejects_name_handshake_compliance_failure",
        ),
        fail_closed=(
            "Missing, repeated, malformed, launch-failed, or missing-reply handshake "
            "evidence becomes a native compliance issue; precommit blocks publication "
            "rather than inferring worker readiness, resetting the first-decision clock, "
            "or accepting a synthetic prewarm."
        ),
    ),
    MatrixRow(
        rule_id="official_called_allin_runout_omission",
        coverage=("official_called_allin_runout",),
        status=CURRENT_STATUS,
        evidence_state=SOURCE_CONTRACT,
        authority=(
            _ref("docs/official-allin-runout-wire-oracle-2026-07-19.md"),
            _ref("AGENTS.md", "may omit every not-yet-sent public street"),
        ),
        production_owners=(
            _ref("sever/engine/game.py", "allin_settled"),
            _ref(
                "web/core/official_wire_probe.py",
                "_showdown_terminal_boundary",
            ),
            _ref(
                "web/core/official_wire_probe.py",
                "replay_events",
            ),
            _ref(
                "web/core/official_wire_probe.py",
                "_cross_bound_showdown_records",
            ),
            _ref(
                "web/core/official_platform_harness.py",
                "_omitted_allin_thp_bindings",
            ),
        ),
        dynamic_gates=(
            _ref(
                "web/core/official_wire_probe.py",
                "_showdown_terminal_boundary",
            ),
            _ref(
                "web/core/official_wire_probe.py",
                "replay_events",
            ),
            _ref(
                "web/core/official_certification.py",
                "_full_v5_completion_issues",
            ),
            _ref(
                "web/core/official_platform_harness.py",
                "round_completion_issues",
            ),
        ),
        prompts=_CORE_PROMPTS,
        prompt_statement=(
            "All five rendered roles must preserve the called all-in terminal "
            "contract: the official EXE may present an omitted runout followed by "
            "settlement/showdown, only exact cross-connection proof is accepted, no "
            "role may fabricate unseen public cards, and strict THP must carry either "
            "the exact observed wire prefix or a complete board, with complementary "
            "cross-wire actions, exact all-in net settlement, and strict THP "
            "exact-prefix-or-five-card board/action/blind/hole/earnings binding. Only a "
            "same-connection raw action awaiting its bounded causal flush is "
            "provisional during live replay; finalized replay is strict."
        ),
        prompt_required_terms=(
            "called all-in",
            "omitted runout",
            "settlement/showdown",
            "cross-connection",
            "fabricate unseen public cards",
            "complementary cross-wire actions",
            "exact all-in net settlement",
            "strict THP exact-prefix-or-five-card board/action/blind/hole/earnings",
        ),
        producer_consumer=(
            "official EXE allin/call plus legal board prefix → two connection-local "
            "terminal records → finalized cross-wire action/exact-settlement/reveal "
            "proof → strict THP exact-prefix-or-five-card board/action/blind/hole/earnings "
            "binding → formal "
            "completion gate"
        ),
        positive_tests=(
            "web/tests/test_official_wire_probe.py::"
            "test_replay_accepts_cross_connection_settled_called_allin_without_runout",
            "web/tests/test_official_wire_probe.py::"
            "test_replay_defers_cross_socket_settlement_order_until_final_binding",
            "web/tests/test_official_wire_probe.py::"
            "test_replay_accepts_only_exact_called_allin_net_settlements",
            "web/tests/test_official_wire_probe.py::"
            "test_wire_probe_replays_idle_action_at_last_raw_observation",
            "web/tests/test_official_platform_harness.py::"
            "test_terminal_hand_completion_requires_exact_wire_boundary_and_thp",
            "sever/tests/test_national_platform_alignment.py::"
            "test_allin_runout_is_internal_thp_truth_but_omitted_from_wire",
        ),
        negative_tests=(
            "web/tests/test_official_wire_probe.py::"
            "test_replay_rejects_unproved_missing_runout_showdown",
            "web/tests/test_official_wire_probe.py::"
            "test_replay_rejects_showdown_cross_seat_mismatch_and_non_showdown_reveal",
            "web/tests/test_official_wire_probe.py::"
            "test_replay_keeps_unpaired_called_allin_reveal_provisional_until_finalized",
        ),
        fail_closed=(
            "A malformed prefix, unfinished/folded all-in, nonterminal reveal, "
            "cross-wire action mismatch, impossible all-in settlement, or any omitted "
            "hand lacking an exact-prefix-or-complete THP board/action/blind/hole/earnings "
            "binding remains an official wire/certification failure. A finalized or "
            "source-less street boundary is never downgraded and no missing public "
            "card is synthesized."
        ),
    ),
    MatrixRow(
        rule_id="raise_terminal_hand70",
        coverage=("raise_terminal_hand70",),
        status=CURRENT_STATUS,
        evidence_state=SOURCE_CONTRACT,
        authority=(
            _ref("docs/official-raise-boundary-oracle-2026-07-11.md"),
            _ref("docs/official-terminal-settlement-oracle-2026-07-11.md"),
            _ref("AGENTS.md", "natural hand 70"),
        ),
        production_owners=(
            _ref("sever/engine/validator.py", "validate_action"),
            _ref("sever/engine/game.py", "GameEngine"),
            _ref("web/core/official_certification.py", "_full_v5_completion_issues"),
        ),
        dynamic_gates=(
            _ref("web/core/runtime_architecture_policy.py", "_verified_official_oracle_identity"),
            _ref("web/core/official_certification.py", "receipt_validation_issues"),
        ),
        prompts=_CORE_PROMPTS,
        prompt_statement=(
            "All five rendered roles must preserve raise-to-total legality and require "
            "official-full-v5 hand-70 proof; no role may reinterpret a raise increment "
            "or certify a missing terminal settlement."
        ),
        prompt_required_terms=("raise-to", "official-full-v5"),
        producer_consumer=(
            "validator street-total bounds → game/strict reducer settlement → "
            "official wire/THP receipt → v5 certificate verifier"
        ),
        positive_tests=(
            "sever/tests/test_national_platform_alignment.py::"
            "test_official_oracle_accepts_exact_2x_reraise_and_rejects_below_boundary",
            "sever/tests/test_national_platform_alignment.py::"
            "test_game_engine_matches_official_hand_70_wire_settlement_boundary",
        ),
        negative_tests=(
            "web/tests/test_official_certification.py::"
            "test_formal_certification_requires_the_final_settlement",
            "web/tests/test_official_certification.py::"
            "test_full_v5_rejects_paired_70_wire_settlement_bypass",
        ),
        fail_closed=(
            "Illegal raise_to is sanitized before the socket; absent hand-70 wire "
            "settlement requires independent THP/footer proof or certification fails."
        ),
    ),
    MatrixRow(
        rule_id="strict_abi_context_fallback_deadline",
        coverage=("strict_abi_context_fallback_deadline",),
        status=CURRENT_STATUS,
        evidence_state=SOURCE_CONTRACT,
        authority=(
            _ref("AGENTS.md", "Strict candidate ABI"),
            _ref("AGENTS.md", "55 second hard deadline"),
        ),
        production_owners=(
            _ref("web/core/national_capability_contract.py", "evaluate_national_capabilities"),
            _ref("web/core/national_native.py", "NativeNationalBot"),
            _ref("web/core/national_native.py", "_policy_worker_main"),
        ),
        dynamic_gates=(
            _ref("web/core/national_runtime_probe.py", "run_national_runtime_probe"),
            _ref("web/core/national_capability_contract.py", "evaluate_national_capabilities"),
        ),
        prompts=_CORE_PROMPTS,
        prompt_statement=(
            "All five rendered roles must keep the typed decision_context ABI, the fixed "
            "192/256/96 baseline, and the 200 ms / 250 ms timing boundary system-owned; "
            "they may propose policy changes but cannot weaken fallback or deadlines. "
            "A public-state string such as opponent check is input, never candidate wire ownership; "
            "only a typed intent may leave a policy entrypoint, never a bare wire string."
        ),
        prompt_required_terms=(
            "typed",
            "192/256/96",
            "200 ms",
            "250 ms",
            "wire string",
        ),
        producer_consumer=(
            "authoritative public state/legal/deadline → schema-v1 decision_context "
            "→ typed policy intent → system fallback/sanitizer → sole raw socket send"
        ),
        positive_tests=(
            "web/tests/test_national_runtime_probe.py::"
            "test_checked_in_bootstrap_policy_uses_all_bounded_match_signals_on_wire",
            "web/tests/test_national_capability_hardening.py::"
            "test_checked_in_strict_policy_passes_hardened_static_decision_guards",
            "web/tests/test_national_capability_hardening.py::"
            "test_typed_intent_distinguishes_public_check_input_from_check_output_kind",
            "web/tests/test_national_capability_hardening.py::"
            "test_typed_intent_allows_typed_helper_output",
        ),
        negative_tests=(
            "web/tests/test_national_runtime_probe.py::test_worker_rejects_non_typed_policy_output",
            "web/tests/test_national_runtime_probe.py::"
            "test_worker_rejects_deadline_profile_specific_late_baseline",
            "web/tests/test_national_capability_hardening.py::"
            "test_typed_intent_rejects_bare_action_return_and_simple_alias",
            "web/tests/test_national_capability_hardening.py::"
            "test_typed_intent_rejects_bare_action_refinement_yields",
            "web/tests/test_national_capability_hardening.py::"
            "test_typed_intent_rejects_bounded_obfuscated_bare_action_outputs",
            "web/tests/test_national_capability_hardening.py::"
            "test_typed_intent_rejects_helper_yield_and_unresolved_helper_cycle",
        ),
        fail_closed=(
            "A bare action return, invalid kind, bad, late, or missing policy result is ignored; "
            "the precomputed legal fallback is sent and the timed worker process group is terminated."
        ),
    ),
    MatrixRow(
        rule_id="system_asset_boundary",
        coverage=("system_asset_boundary",),
        status=CURRENT_STATUS,
        evidence_state=SOURCE_CONTRACT,
        authority=(
            _ref("AGENTS.md", "Strict candidate ABI"),
            _ref("AGENTS.md", "Space-for-time assets"),
        ),
        production_owners=(
            _ref("web/core/bot_namespace.py", "strict_artifact_layout_errors"),
            _ref(
                "web/core/national_capability_contract.py",
                "evaluate_national_capabilities",
            ),
            _ref("web/core/national_native.py", "check_native_contract"),
        ),
        dynamic_gates=(
            _ref("web/core/bot_namespace.py", "strict_artifact_layout_errors"),
            _ref(
                "web/core/national_capability_contract.py",
                "evaluate_national_capabilities",
            ),
            _ref("web/core/national_native.py", "check_native_contract"),
        ),
        prompts=_CORE_PROMPTS,
        prompt_statement=(
            "All five rendered roles must preserve five executable/identity files and "
            "treat an external asset as unavailable to v1 policy until its separate "
            "system-owned asset ABI is bound; no role may make a model/table a Worker "
            "write, a sixth Bot file, or a direct policy path read."
        ),
        prompt_required_terms=(
            "five executable/identity",
            "asset ABI",
            "external asset",
        ),
        producer_consumer=(
            "five-file Bot directory → strict layout/capability/native checks → "
            "quality/precommit/certification; a future external asset requires a "
            "separate system-owned profile before any policy consumer exists"
        ),
        positive_tests=(
            "web/tests/test_policy_pipeline_stages.py::"
            "test_current_candidate_stage_requires_the_exact_five_file_artifact",
        ),
        negative_tests=(
            "web/tests/test_national_capability_hardening.py::"
            "test_static_capability_contract_rejects_unbound_candidate_model_file",
            "web/tests/test_national_decision_tester.py::"
            "test_native_contract_rejects_unbound_candidate_model_file",
        ),
        fail_closed=(
            "Any extra Bot file, symlink, cache, candidate-owned/unbound model, or "
            "direct policy asset access blocks static quality and native launch. R0 "
            "assets remain unavailable until every resolver and identity binding exists."
        ),
    ),
    MatrixRow(
        rule_id="strict_connection_memory",
        coverage=("strict_connection_memory",),
        status=CURRENT_STATUS,
        evidence_state=SOURCE_CONTRACT,
        authority=(
            _ref("AGENTS.md"),
            _ref("docs/official-terminal-settlement-oracle-2026-07-11.md"),
        ),
        production_owners=(
            _ref("web/core/national_native.py", "OpponentTracker"),
            _ref("web/core/national_native.py", "NativeNationalBot"),
        ),
        dynamic_gates=(
            _ref("web/core/national_runtime_probe.py", "run_national_runtime_probe"),
            _ref("web/core/runtime_architecture_policy.py", "_apply_typed_runtime_probe"),
        ),
        prompts=_CORE_PROMPTS,
        prompt_statement=(
            "All five rendered roles must consume only typed, current-generation "
            "connection-memory evidence supplied by the runtime; they may not invent or "
            "promote unbound historical opponent memory."
        ),
        prompt_required_terms=("typed", "current-generation"),
        producer_consumer=(
            "hand starts/actions/inferred closers/showdown evidence → connection-lived "
            "OpponentTracker → capped snapshot in decision_context → policy adjustment"
        ),
        positive_tests=(
            "web/tests/test_national_runtime_probe.py::"
            "test_worker_exercises_typed_context_lines_and_persistent_memory",
        ),
        negative_tests=(
            "web/tests/test_national_runtime_probe.py::"
            "test_profile_counterfactual_rejects_raw_signal_without_confidence_gate",
        ),
        fail_closed=(
            "Malformed, sparse, ambiguous, or unbound tracker evidence is neutralized; "
            "it cannot manufacture a capability or bypass legal fallback."
        ),
    ),
    MatrixRow(
        rule_id="official_wire_replay_harness",
        coverage=("official_replay_harness",),
        status=CURRENT_STATUS,
        evidence_state=SOURCE_CONTRACT,
        authority=(
            _ref("docs/official-terminal-settlement-oracle-2026-07-11.md"),
            _ref("AGENTS.md"),
        ),
        production_owners=(
            _ref("web/core/official_certification.py", "receipt_validation_issues"),
            _ref("web/core/official_certification.py", "run_certification"),
            _ref("scripts/official_certify.py"),
        ),
        dynamic_gates=(
            _ref("web/core/official_certification.py", "official_full_certified"),
            _ref("web/core/official_certification.py", "report_valid_for_spec"),
        ),
        prompts=_CORE_PROMPTS,
        prompt_statement=(
            "All five rendered roles must treat official-full-v5 certificate evidence as "
            "the formal replay authority; they may describe a wire/THP failure but cannot "
            "replace the deterministic certificate gate."
        ),
        prompt_required_terms=("official-full-v5", "certificate"),
        producer_consumer=(
            "official EXE raw wire and THP artifacts → identity-bound receipt/report "
            "validator → signed certificate projection → publication eligibility"
        ),
        positive_tests=(
            "web/tests/test_official_certification.py::"
            "test_full_certification_accepts_exe_terminal_thp_completion_proof",
        ),
        negative_tests=(
            "web/tests/test_official_certification.py::"
            "test_full_v5_rejects_paired_70_wire_settlement_bypass",
            "web/tests/test_official_wire_probe_cli_boundary.py::"
            "test_wire_probe_is_short_diagnostic_and_rejects_unbound_hand_70",
        ),
        fail_closed=(
            "A missing, mismatched, or fabricated wire/THP artifact is inconclusive or "
            "failed and never becomes a formal certificate or strength result."
        ),
    ),
    MatrixRow(
        rule_id=_QUALITY_RUNTIME_IDENTITY_RULE_ID,
        coverage=("quality_precommit_certification",),
        status=CURRENT_STATUS,
        evidence_state=SOURCE_CONTRACT,
        authority=(
            _ref("AGENTS.md", "Generation order"),
            _ref("docs/official-certification-policy.md", "official-full-v5"),
        ),
        production_owners=(
            _ref(
                "web/core/national_runtime_authority.py",
                "current_system_native_runtime_identity",
            ),
            _ref(
                "web/core/national_runtime_probe.py",
                "runtime_probe_native_template_evidence",
            ),
            _ref(
                "web/core/national_runtime_probe.py",
                "validate_runtime_probe_repeatability_evidence",
            ),
            _ref(
                "web/core/runtime_architecture_policy.py",
                "_apply_typed_runtime_probe",
            ),
            _ref("web/core/tool_gates.py", "run_quality_gates"),
            _ref("web/core/tool_eval.py", "run_precommit_eval"),
            _ref("web/core/precommit_eval_contract.py", "validate_precommit_plan"),
            _ref("web/core/tool_commit.py", "commit_bot"),
            _ref(
                "web/core/official_platform_harness.py",
                "build_formal_quality_admission",
            ),
            _ref(
                "web/core/official_certification_job.py",
                "_live_normal_full_admission_issues",
            ),
            _ref("web/core/official_certification_job.py", "start_or_poll_job"),
            _ref("web/core/official_certification_job.py", "_worker_main"),
            _ref("web/core/pipeline_state.py", "route_policy"),
            _ref("web/core/pipeline_state.py", "head_drift_resume_policy"),
            _ref("web/core/tool_helpers.py", "_prepare_official_profile_refresh"),
            _ref("web/core/tool_helpers.py", "_quality_gate_ok"),
            _ref(
                "web/core/system_strict_bootstrap.py",
                "_quality_repeatability_errors",
            ),
        ),
        dynamic_gates=(
            _ref(
                "web/core/national_runtime_probe.py",
                "runtime_probe_native_template_evidence_matches",
            ),
            _ref(
                "web/core/national_runtime_probe.py",
                "validate_runtime_probe_repeatability_evidence",
            ),
            _ref("web/core/tool_gates.py", "run_quality_gates"),
            _ref("web/core/tool_helpers.py", "_native_runtime_evidence_current"),
            _ref("web/core/tool_helpers.py", "_quality_gate_ok"),
            _ref("web/core/tool_eval.py", "run_precommit_eval"),
            _ref("web/core/tool_commit.py", "_run_official_full_commit_gate"),
            _ref(
                "web/core/official_platform_harness.py",
                "formal_quality_admission_integrity_issues",
            ),
            _ref(
                "web/core/official_certification_job.py",
                "_live_normal_full_admission_issues",
            ),
            _ref("web/core/official_certification_job.py", "_spawn_worker"),
            _ref("web/core/official_certification_job.py", "_worker_main"),
            _ref("web/core/pipeline_state.py", "head_drift_resume_policy"),
        ),
        prompts=_CORE_PROMPTS,
        prompt_statement=(
            "All five rendered roles must leave quality, precommit, and certificate "
            "admission to deterministic gates. A schema-2 composite runtime identity "
            "binds system-owned national_bot.py and precompute.py by SHA-256/size plus "
            "combined_digest; precompute-only drift is stale. A model may report a "
            "blocker but cannot approve, mint, or bypass an official certificate or a "
            "stale admission before a durable job; final formal admission failure remains "
            "a deterministic quality outcome, never an infrastructure retry. Ordinary "
            "official-certifying HEAD drift remains a commit poll; only the complete "
            "quality-admission marker may refresh deterministic quality. A repeatability "
            "receipt is a system-produced, fenced writer proof: every persisted per-scenario "
            "transcript, typed intent, canonical wire, runtime row, isolation digest, and "
            "redacted repeated-view digest must validate before any role can reuse quality."
        ),
        prompt_required_terms=(
            "quality",
            "precommit",
            "certificate",
            *_QUALITY_RUNTIME_IDENTITY_REQUIRED_TERMS,
        ),
        producer_consumer=(
            "system-owned national_bot.py + precompute.py bytes → schema-2 composite "
            "runtime identity → two isolated runtime-probe executions → bounded redacted "
            "repeatability receipt (per-scenario transcript/action/wire/runtime, isolation, "
            "and matching view digests) → fresh quality/cache/recovery/v143 bootstrap/formal "
            "admission validators → runtime probe/quality ledger → frozen native precommit "
            "plan/result → live normal-full admission rebind before durable creation, "
            "queued/retry queue claim, pre-Popen spawn, worker claim, and harness EXE work "
            "→ typed quality-admission terminal → exact official_full quality marker + "
            "contract-unchanged revision/stage/workflow CAS → dynamic quality refresh, or "
            "ordinary official-certifying commit poll → new official certificate → "
            "commit/tag/.completed publication transaction"
        ),
        positive_tests=(
            *_QUALITY_RUNTIME_IDENTITY_REQUIRED_POSITIVE_TESTS,
            "web/tests/test_precommit_eval_contract.py::"
            "test_native_batch_plan_binds_ordered_samples_and_execution_phases",
            "web/tests/test_official_commit_gate.py::"
            "test_official_full_pass_is_persisted_in_verified_gate_ledger",
            "web/tests/test_official_commit_gate.py::"
            "test_strict_normal_full_commit_binds_admission_before_job_and_blocks_missing",
        ),
        negative_tests=(
            *_QUALITY_RUNTIME_IDENTITY_REQUIRED_NEGATIVE_TESTS,
            "web/tests/test_official_commit_gate.py::"
            "test_git_commit_bot_rejects_missing_official_certificate_before_git",
            "web/tests/test_precommit_eval_contract.py::"
            "test_plan_fails_closed_when_published_opponent_identity_drifts",
            "web/tests/test_official_platform_harness.py::"
            "test_strict_normal_full_refuses_missing_or_tampered_admission_before_worker",
        ),
        fail_closed=(
            "Any quality, plan, opponent, certificate, stale admission, malformed/missing "
            "repeatability receipt, transcript/action/wire/runtime-row omission, digest divergence, "
            "or composite runtime "
            "identity drift — including precompute-only drift — blocks cache reuse, precommit, "
            "durable job creation/worker spawn, commit/push/tag, and leaves the candidate non-published; "
            "a final formal-admission failure stays quality-gated and cannot become an infrastructure retry. "
            "Without all three quality-marker fields, official-certifying stays commit_bot-only."
        ),
    ),
    MatrixRow(
        rule_id="five_role_prompt_contract",
        coverage=("five_role_prompts",),
        status=CURRENT_STATUS,
        evidence_state=SOURCE_CONTRACT,
        authority=(
            _ref("AGENTS.md", "Generation order"),
            _ref("web/core/llm_query.py", "ACTIVE_LLM_ROLE_CONTRACTS"),
        ),
        production_owners=(
            _ref("web/core/llm_query.py", "ACTIVE_LLM_ROLE_CONTRACTS"),
            _ref("web/core/agent_master.py", "_render_master_final_provider_prompt"),
            _ref("web/core/agent_workers.py", "_render_worker_provider_prompt"),
            _ref("web/core/tool_gates.py", "_render_reviewer_provider_prompt"),
            _ref("web/core/reviewer_retry.py", "build_review_attempt_receipt"),
            _ref("web/core/agent_review.py", "_render_critic_provider_prompt"),
            _ref("web/core/orchestrator.py", "_render_orchestrator_provider_prompt"),
        ),
        dynamic_gates=(
            _ref("web/core/llm_query.py", "resolve_llm_role_contract"),
            _ref("web/core/llm_query.py", "render_llm_prompt"),
            _ref("web/core/reviewer_retry.py", "review_attempt_action"),
            _ref(
                "web/core/system_strict_bootstrap.py",
                "abandon_rejected_blueprint",
            ),
            _ref("web/core/gate_outcome.py", "validate_terminal_gate_outcome"),
        ),
        prompts=_CORE_PROMPTS,
        prompt_statement=(
            "All five rendered roles receive one typed, quality-bound current contract; "
            "they may report deterministic violations but cannot relax role scope, prompt "
            "provenance, or gate ownership. Reviewer verdicts are content-bound: one "
            "negative schedules exactly one independent same-stage review. A conflict "
            "or two negatives conservatively routes an agent-editable candidate to targeted "
            "repair, but terminalizes the fixed system-owned first-strict blueprint through "
            "an exact-CAS review_rejected receipt and canonical abandon; neither path silently approves."
        ),
        prompt_required_terms=("typed", "quality"),
        producer_consumer=(
            "frozen checkpoint/evidence/template inputs → role renderer + sealed provenance "
            "→ Master/Worker/Reviewer/advisory Critic/Orchestrator provider invocation; "
            "immutable dual-Review journal → ordinary targeted repair or fixed first-strict "
            "exact terminal outcome → canonical abandon"
        ),
        positive_tests=(
            "web/tests/test_national_prompt_rules.py::"
            "test_cross_role_prompts_and_reference_packet_bind_current_baseline_contract",
            "web/tests/test_llm_role_contract_registry.py::"
            "test_all_subagent_roles_reach_provider_with_independent_receipts",
            "web/tests/test_reviewer_retry_tool.py::"
            "test_first_reject_retries_same_stage_then_conflict_routes_repair",
            "web/tests/test_reviewer_retry_tool.py::"
            "test_fixed_first_strict_dual_verdict_routes_exact_terminal_abandon",
            "web/tests/test_terminal_gate_outcome.py::"
            "test_dual_review_terminal_projection_is_exact_and_resumable",
        ),
        negative_tests=(
            "web/tests/test_llm_role_contract_registry.py::"
            "test_unknown_role_and_tool_scope_drift_fail_before_provider",
            "web/tests/test_strict_prompt_evidence_boundary.py::"
            "test_prompt_builders_have_no_retired_positive_read_chain",
            "web/tests/test_reviewer_retry.py::"
            "test_attempt_journal_tamper_and_third_attempt_fail_closed",
            "web/tests/test_terminal_gate_outcome.py::"
            "test_dual_review_terminal_projection_cas_failure_preserves_source",
        ),
        fail_closed=(
            "Unknown role, template drift, forged renderer text, forbidden scope, or "
            "unfrozen evidence aborts before a decision-changing provider call. A single "
            "Reviewer rejection cannot terminalize or rerun earlier stages; malformed/infra "
            "output consumes only its typed retry budget, while two verdicts are retained. "
            "The fixed first-strict system blueprint cannot enter repair_planned or "
            "execute_workers; any journal, gate, dual-slot authority, or terminal CAS drift "
            "blocks cleanup and restart instead of fabricating repair authority."
        ),
    ),
    MatrixRow(
        rule_id="evidence_history_isolation",
        coverage=("evidence_history_isolation",),
        status=CURRENT_STATUS,
        evidence_state=SOURCE_CONTRACT,
        authority=(
            _ref("AGENTS.md", "Evidence authority"),
            _ref("AGENTS.md", "zero authority"),
        ),
        production_owners=(
            _ref("web/core/evidence_snapshot.py", "ensure_generation_h2h_snapshot"),
            _ref("web/core/evaluation_bundle.py", "load_current_strict_evaluation_bundle"),
            _ref("web/core/rating_snapshot.py", "reconstruct_h2h_from_match_history"),
            _ref("web/core/agent_master.py", "_validated_snapshot_reference"),
        ),
        dynamic_gates=(
            _ref("web/core/evidence_snapshot.py", "validate_h2h_citations_against_snapshot"),
            _ref("web/core/evaluation_bundle.py", "load_published_evaluation_bundle"),
        ),
        prompts=_CORE_PROMPTS,
        prompt_statement=(
            "All five rendered roles must use only typed, current-generation evidence and "
            "treat archive and legacy material and mutable rating history as quarantined, never "
            "as prompt authority."
        ),
        prompt_required_terms=("current-generation", "archive", "legacy", "rating"),
        producer_consumer=(
            "identity-bound raw native 70-hand replay → immutable rating cycle/evidence snapshot "
            "→ constrained prompt projection; live, foreign, official, Arena, and legacy-untrusted history stay excluded"
        ),
        positive_tests=(
            "web/tests/test_evidence_snapshot.py::test_generation_h2h_snapshot_freezes_live_file",
            "web/tests/test_rating_snapshot.py::test_rebuilds_active_h2h_from_match_history",
        ),
        negative_tests=(
            "web/tests/test_rating_snapshot.py::"
            "test_foreign_history_cannot_change_h2h_chips_or_selection",
            "web/tests/test_evidence_snapshot.py::"
            "test_generation_h2h_snapshot_rejects_payload_tampering",
        ),
        fail_closed=(
            "Unbound, mutable, foreign, missing, or tampered history produces no H2H, "
            "selection, citation, prompt injection, or rating-cycle authority."
        ),
    ),
    MatrixRow(
        rule_id="frontend_authoritative_status",
        coverage=("frontend_authoritative_status",),
        status=CURRENT_STATUS,
        evidence_state=SOURCE_CONTRACT,
        authority=(
            _ref("web/core/epoch_authority.py", "strict_epoch_projection"),
            _ref("web/core/web_ui.py", "_active_generation_status_identity"),
            _ref("web/server/state.py", "task_snapshot"),
        ),
        production_owners=(
            _ref("web/core/web_ui.py", "set_status"),
            _ref("web/core/shutdown_manager.py", "request_shutdown"),
            _ref("web/server/state.py", "task_snapshot"),
            _ref("web/server/state.py", "add_task_snapshot_listener"),
            _ref("web/server/state.py", "_advance_task_lifecycle_locked"),
            _ref("web/server/state.py", "_on_shutdown_requested"),
            _ref("web/server/app.py", "_publish_task_owner"),
            _ref("web/server/app.py", "register_lifespan_runtime_owner"),
            _ref("web/server/app.py", "_stop_orchestrator"),
            _ref("web/server/routes/evolution.py", "_stable_stream_projection"),
            _ref("web/server/routes/evolution.py", "_current_transient_status"),
            _ref("web/server/routes/evolution.py", "_status_event_is_current"),
            _ref("web/server/routes/evolution.py", "_task_owner_event_is_current"),
            _ref("web/frontend/src/api/evolution.ts", "EvolutionState"),
            _ref(
                "web/frontend/src/lib/evolutionStreamController.ts",
                "evolutionStatusMatchesActiveGeneration",
            ),
            _ref(
                "web/frontend/src/lib/evolutionStreamController.ts",
                "shouldAcceptEvolutionStatus",
            ),
            _ref(
                "web/frontend/src/lib/evolutionStreamController.ts",
                "isFreshEvolutionStatusEvent",
            ),
            _ref(
                "web/frontend/src/lib/evolutionStreamController.ts",
                "transientStatusTaskMatches",
            ),
            _ref(
                "web/frontend/src/lib/evolutionStreamController.ts",
                "observeTransientStatusTaskProjection",
            ),
            _ref(
                "web/frontend/src/lib/evolutionStreamController.ts",
                "loseTransientStatusTaskAuthority",
            ),
            _ref("web/frontend/src/pages/EvolutionMonitor.tsx", "acceptTransientStatus"),
            _ref(
                "web/frontend/scripts/static-build-receipt.mjs",
                "verifyReceipt",
            ),
            _ref("web/main.py", "verify_frontend_static_receipt"),
            _ref("pokctl.sh", "frontend_static_receipt_valid"),
            _ref("scripts/pok_restart_observe.sh"),
        ),
        dynamic_gates=(
            _ref("web/server/routes/evolution.py", "_current_transient_status"),
            _ref("web/server/routes/evolution.py", "_status_event_is_current"),
            _ref("web/server/routes/evolution.py", "_task_owner_event_is_current"),
            _ref("web/server/routes/evolution.py", "_task_owner_projection"),
            _ref("web/server/routes/evolution.py", "evolution_state"),
            _ref("web/server/routes/evolution.py", "evolution_stream"),
            _ref(
                "web/frontend/scripts/static-build-receipt.mjs",
                "verifyReceipt",
            ),
            _ref("web/main.py", "verify_frontend_static_receipt"),
            _ref("pokctl.sh", "cmd_verify_frontend_static"),
        ),
        prompts=_CORE_PROMPTS,
        prompt_statement=(
            "All five rendered roles must treat UI status as an authority-gated, "
            "non-authoritative projection: every transient status needs the exact live task owner, "
            "monotonic lifecycle revision, shutdown eligibility, authority-loss fencing, and "
            "checkpoint identity; they may report a stale display but cannot infer or repair a "
            "checkpoint from the frontend."
        ),
        prompt_required_terms=(
            "UI",
            "authority-gated",
            "task owner",
            "lifecycle revision",
        ),
        producer_consumer=(
            "validated epoch/checkpoint identity + live AppState task owner monotonic lifecycle "
            "revision + shutdown eligibility → registered lifespan/current-AppState shutdown manager + "
            "WebUI status/SSE task_owner lifecycle high-water and invalidation → valid task_owner or "
            "task_authority_lost → route replay/state owner gate → typed frontend controller/page "
            "owner-revision conflict gate (same revision can recover only when exact) + bounded transient "
            "status expiry → connected SSE status text only, with HTTP task projection limited to invalidation "
            "→ source-fingerprint-bound static receipt before --no-build launch/restart → health/status presentation"
        ),
        positive_tests=(
            "web/tests/test_routes_evolution.py::TestEvolutionState::"
            "test_state_exposes_transient_status_only_for_current_active_task",
            "web/tests/test_routes_evolution.py::TestEvolutionStream::"
            "test_status_event_is_bound_to_current_checkpoint_at_emission",
            "web/tests/test_routes_control.py::TestEvolutionTaskOwnership::"
            "test_task_owner_listener_observes_replacement_without_polling",
            "web/tests/test_routes_evolution.py::TestEvolutionStream::"
            "test_task_owner_broadcast_is_minimal_typed_invalidation",
            "web/tests/test_frontend_contract_closure.py::"
            "test_frontend_liveness_fails_closed_on_sse_and_daemon_health",
            "web/tests/test_restart_observe_script.py::"
            "test_restart_refuses_stale_no_build_receipt_before_stop",
            "web/tests/test_web_launcher_config.py::"
            "test_valid_no_build_receipt_reaches_uvicorn_only_after_preflight",
        ),
        negative_tests=(
            "web/tests/test_routes_evolution.py::TestEvolutionState::"
            "test_state_drops_stale_or_inactive_transient_master_status",
            "web/tests/test_routes_evolution.py::TestEvolutionState::"
            "test_state_rejects_status_from_replaced_task_owner",
            "web/tests/test_routes_evolution.py::TestEvolutionStream::"
            "test_status_replay_filter_rejects_stale_inactive_or_wrong_revision",
            "web/tests/test_routes_evolution.py::TestEvolutionStream::"
            "test_task_owner_event_replay_rejects_replaced_owner",
            "web/tests/test_frontend_contract_closure.py::"
            "test_frontend_drops_stream_and_cycle_state_instead_of_merging_stale_authority",
            "tests/test_pokctl_checkout_scope.py::"
            "test_pokctl_restart_refuses_stale_no_build_receipt_before_stop",
            "web/tests/test_web_launcher_config.py::"
            "test_no_build_launcher_refuses_receipt_failure_before_server_import",
        ),
        fail_closed=(
            "Stale, replaced-owner, shutdown, lower-revision, equal-revision-conflicting, torn, or "
            "unverified/expired status identity is dropped; HTTP cannot revive a phrase. A stale/malformed "
            "static receipt refuses --no-build before importing the app, starting Uvicorn, or stopping/"
            "restarting the owned service. Null/malformed HTTP or "
            "SSE clears text through task_authority_lost without fabricating R+1, while an exact later same-R "
            "projection may recover and a conflicting same-R projection remains blocked until a newer revision."
        ),
    ),
    MatrixRow(
        rule_id="master_receipt_compiled_proposal_contract",
        coverage=("master_receipt_compiled_proposal_contract",),
        status=CURRENT_STATUS,
        evidence_state=SOURCE_CONTRACT,
        authority=(
            _ref("web/core/output_schema.py", "WORKER_PROMPT_MAX_CHARS"),
            _ref("web/core/agent_master.py", "_selected_proposal_binding"),
            _ref(
                "web/core/system_strict_bootstrap.py",
                "validate_selected_proposal_for_blueprint",
            ),
            _ref(
                "web/core/strict_authority_workflow.py",
                "validate_master_final_projection",
            ),
        ),
        production_owners=(
            _ref("web/core/agent_master.py", "_canonical_provider_worker_prompt"),
            _ref("web/core/plan_compiler.py", "compile_master_plan"),
            _ref("web/core/tool_planning.py", "run_master"),
            _ref("web/core/system_strict_bootstrap.py", "build_master_receipt"),
            _ref("web/core/tool_bot_management.py", "_generic_abandon_stage_block"),
        ),
        dynamic_gates=(
            _ref("web/core/agent_master.py", "_validate_final_proposal_binding"),
            _ref("web/core/tool_planning.py", "run_master"),
            _ref(
                "web/core/system_strict_bootstrap.py",
                "validate_selected_proposal_for_blueprint",
            ),
            _ref(
                "web/core/strict_authority_workflow.py",
                "validate_master_final_projection",
            ),
            _ref("web/core/tool_bot_management.py", "_generic_abandon_stage_block"),
        ),
        prompts=_CORE_PROMPTS,
        prompt_statement=(
            "All five rendered roles preserve the typed, quality-gated selected-proposal "
            "boundary: only a Worker consumes the sealed proposal_id and contract_digest; the "
            "selected EMISSION_CAPS row is the sole model-owned prompt limit, and no role may "
            "substitute a task brief or elevate one into a candidate-owned sixth artifact."
        ),
        prompt_required_terms=("proposal_id", "contract_digest"),
        producer_consumer=(
            "Scout packet + ballots → canonical Master typed-primary contract → sealed "
            "proposal binding + one rstrip-normalized provider prompt + full Worker block → "
            "12,000-character inline compiler/replay form → bootstrap receipt/final-Master "
            "replay → deterministic Worker envelope"
        ),
        positive_tests=(
            "web/tests/test_master_plan_contract_alignment.py::"
            "test_system_bootstrap_reuses_canonical_selected_proposal_contract_without_task_brief",
            "web/tests/test_master_success_return.py::"
            "test_v51_10017_char_strict_master_prompt_stays_inline_and_replays",
            "web/tests/test_master_success_return.py::"
            "test_v51_style_master_binding_overflow_gets_bounded_repair_and_stays_inline",
        ),
        negative_tests=(
            "web/tests/test_master_plan_contract_alignment.py::"
            "test_system_bootstrap_reuses_canonical_selected_proposal_contract_without_task_brief",
            "web/tests/test_master_success_return.py::"
            "test_v51_10017_char_strict_master_prompt_stays_inline_and_replays",
        ),
        fail_closed=(
            "A missing or mismatched typed-primary field, packet binding, source graph, prompt "
            "cap, or final-Master replay blocks Worker dispatch. An unexpected task brief or "
            "compiler compaction is rejected before checkpoint/bootstrap receipt. A receipt failure "
            "can canonically abandon only the pre-Worker direction_audited stage; it cannot spin, "
            "bypass a later gate, or be repaired by a sixth Bot file."
        ),
    ),
    MatrixRow(
        rule_id="strict_gate_receipt_append_order",
        coverage=("strict_gate_receipt_append_order",),
        status=CURRENT_STATUS,
        evidence_state=SOURCE_CONTRACT,
        authority=(
            _ref("web/core/strict_authority_workflow.py", "validate_receipts"),
            _ref("web/core/system_strict_bootstrap.py", "validate_master_receipt"),
            _ref("web/core/system_strict_bootstrap.py", "validate_system_gate_receipt"),
        ),
        production_owners=(
            _ref("web/core/strict_authority_workflow.py", "authority_summary"),
            _ref("web/core/system_strict_bootstrap.py", "_master_subject"),
            _ref("web/core/system_strict_bootstrap.py", "_system_gate_subject"),
            _ref("web/core/system_strict_bootstrap.py", "build_system_gate_receipt"),
        ),
        dynamic_gates=(
            _ref("web/core/strict_authority_workflow.py", "validate_receipts"),
            _ref("web/core/tool_helpers.py", "_review_gate_ok"),
            _ref("web/core/tool_helpers.py", "_critic_gate_ok"),
            _ref("web/core/tool_eval.py", "run_precommit_eval"),
            _ref("web/core/tool_commit.py", "commit_bot"),
        ),
        prompts=_CORE_PROMPTS,
        prompt_statement=(
            "All five rendered roles treat Master, Review, and Critic receipts as append-only "
            "system evidence: current construction owns one exact accepted prefix, while a "
            "stored Master or Review receipt may observe only its canonical, strictly later "
            "Review/Critic suffix; no role may author, reorder, duplicate, or waive it."
        ),
        prompt_required_terms=("append-only", "Review receipt", "Critic suffix"),
        producer_consumer=(
            "accepted Master events → exact Master receipt → Worker/quality → accepted Review "
            "and exact Review receipt → accepted Critic → independent historical receipt replay "
            "plus Review/Critic invocation-evidence gates → native precommit/commit"
        ),
        positive_tests=(
            "web/tests/test_strict_authority_workflow.py::"
            "test_gate_receipt_replay_allows_only_the_ordered_bound_suffix",
        ),
        negative_tests=(
            "web/tests/test_strict_authority_workflow.py::"
            "test_permitted_authority_suffix_must_be_canonical",
            "web/tests/test_strict_authority_workflow.py::"
            "test_gate_revisions_and_permitted_suffix_event_order_are_strict",
        ),
        fail_closed=(
            "An unknown, skipped, duplicate, same-revision, reverse-event-order, context-, "
            "provider-, effect-, role-result-, or receipt-drifted event invalidates the receipt. "
            "A historical allowance never changes current-gate exactness, and precommit/commit "
            "still require both independently verified Review and Critic authority."
        ),
    ),
    MatrixRow(
        rule_id="first_strict_v143_v144_contract",
        coverage=("first_strict_v143_v144",),
        status=CURRENT_STATUS,
        evidence_state=RUNTIME_PENDING,
        authority=(
            _ref("AGENTS.md"),
            _ref("web/core/generation_evidence.py", "build_protocol_bootstrap_evidence_identity"),
            _ref("web/core/epoch_authority.py", "first_strict_operator_transition"),
        ),
        production_owners=(
            _ref("web/core/system_strict_bootstrap.py", "validate_bootstrap_checkpoint"),
            _ref("web/core/national_native.py", "run_native_precommit"),
            _ref("web/core/first_strict_control.py", "validate_control_result"),
            _ref("web/core/tool_eval.py", "_build_first_strict_control_execution_scope"),
            _ref("web/core/official_certification.py", "build_spec"),
            _ref("web/core/tool_commit.py", "commit_bot"),
        ),
        dynamic_gates=(
            _ref("web/core/system_strict_bootstrap.py", "is_declared_native_bootstrap"),
            _ref("web/core/first_strict_control.py", "control_gate_blockers"),
            _ref("web/core/official_certification.py", "official_full_certified"),
            _ref("web/core/epoch_authority.py", "require_policy_epoch_initialized"),
        ),
        prompts=_CORE_PROMPTS,
        prompt_statement=(
            "All five rendered roles must keep v143 operator-only and require its certificate; "
            "v144+ uses official-full-v5 and cannot inherit the bootstrap exception."
        ),
        prompt_required_terms=("v143", "v144+", "official-full-v5", "certificate"),
        producer_consumer=(
            "stopped-checkout reset receipt → fresh v143 checkpoint/control artifact → "
            "native 8×70 repeat and aggregate results with explicit migration_projection=false → "
            "first-strict result validator → "
            "operator first-strict bootstrap certificate/tag → published v143 parent → v144 5+3 full certification"
        ),
        positive_tests=(
            "web/tests/test_first_strict_control.py::"
            "test_control_is_a_direct_content_bound_policy_artifact",
            "web/tests/test_hidden_fixes.py::"
            "test_H1_completed_control_match_recovers_by_same_identity_after_cancel",
            "web/tests/test_checkpoint_epoch_recovery.py::"
            "test_normal_strict_v144_resume_accepts_published_parent_binding",
        ),
        negative_tests=(
            "web/tests/test_first_strict_control.py::"
            "test_control_receipt_rejects_pool_or_authority_escalation",
            "web/tests/test_first_strict_control.py::"
            "test_control_result_rejects_missing_zero_migration_projection",
            "web/tests/test_official_certify_cli.py::"
            "test_cli_first_strict_requires_explicit_one_time_acknowledgement",
        ),
        fail_closed=(
            "No missing control, reset receipt, explicit zero-migration result flag, eligible "
            "parent, certificate, tag, or operator acknowledgement may be inferred; the "
            "checkpoint parks/requires recovery."
        ),
    ),
    MatrixRow(
        rule_id="strict_v1_reviewer_dead_code_contract",
        coverage=("strict_v1_reviewer_dead_code",),
        status=CURRENT_STATUS,
        evidence_state=SOURCE_CONTRACT,
        authority=(
            _ref("web/core/bootstrap_assets/strict_v1/policy.py"),
            _ref("web/core/bootstrap_assets/strict_v1/manifest.json"),
            _ref("web/core/system_strict_bootstrap.py", "validate_blueprint_package"),
            _ref("web/core/national_capability_contract.py", "evaluate_national_capabilities"),
        ),
        production_owners=(
            _ref("web/core/system_strict_bootstrap.py", "materialize_fresh_candidate"),
            _ref("web/core/bootstrap_assets/strict_v1/policy.py", "_preflop_spot_adjustment"),
        ),
        dynamic_gates=(
            _ref("web/core/system_strict_bootstrap.py", "validate_blueprint_package"),
            _ref("web/core/national_capability_contract.py", "evaluate_national_capabilities"),
            _ref("web/core/tool_gates.py", "run_quality_gates"),
            _ref("web/core/tool_gates.py", "run_review"),
        ),
        prompts=_CORE_PROMPTS,
        prompt_statement=(
            "All five roles distinguish deterministic machine-approved bounded policy work from "
            "semantic code review. Reviewer may reject a truly unreachable definition, but cannot "
            "reinterpret a system-bounded tracker or deadline-capped refinement as a protocol "
            "violation. The six heads-up line states retain four actionable and two explicitly "
            "neutral consumers."
        ),
        prompt_required_terms=(
            "unreachable definition",
            "six heads-up line states",
            "explicitly neutral",
        ),
        producer_consumer=(
            "system runtime six-state line.preflop_spot → policy actionable/neutral partition → "
            "explicit neutral adjustment for sb_limp/bb_option → baseline typed intent; checked-in "
            "policy bytes → manifest policy digest/output artifact hash → materializer, capability, "
            "Quality and Reviewer"
        ),
        positive_tests=(
            "web/tests/test_lll_informed_strict_bootstrap.py::"
            "test_line_state_only_spots_are_explicit_neutral_consumers",
        ),
        negative_tests=(
            "web/tests/test_lll_informed_strict_bootstrap.py::"
            "test_checked_in_policy_has_no_retired_opponent_adjustment_helper",
        ),
        fail_closed=(
            "A manifest mismatch, extra candidate file, capability failure, non-typed decision, or "
            "Reviewer rejection cannot publish. Reviewer infrastructure retry is a separate "
            "authority that must be consumed atomically before an approved or rejected semantic "
            "projection; a stale claim cannot be silently ignored."
        ),
    ),
    MatrixRow(
        rule_id="immutable_native_rating_cycle",
        coverage=("immutable_rating_cycle",),
        status=CURRENT_STATUS,
        evidence_state=RUNTIME_PENDING,
        authority=(
            _ref("AGENTS.md"),
            _ref("web/core/evaluation_bundle.py", "publish_evaluation_cycle_manifest"),
        ),
        production_owners=(
            _ref("web/core/elo_daemon.py", "admit_internal_match_result"),
            _ref("web/core/elo_daemon.py", "_save_authoritative_evaluation_cycle"),
            _ref("web/core/rating_snapshot.py", "reconstruct_h2h_from_match_history"),
            _ref("web/core/evaluation_bundle.py", "publish_evaluation_cycle_manifest"),
        ),
        dynamic_gates=(
            _ref("web/core/rating_snapshot.py", "_admitted_70_hand_history_sample"),
            _ref("web/core/evaluation_bundle.py", "load_current_strict_evaluation_bundle"),
        ),
        prompts=_CORE_PROMPTS,
        prompt_statement=(
            "All five rendered roles may reason from frozen 70-hand H2H and rating evidence, "
            "but cannot reopen, fabricate, or publish an immutable cycle themselves."
        ),
        prompt_required_terms=("H2H", "rating", "70-hand"),
        producer_consumer=(
            "two published strict bot artifacts + exact 70-hand native replays → admission "
            "→ immutable cycle manifest/H2H/ratings → frozen selection/evidence snapshot"
        ),
        positive_tests=(
            "web/tests/test_evaluation_bundle.py::"
            "test_daemon_authoritative_save_publishes_one_complete_cycle",
            "web/tests/test_national_native_strict_artifacts.py::"
            "test_immutable_rating_cycle_uses_shared_full_match_budget",
        ),
        negative_tests=(
            "web/tests/test_evaluation_bundle.py::"
            "test_publication_rejects_foreign_match_history_before_cycle_binding",
            "web/tests/test_rating_snapshot.py::"
            "test_missing_expected_identity_disables_all_history_influence",
        ),
        fail_closed=(
            "No incomplete, foreign, identity-drifted, or non-native result can advance "
            "ratings, selection, prompt evidence, or the immutable cycle pointer."
        ),
    ),
    MatrixRow(
        rule_id="stability_ten_generation_observation",
        coverage=("stability_ten_generations",),
        status=CURRENT_STATUS,
        evidence_state=RUNTIME_PENDING,
        authority=(
            _ref("AGENTS.md", "stability"),
            _ref("web/core/post_publication_handoff.py", "complete_post_publication_handoff"),
        ),
        production_owners=(
            _ref("web/core/post_publication_handoff.py", "plan_handoff_step"),
            _ref("web/core/post_publication_handoff.py", "complete_handoff_step"),
            _ref("web/core/stability_observation.py", "stability_observation_projection"),
            _ref(
                "web/core/orchestrator.py",
                "_stability_projection_maintenance_coroutine",
            ),
        ),
        dynamic_gates=(
            _ref("web/core/stability_observation.py", "record_published_generation"),
            _ref("web/core/stability_observation.py", "stability_observation_projection"),
            _ref(
                "web/core/stability_observation.py",
                "stability_observation_cached_projection",
            ),
            _ref(
                "web/core/orchestrator.py",
                "_stability_projection_maintenance_tick",
            ),
        ),
        prompts=_CORE_PROMPTS,
        prompt_statement=(
            "All five rendered roles must treat current-generation quality evidence as the "
            "only input to the N/10 observation; they cannot carry a stability claim across "
            "a restart, cleanup, or identity drift."
        ),
        prompt_required_terms=("current-generation", "quality"),
        producer_consumer=(
            "each post-publication exact main/tag/certificate/native-cycle proof → durable "
            "stability observation → lifecycle-owned pre-expiry single-flight verification → "
            "frontend projection → N/10 only for consecutive verified generations"
        ),
        positive_tests=(
            "web/tests/test_stability_observation.py::"
            "test_ten_consecutive_publications_complete_and_duplicate_is_idempotent",
            "web/tests/test_stability_observation.py::"
            "test_cached_projection_prefetch_keeps_current_value_fresh_until_reverified",
            "web/tests/test_stability_observation.py::"
            "test_orchestrator_stability_maintenance_is_lifecycle_bound",
        ),
        negative_tests=(
            "web/tests/test_stability_observation.py::"
            "test_process_restart_resets_existing_streak",
            "web/tests/test_stability_observation.py::"
            "test_identity_drift_replay_persists_reset_without_recounting_duplicate",
            "web/tests/test_stability_observation.py::"
            "test_cached_projection_expires_to_zero_before_background_refresh",
        ),
        fail_closed=(
            "A repair, manual cleanup, restart, branch/head/certificate/cycle identity drift, "
            "or missing proof resets/hides the count; a prefetch may reuse only an unexpired "
            "verified value, and a late/failed verifier still expires to zero."
        ),
    ),
    MatrixRow(
        rule_id="provider_canonical_abandon_handoff",
        coverage=("continuous_delivery_handoff",),
        status=CURRENT_STATUS,
        evidence_state=SOURCE_CONTRACT,
        authority=(
            _ref("docs/evolution-continuous-delivery-runbook.md"),
            _ref(
                "web/core/tool_bot_management.py",
                "validate_completed_abandon_handoff",
            ),
            _ref(
                "web/core/tool_bot_management.py",
                "reprove_historical_completed_abandon",
            ),
        ),
        production_owners=(
            _ref("web/core/tool_runtime_guard.py", "tool"),
            _ref(
                "web/core/llm_query.py",
                "register_current_provider_evolution_tool_use",
            ),
            _ref(
                "web/core/llm_query.py",
                "cache_verified_provider_terminal_abandon",
            ),
            _ref(
                "web/core/tool_bot_management.py",
                "reprove_historical_completed_abandon",
            ),
            _ref("web/core/orchestrator.py", "_run_one_cycle"),
            _ref(
                "web/core/orchestrator.py",
                "_detect_actionable_stage_handoff",
            ),
        ),
        dynamic_gates=(
            _ref(
                "web/core/tool_bot_management.py",
                "validate_completed_abandon_handoff",
            ),
            _ref(
                "web/core/tool_bot_management.py",
                "reprove_historical_completed_abandon",
            ),
            _ref(
                "web/core/llm_query.py",
                "cache_verified_provider_terminal_abandon",
            ),
            _ref("web/core/orchestrator.py", "_detect_actionable_stage_handoff"),
        ),
        prompts=_CORE_PROMPTS,
        prompt_statement=(
            "All five rendered roles treat canonical abandon as a deterministic terminal "
            "gate: only a routed owner may return a complete proof bound to one explicit "
            "ToolUse id, owner, arguments, and same provider attempt. A handler-before-stream "
            "provisional proof is unconsumable until one exact registration binds it. A missing "
            "checkpoint, historical receipt, or unbound cache cannot authorize a successor. "
            "The operator-only historical reproof reads a finalized terminal transaction only; "
            "it never reconstructs a provider result, checkpoint, or prepare authority."
        ),
        prompt_required_terms=(
            "canonical abandon",
            "ToolUse id",
            "same provider attempt",
        ),
        producer_consumer=(
            "guarded routed owner + immutable pre-call checkpoint → complete schema-2 result "
            "→ same-attempt provisional record (unconsumable) or exact ToolUse "
            "id/owner/arguments registration → unique binding → outer strict reason plus bounded "
            "causation-bound Worker inner reason → same-attempt handoff reproof or "
            "read-only finalized-transaction lineage reproof → outer scheduler terminal boundary"
        ),
        positive_tests=(
            "web/tests/test_orchestrator_timeout_extension.py::"
            "test_user_message_tool_use_binds_canonical_terminal_result",
            "web/tests/test_orchestrator_timeout_extension.py::"
            "test_verified_attempt_cache_recovers_only_one_missing_terminal_result",
            "web/tests/test_orchestrator_timeout_extension.py::"
            "test_attempt_terminal_cache_requires_verified_single_result",
            "web/tests/test_orchestrator_timeout_extension.py::"
            "test_guarded_terminal_owner_records_registered_id_and_arguments",
            "web/tests/test_orchestrator_timeout_extension.py::"
            "test_handler_before_user_tool_use_binds_and_recovers_terminal_handoff",
            "web/tests/test_abandon_helper.py::"
            "test_completed_abandon_handoff_reproves_schema2_split_worker_reason",
            "web/tests/test_abandon_helper.py::"
            "test_historical_completed_abandon_reproof_allows_clean_main_descendant",
            "web/tests/test_abandon_helper.py::"
            "test_historical_completed_abandon_reproof_accepts_bounded_reason_compatibility",
        ),
        negative_tests=(
            "web/tests/test_orchestrator_timeout_extension.py::"
            "test_attempt_terminal_cache_requires_exact_registered_args_and_terminal_owner",
            "web/tests/test_orchestrator_timeout_extension.py::"
            "test_user_tool_use_without_matching_terminal_cache_fails_closed",
            "web/tests/test_orchestrator_timeout_extension.py::"
            "test_terminal_cache_rejects_run_archivist_even_with_matching_tool_use",
            "web/tests/test_orchestrator_timeout_extension.py::"
            "test_provisional_terminal_cache_rejects_wrong_args_and_settled_history",
            "web/tests/test_orchestrator_timeout_extension.py::"
            "test_user_message_side_channel_fallback_result_is_ignored",
            "web/tests/test_abandon_helper.py::"
            "test_completed_abandon_handoff_rejects_missing_worker_inner_reason",
            "web/tests/test_abandon_helper.py::"
            "test_completed_abandon_handoff_rejects_mismatched_outer_reason",
            "web/tests/test_abandon_helper.py::"
            "test_completed_abandon_handoff_rejects_unbound_worker_inner_reason",
            "web/tests/test_abandon_helper.py::"
            "test_historical_completed_abandon_reproof_rejects_untrusted_current_source",
            "web/tests/test_abandon_helper.py::"
            "test_historical_completed_abandon_reproof_rejects_live_or_unfinalized_paths",
            "web/tests/test_abandon_helper.py::"
            "test_historical_completed_abandon_reproof_rejects_tampered_journal",
        ),
        fail_closed=(
            "Missing, duplicate, unregistered or still-provisional, cross-attempt, owner-mismatched, "
            "argument-mismatched, settled-history, proof-mismatched, outer-reason-mismatched, missing or "
            "unbound Worker-inner-reason, SDK/cache-mismatched terminal material, non-main/non-descendant "
            "source, live state, advanced ledger, tampered fence, or resurrected candidate is recovery-blocked; "
            "historical reproof returns terminal evidence only and never prepares a successor."
        ),
    ),
    MatrixRow(
        rule_id="master_scout_closed_falsifier_shape",
        coverage=("master_proposal_closed_schema",),
        status=CURRENT_STATUS,
        evidence_state=SOURCE_CONTRACT,
        authority=(
            _ref("web/core/output_schema.py", "MASTER_PROPOSAL_FALSIFIER_TESTS"),
            _ref("web/core/agent_master.py", "_validated_master_proposal"),
            _ref("web/core/strict_authority_workflow.py", "_project_role_result"),
        ),
        production_owners=(
            _ref(
                "web/core/agent_master.py",
                "_render_master_proposal_provider_prompt",
            ),
            _ref("web/core/agent_master.py", "_proposal_closed_json_shape"),
            _ref("web/core/agent_master.py", "_proposal_schema_repair_guidance"),
            _ref("web/core/agent_master.py", "_parse_master_proposal_output_with_mode"),
            _ref("web/core/agent_master.py", "_validated_master_proposal"),
            _ref("web/core/llm_query.py", "render_llm_role_contract_suffix"),
        ),
        dynamic_gates=(
            _ref("web/core/agent_master.py", "_master_proposal_repair_kind"),
            _ref("web/core/agent_master.py", "_parse_master_proposal_output_with_mode"),
            _ref("web/core/agent_master.py", "_validated_master_proposal"),
            _ref("web/core/agent_master.py", "_proposal_mechanism_target_errors"),
            _ref("web/core/strict_authority_workflow.py", "_project_role_result"),
        ),
        prompts=_CORE_PROMPTS,
        prompt_statement=(
            "All five rendered roles preserve the Master Scout closed falsifier contract: "
            "a falsifier is a closed six-key object, mechanism_target appears only at top level, and "
            "a shared leaf is executable only as a complete owner-qualified literal or an exact "
            "selected-root allowlisted list. A Scout emits one raw JSON object; only a sealed "
            "schema/distinctness repair may recover one unambiguous non-JSON prefix followed by "
            "one object with no trailing prose, while initial Scouts and other roles are unchanged "
            "and no third attempt exists."
        ),
        prompt_required_terms=(
            "closed six-key",
            "mechanism_target",
            "owner-qualified",
            "sealed schema/distinctness repair",
            "unambiguous non-JSON prefix",
            "no third attempt",
        ),
        producer_consumer=(
            "frozen source/evidence mapping → closed Scout JSON prompt → deterministic proposal "
            "validation and strict authority → packet/critics/final Master → bounded Worker contract"
        ),
        positive_tests=(
            "web/tests/test_master_proposal_ensemble.py::"
            "test_proposal_renderer_overrides_embedded_doc_reads_and_future_edges",
            "web/tests/test_master_proposal_ensemble.py::"
            "test_falsifier_schema_repair_explicitly_removes_top_level_target_duplication",
            "web/tests/test_master_proposal_ensemble.py::"
            "test_shared_leaf_scout_v54_prose_prefixed_json_recovers_once",
            "web/tests/test_master_proposal_ensemble.py::"
            "test_strict_projection_requires_the_sealed_repair_actual_role",
        ),
        negative_tests=(
            "web/tests/test_master_proposal_ensemble.py::"
            "test_final_packet_parser_rejects_claim_changed_after_id",
            "web/tests/test_master_proposal_ensemble.py::"
            "test_shared_fold_to_raise_bare_leaf_fails_scout_and_packet_replay",
            "web/tests/test_master_proposal_ensemble.py::"
            "test_v54_eof_object_recovery_is_repair_role_scoped_and_fail_closed",
            "web/tests/test_master_proposal_ensemble.py::"
            "test_v54_recovery_still_requires_full_proposal_semantics",
        ),
        fail_closed=(
            "An extra falsifier key, top-level-target duplication, bare/foreign owner, or exhausted "
            "schema repair is rejected before critics or Workers. The bounded compatibility parser "
            "requires an exact sealed repair role, a single unambiguous object and unchanged semantic/"
            "distinctness validation; trailing prose, multiple/malformed objects, arrays, initial-role "
            "use, or a third attempt fails closed instead of silently normalizing provider output."
        ),
    ),
    MatrixRow(
        rule_id="master_strict_output_projection",
        coverage=("master_strict_output_projection",),
        status=CURRENT_STATUS,
        evidence_state=SOURCE_CONTRACT,
        authority=(
            _ref("web/core/output_schema.py", "WORKER_PROMPT_MAX_CHARS"),
            _ref("web/core/output_schema.py", "STATE_LEARNING_INTERVENTION_TARGET_ALIASES"),
            _ref("web/core/agent_master.py", "_proposal_mechanism_target_errors"),
            _ref("web/core/agent_master.py", "_FRESH_STRICT_CONTROL_MEASUREMENT"),
        ),
        production_owners=(
            _ref("web/core/agent_master.py", "_render_master_proposal_provider_prompt"),
            _ref("web/core/agent_master.py", "_validated_master_proposal"),
            _ref("web/core/agent_master.py", "_canonicalize_selected_proposal_metadata"),
            _ref("web/core/agent_master.py", "_master_final_emission_guard"),
            _ref("web/core/plan_compiler.py", "compile_master_plan"),
            _ref("web/core/tool_planning.py", "run_master"),
            _ref("web/core/strict_authority_workflow.py", "validate_master_final_projection"),
        ),
        dynamic_gates=(
            _ref("web/core/agent_master.py", "_proposal_mechanism_target_errors"),
            _ref("web/core/agent_master.py", "_system_bound_proposal_measurement"),
            _ref("web/core/agent_master.py", "_validate_final_proposal_binding"),
            _ref("web/core/tool_planning.py", "run_master"),
            _ref("web/core/strict_authority_workflow.py", "validate_master_final_projection"),
        ),
        prompts=_CORE_PROMPTS,
        prompt_statement=(
            "All five rendered roles treat selected Master proposal metadata as system-bound, preserve "
            "the closed six-field fresh measurement shape, and obey the selected EMISSION_CAPS row as "
            "the sole deterministic Worker hard cap; strict plans remain inline and never rely on task "
            "brief externalization. "
            "A shared executable leaf is legal only with a complete owner-qualified literal or an exact "
            "selected-root allowlisted list; every bare, punctuation, compact, foreign, unknown-child, "
            "or identifier-continuation spelling is rejected."
        ),
        prompt_required_terms=(
            "selected Master proposal",
            "closed six-field",
            "Worker hard cap",
        ),
        producer_consumer=(
            "frozen architecture mapping + source graph → rendered Scout contract → validated proposal "
            "packet → selected ID plus system-rebound metadata and digest-bound final emission guard → "
            "one lossless production-equivalent compiler pass plus comparison-only bound-context replay → "
            "inline Worker prompt → quality/review/native precommit"
        ),
        positive_tests=(
            "web/tests/test_master_proposal_ensemble.py::"
            "test_shared_fold_to_raise_bare_leaf_fails_scout_and_packet_replay",
            "web/tests/test_master_proposal_ensemble.py::"
            "test_fresh_scout_measurement_is_system_bound_but_closed_shape_is_required",
            "web/tests/test_master_proposal_ensemble.py::"
            "test_master_final_emission_guard_binds_each_selected_prompt_budget",
            "web/tests/test_master_success_return.py::"
            "test_strict_projection_binds_duplicate_selected_metadata",
            "web/tests/test_master_success_return.py::"
            "test_v51_10017_char_strict_master_prompt_stays_inline_and_replays",
        ),
        negative_tests=(
            "web/tests/test_master_proposal_ensemble.py::"
            "test_root_scoped_shared_leaf_list_rejects_unknown_children",
            "web/tests/test_master_proposal_ensemble.py::"
            "test_shared_leaf_human_poker_phrase_fails_closed_even_beside_owner",
            "web/tests/test_master_proposal_ensemble.py::"
            "test_selected_proposal_budget_boundary_and_primary_mapping_are_exact",
        ),
        fail_closed=(
            "Any ambiguous leaf, foreign or unknown target, malformed fresh measurement, unallowed "
            "proposal ID, metadata drift after rebinding, unexpected compiler compaction/task brief, "
            "missing guard provenance, or hard-budget overflow blocks Worker dispatch and may only "
            "canonically abandon at the permitted pre-Worker stage."
        ),
    ),
    MatrixRow(
        rule_id="superseded_archive_untrusted",
        coverage=("superseded_legacy_untrusted",),
        status=SUPERSEDED_STATUS,
        evidence_state=HISTORICAL,
        authority=(
            _ref("archive/README.md", "legacy-untrusted"),
            _ref("docs/archive/README.md", "legacy-untrusted"),
        ),
        production_owners=(
            _ref("web/core/national_runtime_authority.py", "strict_published_bot_names"),
        ),
        dynamic_gates=(
            _ref("web/core/national_runtime_authority.py", "current_system_native_runtime_errors"),
        ),
        prompts=_CORE_PROMPTS,
        producer_consumer=(
            "historical archived bytes → explicit quarantine boundary → no active parser, "
            "candidate, rating, prompt, certification, or runtime consumer"
        ),
        positive_tests=(
            "web/tests/test_official_bootstrap.py::"
            "test_active_module_contains_no_archive_bot_resolution",
        ),
        negative_tests=(
            "web/tests/test_strict_prompt_evidence_boundary.py::"
            "test_prompt_builders_have_no_retired_positive_read_chain",
        ),
        fail_closed=(
            "Archive references are historical only: any attempted active resolution is "
            "rejected and cannot contribute source bytes, history, evidence, or strength."
        ),
        historical_reason=(
            "Legacy engines, prompts, results, and reports remain retained audit material; "
            "they are not current execution inputs."
        ),
    ),
)


def _is_archive_path(path: str) -> bool:
    return "archive" in Path(path).parts


def _safe_repo_path(path: str) -> Path | None:
    candidate = Path(path)
    if candidate.is_absolute() or ".." in candidate.parts or not path:
        return None
    resolved = ROOT / candidate
    try:
        resolved.relative_to(ROOT)
    except ValueError:
        return None
    return resolved


def _validate_ref(
    ref: SourceRef,
    *,
    row_id: str,
    field: str,
    require_symbol: bool = False,
) -> list[str]:
    errors: list[str] = []
    path = _safe_repo_path(ref.path)
    prefix = f"matrix_{field}:{row_id}:{ref.display()}"
    if path is None:
        return [f"{prefix}:unsafe_path"]
    if not path.is_file():
        return [f"{prefix}:missing_path"]
    if require_symbol and not ref.symbol:
        return [f"{prefix}:missing_symbol"]
    if ref.symbol:
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            return [f"{prefix}:unreadable:{type(exc).__name__}"]
        if ref.symbol not in source:
            errors.append(f"{prefix}:missing_symbol")
        elif require_symbol:
            if path.suffix == ".py":
                try:
                    tree = ast.parse(source, filename=str(path))
                except SyntaxError:
                    errors.append(f"{prefix}:unparsable_python")
                else:
                    callable_names = {
                        node.name
                        for node in ast.walk(tree)
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    }
                    if ref.symbol not in callable_names:
                        errors.append(f"{prefix}:missing_symbol")
            elif path.suffix in {".js", ".mjs"}:
                if not re.search(
                    rf"(?:async\s+)?function\s+{re.escape(ref.symbol)}\s*\(",
                    source,
                ):
                    errors.append(f"{prefix}:missing_symbol")
            elif path.suffix == ".sh":
                if not re.search(
                    rf"^\s*(?:function\s+)?{re.escape(ref.symbol)}\s*\(\)\s*\{{",
                    source,
                    re.MULTILINE,
                ):
                    errors.append(f"{prefix}:missing_symbol")
            else:
                errors.append(f"{prefix}:symbol_not_callable_source")
    return errors


def _validate_test_id(test_id: str, *, row_id: str, polarity: str) -> list[str]:
    """Verify pytest-like ``path::...::test_name`` IDs without importing tests."""

    parts = test_id.split("::")
    prefix = f"matrix_{polarity}_test:{row_id}:{test_id}"
    if len(parts) < 2 or not parts[-1].startswith("test_"):
        return [f"{prefix}:invalid_id"]
    path = _safe_repo_path(parts[0])
    if path is None or path.suffix != ".py":
        return [f"{prefix}:unsafe_or_non_python_path"]
    if not path.is_file():
        return [f"{prefix}:missing_path"]
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return [f"{prefix}:unreadable:{type(exc).__name__}"]
    pattern = re.compile(
        rf"^\s*(?:async\s+)?def\s+{re.escape(parts[-1])}\s*\(",
        re.MULTILINE,
    )
    if not pattern.search(source):
        return [f"{prefix}:missing_test"]
    return []


def _row_refs(row: MatrixRow) -> Iterable[SourceRef]:
    yield from row.authority
    yield from row.production_owners
    yield from row.dynamic_gates
    for binding in row.prompts:
        yield binding.renderer
        yield from binding.templates


def _renderer_callable_source(renderer: SourceRef) -> str | None:
    """Return one renderer function's source, without importing its module.

    Matrix validation needs to prove that each listed role still injects the
    shared, source-owned strict-runtime overlay.  Looking for the symbol in the
    whole module is too weak: another renderer could retain the call while the
    listed renderer silently loses it.  Restrict the check to the named
    function's AST segment instead.
    """

    path = _safe_repo_path(renderer.path)
    if path is None or not path.is_file() or not renderer.symbol:
        return None
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, UnicodeError, SyntaxError):
        return None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == renderer.symbol:
            return ast.get_source_segment(source, node)
    return None


def _strict_runtime_prompt_overlay() -> tuple[str | None, str | None]:
    """Load the exact common overlay that all current role renderers inject.

    This is a source-only quality check: the overlay is stdlib-only and has no
    runtime side effects.  A failed import or empty result is an explicit matrix
    failure, never a reason to treat role-specific template text as equivalent.
    """

    try:
        from strategy_reference_pack import current_strict_runtime_prompt_overlay

        overlay = current_strict_runtime_prompt_overlay()
    except Exception as exc:  # fail closed even if a future overlay adds imports
        return None, type(exc).__name__
    if not isinstance(overlay, str) or not overlay.strip():
        return None, "empty"
    return overlay, None


def _prompt_material(binding: PromptBinding, overlay: str) -> str | None:
    """Return the checked-in template inputs plus their common live overlay."""

    template_text: list[str] = []
    for template in binding.templates:
        path = _safe_repo_path(template.path)
        if path is None or not path.is_file():
            return None
        try:
            template_text.append(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            return None
    return "\n\n".join((*template_text, overlay))


def validate_alignment_matrix(
    rows: Sequence[MatrixRow] = CURRENT_ALIGNMENT_ROWS,
) -> list[str]:
    """Return deterministic fail-closed errors for a matrix source snapshot.

    Validation intentionally does not execute evolution, certification, or a
    match.  It proves that a current row cannot silently devolve into a stale
    prose assertion whose listed owner, gate, prompt, or regression no longer
    exists.  Runtime-pending state is represented explicitly, never guessed.
    """

    errors: list[str] = []
    seen_ids: set[str] = set()
    coverage: set[str] = set()
    overlay, overlay_error = _strict_runtime_prompt_overlay()
    if overlay_error is not None:
        errors.append(f"matrix_prompt_overlay_unavailable:{overlay_error}")
    for row in rows:
        if not _RULE_ID_RE.fullmatch(row.rule_id):
            errors.append(f"matrix_rule_id_invalid:{row.rule_id}")
        if row.rule_id in seen_ids:
            errors.append(f"matrix_rule_id_duplicate:{row.rule_id}")
        seen_ids.add(row.rule_id)
        if row.status not in _VALID_STATUSES:
            errors.append(f"matrix_status_invalid:{row.rule_id}:{row.status}")
        if row.evidence_state not in _VALID_EVIDENCE_STATES:
            errors.append(
                f"matrix_evidence_state_invalid:{row.rule_id}:{row.evidence_state}"
            )
        if row.status == CURRENT_STATUS and row.evidence_state == HISTORICAL:
            errors.append(f"matrix_current_row_is_historical:{row.rule_id}")
        if row.status == SUPERSEDED_STATUS and not row.historical_reason.strip():
            errors.append(f"matrix_superseded_reason_missing:{row.rule_id}")
        if not row.coverage:
            errors.append(f"matrix_coverage_missing:{row.rule_id}")
        coverage.update(row.coverage)
        required_collections = (
            ("authority", row.authority),
            ("production_owners", row.production_owners),
            ("dynamic_gates", row.dynamic_gates),
            ("prompts", row.prompts),
            ("positive_tests", row.positive_tests),
            ("negative_tests", row.negative_tests),
        )
        for name, values in required_collections:
            if not values:
                errors.append(f"matrix_{name}_missing:{row.rule_id}")
        if "→" not in row.producer_consumer or len(row.producer_consumer.strip()) < 12:
            errors.append(f"matrix_producer_consumer_invalid:{row.rule_id}")
        if len(row.fail_closed.strip()) < 24:
            errors.append(f"matrix_fail_closed_missing:{row.rule_id}")

        for ref in row.authority:
            errors.extend(_validate_ref(ref, row_id=row.rule_id, field="authority"))
        for ref in row.production_owners:
            errors.extend(_validate_ref(ref, row_id=row.rule_id, field="owner"))
        for ref in row.dynamic_gates:
            errors.extend(
                _validate_ref(
                    ref,
                    row_id=row.rule_id,
                    field="dynamic_gate",
                    require_symbol=True,
                )
            )
        for binding in row.prompts:
            if not binding.role.strip():
                errors.append(f"matrix_prompt_role_missing:{row.rule_id}")
            errors.extend(
                _validate_ref(
                    binding.renderer,
                    row_id=row.rule_id,
                    field="prompt_renderer",
                    require_symbol=True,
                )
            )
            if not binding.templates:
                errors.append(f"matrix_prompt_template_missing:{row.rule_id}:{binding.role}")
            for template in binding.templates:
                errors.extend(
                    _validate_ref(
                        template,
                        row_id=row.rule_id,
                        field="prompt_template",
                    )
                )
        if row.status == CURRENT_STATUS:
            supplied_roles = {binding.role for binding in row.prompts}
            missing_roles = sorted(REQUIRED_PROMPT_ROLES - supplied_roles)
            if missing_roles:
                errors.append(
                    f"matrix_current_prompt_roles_missing:{row.rule_id}:"
                    + ",".join(missing_roles)
                )
            if not row.prompt_statement.strip():
                errors.append(f"matrix_prompt_statement_missing:{row.rule_id}")
            if not row.prompt_required_terms:
                errors.append(f"matrix_prompt_required_terms_missing:{row.rule_id}")
            normalized_statement = row.prompt_statement.casefold()
            seen_prompt_terms: set[str] = set()
            for raw_term in row.prompt_required_terms:
                term = raw_term.strip()
                normalized_term = term.casefold()
                if not term:
                    errors.append(f"matrix_prompt_required_term_blank:{row.rule_id}")
                    continue
                if normalized_term in seen_prompt_terms:
                    errors.append(
                        f"matrix_prompt_required_term_duplicate:{row.rule_id}:{term}"
                    )
                    continue
                seen_prompt_terms.add(normalized_term)
                if normalized_term not in normalized_statement:
                    errors.append(
                        f"matrix_prompt_statement_term_missing:{row.rule_id}:{term}"
                    )
            if overlay is not None:
                for binding in row.prompts:
                    renderer_source = _renderer_callable_source(binding.renderer)
                    if renderer_source is None or not re.search(
                        r"\bcurrent_strict_runtime_prompt_overlay\s*\(",
                        renderer_source,
                    ):
                        errors.append(
                            "matrix_prompt_renderer_overlay_missing:"
                            f"{row.rule_id}:{binding.role}"
                        )
                    material = _prompt_material(binding, overlay)
                    if material is None:
                        continue
                    normalized_material = material.casefold()
                    for raw_term in row.prompt_required_terms:
                        term = raw_term.strip()
                        if term and term.casefold() not in normalized_material:
                            errors.append(
                                "matrix_prompt_rendered_term_missing:"
                                f"{row.rule_id}:{binding.role}:{term}"
                            )
        for test_id in row.positive_tests:
            errors.extend(_validate_test_id(test_id, row_id=row.rule_id, polarity="positive"))
        for test_id in row.negative_tests:
            errors.extend(_validate_test_id(test_id, row_id=row.rule_id, polarity="negative"))

        if row.status == CURRENT_STATUS:
            for ref in _row_refs(row):
                if _is_archive_path(ref.path):
                    errors.append(
                        f"matrix_current_archive_reference:{row.rule_id}:{ref.display()}"
                    )
            for test_id in (*row.positive_tests, *row.negative_tests):
                path = test_id.split("::", 1)[0]
                if _is_archive_path(path):
                    errors.append(
                        f"matrix_current_archive_test_reference:{row.rule_id}:{test_id}"
                    )
            for field, text in (
                ("producer_consumer", row.producer_consumer),
                ("fail_closed", row.fail_closed),
                ("historical_reason", row.historical_reason),
                ("prompt_statement", row.prompt_statement),
            ):
                normalized = str(text).replace("\\", "/").lower()
                if "archive/" in normalized or "docs/archive" in normalized:
                    errors.append(
                        f"matrix_current_archive_text_reference:{row.rule_id}:{field}"
                    )

    missing_coverage = sorted(REQUIRED_COVERAGE - coverage)
    errors.extend(f"matrix_required_coverage_missing:{item}" for item in missing_coverage)

    handshake_rows = [
        row for row in rows if row.rule_id == _RAW_NAME_HANDSHAKE_RULE_ID
    ]
    if len(handshake_rows) != 1:
        errors.append(
            "matrix_raw_name_handshake_rule_missing_or_ambiguous:"
            f"count={len(handshake_rows)}"
        )
    else:
        handshake = handshake_rows[0]
        if handshake.status != CURRENT_STATUS or handshake.evidence_state != SOURCE_CONTRACT:
            errors.append("matrix_raw_name_handshake_not_current_source_contract")
        if "raw_tcp_name_handshake" not in handshake.coverage:
            errors.append("matrix_raw_name_handshake_coverage_missing")
        if not handshake.prompt_statement.strip():
            errors.append("matrix_raw_name_handshake_prompt_statement_missing")
        body = " ".join((
            handshake.prompt_statement,
            handshake.producer_consumer,
            handshake.fail_closed,
        )).lower()
        for term in _RAW_NAME_HANDSHAKE_REQUIRED_TERMS:
            if term not in body:
                errors.append(f"matrix_raw_name_handshake_semantics_missing:{term}")
        for test_id in sorted(
            _RAW_NAME_HANDSHAKE_REQUIRED_POSITIVE_TESTS
            - set(handshake.positive_tests)
        ):
            errors.append(f"matrix_raw_name_handshake_positive_missing:{test_id}")
        for test_id in sorted(
            _RAW_NAME_HANDSHAKE_REQUIRED_NEGATIVE_TESTS
            - set(handshake.negative_tests)
        ):
            errors.append(f"matrix_raw_name_handshake_negative_missing:{test_id}")

    quality_rows = [
        row for row in rows if row.rule_id == _QUALITY_RUNTIME_IDENTITY_RULE_ID
    ]
    if len(quality_rows) != 1:
        errors.append(
            "matrix_quality_runtime_identity_rule_missing_or_ambiguous:"
            f"count={len(quality_rows)}"
        )
    else:
        quality = quality_rows[0]
        if quality.status != CURRENT_STATUS or quality.evidence_state != SOURCE_CONTRACT:
            errors.append("matrix_quality_runtime_identity_not_current_source_contract")
        owner_symbols = {ref.display() for ref in quality.production_owners}
        for owner in sorted(
            set(_QUALITY_RUNTIME_IDENTITY_REQUIRED_OWNER_SYMBOLS) - owner_symbols
        ):
            errors.append(f"matrix_quality_runtime_identity_owner_missing:{owner}")
        body = " ".join((
            quality.prompt_statement,
            quality.producer_consumer,
            quality.fail_closed,
        )).casefold()
        for term in _QUALITY_RUNTIME_IDENTITY_REQUIRED_TERMS:
            if term.casefold() not in body:
                errors.append(
                    f"matrix_quality_runtime_identity_semantics_missing:{term}"
                )
        for test_id in sorted(
            set(_QUALITY_RUNTIME_IDENTITY_REQUIRED_POSITIVE_TESTS)
            - set(quality.positive_tests)
        ):
            errors.append(
                f"matrix_quality_runtime_identity_positive_missing:{test_id}"
            )
        for test_id in sorted(
            set(_QUALITY_RUNTIME_IDENTITY_REQUIRED_NEGATIVE_TESTS)
            - set(quality.negative_tests)
        ):
            errors.append(
                f"matrix_quality_runtime_identity_negative_missing:{test_id}"
            )
    return sorted(set(errors))


def render_current_matrix_markdown(
    rows: Sequence[MatrixRow] = CURRENT_ALIGNMENT_ROWS,
) -> str:
    """Render the checked-in current view used by the human matrix document."""

    lines = [
        "<!-- executable-national-alignment-matrix:begin -->",
        "## Executable current-contract registry (generated)",
        "",
        "This block is generated from `web/core/national_alignment_matrix.py` "
        f"(schema {MATRIX_SCHEMA_VERSION}) and is regression-checked.  `source_contract` "
        "verifies source paths/symbols/test anchors only; `runtime_pending` is not a "
        "runtime, certificate, or strength claim. `current` means an active requirement, "
        "and only `superseded` rows may point at historical archive material.",
        "",
    ]
    for row in rows:
        lines.extend((
            f"### `{row.rule_id}` — {row.status} / {row.evidence_state}",
            "",
            "- Authority/source: " + "; ".join(f"`{ref.display()}`" for ref in row.authority),
            "- Production owner: " + "; ".join(
                f"`{ref.display()}`" for ref in row.production_owners
            ),
            "- Dynamic gate: " + "; ".join(
                f"`{ref.display()}`" for ref in row.dynamic_gates
            ),
            "- Prompt renderer/template: " + "; ".join(
                f"{binding.role}=`{binding.renderer.display()}` → "
                + ", ".join(f"`{template.display()}`" for template in binding.templates)
                for binding in row.prompts
            ),
            *(
                (f"- Prompt statement: {row.prompt_statement}",)
                if row.prompt_statement
                else ()
            ),
            f"- Producer → consumer: {row.producer_consumer}",
            "- Positive regression: " + "; ".join(f"`{value}`" for value in row.positive_tests),
            "- Negative regression: " + "; ".join(f"`{value}`" for value in row.negative_tests),
            f"- Fail-closed: {row.fail_closed}",
        ))
        if row.historical_reason:
            lines.append(f"- Superseded reason: {row.historical_reason}")
        lines.append("")
    lines.append("<!-- executable-national-alignment-matrix:end -->")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":  # pragma: no cover - operator/doc helper
    issues = validate_alignment_matrix()
    if issues:
        raise SystemExit("\n".join(issues))
    print(render_current_matrix_markdown(), end="")
