import json
from pathlib import Path
import re

import pytest


class _UI:
    def clear_io(self):
        pass

    def log_history(self, *_args, **_kwargs):
        pass


def _write_source(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "policy.py").write_text(
        "def get_baseline_decision(context):\n"
        "    return _choose_intent(context)\n\n"
        "def _choose_intent(context):\n"
        "    return {'kind': 'pass'}\n",
        encoding="utf-8",
    )


def _proposal(direction: str, *, shared_claims: bool = False) -> str:
    label = "shared" if shared_claims else direction
    payload = {
        "targeted_failure": f"{label} identifies one repeated reachable decision failure.",
        "structural_change": f"{label} replaces the parent decision mechanism with one bounded path.",
        "counterfactual": f"{label} changes one input while holding cards, seed, and legality fixed.",
        "measurement": f"{label} requires a positive/control decision test and paired native result.",
        "why_not_threshold_tuning": f"{label} changes state flow and a reachable consumer, not one number.",
        "expected_diff": f"{label} changes get_baseline_decision through the existing _choose_intent call edge.",
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
            "test_name": f"test_{label}_decision_path",
            "control": "Run the frozen parent on the same state, cards, and deterministic seed.",
            "intervention": "Run only the proposed mechanism on that identical canonical state.",
            "expected_observation": "The target action changes while the paired control remains unchanged.",
        },
        "evidence_refs": [
            "source:policy.py:get_baseline_decision",
            "source:policy.py:_choose_intent",
        ],
        "risks": "The mechanism may overfit sparse evidence and must remain bounded.",
    }
    return "```json\n" + json.dumps(payload) + "\n```"


def _critic_output(agent_master, proposal_ids, *, reverse=False):
    ids = list(proposal_ids)
    if reverse:
        ids.reverse()
    ballots = []
    for index, proposal_id in enumerate(ids):
        ballots.append({
            "proposal_id": proposal_id,
            "scores": {
                criterion: 5 - min(index, 3)
                for criterion in agent_master._PROPOSAL_CRITIC_CRITERIA
            },
            "reject": False,
            "reason": "All source evidence and the direct runtime call chain are explicit.",
        })
    return json.dumps({"ballots": ballots})


@pytest.mark.asyncio
async def test_proposal_ensemble_validates_evidence_and_blind_criterion_reviews(
    monkeypatch, tmp_path
):
    import agent_master

    source_dir = tmp_path / "national_v143"
    snapshot_dir = tmp_path / "snapshot"
    _write_source(source_dir)
    snapshot_dir.mkdir()
    calls = []

    async def fake_query(
        prompt, _ctx, _ui, role_name, _log_file, tools=None, **kwargs
    ):
        calls.append((role_name, prompt, tools, kwargs))
        if role_name.startswith("MASTER PROPOSAL CRITIC"):
            ids = list(dict.fromkeys(re.findall(
                r'"proposal_id":"([0-9a-f]{16})"', prompt
            )))
            return (
                _critic_output(
                    agent_master,
                    ids,
                    reverse=role_name.endswith("scope"),
                ),
                0.0,
                {},
            )
        direction = role_name.rsplit(" ", 1)[-1]
        return _proposal(direction), 0.0, {}

    monkeypatch.setattr(agent_master, "get_bot_dir", lambda _v: source_dir)
    monkeypatch.setattr(agent_master, "run_claude_query", fake_query)
    packet_text = await agent_master._run_master_proposal_ensemble(
        "frozen planning context",
        source_v=143,
        next_v=149,
        ui=_UI(),
        log_dir=tmp_path,
        allowed_evidence_snapshot_dir=str(snapshot_dir),
    )
    packet = json.loads(packet_text)

    assert packet["valid"] is True
    assert packet["schema_version"] == "master-proposal-packet-v2"
    assert packet["proposal_count"] == 3
    assert packet["valid_critic_count"] == 2
    assert len(packet["allowed_proposal_ids"]) == 3
    assert len(packet["ordered_proposals"]) == 3
    assert set(packet["proposal_invocations"]) == set(
        packet["allowed_proposal_ids"]
    )
    invocation_ids = [
        evidence["invocation_id"]
        for evidence in packet["proposal_invocations"].values()
    ] + [
        review["invocation_evidence"]["invocation_id"]
        for review in packet["critic_reviews"]
    ]
    assert len(invocation_ids) == len(set(invocation_ids)) == 5
    parsed, errors = agent_master._parse_valid_proposal_packet(packet_text)
    assert parsed == packet
    assert errors == []
    assert len(calls) == 5
    scout_calls = calls[:3]
    assert all(call[2] == ["Read"] for call in scout_calls)
    assert all("allowed_evidence_snapshot_dir" in call[3] for call in scout_calls)
    assert all("SYSTEM-VERIFIED SOURCE CALL INDEX" in call[1] for call in scout_calls)
    assert all(
        "policy.py:get_baseline_decision -> policy.py:_choose_intent" in call[1]
        for call in scout_calls
    )
    critic_calls = calls[3:]
    assert all(call[2] == [] for call in critic_calls)
    assert all('"direction":' not in call[1] for call in critic_calls)
    assert all("evidence_traceability" in call[1] for call in critic_calls)
    assert all("Planning context digest:" in call[1] for call in critic_calls)
    assert packet["authority"].startswith("advisory_only")


