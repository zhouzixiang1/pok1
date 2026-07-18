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
    import evolution_infra
    from workflow_kernel import WorkflowStore

    results_dir = tmp_path / "results"
    store = WorkflowStore(results_dir / "workflow" / "events.sqlite3")
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results_dir)
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


def _strict_log(module, store, call, basename):
    return module.strict_invocation_log_path(
        call,
        logs_dir=(
            store.path.parent.parent
            / f"v{call['generation_binding']['next_v']}"
            / "logs"
        ),
        basename=basename,
    )


def test_final_master_dispatch_rejects_filesystem_tool(authority):
    module, store = authority
    checkpoint = _checkpoint()
    call = module.new_call(
        checkpoint,
        slot="master:final",
        context_binding={"slot": "master:final", "suffix": "zero-tool"},
    )

    with pytest.raises(
        module.StrictAuthorityError,
        match="strict_authority_tools_mismatch:master:final",
    ):
        module.dispatch_call(
            call,
            full_prompt="final master must consume only frozen context",
            tools=["Read"],
            owner="pytest",
        )

    assert store.instance(call["run_id"]) == {}


def test_recover_accepted_final_master_skips_duplicate_scout_rebuild(authority):
    module, store = authority
    checkpoint = _checkpoint()
    policy = {"policy_digest": "d" * 64, "focus": "action_profile"}
    packet = {
        "context_digest": "e" * 64,
        "source_code_digest": "f" * 64,
        "ordered_proposals": [],
    }
    role_result = {
        "analysis": "sealed final master plan",
        "proposal_ensemble": packet,
        "tasks": [],
    }
    call = module.new_call(
        checkpoint,
        slot="master:final",
        context_binding=module.final_master_call_context(packet, policy),
    )
    module.dispatch_call(
        call,
        full_prompt="sealed final master prompt",
        tools=[],
        owner="pytest",
    )
    result = ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="sealed-final",
        total_cost_usd=0.01,
        usage={},
        result=json.dumps(role_result, sort_keys=True),
    )
    module._observe_provider_result(
        result,
        invocation_id=call["invocation_id"],
        effect_id=call["effect_id"],
    )
    module.complete_provider_call(
        call,
        raw_output=result.result,
        provider_results=[result],
    )
    module.accept_role_result(
        call,
        role_result=role_result,
        parse_contract="master-plan-schema-v1",
    )

    assert module.recover_accepted_master_final_result(
        checkpoint,
        architecture_policy=policy,
    ) == role_result
    assert len([
        event for event in store.events(call["run_id"])
        if event.event_type == "EffectRequested"
    ]) == 1
    with pytest.raises(
        module.StrictAuthorityError,
        match="strict_authority_phase_slot_context_drift:master:master:final",
    ):
        module.recover_accepted_master_final_result(
            checkpoint,
            architecture_policy={"policy_digest": "changed"},
        )

    # A same-phase revision may legitimately replay a sealed effect, but a
    # later stage is a new state-machine boundary, never a duplicate entry.
    with pytest.raises(
        module.StrictAuthorityError,
        match="strict_authority_checkpoint_stage_invalid:master:final:master_plan_ready",
    ):
        module.recover_accepted_master_final_result(
            {**checkpoint, "checkpoint_revision": 11, "stage": "master_plan_ready"},
            architecture_policy=policy,
        )
    assert len([
        event for event in store.events(call["run_id"])
        if event.event_type == "EffectRequested"
    ]) == 1


def test_real_strict_proposal_projection_enforces_frozen_allowed_primary(
    monkeypatch,
    tmp_path,
):
    """The persisted Scout context, not renderer prose, owns the axis gate."""

    import agent_master
    import evolution_infra
    import strict_authority_workflow as module
    from workflow_kernel import WorkflowStore

    results_dir = tmp_path / "results"
    store = WorkflowStore(results_dir / "workflow" / "events.sqlite3")
    candidate_dir = tmp_path / "national_v155"
    candidate_dir.mkdir()
    (candidate_dir / "policy.py").write_text(
        "def get_baseline_decision(context):\n"
        "    return _choose_intent(context)\n\n"
        "def _choose_intent(context):\n"
        "    return {'kind': 'pass'}\n",
        encoding="utf-8",
    )
    (candidate_dir / ".protocol_bootstrap_no_strength_evidence").mkdir()
    source_graph, source_digest = agent_master._source_symbol_graph(candidate_dir)
    assert source_graph
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(evolution_infra, "get_bot_dir", lambda _v: candidate_dir)
    monkeypatch.setattr(module, "_store", lambda: store)

    architecture_policy = {
        "plan_required_floor_checks": ["incremental_opponent_model"],
        "selected_focus": {"required_checks": ["incremental_opponent_model"]},
    }
    allowed = agent_master._architecture_proposal_primaries(architecture_policy)
    assert allowed == ("action_profile",)
    context = module.proposal_call_context(
        context_digest="a" * 64,
        source_code_digest=source_digest,
        direction="mechanism",
        allowed_primaries=allowed,
    )
    assert context["allowed_primaries"] == ["action_profile"]
    call = module.new_call(
        _checkpoint(),
        slot="proposal:mechanism",
        context_binding=context,
    )

    def proposal_for(*, primary, test_name, target):
        leaf = f"{target}.fold_to_raise"
        return {
            "targeted_failure": (
                f"The bounded {primary} consumer misses one reachable action profile."
            ),
            "structural_change": (
                f"Route only {leaf} through the bounded live decision consumer."
            ),
            "counterfactual": (
                f"Hold cards, legality, deadline, and all roots except {target} fixed."
            ),
            "measurement": (
                "target=fixed_blueprint_control; "
                "primary=typed_falsifier_and_official_5_plus_3; "
                "expected_delta=not_applicable; samples=official_5_plus_3; "
                "uncertainty=no_strength_claim; secondary=none"
            ),
            "why_not_threshold_tuning": (
                "This changes a reachable state consumer rather than one numeric threshold."
            ),
            "mechanism_target": target,
            "expected_diff": (
                f"The paired typed intent changes only when {leaf} changes."
            ),
            "target_files": ["policy.py"],
            "source_symbols": [
                "policy.py:get_baseline_decision",
                "policy.py:_choose_intent",
            ],
            "reachable_chain": [
                "policy.py:get_baseline_decision",
                "policy.py:_choose_intent",
            ],
            "falsifier": {
                "test_name": test_name,
                "state_learning_primary": primary,
                "intervention_target": target,
                "control": f"Hold {leaf} at its bounded paired-state prior.",
                "intervention": f"Change only {leaf} in the paired decision context.",
                "expected_observation": (
                    "The typed intent changes only under that owner-qualified intervention."
                ),
            },
            "evidence_refs": [
                "source:policy.py:get_baseline_decision",
                "source:policy.py:_choose_intent",
            ],
            "risks": "Sparse evidence remains bounded by the system fallback and probe.",
        }

    accepted = module._project_role_result(
        call,
        json.dumps(
            proposal_for(
                primary="action_profile",
                test_name="incremental_opponent_model",
                target="opponent.rates",
            )
        ),
    )
    assert accepted["falsifier"]["state_learning_primary"] == "action_profile"

    with pytest.raises(
        module.StrictAuthorityError,
        match="proposal_falsifier_primary_not_permitted",
    ):
        module._project_role_result(
            call,
            json.dumps(
                proposal_for(
                    primary="terminal_response",
                    test_name="terminal_response_adaptation",
                    target="opponent.terminal_response",
                )
            ),
        )


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


