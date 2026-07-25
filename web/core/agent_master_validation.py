"""Master proposal schema validation and source symbol analysis.

Extracted from agent_master.py for maintainability.

All symbols are re-exported by agent_master.py for backward compatibility.
"""

"""Master Architect agent: plans worker tasks from frozen generation evidence."""

import ast
import hashlib
import json
import re
import time
from pathlib import Path

from bot_namespace import ACTIVE_BOT_PREFIX, bot_name, bot_relpath
from evolution_infra import (
    run_claude_query, substitute_template,
    get_logs_dir, _trim_to_budget, PROMPTS_DIR,
    MAX_MASTER_RETRIES,
    get_bot_dir, MAX_LINES_HARD_CAP,
)

from output_schema import (
    MASTER_PROPOSAL_FALSIFIER_PRIMARY,
    MASTER_PROPOSAL_FALSIFIER_TESTS,
    STATE_LEARNING_INTERVENTION_TARGET_ALIASES,
    STATE_LEARNING_SHARED_INTERVENTION_LEAF_OWNERS,
    STATE_LEARNING_PRIMARY_INTERVENTION_TARGETS,
    STATE_LEARNING_PRIMARY_CHECKS,
    STATE_LEARNING_PRIMARY_PROMPT_TERMS,
    WORKER_PROMPT_MAX_CHARS,
    WORKER_PROMPT_MIN_CHARS,
    master_plan_executable_contract_text,
)
from llm_availability import LLMAvailabilityBlocked, gather_llm_fail_fast


# Keep the rendered strength hypothesis on a validator-known literal.  The
# former ``<W/L/D interval method>`` placeholder prompted natural-language
# values such as ``W/L/D bootstrap 95% CI``; all three Scouts and their one
# schema retry then failed the machine-readable uncertainty contract.
_PROPOSAL_STRENGTH_SAMPLE_FLOOR = ">=30_complete_matches"
_PROPOSAL_UNCERTAINTY_PROMPT_VALUE = "wilson_wld_interval"

# Closed aliases whose natural-language spellings ("fold rate", "fold-rate",
# "fold.rate") routinely appear in legitimate poker prose.  Bind these only at
# an underscore separator (the Python identifier form, e.g. ``fold_rate``) or
# as a compact token (``foldrate``); a space, hyphen, or dot is prose, not an
# alias reference.  Axis-name aliases such as ``terminal_response`` deliberately
# stay permissive so "terminal response" still binds.
_PROSE_PRONE_ALIASES = frozenset({
    "fold_rate",
    "call_rate",
    "raise_rate",
    "allin_rate",
})



def _proposal_schema_repair_guidance(
    projection_hints: tuple[str, ...],
    *,
    require_snapshot_evidence: bool,
    allowed_primaries: tuple[str, ...] | None = None,
) -> str:
    """Render bounded, error-directed Scout repair instructions.

    The final output contract already contains the complete schema.  Repeating
    a generic twelve-item tutorial on every retry increased the prompt while
    hiding the deterministic reason that actually failed.  Keep at most four
    canonical corrections, ordered by semantic risk.
    """

    hints = tuple(str(item) for item in projection_hints)
    guidance: list[str] = []

    def add(text: str) -> None:
        if text not in guidance:
            guidance.append(text)

    if any("proposal_json_object_required" in item for item in hints):
        add(
            "Emit the entire proposal as one JSON object only; no prose, "
            "fences, or trailing text outside that object."
        )
    if any("proposal_schema_mismatch" in item for item in hints):
        add(
            f"Set schema_version to exactly \"{_PROPOSAL_SCHEMA_VERSION}\" at the "
            "top of each proposal object."
        )
    if any("proposal_execution_mode_mismatch" in item for item in hints):
        add(
            "Set execution_mode to the exact value required by this generation's "
            "evidence_mode (strategy_implementation or "
            "fixed_blueprint_capability_audit); copy it verbatim."
        )
    if any("proposal_mechanism_foreign_targets" in item for item in hints):
        add(
            "Do not name, deny, preserve, or qualify any foreign closed target. "
            "State instead that all other decision_context fields are "
            "byte-identical."
        )
    if any("proposal_mechanism_shared_leaf" in item for item in hints):
        roots = tuple(
            sorted({
                STATE_LEARNING_PRIMARY_INTERVENTION_TARGETS[primary]
                for primary in allowed_primaries or ()
                if primary in STATE_LEARNING_PRIMARY_INTERVENTION_TARGETS
            })
        )
        root_clause = (
            f" The only executable root for this frozen proposal is {roots[0]}."
            if len(roots) == 1
            else " Use only the root selected by the mapping row."
        )
        add(
            "Rewrite the complete object from scratch; do not preserve prior "
            "prose. In structural_change, expected_diff, and "
            "falsifier.intervention, use the selected root literal only, never "
            "a bare or qualified leaf, confidence field, or another opponent "
            "path."
            + root_clause
            + " State that all other decision_context fields are byte-identical."
        )
    if any("proposal_mechanism_root_scoped_unknown_leaf" in item for item in hints):
        add(
            "Under the selected root, keep only that root's known child fields; "
            "remove any unrecognized leaf and express the same fact through the "
            "root's existing fields only."
        )
    if any(
        "proposal_mechanism_target_missing" in item
        or "proposal_mechanism_target_mismatch" in item
        or "proposal_falsifier_intervention_target_mismatch" in item
        for item in hints
    ):
        add(
            "Copy the selected row's exact dot target into structural_change, "
            "expected_diff, and falsifier.intervention; bracket notation is "
            "only supplementary and never replaces that literal."
        )
    if any("proposal_mechanism_target_invalid" in item for item in hints):
        add(
            "Set mechanism_target to the exact dot target from one mapping row "
            "of the selected state_learning primary; no other path is valid."
        )
    if any("proposal_target_files_invalid" in item for item in hints):
        add(
            "Set target_files to exactly [\"policy.py\"]; no helper, asset, or "
            "system file may appear there."
        )
    if any(
        "proposal_mechanism_qualified_target_identifier_continuation" in item
        for item in hints
    ):
        add(
            "Do not append identifier characters to any owner-qualified target "
            "or field literal."
        )
    if any("proposal_reachable_chain" in item for item in hints):
        add(
            "Use current direct caller-to-callee edges ending exactly at the one "
            "existing change_symbol; put every chain symbol in source_symbols with "
            "matching source: evidence_refs. An unchanged entry anchor is invalid."
        )
    if any("proposal_change_symbol" in item for item in hints):
        add(
            "Set change_symbol to the exact existing file.py:symbol whose AST body "
            "the Worker will modify, name it in expected_diff, and make it the final "
            "reachable_chain item."
        )
    if any(
        "proposal_evidence" in item or "proposal_source_symbol" in item
        for item in hints
    ):
        add(
            "Use only exact file.py:symbol entries from the verified index and "
            "one matching bare source:file.py:symbol evidence_ref per symbol."
        )
    if any("proposal_snapshot" in item for item in hints):
        add(
            "Copy one exact validated snapshot JSON pointer."
            if require_snapshot_evidence
            else "This mode has no strength snapshot; emit no snapshot reference."
        )
    if any("proposal_measurement" in item for item in hints):
        add(
            "Copy the mode-specific six-field measurement contract exactly; "
            f"use uncertainty={_PROPOSAL_UNCERTAINTY_PROMPT_VALUE} literally "
            "and never replace it with natural-language W/L/D prose."
        )
    if any("proposal_falsifier" in item for item in hints):
        add(
            "falsifier is a closed six-key object: test_name, "
            "state_learning_primary, intervention_target, control, intervention, "
            "expected_observation. Delete every extra key; mechanism_target "
            "appears exactly once at the top level. Copy test_name, "
            "state_learning_primary, mechanism_target, and intervention_target "
            "from one mapping row without relabelling it."
        )
    if not guidance:
        add(
            "Re-emit one complete object and repair only the canonical projection "
            "errors below; preserve the assigned lens and evidence scope."
        )
    return "\n".join(f"- {item}" for item in guidance[:4])


def _render_master_proposal_provider_prompt(inputs):
    from llm_query import LLMRenderedMaterial

    expected = {
        "planning_context", "direction", "directive", "source_v", "next_v",
        "protocol_bootstrap_prepared_only", "singleton_no_strength",
        "source_symbol_index",
        "repair_kind", "projection_hints", "allowed_primaries", "invocation_id",
    }
    if not isinstance(inputs, dict) or set(inputs) != expected:
        raise ValueError("Master proposal renderer input contract mismatch")
    planning_context = str(inputs["planning_context"])
    direction = str(inputs["direction"])
    directive = str(inputs["directive"])
    source_v = int(inputs["source_v"])
    next_v = int(inputs["next_v"])
    bootstrap = bool(inputs["protocol_bootstrap_prepared_only"])
    singleton_no_strength = bool(inputs["singleton_no_strength"])
    if bootstrap and singleton_no_strength:
        raise ValueError("Master proposal planning mode is ambiguous")
    require_snapshot_evidence = not bootstrap and not singleton_no_strength
    evidence_mode = (
        "fresh_strict_control_no_strength"
        if bootstrap
        else "singleton_parent_no_strength"
        if singleton_no_strength
        else "frozen_strength_snapshot"
    )
    measurement_contract = (
        "measurement MUST be a non-empty six-field semicolon string. Its fresh "
        "strict-control value is system-bound to: "
        + _FRESH_STRICT_CONTROL_MEASUREMENT
        + ". It is not a model-selected strength claim and any punctuation or "
        "paraphrase has no authority."
        if bootstrap
        else (
            f"measurement MUST use: target={bot_name(source_v)}; "
            "primary=complete_70_hand_wld; expected_delta=<decimal 0<delta<=1, e.g. 0.03>; "
            f"samples={_PROPOSAL_STRENGTH_SAMPLE_FLOOR}; "
            f"uncertainty={_PROPOSAL_UNCERTAINTY_PROMPT_VALUE}; "
            "secondary=net_chip_ci. Copy the exact key order and copy "
            f"uncertainty={_PROPOSAL_UNCERTAINTY_PROMPT_VALUE} literally; "
            "do not emit natural-language W/L/D punctuation. This is an "
            "unproven post-publication strength "
            "hypothesis; the earlier native precommit is only a regression floor."
            if singleton_no_strength
            else
            "measurement MUST use: target=<one opponent named by the bound snapshot>; "
            "primary=complete_70_hand_wld; expected_delta=<decimal 0<delta<=1, e.g. 0.03>; "
            f"samples={_PROPOSAL_STRENGTH_SAMPLE_FLOOR}; "
            f"uncertainty={_PROPOSAL_UNCERTAINTY_PROMPT_VALUE}; "
            "secondary=net_chip_ci. Copy the exact key order and copy "
            f"uncertainty={_PROPOSAL_UNCERTAINTY_PROMPT_VALUE} literally; "
            "do not emit natural-language W/L/D punctuation. Net chips are "
            "secondary only."
        )
    )
    repair_kind = str(inputs["repair_kind"] or "")
    raw_projection_hints = inputs["projection_hints"]
    if not isinstance(raw_projection_hints, (list, tuple)):
        raise ValueError("Master proposal projection hints must be a list")
    projection_hints = tuple(dict.fromkeys(
        str(item).strip() for item in raw_projection_hints if str(item).strip()
    ))
    if len(projection_hints) > 32 or any(
        len(item) > 160 or re.fullmatch(r"[a-z0-9_:.-]+", item) is None
        for item in projection_hints
    ):
        raise ValueError("Master proposal projection hints are invalid")
    allowed_primaries = _canonical_proposal_primaries(
        inputs["allowed_primaries"]
    )
    mapping_text = _proposal_falsifier_mapping_text(
        allowed_primaries=allowed_primaries,
    )
    allowed_tests = tuple(
        test_name
        for test_name, primary in MASTER_PROPOSAL_FALSIFIER_PRIMARY.items()
        if allowed_primaries is None or primary in allowed_primaries
    )
    is_repair = bool(repair_kind)
    is_distinctness_repair = repair_kind == "distinctness"
    output_contract = (
        "Return one JSON object with exactly: targeted_failure, structural_change, "
        "counterfactual, measurement, why_not_threshold_tuning, mechanism_target, "
        "target_files "
        "(exactly [\"policy.py\"]), expected_diff, source_symbols (1-8 exact "
        "source-relative file.py:symbol references), change_symbol (the one existing "
        "source-relative AST body the Worker must change), reachable_chain (2-8 of "
        "those symbols in direct caller-to-callee order, ending exactly at "
        "change_symbol), falsifier {test_name, "
        "state_learning_primary, intervention_target, control, intervention, "
        "expected_observation}, evidence_refs (source:file.py:symbol "
        "for EVERY source_symbols item; "
        + (
            "at least one snapshot:relative/file.json#/verified/json/pointer"
            if require_snapshot_evidence
            else "snapshot references are forbidden because no strength snapshot exists"
        )
        + "), "
        "and risks. Every chain edge must be a direct syntactic call in the baseline. "
        "Use this CLOSED JSON SHAPE exactly (placeholders are values, not keys): "
        + _proposal_closed_json_shape()
        + ". falsifier has additionalProperties=false: it contains exactly its six "
        "shown keys and MUST NOT contain mechanism_target, source_symbols, target_files, "
        "or any explanatory alias. mechanism_target appears exactly once at the top level. "
        "reachable_chain is executable quality scope, not a generic entry anchor: "
        "it must follow current direct calls from a policy-ABI-reachable caller and "
        "terminate at change_symbol. expected_diff must name that exact symbol as "
        "the existing AST body to modify. Proposed future calls belong only in "
        "structural_change/expected_diff, never in reachable_chain. Every "
        "reachable_chain entry must also appear in source_symbols and have its matching "
        "source: evidence_ref. "
        "Do not invent a symbol or snapshot file. Do not emit tasks, a worker plan, "
        "source choice, proposal_id, Markdown, or commentary."
        " This is national_tcp_policy_v1. policy.py is the only candidate-owned "
        "writable source file; national_bot.py and precompute.py are system-owned "
        "read-only files. Candidate helper modules and candidate-owned assets are "
        "forbidden; a future model/table is allowed only through the system-owned, "
        "manifest-declared, content-bound asset ABI. Propose "
        "a causally distinct policy mechanism over decision_context that returns only "
        "typed pass/fold/allin/raise intents (raise uses raise_to). "
        "IMPORTANT for fresh bootstrap: change_symbol MUST be "
        "\"policy.py:get_baseline_decision\" — it is the only function whose AST body "
        "the first Worker may modify. Do NOT select iter_decisions (its dispatch edge "
        "to get_baseline_decision is system-preserved) or _hole_ids (its call sites "
        "inside get_baseline_decision are system-preserved). Selecting any other "
        "symbol as change_symbol will be rejected by the do-not-touch contract. "
        "IMPORTANT: falsifier.test_name MUST be exactly one of: "
        + ", ".join(allowed_tests)
        + ". "
        "Choose the one that best matches your proposed mechanism. The exact "
        "typed falsifier -> state_learning primary contract is: "
        + mapping_text
        + ". For the selected test_name, copy these exact equalities from its "
        "single row: top-level mechanism_target = row.mechanism_target = "
        "row.intervention_target = falsifier.intervention_target, while "
        "falsifier.state_learning_primary = row.state_learning_primary. "
        "mechanism_target is NEVER the state_learning_primary label. Example: "
        "incremental_opponent_model requires mechanism_target=opponent.rates, "
        "state_learning_primary=action_profile, and "
        "intervention_target=opponent.rates. Copy "
        "the exact intervention_target literal into structural_change, "
        "expected_diff, and falsifier.intervention; each field must describe only "
        "that target and must not name another closed mechanism target or alias. "
        "A foreign target remains forbidden when mentioned only to deny, exclude, "
        "leave unchanged, or disclaim it; say that all other decision_context "
        "fields are byte-identical instead. "
        "Bracket notation such as context['opponent']['rates'] does not replace "
        "the required exact opponent.rates literal. A complete bracket path may "
        "supplement the dot literal, but an incomplete or bare leaf is invalid. "
        "Shared leaf names are namespace-sensitive. Prefer a complete "
        "owner-qualified child. A flat list is also qualified only when the exact "
        "selected root is its immediate header, for example opponent.rates "
        "(aggression, fold_to_raise); never emit a bare leaf outside that exact "
        "root-scoped list. In structural_change, expected_diff, and "
        "falsifier.intervention, never write fold-to-raise, fold to raise, "
        "foldtoraise, or an extra bare fold_to_raise as explanatory prose: an "
        "occurrence is legal only as part of a complete owner-qualified literal "
        "or that exact root-scoped list. Do not name another closed target, its child, or a "
        "sample-count owner in an executable field, even as unchanged. Never append "
        "identifier characters to an owner-qualified target literal. The "
        "required_proposal_terms become final Worker-prompt obligations. A "
        "plan_required_floor_checks entry is an additional generation-wide quality "
        "floor; it is NOT the proposal falsifier unless this filtered mapping says "
        "it is compatible. "
        "NAMESPACE LEAF EXAMPLES (the most common schema rejection — study them; "
        "substitute YOUR selected mechanism_target for <your.target> below): "
        "WRONG (bare leaf in executable field): expected_diff='replace penalty with "
        "posterior['<leaf>'] derived term'. CORRECT (owner-qualified): expected_diff="
        "'replace penalty with a <your.target>.<leaf>-derived term'. "
        "WRONG (bracket path replacing the dot literal): structural_change='read "
        "context['<owner>']['<leaf>']'. CORRECT: structural_change='read <your.target> "
        "(a complete bracket path may supplement but not replace the dot literal)'. "
        "DESCRIBING CODE TO REMOVE: when expected_diff describes deleting an existing "
        "branch that references a foreign target, do NOT write that foreign target's "
        "dotted literal in any executable field — refer to it only by role, e.g. "
        "'remove the existing second condition of the guard' or 'delete the foreign "
        "branch from the existing two-way check'. The same applies to every other "
        "closed target you are not selecting: name it by role ('the existing "
        "unselected branch'), never by its dotted literal. This keeps the executable "
        "prose bound to the single selected mechanism_target while still letting you "
        "describe deletions accurately."
        + " " + measurement_contract
    )
    code_scope = (
        f"Read only the prepared target code at {bot_relpath(next_v)}/ and "
        "system-rendered typed facts already present in this prompt. The "
        "historical lineage source code is quarantined and is not an admissible "
        "planning input.\n\n"
        if bootstrap
        else (
            "Read only the published singleton parent and prepared target code. "
            "No strength snapshot exists yet.\n\n"
            if singleton_no_strength
            else "Read only the allowed frozen snapshot and source/target code.\n\n"
        )
    )
    lineage_scope = (
        f"Historical completion high-water v{source_v} is numeric identity only, "
        f"not a source or parent. The prepared v{next_v} target is the sole "
        "system-owned planning baseline; never open, infer, or inherit "
        f"high-water v{source_v}."
        if bootstrap else
        (
            f"The system-owned singleton parent is fixed at v{source_v} and target "
            f"at v{next_v}. This generation has no peer strength evidence; never "
            "invent H2H or rating claims."
            if singleton_no_strength else
        f"The system-owned source is fixed at v{source_v} and target at "
        f"v{next_v}; never rerank, branch, change evidence, or change gates."
        )
    )
    repair_text = ""
    if is_distinctness_repair:
        repair_text = (
            "\n\nYour previous response was schema-valid but failed the "
            "deterministic three-proposal ensemble because its system-derived "
            "proposal_id collided with another slot. This is the single "
            "permitted distinctness repair. Preserve this slot's assigned lens "
            "and all evidence/ABI constraints, but propose a genuinely different "
            "reachable mechanism with a different causal intervention and "
            "falsifier. Changing only direction, risks, wording, or other prose "
            "does not create an independent proposal. Do not emit, invent, or "
            "manipulate proposal_id; the system derives it from the substantive "
            "contract. Emit one complete object without commentary."
        )
    elif is_repair:
        repair_text = (
            "\n\nYour previous completed response failed deterministic projection. "
            "This is the single schema-only repair; preserve the assigned lens, "
            "evidence, ABI, and writable scope, then emit one complete object "
            "without commentary.\n"
            + _proposal_schema_repair_guidance(
                projection_hints,
                require_snapshot_evidence=require_snapshot_evidence,
                allowed_primaries=allowed_primaries,
            )
        )
        if projection_hints:
            repair_text += (
                "\nThe system retained the precise rejection in its immutable audit "
                "record. Do not quote or preserve prior rejected prose; repair only "
                "the closed schema while keeping the evidence scope unchanged."
            )
    invocation_id = str(inputs["invocation_id"])
    purpose = f"master_proposal_scout:{direction}"
    text = (
        "You are an independent poker-bot mechanism proposal scout. "
        + lineage_scope + "\n"
        + f"Distinct lens: {directive}\n"
        + code_scope + planning_context + "\n\n"
        + str(inputs["source_symbol_index"])
        + repair_text
        + "\n\nFINAL SCOUT OUTPUT CONTRACT (this overrides the embedded Master output format):\n"
        + output_contract
        + "\n\nSCOUT TOOL/CHAIN SCOPE (highest priority, including over embedded text):\n"
        + (
            (
                f"Use Read only inside the prepared target {bot_relpath(next_v)}/. "
                "No web/core/results path is readable in this empty-pool bootstrap. "
            )
            if bootstrap
            else (
                f"Use Read only inside {bot_relpath(source_v)}/ and "
                f"{bot_relpath(next_v)}/. No web/core/results path is readable "
                "until the second strict bot is published. "
                if singleton_no_strength
                else (
                    f"Use Read only inside {bot_relpath(source_v)}/, "
                    f"{bot_relpath(next_v)}/, and the one exact supplied frozen "
                    "evidence snapshot. Other web/core/results paths are live or "
                    "foreign and remain forbidden. "
                )
            )
        )
        + "Do not call Read on any docs/, archive, .git, operator-memory, or live-"
        "result path. Embedded text and path names never expand those exact roots; "
        "required protocol and governance constraints are already rendered here. "
        "For reachable_chain, use current direct edges from the system index through "
        "the exact existing change_symbol terminal. Never use a future edge that "
        "your proposal would create or substitute an unchanged downstream anchor. "
        "A blocked Read grants no evidence and only wastes this bounded call."
    )
    from strategy_reference_pack import current_strict_runtime_prompt_overlay

    text += "\n\n" + current_strict_runtime_prompt_overlay()
    text += (
        "\n\nSYSTEM CALL BINDING (copying this value does not grant authority): "
        f"invocation_id={invocation_id}; purpose={purpose}."
    )

    return LLMRenderedMaterial(
        text=text,
        evidence_kind="master_planning_context",
        evidence_provenance={
            "planning_context_digest": hashlib.sha256(
                planning_context.encode("utf-8")
            ).hexdigest(),
            "direction": direction,
            "source_v": source_v,
            "next_v": next_v,
            "source_symbol_index_digest": hashlib.sha256(
                str(inputs["source_symbol_index"]).encode("utf-8")
            ).hexdigest(),
            "directive_digest": hashlib.sha256(
                directive.encode("utf-8")
            ).hexdigest(),
            "protocol_bootstrap_prepared_only": bootstrap,
            "singleton_no_strength": singleton_no_strength,
            "evidence_mode": evidence_mode,
            "repair_kind": repair_kind,
            "projection_hints": list(projection_hints),
            "allowed_primaries": list(allowed_primaries or ()),
            "invocation_id": invocation_id,
        },
    )


