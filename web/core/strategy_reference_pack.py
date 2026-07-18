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


REFERENCE_PACK_VERSION = "national-tcp-policy-reference-pack-v6"
UNAVAILABLE_PRIMARY_INNOVATIONS = {
    "bounded_precompute_lookup": (
        "system precompute is admitted only as a read-only consumer dependency, "
        "not a standalone candidate innovation; pair it with an executable "
        "sample_counted_candidate_batch card and its live wire falsifier"
    ),
}


def current_strict_runtime_prompt_overlay() -> str:
    """Return the source-owned current runtime contract for provider prompts.

    Prompt templates are role-specific and intentionally contain different
    amounts of strategy detail.  This compact overlay is injected by every
    decision-changing or reviewing role renderer so the final provider-visible
    material has one unambiguous current baseline, timing, launch, and evidence
    boundary.  It is policy guidance only: deterministic runtime and quality
    gates remain the enforcement authority.
    """

    return (
        "# SYSTEM-OWNED CURRENT STRICT RUNTIME ALIGNMENT\n"
        "- On a valid board, the synchronous baseline is fixed deterministic "
        "192/256/96 flop/turn/river sampling, with two direct "
        "`precompute.evaluate_seven` calls per sample. The dynamic quality "
        "gate fail-closes above its 800 top-level evaluator-call cap.\n"
        "- `call` and `check` may occur only as reducer-provided public-state input. "
        "Candidate policy returns a typed intent object (`pass`, `fold`, `allin`, "
        "or `raise` with `raise_to`); never return a bare wire string or integer.\n"
        "- The baseline uses the direct system evaluator only. Imported, "
        "closure, default, or value aliases; `itertools.combinations`; and "
        "nested deck-pair sweeps are rejected from that path. Full `C(45,2)` "
        "remaining-opponent enumeration is refinement-only: bounded, checked "
        "against a monotonic deadline, and permitted only after a legal baseline "
        "has been published.\n"
        "- The official `name` handshake has launch initiated before preflop and "
        "starts the system-owned worker. It is not readiness proof: the first "
        "decision clock includes unfinished policy import; never claim ready, "
        "reset that clock, or waive its gate.\n"
        "- Native TCP precommit uses a 200 ms baseline target. The 250 ms formal "
        "ceiling remains independently binding; neither timing boundary is a "
        "candidate strategy knob.\n"
        "- Quality, precommit, commit, and formal certificate admission bind one "
        "schema-2 composite runtime identity for system-owned `national_bot.py` "
        "and `precompute.py`: exact SHA-256/size for both plus `combined_digest`. "
        "Missing, malformed, mismatched, or precompute-only drift is stale and "
        "fails closed: refresh quality; do not reuse precommit or certify.\n"
        "- The bot directory has exactly five executable/identity files. A model "
        "or packed table is never candidate-owned or path-loaded: it is available "
        "only after a system-owned, content-bound asset ABI binds its registry/"
        "issuance receipt, manifest, byte/query caps, no-follow read-only broker, "
        "all launch-path resolver, and observed influence probe. Until then, no "
        "external asset is available to policy.\n"
        "- A normal full certification request must rebind its current admission before becoming "
        "a durable job, before queued/retry queue claim, pre-Popen worker spawn, worker claim, "
        "and EXE work. A stale admission is not an operator or model judgment call, is a "
        "quality failure rather than infrastructure retry, and never authorizes a worker.\n"
        "- Transient UI status is an authority-gated, non-authoritative display projection: "
        "it must bind the exact live task owner and monotonic task lifecycle revision as well "
        "as the checkpoint identity. A replaced, missing, stale-revision, or mismatched task "
        "owner is dropped rather than shown as current work.\n"
        "- Evidence and history are limited to the role's typed, content-bound, "
        "current-generation inputs. Archive/legacy source, ratings, replays, "
        "lessons, experience, and mutable live result files are quarantined and "
        "cannot be prompt authority.\n"
        "- A canonical abandon is terminal only after its routed owner returns a "
        "complete proof bound to one explicit ToolUse id, owner, arguments, and "
        "same provider attempt. A missing checkpoint, historical receipt, or "
        "unbound cache never authorizes a successor.\n"
        "- A selected Master proposal is system-bound by its `proposal_id` and "
        "`contract_digest`. A compiler-owned task brief may carry its full text, "
        "but it is never a sixth Bot artifact, certificate, or recovery receipt; "
        "no role may substitute a different proposal. Duplicate final metadata is "
        "derived from that selected proposal, and the deterministic Worker hard cap "
        "remains binding.\n"
        "- Strict Master, Review, and Critic authority receipts are append-only "
        "system evidence. Current construction owns the exact accepted prefix; a "
        "stored Master or Review receipt may observe only its canonical, strictly "
        "later Review/Critic suffix. No role may author, reorder, duplicate, or "
        "waive this journal evidence.\n"
        "- A Master Scout falsifier is a closed six-key object; "
        "mechanism_target appears only at the top level. A shared leaf in an "
        "executable claim must be complete owner-qualified or inside an exact "
        "selected-root allowlisted list; bare, punctuation, compact, foreign, and "
        "unknown-child forms fail closed. Fresh bootstrap measurement keeps a closed "
        "six-field shape and is system-bound rather than a strength claim.\n"
        "- A Master proposal Scout emits one raw JSON object. Only a sealed "
        "schema/distinctness repair may recover one unambiguous non-JSON prefix "
        "followed by one object with no trailing prose; initial Scouts and other "
        "roles keep the existing parser, and no third attempt exists.\n"
        "- Deterministic protocol, runtime, and quality gates enforce these facts; "
        "a prompt role may report them but cannot relax them."
    )


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
    required_decision_context_fields: tuple[str, ...]
    required_any_decision_context_fields: tuple[str, ...]
    allowed_files: tuple[str, ...]
    required_worker_terms: tuple[str, ...]
    expected_action_family: str
    reachable_call_chain: tuple[str, ...]
    consumer_trace: str
    falsifier: str
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
        reference_id="range_weighted_candidate_batch_v1",
        title="Range-weighted bounded candidate batch",
        primary_innovations=("sample_counted_candidate_batch",),
        purpose=(
            "Use precomputed pure card facts to generate a finite candidate set, then "
            "spend deadline budget only on high-uncertainty legal actions weighted by "
            "the bounded terminal/showdown posterior."
        ),
        required_decision_context_fields=(
            "hand.street",
            "betting.pot",
            "betting.to_call",
            "betting.pot_odds",
            "betting.spr",
            "legal.policy_kinds",
        ),
        required_any_decision_context_fields=(
            "opponent.terminal_response.confidence",
            "opponent.showdown_range.confidence",
        ),
        allowed_files=("policy.py",),
        required_worker_terms=(
            "range_weighted_candidate_batch_v1",
            "sample_count",
            "deadline",
            "decision_context",
            "iter_decisions",
            "typed intent",
            "control",
        ),
        expected_action_family="typed intent",
        reachable_call_chain=(
            "get_baseline_decision(context)",
            "_baseline_equity(context)",
            "precompute.preflop_equity(card_a, card_b)",
            "iter_decisions(context, baseline, deadline)",
            "_decision_from_equity(context, equity, confidence, samples)",
            "typed fold/pass/allin/raise_to intent",
        ),
        consumer_trace=(
            "decision_context.cards/opponent/deadline -> system precompute lookup and "
            "finite equity batches -> EV comparison -> runtime-legality-sanitized wire action"
        ),
        falsifier=(
            "A fixed-seed short/long run fails if trusted sample work does not grow "
            "or exhaust, or if posterior controls never change a final typed intent."
        ),
        counterfactual=(
            "With a fixed seed, a longer budget or a changed bounded posterior must "
            "perform more trusted work and change at least one final socket-validated intent."
        ),
        bounded_work=(
            "The current valid-board baseline is fixed deterministic 192/256/96 "
            "flop/turn/river sampling with two direct evaluator calls per sample; "
            "each refinement iterator has a finite cap, monotonic deadline check, "
            "and system-observed work count."
        ),
        table_boundary=(
            "precompute.py is system-owned and read-only.  policy.py may consume only "
            "its published pure facts.  Do not add candidate assets or build 1,326-combo "
            "ranges, deck combinations, or file-backed caches per decision."
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
    StrategyReferenceCard(
        reference_id="equity_ev_anytime_v1",
        title="Anytime equity-to-EV refinement",
        primary_innovations=("sample_counted_candidate_batch",),
        purpose=(
            "Use the 169-class fact preflop and the current fixed deterministic "
            "192/256/96 postflop baseline schedule; use a compact made-hand prior "
            "only for invalid/degraded fallback or refinement initialization. Then use "
            "the exact five/seven-card evaluator in deterministic finite batches to "
            "compare call, fold, raise and jam EV under the real monotonic deadline."
        ),
        required_decision_context_fields=(
            "cards.hole",
            "cards.board",
            "betting.pot",
            "betting.to_call",
            "betting.hero_stack",
            "legal.policy_kinds",
            "deadline.refinement_monotonic",
        ),
        required_any_decision_context_fields=(
            "opponent.terminal_response.adaptation_weight",
            "opponent.showdown_range.adaptation_weight",
        ),
        allowed_files=("policy.py",),
        required_worker_terms=(
            "equity_ev_anytime_v1",
            "precompute.evaluate_seven",
            "absolute monotonic deadline",
            "finite batch",
            "expected value",
            "typed intent",
            "same-shape control",
        ),
        expected_action_family="typed intent",
        reachable_call_chain=(
            "get_baseline_decision(context)",
            "_baseline_equity(context)",
            "iter_decisions(context, baseline, deadline)",
            "precompute.deterministic_draw(deck, count, state)",
            "precompute.evaluate_seven(cards)",
            "_decision_from_equity(context, equity, confidence, samples)",
        ),
        consumer_trace=(
            "cards + pot/to_call + legal set + deadline -> fixed-seed weighted equity "
            "estimate -> bounded EV scores -> typed intent -> socket validator"
        ),
        falsifier=(
            "Known evaluator hands or random sever cross-checks disagree, an expired "
            "deadline performs work, or a same-shape table-value control cannot alter "
            "a socket-validated action."
        ),
        counterfactual=(
            "Hold context and seed fixed; longer budget must complete more trusted "
            "samples (or the finite exact river set), and changing only a 169-table "
            "value must change at least one final typed/wire intent."
        ),
        bounded_work=(
            "Baseline publishes the current fixed 192/256/96 flop/turn/river schedule "
            "(two direct evaluator calls per sample, dynamic cap 800), never a full "
            "river enumeration. A compact prior is only invalid/degraded fallback or "
            "refinement initialization. Each refinement batch and total sample count are "
            "capped, with an inner absolute-monotonic deadline guard and latest-safe "
            "publication. Full C(45,2) work is refinement-only."
        ),
        table_boundary=(
            "Consume only direct system-owned precompute.py evaluator, 169-class prior, "
            "and deterministic draw facts; no imported/closure/default evaluator alias, "
            "no baseline combinations, and no nested deck-pair sweep. Combination helpers "
            "are permitted only in deadline-checked refinement. Candidate code performs "
            "no file I/O or table build."
        ),
        forbidden_axes=(
            "wall-clock-relative deadline guesses",
            "unseeded sampling",
            "candidate-generated equity asset",
        ),
        sources=(
            "DeepStack: arXiv:1701.01724",
            "Monte Carlo Continual Resolving: arXiv:1812.07351",
        ),
    ),
    StrategyReferenceCard(
        reference_id="polarized_spr_geometry_v1",
        title="Polarized SPR action geometry",
        primary_innovations=("sample_counted_candidate_batch",),
        purpose=(
            "Generate a small structural raise-to set from live pot, SPR, effective "
            "stack and line ownership, then let progressive equity/EV evidence select "
            "among those legal sizes instead of tuning one threshold."
        ),
        required_decision_context_fields=(
            "hand.street",
            "betting.pot",
            "betting.spr",
            "betting.effective_stack",
            "betting.hero_street_bet",
            "line.can_donk",
            "line.can_delayed_probe",
            "legal.min_raise_to",
            "legal.max_raise_to",
        ),
        required_any_decision_context_fields=(
            "opponent.terminal_response.fold_to_raise",
            "opponent.terminal_response.fold_to_jam",
        ),
        allowed_files=("policy.py",),
        required_worker_terms=(
            "polarized_spr_geometry_v1",
            "SPR geometry",
            "raise_to",
            "effective stack",
            "candidate set",
            "iter_decisions",
            "falsifier",
        ),
        expected_action_family="typed raise_to or non-raise intent",
        reachable_call_chain=(
            "_polarized_raise_fraction(context, equity)",
            "_candidate_raise_fractions(context, equity)",
            "_raise_intent(context, fraction, adaptation_scale)",
            "_decision_from_equity(context, equity, confidence, samples)",
            "typed raise_to intent",
        ),
        consumer_trace=(
            "SPR/pot/effective-stack + donk/delayed-probe line -> finite size geometry "
            "-> posterior-aware EV -> exact legal min/max raise_to -> wire raise"
        ),
        falsifier=(
            "Varying SPR or line ownership while holding cards/posterior fixed never "
            "changes the candidate geometry or final legal raise-to."
        ),
        counterfactual=(
            "A coherent SPR control pair must alter at least one legal raise-to while "
            "both paths retain the same card evidence and typed legality boundary."
        ),
        bounded_work=(
            "At most four structural sizes are scored per completed equity batch; "
            "there is no continuous optimizer or per-decision table construction."
        ),
        table_boundary=(
            "Precompute supplies card facts/equity only; live SPR geometry is derived "
            "from decision_context and cannot be frozen into an opaque sizing table."
        ),
        forbidden_axes=(
            "single magic sizing threshold",
            "raise delta instead of stage-total raise_to",
            "unreachable donk/probe helper",
        ),
        sources=(
            "Safe and Nested Subgame Solving: arXiv:1705.02955",
            "Depth-Limited Solving: arXiv:1805.08195",
        ),
    ),
    StrategyReferenceCard(
        reference_id="action_profile_confidence_v1",
        title="Confidence-gated action-profile consumer",
        primary_innovations=("action_profile",),
        purpose=(
            "Consume the reducer-owned incremental action-profile snapshot only "
            "after its bounded confidence gate clears, then make one legal typed "
            "intent decision depend on the action-rate root while preserving the "
            "baseline decision below that gate."
        ),
        required_decision_context_fields=(
            "opponent.rates.aggression",
            "opponent.rates.fold_to_raise",
            "opponent.confidence",
            "opponent.adaptation_weight",
            "legal.policy_kinds",
            "legal.min_raise_to",
            "legal.max_raise_to",
        ),
        required_any_decision_context_fields=(),
        allowed_files=("policy.py",),
        required_worker_terms=(
            "action_profile_confidence_v1",
            "opponent.rates",
            "confidence gate",
            "typed intent",
            "byte-identical control",
            "falsifier",
        ),
        expected_action_family="typed pass/fold/raise_to intent",
        reachable_call_chain=(
            "get_baseline_decision(context)",
            "_bounded_action_profile(context)",
            "_action_profile_adjusted_intent(context, baseline, profile)",
            "typed pass/fold/raise_to intent",
        ),
        consumer_trace=(
            "decision_context.opponent.rates + reducer-owned confidence -> "
            "bounded action-profile adjustment -> legal typed intent -> "
            "system socket validator"
        ),
        falsifier=(
            "Hold every decision_context field byte-identical except the "
            "opponent.rates root; a high-confidence paired profile must change "
            "at least one legal final typed intent, while the low-confidence "
            "control remains byte-identical to the baseline."
        ),
        counterfactual=(
            "A paired control/intervention changes only the action-profile root "
            "and keeps cards, betting, legality, deadline, and all other "
            "opponent fields byte-identical; malformed or sparse profiles take "
            "the baseline fallback."
        ),
        bounded_work=(
            "Read only compact reducer-provided aggregate rates and confidence; "
            "perform no history scan, profile rebuild, file I/O, or per-decision "
            "model training. Clamp any raise_to through the existing legal range."
        ),
        table_boundary=(
            "This card consumes live decision_context only. It does not authorize "
            "terminal-response/showdown fields, candidate-owned assets, or opaque "
            "model files; any future system-owned asset must use the separately "
            "content-bound asset ABI."
        ),
        forbidden_axes=(
            "opponent.terminal_response",
            "opponent.showdown_range",
            "opponent.samples.fold_to_raise",
            "full match-history rescan",
        ),
        sources=(
            "ReBeL: arXiv:2007.13544",
            "DeepStack (continual resolving and public belief): arXiv:1701.01724",
        ),
    ),
    StrategyReferenceCard(
        reference_id="robust_exploit_mixture_v1",
        title="Selection-guarded robust exploit mixture",
        primary_innovations=("sample_counted_candidate_batch",),
        purpose=(
            "Blend prior play with bounded terminal responses and showdown bucket "
            "weights according to reducer-owned confidence, while discounting the "
            "showdown-selected sample and preserving a prior fallback."
        ),
        required_decision_context_fields=(
            "opponent.adaptation_weight",
            "opponent.rates.aggression",
            "opponent.terminal_response.adaptation_weight",
            "opponent.showdown_range.selection_scope",
            "opponent.showdown_range.selection_bias_guard",
            "opponent.showdown_range.bucket_rates",
            "legal.policy_kinds",
        ),
        required_any_decision_context_fields=(
            "opponent.terminal_response.fold_to_raise",
            "opponent.terminal_response.river_overcall",
        ),
        allowed_files=("policy.py",),
        required_worker_terms=(
            "robust_exploit_mixture_v1",
            "selection bias guard",
            "bounded posterior",
            "bucket multiplier",
            "prior fallback",
            "confidence",
            "typed intent",
        ),
        expected_action_family="typed intent",
        reachable_call_chain=(
            "_opponent_posterior(context)",
            "_opponent_sample_weight(posterior, opponent_hole)",
            "precompute.preflop_bucket(card_a, card_b)",
            "_decision_from_equity(context, equity, confidence, samples)",
        ),
        consumer_trace=(
            "terminal/showdown reducer snapshot -> exact selection guard -> capped "
            "range/response weights -> robust equity/EV -> typed socket intent"
        ),
        falsifier=(
            "An unguarded showdown payload changes the action, a zero-confidence "
            "posterior differs from the prior control, or a guarded bounded posterior "
            "never influences any final legal intent."
        ),
        counterfactual=(
            "Compare guarded vs identical unguarded showdown data and high vs low "
            "terminal response profiles; only guarded/confident evidence may move wire."
        ),
        bounded_work=(
            "Only reducer-provided aggregate fields and per-sampled-hole bucket weights "
            "are read; no hand history or revealed-card list is rescanned."
        ),
        table_boundary=(
            "Uniform 1,326-combo bucket priors and class lookup are system facts. "
            "Posterior values remain bounded live context, never persisted policy assets."
        ),
        forbidden_axes=(
            "unconditional showdown range",
            "uncapped exploit switch",
            "full match-history rescan",
        ),
        sources=(
            "ReBeL: arXiv:2007.13544",
            "Safe and Nested Subgame Solving: arXiv:1705.02955",
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
    primary_text = str(primary or "")
    if primary_text in UNAVAILABLE_PRIMARY_INNOVATIONS:
        return [
            "strategy_reference_primary_unavailable: "
            f"{primary_text}: {UNAVAILABLE_PRIMARY_INNOVATIONS[primary_text]}"
        ]
    card = get_reference_card(reference_id)
    if card is None:
        return [
            "strategy_reference_pack_unknown: "
            f"{reference_id!r}; expected one of {list(reference_pack_ids())}"
        ]
    if primary_text not in card.primary_innovations:
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
    allowed_names = set(card.allowed_files)
    if target_names != allowed_names:
        errors.append(
            "strategy_reference_pack_target_files_mismatch: "
            f"{card.reference_id} requires exactly {list(card.allowed_files)}, "
            f"got {sorted(target_names)}"
        )
    lowered = str(worker_prompt or "").lower()
    missing = [term for term in card.required_worker_terms if term.lower() not in lowered]
    if missing:
        errors.append(
            "strategy_reference_pack_worker_terms_missing: "
            f"{card.reference_id}: {missing}"
        )
    return errors


def master_reference_summary(
    *,
    allowed_primaries: tuple[str, ...] | list[str] | None = None,
) -> str:
    """Render the cards compatible with a frozen architecture focus.

    A normal caller receives the whole small registry.  A focused Master gets
    only cards whose primary is allowed by its immutable architecture policy;
    this prevents a card for one closed opponent axis from teaching literals
    belonging to a different axis.  The selection itself is rendered so the
    prompt digest remains an auditable input rather than an implicit filter.
    """

    selected_primaries: tuple[str, ...] | None
    if allowed_primaries is None:
        selected_primaries = None
        cards = _CARDS
    else:
        selected_primaries = tuple(sorted({
            str(item).strip()
            for item in allowed_primaries
            if str(item).strip()
        }))
        cards = tuple(
            card
            for card in _CARDS
            if set(card.primary_innovations).intersection(selected_primaries)
        )
    lines = [
        "Local, versioned strategy reference cards (source-controlled; choose a card "
        "only for an allowed state_learning primary):",
        f"- registry_version={REFERENCE_PACK_VERSION}; registry_digest={reference_pack_registry_digest()}",
    ]
    if selected_primaries is not None:
        lines.append(
            "- allowed_primaries=" + repr(list(selected_primaries))
            + "; selected_card_ids="
            + repr([card.reference_id for card in cards])
        )
    if selected_primaries is not None and not cards:
        lines.append(
            "- No compatible reference card exists for this frozen primary set. "
            "Do not borrow fields, one-of clauses, or closed-axis examples from "
            "another primary; emit only the system mapping and fail closed if no "
            "valid proposal can be formed."
        )
    for card in cards:
        lines.extend((
            f"- {card.reference_id}: {card.title}. primary={list(card.primary_innovations)}; "
            f"action={card.expected_action_family}.",
            f"  required decision_context fields={list(card.required_decision_context_fields)}; "
            f"one-of={list(card.required_any_decision_context_fields)}.",
            f"  allowed files={list(card.allowed_files)}.",
            f"  required worker literals={list(card.required_worker_terms)}.",
            f"  reachable call chain={list(card.reachable_call_chain)}.",
            f"  consumer trace={card.consumer_trace}",
            f"  falsifier={card.falsifier}",
            f"  proof={card.counterfactual}",
            f"  bound={card.bounded_work}",
        ))
    lines.append(
        "The system-owned, read-only precompute.py facts are acceleration inputs, "
        "never candidate write targets or a state-learning primary by themselves."
    )
    lines.append(
        "bounded_precompute_lookup remains unavailable as a standalone primary: "
        "choose an executable sample-counted card with a live consumer trace."
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
        "- Required decision_context fields: " + ", ".join(card.required_decision_context_fields),
        "- Required one-of decision_context fields: " + ", ".join(card.required_any_decision_context_fields),
        "- Writable file: policy.py only; national_bot.py and precompute.py are read-only.",
        "- Required worker literals: " + ", ".join(card.required_worker_terms),
        f"- Expected action family: {card.expected_action_family}",
        "- Reachable call chain: " + " -> ".join(card.reachable_call_chain),
        f"- Consumer trace: {card.consumer_trace}",
        f"- Falsifier: {card.falsifier}",
        f"- Counterfactual proof: {card.counterfactual}",
        f"- Bounded-work rule: {card.bounded_work}",
        f"- Table boundary: {card.table_boundary}",
        "- Forbidden axes: " + ", ".join(card.forbidden_axes),
        "This card is binding. Do not replace it with a static constant-key table, "
        "an unreachable helper, or a defensive threshold edit.",
    ))