@pytest.mark.parametrize("approved", [False, True])
def test_renderer_source_drift_recovery_is_rejection_only(
    authority,
    monkeypatch,
    tmp_path,
    approved,
):
    module, _store = authority
    checkpoint = _checkpoint(stage="quality_passed", revision=10)
    candidate = tmp_path / "national_v155"
    candidate.mkdir()
    (candidate / "policy.py").write_text("VALUE = 1\n", encoding="utf-8")
    semantic_inputs = {
        "master_plan": {},
        "source_v": 142,
        "next_v": 155,
        "strict_bootstrap": True,
        "focus_areas": [],
    }
    old_context = {
        "phase": "review",
        "candidate_artifact_hash": "a" * 64,
        "quality_gate_digest": "b" * 64,
        "master_receipt_digest": "c" * 64,
        "master_plan_digest": "d" * 64,
        "renderer_semantics": {
            "schema_version": 1,
            "role": "LEAD CODE REVIEWER",
            "semantic_inputs": semantic_inputs,
            "semantic_inputs_digest": module.content_digest(semantic_inputs),
            "renderer_static_identity": {"producer_file_sha256": "old"},
        },
    }
    current_context = deepcopy(old_context)
    current_context["renderer_semantics"]["renderer_static_identity"] = {
        "producer_file_sha256": "new"
    }
    role_result = {
        "approved": approved,
        "quality_score": 3 if not approved else 9,
        "feedback": "terminal reject" if not approved else "approved",
        "change_summary": "reviewed",
        "risk_areas": [],
    }
    call = module.new_call(
        checkpoint,
        slot="review",
        context_binding=old_context,
    )
    module.dispatch_call(
        call,
        full_prompt="old renderer prompt",
        tools=["Read"],
        owner="pytest",
    )
    provider_result = ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="renderer-drift-review",
        total_cost_usd=0.01,
        usage={},
        result=json.dumps(role_result, sort_keys=True),
    )
    module._observe_provider_result(
        provider_result,
        invocation_id=call["invocation_id"],
        effect_id=call["effect_id"],
    )
    module.complete_provider_call(
        call,
        raw_output=provider_result.result,
        provider_results=[provider_result],
    )
    monkeypatch.setattr(
        module,
        "gate_call_context",
        lambda *_args, **_kwargs: current_context,
    )

    recovered = module.recover_terminal_gate_rejection_call(
        checkpoint,
        gate_name="review",
        candidate_dir=candidate,
    )
    if approved:
        assert recovered is None
    else:
        assert recovered["effect_id"] == call["effect_id"]
        assert recovered["terminal_reconciliation"] is True
        assert recovered["projected_role_result"]["approved"] is False


def test_generation_abandon_fences_strict_child_journal(authority):
    module, store = authority
    checkpoint = _checkpoint()
    _call(module, checkpoint, "proposal:mechanism", accept=False)
    run_id = module.authority_run_id(checkpoint["workflow_run_id"])
    before = store.instance(run_id)
    assert before["status"] == "running"

    first = module.abandon_authority(checkpoint, reason="abandon_generation")
    second = module.abandon_authority(checkpoint, reason="abandon_generation")

    assert first["abandoned"] is True
    assert first["fence_epoch"] == 1
    assert second == first
    assert store.instance(run_id)["status"] == "abandoned"
    events = [
        event
        for event in store.events(run_id)
        if event.event_type == "StrictAuthorityAbandoned"
    ]
    assert len(events) == 1
    with pytest.raises(
        module.StrictAuthorityError,
        match="strict_authority_phase_journal_abandoned:master",
    ):
        module.new_call(
            checkpoint,
            slot="proposal:mechanism",
            context_binding={"slot": "proposal:mechanism", "suffix": "one"},
        )