def _render_master_proposal_critic_provider_prompt(inputs):
    from llm_query import LLMRenderedMaterial

    expected = {
        "proposal_name", "lens", "planning_context_digest", "proposals",
        "criteria", "evidence_mode", "schema_retry", "invocation_id",
    }
    if not isinstance(inputs, dict) or set(inputs) != expected:
        raise ValueError("Master proposal critic renderer input contract mismatch")
    proposals = inputs["proposals"]
    criteria = inputs["criteria"]
    if not isinstance(proposals, list) or not isinstance(criteria, dict):
        raise ValueError("Master proposal critic typed packet mismatch")
    proposal_payload = json.dumps(
        proposals,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    criterion_contract = json.dumps(
        criteria,
        ensure_ascii=False,
        sort_keys=True,
    )
    proposal_name = str(inputs["proposal_name"])
    evidence_mode = str(inputs["evidence_mode"])
    if evidence_mode not in {
        "frozen_strength_snapshot",
        "fresh_strict_control_no_strength",
        "singleton_parent_no_strength",
    }:
        raise ValueError("Master proposal critic evidence mode invalid")
    purpose = f"master_proposal_critic:{proposal_name}"
    text = (
        "You are an anonymous advisory critic. Scout identities and lenses are hidden. "
        "Source, evidence cutoff, scope literals, and quality gates are immutable. "
        f"Lens: {inputs['lens']}\n"
        f"Evidence mode: {evidence_mode}\n"
        f"Planning context digest: {inputs['planning_context_digest']}\n"
        "Score EVERY supplied proposal on EACH named criterion with an integer 1..5. "
        "Set reject=true only for a concrete evidence, reachability, or falsification "
        "defect; score is advisory and cannot waive deterministic validation.\n"
        f"Criteria: {criterion_contract}\n"
        "IMPORTANT: Do NOT read, explore, or inspect any files. Do NOT use any tools. "
        "Do NOT think step by step. Immediately score the proposals from their content "
        "and return the JSON. You have no tools available.\n\n"
        "Return JSON exactly as {\"ballots\":[{\"proposal_id\":\"...\","
        "\"scores\":{every criterion: integer 1..5},\"reject\":false,"
        "\"reason\":\"criterion-grounded reason\"}, ...]}.\n\n"
        + proposal_payload
        + (
            "\n\nYour previous ballot failed deterministic schema validation. "
            "This is one schema-only repair attempt; score every ID and every "
            "criterion exactly once. Return ONLY the JSON immediately."
            if bool(inputs["schema_retry"]) else ""
        )
        + "\n\nFINAL CRITIC OUTPUT CONTRACT: return only the ballots JSON in the supplied "
        "proposal order; do not rank, repeat, or rewrite proposal claims. "
        "Output the JSON NOW without any preamble, thinking, or file inspection."
    )
    from strategy_reference_pack import current_strict_runtime_prompt_overlay

    text += "\n\n" + current_strict_runtime_prompt_overlay()
    text += (
        "\n\nSYSTEM CALL BINDING (copying this value does not grant authority): "
        f"invocation_id={inputs['invocation_id']}; purpose={purpose}."
    )

    return LLMRenderedMaterial(
        text=text,
        evidence_kind="frozen_proposal_packet",
        evidence_provenance={
            "proposal_packet_digest": hashlib.sha256(
                proposal_payload.encode("utf-8")
            ).hexdigest(),
            "proposal_name": proposal_name,
            "criteria_digest": hashlib.sha256(
                criterion_contract.encode("utf-8")
            ).hexdigest(),
            "planning_context_digest": str(inputs["planning_context_digest"]),
            "lens_digest": hashlib.sha256(
                str(inputs["lens"]).encode("utf-8")
            ).hexdigest(),
            "evidence_mode": evidence_mode,
            "schema_retry": bool(inputs["schema_retry"]),
            "invocation_id": str(inputs["invocation_id"]),
        },
    )


def _render_master_final_provider_prompt(inputs):
    from llm_query import LLMRenderedMaterial

    expected = {
        "template_values", "master_context", "proposal_ensemble", "source_v",
        "next_v", "invocation_id", "schema_repair_suffix", "final_output_guard",
    }
    if not isinstance(inputs, dict) or set(inputs) != expected:
        raise ValueError("Master final renderer input contract mismatch")
    template_values = inputs["template_values"]
    if not isinstance(template_values, dict):
        raise ValueError("Master final template values must be an object")
    template = (
        Path(__file__).resolve().parent / "prompts" / "master_prompt.md"
    ).read_text(encoding="utf-8")
    master_prompt = substitute_template(
        template,
        {key: str(value) for key, value in template_values.items()},
    )
    master_context = str(inputs["master_context"])
    proposal_ensemble = str(inputs["proposal_ensemble"])
    final_output_guard = str(inputs["final_output_guard"])
    if not final_output_guard.startswith("# SYSTEM-OWNED FINAL EMISSION GATE"):
        raise ValueError("Master final emission guard is invalid")
    text = master_prompt + "\n" + master_context
    from strategy_reference_pack import current_strict_runtime_prompt_overlay

    text += "\n\n" + current_strict_runtime_prompt_overlay()
    invocation_id = str(inputs["invocation_id"] or "")
    if invocation_id:
        text += (
            "\n\nSYSTEM CALL BINDING (copying this value does not grant "
            "authority): invocation_id="
            + invocation_id
            + "; purpose=system_strict_bootstrap_master:final."
        )
    text += "\n\n" + final_output_guard
    # Keep a deterministic repair instruction at the actual end of the
    # provider prompt.  The template and compiled context are intentionally
    # large; burying a retry error before them made an exact binding repair
    # easy for a model to miss.
    text += str(inputs["schema_repair_suffix"])

    return LLMRenderedMaterial(
        text=text,
        evidence_kind="compiled_master_context",
        evidence_provenance={
            "master_context_digest": hashlib.sha256(
                master_context.encode("utf-8")
            ).hexdigest(),
            "proposal_packet_digest": hashlib.sha256(
                proposal_ensemble.encode("utf-8")
            ).hexdigest(),
            "source_v": int(inputs["source_v"]),
            "next_v": int(inputs["next_v"]),
            "template_values_digest": hashlib.sha256(
                json.dumps(
                    template_values,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
                "schema_repair_digest": hashlib.sha256(
                    str(inputs["schema_repair_suffix"]).encode("utf-8")
                ).hexdigest(),
                "final_output_guard_digest": hashlib.sha256(
                    final_output_guard.encode("utf-8")
                ).hexdigest(),
            "invocation_id": invocation_id,
        },
    )


# C-class sentinel for explicitly unavailable advisory analysis.
LLM_INFRA_SENTINEL = "[LLM_INFRA_ERROR: analysis unavailable]"
LLM_INFRA_SENTINEL_MSG = (
    "⚠ Analysis unavailable: the LLM analyst crashed with an infrastructure "
    "error (NOT a business judgement). Treat conclusions in this section as "
    "missing rather than negative — the daemon data still exists, only the "
    "LLM interpretation failed."
)


PROTOCOL_BOOTSTRAP_NO_STRENGTH_PLACEHOLDER = (
    "PROTOCOL BOOTSTRAP NO-STRENGTH: no current-cycle strength evidence exists. "
    "Use only the digest-bound strict prepared artifact, repository-pinned "
    "protocol evidence, and bootstrap receipt supplied by the system."
)


class MasterInfrastructureError(RuntimeError):
    """The Master role produced no plan because its LLM transport failed."""

    def __init__(self, source_v, next_v, prompt_digest, issue):
        self.source_v = source_v
        self.next_v = next_v
        self.prompt_digest = prompt_digest
        self.issue = str(issue)[:500]
        super().__init__(self.issue)


class MasterEnsembleInfrastructureParked(MasterInfrastructureError):
    """One journaled Scout/Ballot is missing; preserve all accepted siblings."""

    def __init__(
        self,
        source_v,
        next_v,
        prompt_digest,
        issue,
        *,
        slot,
        retry_state,
    ):
        super().__init__(source_v, next_v, prompt_digest, issue)
        self.slot = str(slot)
        self.role_attempt = int((retry_state or {}).get("role_attempt") or 1)
        self.accepted_slots = tuple((retry_state or {}).get("accepted_slots") or ())
        self.pending_slots = tuple((retry_state or {}).get("pending_slots") or ())
        self.authority_run_id = str((retry_state or {}).get("run_id") or "")
        self.retry_after_sec = min(
            60.0,
            max(5.0, 5.0 * (2 ** min(self.role_attempt - 1, 4))),
        )
        self.needs_attention = self.role_attempt >= 3


class MasterAuthorityError(RuntimeError):
    """Deterministic checkpoint/evidence authority blocks provider dispatch."""

    def __init__(self, source_v, next_v, prompt_digest, errors):
        self.source_v = source_v
        self.next_v = next_v
        self.prompt_digest = prompt_digest
        self.errors = tuple(
            str(item)[:500]
            for item in (
                errors if isinstance(errors, (list, tuple)) else [errors]
            )
            if str(item)
        ) or ("master_authority_invalid",)
        self.issue = ";".join(self.errors)[:500]
        super().__init__(self.issue)


_MASTER_PROPOSAL_DIRECTIONS = (
    (
        "mechanism",
        "Propose one structural mechanism that replaces a reachable parent behavior; "
        "threshold-only tuning is invalid.",
    ),
    (
        "counterfactual",
        "Start from one falsifiable counterfactual/control and design the smallest "
        "reachable mechanism that could make it pass.",
    ),
    (
        "compute_memory",
        "Explore bounded precomputation, anytime decision work, or persistent match "
        "memory only when the injected policy/evidence makes that axis eligible.",
    ),
)


_PROPOSAL_SCHEMA_VERSION = "master-proposal-v4"
_PROPOSAL_PACKET_SCHEMA_VERSION = "master-proposal-packet-v6"
_PROPOSAL_REPAIR_EOF_OBJECT_PARSE_MODE = (
    "master-proposal-repair-eof-json-object-v1"
)
_POLICY_ABI_ENTRYPOINT_SYMBOLS = (
    "policy.py:get_baseline_decision",
    "policy.py:iter_decisions",
)
_DECISION_RELEVANT_SYMBOL_TERMS = (
    "action",
    "decision",
    "equity",
    "intent",
    "line",
    "memory",
    "opponent",
    "posterior",
    "raise",
    "range",
    "refine",
    "simulation",
    "strategy",
    "strength",
)
_UTILITY_SYMBOL_TERMS = (
    "bounded",
    "card_id",
    "clamp",
    "hole_ids",
    "integer",
    "number",
)
_PROPOSAL_FALSIFIER_TESTS = MASTER_PROPOSAL_FALSIFIER_TESTS


def _proposal_falsifier_primary(test_name: object) -> str | None:
    """Return the closed state-learning primary for one typed falsifier."""

    return MASTER_PROPOSAL_FALSIFIER_PRIMARY.get(str(test_name or "").strip())


def _canonical_proposal_primaries(
    values: object,
) -> tuple[str, ...] | None:
    """Normalize an optional frozen set of permitted proposal primaries."""

    if values is None:
        return None
    if not isinstance(values, (list, tuple, set, frozenset)):
        raise ValueError("proposal allowed primaries must be a collection")
    known = set(STATE_LEARNING_PRIMARY_INTERVENTION_TARGETS)
    normalized = tuple(sorted({
        str(value).strip()
        for value in values
        if str(value).strip()
    }))
    if not normalized:
        return None
    if any(value not in known for value in normalized):
        raise ValueError("proposal allowed primaries are invalid")
    return normalized


def _architecture_proposal_primaries(
    architecture_policy: dict | None,
) -> tuple[str, ...] | None:
    """Derive Scout-visible primaries from immutable architecture checks.

    This is deliberately a projection of the system policy, not an LLM choice.
    If the policy has no falsifier-mapped deficit we preserve the historic
    all-card view.  A focused policy receives only matching cards/mapping rows,
    preventing cross-axis examples from leaking into the sole schema retry.
    """

    if not isinstance(architecture_policy, dict):
        return None
    required_checks = list(architecture_policy.get("plan_required_floor_checks") or ())
    focus = architecture_policy.get("selected_focus")
    if isinstance(focus, dict):
        required_checks.extend(focus.get("required_checks") or ())
    required_set = {str(check).strip() for check in required_checks if str(check).strip()}
    primaries = tuple(
        primary
        for _test, primary in MASTER_PROPOSAL_FALSIFIER_PRIMARY.items()
        if _test in required_set
    )
    return _canonical_proposal_primaries(primaries) if primaries else None


def _proposal_falsifier_mapping_text(
    *,
    allowed_primaries: tuple[str, ...] | None = None,
) -> str:
    """Render the compact machine mapping needed by proposal Scouts.

    Aliases, derived quality checks, and final Worker prompt terms remain
    system-owned validator/compiler data.  Repeating them in every independent
    Scout prompt added thousands of characters without creating provider-owned
    output fields.
    """

    allowed = _canonical_proposal_primaries(allowed_primaries)
    rows = {
        test_name: {
            "state_learning_primary": primary,
            "mechanism_target": (
                STATE_LEARNING_PRIMARY_INTERVENTION_TARGETS[primary]
            ),
            "intervention_target": (
                STATE_LEARNING_PRIMARY_INTERVENTION_TARGETS[primary]
            ),
        }
        for test_name, primary in MASTER_PROPOSAL_FALSIFIER_PRIMARY.items()
        if allowed is None or primary in allowed
    }
    if not rows:
        # An over-narrow architecture-policy filter (e.g. a singleton
        # no-strength bootstrap) can exclude every falsifier primary.  The Scout
        # still needs a complete mapping table to choose a valid test_name, so
        # fall back to the full unfiltered table rather than crashing the whole
        # renderer and abandoning the generation.
        rows = {
            test_name: {
                "state_learning_primary": primary,
                "mechanism_target": (
                    STATE_LEARNING_PRIMARY_INTERVENTION_TARGETS[primary]
                ),
                "intervention_target": (
                    STATE_LEARNING_PRIMARY_INTERVENTION_TARGETS[primary]
                ),
            }
            for test_name, primary in MASTER_PROPOSAL_FALSIFIER_PRIMARY.items()
        }
    return json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _proposal_closed_json_shape() -> str:
    """Return the exact proposal key shape shown to every Scout.

    The runtime validator remains the authority.  This compact machine-readable
    skeleton prevents a frequent provider mistake where the top-level typed
    ``mechanism_target`` is redundantly copied into the closed ``falsifier``
    object, consuming the single schema-repair attempt.
    """

    return json.dumps({
        "targeted_failure": "<text>",
        "structural_change": "<text>",
        "counterfactual": "<text>",
        "measurement": "<exact measurement contract>",
        "why_not_threshold_tuning": "<text>",
        "mechanism_target": "<exact mapping target>",
        "target_files": ["policy.py"],
        "expected_diff": "<text>",
        "source_symbols": ["<file.py:symbol>"],
        "change_symbol": "<file.py:callee>",
        "reachable_chain": ["<file.py:caller>", "<file.py:callee>"],
        "falsifier": {
            "test_name": "<one allowed test>",
            "state_learning_primary": "<mapped primary>",
            "intervention_target": "<same mapping target>",
            "control": "<text>",
            "intervention": "<text>",
            "expected_observation": "<text>",
        },
        "evidence_refs": ["source:<file.py:symbol>"],
        "risks": "<text>",
    }, ensure_ascii=False, separators=(",", ":"))


def _proposal_mechanism_target_errors(
    proposal: dict,
    falsifier: dict,
) -> tuple[str, ...]:
    """Cross-bind the typed mechanism target to the executable proposal fields."""

    primary = _proposal_falsifier_primary(falsifier.get("test_name"))
    if primary is None:
        return ("proposal_mechanism_target_primary_invalid",)
    expected = STATE_LEARNING_PRIMARY_INTERVENTION_TARGETS[primary]
    declared = proposal.get("mechanism_target")
    intervention_target = falsifier.get("intervention_target")
    errors: list[str] = []
    if declared != expected:
        errors.append(
            f"proposal_mechanism_target_mismatch:expected={expected}:actual={declared}"
        )
    if intervention_target != expected:
        errors.append(
            "proposal_falsifier_intervention_target_mismatch:"
            f"expected={expected}:actual={intervention_target}"
        )
    executable_fields = {
        "structural_change": proposal.get("structural_change"),
        "expected_diff": proposal.get("expected_diff"),
        "intervention": falsifier.get("intervention"),
    }

    def literal_appears(value: object, literal: str) -> bool:
        if not isinstance(value, str):
            return False
        # The required dot literal may prefix a qualified child, but must not
        # pass as a substring of a different identifier (for example
        # ``opponent.rates_backup``).
        pattern = (
            r"(?<![a-z0-9_])"
            + re.escape(literal)
            + r"(?![a-z0-9_])"
        )
        return re.search(pattern, value, flags=re.IGNORECASE) is not None

    missing_target_fields = sorted(
        field
        for field, value in executable_fields.items()
        if not literal_appears(value, expected)
    )
    if missing_target_fields:
        errors.append(
            "proposal_mechanism_target_missing_from_executable_fields:"
            + expected
            + ":"
            + ",".join(missing_target_fields)
        )
    mechanism_text = " ".join(
        value.lower()
        for value in executable_fields.values()
        if isinstance(value, str)
    )

    def mask_literals(text: str, literals: tuple[str, ...] | list[str]) -> str:
        masked = text
        for literal in sorted(set(literals), key=len, reverse=True):
            patterns = [(
                r"(?<![a-z0-9_])"
                + re.escape(literal.lower())
                + r"(?![a-z0-9_])"
            )]
            parts = literal.lower().split(".")
            if len(parts) >= 2 and all(
                re.fullmatch(r"[a-z0-9_]+", part) for part in parts
            ):
                patterns.append(
                    r"(?<![a-z0-9_])(?:context|decision_context)"
                    + "".join(
                        r"\s*\[\s*['\"]"
                        + re.escape(part)
                        + r"['\"]\s*\]"
                        for part in parts
                    )
                    + r"(?![a-z0-9_])"
                )
            for pattern in patterns:
                masked = re.sub(
                    pattern,
                    " ",
                    masked,
                    flags=re.IGNORECASE,
                )
        return masked

    root_scoped_list_errors: list[str] = []
    expected_root_children = {
        alias.rsplit(".", 1)[1]
        for alias in STATE_LEARNING_INTERVENTION_TARGET_ALIASES[expected]
        if alias.startswith(expected + ".")
        and re.fullmatch(r"[a-z_][a-z0-9_]*", alias.rsplit(".", 1)[1])
    }

    def mask_root_scoped_shared_leaves(text: str) -> str:
        """Mask a closed, root-qualified shorthand list for the expected axis.

        The proposal contract normally requires a full owner-qualified child
        literal.  A Scout can also make that ownership unambiguous with the
        deliberately narrow ``opponent.rates (aggression, fold_to_raise)``
        notation: the exact selectable root is immediately followed by a flat
        list of identifier leaves.  Treat that syntax as qualified rather than
        rejecting it as a bare shared leaf.  Do not accept prose, nested paths,
        values, or a different root inside the parentheses; those remain
        fail-closed and are still scanned for foreign targets below.
        """

        root_pattern = re.compile(
            r"(?<![a-z0-9_])" + re.escape(expected) + r"\s*\(([^()]*)\)",
            flags=re.IGNORECASE,
        )

        def replace(match: re.Match[str]) -> str:
            body = match.group(1)
            fields = re.split(r"\s*(?:,|\band\b)\s*", body)
            normalized_fields = [
                field.strip().strip("`'\"").lower()
                for field in fields
            ]
            if not normalized_fields or any(
                re.fullmatch(r"[a-z_][a-z0-9_]*", field) is None
                for field in normalized_fields
            ):
                return match.group(0)
            unknown_fields = sorted(set(normalized_fields) - expected_root_children)
            if unknown_fields:
                root_scoped_list_errors.extend(
                    "proposal_mechanism_root_scoped_unknown_leaf:"
                    + expected
                    + ":"
                    + field
                    for field in unknown_fields
                )
                return match.group(0)
            masked_body = body
            for leaf, owners in STATE_LEARNING_SHARED_INTERVENTION_LEAF_OWNERS.items():
                if (
                    leaf.lower() in normalized_fields
                    and f"{expected}.{leaf}" in owners
                ):
                    masked_body = re.sub(
                        r"(?<![a-z0-9_])" + re.escape(leaf) + r"(?![a-z0-9_])",
                        " ",
                        masked_body,
                        flags=re.IGNORECASE,
                    )
            return match.group(0).replace(body, masked_body, 1)

        return root_pattern.sub(replace, text)

    # A SCREAMING_SNAKE_CASE token in executable prose is a Python source
    # constant reference (e.g. ``FOLD_TO_RAISE_PRIOR``), not a bare shared
    # leaf.  Masking it before lower-casing keeps its normalized form
    # (``fold_to_raise_prior``) from containing the bounded shared-leaf
    # substring (``_fold_to_raise_``).  A token whose lowercase form is itself
    # a known leaf or alias is left intact, so an all-uppercase bare leaf
    # (``FOLD_TO_RAISE``) is still caught below.
    _screaming_protected = {
        leaf.lower() for leaf in STATE_LEARNING_SHARED_INTERVENTION_LEAF_OWNERS
    }
    _screaming_protected |= {
        alias.lower()
        for aliases in STATE_LEARNING_INTERVENTION_TARGET_ALIASES.values()
        for alias in aliases
    }

    def _mask_screaming_constants(text: str) -> str:
        def _replace(match: re.Match[str]) -> str:
            token = match.group(0)
            return " " if token.lower() not in _screaming_protected else token

        return re.sub(
            r"(?<![A-Za-z0-9_])[A-Z][A-Z0-9_]{2,}(?![A-Za-z0-9_])",
            _replace,
            text,
        )

    ambiguous_shared_leaves: list[str] = []
    unowned_fields = []
    for value in executable_fields.values():
        if not isinstance(value, str):
            continue
        masked_value = mask_literals(
            _mask_screaming_constants(value).lower(),
            [
                owner
                for owners in STATE_LEARNING_SHARED_INTERVENTION_LEAF_OWNERS.values()
                for owner in owners
            ],
        )
        masked_value = mask_root_scoped_shared_leaves(masked_value)
        unowned_fields.append(masked_value)
    unowned_mechanism_text = " ".join(unowned_fields)
    for leaf, owners in STATE_LEARNING_SHARED_INTERVENTION_LEAF_OWNERS.items():
        unowned_text = unowned_mechanism_text
        # Outside a validated root-scoped list, preserve every spelling of a
        # shared leaf.  Executable prose cannot distinguish an explanatory
        # phrase from a second, unowned input; treating either as harmless
        # would let another opponent namespace alter the claimed mechanism.
        normalized_unowned = re.sub(
            r"[^a-z0-9]+", "_", unowned_text
        ).strip("_")
        bounded_unowned = f"_{normalized_unowned}_"
        compact_leaf = re.sub(r"[^a-z0-9]+", "", leaf.lower())
        if (
            f"_{leaf.lower()}_" in bounded_unowned
            or re.search(
                r"(?<![a-z0-9])"
                + re.escape(compact_leaf)
                + r"(?![a-z0-9])",
                unowned_text,
            )
        ):
            ambiguous_shared_leaves.append(leaf)
    errors.extend(sorted(set(root_scoped_list_errors)))
    if ambiguous_shared_leaves:
        errors.append(
            "proposal_mechanism_shared_leaf_requires_full_namespace:"
            + ",".join(sorted(ambiguous_shared_leaves))
        )

    # Mask complete qualified fields owned by the expected axis before looking
    # for foreign aliases. Otherwise a legitimate phrase such as
    # ``opponent.terminal_response.fold_to_raise rate`` contains the token
    # sequence ``raise rate`` and can be misclassified as the action-profile
    # alias ``raise_rate``.
    expected_qualified_aliases = tuple(
        alias
        for alias in STATE_LEARNING_INTERVENTION_TARGET_ALIASES[expected]
        if alias.startswith(expected + ".")
    )
    qualified_identifier_continuations = sorted(
        f"{alias}:{field}"
        for field, value in executable_fields.items()
        if isinstance(value, str)
        for alias in expected_qualified_aliases
        if re.search(
            r"(?<![a-z0-9_])"
            + re.escape(alias)
            + r"(?=[a-z0-9_])",
            value,
            flags=re.IGNORECASE,
        )
    )
    if qualified_identifier_continuations:
        errors.append(
            "proposal_mechanism_qualified_target_identifier_continuation:"
            + ",".join(qualified_identifier_continuations)
        )
    foreign_scan_text = mask_literals(mechanism_text, expected_qualified_aliases)

    def alias_appears(alias: str) -> bool:
        parts = re.findall(r"[a-z0-9]+", alias.lower())
        if not parts:
            return False
        joiner = (
            r"[_]*"
            if alias.lower() in _PROSE_PRONE_ALIASES
            else r"[^a-z0-9]*"
        )
        alias_pattern = joiner.join(map(re.escape, parts))
        if re.search(
            r"(?<![a-z0-9_])"
            + alias_pattern
            + r"(?![a-z0-9_])",
            foreign_scan_text,
        ):
            return True
        # Long closed aliases must also fail closed when identifier characters
        # are appended (``terminalresponsebackup``).  Keep a leading boundary
        # that also rejects an underscore prefix, so a longer local identifier
        # such as ``raise_fold_rate`` is not misread as the ``fold_rate`` alias;
        # keep short lexical terms such as ``donk`` boundary-only so words such
        # as ``interactionprofile`` and ``donkey`` remain legal.
        compact_alias = "".join(parts)
        if len(compact_alias) < 8:
            return False
        return re.search(
            r"(?<![a-z0-9_])" + alias_pattern,
            foreign_scan_text,
        ) is not None
    # ``deadline`` is a universal safety boundary and can legitimately appear
    # in every bounded strategy proposal.  All other closed mechanism axes have
    # narrow aliases so a proposal cannot carry the correct typed label while
    # its executable prose actually varies terminal, range, or line state.
    foreign_targets = {
        target
        for target, aliases in STATE_LEARNING_INTERVENTION_TARGET_ALIASES.items()
        if target not in {expected, "deadline"}
        and any(alias_appears(alias) for alias in aliases)
    }

    # ``opponent.samples.fold_to_raise`` shares a leaf with two independently
    # governed decision inputs but is not itself a selectable primary target.
    # It must still be treated as foreign executable state, including when a
    # proposal claims it is unchanged.  Otherwise a valid action-profile label
    # could smuggle an unreviewed sample-count intervention through a shared
    # leaf that is absent from the selectable-target alias table.
    def owner_appears(owner: str) -> bool:
        patterns = [
            r"(?<![a-z0-9_])"
            + re.escape(owner.lower())
            + r"(?![a-z0-9_])"
        ]
        parts = owner.lower().split(".")
        if len(parts) >= 2 and all(
            re.fullmatch(r"[a-z0-9_]+", part) for part in parts
        ):
            patterns.append(
                r"(?<![a-z0-9_])(?:context|decision_context)"
                + "".join(
                    r"\s*\[\s*['\"]"
                    + re.escape(part)
                    + r"['\"]\s*\]"
                    for part in parts
                )
                + r"(?![a-z0-9_])"
            )
        return any(
            re.search(pattern, mechanism_text, flags=re.IGNORECASE) is not None
            for pattern in patterns
        )

    for owners in STATE_LEARNING_SHARED_INTERVENTION_LEAF_OWNERS.values():
        for owner in owners:
            owner_target = owner.rsplit(".", 1)[0]
            if owner_target != expected and owner_appears(owner):
                foreign_targets.add(owner_target)
    foreign_targets = sorted(foreign_targets)
    if foreign_targets:
        errors.append(
            "proposal_mechanism_foreign_targets_in_executable_claim:"
            + ",".join(foreign_targets)
        )
    return tuple(errors)


_PROPOSAL_CRITIC_CRITERIA = {
    "evidence_traceability": (
        "Every claimed source fact is bound to a verified source symbol or a frozen "
        "snapshot node with a digest-bound resolved projection."
    ),
    "runtime_reachability": (
        "The verified parent call chain reaches a file that the proposal will edit."
    ),
    "falsifiability": (
        "The control/intervention/expected observation can disprove the mechanism."
    ),
    "causal_attribution": (
        "The structured mechanism_target equals the falsifier intervention_target, "
        "and the executable claim varies only that target rather than unrelated "
        "threshold or profile axes."
    ),
    "frozen_strength_relevance": (
        "When a frozen strength snapshot exists, bind one concrete weakness and "
        "affected decision frequency. In a declared zero-strength generation, "
        "require a poker-decision mechanism with a measurable parent/control "
        "counterfactual and explicitly make no measured-strength claim. Protocol "
        "compliance, observability, and code novelty alone never suffice."
    ),
    "bounded_regression_risk": (
        "The implementation scope and fallback make regressions observable and bounded."
    ),
}

_STRENGTH_SNAPSHOT_FILENAMES = frozenset({
    "head_to_head.json",
    "selection_snapshot.json",
    "glicko_ratings.json",
    "bot_stats.json",
    "bot_action_stats.json",
    "bot_action_stats_per_opp.json",
    "replay_spotlight.json",
})
_SNAPSHOT_METADATA_ONLY_TERMINALS = frozenset({
    "active_bots",
    "schema_version",
    "manifest_digest",
    "evaluation_identity_digest",
    "sha256",
    "bytes",
    "entries",
    "save_num",
    "daemon_run_id",
    "created_at",
    "generated_at",
    "epoch",
    "version",
    "source_v",
    "next_v",
})
_SNAPSHOT_STRENGTH_SIGNAL_KEYS = frozenset({
    "games",
    "a_wins",
    "b_wins",
    "wins",
    "losses",
    "draws",
    "win_rate",
    "selection_score",
    "leaderboard_score",
    "h2h_avg_wr",
    "h2h_games",
    "h2h_coverage",
    "strength_confidence",
    "r",
    "rd",
    "sigma",
    "total_actions",
    "actions",
    "folds",
    "calls",
    "checks",
    "raises",
    "allins",
    "net_chips",
    "secondary_net_chips_mean",
})

_PROPOSAL_SUBSTANTIVE_FIELDS = (
    "schema_version",
    "targeted_failure",
    "structural_change",
    "counterfactual",
    "measurement",
    "why_not_threshold_tuning",
    "mechanism_target",
    "expected_diff",
    "target_files",
    "source_symbols",
    "change_symbol",
    "reachable_chain",
    "falsifier",
    "evidence_refs",
    "snapshot_evidence",
    "execution_mode",
)

_PROPOSAL_MEASUREMENT_FIELDS = (
    "target",
    "primary",
    "expected_delta",
    "samples",
    "uncertainty",
    "secondary",
)

_FRESH_STRICT_CONTROL_MEASUREMENT = (
    "target=fixed_blueprint_control; "
    "primary=typed_falsifier_and_official_5_plus_3; "
    "expected_delta=not_applicable; samples=official_5_plus_3; "
    "uncertainty=no_strength_claim; secondary=none"
)


def _system_bound_proposal_measurement(
    raw_value: object,
    evidence_mode: str | None,
) -> str | None:
    """Return a system-owned bootstrap measurement after field-presence proof.

    A fresh strict control has no opponent, strength sample, or measurement
    choice.  Its six-field measurement is therefore fixed by the bootstrap
    contract, just like final-plan metadata is fixed by the selected proposal.
    The provider must still emit the closed six-field shape.  Once that shape
    is present, punctuation or a paraphrase in its values cannot alter the
    immutable no-strength claim: the system substitutes the canonical control
    contract.  Missing, non-string, and malformed fields remain invalid so
    this is not a schema bypass.
    """

    if evidence_mode != "fresh_strict_control_no_strength":
        return None
    if (
        not isinstance(raw_value, str)
        or _parsed_proposal_measurement(raw_value) is None
    ):
        return None
    return _FRESH_STRICT_CONTROL_MEASUREMENT


def _parsed_proposal_measurement(value: str) -> dict[str, str] | None:
    """Parse the six-field strength hypothesis without substring loopholes."""

    parts = [part.strip() for part in str(value or "").split(";") if part.strip()]
    if len(parts) != len(_PROPOSAL_MEASUREMENT_FIELDS):
        return None
    parsed: dict[str, str] = {}
    for part in parts:
        if "=" not in part:
            return None
        key, item = part.split("=", 1)
        # The measurement string is an unproven post-publication strength
        # hypothesis (see the Master prompt): the downstream native precommit
        # and elo_daemon recompute strength from real 70-hand matches and do
        # not read this field.  Normalize case after stripping whitespace so a
        # model's natural capitalization of the canonical machine literals is
        # not rejected; all contract constants are lowercase, so case-folding
        # cannot weaken the evidence_mode split in
        # _proposal_measurement_contract_valid.
        key = key.strip().lower()
        item = item.strip().lower()
        if not key or not item or key in parsed:
            return None
        parsed[key] = item
    if tuple(parsed) != _PROPOSAL_MEASUREMENT_FIELDS:
        return None
    return parsed


def _proposal_measurement_contract_valid(value: str, evidence_mode: str) -> bool:
    """Require one machine-readable generation hypothesis, not vague test prose."""

    parsed = _parsed_proposal_measurement(value)
    if parsed is None:
        return False
    if evidence_mode == "fresh_strict_control_no_strength":
        return parsed == _parsed_proposal_measurement(
            _FRESH_STRICT_CONTROL_MEASUREMENT
        )
    elif evidence_mode in {
        "frozen_strength_snapshot",
        "singleton_parent_no_strength",
    }:
        if not re.fullmatch(rf"{re.escape(ACTIVE_BOT_PREFIX)}[1-9][0-9]*", parsed["target"]):
            return False
        try:
            expected_delta = float(parsed["expected_delta"])
        except (TypeError, ValueError):
            return False
        uncertainty = parsed["uncertainty"]
        return bool(
            0.0 < expected_delta <= 1.0
            and parsed["primary"] == "complete_70_hand_wld"
            and parsed["samples"] == _PROPOSAL_STRENGTH_SAMPLE_FLOOR
            and uncertainty == _PROPOSAL_UNCERTAINTY_PROMPT_VALUE
            and parsed["secondary"] == "net_chip_ci"
        )
    else:
        return False


def _measurement_target_bound_to_snapshot(
    measurement: str,
    snapshot_evidence: list[dict],
) -> bool:
    parsed = _parsed_proposal_measurement(measurement)
    if parsed is None:
        return False
    target = parsed["target"]
    bound_bots: set[str] = set()
    for binding in snapshot_evidence:
        if not isinstance(binding, dict):
            continue
        bound_bots.update(re.findall(
            rf"{re.escape(ACTIVE_BOT_PREFIX)}[1-9][0-9]*",
            (
                str(binding.get("reference") or "")
                + "\n"
                + str(binding.get("resolved_projection") or "")
            ).lower(),
        ))
    return target in bound_bots


def _snapshot_node_has_strength_signal(node: object, filename: str) -> bool:
    """Reject pool/manifest containers that do not contain a strength fact."""

    if isinstance(node, str):
        return filename == "replay_spotlight.json" and len(node.strip()) >= 40
    if isinstance(node, dict):
        keys = {str(key).lower() for key in node}
        if keys.intersection(_SNAPSHOT_STRENGTH_SIGNAL_KEYS):
            return True
        return any(
            _snapshot_node_has_strength_signal(value, filename)
            for value in list(node.values())[:64]
            if isinstance(value, (dict, list))
        )
    if isinstance(node, list):
        return any(
            _snapshot_node_has_strength_signal(item, filename)
            for item in node[:64]
            if isinstance(item, (dict, list))
        )
    return False


def _safe_relative_python_path(value: object) -> str | None:
    """Return one normalized source-relative Python path, never an escape."""
    raw = str(value or "").strip().replace("\\", "/")
    path = Path(raw)
    if (
        not raw
        or path.is_absolute()
        or ".." in path.parts
        or path.suffix != ".py"
    ):
        return None
    return path.as_posix()


def _source_symbol_graph(source_dir: Path) -> tuple[dict[str, set[str]], str]:
    """Index real top-level functions/methods and their direct call leaves.

    The graph deliberately proves only a small, deterministic claim: every
    symbol exists in the frozen baseline and every adjacent item in a submitted
    reachability chain is a direct syntactic call.  It does not ask an LLM to
    judge whether prose merely *sounds* reachable.
    """
    graph: dict[str, set[str]] = {}
    digest = hashlib.sha256()
    source_dir = Path(source_dir).resolve()
    for path in sorted(source_dir.rglob("*.py")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        try:
            relative = path.resolve().relative_to(source_dir).as_posix()
            payload = path.read_bytes()
        except (OSError, ValueError):
            continue
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(payload)
        digest.update(b"\0")
        try:
            tree = ast.parse(payload, filename=relative)
        except SyntaxError:
            # Syntax-invalid files cannot supply evidence, but they still bind
            # the source artifact digest and therefore cannot drift invisibly.
            continue

        def calls(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
            """Return calls executed by this body, excluding nested scopes."""

            result: set[str] = set()

            class DirectBodyCalls(ast.NodeVisitor):
                def visit_Call(self, child: ast.Call) -> None:
                    target = child.func
                    if isinstance(target, ast.Name):
                        result.add(target.id)
                    elif isinstance(target, ast.Attribute):
                        result.add(target.attr)
                    self.generic_visit(child)

                # A nested scope's body is not executed merely because the
                # enclosing policy function runs. Treat it as a separate,
                # unindexed proof obligation instead of inventing a direct edge.
                def visit_FunctionDef(self, child: ast.FunctionDef) -> None:
                    return None

                def visit_AsyncFunctionDef(
                    self,
                    child: ast.AsyncFunctionDef,
                ) -> None:
                    return None

                def visit_ClassDef(self, child: ast.ClassDef) -> None:
                    return None

                def visit_Lambda(self, child: ast.Lambda) -> None:
                    return None

            visitor = DirectBodyCalls()
            for statement in node.body:
                visitor.visit(statement)
            return result

        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                graph[f"{relative}:{node.name}"] = calls(node)
            elif isinstance(node, ast.ClassDef):
                # Calling a class does not execute every method body. Keep the
                # class symbol available as a callee but give it no fabricated
                # aggregate edges; each method owns its own direct-call facts.
                graph[f"{relative}:{node.name}"] = set()
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        graph[f"{relative}:{node.name}.{child.name}"] = calls(child)
    return graph, digest.hexdigest()


def _source_symbol_ast_digest(source_dir: Path, symbol: str) -> str | None:
    """Digest one exact graph symbol's executable AST without line metadata."""

    normalized = _normalize_source_symbol(symbol)
    if normalized is None:
        return None
    relative, qualified = normalized.rsplit(":", 1)
    source_dir = Path(source_dir).resolve()
    candidate = (source_dir / relative).resolve()
    try:
        candidate.relative_to(source_dir)
        tree = ast.parse(candidate.read_bytes(), filename=relative)
    except (OSError, ValueError, SyntaxError):
        return None
    parts = qualified.split(".")
    node: ast.AST | None = None
    for top_level in tree.body:
        if getattr(top_level, "name", None) == parts[0] and isinstance(
            top_level,
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
        ):
            node = top_level
            break
    for part in parts[1:]:
        if not isinstance(node, ast.ClassDef):
            return None
        node = next(
            (
                child
                for child in node.body
                if getattr(child, "name", None) == part
                and isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            ),
            None,
        )
    if node is None:
        return None
    canonical = ast.dump(node, annotate_fields=True, include_attributes=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _proposal_source_symbol_digests(
    proposals: list[dict],
    source_dir: Path,
) -> dict[str, dict[str, str]]:
    """Freeze the prepared baseline functions named by each Scout contract."""

    result: dict[str, dict[str, str]] = {}
    for proposal in proposals:
        proposal_id = str(proposal.get("proposal_id") or "")
        row = {}
        for symbol in proposal.get("source_symbols") or []:
            digest = _source_symbol_ast_digest(source_dir, str(symbol))
            if digest is None:
                raise ValueError(
                    f"proposal_source_symbol_digest_missing:{proposal_id}:{symbol}"
                )
            row[str(symbol)] = digest
        result[proposal_id] = row
    return result


def _verified_source_edges(
    graph: dict[str, set[str]],
) -> dict[str, list[str]]:
    """Resolve direct call leaves to unique frozen source symbols."""

    symbols_by_leaf: dict[str, list[str]] = {}
    for symbol in sorted(graph):
        leaf = symbol.rsplit(":", 1)[1].rsplit(".", 1)[-1]
        symbols_by_leaf.setdefault(leaf, []).append(symbol)
    return {
        caller: sorted({
            candidates[0]
            for leaf in graph[caller]
            if len(candidates := symbols_by_leaf.get(leaf, [])) == 1
            and candidates[0] != caller
        })
        for caller in sorted(graph)
    }


def _policy_abi_reachable_depths(
    graph: dict[str, set[str]],
) -> dict[str, int]:
    """Return symbols reachable from the two candidate policy ABI entries."""

    verified_edges = _verified_source_edges(graph)
    reachable = {
        symbol: 0
        for symbol in _POLICY_ABI_ENTRYPOINT_SYMBOLS
        if symbol in verified_edges
    }
    pending = list(reachable)
    while pending:
        caller = pending.pop(0)
        for callee in verified_edges.get(caller, ()):
            if callee in verified_edges and callee not in reachable:
                reachable[callee] = reachable[caller] + 1
                pending.append(callee)
    return reachable


def _source_symbol_prompt_index(
    graph: dict[str, set[str]],
    *,
    maximum_chars: int = 18_000,
) -> str:
    """Render deterministic, validator-matching call evidence for weak scouts.

    Asking a weaker model to rediscover exact ``file.py:symbol`` spellings and
    direct call leaves wastes both calls and context.  The system has already
    parsed the frozen source, so expose the accepted edge vocabulary directly.
    Lines are kept whole under a hard bound; omitted tails remain available via
    the read-only source tool but cannot be invented in a proposal.
    """
    verified_edges = _verified_source_edges(graph)

    header = (
        "SYSTEM-VERIFIED SOURCE CALL INDEX (exact proposal spellings; each arrow "
        "is a validator-accepted direct syntactic call leaf):"
    )
    lines = [header]
    used_chars = len(header)

    def append_line(line: str) -> bool:
        nonlocal used_chars
        required = len(line) + (1 if lines else 0)
        if used_chars + required > maximum_chars:
            return False
        lines.append(line)
        used_chars += required
        return True

    # Prefer only policy edges reachable from the two actual policy ABI
    # entrypoints.  A syntactically valid but dead helper must not become the
    # model's easiest copied chain merely because its name sorts first.
    reachable_depth = _policy_abi_reachable_depths(graph)
    entrypoints = set(_POLICY_ABI_ENTRYPOINT_SYMBOLS)
    preferred_candidates = [
        (
            reachable_depth[caller],
            caller,
            callee,
        )
        for caller in reachable_depth
        if caller.startswith("policy.py:")
        for callee in verified_edges.get(caller, ())
    ]

    def preferred_rank(item: tuple[int, str, str]) -> tuple:
        depth, caller, callee = item
        leaf = callee.rsplit(":", 1)[1].rsplit(".", 1)[-1].lower()
        downstream = verified_edges.get(callee, ())
        decision_score = sum(
            term in leaf for term in _DECISION_RELEVANT_SYMBOL_TERMS
        ) + sum(
            any(term in target.lower() for term in _DECISION_RELEVANT_SYMBOL_TERMS)
            for target in downstream
        )
        utility_score = sum(term in leaf for term in _UTILITY_SYMBOL_TERMS)
        return (
            decision_score <= 0,
            utility_score > 0,
            -decision_score,
            caller not in entrypoints,
            depth,
            -len(downstream),
            caller,
            callee,
        )

    preferred = sorted(preferred_candidates, key=preferred_rank)[:8]
    preferred_header = (
        "SYSTEM-VERIFIED PREFERRED CURRENT STARTING EDGES (extend through "
        "current direct edges until reachable_chain terminates at change_symbol; "
        "a two-symbol edge is complete only when its callee is change_symbol):"
    )
    preferred_lines = [
        "- " + json.dumps([caller, callee], separators=(",", ":"))
        for _depth, caller, callee in preferred
    ]
    if preferred_lines and (
        used_chars + 1 + len(preferred_header) + 1 + len(preferred_lines[0])
        <= maximum_chars
    ):
        append_line(preferred_header)
        for line in preferred_lines:
            if not append_line(line):
                break
        append_line("FULL VALIDATED EDGE INDEX:")
    # The validator requires the chain's first symbol to be reachable from the
    # candidate policy ABI.  Publishing unrelated national_bot/precompute/dead
    # helper edges made them look admissible and consumed ~8k prompt chars.
    # Every callee below is still a verified syntactic edge; only impossible
    # starting subgraphs are omitted.
    for caller in sorted(
        reachable_depth,
        key=lambda symbol: (reachable_depth[symbol], symbol),
    ):
        callees = verified_edges[caller]
        if not callees:
            continue
        line = f"- {caller} -> {', '.join(callees)}"
        if not append_line(line):
            append_line("- [remaining verified edges omitted by deterministic size bound]")
            break
    if len(lines) == 1:
        append_line("- [no validator-accepted internal call edges]")
    return "\n".join(lines)


def _snapshot_reference_prompt_index(snapshot_dir: Path) -> str:
    """Render bounded, validator-ready JSON-pointer anchors for Scout evidence."""

    root = Path(snapshot_dir)
    rows: list[str] = []
    try:
        candidates = sorted(
            path for path in root.iterdir()
            if path.is_file() and not path.is_symlink()
            and path.suffix.lower() == ".json"
            and path.name in _STRENGTH_SNAPSHOT_FILENAMES
        )[:16]
    except OSError:
        candidates = []
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload:
            pointers = []
            for key in (
                key
                for key in payload
                if str(key).lower() not in _SNAPSHOT_METADATA_ONLY_TERMINALS
            ):
                escaped = str(key).replace("~", "~0").replace("/", "~1")
                reference = f"snapshot:{path.name}#/{escaped}"
                if _snapshot_reference_evidence_binding(reference, root) is None:
                    continue
                pointers.append(reference)
                if len(pointers) >= 12:
                    break
            if not pointers:
                continue
            rows.append(
                f"- {path.name}: " + ", ".join(pointers)
            )
        elif isinstance(payload, list) and payload:
            reference = f"snapshot:{path.name}#/0"
            if _snapshot_reference_evidence_binding(reference, root) is not None:
                rows.append(f"- {path.name}: {reference}")
    if not rows:
        return ""
    return "\n".join((
        "SYSTEM-VERIFIED SNAPSHOT POINTER INDEX (Read the chosen JSON and copy "
        "at least one exact relative pointer whose node supports the weakness):",
        *rows,
    ))


def _normalize_source_symbol(value: object) -> str | None:
    text = str(value or "").strip()
    if ":" not in text:
        return None
    filename, symbol = text.rsplit(":", 1)
    filename = _safe_relative_python_path(filename)
    if filename is None:
        return None
    symbol_parts = symbol.split(".")
    if not symbol_parts or any(not part.isidentifier() for part in symbol_parts):
        return None
    return f"{filename}:{symbol}"


def _fuzzy_resolve_symbol(
    symbol: str,
    source_graph: dict,
    *,
    emit_event: bool = True,
) -> str | None:
    """Resolve a source symbol that may have a minor naming error.

    The system injects the exact call index into every scout prompt, but weak
    models still misspell function names (e.g. ``_hole_ids`` instead of
    ``_card_ids``).  This resolver corrects unambiguous within-file mismatches
    without weakening the existence guarantee: the resolved symbol must still
    be a real entry in the frozen source graph.
    """
    if symbol in source_graph:
        return symbol
    file_part, _, leaf = symbol.rpartition(":")
    if not file_part or not leaf:
        return None
    candidates = []
    for key in source_graph:
        key_file, _, key_leaf = key.rpartition(":")
        if key_file == file_part:
            candidates.append((key, key_leaf.rsplit(".", 1)[-1]))
    if not candidates:
        return None
    bare_leaf = leaf.rsplit(".", 1)[-1]
    exact = [k for k, cl in candidates if cl == bare_leaf]
    if len(exact) == 1:
        return exact[0]
    import difflib
    close = difflib.get_close_matches(
        bare_leaf, [cl for _, cl in candidates], n=1, cutoff=0.5)
    if not close:
        return None
    matches = [k for k, cl in candidates if cl == close[0]]
    if len(matches) == 1:
        if emit_event:
            from system_log import log_system_event
            log_system_event("proposal.fuzzy_symbol_resolution", "info",
                f"fuzzy resolved {symbol} to {matches[0]}",
                {"claimed": symbol, "resolved": matches[0]})
        return matches[0]
    return None


def _validated_snapshot_reference(value: object, snapshot_dir: Path | None) -> str | None:
    text = str(value or "").strip()
    if not text.startswith("snapshot:") or "#" not in text:
        return None
    path_text, locator = text[len("snapshot:"):].split("#", 1)
    relative = Path(path_text.strip().replace("\\", "/"))
    if (
        snapshot_dir is None
        or not path_text.strip()
        or relative.is_absolute()
        or ".." in relative.parts
        or not locator.strip()
    ):
        return None
    root = Path(snapshot_dir).resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    locator = locator.strip()
    if candidate.suffix.lower() != ".json" or not locator.startswith("/"):
        return None
    try:
        node = json.loads(candidate.read_text(encoding="utf-8"))
        for raw_part in locator[1:].split("/") if locator != "/" else []:
            part = raw_part.replace("~1", "/").replace("~0", "~")
            if isinstance(node, dict):
                if part not in node:
                    return None
                node = node[part]
            elif isinstance(node, list):
                if not part.isdigit() or int(part) >= len(node):
                    return None
                node = node[int(part)]
            else:
                return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    return f"snapshot:{relative.as_posix()}#{locator[:240]}"


def _snapshot_reference_evidence_binding(
    value: object,
    snapshot_dir: Path | None,
) -> dict | None:
    """Bind one strength-bearing snapshot node, not a metadata-only locator."""

    reference = _validated_snapshot_reference(value, snapshot_dir)
    if reference is None or snapshot_dir is None:
        return None
    path_text, locator = reference[len("snapshot:"):].split("#", 1)
    relative = Path(path_text)
    if (
        relative.as_posix() not in _STRENGTH_SNAPSHOT_FILENAMES
        or locator == "/"
    ):
        return None
    parts = [
        raw.replace("~1", "/").replace("~0", "~")
        for raw in locator[1:].split("/")
        if raw
    ]
    if not parts or parts[-1].lower() in _SNAPSHOT_METADATA_ONLY_TERMINALS:
        return None
    try:
        node = json.loads(
            (Path(snapshot_dir) / relative).read_text(encoding="utf-8")
        )
        for part in parts:
            if isinstance(node, dict):
                node = node[part]
            elif isinstance(node, list):
                node = node[int(part)]
            else:
                return None
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        KeyError,
        IndexError,
        TypeError,
        ValueError,
    ):
        return None
    if not _snapshot_node_has_strength_signal(node, relative.as_posix()):
        # Pool membership, manifest identity, and naked scalar values cannot
        # explain a weakness.  Admit only a bounded row/container containing a
        # concrete W/L/D, rating, action, chip, or replay-strength signal.
        return None
    canonical = json.dumps(
        node,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(canonical) < 20:
        return None
    projection = canonical[:1600]
    return {
        "reference": reference,
        "node_sha256": hashlib.sha256(
            canonical.encode("utf-8")
        ).hexdigest(),
        "resolved_projection": projection,
        "projection_sha256": hashlib.sha256(
            projection.encode("utf-8")
        ).hexdigest(),
        "projection_truncated": len(canonical) > 1600,
    }


def _proposal_substantive_contract(proposal: dict) -> dict:
    """Return only decision-bearing mechanism claims used for diversity.

    ``direction`` is scout routing and ``risks`` is advisory prose.  Neither
    may make an otherwise identical mechanism count as an independent option.
    """

    return {
        key: proposal.get(key)
        for key in _PROPOSAL_SUBSTANTIVE_FIELDS
    }


def _proposal_identity(proposal: dict) -> str:
    identity_payload = _proposal_substantive_contract(proposal)
    return hashlib.sha256(
        json.dumps(
            identity_payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:16]


def _master_proposal_repair_kind(
    direction: str,
    actual_role: str | None,
) -> str | None:
    """Return the sealed repair kind for one exact Scout provider role.

    A provider's prose cannot opt itself into recovery.  The caller must pass
    the system-owned role used for that invocation; the first attempt and
    every unrelated role remain on the repository-wide strict parser.
    """

    base_role = f"MASTER PROPOSAL {str(direction)}"
    role = str(actual_role or "")
    if role == base_role + " SCHEMA RETRY":
        return "schema"
    if role == base_role + " DISTINCTNESS RETRY":
        return "distinctness"
    return None


def _parse_master_proposal_output_with_mode(
    output: str,
    direction: str,
    *,
    actual_role: str | None = None,
) -> tuple[object | None, str]:
    """Parse a Scout result without widening the global JSON contract.

    Every output first uses the repository-wide parser, preserving all existing
    successful raw/fenced shapes.  Only when that parser fails may the one
    schema/distinctness repair recover the exact v54 failure shape: non-JSON
    prose followed by one complete JSON object with no trailing non-whitespace
    bytes.  Braces/brackets in the prefix make the object boundary ambiguous;
    arrays, multiple objects, malformed JSON, and trailing prose fail closed.
    """

    from llm_query import parse_json_output_with_mode

    raw = str(output or "")
    parsed, global_mode = parse_json_output_with_mode(raw)
    if parsed is not None:
        return parsed, global_mode
    if _master_proposal_repair_kind(direction, actual_role) is None:
        return None, global_mode
    if not raw.strip():
        return None, "PROPOSAL_REPAIR_EOF_OBJECT_REJECTED"
    object_start = raw.find("{")
    if object_start < 0:
        return None, "PROPOSAL_REPAIR_EOF_OBJECT_REJECTED"
    prefix = raw[:object_start]
    if prefix.strip() and set(prefix).intersection("{}[]"):
        return None, "PROPOSAL_REPAIR_EOF_OBJECT_AMBIGUOUS_PREFIX"
    candidate = raw[object_start:]
    try:
        parsed, end = json.JSONDecoder().raw_decode(candidate)
    except (TypeError, ValueError):
        return None, "PROPOSAL_REPAIR_EOF_OBJECT_REJECTED"
    if candidate[end:].strip() or not isinstance(parsed, dict):
        return None, "PROPOSAL_REPAIR_EOF_OBJECT_REJECTED"
    return parsed, _PROPOSAL_REPAIR_EOF_OBJECT_PARSE_MODE


def _validated_master_proposal(
    output: str,
    direction: str,
    *,
    source_graph: dict[str, set[str]] | None = None,
    snapshot_dir: Path | None = None,
    national_policy_only: bool = False,
    require_snapshot_evidence: bool = False,
    execution_mode: str = "strategy_implementation",
    evidence_mode: str | None = None,
    expected_measurement_target: str | None = None,
    forbidden_measurement_target: str | None = None,
    enforce_bindability: bool = True,
    allowed_primaries: tuple[str, ...] | None = None,
    actual_role: str | None = None,
) -> dict | None:
    """Normalize one evidence-bound proposal before critics or Master see it."""
    allowed_primaries = _canonical_proposal_primaries(allowed_primaries)
    data, _mode = _parse_master_proposal_output_with_mode(
        output or "",
        direction,
        actual_role=actual_role,
    )
    if not isinstance(data, dict):
        return None
    if any(data.get(key) for key in ("branch_from", "source_override", "source_v_override")):
        return None
    required = (
        "targeted_failure",
        "structural_change",
        "counterfactual",
        "measurement",
        "why_not_threshold_tuning",
        "expected_diff",
    )
    normalized = {
        "schema_version": _PROPOSAL_SCHEMA_VERSION,
        "direction": direction,
    }
    if execution_mode not in {
        "strategy_implementation",
        "fixed_blueprint_capability_audit",
    }:
        return None
    normalized["execution_mode"] = execution_mode
    raw_mechanism_target = data.get("mechanism_target")
    if not isinstance(raw_mechanism_target, str):
        return None
    mechanism_target = raw_mechanism_target.strip()
    if mechanism_target not in set(
        STATE_LEARNING_PRIMARY_INTERVENTION_TARGETS.values()
    ):
        return None
    normalized["mechanism_target"] = mechanism_target
    for key in required:
        value = (
            _system_bound_proposal_measurement(data.get(key), evidence_mode)
            if key == "measurement"
            else None
        ) or str(data.get(key) or "").strip()
        if len(value) < 20:
            return None
        normalized[key] = value[:1600]
    if evidence_mode is not None and not _proposal_measurement_contract_valid(
        normalized["measurement"], evidence_mode
    ):
        return None
    raw_files = data.get("target_files") or []
    if not isinstance(raw_files, list):
        return None
    target_files = []
    for value in raw_files[:3]:
        name = _safe_relative_python_path(value)
        if name is None or name in target_files:
            continue
        target_files.append(name)
    if not target_files:
        return None
    if national_policy_only:
        if target_files != ["policy.py"]:
            return None
    normalized["target_files"] = target_files

    raw_symbols = data.get("source_symbols")
    if not isinstance(raw_symbols, list) or not 1 <= len(raw_symbols) <= 8:
        return None
    source_symbols: list[str] = []
    for raw_symbol in raw_symbols:
        symbol = _normalize_source_symbol(raw_symbol)
        if symbol is None or symbol in source_symbols:
            return None
        if source_graph is not None and symbol not in source_graph:
            resolved = _fuzzy_resolve_symbol(symbol, source_graph)
            if resolved is None or resolved in source_symbols:
                return None
            symbol = resolved
        source_symbols.append(symbol)
    normalized["source_symbols"] = source_symbols

    change_symbol = _normalize_source_symbol(data.get("change_symbol"))
    if change_symbol is not None and source_graph is not None and change_symbol not in source_graph:
        change_symbol = _fuzzy_resolve_symbol(change_symbol, source_graph)
    if change_symbol is None or change_symbol not in source_symbols:
        return None
    if change_symbol.rsplit(":", 1)[0] not in target_files:
        return None
    normalized["change_symbol"] = change_symbol

    raw_chain = data.get("reachable_chain")
    if not isinstance(raw_chain, list) or not 2 <= len(raw_chain) <= 8:
        return None
    chain: list[str] = []
    for raw_symbol in raw_chain:
        symbol = _normalize_source_symbol(raw_symbol)
        if symbol is None:
            return None
        if symbol not in source_symbols and source_graph is not None:
            resolved = _fuzzy_resolve_symbol(symbol, source_graph)
            if resolved is not None and resolved in source_symbols:
                symbol = resolved
        if symbol not in source_symbols:
            return None
        chain.append(symbol)
    if len(set(chain)) != len(chain):
        return None
    if not chain or chain[-1] != change_symbol:
        return None
    if source_graph is not None:
        verified_edges = _verified_source_edges(source_graph)
        for caller, callee in zip(chain, chain[1:]):
            if callee not in verified_edges.get(caller, ()):
                return None
        if (
            national_policy_only
            and chain[0] not in _policy_abi_reachable_depths(source_graph)
        ):
            return None
    chain_files = {item.rsplit(":", 1)[0] for item in chain}
    if not chain_files.intersection(target_files):
        return None
    normalized["reachable_chain"] = chain

    falsifier = data.get("falsifier")
    falsifier_fields = {
        "test_name",
        "state_learning_primary",
        "intervention_target",
        "control",
        "intervention",
        "expected_observation",
    }
    if not isinstance(falsifier, dict) or set(falsifier) != falsifier_fields:
        return None
    normalized_falsifier = {}
    for key in falsifier_fields:
        raw_value = falsifier.get(key)
        if not isinstance(raw_value, str):
            return None
        value = raw_value.strip()
        minimum = 3 if key in {
            "test_name",
            "state_learning_primary",
            "intervention_target",
        } else 20
        if len(value) < minimum:
            return None
        if key == "test_name" and not value.replace("_", "").isalnum():
            return None
        if key == "test_name" and value not in _PROPOSAL_FALSIFIER_TESTS:
            return None
        normalized_falsifier[key] = value[:1000]
    primary = _proposal_falsifier_primary(normalized_falsifier["test_name"])
    if (
        primary is None
        or (
            allowed_primaries is not None
            and primary not in allowed_primaries
        )
        or normalized_falsifier["test_name"]
        not in STATE_LEARNING_PRIMARY_CHECKS[primary]
        or normalized_falsifier["state_learning_primary"] != primary
        or normalized_falsifier["intervention_target"]
        != STATE_LEARNING_PRIMARY_INTERVENTION_TARGETS[primary]
        or _proposal_mechanism_target_errors(
            normalized,
            normalized_falsifier,
        )
    ):
        return None
    normalized["falsifier"] = normalized_falsifier
    raw_refs = data.get("evidence_refs")
    # Weak models sometimes produce evidence_refs as a dict instead of a list.
    # Coerce dict values to a list so we don't reject otherwise valid proposals.
    if isinstance(raw_refs, dict):
        raw_refs = list(raw_refs.values())
    if not isinstance(raw_refs, list) or not 1 <= len(raw_refs) <= 10:
        return None
    evidence_refs: list[str] = []
    snapshot_evidence: list[dict] = []
    source_ref_symbols: set[str] = set()
    snapshot_ref_count = 0
    for raw_ref in raw_refs:
        text = str(raw_ref or "").strip()
        normalized_ref = None
        # Accept source:, call_index:, code:, ref: prefixes as source references.
        # Weak models frequently use the wrong prefix; the integrity guarantee
        # is that the symbol exists in the frozen source graph, not the label.
        matched_source = False
        for prefix in ("source:", "call_index:", "code:", "ref:"):
            if text.lower().startswith(prefix):
                matched_source = True
                remainder = text[len(prefix):].strip()
                # Strip trailing descriptions that weak models append.
                for sep in (" [", " \u2014", " -", " (", "\t"):
                    idx = remainder.find(sep)
                    if idx > 0:
                        remainder = remainder[:idx].strip()
                symbol = _normalize_source_symbol(remainder)
                if symbol is not None:
                    if symbol not in source_symbols and source_graph is not None:
                        resolved = _fuzzy_resolve_symbol(symbol, source_graph)
                        if resolved is not None and resolved in source_symbols:
                            symbol = resolved
                    if symbol in source_symbols:
                        normalized_ref = f"source:{symbol}"
                        source_ref_symbols.add(symbol)
                break
        if not matched_source and text.startswith("snapshot:"):
            binding = _snapshot_reference_evidence_binding(text, snapshot_dir)
            if binding is not None:
                normalized_ref = binding["reference"]
                snapshot_evidence.append(binding)
                snapshot_ref_count += 1
        if normalized_ref is None or normalized_ref in evidence_refs:
            return None
        evidence_refs.append(normalized_ref)
    if source_ref_symbols != set(source_symbols):
        return None
    if snapshot_ref_count > 2:
        return None
    if require_snapshot_evidence and snapshot_ref_count < 1:
        return None
    normalized["evidence_refs"] = evidence_refs
    normalized["snapshot_evidence"] = snapshot_evidence
    if (
        evidence_mode == "frozen_strength_snapshot"
        and not _measurement_target_bound_to_snapshot(
            normalized["measurement"],
            snapshot_evidence,
        )
    ):
        return None
    if expected_measurement_target is not None:
        parsed_measurement = _parsed_proposal_measurement(
            normalized["measurement"]
        )
        if (
            parsed_measurement is None
            or parsed_measurement["target"]
            != str(expected_measurement_target).strip().lower()
        ):
            return None
    if forbidden_measurement_target is not None:
        parsed_measurement = _parsed_proposal_measurement(
            normalized["measurement"]
        )
        if (
            parsed_measurement is None
            or parsed_measurement["target"]
            == str(forbidden_measurement_target).strip().lower()
        ):
            return None

    risks = str(data.get("risks") or "").strip()
    if len(risks) < 20:
        return None
    normalized["risks"] = risks[:1200]

    # Identity is a pure function of the proposal claims and verified evidence,
    # not scout identity, critic order, generation number, or wall clock.
    normalized["proposal_id"] = _proposal_identity(normalized)
    if enforce_bindability and _proposal_worker_bindability_error(normalized):
        return None
    return normalized


def _master_proposal_projection_hints(
    output: str,
    *,
    source_graph: dict[str, set[str]] | None = None,
    snapshot_dir: Path | None = None,
    national_policy_only: bool = False,
    require_snapshot_evidence: bool = False,
    evidence_mode: str | None = None,
    allowed_primaries: tuple[str, ...] | None = None,
) -> list[str]:
    """Return stable field-level hints without weakening proposal validation.

    Acceptance remains owned exclusively by :func:`_validated_master_proposal`.
    These codes explain common deterministic rejection points to the one
    existing schema-repair attempt, so it does not have to guess which part of
    the large object failed.  Hints never contain provider prose or paths.
    """

    from llm_query import parse_json_output_with_mode

    try:
        allowed_primaries = _canonical_proposal_primaries(allowed_primaries)
    except ValueError:
        return ["proposal_allowed_primaries_invalid"]

    data, _mode = parse_json_output_with_mode(output or "")
    if not isinstance(data, dict):
        return ["proposal_json_object_required"]
    errors: list[str] = []
    if any(data.get(key) for key in (
        "branch_from", "source_override", "source_v_override",
    )):
        errors.append("proposal_source_override_forbidden")
    for key in (
        "targeted_failure",
        "structural_change",
        "counterfactual",
        "measurement",
        "why_not_threshold_tuning",
        "expected_diff",
    ):
        if len(str(data.get(key) or "").strip()) < 20:
            errors.append(f"proposal_required_text_invalid:{key}")
    mechanism_target = data.get("mechanism_target")
    if (
        not isinstance(mechanism_target, str)
        or mechanism_target.strip()
        not in set(STATE_LEARNING_PRIMARY_INTERVENTION_TARGETS.values())
    ):
        errors.append("proposal_mechanism_target_invalid")
    measurement = (
        _system_bound_proposal_measurement(data.get("measurement"), evidence_mode)
        or str(data.get("measurement") or "")
    )
    if evidence_mode is not None and not _proposal_measurement_contract_valid(
        measurement, evidence_mode
    ):
        errors.append("proposal_measurement_contract_invalid")

    raw_files = data.get("target_files")
    target_files = []
    if isinstance(raw_files, list):
        for value in raw_files[:3]:
            normalized = _safe_relative_python_path(value)
            if normalized is not None and normalized not in target_files:
                target_files.append(normalized)
    if not target_files or (
        national_policy_only and target_files != ["policy.py"]
    ):
        errors.append("proposal_target_files_invalid")

    raw_symbols = data.get("source_symbols")
    source_symbols: list[str] = []
    if not isinstance(raw_symbols, list) or not 1 <= len(raw_symbols) <= 8:
        errors.append("proposal_source_symbols_count_invalid")
    else:
        for value in raw_symbols:
            symbol = _normalize_source_symbol(value)
            if symbol is not None and source_graph is not None and symbol not in source_graph:
                symbol = _fuzzy_resolve_symbol(
                    symbol,
                    source_graph,
                    emit_event=False,
                )
            if symbol is None or symbol in source_symbols:
                errors.append("proposal_source_symbols_invalid")
                break
            source_symbols.append(symbol)

    change_symbol = _normalize_source_symbol(data.get("change_symbol"))
    if change_symbol is not None and source_graph is not None and change_symbol not in source_graph:
        change_symbol = _fuzzy_resolve_symbol(
            change_symbol,
            source_graph,
            emit_event=False,
        )
    if change_symbol is None or change_symbol not in source_symbols:
        errors.append("proposal_change_symbol_not_in_source_symbols")
    else:
        if change_symbol.rsplit(":", 1)[0] not in target_files:
            errors.append("proposal_change_symbol_not_in_target_files")

    raw_chain = data.get("reachable_chain")
    chain: list[str] = []
    if not isinstance(raw_chain, list) or not 2 <= len(raw_chain) <= 8:
        errors.append("proposal_reachable_chain_count_invalid")
    else:
        for value in raw_chain:
            symbol = _normalize_source_symbol(value)
            if symbol is None:
                errors.append("proposal_reachable_chain_symbol_invalid")
                break
            if symbol not in source_symbols and source_graph is not None:
                resolved = _fuzzy_resolve_symbol(
                    symbol,
                    source_graph,
                    emit_event=False,
                )
                if resolved is not None and resolved in source_symbols:
                    symbol = resolved
            chain.append(symbol)
        if len(chain) != len(set(chain)):
            errors.append("proposal_reachable_chain_duplicate")
        if chain and chain[-1] != change_symbol:
            errors.append("proposal_change_symbol_not_chain_terminal")
        if chain and any(symbol not in source_symbols for symbol in chain):
            errors.append("proposal_reachable_chain_member_not_in_source_symbols")
        if source_graph is not None and len(chain) >= 2:
            verified_edges = _verified_source_edges(source_graph)
            if any(
                callee not in verified_edges.get(caller, ())
                for caller, callee in zip(chain, chain[1:])
            ):
                errors.append("proposal_reachable_chain_edge_not_current")
            if (
                national_policy_only
                and chain[0] not in _policy_abi_reachable_depths(source_graph)
            ):
                errors.append(
                    "proposal_reachable_chain_not_policy_abi_reachable"
                )
        if chain and not {
            symbol.rsplit(":", 1)[0] for symbol in chain
        }.intersection(target_files):
            errors.append("proposal_reachable_chain_target_file_missing")

    falsifier = data.get("falsifier")
    falsifier_fields = {
        "test_name",
        "state_learning_primary",
        "intervention_target",
        "control",
        "intervention",
        "expected_observation",
    }
    if (
        not isinstance(falsifier, dict)
        or set(falsifier) != falsifier_fields
        or any(not isinstance(falsifier.get(key), str) for key in falsifier_fields)
        or any(
            len(falsifier[key].strip())
            < (
                3
                if key in {
                    "test_name",
                    "state_learning_primary",
                    "intervention_target",
                }
                else 20
            )
            for key in falsifier_fields
        )
    ):
        errors.append("proposal_falsifier_invalid")
    elif falsifier["test_name"].strip() not in _PROPOSAL_FALSIFIER_TESTS:
        errors.append("proposal_falsifier_test_name_invalid")
    else:
        test_name = falsifier["test_name"].strip()
        primary = _proposal_falsifier_primary(test_name)
        if primary is None or test_name not in STATE_LEARNING_PRIMARY_CHECKS[primary]:
            errors.append("proposal_falsifier_primary_mapping_invalid")
        elif (
            allowed_primaries is not None
            and primary not in allowed_primaries
        ):
            errors.append("proposal_falsifier_primary_not_permitted")
        elif falsifier["state_learning_primary"].strip() != primary:
            errors.append(
                "proposal_falsifier_state_learning_primary_mismatch:"
                f"expected={primary}:actual="
                f"{falsifier['state_learning_primary'].strip()}"
            )
        elif falsifier["intervention_target"].strip() != (
            STATE_LEARNING_PRIMARY_INTERVENTION_TARGETS[primary]
        ):
            errors.append(
                "proposal_falsifier_intervention_target_mismatch:"
                "expected="
                f"{STATE_LEARNING_PRIMARY_INTERVENTION_TARGETS[primary]}:"
                f"actual={falsifier['intervention_target'].strip()}"
            )
        else:
            normalized_target_probe = dict(data)
            normalized_target_probe["mechanism_target"] = (
                mechanism_target.strip()
                if isinstance(mechanism_target, str)
                else mechanism_target
            )
            normalized_falsifier_probe = {
                key: value.strip()
                for key, value in falsifier.items()
                if isinstance(value, str)
            }
            errors.extend(_proposal_mechanism_target_errors(
                normalized_target_probe,
                normalized_falsifier_probe,
            ))

    raw_refs = data.get("evidence_refs")
    if isinstance(raw_refs, dict):
        raw_refs = list(raw_refs.values())
    referenced: set[str] = set()
    normalized_refs: set[str] = set()
    snapshot_ref_count = 0
    if not isinstance(raw_refs, list) or not 1 <= len(raw_refs) <= 10:
        errors.append("proposal_evidence_refs_shape_invalid")
    else:
        for value in raw_refs:
            text = str(value or "").strip()
            normalized_ref = None
            matched_source = False
            for prefix in ("source:", "call_index:", "code:", "ref:"):
                if text.lower().startswith(prefix):
                    matched_source = True
                    remainder = text[len(prefix):].strip()
                    for separator in (" [", " —", " -", " (", "\t"):
                        index = remainder.find(separator)
                        if index > 0:
                            remainder = remainder[:index].strip()
                    symbol = _normalize_source_symbol(remainder)
                    if (
                        symbol is not None
                        and symbol not in source_symbols
                        and source_graph is not None
                    ):
                        resolved = _fuzzy_resolve_symbol(
                            symbol,
                            source_graph,
                            emit_event=False,
                        )
                        if resolved is not None and resolved in source_symbols:
                            symbol = resolved
                    if symbol is not None:
                        if symbol in source_symbols:
                            referenced.add(symbol)
                            normalized_ref = f"source:{symbol}"
                    break
            if not matched_source and text.startswith("snapshot:"):
                binding = _snapshot_reference_evidence_binding(
                    text,
                    snapshot_dir,
                )
                normalized_ref = (
                    binding.get("reference")
                    if isinstance(binding, dict)
                    else None
                )
                if normalized_ref is not None:
                    snapshot_ref_count += 1
            if normalized_ref is None or normalized_ref in normalized_refs:
                errors.append("proposal_evidence_ref_invalid")
            else:
                normalized_refs.add(normalized_ref)
        if source_symbols and referenced != set(source_symbols):
            errors.append("proposal_evidence_refs_incomplete")
        if require_snapshot_evidence and snapshot_ref_count < 1:
            errors.append("proposal_snapshot_evidence_required")
        if snapshot_ref_count > 2:
            errors.append("proposal_snapshot_evidence_too_many")
    if len(str(data.get("risks") or "").strip()) < 20:
        errors.append("proposal_risks_invalid")
    budget_probe = _validated_master_proposal(
        output,
        "projection",
        source_graph=source_graph,
        snapshot_dir=snapshot_dir,
        national_policy_only=national_policy_only,
        require_snapshot_evidence=require_snapshot_evidence,
        execution_mode=(
            "fixed_blueprint_capability_audit"
            if evidence_mode == "fresh_strict_control_no_strength"
            else "strategy_implementation"
        ),
        evidence_mode=evidence_mode,
        enforce_bindability=False,
        allowed_primaries=allowed_primaries,
    )
    if isinstance(budget_probe, dict):
        bindability_error = _proposal_worker_bindability_error(budget_probe)
        if bindability_error:
            errors.append(bindability_error)
    return list(dict.fromkeys(errors))


def _validated_proposal_critique(output: str, proposal_ids: set[str]) -> dict | None:
    from llm_query import parse_json_output_with_mode

    data, _mode = parse_json_output_with_mode(output or "")
    if not isinstance(data, dict) or set(data) != {"ballots"}:
        return None
    raw_ballots = data.get("ballots")
    if not isinstance(raw_ballots, list) or len(raw_ballots) != len(proposal_ids):
        return None
    ballots = []
    seen: set[str] = set()
    for raw_ballot in raw_ballots:
        if not isinstance(raw_ballot, dict) or set(raw_ballot) != {
            "proposal_id",
            "scores",
            "reject",
            "reason",
        }:
            return None
        proposal_id = raw_ballot.get("proposal_id")
        scores = raw_ballot.get("scores")
        reason_value = raw_ballot.get("reason")
        reason = reason_value.strip() if isinstance(reason_value, str) else ""
        reject = raw_ballot.get("reject")
        if (
            not isinstance(proposal_id, str)
            or proposal_id not in proposal_ids
            or proposal_id in seen
            or not isinstance(scores, dict)
            or set(scores) != set(_PROPOSAL_CRITIC_CRITERIA)
            or not isinstance(reject, bool)
            or len(reason) < 12
        ):
            return None
        normalized_scores = {}
        for criterion in _PROPOSAL_CRITIC_CRITERIA:
            score = scores.get(criterion)
            if isinstance(score, bool) or not isinstance(score, int) or not 1 <= score <= 5:
                return None
            normalized_scores[criterion] = score
        seen.add(proposal_id)
        ballots.append({
            "proposal_id": proposal_id,
            "scores": normalized_scores,
            "total_score": sum(normalized_scores.values()),
            "reject": reject,
            "reason": reason[:1000],
        })
    if seen != proposal_ids:
        return None
    ranking = [
        item["proposal_id"]
        for item in sorted(
            ballots,
            key=lambda item: (
                item["reject"],
                -item["total_score"],
                item["proposal_id"],
            ),
        )
    ]
    return {
        "ranking": ranking,
        "reject": [item["proposal_id"] for item in ballots if item["reject"]],
        "ballots": ballots,
    }


def _proposal_packet_error(
    reason: str,
    *,
    context_digest: str = "",
    source_code_digest: str = "",
) -> str:
    return json.dumps({
        "schema_version": _PROPOSAL_PACKET_SCHEMA_VERSION,
        "valid": False,
        "reason": str(reason)[:500],
        "context_digest": context_digest,
        "source_code_digest": source_code_digest,
        "proposal_count": 0,
        "valid_critic_count": 0,
        "allowed_proposal_ids": [],
        "ordered_proposals": [],
        "proposal_source_symbol_digests": {},
        "proposal_invocations": {},
        "critic_reviews": [],
    }, ensure_ascii=False, sort_keys=True)


def _parse_valid_proposal_packet_impl(
    packet_text: str,
) -> tuple[dict | None, list[str]]:
    """Validate the machine packet again at the final-Master trust boundary."""
    try:
        packet = json.loads(packet_text)
    except (TypeError, json.JSONDecodeError):
        return None, ["proposal_packet_not_json"]
    if not isinstance(packet, dict):
        return None, ["proposal_packet_not_object"]
    errors = []
    if packet.get("schema_version") != _PROPOSAL_PACKET_SCHEMA_VERSION:
        errors.append("proposal_packet_schema_mismatch")
    if packet.get("valid") is not True:
        errors.append(f"proposal_packet_invalid:{packet.get('reason', 'unknown')}")
        # Error packets intentionally contain only the primary rejection and
        # safe identity fields. Do not feed that reduced shape through the
        # success-packet validator and obscure the actual cause with secondary
        # field, evidence-mode, and critic diagnostics.
        return None, errors
    expected_packet_fields = {
        "schema_version",
        "valid",
        "authority",
        "context_digest",
        "source_code_digest",
        "evidence_mode",
        "critic_criteria",
        "proposal_count",
        "valid_critic_count",
        "allowed_proposal_ids",
        "ordered_proposals",
        "proposal_source_symbol_digests",
        "proposal_invocations",
        "critic_reviews",
    }
    if set(packet) != expected_packet_fields:
        errors.append("proposal_packet_fields_mismatch")
    evidence_mode = str(packet.get("evidence_mode") or "")
    expected_execution_mode = {
        "frozen_strength_snapshot": "strategy_implementation",
        "singleton_parent_no_strength": "strategy_implementation",
        "fresh_strict_control_no_strength": "fixed_blueprint_capability_audit",
    }.get(evidence_mode)
    if expected_execution_mode is None:
        errors.append("proposal_packet_evidence_mode_invalid")
    proposals = packet.get("ordered_proposals")
    allowed = packet.get("allowed_proposal_ids")
    if not isinstance(proposals, list) or len(proposals) != 3:
        errors.append("proposal_packet_requires_exactly_three_proposals")
        proposals = []
    if packet.get("proposal_count") != 3:
        errors.append("proposal_packet_count_must_be_three")
    proposal_ids = [
        str(item.get("proposal_id") or "")
        for item in proposals
        if isinstance(item, dict)
    ]
    if (
        len(proposal_ids) != len(proposals)
        or len(set(proposal_ids)) != len(proposal_ids)
        or not isinstance(allowed, list)
        or not 1 <= len(allowed) <= len(proposal_ids)
        or len(set(map(str, allowed))) != len(allowed)
        or not set(map(str, allowed)).issubset(set(proposal_ids))
    ):
        errors.append("proposal_packet_id_set_mismatch")
    required_proposal_fields = {
        "schema_version",
        "direction",
        "proposal_id",
        "targeted_failure",
        "structural_change",
        "counterfactual",
        "measurement",
        "why_not_threshold_tuning",
        "mechanism_target",
        "expected_diff",
        "target_files",
        "source_symbols",
        "change_symbol",
        "reachable_chain",
        "falsifier",
        "evidence_refs",
        "snapshot_evidence",
        "execution_mode",
        "risks",
    }
    for item in proposals:
        if not isinstance(item, dict):
            continue
        if set(item) != required_proposal_fields:
            errors.append(f"proposal_packet_fields_missing:{item.get('proposal_id', '')}")
            continue
        proposal_id = item.get("proposal_id")
        malformed_shape = False
        if (
            not isinstance(proposal_id, str)
            or re.fullmatch(r"[0-9a-f]{16}", proposal_id) is None
        ):
            errors.append("proposal_id_invalid")
            malformed_shape = True
        scalar_minimums = {
            "targeted_failure": 20,
            "structural_change": 20,
            "counterfactual": 20,
            "measurement": 20,
            "why_not_threshold_tuning": 20,
            "expected_diff": 20,
            "risks": 20,
        }
        for field, minimum in scalar_minimums.items():
            value = item.get(field)
            if not isinstance(value, str) or len(value.strip()) < minimum:
                errors.append(f"proposal_packet_{field}_invalid:{proposal_id or ''}")
                malformed_shape = True
        if not isinstance(item.get("change_symbol"), str):
            errors.append(
                f"proposal_packet_change_symbol_invalid:{proposal_id or ''}"
            )
            malformed_shape = True
        collection_contracts = {
            "target_files": (1, 3),
            "source_symbols": (1, 8),
            "reachable_chain": (2, 8),
            "evidence_refs": (1, 10),
            "snapshot_evidence": (0, 2),
        }
        for field, (minimum, maximum) in collection_contracts.items():
            value = item.get(field)
            if (
                not isinstance(value, list)
                or not minimum <= len(value) <= maximum
                or (
                    field != "snapshot_evidence"
                    and any(not isinstance(entry, str) for entry in value)
                )
            ):
                errors.append(
                    f"proposal_packet_{field}_shape_invalid:{proposal_id or ''}"
                )
                malformed_shape = True
        if malformed_shape:
            continue
        if item.get("schema_version") != _PROPOSAL_SCHEMA_VERSION:
            errors.append(f"proposal_schema_mismatch:{item.get('proposal_id', '')}")
        if item.get("execution_mode") != expected_execution_mode:
            errors.append(
                f"proposal_execution_mode_mismatch:{item.get('proposal_id', '')}"
            )
        if not _proposal_measurement_contract_valid(
            str(item.get("measurement") or ""),
            evidence_mode,
        ):
            errors.append(
                f"proposal_measurement_contract_invalid:{item.get('proposal_id', '')}"
            )
        if item.get("mechanism_target") not in set(
            STATE_LEARNING_PRIMARY_INTERVENTION_TARGETS.values()
        ):
            errors.append(
                f"proposal_mechanism_target_invalid:{item.get('proposal_id', '')}"
            )
        change_symbol = _normalize_source_symbol(item.get("change_symbol"))
        source_symbols = list(map(str, item.get("source_symbols") or []))
        reachable_chain = list(map(str, item.get("reachable_chain") or []))
        target_files = list(map(str, item.get("target_files") or []))
        if change_symbol != item.get("change_symbol"):
            errors.append(
                f"proposal_change_symbol_invalid:{item.get('proposal_id', '')}"
            )
        elif change_symbol not in source_symbols:
            errors.append(
                "proposal_change_symbol_not_in_source_symbols:"
                f"{item.get('proposal_id', '')}"
            )
        else:
            if change_symbol.rsplit(":", 1)[0] not in target_files:
                errors.append(
                    "proposal_change_symbol_not_in_target_files:"
                    f"{item.get('proposal_id', '')}"
                )
            if not reachable_chain or reachable_chain[-1] != change_symbol:
                errors.append(
                    "proposal_change_symbol_not_chain_terminal:"
                    f"{item.get('proposal_id', '')}"
                )
        falsifier = item.get("falsifier")
        falsifier_fields = {
            "test_name",
            "state_learning_primary",
            "intervention_target",
            "control",
            "intervention",
            "expected_observation",
        }
        if (
            not isinstance(falsifier, dict)
            or set(falsifier) != falsifier_fields
            or any(not isinstance(falsifier.get(key), str) for key in falsifier_fields)
        ):
            errors.append(
                f"proposal_falsifier_invalid:{item.get('proposal_id', '')}"
            )
        else:
            test_name = falsifier["test_name"].strip()
            primary = _proposal_falsifier_primary(test_name)
            if (
                primary is None
                or test_name not in STATE_LEARNING_PRIMARY_CHECKS[primary]
            ):
                errors.append(
                    "proposal_falsifier_primary_mapping_invalid:"
                    f"{item.get('proposal_id', '')}"
                )
            elif falsifier["state_learning_primary"].strip() != primary:
                errors.append(
                    "proposal_falsifier_state_learning_primary_mismatch:"
                    f"{item.get('proposal_id', '')}:expected={primary}:actual="
                    f"{falsifier['state_learning_primary'].strip()}"
                )
            elif falsifier["intervention_target"].strip() != (
                STATE_LEARNING_PRIMARY_INTERVENTION_TARGETS[primary]
            ):
                errors.append(
                    "proposal_falsifier_intervention_target_mismatch:"
                    f"{item.get('proposal_id', '')}:expected="
                    f"{STATE_LEARNING_PRIMARY_INTERVENTION_TARGETS[primary]}:"
                    f"actual={falsifier['intervention_target'].strip()}"
                )
            else:
                errors.extend(
                    error + f":{item.get('proposal_id', '')}"
                    for error in _proposal_mechanism_target_errors(
                        item,
                        falsifier,
                    )
                )
        try:
            bindability_error = _proposal_worker_bindability_error(item)
        except Exception:
            errors.append(
                f"proposal_worker_binding_invalid:{item.get('proposal_id', '')}"
            )
        else:
            if bindability_error:
                errors.append(bindability_error)
        snapshot_evidence = item.get("snapshot_evidence")
        if not isinstance(snapshot_evidence, list):
            errors.append(
                f"proposal_snapshot_evidence_not_list:{item.get('proposal_id', '')}"
            )
            snapshot_evidence = []
        if evidence_mode == "frozen_strength_snapshot" and not snapshot_evidence:
            errors.append(
                f"proposal_snapshot_evidence_missing:{item.get('proposal_id', '')}"
            )
        if evidence_mode != "frozen_strength_snapshot" and snapshot_evidence:
            errors.append(
                f"proposal_snapshot_evidence_forbidden:{item.get('proposal_id', '')}"
            )
        snapshot_refs = []
        for binding in snapshot_evidence:
            if not isinstance(binding, dict) or set(binding) != {
                "reference",
                "node_sha256",
                "resolved_projection",
                "projection_sha256",
                "projection_truncated",
            }:
                errors.append(
                    f"proposal_snapshot_binding_invalid:{item.get('proposal_id', '')}"
                )
                continue
            projection = str(binding.get("resolved_projection") or "")
            reference = str(binding.get("reference") or "")
            if (
                not reference.startswith("snapshot:")
                or reference not in (item.get("evidence_refs") or [])
                or not re.fullmatch(r"[0-9a-f]{64}", str(binding.get("node_sha256") or ""))
                or binding.get("projection_sha256")
                != hashlib.sha256(projection.encode("utf-8")).hexdigest()
                or not isinstance(binding.get("projection_truncated"), bool)
                or len(projection) < 20
                or len(projection) > 1600
            ):
                errors.append(
                    f"proposal_snapshot_binding_invalid:{item.get('proposal_id', '')}"
                )
            snapshot_refs.append(reference)
        expected_snapshot_refs = [
            str(ref)
            for ref in (item.get("evidence_refs") or [])
            if str(ref).startswith("snapshot:")
        ]
        if snapshot_refs != expected_snapshot_refs:
            errors.append(
                f"proposal_snapshot_binding_set_mismatch:{item.get('proposal_id', '')}"
            )
        if (
            evidence_mode == "frozen_strength_snapshot"
            and not _measurement_target_bound_to_snapshot(
                str(item.get("measurement") or ""),
                snapshot_evidence,
            )
        ):
            errors.append(
                f"proposal_measurement_target_not_snapshot_bound:"
                f"{item.get('proposal_id', '')}"
            )
        if item.get("proposal_id") != _proposal_identity(item):
            errors.append(f"proposal_identity_mismatch:{item.get('proposal_id', '')}")
    source_symbol_digests = packet.get("proposal_source_symbol_digests")
    if (
        not isinstance(source_symbol_digests, dict)
        or set(source_symbol_digests) != set(proposal_ids)
    ):
        errors.append("proposal_source_symbol_digest_set_mismatch")
        source_symbol_digests = {}
    for proposal in proposals:
        if not isinstance(proposal, dict):
            continue
        proposal_id = str(proposal.get("proposal_id") or "")
        row = source_symbol_digests.get(proposal_id)
        symbols = proposal.get("source_symbols")
        if (
            not isinstance(row, dict)
            or not isinstance(symbols, list)
            or set(row) != set(map(str, symbols))
            or any(
                re.fullmatch(r"[0-9a-f]{64}", str(value or "")) is None
                for value in (row or {}).values()
            )
        ):
            errors.append(
                f"proposal_source_symbol_digest_invalid:{proposal_id}"
            )
    proposal_invocations = packet.get("proposal_invocations")
    if (
        not isinstance(proposal_invocations, dict)
        or set(proposal_invocations) != set(proposal_ids)
    ):
        errors.append("proposal_invocation_set_mismatch")
        proposal_invocations = {}
    invocation_ids: list[str] = []
    try:
        from bot_artifact import canonical_digest
        from system_strict_bootstrap import validate_llm_invocation_evidence

        for proposal in proposals:
            if not isinstance(proposal, dict):
                continue
            proposal_id = str(proposal.get("proposal_id") or "")
            evidence = proposal_invocations.get(proposal_id)
            direction = str(proposal.get("direction") or "")
            evidence_errors = validate_llm_invocation_evidence(
                evidence,
                expected_purpose=f"master_proposal_scout:{direction}",
            )
            errors.extend(
                f"proposal_invocation_invalid:{proposal_id}:{item}"
                for item in evidence_errors
            )
            if isinstance(evidence, dict):
                invocation_ids.append(str(evidence.get("invocation_id") or ""))
                role = str(evidence.get("role") or "")
                if not role.startswith(f"MASTER PROPOSAL {direction}"):
                    errors.append(
                        f"proposal_invocation_role_mismatch:{proposal_id}"
                    )
                if evidence.get("role_result_digest") != canonical_digest(proposal):
                    errors.append(
                        f"proposal_invocation_result_mismatch:{proposal_id}"
                    )
    except Exception as exc:
        errors.append(
            f"proposal_invocation_validation_error:{type(exc).__name__}"
        )
    if packet.get("valid_critic_count") != 2:
        errors.append("proposal_packet_requires_two_valid_critics")
    reviews = packet.get("critic_reviews")
    expected_critic_ids = {"falsification", "scope"}
    if not isinstance(reviews, list) or len(reviews) != 2:
        errors.append("proposal_packet_requires_two_critic_reviews")
        reviews = []
    critic_ids = {
        str(review.get("critic_id") or "")
        for review in reviews
        if isinstance(review, dict)
    }
    if critic_ids != expected_critic_ids:
        errors.append("proposal_critic_identity_set_mismatch")
    try:
        from bot_artifact import canonical_digest
        from system_strict_bootstrap import validate_llm_invocation_evidence

        reject_counts = {proposal_id: 0 for proposal_id in proposal_ids}
        for review in reviews:
            if not isinstance(review, dict):
                errors.append("proposal_critic_review_not_object")
                continue
            critic_id = str(review.get("critic_id") or "")
            if set(review) != {
                "critic_id",
                "ranking",
                "reject",
                "ballots",
                "invocation_evidence",
            }:
                errors.append(f"proposal_critic_review_fields_mismatch:{critic_id}")
            ballots = review.get("ballots")
            if not isinstance(ballots, list) or len(ballots) != len(proposal_ids):
                errors.append(f"proposal_critic_ballot_count_mismatch:{critic_id}")
                ballots = []
            seen_ballots: set[str] = set()
            normalized_ballots = []
            for ballot in ballots:
                if not isinstance(ballot, dict) or set(ballot) != {
                    "proposal_id",
                    "scores",
                    "total_score",
                    "reject",
                    "reason",
                }:
                    errors.append(
                        f"proposal_critic_ballot_fields_mismatch:{critic_id}"
                    )
                    continue
                proposal_id = ballot.get("proposal_id")
                scores = ballot.get("scores")
                reject = ballot.get("reject")
                reason = ballot.get("reason")
                if (
                    not isinstance(proposal_id, str)
                    or proposal_id not in set(proposal_ids)
                    or proposal_id in seen_ballots
                    or not isinstance(scores, dict)
                    or set(scores) != set(_PROPOSAL_CRITIC_CRITERIA)
                    or not isinstance(reject, bool)
                    or not isinstance(reason, str)
                    or len(reason.strip()) < 12
                ):
                    errors.append(
                        f"proposal_critic_ballot_schema_invalid:{critic_id}"
                    )
                    continue
                if any(
                    isinstance(score, bool)
                    or not isinstance(score, int)
                    or not 1 <= score <= 5
                    for score in scores.values()
                ):
                    errors.append(
                        f"proposal_critic_ballot_score_invalid:{critic_id}"
                    )
                    continue
                total = sum(scores.values())
                if ballot.get("total_score") != total:
                    errors.append(
                        f"proposal_critic_ballot_total_mismatch:{critic_id}"
                    )
                seen_ballots.add(proposal_id)
                normalized_ballots.append(ballot)
                if reject:
                    reject_counts[proposal_id] += 1
            if seen_ballots != set(proposal_ids):
                errors.append(f"proposal_critic_ballot_set_mismatch:{critic_id}")
            expected_ranking = [
                item["proposal_id"]
                for item in sorted(
                    normalized_ballots,
                    key=lambda item: (
                        item["reject"],
                        -item["total_score"],
                        item["proposal_id"],
                    ),
                )
            ]
            expected_reject = [
                item["proposal_id"]
                for item in normalized_ballots
                if item["reject"]
            ]
            if review.get("ranking") != expected_ranking:
                errors.append(f"proposal_critic_ranking_mismatch:{critic_id}")
            if review.get("reject") != expected_reject:
                errors.append(f"proposal_critic_reject_mismatch:{critic_id}")
            evidence = review.get("invocation_evidence")
            evidence_errors = validate_llm_invocation_evidence(
                evidence,
                expected_purpose=f"master_proposal_critic:{critic_id}",
            )
            errors.extend(
                f"proposal_critic_invocation_invalid:{critic_id}:{item}"
                for item in evidence_errors
            )
            if isinstance(evidence, dict):
                invocation_ids.append(str(evidence.get("invocation_id") or ""))
                role = str(evidence.get("role") or "")
                if not role.startswith(f"MASTER PROPOSAL CRITIC {critic_id}"):
                    errors.append(
                        f"proposal_critic_invocation_role_mismatch:{critic_id}"
                    )
                role_result = {
                    key: value
                    for key, value in review.items()
                    if key not in {"critic_id", "invocation_evidence"}
                }
                if evidence.get("role_result_digest") != canonical_digest(
                    role_result
                ):
                    errors.append(
                        f"proposal_critic_invocation_result_mismatch:{critic_id}"
                    )
        expected_allowed = [
            proposal_id
            for proposal_id in proposal_ids
            if reject_counts.get(proposal_id, 0) < 2
        ]
        if list(map(str, allowed or [])) != expected_allowed:
            errors.append("proposal_packet_allowed_ids_veto_mismatch")
        if not expected_allowed:
            errors.append("proposal_packet_all_proposals_unanimously_rejected")
    except Exception as exc:
        errors.append(
            f"proposal_critic_invocation_validation_error:{type(exc).__name__}"
        )
    if len(invocation_ids) != 5 or len(set(invocation_ids)) != 5:
        errors.append("proposal_packet_invocations_not_independent")
    if packet.get("critic_criteria") != _PROPOSAL_CRITIC_CRITERIA:
        errors.append("proposal_critic_criteria_mismatch")
    context_digest = str(packet.get("context_digest") or "")
    source_digest = str(packet.get("source_code_digest") or "")
    if (
        len(context_digest) != 64
        or len(source_digest) != 64
        or any(char not in "0123456789abcdef" for char in context_digest + source_digest)
    ):
        errors.append("proposal_packet_digest_invalid")
    return (None, errors) if errors else (packet, [])


def _parse_valid_proposal_packet(packet_text: str) -> tuple[dict | None, list[str]]:
    """Total fail-closed wrapper around durable proposal-packet validation."""

    try:
        return _parse_valid_proposal_packet_impl(packet_text)
    except Exception as exc:
        return None, [
            "proposal_packet_validation_error:"
            f"{type(exc).__name__}:{str(exc)[:200]}"
        ]


def _proposal_binding_error(code: str, payload: dict) -> str:
    return code + ":" + json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _provider_prompt_reserved_markers(prompt: str) -> tuple[str, ...]:
    """Return system-owned delimiters a provider is never allowed to emit."""

    from plan_compiler import (
        SELECTED_PROPOSAL_BEGIN,
        SELECTED_PROPOSAL_END,
        SYSTEM_OWNED_CONTRACT_BEGIN,
        SYSTEM_OWNED_CONTRACT_END,
    )

    return tuple(
        marker
        for marker in (
            SELECTED_PROPOSAL_BEGIN,
            SELECTED_PROPOSAL_END,
            SYSTEM_OWNED_CONTRACT_BEGIN,
            SYSTEM_OWNED_CONTRACT_END,
        )
        if marker in prompt
    )


def _canonical_provider_worker_prompt(prompt: str) -> str:
    """Return exactly the provider text that selected-contract binding uses.

    The binder has always removed a trailing Unicode-whitespace suffix before
    appending system-owned blocks.  Validate the same canonical text so a
    prompt at a selected cap has one arithmetic meaning in the model repair
    error, the final bind, and the later compiler.  This is whitespace
    normalization only: provider-authored non-whitespace text is never
    shortened to make a plan fit.
    """

    return prompt.rstrip()


def _task_proposal_scope_paths(task: dict) -> tuple[set[str], tuple[dict, ...]]:
    """Parse proposal-writable task paths without iterating provider scalars."""

    paths: set[str] = set()
    invalid: list[dict] = []
    for field in ("target_files", "files_allowed"):
        if field not in task:
            continue
        values = task.get(field)
        if not isinstance(values, list):
            invalid.append({
                "field": field,
                "expected_type": "list",
                "actual_type": type(values).__name__,
            })
            continue
        for index, value in enumerate(values):
            if not isinstance(value, str):
                invalid.append({
                    "field": field,
                    "index": index,
                    "expected_type": "str",
                    "actual_type": type(value).__name__,
                })
                continue
            path = _safe_relative_python_path(value)
            if path is not None:
                paths.add(path)
    return paths, tuple(invalid)


def _resolve_allowed_selected_proposal(
    data: dict,
    packet: dict,
) -> tuple[dict | None, list[str]]:
    """Resolve the one provider-selected immutable proposal, or fail closed."""

    if not isinstance(data, dict):
        return None, ["master_output_not_object"]
    selected = data.get("selected_proposal_id")
    if not isinstance(selected, str):
        return None, ["selected_proposal_id_must_be_one_string"]
    proposals = {
        item["proposal_id"]: item
        for item in packet.get("ordered_proposals", [])
        if isinstance(item, dict) and isinstance(item.get("proposal_id"), str)
    }
    proposal = proposals.get(selected)
    if (
        proposal is None
        or selected not in set(map(str, packet.get("allowed_proposal_ids") or []))
    ):
        return None, [f"selected_proposal_id_not_allowed:{selected}"]
    return proposal, []


def _canonicalize_selected_proposal_metadata(
    data: dict,
    packet: dict,
) -> tuple[dict, dict | None, list[str], tuple[str, ...]]:
    """Bind duplicated display metadata to the selected immutable proposal.

    ``selected_proposal_id`` is the provider's one semantic selection.  Its
    ``targeted_failure`` and ``measurement`` are already sealed in the
    proposal packet, so letting a final-Master free-text duplicate override or
    accidentally paraphrase them only creates a non-causal retry failure.  The
    system therefore derives the two duplicate plan fields before any schema,
    Worker, or strict-authority projection consumes the plan.  Selection,
    task scope, runtime contract, and provider prompt remain independently
    validated below.
    """

    proposal, errors = _resolve_allowed_selected_proposal(data, packet)
    if errors or proposal is None:
        return data, None, errors, ()
    result = json.loads(json.dumps(data, ensure_ascii=False))
    expected = {
        "targeted_failure": str(proposal["targeted_failure"]),
        "measurement_plan": str(proposal["measurement"]),
    }
    rebound = tuple(
        key for key, value in expected.items() if result.get(key) != value
    )
    result.update(expected)
    return result, proposal, [], rebound


def _validate_final_proposal_binding(data: dict, packet: dict) -> list[str]:
    """Require one exact proposal selection and its writable-file contract."""
    proposal, selection_errors = _resolve_allowed_selected_proposal(data, packet)
    if selection_errors or proposal is None:
        return selection_errors
    selected = str(data["selected_proposal_id"])
    errors = []
    if str(data.get("targeted_failure") or "").strip() != proposal["targeted_failure"]:
        errors.append("targeted_failure_must_exactly_copy_selected_proposal")
    if str(data.get("measurement_plan") or "").strip() != proposal["measurement"]:
        errors.append("measurement_plan_must_exactly_copy_selected_proposal")
    writable: set[str] = set()
    tasks = data.get("tasks")
    task_scopes: list[tuple[dict, set[str]]] = []
    if isinstance(tasks, list):
        for task in tasks:
            if not isinstance(task, dict):
                continue
            task_files, scope_errors = _task_proposal_scope_paths(task)
            task_scopes.append((task, task_files))
            writable.update(task_files)
            for scope_error in scope_errors:
                errors.append(_proposal_binding_error(
                    "selected_proposal_worker_scope_type_invalid",
                    {
                        "worker_id": task.get("worker_id"),
                        "proposal_id": selected,
                        **scope_error,
                    },
                ))
    missing_files = sorted(set(proposal["target_files"]) - writable)
    if missing_files:
        errors.append(f"selected_proposal_target_files_not_writable:{missing_files}")
    compilation = _selected_proposal_compilation_contract(proposal)
    binding_chars = int(compilation["reserved_selected_contract_chars"])
    expected_primary = str(compilation["state_learning_primary"])
    selected_check = str(compilation["falsifier_test_name"])
    required_primary_checks = set(map(
        str,
        compilation["required_primary_checks"],
    ))
    bound_task_count = 0
    falsifier_check_bound = False
    observed_primaries: list[dict] = []
    if isinstance(tasks, list):
        for task, task_files in task_scopes:
            if not task_files.intersection(proposal["target_files"]):
                continue
            bound_task_count += 1
            try:
                from output_schema import RuntimeContract

                runtime_contract = RuntimeContract.model_validate(
                    task.get("runtime_contract")
                )
            except Exception:
                runtime_contract = None
            state_learning = (
                runtime_contract.state_learning
                if runtime_contract is not None
                else None
            )
            checks_required = task.get("checks_required") or []
            actual_primary = (
                state_learning.primary_innovation()
                if state_learning is not None
                else None
            )
            task_checks = (
                set(map(str, checks_required))
                if isinstance(checks_required, list)
                else set()
            )
            observed_primaries.append({
                "worker_id": task.get("worker_id", bound_task_count),
                "state_learning_primary": actual_primary,
                "checks_required": sorted(task_checks),
            })
            if actual_primary == expected_primary:
                missing_checks = sorted(required_primary_checks - task_checks)
                if not missing_checks and selected_check in required_primary_checks:
                    falsifier_check_bound = True
                elif missing_checks:
                    errors.append(_proposal_binding_error(
                        "selected_proposal_primary_checks_missing",
                        {
                            "worker_id": task.get("worker_id", bound_task_count),
                            "proposal_id": selected,
                            "state_learning_primary": expected_primary,
                            "proposal_falsifier": selected_check,
                            "missing_checks": missing_checks,
                            "required_primary_checks": sorted(required_primary_checks),
                        },
                    ))
            raw_prompt = task.get("worker_prompt")
            if not isinstance(raw_prompt, str):
                errors.append(_proposal_binding_error(
                    "selected_proposal_worker_prompt_type_invalid",
                    {
                        "worker_id": task.get("worker_id", bound_task_count),
                        "proposal_id": selected,
                        "expected_type": "str",
                        "actual_type": type(raw_prompt).__name__,
                    },
                ))
                continue
            prompt = _canonical_provider_worker_prompt(raw_prompt)
            if len(prompt.strip()) < WORKER_PROMPT_MIN_CHARS:
                errors.append(_proposal_binding_error(
                    "selected_proposal_worker_prompt_below_minimum",
                    {
                        "worker_id": task.get("worker_id", bound_task_count),
                        "proposal_id": selected,
                        "actual_provider_chars": len(prompt),
                        "actual_non_whitespace_chars": len(prompt.strip()),
                        "minimum_provider_chars": WORKER_PROMPT_MIN_CHARS,
                    },
                ))
            reserved_markers = _provider_prompt_reserved_markers(prompt)
            if reserved_markers:
                errors.append(_proposal_binding_error(
                    "selected_proposal_worker_prompt_reserved_marker",
                    {
                        "worker_id": task.get("worker_id", bound_task_count),
                        "proposal_id": selected,
                        "reserved_markers": list(reserved_markers),
                    },
                ))
            runtime_contract_reserve = int(
                compilation["reserved_runtime_contract_max_chars"]
            )
            combined_chars = (
                len(prompt) + binding_chars + 2 + runtime_contract_reserve
            )
            if combined_chars > WORKER_PROMPT_MAX_CHARS:
                budget_payload = {
                    "worker_id": task.get("worker_id", bound_task_count),
                    "proposal_id": selected,
                    "actual_provider_chars": len(prompt),
                    "reserved_selected_contract_chars": binding_chars,
                    "reserved_runtime_contract_max_chars": (
                        runtime_contract_reserve
                    ),
                    "separator_chars": 2,
                    "combined_chars": combined_chars,
                    "global_cap_chars": WORKER_PROMPT_MAX_CHARS,
                    "max_provider_chars": compilation["max_provider_chars"],
                    "overflow_chars": combined_chars - WORKER_PROMPT_MAX_CHARS,
                    "character_metric": compilation["character_metric"],
                }
                if len(raw_prompt) != len(prompt):
                    budget_payload["submitted_provider_chars"] = len(raw_prompt)
                    budget_payload["trimmed_trailing_whitespace_chars"] = (
                        len(raw_prompt) - len(prompt)
                    )
                errors.append(_proposal_binding_error(
                    "selected_proposal_worker_prompt_has_no_binding_budget",
                    budget_payload,
                ))
    if bound_task_count == 0 and not missing_files:
        errors.append("selected_proposal_has_no_bound_worker_task")
    elif bound_task_count and not falsifier_check_bound:
        errors.append(_proposal_binding_error(
            "selected_proposal_falsifier_not_bound_to_runtime_primary_check",
            {
                "proposal_id": selected,
                "proposal_falsifier": selected_check,
                "expected_state_learning_primary": expected_primary,
                "required_primary_checks": sorted(required_primary_checks),
                "observed_bound_tasks": observed_primaries,
            },
        ))
    from plan_compiler import selected_proposal_change_contract_errors

    if not missing_files:
        errors.extend(selected_proposal_change_contract_errors(
            data,
            change_symbol=str(proposal.get("change_symbol") or ""),
            reachable_chain=proposal.get("reachable_chain") or [],
            target_files=proposal.get("target_files") or [],
        ))
    return errors


def _selected_proposal_contract(proposal: dict) -> dict:
    falsifier = dict(proposal["falsifier"])
    state_learning_primary = _proposal_falsifier_primary(
        falsifier.get("test_name")
    )
    if (
        state_learning_primary is None
        or falsifier.get("state_learning_primary") != state_learning_primary
        or falsifier.get("intervention_target")
        != STATE_LEARNING_PRIMARY_INTERVENTION_TARGETS[state_learning_primary]
        or proposal.get("mechanism_target") != falsifier.get("intervention_target")
    ):
        raise ValueError("selected proposal falsifier has no typed primary")
    contract = {
        "schema_version": 1,
        "proposal_id": str(proposal["proposal_id"]),
        "targeted_failure": str(proposal["targeted_failure"]),
        "structural_change": str(proposal["structural_change"]),
        "counterfactual": str(proposal["counterfactual"]),
        "measurement": str(proposal["measurement"]),
        "expected_diff": str(proposal["expected_diff"]),
        "target_files": list(proposal["target_files"]),
        "source_symbols": list(proposal["source_symbols"]),
        "change_symbol": str(proposal["change_symbol"]),
        "reachable_chain": list(proposal["reachable_chain"]),
        "falsifier": falsifier,
        "state_learning_primary": state_learning_primary,
        "mechanism_target": proposal["mechanism_target"],
        "intervention_target": falsifier["intervention_target"],
        "required_primary_checks": list(
            STATE_LEARNING_PRIMARY_CHECKS[state_learning_primary]
        ),
        "evidence_refs": list(proposal["evidence_refs"]),
        "snapshot_evidence": list(proposal.get("snapshot_evidence") or []),
        "execution_mode": str(
            proposal.get("execution_mode") or "strategy_implementation"
        ),
        "why_not_threshold_tuning": str(proposal["why_not_threshold_tuning"]),
        "risks": str(proposal["risks"]),
    }
    contract["contract_digest"] = hashlib.sha256(
        json.dumps(
            contract,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return contract


def _selected_proposal_binding(proposal: dict, packet: dict) -> dict:
    """Project the one canonical packet-to-plan binding used by every mode."""

    contract = _selected_proposal_contract(proposal)
    return {
        "schema_version": _PROPOSAL_PACKET_SCHEMA_VERSION,
        "selected_proposal_id": proposal["proposal_id"],
        "contract_digest": contract["contract_digest"],
        "context_digest": packet["context_digest"],
        "source_code_digest": packet["source_code_digest"],
        "target_files": list(contract["target_files"]),
        "source_symbols": list(contract["source_symbols"]),
        "change_symbol": contract["change_symbol"],
        "reachable_chain": list(contract["reachable_chain"]),
        "falsifier": dict(contract["falsifier"]),
        "mechanism_target": contract["mechanism_target"],
        "state_learning_primary": contract["state_learning_primary"],
        "intervention_target": contract["intervention_target"],
        "required_primary_checks": list(contract["required_primary_checks"]),
        "evidence_refs": list(contract["evidence_refs"]),
        "snapshot_evidence": list(contract["snapshot_evidence"]),
        "execution_mode": contract["execution_mode"],
        "targeted_failure": contract["targeted_failure"],
        "structural_change": contract["structural_change"],
        "counterfactual": contract["counterfactual"],
        "measurement": contract["measurement"],
        "expected_diff": contract["expected_diff"],
        "why_not_threshold_tuning": contract["why_not_threshold_tuning"],
        "risks": contract["risks"],
        "selected_proposal": {
            key: value for key, value in proposal.items() if key != "direction"
        },
        "proposal_packet_digest": hashlib.sha256(
            json.dumps(
                packet,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
    }


def _selected_proposal_worker_block(proposal: dict) -> str:
    from plan_compiler import SELECTED_PROPOSAL_BEGIN, SELECTED_PROPOSAL_END

    contract = _selected_proposal_contract(proposal)
    execution_instruction = (
        "The checked-in fixed blueprint owns the v143 output bytes. Treat this "
        "proposal only as a capability-audit lens: do not claim that its prose "
        "caused the implementation or proves poker strength. The system quality "
        "gate must verify the named typed falsifier against the fixed blueprint."
        if contract["execution_mode"] == "fixed_blueprint_capability_audit"
        else
        "Modify the exact change_symbol through the named reachable chain. Do not "
        "substitute an unmeasured threshold-only edit, a second mechanism, or "
        "telemetry-only code. Preserve counterfactual and measurement as the "
        "generation hypothesis, and expose the named falsifier through the task "
        "RuntimeContract/checks_required so the system typed probe can execute it."
    )
    return "\n".join((
        SELECTED_PROPOSAL_BEGIN,
        "# SYSTEM-BOUND SELECTED PROPOSAL CONTRACT",
        f"proposal_id={contract['proposal_id']}",
        f"contract_digest={contract['contract_digest']}",
        f"execution_mode={contract['execution_mode']}",
        f"targeted_failure={contract['targeted_failure']}",
        f"structural_change={contract['structural_change']}",
        f"counterfactual={contract['counterfactual']}",
        f"measurement={contract['measurement']}",
        f"expected_diff={contract['expected_diff']}",
        "source_symbols=" + json.dumps(
            contract["source_symbols"], ensure_ascii=False, separators=(",", ":")
        ),
        f"change_symbol={contract['change_symbol']}",
        "reachable_chain=" + json.dumps(
            contract["reachable_chain"], ensure_ascii=False, separators=(",", ":")
        ),
        f"state_learning_primary={contract['state_learning_primary']}",
        f"mechanism_target={contract['mechanism_target']}",
        f"intervention_target={contract['intervention_target']}",
        "required_primary_checks=" + json.dumps(
            contract["required_primary_checks"],
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        "falsifier=" + json.dumps(
            contract["falsifier"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
        "evidence_refs=" + json.dumps(
            contract["evidence_refs"], ensure_ascii=False, separators=(",", ":")
        ),
        "snapshot_evidence=" + json.dumps(
            contract["snapshot_evidence"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "not_threshold_tuning=" + contract["why_not_threshold_tuning"],
        "risks=" + contract["risks"],
        execution_instruction,
        SELECTED_PROPOSAL_END,
    ))


def _selected_proposal_compilation_contract(proposal: dict) -> dict:
    """Return the exact provider budget and typed-primary binding for a proposal."""

    from plan_compiler import SYSTEM_OWNED_CONTRACT_MAX_CHARS

    contract = _selected_proposal_contract(proposal)
    binding_chars = len(_selected_proposal_worker_block(proposal))
    separator_chars = 2
    return {
        "proposal_id": contract["proposal_id"],
        "falsifier_test_name": contract["falsifier"]["test_name"],
        "mechanism_target": contract["mechanism_target"],
        "change_symbol": contract["change_symbol"],
        "state_learning_primary": contract["state_learning_primary"],
        "intervention_target": contract["intervention_target"],
        "required_primary_checks": list(contract["required_primary_checks"]),
        "reserved_selected_contract_chars": binding_chars,
        "separator_chars": separator_chars,
        "reserved_runtime_contract_max_chars": (
            SYSTEM_OWNED_CONTRACT_MAX_CHARS
        ),
        "global_cap_chars": WORKER_PROMPT_MAX_CHARS,
        "max_provider_chars": (
            WORKER_PROMPT_MAX_CHARS
            - binding_chars
            - separator_chars
            - SYSTEM_OWNED_CONTRACT_MAX_CHARS
        ),
        "character_metric": "python_unicode_code_points",
    }


def _proposal_worker_bindability_error(proposal: dict) -> str | None:
    compilation = _selected_proposal_compilation_contract(proposal)
    if int(compilation["max_provider_chars"]) >= WORKER_PROMPT_MIN_CHARS:
        return None
    return _proposal_binding_error(
        "proposal_worker_binding_cannot_fit_minimum_prompt",
        {
            **compilation,
            "minimum_provider_chars": WORKER_PROMPT_MIN_CHARS,
        },
    )


def _proposal_compilation_contract_text(packet: dict) -> str:
    """Render all allowed proposal budgets before the final Master chooses one."""

    allowed = set(map(str, packet.get("allowed_proposal_ids") or []))
    rows = [
        _selected_proposal_compilation_contract(proposal)
        for proposal in packet.get("ordered_proposals") or []
        if str(proposal.get("proposal_id") or "") in allowed
    ]
    return json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _master_final_emission_guard(packet: dict) -> str:
    """Render the final, system-owned selected-plan emission limits.

    The final Master chooses an immutable proposal by id, then supplies only
    task-specific implementation reasoning.  Proposal metadata and the full
    selected contract are system-bound later, so repeating either in a Worker
    prompt wastes the very limited provider-owned prompt budget.
    """

    allowed = set(map(str, packet.get("allowed_proposal_ids") or []))
    rows = []
    for proposal in packet.get("ordered_proposals") or []:
        if not isinstance(proposal, dict):
            continue
        proposal_id = str(proposal.get("proposal_id") or "")
        if proposal_id not in allowed:
            continue
        compilation = _selected_proposal_compilation_contract(proposal)
        hard_cap = int(compilation["max_provider_chars"])
        rows.append({
            "proposal_id": proposal_id,
            "worker_prompt_hard_cap_chars": hard_cap,
            "worker_prompt_advisory_target_chars": max(
                WORKER_PROMPT_MIN_CHARS,
                hard_cap - 128,
            ),
        })
    if not rows:
        raise ValueError("Master final emission guard has no allowed proposal")
    return (
        "# SYSTEM-OWNED FINAL EMISSION GATE (highest priority)\n"
        "selected_proposal_id is your only proposal-selection field. The system "
        "binds targeted_failure and measurement_plan from that selected immutable "
        "proposal; do not paraphrase, expand, or use either duplicate field to "
        "change scope. For every task that writes a selected target file, keep "
        "worker_prompt near the listed advisory target (Unicode code points) and "
        "never exceed its hard cap. That selected row is the sole model-owned "
        "length authority: do not rely on template-wide length advice, compiler "
        "externalization, truncation, or a task brief to make it fit. Describe only "
        "task-specific implementation and checks; when the cap is small, use compact "
        "directives rather than reproducing code, the proposal, or the runtime "
        "contract. The system appends those immutable blocks after validation.\n"
        "EMISSION_CAPS="
        + json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\nReturn the single JSON object now; do not emit analysis outside it."
    )


def _bind_selected_proposal_workers(data: dict, proposal: dict) -> dict:
    """Compile the selected mechanism into every writable target task."""
    result = json.loads(json.dumps(data, ensure_ascii=False))
    block = _selected_proposal_worker_block(proposal)
    target_files = set(proposal["target_files"])
    for task in result.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        task_files, scope_errors = _task_proposal_scope_paths(task)
        if scope_errors:
            continue
        if task_files.intersection(target_files):
            provider_prompt = task.get("worker_prompt")
            if (
                not isinstance(provider_prompt, str)
                or len(provider_prompt.strip()) < WORKER_PROMPT_MIN_CHARS
                or _provider_prompt_reserved_markers(provider_prompt)
            ):
                continue
            task["worker_prompt"] = (
                _canonical_provider_worker_prompt(provider_prompt)
                + "\n\n"
                + block
            )
    return result


def _project_strict_final_master_result(
    output: str,
    *,
    proposal_packet: dict | None,
    architecture_policy: dict | None,
) -> tuple[dict | None, list[str]]:
    """Deterministically project provider text to the accepted strict plan.

    This is intentionally the same post-provider transformation used by the
    first strict Master path: proposal selection, system-owned policy ABI terms,
    canonical Master schema normalization, and the frozen proposal bindings.
    Strict authority calls this function before completing the provider effect,
    so an unrelated caller-supplied plan can never be accepted as LLM output.
    """

    if not isinstance(proposal_packet, dict):
        return None, ["proposal_packet_missing"]
    packet, packet_errors = _parse_valid_proposal_packet(json.dumps(
        proposal_packet,
        ensure_ascii=False,
        sort_keys=True,
    ))
    if packet_errors or packet is None:
        return None, ["proposal_packet_invalid:" + item for item in packet_errors]
    if not isinstance(architecture_policy, dict) or not architecture_policy:
        return None, ["architecture_policy_missing"]

    from llm_query import parse_json_output_with_mode

    data, failure_mode = parse_json_output_with_mode(output or "")
    if not isinstance(data, dict) or "tasks" not in data:
        return None, [f"master_output_invalid:{failure_mode}"]
    data, selected_proposal, selection_errors, _metadata_rebound = (
        _canonicalize_selected_proposal_metadata(data, packet)
    )
    if selection_errors or selected_proposal is None:
        return None, selection_errors
    binding_errors = _validate_final_proposal_binding(data, packet)
    if binding_errors:
        return None, binding_errors
    selected_proposal_id = data.pop("selected_proposal_id")
    data = _bind_selected_proposal_workers(data, selected_proposal)

    from plan_compiler import (
        bind_system_owned_policy_abi,
        bind_system_owned_worker_contract_terms,
    )

    data, _policy_abi = bind_system_owned_policy_abi(
        data,
        policy=architecture_policy,
    )
    data, _terms = bind_system_owned_worker_contract_terms(data)
    if _terms.get("overflow_tasks"):
        return None, [_proposal_binding_error(
            "system_owned_worker_contract_binding_overflow",
            {"tasks": _terms["overflow_tasks"]},
        )]
    if any(data.get(field) for field in (
        "branch_from",
        "source_override",
        "source_v_override",
    )):
        return None, ["master_source_override_forbidden"]

    from output_schema import validate_agent_output

    data, schema_errors = validate_agent_output("master", data)
    if schema_errors:
        return None, ["master_schema:" + item for item in schema_errors]

    data["selected_proposal_id"] = selected_proposal_id
    data["proposal_binding"] = _selected_proposal_binding(
        selected_proposal,
        packet,
    )
    data["proposal_ensemble"] = packet
    return data, []


def _record_master_invocation_evidence(
    result: dict,
    *,
    output: str,
    role_result: dict,
) -> dict:
    """Record a new invocation or reuse the exact accepted replay evidence."""

    from system_strict_bootstrap import (
        llm_result_digest,
        record_llm_invocation_evidence,
    )

    strict_call = result.get("strict_call")
    journal_bound = bool(
        isinstance(strict_call, dict)
        and strict_call.get("effect_id")
        and strict_call.get("accepted_receipt")
    )
    if journal_bound:
        from strict_authority_workflow import record_bound_invocation_evidence

        return record_bound_invocation_evidence(
            strict_call,
            log_file=result["log_file"],
        )
    evidence = record_llm_invocation_evidence(
        invocation_id=result["invocation_id"],
        purpose=result["purpose"],
        role=result["role"],
        prompt_digest=hashlib.sha256(
            result["prompt"].encode("utf-8")
        ).hexdigest(),
        raw_output_digest=hashlib.sha256(output.encode("utf-8")).hexdigest(),
        result_digest=llm_result_digest(result["cost_usd"], result["usage"]),
        role_result=role_result,
        log_file=result["log_file"],
    )
    return evidence