@pytest.mark.asyncio
async def test_protocol_bootstrap_scout_indexes_only_prepared_baseline(
    monkeypatch,
    tmp_path,
):
    import agent_master

    prepared = tmp_path / "national_v149"
    snapshot = tmp_path / "snapshot"
    _write_source(prepared)
    snapshot.mkdir()
    graph_calls = []

    def source_graph(path):
        graph_calls.append(path)
        return {
            "policy.py:get_baseline_decision": {"_choose_intent"},
            "policy.py:_choose_intent": set(),
        }, "b" * 64

    async def invalid_query(*_args, **_kwargs):
        return "{}", 0.0, {}

    monkeypatch.setattr(agent_master, "get_bot_dir", lambda _v: prepared)
    monkeypatch.setattr(agent_master, "_source_symbol_graph", source_graph)
    monkeypatch.setattr(agent_master, "run_claude_query", invalid_query)

    packet = json.loads(await agent_master._run_master_proposal_ensemble(
        "strict migration context",
        source_v=142,
        next_v=149,
        ui=_UI(),
        log_dir=tmp_path,
        allowed_evidence_snapshot_dir=str(snapshot),
        baseline_v=149,
        protocol_bootstrap_prepared_only=True,
    ))

    assert packet["valid"] is False
    assert graph_calls == [prepared]
    assert packet["source_code_digest"] == "b" * 64


def test_source_symbol_prompt_index_is_deterministic_and_line_bounded(tmp_path):
    import agent_master

    source_dir = tmp_path / "source"
    _write_source(source_dir)
    graph, _digest = agent_master._source_symbol_graph(source_dir)

    first = agent_master._source_symbol_prompt_index(graph)
    second = agent_master._source_symbol_prompt_index(dict(reversed(list(graph.items()))))
    bounded = agent_master._source_symbol_prompt_index(graph, maximum_chars=130)

    assert first == second
    assert "policy.py:get_baseline_decision -> policy.py:_choose_intent" in first
    assert all(len(line) <= 130 for line in bounded.splitlines())


def test_same_leaf_in_two_files_is_not_accepted_as_reachability(tmp_path):
    import agent_master
    import system_strict_bootstrap

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "policy.py").write_text(
        "def get_baseline_decision(context):\n"
        "    return _choose_intent(context)\n\n"
        "def _choose_intent(context):\n"
        "    return {'kind': 'pass'}\n\n"
        "class AlternatePolicy:\n"
        "    def _choose_intent(self, context):\n"
        "        return {'kind': 'fold'}\n",
        encoding="utf-8",
    )
    graph, _digest = agent_master._source_symbol_graph(source_dir)
    prompt_index = agent_master._source_symbol_prompt_index(graph)
    assert "policy.py:get_baseline_decision ->" not in prompt_index

    payload = json.loads(
        _proposal("mechanism").split("```json\n", 1)[1].rsplit("\n```", 1)[0]
    )
    payload["source_symbols"] = [
        "policy.py:get_baseline_decision",
        "policy.py:AlternatePolicy._choose_intent",
    ]
    payload["reachable_chain"] = list(payload["source_symbols"])
    payload["evidence_refs"] = [
        "source:policy.py:get_baseline_decision",
        "source:policy.py:AlternatePolicy._choose_intent",
    ]
    assert agent_master._validated_master_proposal(
        json.dumps(payload),
        "mechanism",
        source_graph=graph,
        snapshot_dir=tmp_path,
    ) is None
    assert system_strict_bootstrap._chain_errors(
        graph,
        payload["reachable_chain"],
    ) == ["system_bootstrap_selected_chain_unreachable"]


@pytest.mark.asyncio
async def test_critic_proposal_order_is_context_digest_deterministic(monkeypatch, tmp_path):
    import agent_master

    source_dir = tmp_path / "source"
    snapshot_dir = tmp_path / "snapshot"
    _write_source(source_dir)
    snapshot_dir.mkdir()
    observed = []

    async def fake_query(prompt, _ctx, _ui, role_name, *_args, **_kwargs):
        if role_name.startswith("MASTER PROPOSAL CRITIC"):
            ids = list(dict.fromkeys(re.findall(
                r'"proposal_id":"([0-9a-f]{16})"', prompt
            )))
            observed.append((role_name, ids))
            return _critic_output(agent_master, ids), 0.0, {}
        return _proposal(role_name.rsplit(" ", 1)[-1]), 0.0, {}

    monkeypatch.setattr(agent_master, "get_bot_dir", lambda _v: source_dir)
    monkeypatch.setattr(agent_master, "run_claude_query", fake_query)
    kwargs = dict(
        planning_context="same frozen planning context",
        source_v=143,
        next_v=149,
        ui=_UI(),
        log_dir=tmp_path,
        allowed_evidence_snapshot_dir=str(snapshot_dir),
    )
    first = json.loads(await agent_master._run_master_proposal_ensemble(**kwargs))
    first_orders = list(observed)
    observed.clear()
    second = json.loads(await agent_master._run_master_proposal_ensemble(**kwargs))

    assert observed == first_orders
    assert second["context_digest"] == first["context_digest"]
    assert second["allowed_proposal_ids"] == first["allowed_proposal_ids"]


