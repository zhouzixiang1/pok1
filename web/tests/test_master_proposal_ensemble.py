import json
import re

import pytest


class _UI:
    def clear_io(self):
        pass

    def log_history(self, *_args, **_kwargs):
        pass


def _write_source(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "strategy.py").write_text(
        "def get_action(state):\n"
        "    return choose_action(state)\n\n"
        "def choose_action(state):\n"
        "    return 'check'\n",
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
        "expected_diff": f"{label} changes get_action through the existing choose_action call edge.",
        "target_files": ["strategy.py"],
        "source_symbols": ["strategy.py:get_action", "strategy.py:choose_action"],
        "reachable_chain": ["strategy.py:get_action", "strategy.py:choose_action"],
        "falsifier": {
            "test_name": f"test_{label}_decision_path",
            "control": "Run the frozen parent on the same state, cards, and deterministic seed.",
            "intervention": "Run only the proposed mechanism on that identical canonical state.",
            "expected_observation": "The target action changes while the paired control remains unchanged.",
        },
        "evidence_refs": [
            "source:strategy.py:get_action",
            "source:strategy.py:choose_action",
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

    source_dir = tmp_path / "national_v142"
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
        source_v=142,
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
    assert len(calls) == 5
    scout_calls = calls[:3]
    assert all(call[2] == ["Read"] for call in scout_calls)
    assert all("allowed_evidence_snapshot_dir" in call[3] for call in scout_calls)
    assert all("SYSTEM-VERIFIED SOURCE CALL INDEX" in call[1] for call in scout_calls)
    assert all(
        "strategy.py:get_action -> strategy.py:choose_action" in call[1]
        for call in scout_calls
    )
    critic_calls = calls[3:]
    assert all(call[2] == [] for call in critic_calls)
    assert all('"direction":' not in call[1] for call in critic_calls)
    assert all("evidence_traceability" in call[1] for call in critic_calls)
    assert all("Planning context digest:" in call[1] for call in critic_calls)
    assert packet["authority"].startswith("advisory_only")


def test_source_symbol_prompt_index_is_deterministic_and_line_bounded(tmp_path):
    import agent_master

    source_dir = tmp_path / "source"
    _write_source(source_dir)
    graph, _digest = agent_master._source_symbol_graph(source_dir)

    first = agent_master._source_symbol_prompt_index(graph)
    second = agent_master._source_symbol_prompt_index(dict(reversed(list(graph.items()))))
    bounded = agent_master._source_symbol_prompt_index(graph, maximum_chars=130)

    assert first == second
    assert "strategy.py:get_action -> strategy.py:choose_action" in first
    assert all(len(line) <= 130 for line in bounded.splitlines())


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
        source_v=142,
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
        source_v=142,
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


def test_master_proposal_validation_rejects_override_and_fake_call_edge(tmp_path):
    import agent_master

    source_dir = tmp_path / "source"
    _write_source(source_dir)
    graph, _digest = agent_master._source_symbol_graph(source_dir)
    payload = json.loads(_proposal("mechanism").split("```json\n", 1)[1].rsplit("\n```", 1)[0])
    payload["branch_from"] = "v120"
    assert agent_master._validated_master_proposal(
        json.dumps(payload), "mechanism", source_graph=graph, snapshot_dir=tmp_path
    ) is None

    payload.pop("branch_from")
    payload["reachable_chain"] = [
        "strategy.py:choose_action",
        "strategy.py:get_action",
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


def test_final_master_binding_rejects_missing_id_and_unbound_files():
    import agent_master

    proposal = {
        "proposal_id": "a" * 16,
        "targeted_failure": "One exact evidence-bound reachable failure.",
        "target_files": ["strategy.py"],
        "structural_change": "Replace one reachable branch with a bounded state mechanism.",
        "expected_diff": "Wire that mechanism through the existing sanitized action path.",
        "reachable_chain": ["strategy.py:get_action", "strategy.py:choose_action"],
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
    assert errors == ["selected_proposal_target_files_not_writable:['strategy.py']"]


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
            "target_files": ["strategy.py"],
            "files_allowed": ["strategy.py"],
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
    proposal = agent_master._validated_master_proposal(
        _proposal("mechanism"),
        "mechanism",
        source_graph=graph,
        snapshot_dir=tmp_path,
    )
    assert proposal is not None
    packet = {
        "schema_version": "master-proposal-packet-v2",
        "valid": True,
        "context_digest": "c" * 64,
        "source_code_digest": source_digest,
        "proposal_count": 1,
        "valid_critic_count": 2,
        "allowed_proposal_ids": [proposal["proposal_id"]],
        "ordered_proposals": [proposal],
        "critic_reviews": [],
    }
    packet["ordered_proposals"][0]["structural_change"] += " tampered"

    parsed, errors = agent_master._parse_valid_proposal_packet(json.dumps(packet))

    assert parsed is None
    assert any(error.startswith("proposal_identity_mismatch:") for error in errors)