def test_generation_abandon_tombstone_blocks_predispatch_descriptor(authority):
    module, store = authority
    checkpoint = _checkpoint()
    call = module.new_call(
        checkpoint,
        slot="proposal:mechanism",
        context_binding={"slot": "proposal:mechanism", "suffix": "one"},
    )
    run_id = module.authority_run_id(checkpoint["workflow_run_id"])
    assert store.instance(run_id) == {}

    first = module.abandon_authority(checkpoint, reason="abandon_generation")
    second = module.abandon_authority(checkpoint, reason="abandon_generation")

    assert first["present"] is True
    assert first["abandoned"] is True
    assert first["fence_epoch"] == 1
    assert second == first
    assert store.instance(run_id)["status"] == "abandoned"
    assert [
        event.event_type
        for event in store.events(run_id)
        if event.event_type == "StrictAuthorityAbandoned"
    ] == ["StrictAuthorityAbandoned"]
    with pytest.raises(
        module.StrictAuthorityError,
        match="strict_authority_dispatch_journal_abandoned",
    ):
        module.dispatch_call(
            call,
            full_prompt="stale provider prompt",
            tools=module.SLOT_TOOLS["proposal:mechanism"],
            owner="stale-dispatch",
        )
    assert store.instance(run_id)["stream_version"] == first["stream_version"]


def test_generation_abandon_recovers_preterminal_tombstone(authority):
    module, store = authority
    checkpoint = _checkpoint()
    run_id = module.authority_run_id(checkpoint["workflow_run_id"])
    # Model a process death after the fail-closed tombstone transaction but
    # before the terminal event/fence transaction.
    store.ensure_instance(
        run_id,
        definition_version=module.DEFINITION_VERSION,
        status="abandoned",
    )
    assert store.instance(run_id)["fence_epoch"] == 0
    assert store.events(run_id) == []

    recovered = module.abandon_authority(
        checkpoint,
        reason="abandon_generation",
    )
    repeated = module.abandon_authority(
        checkpoint,
        reason="abandon_generation",
    )

    assert recovered["abandoned"] is True
    assert recovered["fence_epoch"] == 1
    assert repeated == recovered
    assert [event.event_type for event in store.events(run_id)] == [
        "StrictAuthorityAbandoned"
    ]


def test_generation_abandon_blocks_accepted_provider_replay_dispatch(authority):
    module, store = authority
    checkpoint = _checkpoint()
    original, _role_result, _receipt = _call(
        module,
        checkpoint,
        "proposal:mechanism",
    )
    replay = module.new_call(
        checkpoint,
        slot="proposal:mechanism",
        context_binding={"slot": "proposal:mechanism", "suffix": "one"},
    )
    assert replay["replay_provider"] is True
    assert replay["effect_id"] == original["effect_id"]

    module.abandon_authority(checkpoint, reason="abandon_generation")

    with pytest.raises(
        module.StrictAuthorityError,
        match="strict_authority_dispatch_journal_abandoned",
    ):
        module.dispatch_call(
            replay,
            full_prompt="stale replay prompt",
            tools=module.SLOT_TOOLS["proposal:mechanism"],
            owner="stale-replay",
        )
    assert "replay_request_prompt_digest" not in replay
    assert store.instance(replay["run_id"])["status"] == "abandoned"


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

    advanced_checkpoint = {
        **checkpoint,
        "checkpoint_revision": checkpoint["checkpoint_revision"] + 1,
        "audit_attempt": 1,
    }
    fresh = module.new_call(
        advanced_checkpoint,
        slot="proposal:mechanism",
        context_binding={"slot": "proposal:mechanism", "suffix": "poison"},
    )
    assert fresh.get("replay_provider") is not True
    assert fresh["invocation_id"] != call["invocation_id"]
    assert fresh["checkpoint_revision"] == checkpoint["checkpoint_revision"]
    assert fresh["schema_retry_required"] is True
    assert fresh["schema_attempt"] == 2
    assert "single permitted" in module.schema_retry_prompt(fresh)

    module.dispatch_call(
        fresh,
        full_prompt="one bounded schema repair",
        tools=module.SLOT_TOOLS["proposal:mechanism"],
        owner="pytest",
    )
    second = ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="poison-session-two",
        total_cost_usd=0.01,
        usage={},
        result="still invalid for this role",
    )
    module._observe_provider_result(
        second,
        invocation_id=fresh["invocation_id"],
        effect_id=fresh["effect_id"],
    )
    module.complete_provider_call(
        fresh,
        raw_output="still invalid for this role",
        provider_results=[second],
    )
    with pytest.raises(module.StrictAuthorityError, match="schema_retry_exhausted"):
        module.new_call(
            advanced_checkpoint,
            slot="proposal:mechanism",
            context_binding={"slot": "proposal:mechanism", "suffix": "poison"},
        )


