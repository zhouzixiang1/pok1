"""Master Architect agent: plans worker tasks from frozen generation evidence."""

import ast
import hashlib
import json
import re
import time
from pathlib import Path

from bot_namespace import bot_name, bot_relpath
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
            "Copy one complete two-symbol PREFERRED CURRENT ENTRY ANCHOR; put "
            "both symbols in source_symbols with matching source: evidence_refs."
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
            f"measurement MUST use: target=national_v{source_v}; "
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
        "source-relative file.py:symbol references), reachable_chain (2-8 of those "
        "symbols in direct caller-to-callee order), falsifier {test_name, "
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
        "A two-symbol entry anchor is sufficient to prove current reachability, "
        "but it is not the proposed poker mechanism. Copy one SYSTEM-VERIFIED "
        "PREFERRED CURRENT ENTRY ANCHOR exactly when available, then use "
        "structural_change, counterfactual, measurement, and expected_diff to state "
        "a decision-relevant strategy effect. Proposed future calls belong only in "
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
        "it is compatible."
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
        "For reachable_chain, prefer one exact two-symbol PREFERRED CURRENT ENTRY "
        "ANCHOR from the system index. It proves a live path but is not by itself a "
        "strategy change. Never use a future edge that your proposal would create. "
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


_PROPOSAL_SCHEMA_VERSION = "master-proposal-v3"
_PROPOSAL_PACKET_SCHEMA_VERSION = "master-proposal-packet-v5"
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
        raise ValueError("proposal falsifier mapping has no permitted rows")
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

    ambiguous_shared_leaves: list[str] = []
    unowned_fields = []
    for value in executable_fields.values():
        if not isinstance(value, str):
            continue
        masked_value = mask_literals(
            value.lower(),
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
        alias_pattern = r"[^a-z0-9]*".join(map(re.escape, parts))
        if re.search(
            r"(?<![a-z0-9])"
            + alias_pattern
            + r"(?![a-z0-9])",
            foreign_scan_text,
        ):
            return True
        # Long closed aliases must also fail closed when identifier characters
        # are appended (``terminalresponsebackup``). Keep a leading boundary,
        # and keep short lexical terms such as ``donk`` boundary-only, so words
        # such as ``interactionprofile`` and ``donkey`` remain legal.
        compact_alias = "".join(parts)
        if len(compact_alias) < 8:
            return False
        return re.search(
            r"(?<![a-z0-9])" + alias_pattern,
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
        # The provider prompt publishes a canonical machine value, not a
        # case-insensitive prose vocabulary. Preserve bytes after surrounding
        # whitespace so keys and enum-like values must match that contract
        # exactly; otherwise an output can look compliant while storing a
        # different non-canonical measurement identity.
        key = key.strip()
        item = item.strip()
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
        if not re.fullmatch(r"national_v[1-9][0-9]*", parsed["target"]):
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
            r"national_v[1-9][0-9]*",
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
        "SYSTEM-VERIFIED PREFERRED CURRENT ENTRY ANCHORS (copy one JSON array "
        "exactly; two symbols prove reachability, not the proposed mechanism):"
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
        "Implement this one mechanism through the named reachable chain. Do not "
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


async def _run_master_proposal_ensemble(
    planning_context: str,
    *,
    source_v: int,
    next_v: int,
    ui,
    log_dir: Path,
    allowed_evidence_snapshot_dir: str,
    baseline_v: int | None = None,
    protocol_bootstrap_prepared_only: bool = False,
    singleton_no_strength: bool = False,
    strict_checkpoint: dict | None = None,
    allowed_primaries: tuple[str, ...] | None = None,
) -> str:
    """Three proposals, two anonymous criterion critics, deterministic veto/order.

    The ballots cannot alter lineage, evidence cutoffs, executable literals,
    or gates.  Their only blocking authority is a two-ballot rejection veto;
    the final plan still passes the canonical schema/compiler/validator path.
    """
    import asyncio

    if protocol_bootstrap_prepared_only and singleton_no_strength:
        raise ValueError("Master proposal planning mode is ambiguous")
    allowed_primaries = _canonical_proposal_primaries(allowed_primaries)

    context_digest = hashlib.sha256(planning_context.encode("utf-8")).hexdigest()
    try:
        baseline_dir = get_bot_dir(
            int(baseline_v) if baseline_v is not None else int(source_v)
        )
        source_graph, source_code_digest = _source_symbol_graph(baseline_dir)
    except Exception as exc:
        return _proposal_packet_error(
            f"source_symbol_index_failed:{type(exc).__name__}:{str(exc)[:240]}",
            context_digest=context_digest,
        )
    if not source_graph:
        return _proposal_packet_error(
            "source_symbol_index_empty",
            context_digest=context_digest,
        )
    no_strength_snapshot = bool(
        protocol_bootstrap_prepared_only or singleton_no_strength
    )
    snapshot_dir = (
        None if no_strength_snapshot else Path(allowed_evidence_snapshot_dir)
    )
    require_snapshot_evidence = not no_strength_snapshot
    proposal_execution_mode = (
        "fixed_blueprint_capability_audit"
        if protocol_bootstrap_prepared_only
        else "strategy_implementation"
    )
    evidence_mode = (
        "fresh_strict_control_no_strength"
        if protocol_bootstrap_prepared_only
        else "singleton_parent_no_strength"
        if singleton_no_strength
        else "frozen_strength_snapshot"
    )
    source_symbol_index = _source_symbol_prompt_index(source_graph)
    if require_snapshot_evidence:
        snapshot_reference_index = _snapshot_reference_prompt_index(snapshot_dir)
        if not snapshot_reference_index:
            return _proposal_packet_error(
                "snapshot_reference_index_empty",
                context_digest=context_digest,
                source_code_digest=source_code_digest,
            )
        source_symbol_index += "\n\n" + snapshot_reference_index
    # Every no-strength protocol-bootstrap generation has a checkpoint-owned
    # identity and must retain successful Scout/Ballot work across transport
    # retries and process restarts. Restricting the durable authority journal
    # to the one-time fresh v143 bootstrap caused singleton successors to lose
    # two valid Scout results whenever the third provider stalled.
    strict_authority_enabled = (
        (protocol_bootstrap_prepared_only or singleton_no_strength)
        and isinstance(strict_checkpoint, dict)
    )

    def raise_provider_failure(
        phase: str,
        role_name: str,
        error: BaseException,
        *,
        slot: str,
    ) -> None:
        issue = (
            f"{phase}:{role_name}:"
            f"{type(error).__name__}:{str(error)[:300]}"
        )
        if strict_authority_enabled:
            from strict_authority_workflow import master_provider_retry_state

            retry_state = master_provider_retry_state(
                strict_checkpoint,
                failed_slot=slot,
            )
            # Only a fenced provider effect may enter the attempt-neutral park
            # path. Renderer/log/local control errors that occur before an
            # exact dispatch have no durable provider-failure proof and must
            # retain the ordinary bounded infrastructure classification.
            if not retry_state.get("failed_effect_ids"):
                raise MasterInfrastructureError(
                    source_v,
                    next_v,
                    context_digest,
                    issue,
                ) from error
            raise MasterEnsembleInfrastructureParked(
                source_v,
                next_v,
                context_digest,
                issue,
                slot=slot,
                retry_state=retry_state,
            ) from error
        raise MasterInfrastructureError(
            source_v,
            next_v,
            context_digest,
            issue,
        ) from error
    proposal_read_dirs = (
        [get_bot_dir(int(next_v))]
        if protocol_bootstrap_prepared_only
        else [get_bot_dir(int(source_v)), get_bot_dir(int(next_v))]
    )

    async def propose(
        direction: str,
        directive: str,
        *,
        repair: dict | None = None,
    ):
        strict_call = None
        if strict_authority_enabled:
            from strict_authority_workflow import new_call, proposal_call_context

            strict_call = new_call(
                strict_checkpoint,
                slot=f"proposal:{direction}",
                context_binding=proposal_call_context(
                    context_digest=context_digest,
                    source_code_digest=source_code_digest,
                    direction=direction,
                    allowed_primaries=allowed_primaries,
                    # Preserve byte-for-byte compatibility with published
                    # fresh-v143 receipts. Only the newly admitted singleton
                    # successor projection needs an explicit mode marker.
                    evidence_mode=(
                        "singleton_parent_no_strength"
                        if singleton_no_strength
                        else None
                    ),
                ),
            )
            invocation_id = strict_call["invocation_id"]
            if repair is None and strict_call.get("replay_provider"):
                replay_role = str(strict_call.get("actual_role") or "")
                base_role = f"MASTER PROPOSAL {direction}"
                if replay_role == base_role + " DISTINCTNESS RETRY":
                    repair = {"kind": "distinctness"}
                elif replay_role == base_role + " SCHEMA RETRY":
                    repair = {"kind": "schema"}
                elif replay_role != base_role:
                    raise RuntimeError(
                        "strict_proposal_replay_role_invalid:"
                        f"{direction}:{replay_role}"
                    )
            if repair is None and strict_call.get("schema_retry_required"):
                prior_rejection = strict_call.get("prior_schema_rejection") or {}
                repair = {
                    "kind": (
                        "distinctness"
                        if prior_rejection.get("rejection_kind")
                        == "proposal_identity_collision"
                        else "schema"
                    ),
                    "projection_hints": list(
                        prior_rejection.get("projection_errors") or ()
                    ),
                }
        else:
            from system_strict_bootstrap import new_llm_invocation_id

            invocation_id = new_llm_invocation_id()
        repair_kind = str((repair or {}).get("kind") or "")
        projection_hints = list((repair or {}).get("projection_hints") or ())
        is_repair = bool(repair_kind)
        is_distinctness_repair = repair_kind == "distinctness"
        purpose = f"master_proposal_scout:{direction}"
        log_basename = (
            f"master_proposal_{direction}_{'distinctness' if is_distinctness_repair else 'schema'}_retry_io.txt"
            if is_repair
            else f"master_proposal_{direction}_io.txt"
        )
        log_file = log_dir / log_basename
        if strict_call is not None:
            from strict_authority_workflow import strict_invocation_log_path

            log_file = strict_invocation_log_path(
                strict_call,
                logs_dir=log_dir,
                basename=log_basename,
            )
        retry_label = (
            " DISTINCTNESS RETRY"
            if is_distinctness_repair
            else " SCHEMA RETRY" if is_repair else ""
        )
        proposal_role = f"MASTER PROPOSAL {direction}{retry_label}"
        from llm_query import render_llm_prompt

        rendered_prompt = render_llm_prompt(
            proposal_role,
            producer=_render_master_proposal_provider_prompt,
            renderer_inputs={
                "planning_context": planning_context,
                "direction": str(direction),
                "directive": str(directive),
                "source_v": int(source_v),
                "next_v": int(next_v),
                "protocol_bootstrap_prepared_only": bool(
                    protocol_bootstrap_prepared_only
                ),
                "singleton_no_strength": bool(singleton_no_strength),
                "source_symbol_index": source_symbol_index,
                "repair_kind": repair_kind,
                "projection_hints": projection_hints,
                "allowed_primaries": list(allowed_primaries or ()),
                "invocation_id": str(invocation_id),
            },
        )
        output, cost_usd, usage = await run_claude_query(
            rendered_prompt,
            [],
            ui,
            proposal_role,
            log_file,
            tools=["Read"],
            allowed_evidence_snapshot_dir=allowed_evidence_snapshot_dir,
            allowed_read_dirs=proposal_read_dirs,
            strict_authority=strict_call,
        )
        return {
            "output": output,
            "cost_usd": cost_usd,
            "usage": usage,
            "invocation_id": invocation_id,
            "purpose": purpose,
            "role": (
                f"MASTER PROPOSAL {direction}"
                f"{retry_label}"
            ),
            "prompt": str(rendered_prompt),
            "log_file": str(log_file),
            "strict_call": strict_call,
        }

    proposal_results = await gather_llm_fail_fast(
        *(propose(direction, directive) for direction, directive in _MASTER_PROPOSAL_DIRECTIONS),
    )
    proposals = []
    proposal_invocations: dict[str, dict] = {}
    seen_proposal_ids: set[str] = set()
    proposal_provider_errors: list[tuple[str, BaseException]] = []
    invalid_proposal_specs: list[tuple[str, str, dict]] = []
    accepted_proposal_directions: dict[str, str] = {}

    def proposal_actual_role(result: object) -> str | None:
        if not isinstance(result, dict):
            return None
        strict_call = result.get("strict_call")
        if isinstance(strict_call, dict):
            # The journal-bound dispatched role is authority.  A missing value
            # may not fall back to a caller-supplied result label.
            return str(strict_call.get("actual_role") or "") or None
        return str(result.get("role") or "") or None

    for (direction, _directive), result in zip(_MASTER_PROPOSAL_DIRECTIONS, proposal_results):
        if isinstance(result, BaseException):
            from strict_authority_workflow import StrictAuthorityError

            if isinstance(result, StrictAuthorityError):
                raise result
            proposal_provider_errors.append((direction, result))
            continue
        output = result.get("output", "") if isinstance(result, dict) else ""
        proposal = _validated_master_proposal(
            output,
            direction,
            source_graph=source_graph,
            snapshot_dir=snapshot_dir,
            national_policy_only=True,
            require_snapshot_evidence=require_snapshot_evidence,
            execution_mode=proposal_execution_mode,
            evidence_mode=evidence_mode,
            expected_measurement_target=(
                bot_name(int(source_v)) if singleton_no_strength else None
            ),
            forbidden_measurement_target=(
                bot_name(int(next_v)) if require_snapshot_evidence else None
            ),
            allowed_primaries=allowed_primaries,
            actual_role=proposal_actual_role(result),
        )
        if proposal is None:
            repair = {"kind": "schema"}
            repair["projection_hints"] = (
                _master_proposal_projection_hints(
                    output,
                    source_graph=source_graph,
                    snapshot_dir=snapshot_dir,
                    national_policy_only=True,
                    require_snapshot_evidence=require_snapshot_evidence,
                    evidence_mode=evidence_mode,
                    allowed_primaries=allowed_primaries,
                )
                or ["proposal_contract_invalid"]
            )
            invalid_proposal_specs.append(
                (direction, _directive, repair)
            )
            continue
        proposal_id = proposal["proposal_id"]
        if proposal_id in seen_proposal_ids:
            if strict_authority_enabled:
                from strict_authority_workflow import reject_duplicate_proposal

                reject_duplicate_proposal(result["strict_call"])
            invalid_proposal_specs.append((
                direction,
                _directive,
                {
                    "kind": "distinctness",
                    "proposal_id": proposal_id,
                    "conflicting_direction": accepted_proposal_directions[
                        proposal_id
                    ],
                },
            ))
            continue
        if strict_authority_enabled:
            from strict_authority_workflow import accept_role_result

            accept_role_result(
                result["strict_call"],
                role_result=proposal,
                parse_contract=_PROPOSAL_SCHEMA_VERSION,
            )

        proposal_invocations[proposal_id] = _record_master_invocation_evidence(
            result,
            output=output,
            role_result=proposal,
        )
        seen_proposal_ids.add(proposal_id)
        accepted_proposal_directions[proposal_id] = direction
        proposals.append(proposal)
    if proposal_provider_errors:
        direction, error = proposal_provider_errors[0]
        raise_provider_failure(
            "proposal_scout",
            direction,
            error,
            slot=f"proposal:{direction}",
        )
    if invalid_proposal_specs:
        retry_results = await gather_llm_fail_fast(
            *(
                propose(direction, directive, repair=repair)
                for direction, directive, repair in invalid_proposal_specs
            ),
        )
        retry_provider_errors: list[tuple[str, BaseException]] = []
        for (direction, _directive, _repair), result in zip(
            invalid_proposal_specs, retry_results
        ):
            if isinstance(result, LLMAvailabilityBlocked):
                raise result
            if isinstance(result, BaseException):
                from strict_authority_workflow import StrictAuthorityError

                if isinstance(result, StrictAuthorityError):
                    raise result
                retry_provider_errors.append((direction, result))
                continue
            output = result.get("output", "") if isinstance(result, dict) else ""
            proposal = _validated_master_proposal(
                output,
                direction,
                source_graph=source_graph,
                snapshot_dir=snapshot_dir,
                national_policy_only=True,
                require_snapshot_evidence=require_snapshot_evidence,
                execution_mode=proposal_execution_mode,
                evidence_mode=evidence_mode,
                expected_measurement_target=(
                    bot_name(int(source_v)) if singleton_no_strength else None
                ),
                forbidden_measurement_target=(
                    bot_name(int(next_v)) if require_snapshot_evidence else None
                ),
                allowed_primaries=allowed_primaries,
                actual_role=proposal_actual_role(result),
            )
            if proposal is None:
                continue
            proposal_id = proposal["proposal_id"]
            if proposal_id in seen_proposal_ids:
                if strict_authority_enabled:
                    from strict_authority_workflow import reject_duplicate_proposal

                    reject_duplicate_proposal(result["strict_call"])
                continue
            if strict_authority_enabled:
                from strict_authority_workflow import accept_role_result

                accept_role_result(
                    result["strict_call"],
                    role_result=proposal,
                    parse_contract=_PROPOSAL_SCHEMA_VERSION,
                )

            proposal_invocations[proposal_id] = _record_master_invocation_evidence(
                result,
                output=output,
                role_result=proposal,
            )
            seen_proposal_ids.add(proposal_id)
            accepted_proposal_directions[proposal_id] = direction
            proposals.append(proposal)
        if retry_provider_errors:
            direction, error = retry_provider_errors[0]
            raise_provider_failure(
                "proposal_scout_repair",
                direction,
                error,
                slot=f"proposal:{direction}",
            )
    if len(proposals) != len(_MASTER_PROPOSAL_DIRECTIONS):
        return _proposal_packet_error(
            "three_distinct_schema_valid_scout_proposals_required:"
            f"got_{len(proposals)}",
            context_digest=context_digest,
            source_code_digest=source_code_digest,
        )
    try:
        proposal_source_symbol_digests = _proposal_source_symbol_digests(
            proposals,
            baseline_dir,
        )
    except Exception as exc:
        return _proposal_packet_error(
            "proposal_source_symbol_digest_failed:"
            f"{type(exc).__name__}:{str(exc)[:240]}",
            context_digest=context_digest,
            source_code_digest=source_code_digest,
        )

    async def critique(name: str, lens: str, *, schema_retry: bool = False):
        strict_call = None
        if strict_authority_enabled:
            from strict_authority_workflow import ballot_call_context, new_call

            strict_call = new_call(
                strict_checkpoint,
                slot=f"ballot:{name}",
                context_binding=ballot_call_context(
                    context_digest=context_digest,
                    source_code_digest=source_code_digest,
                    critic_id=name,
                    proposal_ids=(item["proposal_id"] for item in proposals),
                    critic_criteria=_PROPOSAL_CRITIC_CRITERIA,
                ),
            )
            invocation_id = strict_call["invocation_id"]
            if strict_call.get("replay_provider"):
                replay_role = str(strict_call.get("actual_role") or "")
                base_role = f"MASTER PROPOSAL CRITIC {name}"
                if replay_role == base_role + " SCHEMA RETRY":
                    schema_retry = True
                elif replay_role != base_role:
                    raise RuntimeError(
                        "strict_ballot_replay_role_invalid:"
                        f"{name}:{replay_role}"
                    )
            elif strict_call.get("schema_retry_required"):
                schema_retry = True
        else:
            from system_strict_bootstrap import new_llm_invocation_id

            invocation_id = new_llm_invocation_id()
        # No scout lens/identity is exposed.  Each critic receives a different
        # but replayable ordering derived from the immutable planning digest.
        critic_proposals = [
            {key: value for key, value in proposal.items() if key != "direction"}
            for proposal in proposals
        ]
        critic_proposals.sort(
            key=lambda item: hashlib.sha256(
                f"{context_digest}:{name}:{item['proposal_id']}".encode("utf-8")
            ).hexdigest()
        )
        purpose = f"master_proposal_critic:{name}"
        log_basename = (
            f"master_proposal_critic_{name}_schema_retry_io.txt"
            if schema_retry
            else f"master_proposal_critic_{name}_io.txt"
        )
        log_file = log_dir / log_basename
        if strict_call is not None:
            from strict_authority_workflow import strict_invocation_log_path

            log_file = strict_invocation_log_path(
                strict_call,
                logs_dir=log_dir,
                basename=log_basename,
            )
        critic_role = (
            f"MASTER PROPOSAL CRITIC {name}"
            f"{' SCHEMA RETRY' if schema_retry else ''}"
        )
        from llm_query import render_llm_prompt

        rendered_prompt = render_llm_prompt(
            critic_role,
            producer=_render_master_proposal_critic_provider_prompt,
            renderer_inputs={
                "proposal_name": str(name),
                "lens": str(lens),
                "planning_context_digest": context_digest,
                "proposals": critic_proposals,
                "criteria": _PROPOSAL_CRITIC_CRITERIA,
                "evidence_mode": evidence_mode,
                "schema_retry": bool(schema_retry),
                "invocation_id": str(invocation_id),
            },
        )
        output, cost_usd, usage = await run_claude_query(
            rendered_prompt,
            [],
            ui,
            critic_role,
            log_file,
            tools=[],
            strict_authority=strict_call,
        )
        return {
            "output": output,
            "cost_usd": cost_usd,
            "usage": usage,
            "invocation_id": invocation_id,
            "purpose": purpose,
            "role": (
                f"MASTER PROPOSAL CRITIC {name}"
                f"{' SCHEMA RETRY' if schema_retry else ''}"
            ),
            "prompt": str(rendered_prompt),
            "log_file": str(log_file),
            "critic_id": name,
            "strict_call": strict_call,
        }

    critic_results = await gather_llm_fail_fast(
        critique("falsification", "Counterfactual quality, causal attribution, and evidence support."),
        critique("scope", "Reachability, bounded implementation scope, and regression risk."),
    )
    proposal_ids = {item["proposal_id"] for item in proposals}
    critiques = []
    invalid_critics = []
    critic_provider_errors: list[tuple[str, BaseException]] = []
    critic_specs = (
        ("falsification", "Counterfactual quality, causal attribution, and evidence support."),
        ("scope", "Reachability, bounded implementation scope, and regression risk."),
    )
    for spec, result in zip(critic_specs, critic_results):
        if isinstance(result, BaseException):
            from strict_authority_workflow import StrictAuthorityError

            if isinstance(result, StrictAuthorityError):
                raise result
            critic_provider_errors.append((spec[0], result))
            continue
        output = result.get("output", "") if isinstance(result, dict) else ""
        critique_row = _validated_proposal_critique(output, proposal_ids)
        if critique_row is not None:
            critique_row["critic_id"] = result["critic_id"]
            if strict_authority_enabled:
                from strict_authority_workflow import accept_role_result

                accept_role_result(
                    result["strict_call"],
                    role_result={
                        key: value
                        for key, value in critique_row.items()
                        if key not in {"critic_id", "invocation_evidence"}
                    },
                    parse_contract="master-proposal-ballot-v1",
                )
            critique_row["invocation_evidence"] = (
                _record_master_invocation_evidence(
                    result,
                    output=output,
                    role_result={
                        key: value
                        for key, value in critique_row.items()
                        if key not in {"critic_id", "invocation_evidence"}
                    },
                )
            )
            critiques.append(critique_row)
        else:
            invalid_critics.append(spec)

    if critic_provider_errors:
        critic_id, error = critic_provider_errors[0]
        raise_provider_failure(
            "proposal_critic",
            critic_id,
            error,
            slot=f"ballot:{critic_id}",
        )
    if invalid_critics:
        retry_results = await gather_llm_fail_fast(
            *(
                critique(name, lens, schema_retry=True)
                for name, lens in invalid_critics
            ),
        )
        retry_critic_provider_errors: list[tuple[str, BaseException]] = []
        for (critic_id, _lens), result in zip(invalid_critics, retry_results):
            if isinstance(result, LLMAvailabilityBlocked):
                raise result
            if isinstance(result, BaseException):
                from strict_authority_workflow import StrictAuthorityError

                if isinstance(result, StrictAuthorityError):
                    raise result
                retry_critic_provider_errors.append((critic_id, result))
                continue
            output = result.get("output", "") if isinstance(result, dict) else ""
            critique_row = _validated_proposal_critique(output, proposal_ids)
            if critique_row is not None:
                critique_row["critic_id"] = result["critic_id"]
                if strict_authority_enabled:
                    from strict_authority_workflow import accept_role_result

                    accept_role_result(
                        result["strict_call"],
                        role_result={
                            key: value
                            for key, value in critique_row.items()
                            if key not in {"critic_id", "invocation_evidence"}
                        },
                        parse_contract="master-proposal-ballot-v1",
                    )
                critique_row["invocation_evidence"] = (
                    _record_master_invocation_evidence(
                        result,
                        output=output,
                        role_result={
                            key: value
                            for key, value in critique_row.items()
                            if key not in {"critic_id", "invocation_evidence"}
                        },
                    )
                )
                critiques.append(critique_row)
        if retry_critic_provider_errors:
            critic_id, error = retry_critic_provider_errors[0]
            raise_provider_failure(
                "proposal_critic_repair",
                critic_id,
                error,
                slot=f"ballot:{critic_id}",
            )

    if len(critiques) != 2:
        return _proposal_packet_error(
            f"expected_two_schema_valid_critics_got_{len(critiques)}",
            context_digest=context_digest,
            source_code_digest=source_code_digest,
        )

    # Deterministic equal-criterion aggregation. Critic prose cannot create a
    # candidate. Two independent schema-valid rejects form a narrow veto so
    # final Master cannot resurrect a proposal both ballots found concretely
    # unfalsifiable, ungrounded, or strategically irrelevant.
    order = {item["proposal_id"]: index for index, item in enumerate(proposals)}
    scores = {proposal_id: 0 for proposal_id in proposal_ids}
    rejects = {proposal_id: 0 for proposal_id in proposal_ids}
    for critique_row in critiques:
        for ballot in critique_row["ballots"]:
            scores[ballot["proposal_id"]] += ballot["total_score"]
        for proposal_id in critique_row["reject"]:
            rejects[proposal_id] += 1
    proposals.sort(
        key=lambda item: (
            rejects[item["proposal_id"]] >= 2,
            -scores[item["proposal_id"]],
            order[item["proposal_id"]],
        )
    )
    allowed_proposal_ids = [
        item["proposal_id"]
        for item in proposals
        if rejects[item["proposal_id"]] < 2
    ]
    if not allowed_proposal_ids:
        return _proposal_packet_error(
            "all_three_proposals_unanimously_rejected",
            context_digest=context_digest,
            source_code_digest=source_code_digest,
        )
    packet = {
        "schema_version": _PROPOSAL_PACKET_SCHEMA_VERSION,
        "valid": True,
        "authority": (
            "ballots_rank_and_unanimous_reject_vetoes; final Master chooses among "
            "remaining IDs under frozen lineage/evidence and canonical "
            "runtime/schema/gate contracts"
        ),
        "context_digest": context_digest,
        "source_code_digest": source_code_digest,
        "evidence_mode": evidence_mode,
        "critic_criteria": _PROPOSAL_CRITIC_CRITERIA,
        "proposal_count": len(proposals),
        "valid_critic_count": len(critiques),
        "allowed_proposal_ids": allowed_proposal_ids,
        "ordered_proposals": proposals,
        "proposal_source_symbol_digests": proposal_source_symbol_digests,
        "proposal_invocations": proposal_invocations,
        "critic_reviews": critiques,
    }
    return json.dumps(
        packet,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _render_analysis_section(text: str, default_msg: str) -> str:
    """Map an analyst's raw return into the text injected into the Master prompt.

    - Empty/None -> default "no data" message (unchanged behaviour).
    - LLM_INFRA_SENTINEL -> explicit "LLM crashed" warning (so the Master does
      not misread a missing analysis as a negative business signal).
    - Anything else -> the actual analysis text.
    """
    if not text or not text.strip():
        return default_msg
    if text.strip() == LLM_INFRA_SENTINEL:
        return LLM_INFRA_SENTINEL_MSG
    return text


def _line_budget_summary(bot_v: int, *, baseline_label: str = "source") -> str:
    """Summarize LOC pressure for the exact baseline Workers will edit."""
    try:
        bot_dir = get_bot_dir(bot_v)
    except Exception:
        return "Line budget: unavailable."
    lines = [f"Line budget / file-size pressure ({baseline_label}={bot_dir.name}):"]
    for filename in ("policy.py",):
        path = bot_dir / filename
        if not path.exists():
            continue
        try:
            count = sum(1 for _ in path.open(encoding="utf-8"))
        except Exception:
            continue
        remaining = MAX_LINES_HARD_CAP - count
        status = "ok"
        if remaining <= 100:
            status = "near_hard_cap"
        lines.append(f"- {filename}: {count}/{MAX_LINES_HARD_CAP} lines, remaining={remaining}, status={status}")
    if len(lines) == 1:
        return "Line budget: policy.py not found."
    if any("near_hard_cap" in line for line in lines):
        lines.append(
            "MANDATORY when near_hard_cap: recover LOC inside policy.py before adding "
            "behavior; candidate helper modules are outside the artifact ABI."
        )
    return "\n".join(lines)


def _exact_official_compliance_feedback(baseline_v: int) -> str:
    """Render only compliance facts bound to this current-epoch artifact.

    Planning must not summarize the newest files in the global official status
    directory: those files may describe another version, artifact, or epoch.
    The strict epoch receipt and candidate hash must both bind a fact to the
    exact readable baseline. Advisory LLM repair prose and EXE outcomes are
    deliberately excluded.
    """

    baseline = get_bot_dir(int(baseline_v))
    label = bot_name(int(baseline_v))
    unavailable = (
        f"No exact-identity official-full-v5 compliance fact is available for "
        f"{label}; feedback from other epochs, versions, or artifact hashes is excluded."
    )
    try:
        from bot_artifact import hash_path
        from bot_namespace import ROLE_CANDIDATE, resolve_national_bot_spec
        from official_certification import (
            FULL_POLICY_ID,
            _deterministic_status_receipt_issues,
            read_status,
        )

        spec = resolve_national_bot_spec(
            baseline,
            ROLE_CANDIDATE,
            require_completion=False,
            require_certificate=False,
        )
        if not spec.eligible:
            return unavailable
        artifact_hash = hash_path(baseline)
        status = read_status(baseline)
        identity = (
            status.get("certification_identity")
            if isinstance(status, dict)
            else None
        )
        if not isinstance(identity, dict):
            return unavailable
        if (
            status.get("bot") != label
            or status.get("mode") != "full"
            or status.get("policy_id") != FULL_POLICY_ID
            or identity.get("policy_id") != FULL_POLICY_ID
            or identity.get("candidate_hash") != artifact_hash
        ):
            return unavailable
        # Mutable status JSON and issue strings are never planning authority on
        # their own. Re-open the content-bound deterministic evidence/archive
        # receipt for this exact live artifact before admitting even a
        # compliance-only issue. Signed publication certificates cover passes;
        # this path exists to carry exact failed/inconclusive protocol facts.
        if _deterministic_status_receipt_issues(status, candidate=baseline):
            return unavailable
        receipt = status.get("official_deterministic_status_receipt")
        verdict = receipt.get("verdict") if isinstance(receipt, dict) else None
        if not isinstance(verdict, dict):
            return unavailable
        issues = verdict.get("issues") or []
        if not isinstance(issues, list):
            return unavailable
        lines = [
            "Exact current-epoch artifact compliance fact only; official EXE "
            "wins, losses, chips, THP earnings, and advisory repair prose are excluded.",
            (
                f"- {label}: artifact_hash={artifact_hash}, policy={FULL_POLICY_ID}, "
                f"status={status.get('status')}, "
                f"classification={verdict.get('classification')}, "
                f"blocking={bool(verdict.get('blocking'))}, "
                f"inconclusive={bool(verdict.get('inconclusive'))}"
            ),
        ]
        if issues:
            lines.append(
                "  deterministic_issues: "
                + "; ".join(str(item)[:180] for item in issues[:5])
            )
        return "\n".join(lines)
    except Exception:
        return unavailable


# ──────────────────────────────────────────────
# Master Analysis
# ──────────────────────────────────────────────

async def _run_master_analysis(source_v, next_v, stagnation_info, ui,
                               match_analysis="", performance_verification="",
                               replay_spotlight="", bot_action_stats="",
                               opponent_profiles="", research_proposals="",
                               architecture_policy=None,
                               prepared_baseline=None,
                               protocol_bootstrap=None):
    """Run Master analysis — can run concurrently with daemon evaluation."""
    master_prompt = (
        Path(__file__).resolve().parent / "prompts" / "master_prompt.md"
    ).read_text(encoding="utf-8")
    if protocol_bootstrap is not None and not isinstance(protocol_bootstrap, dict):
        raise MasterAuthorityError(
            source_v,
            next_v,
            hashlib.sha256(b"protocol-bootstrap-not-object").hexdigest(),
            ["protocol_bootstrap_not_object"],
        )
    protocol_bootstrap_active = isinstance(protocol_bootstrap, dict)
    protocol_bootstrap_mode = (
        str(protocol_bootstrap.get("mode") or "")
        if protocol_bootstrap_active
        else ""
    )
    fresh_bootstrap = (
        protocol_bootstrap_mode == "fresh_national_policy_bootstrap"
    )
    singleton_no_strength = (
        protocol_bootstrap_mode == "singleton_strict_bootstrap"
    )
    protocol_bootstrap_no_strength = fresh_bootstrap or singleton_no_strength
    if protocol_bootstrap_active and not protocol_bootstrap_no_strength:
        raise MasterAuthorityError(
            source_v,
            next_v,
            hashlib.sha256(b"protocol-bootstrap-mode-invalid").hexdigest(),
            [f"protocol_bootstrap_mode_invalid:{protocol_bootstrap_mode or 'missing'}"],
        )
    strict_checkpoint = None
    if protocol_bootstrap_active:
        from evolution_infra import read_pipeline_checkpoint

        strict_checkpoint = read_pipeline_checkpoint() or {}
        checkpoint_bootstrap = (
            (strict_checkpoint.get("audit_context") or {}).get(
                "protocol_bootstrap"
            )
            if isinstance(strict_checkpoint, dict)
            else None
        )
        if checkpoint_bootstrap != protocol_bootstrap:
            raise MasterAuthorityError(
                source_v,
                next_v,
                hashlib.sha256(
                    json.dumps(
                        {
                            "argument": protocol_bootstrap,
                            "checkpoint": checkpoint_bootstrap,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
                ["protocol_bootstrap_argument_checkpoint_mismatch"],
            )
        if fresh_bootstrap:
            from system_strict_bootstrap import is_declared_native_bootstrap

            if not is_declared_native_bootstrap(strict_checkpoint):
                raise MasterAuthorityError(
                    source_v,
                    next_v,
                    hashlib.sha256(b"strict-checkpoint-missing").hexdigest(),
                    ["strict_authority_checkpoint_not_declared"],
                )
        else:
            singleton_errors = []
            if strict_checkpoint.get("stage") != "direction_audited":
                singleton_errors.append(
                    "singleton_master_checkpoint_not_direction_audited"
                )
            try:
                from generation_evidence import (
                    build_protocol_bootstrap_evidence_identity,
                    live_protocol_bootstrap_allocation_errors,
                )

                singleton_identity = build_protocol_bootstrap_evidence_identity(
                    strict_checkpoint,
                    version=int(next_v),
                    source_v=int(source_v),
                )
                if (
                    singleton_identity.get("mode")
                    != "singleton_strict_successor_bootstrap"
                ):
                    singleton_errors.append(
                        "singleton_master_evidence_identity_mode_mismatch"
                    )
                singleton_errors.extend(
                    live_protocol_bootstrap_allocation_errors(
                        strict_checkpoint,
                        version=int(next_v),
                    )
                )
            except Exception as exc:
                singleton_errors.append(
                    "singleton_master_evidence_identity_invalid:"
                    f"{type(exc).__name__}:{str(exc)[:240]}"
                )
            try:
                from checkpoint_schema import (
                    live_checkpoint_parent_authority_errors,
                )

                singleton_errors.extend(
                    live_checkpoint_parent_authority_errors(strict_checkpoint)
                )
            except Exception as exc:
                singleton_errors.append(
                    "singleton_master_parent_authority_error:"
                    f"{type(exc).__name__}"
                )
            try:
                from evolution_infra import get_active_bots

                if list(get_active_bots()) != [bot_name(int(source_v))]:
                    singleton_errors.append(
                        "singleton_master_active_pool_not_exact_parent"
                    )
            except Exception as exc:
                singleton_errors.append(
                    "singleton_master_active_pool_unavailable:"
                    f"{type(exc).__name__}"
                )
            if singleton_errors:
                raise MasterAuthorityError(
                    source_v,
                    next_v,
                    hashlib.sha256(
                        json.dumps(
                            singleton_errors,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest(),
                    list(dict.fromkeys(singleton_errors)),
                )
    if protocol_bootstrap_no_strength:
        from strict_authority_workflow import recover_accepted_master_final_result

        recovered_final = recover_accepted_master_final_result(
            strict_checkpoint,
            architecture_policy=(
                architecture_policy
                if isinstance(architecture_policy, dict)
                else {}
            ),
        )
        if recovered_final is not None:
            ui.log_history(
                "Master recovered sealed final authority without re-running "
                "Scout or Critic providers.",
                "info",
            )
            return recovered_final
    # Apply section budgets so one evidence source cannot crowd out match analysis.
    # C-class: render the sentinel (returned when the analyst LLM crashed on an
    # infrastructure error) into an explicit warning BEFORE trimming, so the
    # Master sees "LLM crashed" rather than "no data" (which would be read as a
    # negative business signal). Non-sentinel text passes through unchanged.
    match_analysis_rendered = (
        PROTOCOL_BOOTSTRAP_NO_STRENGTH_PLACEHOLDER
        if protocol_bootstrap_no_strength
        else _render_analysis_section(match_analysis, "")
    )
    perf_rendered = (
        PROTOCOL_BOOTSTRAP_NO_STRENGTH_PLACEHOLDER
        if protocol_bootstrap_no_strength
        else _render_analysis_section(
            performance_verification, "No performance verification data available.",
        )
    )
    if protocol_bootstrap_no_strength:
        stagnation_info = PROTOCOL_BOOTSTRAP_NO_STRENGTH_PLACEHOLDER
    match_analysis_trimmed = _trim_to_budget(match_analysis_rendered, 10_000, tail=True)
    perf_trimmed = _trim_to_budget(perf_rendered, 4_000)

    if protocol_bootstrap_no_strength:
        bot_action_stats_trimmed = PROTOCOL_BOOTSTRAP_NO_STRENGTH_PLACEHOLDER
        opponent_profiles_trimmed = PROTOCOL_BOOTSTRAP_NO_STRENGTH_PLACEHOLDER
        replay_spotlight_trimmed = PROTOCOL_BOOTSTRAP_NO_STRENGTH_PLACEHOLDER
    else:
        bot_action_stats_trimmed = _trim_to_budget(
            bot_action_stats or "No bot action statistics available.", 12_000)
        opponent_profiles_trimmed = _trim_to_budget(
            opponent_profiles or "No per-opponent behavior profiles available.", 8_000)
        replay_spotlight_trimmed = _trim_to_budget(
            replay_spotlight or "No replay spotlight data available.", 8_000)
    research_trimmed = _trim_to_budget(
        (
            "No admissible non-match literature receipt was supplied for this "
            "protocol-bootstrap plan. Historical matchup-derived research was not loaded."
            if protocol_bootstrap_no_strength
            else research_proposals
            or "No web-derived research proposals this generation (run_literature_probe not triggered or returned none)."
        ),
        4_000,
    )
    planning_baseline_v = (
        next_v
        if isinstance(prepared_baseline, dict) or protocol_bootstrap_no_strength
        else source_v
    )
    planning_baseline_label = (
        "prepared_crossover_child"
        if isinstance(prepared_baseline, dict)
        else "prepared_fresh_strict_control"
        if fresh_bootstrap
        else "prepared_singleton_child"
        if singleton_no_strength
        else "source_parent"
    )
    if fresh_bootstrap:
        official_feedback = (
            "Historical official-certification feedback was not loaded. Use only "
            "the repository-pinned official oracle and architecture policy."
        )
    elif singleton_no_strength:
        official_feedback = _trim_to_budget(
            _exact_official_compliance_feedback(int(source_v)),
            6_000,
        )
    else:
        official_feedback = _trim_to_budget(
            _exact_official_compliance_feedback(int(planning_baseline_v)),
            6_000,
        )
    try:
        from national_capability_contract import (
            evaluate_national_capabilities,
            national_runtime_feedback_summary,
        )
        runtime_capabilities = evaluate_national_capabilities(
            get_bot_dir(planning_baseline_v)
        )
        runtime_feedback = _trim_to_budget(
            national_runtime_feedback_summary(runtime_capabilities),
            4_000,
        )
    except Exception as exc:
        runtime_feedback = f"National runtime architecture feedback unavailable: {type(exc).__name__}: {str(exc)[:200]}"
    if isinstance(architecture_policy, dict):
        try:
            from runtime_architecture_policy import architecture_policy_prompt
            architecture_policy_text = architecture_policy_prompt(architecture_policy)
        except Exception as exc:
            architecture_policy_text = (
                f"Runtime architecture policy rendering failed: {type(exc).__name__}: {str(exc)[:200]}"
            )
    else:
        architecture_policy_text = "System-owned runtime architecture policy: not active for this source."
    proposal_allowed_primaries = _architecture_proposal_primaries(
        architecture_policy if isinstance(architecture_policy, dict) else None
    )
    try:
        from strategy_reference_pack import master_reference_summary
        strategy_reference_packet = _trim_to_budget(
            master_reference_summary(
                allowed_primaries=proposal_allowed_primaries,
            ),
            6_000,
        )
    except Exception as exc:
        strategy_reference_packet = (
            "Local strategy reference cards unavailable: "
            f"{type(exc).__name__}: {str(exc)[:200]}"
        )
    try:
        from workflow_profiles import get_workflow_profile, profile_summary
        workflow_profile = get_workflow_profile()
        workflow_profile_text = profile_summary(workflow_profile)
    except Exception as exc:
        raise RuntimeError(
            "workflow_profile_contract_unavailable: "
            f"{type(exc).__name__}: {str(exc)[:240]}"
        ) from exc
    line_budget_text = _line_budget_summary(
        planning_baseline_v,
        baseline_label=planning_baseline_label,
    )
    if isinstance(prepared_baseline, dict):
        try:
            from prepared_baseline_contract import prepared_baseline_prompt

            prepared_baseline_text = _trim_to_budget(
                prepared_baseline_prompt(prepared_baseline),
                18_000,
            )
        except Exception as exc:
            prepared_baseline_text = (
                "Prepared crossover baseline rendering failed closed before this "
                f"prompt should run: {type(exc).__name__}: {str(exc)[:240]}"
            )
    elif fresh_bootstrap:
        prepared_baseline_text = (
            "Fresh strict-control baseline: Workers start from the prepared target "
            "artifact whose national_bot.py has already been replaced and verified "
            "by the system-owned current runtime. The historical source launcher is "
            "not executable evidence. This fixed blueprint establishes the epoch's "
            "protocol and capability floor; it is not a measured strength improvement."
        )
    elif singleton_no_strength:
        prepared_baseline_text = (
            f"Singleton strict baseline: `{bot_relpath(next_v)}/` is the frozen "
            f"prepared copy inherited from published `{bot_relpath(source_v)}/`. "
            "The parent is readable comparison evidence and the target is writable. "
            "No peer rating or H2H evidence exists yet, so this plan may propose a "
            "poker mechanism but must not claim that strength is already proven."
        )
    else:
        prepared_baseline_text = (
            "No two-parent prepared baseline: Workers start from the copied source parent."
        )
    if protocol_bootstrap_no_strength:
        h2h_data_file = "UNAVAILABLE_PROTOCOL_BOOTSTRAP"
        selection_data_file = "UNAVAILABLE_PROTOCOL_BOOTSTRAP"
        h2h_snapshot_contract = (
            f"{'FRESH STRICT CONTROL' if fresh_bootstrap else 'SINGLETON STRICT GENERATION'}: "
            "no two-bot strict executable pool exists, so "
            "ratings, H2H, rankings, match replays, and strength conclusions are "
            "intentionally unavailable. Do not read live result files or cite "
            "historical quarantined ratings. Plan only from admissible code, typed "
            "strategy references, and the content-bound no-strength context. "
            f"receipt={protocol_bootstrap.get('receipt_digest')}"
        )
        allowed_evidence_snapshot_dir = str(
            get_bot_dir(next_v) / ".protocol_bootstrap_no_strength_evidence"
        )
    else:
        try:
            from evidence_snapshot import (
                h2h_snapshot_contract_text,
                load_generation_snapshot_identity,
            )
            h2h_snapshot = load_generation_snapshot_identity(next_v)
            if not h2h_snapshot.get("available"):
                raise RuntimeError(
                    f"generation evidence snapshot unavailable: {h2h_snapshot.get('reason')}"
                )
            h2h_data_file = h2h_snapshot.get("h2h_relpath", "web/core/results/head_to_head.json")
            selection_data_file = h2h_snapshot.get(
                "selection_relpath",
                f"web/core/results/v{next_v}/evidence_snapshot/selection_snapshot.json",
            )
            h2h_snapshot_contract = h2h_snapshot_contract_text(next_v, source_v=source_v)
            allowed_evidence_snapshot_dir = str(
                Path(h2h_snapshot["manifest_path"]).parent
            )
        except Exception as exc:
            ui.log_history(
                f"Master blocked: stable evaluation snapshot unavailable ({exc})",
                "error",
            )
            return None

    if fresh_bootstrap:
        planning_code_input_contract = (
            f"- `{bot_relpath(next_v)}/` — the sole readable prepared code "
            "baseline and the target Workers must edit and verify.\n"
            f"- v{source_v} is numeric completion high-water authority only. It "
            "is not a source, parent, reference, opponent, or readable code input; "
            "do not open, compare, cite, import, or inherit it."
        )
        source_selection_contract = (
            f"This is the one-time fresh strict bootstrap for v{next_v}. "
            f"Historical high-water v{source_v} supplies only the next version "
            "number and is not an ancestor. The prepared target artifact is the "
            "only code baseline. You MUST NOT set `branch_from`, a parent, or any "
            "source-override field."
        )
        target_path_contract = (
            f"This bootstrap plans directly on prepared target `{bot_relpath(next_v)}/`. "
            "Every Worker edit/Read/py_compile command MUST point there; imports, "
            "smoke and dynamic tests belong to the system quality gate. "
            "No historical code directory is a readable comparison input; the "
            "system-owned bootstrap receipt proves fresh lineage."
        )
    elif singleton_no_strength:
        planning_code_input_contract = (
            f"- `{bot_relpath(source_v)}/` — the sole published strict parent; "
            "read-only comparison and inheritance authority.\n"
            f"- `{bot_relpath(next_v)}/` — the frozen prepared child and only "
            "writable Worker target.\n"
            "- No strength snapshot exists until this second Bot is published; "
            "do not invent ratings, H2H, replay, or population rankings."
        )
        source_selection_contract = (
            f"The system fixed published v{source_v} as the only possible parent "
            f"for singleton v{next_v}. Do not rerank or set `branch_from`; this is "
            "inheritance authority, not evidence that either strategy is stronger."
        )
        target_path_contract = (
            f"This generation evolves published source `{bot_relpath(source_v)}/` "
            f"into target `{bot_relpath(next_v)}/`. Workers edit only the target; "
            "the source remains read-only. The proposal-specific runtime "
            "counterfactual and native precommit against the published parent are "
            "mandatory regression evidence, not a claim of final superiority."
        )
    else:
        planning_code_input_contract = (
            f"- `{bot_relpath(source_v)}/` — current source bot code; read-only "
            "parent/reference.\n"
            f"- `{bot_relpath(next_v)}/` — target bot directory; Workers must edit "
            "and verify this directory."
        )
        source_selection_contract = (
            "The source ancestor to evolve from is decided automatically by the "
            "system in prepare_generation from the frozen selection evidence. You "
            "MUST NOT set `branch_from` or any source-override field in your plan; "
            "the system rejects it. Focus only on the task plan and analysis."
        )
        target_path_contract = (
            f"This generation evolves source `{bot_relpath(source_v)}/` into target "
            f"`{bot_relpath(next_v)}/`.\n\nIn every `worker_prompt`, "
            f"edit/Read/py_compile commands MUST point at "
            f"`{bot_relpath(next_v)}/`, never `{bot_relpath(source_v)}/`. The source "
            "path is read-only comparison evidence; do not ask Workers to edit, "
            "patch, compile, import from, or run checks inside it. Imports, smoke "
            "and dynamic tests are system quality-gate work. The Worker "
            "wrapper already supplies the exact parent-vs-target diff."
        )

    master_template_values = {
        "stagnation_info": stagnation_info,
        "match_analysis": match_analysis_trimmed,
        "performance_verification": perf_trimmed,
        "source_v": str(source_v),
        "next_v": str(next_v),
        "replay_spotlight": replay_spotlight_trimmed,
        "bot_action_stats": bot_action_stats_trimmed,
        "opponent_profiles": opponent_profiles_trimmed,
        "research_proposals": research_trimmed,
        "official_feedback": official_feedback,
        "runtime_feedback": runtime_feedback,
        "strategy_reference_packet": strategy_reference_packet,
        "h2h_data_file": h2h_data_file,
        "selection_data_file": selection_data_file,
        "h2h_snapshot_contract": h2h_snapshot_contract,
        "master_plan_executable_contract": master_plan_executable_contract_text(),
        "planning_code_input_contract": planning_code_input_contract,
        "source_selection_contract": source_selection_contract,
        "target_path_contract": target_path_contract,
    }
    master_prompt = substitute_template(master_prompt, master_template_values)
    evidence_context = (
        "Protocol bootstrap has no strength snapshot. Do not open live ratings, "
        "H2H, replay, bot_stats, rating_history, or eval_rounds files.\n"
        if protocol_bootstrap_no_strength
        else
        f"Selection evidence snapshot: {selection_data_file}\n"
        f"Use only that digest-bound snapshot for ratings, RD, games, coverage, trends, and ranking; "
        f"do not reopen live glicko_ratings.json, bot_stats.json, rating_history.jsonl, "
        f"or eval_rounds.jsonl.\n"
        f"Head-to-Head data snapshot: {h2h_data_file}\n"
        f"Do not read live H2H for matchup counts during planning; use the snapshot above.\n"
    )
    source_context = (
        "Historical lineage source directory: quarantined and intentionally not "
        "provided; do not open or cite it.\n"
        if fresh_bootstrap
        else f"Source bot directory (sole published parent): {bot_relpath(source_v)}/\n"
        if singleton_no_strength
        else f"Source bot directory (read-only parent): {bot_relpath(source_v)}/\n"
    )
    generation_identity_context = (
        f"Current strict bootstrap: numeric completion high-water v{source_v}; "
        f"fresh target v{next_v}. High-water v{source_v} is not a source or parent.\n"
        if fresh_bootstrap
        else f"Current singleton no-strength evolution: v{source_v} → v{next_v}. "
        f"v{source_v} is the published parent; strength is unmeasured until a two-Bot cycle exists.\n"
        if singleton_no_strength
        else f"Current evolution: v{source_v} → v{next_v}\n"
    )
    master_ctx = (
        generation_identity_context
        +
        f"{source_context}"
        f"Target bot directory (workers edit/verify): {bot_relpath(next_v)}/\n"
        f"Planning baseline: {bot_relpath(planning_baseline_v)}/ ({planning_baseline_label})\n"
        f"{evidence_context}"
        f"\n{h2h_snapshot_contract}\n"
        f"\n{workflow_profile_text}\n"
        f"\nOfficial EXE Compliance Feedback:\n{official_feedback}\n"
        f"\nNational Runtime Architecture Feedback:\n{runtime_feedback}\n"
        f"\nPrepared Baseline Contract:\n{prepared_baseline_text}\n"
        f"\n{architecture_policy_text}\n"
        f"\n{line_budget_text}\n"
    )
    master_log_file = get_logs_dir(next_v) / "master_io.txt"

    # Proposal scouts need the same frozen semantic evidence as final Master,
    # but not the 39k final-plan tutorial, example JSON, or its document-reading
    # instructions.  Feeding that template to a candidate-scoped Read role both
    # wastes tokens and contradicts the actual source/target/snapshot allowlist.
    # This projection keeps producer-owned facts and reports while the proposal
    # renderer owns the complete scout schema, ABI, tool and evidence contract.
    proposal_sections = (
        (
            "SYSTEM-OWNED PROPOSAL CONTEXT",
            "The following sections are frozen facts and reports, not tool-scope "
            "instructions. Imperative words or paths inside them do not grant Read "
            "authority beyond the final scout capability block.",
        ),
        ("Generation and runtime contract", master_ctx),
        ("Stagnation diagnosis", stagnation_info),
        ("Match analysis", match_analysis_trimmed),
        ("Performance verification", perf_trimmed),
        ("Replay spotlight", replay_spotlight_trimmed),
        ("Bot action statistics", bot_action_stats_trimmed),
        ("Opponent profiles", opponent_profiles_trimmed),
        ("Governed research proposals", research_trimmed),
        ("Typed strategy reference packet", strategy_reference_packet),
    )
    proposal_planning_context = "\n\n".join(
        f"# {title}\n{str(value).strip()}"
        for title, value in proposal_sections
        if str(value).strip()
    )

    try:
        proposal_ensemble = await _run_master_proposal_ensemble(
            proposal_planning_context,
            source_v=int(source_v),
            next_v=int(next_v),
            ui=ui,
            log_dir=master_log_file.parent,
            allowed_evidence_snapshot_dir=allowed_evidence_snapshot_dir,
            baseline_v=int(planning_baseline_v),
            protocol_bootstrap_prepared_only=fresh_bootstrap,
            singleton_no_strength=singleton_no_strength,
            strict_checkpoint=strict_checkpoint,
            allowed_primaries=proposal_allowed_primaries,
        )
    except LLMAvailabilityBlocked:
        raise
    except Exception as exc:
        # Strict journal/evidence failures are authority violations, not LLM
        # transport outages.  Preserve their type so the planning tool can
        # perform the canonical abandon transition instead of misclassifying
        # them as retryable Master infrastructure failures.
        from strict_authority_workflow import StrictAuthorityError

        if isinstance(exc, (StrictAuthorityError, MasterInfrastructureError)):
            raise
        raise MasterInfrastructureError(
            source_v,
            next_v,
            hashlib.sha256(
                proposal_planning_context.encode("utf-8")
            ).hexdigest(),
            f"proposal_ensemble:{type(exc).__name__}: {str(exc)[:400]}",
        ) from exc
    proposal_packet, proposal_packet_errors = _parse_valid_proposal_packet(
        proposal_ensemble
    )
    if proposal_packet_errors:
        ui.log_history(
            "Master blocked: proposal ensemble failed closed ("
            + "; ".join(proposal_packet_errors[:4])
            + ").",
            "error",
        )
        try:
            from system_log import log_system_event
            log_system_event(
                "pipeline.master_proposal_packet_rejected",
                "error",
                f"Master v{next_v} proposal packet rejected",
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "errors": proposal_packet_errors,
                },
            )
        except Exception:
            pass
        return None
    assert proposal_packet is not None
    expected_proposal_evidence_mode = (
        "fresh_strict_control_no_strength"
        if fresh_bootstrap
        else "singleton_parent_no_strength"
        if singleton_no_strength
        else "frozen_strength_snapshot"
    )
    if proposal_packet.get("evidence_mode") != expected_proposal_evidence_mode:
        ui.log_history(
            "Master blocked: proposal evidence mode does not match the checkpoint.",
            "error",
        )
        return None
    try:
        expected_symbol_digests = _proposal_source_symbol_digests(
            proposal_packet.get("ordered_proposals") or [],
            get_bot_dir(int(planning_baseline_v)),
        )
    except Exception as exc:
        ui.log_history(
            "Master blocked: prepared baseline symbol digest verification failed "
            f"({type(exc).__name__}: {str(exc)[:180]}).",
            "error",
        )
        return None
    if proposal_packet.get("proposal_source_symbol_digests") != (
        expected_symbol_digests
    ):
        ui.log_history(
            "Master blocked: proposal source-symbol digests do not match the "
            "prepared baseline.",
            "error",
        )
        return None
    measurement_targets = []
    for proposal in proposal_packet.get("ordered_proposals") or []:
        parsed_measurement = _parsed_proposal_measurement(
            str((proposal or {}).get("measurement") or "")
        )
        measurement_targets.append(
            parsed_measurement.get("target") if parsed_measurement else ""
        )
    expected_singleton_target = bot_name(int(source_v))
    forbidden_candidate_target = bot_name(int(next_v))
    if (
        singleton_no_strength
        and any(target != expected_singleton_target for target in measurement_targets)
    ) or (
        not protocol_bootstrap_no_strength
        and any(target == forbidden_candidate_target for target in measurement_targets)
    ):
        ui.log_history(
            "Master blocked: proposal measurement target is not an admissible "
            "published/frozen opponent.",
            "error",
        )
        return None
    master_ctx += (
        "\n# Weak-model proposal ensemble (evidence-validated choices)\n"
        + proposal_ensemble
        + "\nFINAL PROPOSAL BINDING CONTRACT (overrides any conflicting embedded "
        "output example): select exactly one allowed proposal and emit its ID as the "
        "top-level string field selected_proposal_id. Copy that proposal's "
        "targeted_failure EXACTLY into plan targeted_failure and copy its measurement "
        "EXACTLY into plan measurement_plan. Every selected "
        "proposal target_files path must be writable in at least one task. You may "
        "elaborate implementation details, but may not synthesize a fourth proposal, "
        "combine mechanisms, or treat critic votes as permission to change source, "
        "evidence, scope, or gates. The proposal falsifier's "
        "state_learning_primary and every required_primary_checks item are exact; "
        "plan_required_floor_checks are additional and do not replace them.\n"
        "SYSTEM-DERIVED PER-PROPOSAL COMPILATION CONTRACTS (Python Unicode "
        "character counts, not tokens or UTF-8 bytes):\n"
        + _proposal_compilation_contract_text(proposal_packet)
        + "\nFor the selected proposal, every provider worker_prompt that touches a "
        "target file MUST be no longer than max_provider_chars. The system then "
        "appends reserved_selected_contract_chars, separator_chars, and a bounded "
        "runtime-contract block no longer than reserved_runtime_contract_max_chars; do not "
        "repeat the immutable proposal prose merely to consume that reserved budget.\n"
    )

    master_schema_repair_suffix = ""
    for attempt in range(MAX_MASTER_RETRIES):
        ui.clear_io()
        strict_final_call = None
        final_log_file = master_log_file
        final_role = f"MASTER (Try {attempt+1})"
        if protocol_bootstrap_no_strength:
            from strict_authority_workflow import (
                final_master_call_context,
                new_call,
                strict_invocation_log_path,
            )

            strict_final_call = new_call(
                strict_checkpoint,
                slot="master:final",
                role=final_role,
                context_binding=final_master_call_context(
                    proposal_packet,
                    architecture_policy if isinstance(architecture_policy, dict) else {},
                ),
            )
            if strict_final_call.get("replay_provider"):
                replay_role = str(strict_final_call.get("actual_role") or "")
                if not re.fullmatch(r"MASTER \(Try [1-3]\)", replay_role):
                    raise RuntimeError(
                        f"strict_final_master_replay_role_invalid:{replay_role}"
                    )
                final_role = replay_role
            final_log_file = strict_invocation_log_path(
                strict_final_call,
                logs_dir=master_log_file.parent,
                basename=master_log_file.name,
            )
        try:
            from llm_query import render_llm_prompt

            rendered_prompt = render_llm_prompt(
                final_role,
                producer=_render_master_final_provider_prompt,
                renderer_inputs={
                    "template_values": master_template_values,
                    "master_context": master_ctx,
                    "proposal_ensemble": proposal_ensemble,
                    "source_v": int(source_v),
                    "next_v": int(next_v),
                    "invocation_id": str(
                        (strict_final_call or {}).get("invocation_id") or ""
                    ),
                    "schema_repair_suffix": master_schema_repair_suffix,
                    "final_output_guard": _master_final_emission_guard(
                        proposal_packet
                    ),
                },
            )
            output, _, _ = await run_claude_query(
                rendered_prompt, [], ui,
                final_role, final_log_file,
                tools=[],
                strict_authority=strict_final_call,
            )
        except LLMAvailabilityBlocked:
            raise
        except Exception as exc:
            from strict_authority_workflow import StrictAuthorityError

            if isinstance(exc, StrictAuthorityError):
                raise
            _final_mode = f"LLM_EXCEPTION:{type(exc).__name__}"
            try:
                ui.log_history(
                    f"Master LLM call failed ({type(exc).__name__}): {str(exc)[:240]}",
                    "error",
                )
            except Exception:
                pass
            try:
                from system_log import log_system_event
                log_system_event(
                    "pipeline.master_llm_call_failed",
                    "error",
                    (
                        f"Master v{next_v} try {attempt+1} LLM call failed: "
                        f"{type(exc).__name__}: {str(exc)[:240]}"
                    ),
                    {
                        "next_v": next_v,
                        "source_v": source_v,
                        "attempt": attempt + 1,
                        "failure_mode": _final_mode,
                        "exception_type": type(exc).__name__,
                        "error": str(exc)[:500],
                    },
                )
            except Exception:
                pass
            raise MasterInfrastructureError(
                source_v,
                next_v,
                hashlib.sha256(
                    (master_prompt + "\n" + master_ctx).encode("utf-8")
                ).hexdigest(),
                f"{type(exc).__name__}: {str(exc)[:400]}",
            ) from exc
        # A2 (v125 retry-storm fix): classify the parse failure so the log
        # distinguishes NO_FENCE (model never emitted JSON) / NO_JSON (empty) /
        # PARSE_ERROR (had JSON but unparseable) — instead of the undifferentiated
        # "malformed JSON" that hid three distinct root causes.
        from llm_query import parse_json_output_with_mode
        data, _failure_mode = parse_json_output_with_mode(output)
        if data and "tasks" in data:
            (
                data,
                selected_proposal,
                selection_errors,
                metadata_rebound,
            ) = _canonicalize_selected_proposal_metadata(
                data,
                proposal_packet,
            )
            proposal_binding_errors = _validate_final_proposal_binding(
                data,
                proposal_packet,
            ) if not selection_errors else selection_errors
            if proposal_binding_errors:
                ui.log_history(
                    "Master plan rejected by proposal binding: "
                    + "; ".join(proposal_binding_errors[:4]),
                    "warn",
                )
                if attempt + 1 < MAX_MASTER_RETRIES:
                    # The final renderer rebuilds the template on every call;
                    # only this suffix is part of the next provider prompt.
                    # Mutating the earlier local ``master_prompt`` silently
                    # discarded the deterministic binding diagnostics.
                    master_schema_repair_suffix += (
                        "\n\n# Previous proposal binding failed; re-emit the complete "
                        "plan and fix all items:\n- "
                        + "\n- ".join(proposal_binding_errors)[:1500]
                        + "\n"
                    )
                    import asyncio
                    await asyncio.sleep(2)
                    continue
                try:
                    from system_log import log_system_event
                    log_system_event(
                        "pipeline.master_proposal_binding_exhausted",
                        "error",
                        f"Master v{next_v} failed proposal binding after retries",
                        {
                            "next_v": next_v,
                            "source_v": source_v,
                            "errors": proposal_binding_errors,
                        },
                    )
                except Exception:
                    pass
                return None
            selected_proposal_id = data.pop("selected_proposal_id")
            assert selected_proposal is not None
            if metadata_rebound:
                ui.log_history(
                    "Master selected-proposal metadata was rebound by the system: "
                    + ", ".join(metadata_rebound) + ".",
                    "info",
                )
                try:
                    from system_log import log_system_event
                    log_system_event(
                        "pipeline.master_selected_proposal_metadata_bound",
                        "info",
                        f"Master v{next_v}: bound selected proposal metadata",
                        {
                            "next_v": next_v,
                            "source_v": source_v,
                            "selected_proposal_id": selected_proposal_id,
                            "fields": list(metadata_rebound),
                        },
                    )
                except Exception:
                    pass
            data = _bind_selected_proposal_workers(data, selected_proposal)
            # The structured runtime contract and reference-card choice already
            # determine a small set of literal execution anchors.  Bind those
            # system-owned terms before Pydantic validation instead of asking a
            # weaker planner model to reproduce them losslessly in free prose.
            # Invalid contracts are intentionally left untouched and still fail
            # the canonical schema gate below.
            from plan_compiler import (
                bind_system_owned_policy_abi,
                bind_system_owned_worker_contract_terms,
            )
            data, _policy_abi_binding_meta = (
                bind_system_owned_policy_abi(
                    data,
                    policy=(
                        architecture_policy
                        if isinstance(architecture_policy, dict)
                        else None
                    ),
                )
            )
            data, _binding_meta = bind_system_owned_worker_contract_terms(data)
            if _binding_meta.get("overflow_tasks"):
                binding_overflow_error = _proposal_binding_error(
                    "system_owned_worker_contract_binding_overflow",
                    {"tasks": _binding_meta["overflow_tasks"]},
                )
                ui.log_history(
                    "Master plan rejected by system-owned contract binding: "
                    + binding_overflow_error,
                    "warn",
                )
                if attempt + 1 < MAX_MASTER_RETRIES:
                    master_schema_repair_suffix += (
                        "\n\n# Previous system-owned contract binding failed; "
                        "re-emit the complete plan and fix this item:\n- "
                        + binding_overflow_error[:1500]
                        + "\n"
                    )
                    import asyncio
                    await asyncio.sleep(2)
                    continue
                return None
            if _policy_abi_binding_meta.get("bound"):
                ui.log_history(
                    "Master plan compiler bound the closed national policy ABI.",
                    "info",
                )
            if _binding_meta.get("bound"):
                ui.log_history(
                    "Master plan contract compiler bound missing execution anchors "
                    f"for {len(_binding_meta.get('bound_tasks', []))} worker task(s).",
                    "info",
                )
                try:
                    from system_log import log_system_event
                    log_system_event(
                        "pipeline.master_contract_terms_bound",
                        "info",
                        f"Master v{next_v}: bound system-owned worker contract terms",
                        {
                            "next_v": next_v,
                            "source_v": source_v,
                            "attempt": attempt + 1,
                            "binding": _binding_meta,
                        },
                    )
                except Exception:
                    pass
            # P0 修复：在 Pydantic 剥离 branch_from (extra='ignore') 之前，对原始 dict
            # 跑 Master 的 source-override 硬校验。MasterPlan 删除 branch_from 字段后，
            # model_validate 会静默丢弃该键，必须在丢弃前拦截。
            # This pre-schema check catches source override fields before
            # Pydantic strips unknown keys. Canonical validation runs again
            # after plan normalization in the tool layer.
            _src_override = any(data.get(f) for f in ("branch_from", "source_override", "source_v_override"))
            if _src_override:
                ui.log_history(
                    "Master plan rejected: must not set branch_from.",
                    "warn",
                )
                import asyncio as _asyncio
                await _asyncio.sleep(2)
                continue
            from output_schema import validate_agent_output
            data, errors = validate_agent_output("master", data)
            if errors:
                ui.log_history(f"Master plan validation issues: {'; '.join(errors[:3])}", "warn")
                # Hard gate: inject schema errors into the next retry's prompt so
                # the Master re-emits strictly schema-conformant JSON rather than
                # silently returning the malformed plan. errors text is truncated
                # to avoid unbounded prompt growth across retries.
                if attempt + 1 < MAX_MASTER_RETRIES:
                    err_block = "\n".join(f"- {e}" for e in errors)[:1500]
                    master_schema_repair_suffix += (
                        "\n\n# 上一轮计划校验失败，必须修正：\n"
                        + err_block
                        + "\n请重新输出严格符合 schema 的 JSON。"
                    )
                    ui.log_history("Master plan rejected by schema. Retrying with errors...", "warn")
                    import asyncio
                    await asyncio.sleep(2)
                    continue
                # Retries exhausted: fail closed. A malformed plan cannot become
                # an executable worker contract merely because retries ran out.
                ui.log_history(
                    f"Master plan still violates schema after {MAX_MASTER_RETRIES} retries; "
                    "rejecting generation plan.",
                    "error"
                )
                try:
                    from system_log import log_system_event
                    log_system_event(
                        "pipeline.master_schema_gate_exhausted", "error",
                        f"Master plan schema validation failed after {MAX_MASTER_RETRIES} retries: "
                        + "; ".join(errors[:5]),
                    )
                except Exception:
                    pass
                return None
            data["selected_proposal_id"] = selected_proposal_id
            data["proposal_binding"] = _selected_proposal_binding(
                selected_proposal,
                proposal_packet,
            )
            # Freeze all three independent proposals and both anonymous ballots
            # with the selected plan.  The deterministic Worker envelope then
            # binds the actual governance evidence, not merely an invocation bit.
            data["proposal_ensemble"] = proposal_packet
            if protocol_bootstrap_no_strength:
                from strict_authority_workflow import accept_role_result

                accept_role_result(
                    strict_final_call,
                    role_result=data,
                    parse_contract="master-plan-schema-v1",
                )
            # SUCCESS path (BUGFIX, root cause of the v107–v127 Master deadlock):
            # the plan parsed with `tasks`, carries no branch_from override, and
            # passed schema validation with NO errors. This `return data` was
            # MISSING for 11+ generations: every valid plan fell through to the
            # "Master output malformed JSON" branch below, burned all
            # MAX_MASTER_RETRIES, and returned None. The SDK-signature fix
            # (48b51f2/c537ff1) only cured the EMPTY-output case — once plans
            # came back non-empty and valid, this missing return STILL discarded
            # them, which is exactly why "malformed-JSON persists post-fix" was
            # observed. NOT a schema/SDK-sig/direction-audit problem.
            ui.log_history("Master plan accepted (valid JSON, schema-clean).", "info")
            # RC1 (success-path symmetry): emit the success terminal event here so
            # the clean-success path is as visible as the failure paths above. The
            # degraded path (:177) already emits pipeline.master_schema_gate_exhausted
            # (error) — only this clean branch was event-silent. Without it, a
            # master-success-return-bug regression (valid plan parsed but the
            # function then failed to return) is invisible in the event stream;
            # prepare_done=N vs master_plan_accepted=0 would now expose it at once.
            try:
                from event_bus import success
                success("pipeline.master_plan_accepted",
                        f"Master v{next_v} plan accepted (schema-clean, try {attempt+1})",
                        next_v=next_v, source_v=source_v,
                        master_try=attempt + 1,
                        num_tasks=len(data.get("tasks", [])),
                        selected_proposal_id=selected_proposal_id,
                        proposal_context_digest=proposal_packet["context_digest"])
            except Exception:
                pass
            return data
        ui.log_history(
            f"Master output malformed JSON (mode={_failure_mode}). Retrying...",
            "warn",
        )
        try:
            from system_log import log_system_event
            log_system_event(
                "pipeline.master_malformed_json", "warn",
                f"Master v{next_v} try {attempt+1} output parse failed (mode={_failure_mode})",
                {"next_v": next_v, "source_v": source_v, "attempt": attempt + 1,
                 "failure_mode": _failure_mode, "output_len": len(output or "")},
            )
        except Exception:
            pass
        import asyncio
        await asyncio.sleep(2)

    _final_mode = locals().get("_failure_mode", "UNKNOWN")
    ui.log_history(
        f"Master failed to plan after {MAX_MASTER_RETRIES} retries (last mode={_final_mode}).",
        "error",
    )
    try:
        from system_log import log_system_event
        log_system_event(
            "pipeline.master_failed_to_plan", "error",
            f"Master v{next_v} failed to plan after {MAX_MASTER_RETRIES} retries (last mode={_final_mode})",
            {"next_v": next_v, "source_v": source_v,
             "last_failure_mode": _final_mode, "retries": MAX_MASTER_RETRIES},
        )
    except Exception:
        pass
    return None
