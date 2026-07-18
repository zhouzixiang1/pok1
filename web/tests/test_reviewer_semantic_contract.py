from __future__ import annotations

from copy import deepcopy
import json

import pytest


_EVIDENCE_SCOPE = (
    "reachable_symbol_delta_plus_typed_capability_only;"
    "not_full_counterfactual_or_strength_proof"
)


def _inputs(*, execution_mode: str):
    import tool_gates

    strategy = execution_mode == "strategy_implementation"
    binding = {
        "execution_mode": execution_mode,
        "selected_proposal_id": "proposal-a",
        "contract_digest": "a" * 64,
        "falsifier": {"test_name": "typed-capability-check"},
    }
    plan = {
        "analysis": "bounded plan",
        "proposal_binding": binding,
        "tasks": [{"worker_id": "W1", "target_files": ["policy.py"]}],
    }
    quality = {
        "all_passed": True,
        "critical_scenarios_passed": True,
        "selected_proposal_quality_ok": True,
        "selected_proposal_quality_evidence": {
            "required": True,
            "ok": True,
            "check_id": "typed-capability-check",
            "check_evidence_digest": "b" * 64,
            "proposal_contract_digest": "a" * 64,
            "evidence_scope": _EVIDENCE_SCOPE,
            "reachable_symbol_diff_required": strategy,
            "reachable_symbol_diff_ok": True,
            "changed_reachable_symbols": (
                ["policy.py:get_baseline_decision"] if strategy else []
            ),
            "reachable_symbol_diff_digest": "c" * 64 if strategy else "",
            "errors": [],
        },
    }
    semantic_contract = tool_gates._review_semantic_contract(plan, quality)
    return {
        "master_plan": plan,
        "source_v": 142,
        "next_v": 143,
        "strict_bootstrap": execution_mode == "fixed_blueprint_capability_audit",
        "invocation_id": (
            "1" * 32 if execution_mode == "fixed_blueprint_capability_audit" else ""
        ),
        "focus_areas": [],
        "review_semantic_contract": semantic_contract,
    }, quality


def test_fixed_blueprint_review_is_capability_audit_not_prose_implementation():
    import llm_query
    import tool_gates

    inputs, _quality = _inputs(
        execution_mode="fixed_blueprint_capability_audit"
    )
    rendered = llm_query.render_llm_prompt(
        "LEAD CODE REVIEWER",
        producer=tool_gates._render_reviewer_provider_prompt,
        renderer_inputs=inputs,
    )
    replay = json.loads(rendered.renderer_inputs_json)

    assert replay["review_semantic_contract"]["review_semantic_mode"] == (
        "fixed_blueprint_capability_audit_v1"
    )
    assert "FIXED BLUEPRINT CAPABILITY AUDIT" in rendered.text
    assert "helper names, field names" in rendered.text
    assert "Do not reject because the fixed blueprint uses different" in rendered.text
    assert "Do not claim proposal causality or poker strength" in rendered.text
    provenance = json.loads(
        rendered.dispatch_receipt.evidence.provenance_json
    )
    assert provenance["review_semantic_contract_digest"] == (
        replay["review_semantic_contract"]["contract_digest"]
    )


def test_strategy_review_still_requires_mechanism_and_reachable_chain():
    import llm_query
    import tool_gates

    fixed_inputs, _fixed_quality = _inputs(
        execution_mode="fixed_blueprint_capability_audit"
    )
    strategy_inputs, _quality = _inputs(execution_mode="strategy_implementation")
    fixed = llm_query.render_llm_prompt(
        "LEAD CODE REVIEWER",
        producer=tool_gates._render_reviewer_provider_prompt,
        renderer_inputs=fixed_inputs,
    )
    strategy = llm_query.render_llm_prompt(
        "LEAD CODE REVIEWER",
        producer=tool_gates._render_reviewer_provider_prompt,
        renderer_inputs=strategy_inputs,
    )

    assert "STRATEGY IMPLEMENTATION REVIEW" in strategy.text
    assert "materially changed reachable chain" in strategy.text
    assert fixed.dispatch_receipt.receipt_digest != (
        strategy.dispatch_receipt.receipt_digest
    )
    assert fixed_inputs["review_semantic_contract"]["contract_digest"] != (
        strategy_inputs["review_semantic_contract"]["contract_digest"]
    )


def test_review_semantic_contract_fails_closed_on_quality_or_mode_drift():
    import tool_gates

    inputs, quality = _inputs(
        execution_mode="fixed_blueprint_capability_audit"
    )
    plan = inputs["master_plan"]

    failed = deepcopy(quality)
    failed["selected_proposal_quality_evidence"]["ok"] = False
    with pytest.raises(ValueError, match="selected_capability_not_passed"):
        tool_gates._review_semantic_contract(plan, failed)

    missing_gate_flag = deepcopy(quality)
    missing_gate_flag.pop("selected_proposal_quality_ok")
    with pytest.raises(
        ValueError,
        match="selected_capability_gate_flag_not_passed",
    ):
        tool_gates._review_semantic_contract(plan, missing_gate_flag)

    forged = deepcopy(quality)
    forged["selected_proposal_quality_evidence"][
        "proposal_contract_digest"
    ] = "f" * 64
    with pytest.raises(ValueError, match="proposal_contract_digest_mismatch"):
        tool_gates._review_semantic_contract(plan, forged)

    unknown_plan = deepcopy(plan)
    unknown_plan["proposal_binding"]["execution_mode"] = "prose-decides-mode"
    with pytest.raises(ValueError, match="execution mode is not recognized"):
        tool_gates._review_semantic_contract(unknown_plan, quality)


def test_quality_checkpoint_projection_persists_exact_selected_evidence():
    import tool_gates

    _inputs_value, quality = _inputs(
        execution_mode="fixed_blueprint_capability_audit"
    )
    selected = quality["selected_proposal_quality_evidence"]
    result = {
        "selected_proposal_quality_evidence": selected,
        "selected_proposal_quality_ok": True,
        "national_architecture_transition": {"generic": "not-authority"},
        "national_capability_contract": {"generic": "not-authority"},
    }

    projection = tool_gates._quality_review_evidence_projection(result)
    assert projection == {
        "selected_proposal_quality_evidence": selected,
        "selected_proposal_quality_ok": True,
    }
    assert projection["selected_proposal_quality_evidence"] is not selected

    missing = deepcopy(result)
    missing.pop("selected_proposal_quality_evidence")
    with pytest.raises(ValueError, match="projection missing"):
        tool_gates._quality_review_evidence_projection(missing)


def test_reviewer_does_not_reconstruct_missing_evidence_from_generic_quality():
    import tool_gates

    inputs, quality = _inputs(
        execution_mode="fixed_blueprint_capability_audit"
    )
    missing = deepcopy(quality)
    missing.pop("selected_proposal_quality_evidence")
    missing["national_architecture_transition"] = {
        "selected_dynamic_checks": ["typed-capability-check"]
    }
    missing["national_capability_contract"] = {
        "dynamic_runtime_probe": {"ok": True}
    }

    with pytest.raises(ValueError, match="selected proposal evidence missing"):
        tool_gates._review_semantic_contract(inputs["master_plan"], missing)


def test_renderer_rejects_semantic_contract_digest_tampering():
    import tool_gates

    inputs, _quality = _inputs(
        execution_mode="fixed_blueprint_capability_audit"
    )
    inputs["review_semantic_contract"]["quality_gate_digest"] = "0" * 64
    with pytest.raises(ValueError, match="semantic contract digest mismatch"):
        tool_gates._render_reviewer_provider_prompt(inputs)