def test_proposal_projection_error_is_durable_and_repairs_once(
    monkeypatch,
    tmp_path,
):
    import agent_master
    import evolution_infra
    import strict_authority_workflow as module
    from workflow_kernel import WorkflowStore

    results_dir = tmp_path / "results"
    store = WorkflowStore(results_dir / "workflow" / "events.sqlite3")
    candidate_dir = tmp_path / "national_v155"
    candidate_dir.mkdir()
    (candidate_dir / "policy.py").write_text(
        "def get_baseline_decision(context):\n"
        "    return _choose_intent(context)\n\n"
        "def _choose_intent(context):\n"
        "    return {'kind': 'pass'}\n",
        encoding="utf-8",
    )
    _source_graph, source_digest = agent_master._source_symbol_graph(candidate_dir)
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(evolution_infra, "get_bot_dir", lambda _v: candidate_dir)
    monkeypatch.setattr(module, "_store", lambda: store)

    invalid_proposal = json.dumps({
        "targeted_failure": "The current reachable decision path repeats one bounded failure.",
        "structural_change": "Replace that decision mechanism with one deadline-bounded state path.",
        "counterfactual": "Hold cards and legality fixed while changing only this mechanism.",
        "measurement_plan": "This wrong field name must not satisfy measurement.",
        "why_not_threshold_tuning": "This changes state flow and its consumer, not one threshold.",
        "mechanism_target": "deadline",
        "expected_diff": "The existing entrypoint still reaches the changed intent consumer before the deadline.",
        "target_files": ["policy.py"],
        "source_symbols": [
            "policy.py:get_baseline_decision",
            "policy.py:_choose_intent",
        ],
        "reachable_chain": [
            "policy.py:get_baseline_decision",
            "policy.py:_choose_intent",
        ],
            "falsifier": {
                "test_name": "fast_policy_baseline",
                "state_learning_primary": "sample_counted_candidate_batch",
                "intervention_target": "deadline",
                "control": "Run the frozen parent with sample_count=1 before the deadline on the same canonical state and seed.",
                "intervention": "Run only the proposed mechanism with a changed deadline on that identical state.",
            "expected_observation": "The intervention changes while the paired control stays fixed.",
        },
        "evidence_refs": [
            "source:policy.py:get_baseline_decision",
            "source:policy.py:_choose_intent",
        ],
        "risks": "Sparse evidence can overfit, so the mechanism remains bounded.",
    })
    checkpoint = _checkpoint()
    context = {"source_code_digest": source_digest}

    def complete_invalid(call, session_id):
        module.dispatch_call(
            call,
            full_prompt=f"invalid proposal {session_id}",
            tools=module.SLOT_TOOLS["proposal:mechanism"],
            owner="pytest",
        )
        result = ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id=session_id,
            total_cost_usd=0.01,
            usage={},
            result=invalid_proposal,
        )
        module._observe_provider_result(
            result,
            invocation_id=call["invocation_id"],
            effect_id=call["effect_id"],
        )
        module.complete_provider_call(
            call,
            raw_output=invalid_proposal,
            provider_results=[result],
        )

    first = module.new_call(
        checkpoint,
        slot="proposal:mechanism",
        context_binding=context,
    )
    complete_invalid(first, "granular-projection-one")
    expected_errors = [
        "strict_authority_role_projection_rejected:proposal:mechanism",
        "strict_authority_proposal_projection:"
        "proposal_required_text_invalid:measurement",
        "strict_authority_proposal_projection:"
        "proposal_measurement_contract_invalid",
    ]
    rejections = [
        event
        for event in store.events(first["run_id"])
        if event.event_type == module.REJECTED_EVENT
    ]
    assert len(rejections) == 1
    assert rejections[0].payload["projection_errors"] == expected_errors

    repair = module.new_call(
        checkpoint,
        slot="proposal:mechanism",
        context_binding=context,
    )
    assert repair["schema_retry_required"] is True
    assert repair["schema_attempt"] == module.MAX_SCHEMA_ATTEMPTS_PER_SLOT == 2
    assert repair["prior_schema_rejection"]["projection_errors"] == expected_errors
    repair_prompt = module.schema_retry_prompt(repair)
    assert "single permitted schema-only repair attempt" in repair_prompt
    assert expected_errors[1] in repair_prompt

    complete_invalid(repair, "granular-projection-two")
    rejections = [
        event
        for event in store.events(first["run_id"])
        if event.event_type == module.REJECTED_EVENT
    ]
    assert len(rejections) == 2
    assert all(
        event.payload["projection_errors"] == expected_errors
        for event in rejections
    )
    with pytest.raises(module.StrictAuthorityError, match="schema_retry_exhausted"):
        module.new_call(
            checkpoint,
            slot="proposal:mechanism",
            context_binding=context,
        )


def test_dead_provider_owner_is_fenced_before_fresh_dispatch(authority, monkeypatch):
    module, store = authority
    checkpoint = _checkpoint()
    context = {"slot": "proposal:mechanism", "suffix": "dead-owner"}
    original = module.new_call(
        checkpoint,
        slot="proposal:mechanism",
        context_binding=context,
    )
    module.dispatch_call(
        original,
        full_prompt="provider that exits before a result",
        tools=module.SLOT_TOOLS["proposal:mechanism"],
        owner="llm_query:999999:MASTER PROPOSAL mechanism",
    )
    monkeypatch.setattr(module, "_provider_owner_is_alive", lambda _owner: False)

    recovered = module.new_call(
        checkpoint,
        slot="proposal:mechanism",
        context_binding=context,
    )

    assert recovered["invocation_id"] != original["invocation_id"]
    assert store.effect(original["effect_id"])["status"] == "exhausted"
    assert store.effect(original["effect_id"])["lease_owner"] is None


def test_live_provider_owner_blocks_duplicate_slot_dispatch(authority, monkeypatch):
    module, _store = authority
    checkpoint = _checkpoint()
    context = {"slot": "proposal:mechanism", "suffix": "live-owner"}
    original = module.new_call(
        checkpoint,
        slot="proposal:mechanism",
        context_binding=context,
    )
    module.dispatch_call(
        original,
        full_prompt="live provider",
        tools=module.SLOT_TOOLS["proposal:mechanism"],
        owner="llm_query:123:MASTER PROPOSAL mechanism",
    )
    monkeypatch.setattr(module, "_provider_owner_is_alive", lambda _owner: True)

    with pytest.raises(module.StrictAuthorityError, match="provider_call_active"):
        module.new_call(
            checkpoint,
            slot="proposal:mechanism",
            context_binding=context,
        )


def test_terminal_result_is_the_only_canonical_strict_output(authority):
    module, _store = authority
    result = ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="terminal-authority",
        total_cost_usd=0.01,
        usage={},
        result='{"terminal":true}',
    )

    assert module.canonical_provider_output([result]) == '{"terminal":true}'


