from __future__ import annotations

from copy import deepcopy
import json

import pytest
from claude_agent_sdk import ResultMessage


def _checkpoint(*, stage="direction_audited", revision=10):
    return {
        "workflow_run_id": "strict-authority-test-run",
        "source_v": 142,
        "next_v": 155,
        "stage": stage,
        "checkpoint_revision": revision,
        "audit_context": {
            "protocol_bootstrap": {"receipt_digest": "a" * 64},
            "prepared_artifact_contract": {
                "contract_digest": "b" * 64,
                "prepared_artifact_hash": "c" * 64,
            },
        },
    }


@pytest.fixture
def authority(monkeypatch, tmp_path):
    import strict_authority_workflow as module
    from workflow_kernel import WorkflowStore

    store = WorkflowStore(tmp_path / "events.sqlite3")
    monkeypatch.setattr(module, "_store", lambda: store)
    monkeypatch.setattr(
        module,
        "_project_role_result",
        lambda _call, raw_output: json.loads(raw_output),
    )
    return module, store


def _call(
    module,
    checkpoint,
    slot,
    *,
    suffix="one",
    provider_uuid=None,
    accept=True,
    role_result=None,
):
    parse_contract = module.SLOT_PARSE_CONTRACTS[slot]
    role_result = (
        role_result
        if role_result is not None
        else {"slot": slot, "suffix": suffix}
    )
    raw_output = json.dumps(role_result, sort_keys=True)
    call = module.new_call(
        checkpoint,
        slot=slot,
        context_binding={"slot": slot, "suffix": suffix},
    )
    module.dispatch_call(
        call,
        full_prompt=f"final prompt {slot} {suffix}",
        tools=module.SLOT_TOOLS[slot],
        owner="pytest",
    )
    provider_result = ResultMessage(
        subtype="success",
        duration_ms=10,
        duration_api_ms=10,
        is_error=False,
        num_turns=1,
        session_id=(provider_uuid or f"provider-{slot}-{suffix}"),
        total_cost_usd=0.01,
        usage={"input_tokens": 1, "output_tokens": 1},
        result=raw_output,
    )
    module._observe_provider_result(
        provider_result,
        invocation_id=call["invocation_id"],
        effect_id=call["effect_id"],
    )
    module.complete_provider_call(
        call,
        raw_output=raw_output,
        provider_results=[provider_result],
    )
    receipt = None
    if accept:
        receipt = module.accept_role_result(
            call,
            role_result=role_result,
            parse_contract=parse_contract,
        )
    return call, role_result, receipt


def test_provider_result_without_schema_acceptance_is_not_authority(authority):
    module, _store = authority
    checkpoint = _checkpoint()
    _call(module, checkpoint, "proposal:mechanism", accept=False)

    _refs, errors = module.validate_receipts(
        checkpoint,
        required_slots=("proposal:mechanism",),
        require_no_other_accepted=True,
    )
    assert "strict_authority_proposal:mechanism_accepted_count:0" in errors


def test_completed_provider_before_accept_is_replayed_without_new_effect(authority):
    module, store = authority
    checkpoint = _checkpoint()
    original, role_result, _receipt = _call(
        module,
        checkpoint,
        "proposal:mechanism",
        accept=False,
    )

    recovered = module.new_call(
        checkpoint,
        slot="proposal:mechanism",
        context_binding={"slot": "proposal:mechanism", "suffix": "one"},
    )
    assert recovered["replay_provider"] is True
    assert recovered["effect_id"] == original["effect_id"]
    assert recovered["invocation_id"] == original["invocation_id"]
    module.dispatch_call(
        recovered,
        full_prompt="a restart must not replace the persisted provider prompt",
        tools=module.SLOT_TOOLS["proposal:mechanism"],
        owner="pytest-recovery",
    )
    receipt = module.accept_role_result(
        recovered,
        role_result=role_result,
        parse_contract=module.SLOT_PARSE_CONTRACTS["proposal:mechanism"],
    )
    assert receipt["effect_id"] == original["effect_id"]
    observed = [
        event for event in store.events(original["run_id"])
        if event.event_type == "StrictProviderResultObserved"
    ]
    assert len(observed) == 1


