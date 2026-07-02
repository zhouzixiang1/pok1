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
        description="Botzone JSON contract and national TCP wire-compatibility boundaries.",
        required_spot_fields=("legal_actions", "raise_min", "raise_max", "to_call"),
        forbidden_patterns=("tcp_text_stdout", "wire_bet_token", "positive_allin"),
        gate_metrics=("protected_contract_ok", "national_protocol_ok", "national_acceptance_ok"),
    ),
    "action_sanitizer": SkillLayer(
        layer_id="action_sanitizer",
        poker_skill_ref="P1 action grounding",
        description="Integer action encoding, raise-to-total legality, all-in representation, call/check mapping.",
        required_spot_fields=("legal_actions", "raise_min", "raise_max", "to_call", "street"),
        forbidden_patterns=("raise_by_increment", "below_min_raise", "postflop_check_check"),
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
        gate_metrics=("h2h_delta", "exploitability_delta"),
    ),
    "telemetry": SkillLayer(
        layer_id="telemetry",
        poker_skill_ref="Evaluation harness",
        description="Logging, placement probes, gate observability, and per-layer attribution.",
        required_spot_fields=("skill_layer", "street", "action_family"),
        gate_metrics=("telemetry_fidelity_ok", "reachability_ok"),
    ),
    "adapter": SkillLayer(
        layer_id="adapter",
        poker_skill_ref="Protocol bridge",
        description="National TCP adapter card/action conversion and THP-facing compliance.",
        required_spot_fields=("street", "position", "legal_actions"),
        forbidden_patterns=("suit_mapping_reuse", "wire_bet_token", "check_check_after_postflop_check"),
        gate_metrics=("national_acceptance_ok", "adapter_telemetry_clean"),
    ),
    "map_elites": SkillLayer(
        layer_id="map_elites",
        poker_skill_ref="Evolution archive",
        description="Behavior niche exploration and frontier diversity.",
        required_spot_fields=("niche_id", "behavior_fingerprint"),
        gate_metrics=("coverage", "niche_elite_score"),
    ),
    "novelty": SkillLayer(
        layer_id="novelty",
        poker_skill_ref="Evolution archive",
        description="Safe exploration of non-dominant but diverse candidate mechanisms.",
        required_spot_fields=("niche_id", "parent_ids", "diff_hash"),
        gate_metrics=("selection_score", "children_count"),
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