def test_duplicate_proposal_rejection_is_durable_and_bounded(authority):
    module, store = authority
    checkpoint = _checkpoint()
    proposal_id = "duplicate-proposal-id"
    _call(
        module,
        checkpoint,
        "proposal:mechanism",
        role_result={"proposal_id": proposal_id, "direction": "mechanism"},
    )
    context = {"slot": "proposal:counterfactual", "suffix": "duplicate"}

    def complete_duplicate(call, session_id):
        role_result = {
            "proposal_id": proposal_id,
            "direction": "counterfactual",
        }
        raw_output = json.dumps(role_result, sort_keys=True)
        module.dispatch_call(
            call,
            full_prompt=f"duplicate provider {session_id}",
            tools=module.SLOT_TOOLS["proposal:counterfactual"],
            owner="pytest",
        )
        result = ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id=session_id,
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
        return module.reject_duplicate_proposal(call)

    first = module.new_call(
        checkpoint,
        slot="proposal:counterfactual",
        context_binding=context,
    )
    rejection = complete_duplicate(first, "duplicate-one")
    assert rejection["conflicting_slots"] == ["proposal:mechanism"]
    assert any(
        event.event_type == module.REJECTED_EVENT
        and event.payload.get("rejection_kind")
        == "proposal_identity_collision"
        for event in store.events(first["run_id"])
    )

    repair = module.new_call(
        checkpoint,
        slot="proposal:counterfactual",
        context_binding=context,
    )
    assert repair.get("replay_provider") is not True
    assert repair["schema_retry_required"] is True
    assert repair["schema_attempt"] == 2
    repair_prompt = module.schema_retry_prompt(repair)
    assert "ENSEMBLE DISTINCTNESS REPAIR" in repair_prompt
    assert "proposal_id is derived by the system" in repair_prompt
    assert "Changing only direction, risks, formatting" in repair_prompt
    complete_duplicate(repair, "duplicate-two")

    with pytest.raises(module.StrictAuthorityError, match="schema_retry_exhausted"):
        module.new_call(
            checkpoint,
            slot="proposal:counterfactual",
            context_binding=context,
        )


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


def test_pre_contract_33_proposal_parse_contract_cannot_be_replayed(authority):
    module, _store = authority
    call, _result, _receipt = _call(
        module,
        _checkpoint(),
        "proposal:mechanism",
        accept=False,
    )

    assert module.SLOT_PARSE_CONTRACTS["proposal:mechanism"] == "master-proposal-v3"
    with pytest.raises(module.StrictAuthorityError, match="parse_contract"):
        module.accept_role_result(
            call,
            role_result={"schema_version": "master-proposal-v2"},
            parse_contract="master-proposal-v2",
        )


def test_master_revision_drift_and_reverse_gate_revision_are_rejected(
    authority,
    monkeypatch,
):
    module, _store = authority
    for slot in module.MASTER_SLOTS:
        _call(module, _checkpoint(revision=10), slot)
    _call(module, _checkpoint(stage="quality_passed", revision=9), "review")
    _call(module, _checkpoint(stage="reviewed", revision=8), "critic")

    accepted, accepted_errors = module._accepted_events(
        _checkpoint(stage="reviewed", revision=99)
    )
    assert accepted_errors == []
    tampered = []
    for event in accepted:
        if event.payload.get("slot") != "master:final":
            tampered.append(event)
            continue
        payload = deepcopy(event.payload)
        payload["checkpoint_revision"] = 11
        payload["receipt_digest"] = module.content_digest({
            key: value
            for key, value in payload.items()
            if key != "receipt_digest"
        })
        tampered.append(type(event)(
            run_id=event.run_id,
            seq=event.seq,
            event_type=event.event_type,
            schema_version=event.schema_version,
            payload=payload,
            payload_digest=module.content_digest(payload),
            causation_id=event.causation_id,
        ))
    monkeypatch.setattr(
        module,
        "_accepted_events",
        lambda _checkpoint: (list(tampered), []),
    )

    _refs, errors = module.validate_receipts(
        _checkpoint(stage="reviewed", revision=99),
        required_slots=module.ALL_SLOTS,
        require_no_other_accepted=True,
    )
    assert "strict_authority_master_checkpoint_revision_drift" in errors
    assert "strict_authority_review_revision_precedes_master" in errors
    assert "strict_authority_critic_revision_precedes_review" in errors


def test_master_phase_revision_survives_checkpoint_metadata_updates(authority):
    module, _store = authority
    original_checkpoint = _checkpoint(revision=10)
    original, role_result, receipt = _call(
        module,
        original_checkpoint,
        "proposal:mechanism",
    )
    advanced_checkpoint = {
        **original_checkpoint,
        "checkpoint_revision": 12,
        "audit_attempt": 1,
    }

    recovered = module.new_call(
        advanced_checkpoint,
        slot="proposal:mechanism",
        context_binding={"slot": "proposal:mechanism", "suffix": "one"},
    )
    assert recovered["replay_provider"] is True
    assert recovered["checkpoint_revision"] == 10
    assert recovered["effect_id"] == original["effect_id"]
    assert recovered["accepted_receipt"] == receipt
    assert recovered["accepted_role_result"] == role_result

    expected_results = {"proposal:mechanism": role_result}
    for slot in module.MASTER_SLOTS[1:]:
        fresh, fresh_result, _fresh_receipt = _call(
            module,
            advanced_checkpoint,
            slot,
        )
        assert fresh.get("replay_provider") is not True
        assert fresh["checkpoint_revision"] == 10
        expected_results[slot] = fresh_result

    _refs, errors = module.validate_receipts(
        advanced_checkpoint,
        required_slots=module.MASTER_SLOTS,
        expected_role_results=expected_results,
        require_no_other_accepted=True,
    )
    assert errors == []


def test_master_phase_revision_rejects_checkpoint_regression(authority):
    module, _store = authority
    _call(module, _checkpoint(revision=10), "proposal:mechanism")

    with pytest.raises(
        module.StrictAuthorityError,
        match="strict_authority_phase_checkpoint_revision_regressed:master",
    ):
        module.new_call(
            _checkpoint(revision=9),
            slot="proposal:counterfactual",
            context_binding={"slot": "proposal:counterfactual"},
        )