def test_projection_rejection_is_not_replayed_as_poison(authority, monkeypatch):
    module, store = authority
    checkpoint = _checkpoint()

    def reject(_call, _raw_output):
        raise module.StrictAuthorityError("schema-invalid")

    monkeypatch.setattr(module, "_project_role_result", reject)
    call = module.new_call(
        checkpoint,
        slot="proposal:mechanism",
        context_binding={"slot": "proposal:mechanism", "suffix": "poison"},
    )
    module.dispatch_call(
        call,
        full_prompt="poison prompt",
        tools=module.SLOT_TOOLS["proposal:mechanism"],
        owner="pytest",
    )
    result = ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="poison-session",
        total_cost_usd=0.01,
        usage={},
        result="not valid for this role",
    )
    module._observe_provider_result(
        result,
        invocation_id=call["invocation_id"],
        effect_id=call["effect_id"],
    )
    module.complete_provider_call(
        call,
        raw_output="not valid for this role",
        provider_results=[result],
    )
    assert any(
        event.event_type == module.REJECTED_EVENT
        for event in store.events(call["run_id"])
    )

    fresh = module.new_call(
        checkpoint,
        slot="proposal:mechanism",
        context_binding={"slot": "proposal:mechanism", "suffix": "poison"},
    )
    assert fresh.get("replay_provider") is not True
    assert fresh["invocation_id"] != call["invocation_id"]


def test_terminal_sdk_output_and_projected_role_are_both_enforced(authority):
    module, _store = authority
    checkpoint = _checkpoint()
    call = module.new_call(
        checkpoint,
        slot="proposal:mechanism",
        context_binding={"slot": "proposal:mechanism", "suffix": "binding"},
    )
    module.dispatch_call(
        call,
        full_prompt="binding prompt",
        tools=module.SLOT_TOOLS["proposal:mechanism"],
        owner="pytest",
    )
    result = ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="binding-session",
        total_cost_usd=0.01,
        usage={},
        result='{"provider":true}',
    )
    module._observe_provider_result(
        result,
        invocation_id=call["invocation_id"],
        effect_id=call["effect_id"],
    )
    with pytest.raises(module.StrictAuthorityError, match="raw_result_mismatch"):
        module.complete_provider_call(
            call,
            raw_output='{"caller":true}',
            provider_results=[result],
        )

    call, role_result, _receipt = _call(
        module,
        checkpoint,
        "proposal:counterfactual",
        accept=False,
    )
    with pytest.raises(module.StrictAuthorityError, match="projection_mismatch"):
        module.accept_role_result(
            call,
            role_result={**role_result, "injected": True},
            parse_contract=module.SLOT_PARSE_CONTRACTS["proposal:counterfactual"],
        )


def test_terminal_structured_output_is_bound_to_raw_and_projection(authority):
    module, _store = authority
    checkpoint = _checkpoint()
    role_result = {"slot": "proposal:mechanism", "structured": True}
    raw_output = json.dumps(role_result, sort_keys=True)
    call = module.new_call(
        checkpoint,
        slot="proposal:mechanism",
        context_binding={"slot": "proposal:mechanism", "suffix": "structured"},
    )
    module.dispatch_call(
        call,
        full_prompt="structured binding prompt",
        tools=module.SLOT_TOOLS["proposal:mechanism"],
        owner="pytest",
    )
    result = ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="structured-session",
        total_cost_usd=0.01,
        usage={},
        structured_output=role_result,
    )
    module._observe_provider_result(
        result,
        invocation_id=call["invocation_id"],
        effect_id=call["effect_id"],
    )
    completed = module.complete_provider_call(
        call,
        raw_output=raw_output,
        provider_results=[result],
    )
    assert completed["mode"] == "terminal_structured_output"
    receipt = module.accept_role_result(
        call,
        role_result=role_result,
        parse_contract=module.SLOT_PARSE_CONTRACTS["proposal:mechanism"],
    )
    assert receipt["role_result_digest"] == module.content_digest(role_result)