@pytest.mark.asyncio
async def test_ensemble_repairs_one_scout_and_critic_schema_failure(
    monkeypatch, tmp_path
):
    import agent_master

    source_dir = tmp_path / "source"
    snapshot_dir = tmp_path / "snapshot"
    _write_source(source_dir)
    snapshot_dir.mkdir()
    roles = []

    async def fake_query(prompt, _ctx, _ui, role_name, *_args, **_kwargs):
        roles.append(role_name)
        if role_name.startswith("MASTER PROPOSAL CRITIC"):
            if (
                "falsification" in role_name
                and "SCHEMA RETRY" not in role_name
            ):
                return "{}", 0.0, {}
            ids = list(dict.fromkeys(re.findall(
                r'"proposal_id":"([0-9a-f]{16})"', prompt
            )))
            return _critic_output(agent_master, ids), 0.0, {}
        if "mechanism" in role_name and "SCHEMA RETRY" not in role_name:
            return "{}", 0.0, {}
        direction = next(
            name
            for name in ("mechanism", "counterfactual", "compute_memory")
            if name in role_name
        )
        return _proposal(direction), 0.0, {}

    monkeypatch.setattr(agent_master, "get_bot_dir", lambda _v: source_dir)
    monkeypatch.setattr(agent_master, "run_claude_query", fake_query)

    packet = json.loads(await agent_master._run_master_proposal_ensemble(
        "frozen planning context",
        source_v=143,
        next_v=149,
        ui=_UI(),
        log_dir=tmp_path,
        allowed_evidence_snapshot_dir=str(snapshot_dir),
    ))

    assert packet["valid"] is True
    assert packet["proposal_count"] == 3
    assert packet["valid_critic_count"] == 2
    assert len(roles) == 7
    assert "MASTER PROPOSAL mechanism SCHEMA RETRY" in roles
    assert "MASTER PROPOSAL CRITIC falsification SCHEMA RETRY" in roles


@pytest.mark.asyncio
async def test_duplicate_proposal_gets_one_causally_distinct_repair(
    monkeypatch, tmp_path
):
    import agent_master

    source_dir = tmp_path / "source"
    snapshot_dir = tmp_path / "snapshot"
    _write_source(source_dir)
    snapshot_dir.mkdir()
    calls = []

    async def fake_query(prompt, _ctx, _ui, role_name, *_args, **_kwargs):
        calls.append((role_name, prompt))
        if role_name.startswith("MASTER PROPOSAL CRITIC"):
            ids = list(dict.fromkeys(re.findall(
                r'"proposal_id":"([0-9a-f]{16})"', prompt
            )))
            return _critic_output(agent_master, ids), 0.0, {}
        if "counterfactual" in role_name:
            if "DISTINCTNESS RETRY" in role_name:
                return _proposal("counterfactual"), 0.0, {}
            return _proposal("mechanism", shared_claims=True), 0.0, {}
        if "mechanism" in role_name:
            return _proposal("mechanism", shared_claims=True), 0.0, {}
        return _proposal("compute_memory"), 0.0, {}

    monkeypatch.setattr(agent_master, "get_bot_dir", lambda _v: source_dir)
    monkeypatch.setattr(agent_master, "run_claude_query", fake_query)

    packet = json.loads(await agent_master._run_master_proposal_ensemble(
        "frozen planning context",
        source_v=143,
        next_v=149,
        ui=_UI(),
        log_dir=tmp_path,
        allowed_evidence_snapshot_dir=str(snapshot_dir),
    ))

    assert packet["valid"] is True
    assert packet["proposal_count"] == 3
    assert len(set(packet["allowed_proposal_ids"])) == 3
    repair_calls = [
        item for item in calls if "DISTINCTNESS RETRY" in item[0]
    ]
    assert len(repair_calls) == 1
    assert repair_calls[0][0] == (
        "MASTER PROPOSAL counterfactual DISTINCTNESS RETRY"
    )
    assert "single permitted distinctness repair" in repair_calls[0][1]
    assert "genuinely different reachable mechanism" in repair_calls[0][1]
    assert "Changing only direction, risks, wording" in repair_calls[0][1]
    assert len(calls) == 6