def test_master_phase_revision_rejects_mixed_durable_effect_anchors(authority):
    module, store = authority
    original, _role_result, _receipt = _call(
        module,
        _checkpoint(revision=10),
        "proposal:mechanism",
    )
    mixed_input = deepcopy(store.effect(original["effect_id"])["input_payload"])
    mixed_context = {"slot": "proposal:counterfactual", "suffix": "mixed"}
    mixed_input.update({
        "slot": "proposal:counterfactual",
        "role": module.SLOT_CONTRACTS["proposal:counterfactual"][0],
        "purpose": module.SLOT_CONTRACTS["proposal:counterfactual"][1],
        "invocation_id": "d" * 32,
        "checkpoint_revision": 11,
        "context_binding": mixed_context,
        "context_binding_digest": module.content_digest(mixed_context),
        "prompt_digest": "e" * 64,
        "actual_role": module.SLOT_CONTRACTS["proposal:counterfactual"][0],
    })
    store.request_effect(
        run_id=original["run_id"],
        effect_id="strict-llm-" + "f" * 64,
        kind=module.EFFECT_KIND,
        input_payload=mixed_input,
        causation_id="pytest-mixed-master-revision",
        max_attempts=1,
    )

    with pytest.raises(
        module.StrictAuthorityError,
        match="strict_authority_phase_checkpoint_revision_drift:master",
    ):
        module.new_call(
            _checkpoint(revision=11),
            slot="ballot:scope",
            context_binding={"slot": "ballot:scope"},
        )


def test_final_receipts_reject_unaccepted_mixed_phase_revision(authority):
    module, store = authority
    checkpoint = _checkpoint(revision=10)
    original, role_result, _receipt = _call(
        module,
        checkpoint,
        "proposal:mechanism",
    )
    mixed_input = deepcopy(store.effect(original["effect_id"])["input_payload"])
    mixed_context = {"slot": "proposal:counterfactual", "suffix": "unaccepted"}
    mixed_input.update({
        "slot": "proposal:counterfactual",
        "role": module.SLOT_CONTRACTS["proposal:counterfactual"][0],
        "purpose": module.SLOT_CONTRACTS["proposal:counterfactual"][1],
        "invocation_id": "1" * 32,
        "checkpoint_revision": 11,
        "context_binding": mixed_context,
        "context_binding_digest": module.content_digest(mixed_context),
        "prompt_digest": "2" * 64,
        "actual_role": module.SLOT_CONTRACTS["proposal:counterfactual"][0],
    })
    store.request_effect(
        run_id=original["run_id"],
        effect_id="strict-llm-" + "3" * 64,
        kind=module.EFFECT_KIND,
        input_payload=mixed_input,
        causation_id="pytest-unaccepted-mixed-master-revision",
        max_attempts=1,
    )

    _refs, errors = module.validate_receipts(
        {**checkpoint, "checkpoint_revision": 11},
        required_slots=("proposal:mechanism",),
        expected_role_results={"proposal:mechanism": role_result},
        require_no_other_accepted=True,
    )
    assert "strict_authority_phase_checkpoint_revision_drift:master" in errors


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


def test_gate_phase_revision_replays_only_identical_semantics(authority):
    module, _store = authority
    checkpoint = _checkpoint(stage="quality_passed", revision=20)
    original = module.new_call(
        checkpoint,
        slot="review",
        context_binding={"semantic": "focus-a"},
    )
    module.dispatch_call(
        original,
        full_prompt="review focus a",
        tools=module.SLOT_TOOLS["review"],
        owner="pytest",
    )
    module.fail_provider_call(original, "provider unavailable")

    advanced = deepcopy(checkpoint)
    advanced["checkpoint_revision"] = 21
    retry = module.new_call(
        advanced,
        slot="review",
        context_binding={"semantic": "focus-a"},
    )
    assert retry["checkpoint_revision"] == 20

    with pytest.raises(
        module.StrictAuthorityError,
        match="strict_authority_phase_slot_context_drift:review:review",
    ):
        module.new_call(
            advanced,
            slot="review",
            context_binding={"semantic": "focus-b"},
        )


def test_master_phase_revision_replays_only_identical_per_slot_context(authority):
    module, _store = authority
    checkpoint = _checkpoint(revision=10)
    original = module.new_call(
        checkpoint,
        slot="proposal:mechanism",
        context_binding={"semantic": "planning-context-a"},
    )
    module.dispatch_call(
        original,
        full_prompt="master mechanism context a",
        tools=module.SLOT_TOOLS["proposal:mechanism"],
        owner="pytest",
    )
    module.fail_provider_call(original, "provider unavailable")

    advanced = deepcopy(checkpoint)
    advanced["checkpoint_revision"] = 11
    retry = module.new_call(
        advanced,
        slot="proposal:mechanism",
        context_binding={"semantic": "planning-context-a"},
    )
    assert retry["checkpoint_revision"] == 10
    assert retry.get("replay_provider") is not True

    with pytest.raises(
        module.StrictAuthorityError,
        match=(
            "strict_authority_phase_slot_context_drift:"
            "master:proposal:mechanism"
        ),
    ):
        module.new_call(
            advanced,
            slot="proposal:mechanism",
            context_binding={"semantic": "planning-context-b"},
        )


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


def test_accept_before_evidence_record_recovers_once_and_binds(authority):
    module, store = authority
    checkpoint = _checkpoint()
    original, _role_result, _receipt = _call(
        module,
        checkpoint,
        "proposal:mechanism",
    )
    log_file = _strict_log(
        module,
        store,
        original,
        "master_proposal_mechanism_io.txt",
    )
    with pytest.raises(
        module.StrictAuthorityError,
        match="strict_authority_invocation_evidence_provider_log_invalid",
    ):
        module.record_bound_invocation_evidence(
            original,
            log_file=log_file,
        )
    log_file.write_text("", encoding="utf-8")
    with pytest.raises(
        module.StrictAuthorityError,
        match="strict_authority_invocation_evidence_provider_log_invalid",
    ):
        module.record_bound_invocation_evidence(
            original,
            log_file=log_file,
        )
    log_file.write_text("completed provider log\n", encoding="utf-8")

    first = module.record_bound_invocation_evidence(
        original,
        log_file=log_file,
    )
    recovered = module.new_call(
        {**checkpoint, "checkpoint_revision": 11},
        slot="proposal:mechanism",
        context_binding={
            "slot": "proposal:mechanism",
            "suffix": "one",
        },
    )
    second = module.record_bound_invocation_evidence(
        recovered,
        log_file=log_file,
    )

    assert second == first
    assert log_file.read_text(encoding="utf-8").count(
        "[SYSTEM LLM INVOCATION EVIDENCE]"
    ) == 1
    bindings = [
        event
        for event in store.events(original["run_id"])
        if event.event_type == module.INVOCATION_EVIDENCE_BOUND_EVENT
    ]
    assert len(bindings) == 1