def test_exact_eight_slots_are_distinct_and_stage_ordered(authority):
    module, _store = authority
    checkpoints = {
        **{slot: _checkpoint(revision=10) for slot in module.MASTER_SLOTS},
        "review": _checkpoint(stage="quality_passed", revision=20),
        "critic": _checkpoint(stage="reviewed", revision=30),
    }
    expected_results = {}
    expected_contexts = {}
    receipts = []
    for slot in module.ALL_SLOTS:
        _call_state, result, receipt = _call(module, checkpoints[slot], slot)
        expected_results[slot] = result
        expected_contexts[slot] = {"slot": slot, "suffix": "one"}
        receipts.append(receipt)

    refs, errors = module.validate_receipts(
        checkpoints["critic"],
        required_slots=module.ALL_SLOTS,
        expected_role_results=expected_results,
        expected_context_bindings=expected_contexts,
        require_no_other_accepted=True,
    )
    assert errors == []
    assert set(refs) == set(module.ALL_SLOTS)
    assert len({item["effect_id"] for item in receipts}) == 8
    assert len({item["invocation_id"] for item in receipts}) == 8


def _master_plan_for_role_results():
    proposals = [
        {"direction": direction, "proposal_id": f"proposal-{index}", "claim": direction}
        for index, direction in enumerate(
            ("mechanism", "counterfactual", "compute_memory"),
            start=1,
        )
    ]
    reviews = [
        {
            "critic_id": critic_id,
            "ranking": [item["proposal_id"] for item in proposals],
            "reject": [],
            "ballots": [{"proposal_id": item["proposal_id"]} for item in proposals],
            "invocation_evidence": {"receipt": critic_id},
        }
        for critic_id in ("falsification", "scope")
    ]
    return {
        "analysis": "the schema-valid final Master plan",
        "tasks": [{"worker_id": 1, "worker_prompt": "bounded migration"}],
        "proposal_ensemble": {
            "ordered_proposals": proposals,
            "critic_reviews": reviews,
        },
        "architecture_policy": {"policy_digest": "d" * 64},
        "runtime_contract_ledger": {"ledger_digest": "e" * 64},
    }


@pytest.mark.parametrize(
    ("tamper", "expected_slot"),
    [
        ("proposal", "proposal:mechanism"),
        ("ballot", "ballot:falsification"),
    ],
)
def test_expected_master_role_results_reject_each_payload_tamper(
    authority,
    tamper,
    expected_slot,
):
    module, _store = authority
    checkpoint = _checkpoint()
    plan = _master_plan_for_role_results()
    expected = module.expected_master_role_results(plan)
    accepted_final = deepcopy(plan)
    accepted_final.pop("architecture_policy")
    accepted_final.pop("runtime_contract_ledger")
    for slot in module.MASTER_SLOTS:
        _call(
            module,
            checkpoint,
            slot,
            role_result=(
                accepted_final if slot == "master:final" else expected[slot]
            ),
        )
    _refs, errors = module.validate_receipts(
        checkpoint,
        required_slots=module.MASTER_SLOTS,
        expected_role_results=expected,
        require_no_other_accepted=True,
    )
    assert errors == []

    tampered_plan = deepcopy(plan)
    if tamper == "proposal":
        tampered_plan["proposal_ensemble"]["ordered_proposals"][0][
            "claim"
        ] = "tampered proposal claim"
    elif tamper == "ballot":
        tampered_plan["proposal_ensemble"]["critic_reviews"][0][
            "ranking"
        ].reverse()
    tampered_expected = module.expected_master_role_results(tampered_plan)
    _refs, errors = module.validate_receipts(
        checkpoint,
        required_slots=module.MASTER_SLOTS,
        expected_role_results=tampered_expected,
        require_no_other_accepted=True,
    )
    assert f"strict_authority_{expected_slot}_role_result_mismatch" in errors