@pytest.mark.asyncio
async def test_second_duplicate_fails_closed_without_critics_or_another_repair(
    monkeypatch, tmp_path
):
    import agent_master

    source_dir = tmp_path / "source"
    snapshot_dir = tmp_path / "snapshot"
    _write_source(source_dir)
    snapshot_dir.mkdir()
    roles = []

    async def fake_query(_prompt, _ctx, _ui, role_name, *_args, **_kwargs):
        roles.append(role_name)
        if "mechanism" in role_name or "counterfactual" in role_name:
            return _proposal("mechanism", shared_claims=True), 0.0, {}
        return _proposal("compute_memory"), 0.0, {}

    monkeypatch.setattr(agent_master, "get_bot_dir", lambda _v: source_dir)
    monkeypatch.setattr(agent_master, "run_claude_query", fake_query)

    packet = json.loads(await agent_master._run_master_proposal_ensemble(
        "frozen planning context",
        source_v=143,
        next_v=149,
        ui=_UI(),
        log_dir=tmp_path,
        allowed_evidence_snapshot_dir=str(snapshot_dir),
    ))

    assert packet["valid"] is False
    assert packet["reason"] == (
        "three_distinct_schema_valid_scout_proposals_required:got_2"
    )
    assert roles.count(
        "MASTER PROPOSAL counterfactual DISTINCTNESS RETRY"
    ) == 1
    assert len(roles) == 4
    assert not any("CRITIC" in role for role in roles)


@pytest.mark.asyncio
async def test_strict_duplicate_rejection_restarts_as_distinctness_repair(
    monkeypatch, tmp_path
):
    import agent_master
    import evolution_infra
    import strict_authority_workflow

    class CrashAfterDurableRejection(RuntimeError):
        pass

    source_dir = tmp_path / "source"
    snapshot_dir = tmp_path / "snapshot"
    _write_source(source_dir)
    snapshot_dir.mkdir()
    results_dir = tmp_path / "results"
    log_dir = results_dir / "v143" / "logs"
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results_dir)
    accepted_by_slot = {}
    rejected_slots = set()
    observed_roles = []
    invocation_counter = 0
    crash_once = True

    def fake_new_call(_checkpoint, *, slot, **_kwargs):
        nonlocal invocation_counter
        invocation_counter += 1
        call = {
            "slot": slot,
            "invocation_id": f"{invocation_counter:032x}",
            "effect_id": "strict-llm-" + f"{invocation_counter:064x}",
            "generation_binding": {"next_v": 143},
        }
        if slot in rejected_slots and slot not in accepted_by_slot:
            call.update({
                "schema_retry_required": True,
                "schema_attempt": 2,
                "prior_schema_rejection": {
                    "rejection_kind": "proposal_identity_collision",
                    "projection_errors": [
                        "strict_authority_proposal_identity_collision"
                    ],
                },
            })
        return call

    def fake_accept(call, *, role_result, parse_contract):
        assert parse_contract in {
            "master-proposal-v2",
            "master-proposal-ballot-v1",
        }
        accepted_by_slot[call["slot"]] = role_result
        return {"slot": call["slot"]}

    def fake_reject(call):
        nonlocal crash_once
        rejected_slots.add(call["slot"])
        if crash_once:
            crash_once = False
            raise CrashAfterDurableRejection("simulated post-journal crash")
        return {"slot": call["slot"]}

    async def fake_query(prompt, _ctx, _ui, role_name, *_args, **kwargs):
        observed_roles.append(role_name)
        strict_call = kwargs["strict_authority"]
        if role_name.startswith("MASTER PROPOSAL CRITIC"):
            ids = list(dict.fromkeys(re.findall(
                r'"proposal_id":"([0-9a-f]{16})"', prompt
            )))
            return _critic_output(agent_master, ids), 0.0, {}
        slot = strict_call["slot"]
        if slot == "proposal:counterfactual":
            if strict_call.get("schema_retry_required"):
                assert "DISTINCTNESS RETRY" in role_name
                return _proposal("counterfactual"), 0.0, {}
            return _proposal("mechanism", shared_claims=True), 0.0, {}
        if slot == "proposal:mechanism":
            return _proposal("mechanism", shared_claims=True), 0.0, {}
        return _proposal("compute_memory"), 0.0, {}

    monkeypatch.setattr(agent_master, "get_bot_dir", lambda _v: source_dir)
    monkeypatch.setattr(agent_master, "run_claude_query", fake_query)
    monkeypatch.setattr(strict_authority_workflow, "new_call", fake_new_call)
    monkeypatch.setattr(
        strict_authority_workflow, "accept_role_result", fake_accept
    )
    monkeypatch.setattr(
        strict_authority_workflow, "reject_duplicate_proposal", fake_reject
    )
    kwargs = dict(
        planning_context="frozen planning context",
        source_v=142,
        next_v=143,
        ui=_UI(),
        log_dir=log_dir,
        allowed_evidence_snapshot_dir=str(snapshot_dir),
        baseline_v=143,
        protocol_bootstrap_prepared_only=True,
        strict_checkpoint={},
    )

    with pytest.raises(CrashAfterDurableRejection):
        await agent_master._run_master_proposal_ensemble(**kwargs)

    packet = json.loads(await agent_master._run_master_proposal_ensemble(**kwargs))

    assert packet["valid"] is True
    assert len(set(packet["allowed_proposal_ids"])) == 3
    assert rejected_slots == {"proposal:counterfactual"}
    assert (
        "MASTER PROPOSAL counterfactual DISTINCTNESS RETRY"
        in observed_roles
    )


