"""Offline poker skill-library metadata for prompts, harnesses, and gates.

This module intentionally does not provide runtime poker decisions. It is the
shared vocabulary that lets prompts, decision scenarios, candidate records, and
quality reports agree on what a generation is trying to improve.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SkillLayer:
    layer_id: str
    poker_skill_ref: str
    description: str
    required_spot_fields: tuple[str, ...] = ()
    forbidden_patterns: tuple[str, ...] = ()
    gate_metrics: tuple[str, ...] = ()
    example_scenarios: tuple[str, ...] = ()


SKILL_LAYERS: dict[str, SkillLayer] = {
    "protocol": SkillLayer(
        layer_id="protocol",
        poker_skill_ref="P1 rules/output format",
        description="Official delimiter-free raw-TCP stream decoding, state transitions, and wire-action boundaries.",
        required_spot_fields=("legal_actions", "raise_min", "raise_max", "to_call"),
        forbidden_patterns=("newline_framing", "wire_bet_token", "unsolicited_action"),
        gate_metrics=("national_native_contract_ok", "national_protocol_ok", "national_acceptance_ok"),
    ),
    "action_intent": SkillLayer(
        layer_id="action_intent",
        poker_skill_ref="P1 typed action intent",
        description="Policy selection among pass/fold/all-in/exact raise-to intents; the system socket owner alone maps and validates wire actions.",
        required_spot_fields=("legal_actions", "raise_min", "raise_max", "to_call", "street"),
        forbidden_patterns=("raise_by_increment", "below_min_raise", "direct_call_check_intent", "candidate_wire_send"),
        gate_metrics=("decision_pass_rate", "clamped_raises", "allin_conversions"),
    ),
    "preflop_range": SkillLayer(
        layer_id="preflop_range",
        poker_skill_ref="P2 preflop ranges",
        description="Opening, defending, blind-vs-blind, and limp/open response ranges.",
        required_spot_fields=("position", "to_call", "stack", "line_template"),
        gate_metrics=("preflop_pass_rate", "vpip", "threebet_rate"),
    ),
    "bb_vs_limp": SkillLayer(
        layer_id="bb_vs_limp",
        poker_skill_ref="P2 preflop blind defense",
        description="Big-blind response after small-blind limp: check, iso-raise, and trap ranges.",
        required_spot_fields=("position", "to_call", "stack", "preflop_spot", "line_template"),
        gate_metrics=("preflop_pass_rate", "bb_vs_limp_raise_rate", "bb_vs_limp_ev"),
    ),
    "bb_vs_open": SkillLayer(
        layer_id="bb_vs_open",
        poker_skill_ref="P2 preflop blind defense",
        description="Big-blind defense versus small-blind open/raise: fold, call, 3-bet, and shove ranges.",
        required_spot_fields=("position", "to_call", "stack", "preflop_spot", "raise_size"),
        gate_metrics=("preflop_pass_rate", "bb_defend_rate", "threebet_rate"),
    ),
    "texture": SkillLayer(
        layer_id="texture",
        poker_skill_ref="P3 postflop principles",
        description="Board texture, made-hand tiering, draws, nutted risk, and equity realization.",
        required_spot_fields=("street", "board_texture", "made_strength", "draw_strength"),
        gate_metrics=("postflop_pass_rate", "showdown_wr"),
    ),
    "spr": SkillLayer(
        layer_id="spr",
        poker_skill_ref="P3/P4 stack-to-pot commitment",
        description="SPR, pot odds, stack-off gates, and call/fold commitment thresholds.",
        required_spot_fields=("street", "pot", "to_call", "stack", "spr_bucket"),
        gate_metrics=("spr_pass_rate", "allin_adjusted_ev"),
    ),
    "blocker": SkillLayer(
        layer_id="blocker",
        poker_skill_ref="P4 targeted ATT/DEF",
        description="Blocker-aware bluffing, bluff catching, and value-selection.",
        required_spot_fields=("street", "board_texture", "hole_cards", "line_template"),
        gate_metrics=("river_pass_rate", "bluff_catch_rate"),
    ),
    "line_template": SkillLayer(
        layer_id="line_template",
        poker_skill_ref="P4 targeted strategy",
        description="Action-line templates across streets and position-aware response families.",
        required_spot_fields=("street", "position", "line_template", "legal_actions"),
        gate_metrics=("line_pass_rate", "street_action_mix"),
    ),
    "opponent_model": SkillLayer(
        layer_id="opponent_model",
        poker_skill_ref="P4 opponent-conditioned strategy",
        description="Per-opponent aggression, fold, showdown, and sizing adaptation.",
        required_spot_fields=("opponent_profile", "street", "line_template"),
        gate_metrics=("h2h_delta", "opponent_profile_consumed"),
    ),
    "runtime_architecture": SkillLayer(
        layer_id="runtime_architecture",
        poker_skill_ref="National native runtime",
        description="Decision-time budgets, bounded work, fallbacks, and diagnostics for the 60-second official action window.",
        required_spot_fields=("decision_ms", "fallback_reason", "skill_layer"),
        forbidden_patterns=("unbounded_loop", "decision_file_io", "stdout_debug"),
        gate_metrics=("national_capability_contract_ok", "decision_latency_p95", "trace_decision_coverage"),
    ),
    "precompute": SkillLayer(
        layer_id="precompute",
        poker_skill_ref="Space-for-time lookup",
        description="Bounded immutable lookup tables and caches for pure card, texture, range, and evaluator facts.",
        required_spot_fields=("cache_name", "max_entries", "build_phase"),
        forbidden_patterns=("build_large_table_in_policy_decision", "runtime_file_cache", "uncapped_cache_growth"),
        gate_metrics=("import_time_ms", "decision_latency_p95", "cache_hit_rate"),
    ),
    "match_memory": SkillLayer(
        layer_id="match_memory",
        poker_skill_ref="Persistent 70-hand memory",
        description="Incremental opponent and match-state tracking that persists across hands and resets on a new TCP connection.",
        required_spot_fields=("opponent_profile", "hand_index", "reset_boundary"),
        forbidden_patterns=("full_history_scan_per_action", "match_state_leak_across_connection"),
        gate_metrics=("incremental_model_updates", "opponent_profile_consumed", "h2h_delta"),
    ),
    "telemetry": SkillLayer(
        layer_id="telemetry",
        poker_skill_ref="Evaluation harness",
        description="Logging, placement probes, gate observability, and per-layer attribution.",
        required_spot_fields=("skill_layer", "street", "action_family"),
        gate_metrics=("telemetry_fidelity_ok", "reachability_ok"),
    ),
    "native_tcp": SkillLayer(
        layer_id="native_tcp",
        poker_skill_ref="National policy/context boundary",
        description="Consumption of the system-owned national decision context and typed action-intent ABI.",
        required_spot_fields=("street", "position", "legal_actions", "to_call"),
        forbidden_patterns=("candidate_socket_access", "json_transport", "wire_bet_token", "candidate_action_sanitizer"),
        gate_metrics=("national_native_contract_ok", "national_acceptance_ok", "native_tcp_smoke_ok"),
    ),
    "novelty": SkillLayer(
        layer_id="novelty",
        poker_skill_ref="Frozen structural proposal contract",
        description=(
            "Falsifiable exploration identified by a frozen proposal, reachable "
            "producer-to-consumer call chain, control, and socket-intent evidence."
        ),
        required_spot_fields=(
            "proposal_id",
            "structural_call_chain",
            "falsifier",
            "consumer_trace_digest",
        ),
        forbidden_patterns=(
            "mutable_evolution_archive",
            "niche_child_count_authority",
            "threshold_only_novelty",
        ),
        gate_metrics=(
            "structural_diff_present",
            "consumer_trace_verified",
            "counterfactual_intent_changed",
        ),
    ),
}


def valid_skill_layers() -> set[str]:
    return set(SKILL_LAYERS)


def normalize_skill_layer(layer: str | None) -> str:
    text = (layer or "").strip()
    return text if text in SKILL_LAYERS else ""


def describe_skill_layers(layers: list[str] | tuple[str, ...] | None = None) -> str:
    selected = layers or tuple(SKILL_LAYERS)
    lines = []
    for layer in selected:
        spec = SKILL_LAYERS.get(layer)
        if not spec:
            continue
        fields = ", ".join(spec.required_spot_fields) or "none"
        metrics = ", ".join(spec.gate_metrics) or "none"
        lines.append(
            f"- {spec.layer_id}: {spec.description} "
            f"(ref={spec.poker_skill_ref}; spot_fields={fields}; gate_metrics={metrics})"
        )
    return "\n".join(lines)


def scenario_skill_metadata(scenario: dict[str, Any]) -> dict[str, Any]:
    layer = normalize_skill_layer(scenario.get("skill_layer"))
    spec = SKILL_LAYERS.get(layer)
    if not spec:
        return {"skill_layer": layer or "unspecified", "missing_required_fields": []}
    missing = [field for field in spec.required_spot_fields if field not in scenario]
    return {
        "skill_layer": layer,
        "poker_skill_ref": spec.poker_skill_ref,
        "missing_required_fields": missing,
        "gate_metrics": list(spec.gate_metrics),
    }