def test_master_role_results_never_guess_a_compiled_final_payload(authority):
    module, _store = authority
    plan = _master_plan_for_role_results()
    plan["plan_compiler"] = {"compiled": True}
    results = module.expected_master_role_results(plan)
    assert set(results) == set(module.MASTER_SLOTS[:5])
    assert "master:final" not in results


def test_wrong_stage_and_parse_contract_fail_closed(authority):
    module, _store = authority
    with pytest.raises(module.StrictAuthorityError, match="checkpoint_stage"):
        module.new_call(
            _checkpoint(stage="quality_passed"),
            slot="master:final",
            context_binding={"phase": "master_final"},
        )

    call, _result, _receipt = _call(
        module,
        _checkpoint(),
        "proposal:mechanism",
        accept=False,
    )
    with pytest.raises(module.StrictAuthorityError, match="parse_contract"):
        module.accept_role_result(
            call,
            role_result={"valid": True},
            parse_contract="wrong-schema",
        )


def test_master_revision_drift_and_reverse_gate_revision_are_rejected(authority):
    module, _store = authority
    for index, slot in enumerate(module.MASTER_SLOTS):
        _call(module, _checkpoint(revision=10 + (index == 5)), slot)
    _call(module, _checkpoint(stage="quality_passed", revision=9), "review")
    _call(module, _checkpoint(stage="reviewed", revision=8), "critic")

    _refs, errors = module.validate_receipts(
        _checkpoint(stage="reviewed", revision=99),
        required_slots=module.ALL_SLOTS,
        require_no_other_accepted=True,
    )
    assert "strict_authority_master_checkpoint_revision_drift" in errors
    assert "strict_authority_review_revision_precedes_master" in errors
    assert "strict_authority_critic_revision_precedes_review" in errors


def test_provider_event_reuse_across_slots_is_rejected(authority):
    module, _store = authority
    checkpoint = _checkpoint()
    shared_provider_payload = {"same": "terminal-provider-event"}
    _call(
        module,
        checkpoint,
        "proposal:mechanism",
        suffix="first",
        provider_uuid="same-provider-event",
        role_result=shared_provider_payload,
    )
    _call(
        module,
        checkpoint,
        "proposal:counterfactual",
        suffix="second",
        provider_uuid="same-provider-event",
        role_result=shared_provider_payload,
    )

    _refs, errors = module.validate_receipts(
        checkpoint,
        required_slots=("proposal:mechanism", "proposal:counterfactual"),
        require_no_other_accepted=True,
    )
    assert "strict_authority_provider_event_reused" in errors


def test_wrong_context_binding_is_rejected(authority):
    module, _store = authority
    checkpoint = _checkpoint()
    _call(module, checkpoint, "proposal:mechanism")
    _refs, errors = module.validate_receipts(
        checkpoint,
        required_slots=("proposal:mechanism",),
        expected_context_bindings={
            "proposal:mechanism": {"slot": "wrong", "suffix": "one"}
        },
    )
    assert "strict_authority_proposal:mechanism_context_binding_mismatch" in errors


def test_master_summary_rejects_missing_separate_final_master_receipt(authority):
    module, _store = authority
    checkpoint = _checkpoint()
    for slot in module.MASTER_SLOTS[:-1]:
        _call(module, checkpoint, slot)

    _refs, errors = module.validate_receipts(
        checkpoint,
        required_slots=module.MASTER_SLOTS,
        require_no_other_accepted=True,
    )
    assert "strict_authority_master:final_accepted_count:0" in errors