def test_strict_invocation_logs_are_isolated_across_reprepared_workflows(
    authority,
    tmp_path,
):
    module, _store = authority
    first_checkpoint = _checkpoint()
    second_checkpoint = {
        **_checkpoint(),
        "workflow_run_id": "strict-authority-test-run-reprepared",
    }
    first_call, first_result, _receipt = _call(
        module,
        first_checkpoint,
        "proposal:mechanism",
    )
    second_call, second_result, _receipt = _call(
        module,
        second_checkpoint,
        "proposal:mechanism",
    )
    logs_dir = tmp_path / "results" / "v155" / "logs"
    first_log = module.strict_invocation_log_path(
        first_call,
        logs_dir=logs_dir,
        basename="master_proposal_mechanism_io.txt",
    )
    second_log = module.strict_invocation_log_path(
        second_call,
        logs_dir=logs_dir,
        basename="master_proposal_mechanism_io.txt",
    )

    assert first_log != second_log
    assert first_log.parent.name == first_call["invocation_id"]
    assert second_log.parent.name == second_call["invocation_id"]
    assert first_log.name == second_log.name
    assert module.strict_invocation_log_path(
        first_call,
        logs_dir=logs_dir,
        basename=first_log.name,
    ) == first_log

    first_log.write_text("first workflow provider log\n", encoding="utf-8")
    first_evidence = module.record_bound_invocation_evidence(
        first_call,
        log_file=first_log,
    )
    first_bytes = first_log.read_bytes()
    second_log.write_text("second workflow provider log\n", encoding="utf-8")
    second_evidence = module.record_bound_invocation_evidence(
        second_call,
        log_file=second_log,
    )

    assert first_log.read_bytes() == first_bytes
    assert first_evidence["io_log_path"] != second_evidence["io_log_path"]
    assert first_evidence["role_result_digest"] != ""
    assert second_evidence["role_result_digest"] != ""
    assert first_result == second_result
    assert first_log.read_text(encoding="utf-8").count(
        "[SYSTEM LLM INVOCATION EVIDENCE]"
    ) == 1
    assert second_log.read_text(encoding="utf-8").count(
        "[SYSTEM LLM INVOCATION EVIDENCE]"
    ) == 1


def test_strict_invocation_log_rejects_symlinked_parent(tmp_path, monkeypatch):
    import strict_authority_workflow as module
    import evolution_infra

    results_dir = tmp_path / "results"
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results_dir)
    outside = tmp_path / "outside"
    outside.mkdir()
    (results_dir / "v155").mkdir(parents=True)
    linked_logs = results_dir / "v155" / "logs"
    linked_logs.symlink_to(outside, target_is_directory=True)
    call = {
        "invocation_id": "6" * 32,
        "generation_binding": {"next_v": 155},
    }

    with pytest.raises(
        module.StrictAuthorityError,
        match="strict_authority_invocation_log_parent_invalid",
    ):
        module.strict_invocation_log_path(
            call,
            logs_dir=linked_logs,
            basename="critic_io.txt",
        )
    assert not (outside / "strict_invocations").exists()

    linked_parent = results_dir / "linked_parent"
    linked_parent.symlink_to(outside, target_is_directory=True)
    with pytest.raises(
        module.StrictAuthorityError,
        match="strict_authority_invocation_log_generation_root_mismatch",
    ):
        module.strict_invocation_log_path(
            call,
            logs_dir=linked_parent / "v143" / "logs",
            basename="critic_io.txt",
        )
    assert not (outside / "v143").exists()


@pytest.mark.parametrize("collision", ["strict_root", "invocation", "log"])
def test_strict_invocation_log_normalizes_filesystem_collisions(
    tmp_path,
    collision,
    monkeypatch,
):
    import strict_authority_workflow as module
    import evolution_infra

    invocation_id = "7" * 32
    results_dir = tmp_path / collision
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results_dir)
    logs_dir = results_dir / "v155" / "logs"
    logs_dir.mkdir(parents=True)
    strict_root = logs_dir / "strict_invocations"
    if collision == "strict_root":
        strict_root.write_text("not a directory\n")
    else:
        strict_root.mkdir()
        invocation_dir = strict_root / invocation_id
        if collision == "invocation":
            invocation_dir.write_text("not a directory\n")
        else:
            invocation_dir.mkdir()
            (invocation_dir / "reviewer_io.txt").mkdir()

    with pytest.raises(module.StrictAuthorityError):
        module.strict_invocation_log_path(
            {
                "invocation_id": invocation_id,
                "generation_binding": {"next_v": 155},
            },
            logs_dir=logs_dir,
            basename="reviewer_io.txt",
        )