@pytest.mark.asyncio
async def test_strict_log_allocation_failure_remains_authority_error(
    monkeypatch,
    tmp_path,
):
    import agent_master
    import evolution_infra
    import strict_authority_workflow as authority

    source_dir = tmp_path / "source"
    snapshot_dir = tmp_path / "snapshot"
    _write_source(source_dir)
    snapshot_dir.mkdir()
    results_dir = tmp_path / "results"
    log_dir = results_dir / "v143" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "strict_invocations").write_text("collision\n")
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results_dir)
    counter = {"value": 0}

    def fake_new_call(_checkpoint, *, slot, **_kwargs):
        counter["value"] += 1
        return {
            "slot": slot,
            "invocation_id": f"{counter['value']:032x}",
            "generation_binding": {"next_v": 143},
        }

    async def forbidden_provider(*_args, **_kwargs):
        raise AssertionError("provider must not run after log allocation failure")

    monkeypatch.setattr(agent_master, "get_bot_dir", lambda _v: source_dir)
    monkeypatch.setattr(agent_master, "run_claude_query", forbidden_provider)
    monkeypatch.setattr(authority, "new_call", fake_new_call)

    with pytest.raises(
        authority.StrictAuthorityError,
        match="strict_authority_invocation_log_filesystem_invalid",
    ):
        await agent_master._run_master_proposal_ensemble(
            planning_context="frozen planning context",
            source_v=142,
            next_v=143,
            ui=_UI(),
            log_dir=log_dir,
            allowed_evidence_snapshot_dir=str(snapshot_dir),
            baseline_v=143,
            protocol_bootstrap_prepared_only=True,
            strict_checkpoint={},
        )