@pytest.mark.parametrize(
    ("slot", "stage", "revision"),
    [
        ("proposal:mechanism", "direction_audited", 10),
        ("ballot:scope", "direction_audited", 10),
        ("master:final", "direction_audited", 10),
        ("review", "quality_passed", 20),
        ("critic", "reviewed", 30),
    ],
)
def test_crash_after_accept_reuses_slot_without_provider_or_duplicate(
    authority,
    slot,
    stage,
    revision,
):
    module, _store = authority
    checkpoint = _checkpoint(stage=stage, revision=revision)
    original, role_result, receipt = _call(module, checkpoint, slot)

    recovered = module.new_call(
        checkpoint,
        slot=slot,
        context_binding={"slot": slot, "suffix": "one"},
    )
    assert recovered["replay_provider"] is True
    assert recovered["invocation_id"] == original["invocation_id"]
    assert recovered["effect_id"] == original["effect_id"]
    module.dispatch_call(
        recovered,
        full_prompt=f"final prompt {slot} one",
        tools=module.SLOT_TOOLS[slot],
        owner="pytest-recovery",
    )
    replayed = module.accept_role_result(
        recovered,
        role_result=role_result,
        parse_contract=module.SLOT_PARSE_CONTRACTS[slot],
    )
    assert replayed == receipt

    refs, errors = module.validate_receipts(
        checkpoint,
        required_slots=(slot,),
        expected_role_results={slot: role_result},
        require_no_other_accepted=True,
    )
    assert errors == []
    assert refs[slot]["receipt_digest"] == receipt["receipt_digest"]


@pytest.mark.parametrize(
    ("slot", "original_role", "restart_role"),
    [
        (
            "proposal:mechanism",
            "MASTER PROPOSAL mechanism SCHEMA RETRY",
            "MASTER PROPOSAL mechanism",
        ),
        ("master:final", "MASTER (Try 2)", "MASTER (Try 1)"),
    ],
)
def test_retry_attempt_receipt_replays_from_initial_attempt_prompt(
    authority,
    slot,
    original_role,
    restart_role,
):
    module, _store = authority
    checkpoint = _checkpoint()
    context = {"slot": slot, "attempt_binding": "stable"}
    call = module.new_call(
        checkpoint,
        slot=slot,
        role=original_role,
        context_binding=context,
    )
    module.dispatch_call(
        call,
        full_prompt="schema repair / Try 2 prompt",
        tools=module.SLOT_TOOLS[slot],
        owner="pytest-original-attempt",
        actual_role=original_role,
    )
    role_result = {"valid": True, "slot": slot}
    raw_output = json.dumps(role_result, sort_keys=True)
    result = ResultMessage(
        subtype="success",
        duration_ms=10,
        duration_api_ms=10,
        is_error=False,
        num_turns=1,
        session_id=f"retry-session-{slot}",
        total_cost_usd=0.01,
        usage={},
        result=raw_output,
    )
    module._observe_provider_result(
        result,
        invocation_id=call["invocation_id"],
        effect_id=call["effect_id"],
    )
    module.complete_provider_call(
        call,
        raw_output=raw_output,
        provider_results=[result],
    )
    first_receipt = module.accept_role_result(
        call,
        role_result=role_result,
        parse_contract=module.SLOT_PARSE_CONTRACTS[slot],
    )

    recovered = module.new_call(
        checkpoint,
        slot=slot,
        role=restart_role,
        context_binding=context,
    )
    module.dispatch_call(
        recovered,
        full_prompt="fresh initial-attempt prompt is intentionally different",
        tools=module.SLOT_TOOLS[slot],
        owner="pytest-restart",
        actual_role=restart_role,
    )
    assert recovered["replay_provider"] is True
    assert module.accept_role_result(
        recovered,
        role_result=role_result,
        parse_contract=module.SLOT_PARSE_CONTRACTS[slot],
    ) == first_receipt
    _refs, errors = module.validate_receipts(
        checkpoint,
        required_slots=(slot,),
        require_no_other_accepted=True,
    )
    assert errors == []