def test_strict_invocation_log_rejects_arbitrary_or_wrong_version_root(
    authority,
):
    module, store = authority
    call, _role_result, _receipt = _call(
        module,
        _checkpoint(),
        "proposal:mechanism",
    )

    for logs_dir in (
        store.path.parent.parent / "unrelated" / "logs",
        store.path.parent.parent / "v154" / "logs",
    ):
        with pytest.raises(
            module.StrictAuthorityError,
            match="strict_authority_invocation_log_generation_root_mismatch",
        ):
            module.strict_invocation_log_path(
                call,
                logs_dir=logs_dir,
                basename="master_proposal_mechanism_io.txt",
            )

    foreign_log = (
        store.path.parent.parent
        / "unrelated"
        / "logs"
        / "strict_invocations"
        / call["invocation_id"]
        / "master_proposal_mechanism_io.txt"
    )
    foreign_log.parent.mkdir(parents=True)
    foreign_log.write_text("foreign provider bytes\n", encoding="utf-8")
    with pytest.raises(
        module.StrictAuthorityError,
        match="strict_authority_invocation_evidence_log_identity_mismatch",
    ):
        module.record_bound_invocation_evidence(call, log_file=foreign_log)
    assert "[SYSTEM LLM INVOCATION EVIDENCE]" not in foreign_log.read_text(
        encoding="utf-8"
    )


def test_strict_evidence_rejects_flat_same_basename(authority):
    module, store = authority
    call, _role_result, _receipt = _call(
        module,
        _checkpoint(),
        "proposal:mechanism",
    )
    flat_log = store.path.parent / "master_proposal_mechanism_io.txt"
    flat_log.write_text("completed provider log\n", encoding="utf-8")

    with pytest.raises(
        module.StrictAuthorityError,
        match="strict_authority_invocation_evidence_log_identity_mismatch",
    ):
        module.record_bound_invocation_evidence(
            call,
            log_file=flat_log,
        )


def test_invocation_evidence_recovery_append_is_atomic(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from system_strict_bootstrap import record_llm_invocation_evidence

    log_file = tmp_path / "strict-provider_io.txt"
    log_file.write_text("completed provider log\n", encoding="utf-8")
    kwargs = {
        "invocation_id": "1" * 32,
        "purpose": "master_proposal_scout:mechanism",
        "role": "MASTER PROPOSAL mechanism",
        "prompt_digest": "2" * 64,
        "raw_output_digest": "3" * 64,
        "result_digest": "4" * 64,
        "role_result": {"proposal_id": "5" * 16},
        "log_file": log_file,
        "recover_or_record": True,
    }

    with ThreadPoolExecutor(max_workers=2) as executor:
        receipts = list(executor.map(
            lambda _index: record_llm_invocation_evidence(**kwargs),
            range(2),
        ))

    assert receipts[0] == receipts[1]
    assert log_file.read_text(encoding="utf-8").count(
        "[SYSTEM LLM INVOCATION EVIDENCE]"
    ) == 1


def test_gate_evidence_replay_is_stable_and_log_drift_fails(authority):
    module, store = authority
    checkpoint = _checkpoint(stage="quality_passed", revision=20)
    original, _role_result, _receipt = _call(
        module,
        checkpoint,
        "review",
    )
    log_file = _strict_log(module, store, original, "reviewer_io.txt")
    log_file.write_text("completed provider log\n", encoding="utf-8")
    first = module.record_bound_invocation_evidence(
        original,
        log_file=log_file,
    )
    recovered = module.new_call(
        {**checkpoint, "checkpoint_revision": 21},
        slot="review",
        context_binding={"slot": "review", "suffix": "one"},
    )
    second = module.record_bound_invocation_evidence(
        recovered,
        log_file=log_file,
    )
    assert second == first

    with log_file.open("a", encoding="utf-8") as handle:
        handle.write("post-binding drift\n")
    with pytest.raises(
        module.StrictAuthorityError,
        match="system_bootstrap_llm_invocation_log_digest_mismatch",
    ):
        module.record_bound_invocation_evidence(
            recovered,
            log_file=log_file,
        )


def test_invocation_evidence_prompt_digest_must_match_provider_effect(
    authority,
):
    module, store = authority
    checkpoint = _checkpoint()
    call, role_result, _receipt = _call(
        module,
        checkpoint,
        "proposal:mechanism",
    )
    provider = store.effect(call["effect_id"])["result_payload"]
    from system_strict_bootstrap import (
        llm_result_digest,
        record_llm_invocation_evidence,
    )

    evidence = record_llm_invocation_evidence(
        invocation_id=call["invocation_id"],
        purpose=call["purpose"],
        role=call["actual_role"],
        prompt_digest="f" * 64,
        raw_output_digest=call["raw_output_digest"],
        result_digest=llm_result_digest(
            provider["provider_cost_usd"],
            provider["provider_usage"],
        ),
        role_result=role_result,
        log_file=_strict_log(
            module,
            store,
            call,
            "master_proposal_mechanism_io.txt",
        ),
    )
    assert evidence["prompt_digest"] != call["prompt_digest"]
    with pytest.raises(
        module.StrictAuthorityError,
        match="strict_authority_invocation_evidence_prompt_digest_mismatch",
    ):
        module.bind_invocation_evidence(call, evidence)


def test_evidence_recovery_error_becomes_strict_authority(authority):
    module, store = authority
    call, role_result, _receipt = _call(
        module,
        _checkpoint(),
        "proposal:mechanism",
    )
    provider = store.effect(call["effect_id"])["result_payload"]
    from system_strict_bootstrap import (
        llm_result_digest,
        record_llm_invocation_evidence,
    )

    log_file = _strict_log(
        module,
        store,
        call,
        "master_proposal_mechanism_io.txt",
    )
    log_file.write_text("completed provider log\n", encoding="utf-8")
    kwargs = {
        "invocation_id": call["invocation_id"],
        "purpose": call["purpose"],
        "role": call["actual_role"],
        "prompt_digest": call["prompt_digest"],
        "raw_output_digest": call["raw_output_digest"],
        "result_digest": llm_result_digest(
            provider["provider_cost_usd"],
            provider["provider_usage"],
        ),
        "role_result": role_result,
        "log_file": log_file,
    }
    record_llm_invocation_evidence(**kwargs)
    record_llm_invocation_evidence(**kwargs)

    with pytest.raises(
        module.StrictAuthorityError,
        match="system_bootstrap_llm_invocation_recovery_evidence_invalid",
    ):
        module.record_bound_invocation_evidence(
            call,
            log_file=log_file,
        )