@pytest.mark.asyncio
async def test_strict_partial_packet_replays_accepted_slots_across_revision(
    monkeypatch,
    tmp_path,
):
    import agent_master
    import evolution_infra
    import strict_authority_workflow as authority
    from claude_agent_sdk import ResultMessage
    from workflow_kernel import WorkflowStore

    source_dir = tmp_path / "source"
    snapshot_dir = source_dir / ".protocol_bootstrap_no_strength_evidence"
    _write_source(source_dir)
    snapshot_dir.mkdir()
    store = WorkflowStore(tmp_path / "strict-authority.sqlite3")
    results_dir = tmp_path / "results"
    log_dir = results_dir / "v143" / "logs"
    monkeypatch.setattr(authority, "_store", lambda: store)
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(agent_master, "get_bot_dir", lambda _v: source_dir)
    monkeypatch.setattr(evolution_infra, "get_bot_dir", lambda _v: source_dir)

    provider_slots = []
    provider_logs = []
    compute_calls = {"count": 0}
    provider_counter = {"value": 0}

    async def fake_query(prompt, _ctx, _ui, role_name, *_args, **kwargs):
        call = kwargs["strict_authority"]
        log_file = Path(_args[0])
        provider_logs.append((call["invocation_id"], log_file))
        authority.dispatch_call(
            call,
            full_prompt=str(prompt),
            tools=kwargs["tools"],
            owner="pytest-strict-partial-packet",
            actual_role=role_name,
        )
        if call.get("replay_provider"):
            return (
                call["replay_raw_output"],
                float(call.get("replay_cost_usd") or 0.0),
                call.get("replay_usage") or {},
            )

        with log_file.open("a", encoding="utf-8") as handle:
            handle.write(f"provider call for {role_name}\n")

        slot = call["slot"]
        provider_slots.append(slot)
        if slot == "proposal:compute_memory":
            compute_calls["count"] += 1
            if compute_calls["count"] == 1:
                error = RuntimeError("simulated provider cleanup failure")
                authority.fail_provider_call(call, error)
                raise error
            output = (
                "not a schema-valid proposal"
                if compute_calls["count"] == 2
                else _proposal("compute_memory")
            )
        elif slot.startswith("proposal:"):
            output = _proposal(slot.split(":", 1)[1])
        else:
            proposal_ids = list(dict.fromkeys(re.findall(
                r'"proposal_id":"([0-9a-f]{16})"',
                str(prompt),
            )))
            output = _critic_output(agent_master, proposal_ids)

        provider_counter["value"] += 1
        result = ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id=f"strict-partial-{provider_counter['value']}",
            total_cost_usd=0.01,
            usage={"input_tokens": 1, "output_tokens": 1},
            result=output,
        )
        authority._observe_provider_result(
            result,
            invocation_id=call["invocation_id"],
            effect_id=call["effect_id"],
        )
        authority.complete_provider_call(
            call,
            raw_output=output,
            provider_results=[result],
        )
        return output, 0.01, result.usage

    monkeypatch.setattr(agent_master, "run_claude_query", fake_query)
    checkpoint = {
        "workflow_run_id": "generation:143:workflow-v-test",
        "source_v": 142,
        "next_v": 143,
        "stage": "direction_audited",
        "checkpoint_revision": 10,
        "audit_context": {
            "protocol_bootstrap": {"receipt_digest": "a" * 64},
            "prepared_artifact_contract": {
                "contract_digest": "b" * 64,
                "prepared_artifact_hash": "c" * 64,
            },
        },
    }
    kwargs = {
        "planning_context": "frozen strict partial packet context",
        "source_v": 142,
        "next_v": 143,
        "ui": _UI(),
        "log_dir": log_dir,
        "allowed_evidence_snapshot_dir": str(snapshot_dir),
        "baseline_v": 143,
        "protocol_bootstrap_prepared_only": True,
    }

    first = json.loads(await agent_master._run_master_proposal_ensemble(
        strict_checkpoint=checkpoint,
        **kwargs,
    ))
    assert first["valid"] is False
    assert first["reason"].endswith("got_2")
    accepted_before = [
        event.payload["slot"]
        for event in store.events(authority.authority_run_id(
            checkpoint["workflow_run_id"]
        ))
        if event.event_type == authority.ACCEPTED_EVENT
    ]
    assert sorted(accepted_before) == [
        "proposal:counterfactual",
        "proposal:mechanism",
    ]

    provider_count_before_retry = len(provider_slots)
    advanced_checkpoint = {
        **checkpoint,
        "checkpoint_revision": 11,
        "audit_attempt": 1,
    }
    second = json.loads(await agent_master._run_master_proposal_ensemble(
        strict_checkpoint=advanced_checkpoint,
        **kwargs,
    ))
    assert second["valid"] is True
    retry_provider_slots = provider_slots[provider_count_before_retry:]
    assert "proposal:mechanism" not in retry_provider_slots
    assert "proposal:counterfactual" not in retry_provider_slots
    assert retry_provider_slots.count("proposal:compute_memory") == 1
    assert retry_provider_slots.count("ballot:falsification") == 1
    assert retry_provider_slots.count("ballot:scope") == 1
    assert len({path for _invocation_id, path in provider_logs}) == len({
        invocation_id for invocation_id, _path in provider_logs
    })
    assert all(
        path.parent.name == invocation_id
        and path.parent.parent.name == "strict_invocations"
        for invocation_id, path in provider_logs
    )

    _refs, errors = authority.validate_receipts(
        advanced_checkpoint,
        required_slots=authority.MASTER_SLOTS[:5],
        require_no_other_accepted=True,
    )
    assert errors == []
    accepted_revisions = {
        event.payload["checkpoint_revision"]
        for event in store.events(authority.authority_run_id(
            checkpoint["workflow_run_id"]
        ))
        if event.event_type == authority.ACCEPTED_EVENT
    }
    assert accepted_revisions == {10}

    # Complete the sixth MASTER slot against this exact evidence-bearing packet.
    # This is the crash boundary that matters in production: all provider and
    # schema effects are durable, but the outer checkpoint has not projected the
    # accepted plan yet.
    from runtime_architecture_policy import native_policy_runtime_contract
    from tests.test_master_success_return import _strict_prompt_plan

    architecture_policy = {
        "epoch": "national_tcp_policy_v1",
        "policy_abi": native_policy_runtime_contract()["policy_abi"],
    }
    selected = second["ordered_proposals"][0]
    final_plan = _strict_prompt_plan()
    final_plan["selected_proposal_id"] = selected["proposal_id"]
    final_plan["targeted_failure"] = selected["targeted_failure"]
    final_output = "```json\n" + json.dumps(final_plan) + "\n```\n"
    final_call = authority.new_call(
        advanced_checkpoint,
        slot="master:final",
        role="MASTER (Try 1)",
        context_binding=authority.final_master_call_context(
            second,
            architecture_policy,
        ),
    )
    authority.dispatch_call(
        final_call,
        full_prompt="sealed final Master prompt",
        tools=["Read"],
        owner="pytest-strict-final-master",
        actual_role="MASTER (Try 1)",
    )
    final_result = ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="strict-final-master",
        total_cost_usd=0.01,
        usage={"input_tokens": 1, "output_tokens": 1},
        result=final_output,
    )
    authority._observe_provider_result(
        final_result,
        invocation_id=final_call["invocation_id"],
        effect_id=final_call["effect_id"],
    )
    authority.complete_provider_call(
        final_call,
        raw_output=final_output,
        provider_results=[final_result],
    )
    accepted_final_plan = final_call["projected_role_result"]
    authority.accept_role_result(
        final_call,
        role_result=accepted_final_plan,
        parse_contract="master-plan-schema-v1",
    )
    _refs, errors = authority.validate_receipts(
        advanced_checkpoint,
        required_slots=authority.MASTER_SLOTS,
        require_no_other_accepted=True,
    )
    assert errors == []
    expected_contexts = authority.expected_master_contexts(
        accepted_final_plan
    )
    expected_contexts["master:final"] = authority.final_master_call_context(
        second,
        architecture_policy,
    )
    summary = authority.authority_summary(
        advanced_checkpoint,
        required_slots=authority.MASTER_SLOTS,
        expected_role_results=(
            authority.expected_master_role_results(accepted_final_plan)
        ),
        expected_context_bindings=expected_contexts,
        require_no_other_accepted=True,
    )
    assert set(summary["receipts"]) == set(authority.MASTER_SLOTS)

    # Advancing checkpoint metadata must replay all five packet roles without
    # appending their logs or minting new invocation evidence.  The exact packet
    # bytes, and therefore the master:final context digest, stay unchanged.
    packet_before = json.dumps(
        second,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    provider_count_before_full_replay = len(provider_slots)
    fully_advanced_checkpoint = {
        **checkpoint,
        "checkpoint_revision": 12,
        "audit_attempt": 2,
    }
    third = json.loads(await agent_master._run_master_proposal_ensemble(
        strict_checkpoint=fully_advanced_checkpoint,
        **kwargs,
    ))
    packet_after = json.dumps(
        third,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert packet_after == packet_before
    assert len(provider_slots) == provider_count_before_full_replay

    replay_final = authority.new_call(
        fully_advanced_checkpoint,
        slot="master:final",
        role="MASTER (Try 1)",
        context_binding=authority.final_master_call_context(
            third,
            architecture_policy,
        ),
    )
    assert replay_final["replay_provider"] is True
    assert replay_final["invocation_id"] == final_call["invocation_id"]
    authority.dispatch_call(
        replay_final,
        full_prompt="re-rendered final Master prompt after restart",
        tools=["Read"],
        owner="pytest-strict-final-master-replay",
        actual_role="MASTER (Try 1)",
    )
    replay_projection, replay_errors = (
        agent_master._project_strict_final_master_result(
            replay_final["replay_raw_output"],
            proposal_packet=third,
            architecture_policy=architecture_policy,
        )
    )
    assert replay_errors == []
    assert replay_projection == accepted_final_plan
    authority.accept_role_result(
        replay_final,
        role_result=replay_projection,
        parse_contract="master-plan-schema-v1",
    )
    assert len(provider_slots) == provider_count_before_full_replay

    bound_log = Path(next(iter(
        third["proposal_invocations"].values()
    ))["io_log_path"])
    with bound_log.open("a", encoding="utf-8") as handle:
        handle.write("\npost-binding mutation\n")
    with pytest.raises(
        authority.StrictAuthorityError,
        match="system_bootstrap_llm_invocation_log_digest_mismatch",
    ):
        await agent_master._run_master_proposal_ensemble(
            strict_checkpoint=fully_advanced_checkpoint,
            **kwargs,
        )
    assert len(provider_slots) == provider_count_before_full_replay


def test_proposal_id_is_stable_and_not_scout_identity(monkeypatch, tmp_path):
    import agent_master

    source_dir = tmp_path / "source"
    _write_source(source_dir)
    graph, _digest = agent_master._source_symbol_graph(source_dir)
    first = agent_master._validated_master_proposal(
        _proposal("mechanism", shared_claims=True),
        "mechanism",
        source_graph=graph,
        snapshot_dir=tmp_path,
    )
    second = agent_master._validated_master_proposal(
        _proposal("counterfactual", shared_claims=True),
        "counterfactual",
        source_graph=graph,
        snapshot_dir=tmp_path,
    )

    assert first is not None and second is not None
    assert first["proposal_id"] == second["proposal_id"]
    assert first["direction"] != second["direction"]

    risks_only = json.loads(
        _proposal("counterfactual", shared_claims=True)
        .split("```json\n", 1)[1]
        .rsplit("\n```", 1)[0]
    )
    risks_only["risks"] = (
        "Different advisory risk prose cannot manufacture an independent mechanism."
    )
    third = agent_master._validated_master_proposal(
        json.dumps(risks_only),
        "compute_memory",
        source_graph=graph,
        snapshot_dir=tmp_path,
    )
    assert third is not None
    assert third["proposal_id"] == first["proposal_id"]


def test_master_proposal_validation_rejects_override_and_fake_call_edge(tmp_path):
    import agent_master

    source_dir = tmp_path / "source"
    _write_source(source_dir)
    graph, _digest = agent_master._source_symbol_graph(source_dir)
    payload = json.loads(_proposal("mechanism").split("```json\n", 1)[1].rsplit("\n```", 1)[0])
    payload["branch_from"] = "v143"
    assert agent_master._validated_master_proposal(
        json.dumps(payload), "mechanism", source_graph=graph, snapshot_dir=tmp_path
    ) is None

    payload.pop("branch_from")
    payload["reachable_chain"] = [
        "policy.py:_choose_intent",
        "policy.py:get_baseline_decision",
    ]
    assert agent_master._validated_master_proposal(
        json.dumps(payload), "mechanism", source_graph=graph, snapshot_dir=tmp_path
    ) is None


def test_critic_validation_requires_complete_integer_criterion_ballots():
    import agent_master

    proposal_ids = {"a" * 16, "b" * 16}
    valid = _critic_output(agent_master, sorted(proposal_ids))
    assert agent_master._validated_proposal_critique(valid, proposal_ids) is not None

    malformed = json.loads(valid)
    malformed["ballots"][0]["scores"].pop("falsifiability")
    assert agent_master._validated_proposal_critique(
        json.dumps(malformed), proposal_ids
    ) is None

    for mutate in (
        lambda value: value.update({"unexpected": True}),
        lambda value: value["ballots"][0].update({"unexpected": True}),
        lambda value: value["ballots"][0].update({"reject": 1}),
        lambda value: value["ballots"][0].update({"reason": ["not", "text"]}),
    ):
        malformed = json.loads(valid)
        mutate(malformed)
        assert agent_master._validated_proposal_critique(
            json.dumps(malformed), proposal_ids
        ) is None


def test_final_master_binding_rejects_missing_id_and_unbound_files():
    import agent_master

    proposal = {
        "proposal_id": "a" * 16,
        "targeted_failure": "One exact evidence-bound reachable failure.",
        "target_files": ["policy.py"],
        "structural_change": "Replace one reachable branch with a bounded state mechanism.",
        "expected_diff": "Wire that mechanism through the existing sanitized action path.",
        "reachable_chain": [
            "policy.py:get_baseline_decision",
            "policy.py:_choose_intent",
        ],
        "falsifier": {
            "test_name": "test_bound_mechanism",
            "control": "Keep the parent state and deterministic seed fixed for comparison.",
            "intervention": "Enable only the selected mechanism on the same state and seed.",
            "expected_observation": "The selected action changes only under the intervention.",
        },
        "why_not_threshold_tuning": (
            "The change replaces state flow and its consumer instead of changing one number."
        ),
    }
    packet = {"ordered_proposals": [proposal]}
    assert agent_master._validate_final_proposal_binding(
        {"tasks": []}, packet
    ) == ["selected_proposal_id_must_be_one_string"]

    errors = agent_master._validate_final_proposal_binding({
        "selected_proposal_id": "a" * 16,
        "targeted_failure": proposal["targeted_failure"],
        "tasks": [{"target_files": ["opponent.py"]}],
    }, packet)
    assert errors == ["selected_proposal_target_files_not_writable:['policy.py']"]


def test_selected_proposal_is_compiled_into_worker_prompt(tmp_path):
    import agent_master

    source_dir = tmp_path / "source"
    _write_source(source_dir)
    graph, _digest = agent_master._source_symbol_graph(source_dir)
    proposal = agent_master._validated_master_proposal(
        _proposal("mechanism"),
        "mechanism",
        source_graph=graph,
        snapshot_dir=tmp_path,
    )
    assert proposal is not None
    plan = {
        "tasks": [{
            "worker_id": 1,
            "target_files": ["policy.py"],
            "files_allowed": ["policy.py"],
            "worker_prompt": "Implement the selected structural mechanism.",
        }]
    }

    bound = agent_master._bind_selected_proposal_workers(plan, proposal)
    contract = agent_master._selected_proposal_contract(proposal)
    prompt = bound["tasks"][0]["worker_prompt"]

    assert plan["tasks"][0]["worker_prompt"] not in {"", prompt}
    assert f"proposal_id={proposal['proposal_id']}" in prompt
    assert f"contract_digest={contract['contract_digest']}" in prompt
    assert proposal["structural_change"] in prompt
    assert proposal["falsifier"]["expected_observation"] in prompt
    assert "Do not substitute a threshold-only edit" in prompt


def test_final_packet_parser_rejects_claim_changed_after_id(tmp_path):
    import agent_master

    source_dir = tmp_path / "source"
    _write_source(source_dir)
    graph, source_digest = agent_master._source_symbol_graph(source_dir)
    proposals = [
        agent_master._validated_master_proposal(
            _proposal(direction),
            direction,
            source_graph=graph,
            snapshot_dir=tmp_path,
        )
        for direction in ("mechanism", "counterfactual", "compute_memory")
    ]
    assert all(proposal is not None for proposal in proposals)
    packet = {
        "schema_version": "master-proposal-packet-v2",
        "valid": True,
        "context_digest": "c" * 64,
        "source_code_digest": source_digest,
        "proposal_count": 3,
        "valid_critic_count": 2,
        "allowed_proposal_ids": [proposal["proposal_id"] for proposal in proposals],
        "ordered_proposals": proposals,
        "critic_reviews": [],
    }
    packet["ordered_proposals"][0]["structural_change"] += " tampered"

    parsed, errors = agent_master._parse_valid_proposal_packet(json.dumps(packet))

    assert parsed is None
    assert any(error.startswith("proposal_identity_mismatch:") for error in errors)
