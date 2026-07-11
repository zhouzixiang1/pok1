"""Versioned, local strategy cards for national-native generations.

The evolution agents already receive broad poker prose, replay summaries, and
web-research hypotheses.  That is useful for an expert planner but too open
ended for a weaker model: it can relabel a discarded dictionary lookup as a
"state-learning" innovation.  This module is the small, source-controlled
middle layer between research and code.  A card is a falsifiable implementation
recipe, not a policy table and not a runtime decision engine.

Cards deliberately describe *how to use* live state and bounded computation.
They never ship a hand-strength policy that an LLM could blindly copy.  Strategy
code remains stdlib-only and owns its own tested action logic.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Any


REFERENCE_PACK_VERSION = "national-native-reference-pack-v1"


@dataclass(frozen=True)
class StrategyReferenceCard:
    """One bounded, implementation-ready strategy recipe.

    ``required_*`` fields are deliberately machine-readable enough for plan
    validation.  They are not a claim that a card is automatically profitable;
    local native precommit remains the strategy authority.
    """

    reference_id: str
    title: str
    primary_innovations: tuple[str, ...]
    purpose: str
    required_hand_runtime_fields: tuple[str, ...]
    required_any_opponent_runtime_fields: tuple[str, ...]
    allowed_files: tuple[str, ...]
    required_worker_terms: tuple[str, ...]
    expected_action_family: str
    counterfactual: str
    bounded_work: str
    table_boundary: str
    forbidden_axes: tuple[str, ...]
    sources: tuple[str, ...]

    def digest(self) -> str:
        return _digest(asdict(self))


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


_CARDS: tuple[StrategyReferenceCard, ...] = (
    StrategyReferenceCard(
        reference_id="lead_sizing_geometry_v1",
        title="SPR-aware proactive lead sizing",
        primary_innovations=("bounded_precompute_lookup",),
        purpose=(
            "Use bounded pure facts only to accelerate a live, stack-aware lead-sizing "
            "decision.  Select a legal raise-to family that builds a geometric pot "
            "toward a value/bluff river plan; do not retune defensive call/fold margins."
        ),
        required_hand_runtime_fields=(
            "hand_runtime.street",
            "hand_runtime.spr",
            "hand_runtime.pot",
            "hand_runtime.effective_stack",
            "hand_runtime.hero_position",
            "hand_runtime.preflop_aggressor",
            "hand_runtime.street_open",
        ),
        required_any_opponent_runtime_fields=(
            "opponent_runtime.terminal_response.confidence",
            "opponent_runtime.showdown_range.confidence",
        ),
        allowed_files=("strategy.py", "postflop.py", "precompute.py", "simulation.py"),
        required_worker_terms=(
            "lead_sizing_geometry_v1",
            "hand_runtime",
            "opponent_runtime",
            "sanitized action",
            "control",
        ),
        expected_action_family="raise_to",
        counterfactual=(
            "On the same legal lead spot, changing only SPR or the confidence-scaled "
            "terminal/showdown input must change the final sanitized raise-to family."
        ),
        bounded_work=(
            "Publish the legal baseline under 250 ms.  Any lookup grid is immutable, "
            "module-import bounded, and cannot construct ranges or tables in get_action."
        ),
        table_boundary=(
            "A table may encode pure card/texture buckets or compact sizing families; "
            "its lookup key must vary across trusted probe states and it may not be "
            "a static policy keyed by a constant or replace live SPR/state inputs."
        ),
        forbidden_axes=(
            "preflop threshold tuning",
            "reactive call/fold margin tuning",
            "defensive polarized-jam gate",
        ),
        sources=(
            "DeepStack (continual resolving and public belief): arXiv:1701.01724",
            "Depth-Limited Solving for Imperfect-Information Games: arXiv:1805.08195",
        ),
    ),
    StrategyReferenceCard(
        reference_id="range_weighted_candidate_batch_v1",
        title="Range-weighted bounded candidate batch",
        primary_innovations=("sample_counted_candidate_batch",),
        purpose=(
            "Use precomputed pure card facts to generate a finite candidate set, then "
            "spend deadline budget only on high-uncertainty legal actions weighted by "
            "the bounded terminal/showdown posterior."
        ),
        required_hand_runtime_fields=(
            "hand_runtime.street",
            "hand_runtime.pot",
            "hand_runtime.to_call",
            "hand_runtime.pot_odds",
            "hand_runtime.spr",
        ),
        required_any_opponent_runtime_fields=(
            "opponent_runtime.terminal_response.confidence",
            "opponent_runtime.showdown_range.confidence",
        ),
        allowed_files=("strategy.py", "simulation.py", "precompute.py", "opponent.py"),
        required_worker_terms=(
            "range_weighted_candidate_batch_v1",
            "sample_count",
            "deadline",
            "opponent_runtime",
            "control",
        ),
        expected_action_family="any_legal_action",
        counterfactual=(
            "With a fixed seed, a longer budget or a changed bounded posterior must "
            "perform more trusted work and change at least one final sanitized action."
        ),
        bounded_work=(
            "The baseline is legal before refinement; each refinement iterator has a "
            "finite cap, monotonic deadline check, and system-observed work count."
        ),
        table_boundary=(
            "Precompute only pure facts such as canonical hole/board/range buckets. "
            "Lookup keys must vary across trusted probe states; do not build "
            "1,326-combo ranges, deck combinations, or file-backed caches per decision."
        ),
        forbidden_axes=(
            "unbounded Monte Carlo",
            "full request history rescans",
            "candidate-reported work counters as proof",
        ),
        sources=(
            "DeepStack (continual resolving and public belief): arXiv:1701.01724",
            "OpenSpiel CFR/External-Sampling CFR reference documentation",
        ),
    ),
)

_BY_ID = {card.reference_id: card for card in _CARDS}


def reference_pack_ids() -> tuple[str, ...]:
    return tuple(card.reference_id for card in _CARDS)


def get_reference_card(reference_id: str | None) -> StrategyReferenceCard | None:
    return _BY_ID.get(str(reference_id or "").strip())


def default_reference_pack_id(primary: str | None) -> str:
    """Return the one deterministic fallback card for a work primitive.

    This is used only by system-generated repair contracts.  Normal Master
    plans still choose an explicit id and are rejected if it does not match.
    Keeping the fallback here prevents scattered hard-coded ids from silently
    drifting away from the registry.
    """
    primary_text = str(primary or "")
    for card in _CARDS:
        if primary_text in card.primary_innovations:
            return card.reference_id
    return ""


def reference_pack_registry_digest() -> str:
    return _digest({
        "version": REFERENCE_PACK_VERSION,
        "cards": [asdict(card) for card in _CARDS],
    })


def validate_reference_selection(reference_id: str | None, primary: str | None) -> list[str]:
    """Return deterministic plan-time errors for a selected primary/card pair."""
    card = get_reference_card(reference_id)
    if card is None:
        return [
            "strategy_reference_pack_unknown: "
            f"{reference_id!r}; expected one of {list(reference_pack_ids())}"
        ]
    if str(primary or "") not in card.primary_innovations:
        return [
            "strategy_reference_pack_primary_mismatch: "
            f"{card.reference_id} cannot support {primary!r}"
        ]
    return []


def validate_reference_task(
    reference_id: str | None,
    primary: str | None,
    *,
    target_files: list[str] | tuple[str, ...] | None = None,
    worker_prompt: str = "",
) -> list[str]:
    """Validate the parts that only exist at WorkerTask scope."""
    errors = validate_reference_selection(reference_id, primary)
    card = get_reference_card(reference_id)
    if card is None:
        return errors
    target_names = {
        str(item).replace("\\", "/").rsplit("/", 1)[-1]
        for item in target_files or []
        if str(item).strip()
    }
    if target_names and not target_names.intersection(card.allowed_files):
        errors.append(
            "strategy_reference_pack_target_files_mismatch: "
            f"{card.reference_id} needs one of {list(card.allowed_files)}, got {sorted(target_names)}"
        )
    lowered = str(worker_prompt or "").lower()
    missing = [term for term in card.required_worker_terms if term.lower() not in lowered]
    if missing:
        errors.append(
            "strategy_reference_pack_worker_terms_missing: "
            f"{card.reference_id}: {missing}"
        )
    return errors


def master_reference_summary() -> str:
    """Small card index for Master, intentionally far shorter than a survey."""
    lines = [
        "Local, versioned strategy reference cards (source-controlled; choose a card "
        "only for a work-primitive state_learning primary):",
        f"- registry_version={REFERENCE_PACK_VERSION}; registry_digest={reference_pack_registry_digest()}",
    ]
    for card in _CARDS:
        lines.extend((
            f"- {card.reference_id}: {card.title}. primary={list(card.primary_innovations)}; "
            f"action={card.expected_action_family}.",
            f"  required live hand fields={list(card.required_hand_runtime_fields)}; "
            f"one opponent field={list(card.required_any_opponent_runtime_fields)}.",
            f"  allowed files={list(card.allowed_files)}.",
            f"  required worker literals={list(card.required_worker_terms)}.",
            f"  proof={card.counterfactual}",
            f"  bound={card.bounded_work}",
        ))
    lines.append(
        "Foundation-only tables (HOLE_COMBO_FACTS, STRAIGHT_HIGH_BY_MASK, "
        "FIVE_OF_SEVEN_INDICES) are acceleration facts, never a state-learning "
        "primary by themselves."
    )
    return "\n".join(lines)


def worker_reference_card(reference_id: str | None) -> str:
    card = get_reference_card(reference_id)
    if card is None:
        return ""
    return "\n".join((
        "# Binding Local Strategy Reference Card",
        f"- id={card.reference_id}; registry_digest={reference_pack_registry_digest()}; "
        f"card_digest={card.digest()}",
        f"- Purpose: {card.purpose}",
        "- Required live hand fields: " + ", ".join(card.required_hand_runtime_fields),
        "- Required one-of opponent fields: " + ", ".join(card.required_any_opponent_runtime_fields),
        "- Required worker literals: " + ", ".join(card.required_worker_terms),
        f"- Expected action family: {card.expected_action_family}",
        f"- Counterfactual proof: {card.counterfactual}",
        f"- Bounded-work rule: {card.bounded_work}",
        f"- Table boundary: {card.table_boundary}",
        "- Forbidden axes: " + ", ".join(card.forbidden_axes),
        "This card is binding. Do not replace it with a static constant-key table, "
        "an unreachable helper, or a defensive threshold edit.",
    ))
