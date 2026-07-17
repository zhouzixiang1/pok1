import hashlib
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


def _write_strength_snapshot(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "head_to_head.json").write_text(json.dumps({
        "national_v143 vs national_v144": {
            "games": 36,
            "a_wins": 14,
            "b_wins": 20,
            "draws": 2,
            "win_rate": 0.4167,
        },
    }), encoding="utf-8")


def _proposal(
    direction: str,
    *,
    shared_claims: bool = False,
    snapshot: bool = False,
    fresh: bool = False,
) -> str:
    label = "shared" if shared_claims else direction
    payload = {
        "targeted_failure": f"{label} identifies one repeated reachable decision failure.",
        "structural_change": f"{label} replaces the parent decision mechanism with one deadline-bounded path.",
        "counterfactual": f"{label} changes one input while holding cards, seed, and legality fixed.",
        "measurement": (
            "target=fixed_blueprint_control; "
            "primary=typed_falsifier_and_official_5_plus_3; "
            "expected_delta=not_applicable; samples=official_5_plus_3; "
            "uncertainty=no_strength_claim; secondary=none"
            if fresh else
            "target=national_v143; primary=complete_70_hand_wld; "
            "expected_delta=0.03; "
            "samples=>=30_complete_matches; uncertainty=wilson_wld_interval; "
            "secondary=net_chip_ci"
        ),
        "why_not_threshold_tuning": f"{label} changes state flow and a reachable consumer, not one number.",
        "mechanism_target": "deadline",
        "expected_diff": f"{label} changes get_baseline_decision through the existing _choose_intent call edge before the deadline.",
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
            "control": "Run the frozen parent with sample_count=1 before the deadline on the same state, cards, and deterministic seed.",
            "intervention": "Run only the proposed mechanism with a changed deadline on that identical canonical state.",
            "expected_observation": "The target action changes while the paired control remains unchanged.",
        },
        "evidence_refs": [
            "source:policy.py:get_baseline_decision",
            "source:policy.py:_choose_intent",
        ],
        "risks": "The mechanism may overfit sparse evidence and must remain bounded.",
    }
    if snapshot:
        payload["evidence_refs"].append(
            "snapshot:head_to_head.json#/national_v143 vs national_v144"
        )
    return "```json\n" + json.dumps(payload) + "\n```"


def _critic_output(
    agent_master,
    proposal_ids,
    *,
    reverse=False,
    reject_ids=(),
):
    ids = list(proposal_ids)
    if reverse:
        ids.reverse()
    rejected = set(reject_ids)
    ballots = []
    for index, proposal_id in enumerate(ids):
        ballots.append({
            "proposal_id": proposal_id,
            "scores": {
                criterion: 5 - min(index, 3)
                for criterion in agent_master._PROPOSAL_CRITIC_CRITERIA
            },
            "reject": proposal_id in rejected,
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
    _write_strength_snapshot(snapshot_dir)
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
        return _proposal(direction, snapshot=True), 0.0, {}

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
    assert packet["schema_version"] == "master-proposal-packet-v5"
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
    assert packet["authority"].startswith(
        "ballots_rank_and_unanimous_reject_vetoes"
    )


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
    assert (
        '- ["policy.py:get_baseline_decision","policy.py:_choose_intent"]'
        in first
    )
    assert "SYSTEM-VERIFIED PREFERRED CURRENT ENTRY ANCHORS" in first
    assert "policy.py:get_baseline_decision -> policy.py:_choose_intent" in first
    assert all(len(line) <= 130 for line in bounded.splitlines())
    assert len(bounded) <= 130


def test_preferred_current_chains_exclude_unreachable_policy_helpers():
    import agent_master

    graph = {
        "policy.py:get_baseline_decision": {"_live"},
        "policy.py:iter_decisions": set(),
        "policy.py:_live": {"card_id"},
        "policy.py:_dead_sorted_first": {"_live"},
        "precompute.py:card_id": set(),
    }
    prompt = agent_master._source_symbol_prompt_index(graph)
    preferred = prompt.split(
        "SYSTEM-VERIFIED PREFERRED CURRENT ENTRY ANCHORS", 1
    )[1].split("FULL VALIDATED EDGE INDEX", 1)[0]

    assert "policy.py:get_baseline_decision" in preferred
    assert "policy.py:_live" in preferred
    assert "policy.py:_dead_sorted_first" not in preferred


def test_preferred_anchors_prioritize_decision_mechanism_over_many_utilities():
    import agent_master

    utility_leaves = {f"_clamp_{index}" for index in range(10)}
    graph = {
        "policy.py:get_baseline_decision": utility_leaves | {"_route"},
        "policy.py:iter_decisions": set(),
        "policy.py:_route": {"_decision_from_equity"},
        "policy.py:_decision_from_equity": set(),
        **{
            f"policy.py:{leaf}": set()
            for leaf in utility_leaves
        },
    }

    prompt = agent_master._source_symbol_prompt_index(graph)
    preferred = prompt.split(
        "SYSTEM-VERIFIED PREFERRED CURRENT ENTRY ANCHORS", 1
    )[1].split("FULL VALIDATED EDGE INDEX", 1)[0]
    anchors = [line for line in preferred.splitlines() if line.startswith("- [")]

    assert len(anchors) == 8
    assert "policy.py:_decision_from_equity" in anchors[0]
    assert any("policy.py:_route" in line for line in anchors[:2])
    assert not all(
        any(f"policy.py:{leaf}" in line for line in anchors)
        for leaf in utility_leaves
    )


def test_source_graph_does_not_invent_edges_from_nested_or_class_scopes(tmp_path):
    import agent_master

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "policy.py").write_text(
        "def get_baseline_decision(context):\n"
        "    def nested():\n"
        "        return _dead(context)\n"
        "    callback = lambda: _dead(context)\n"
        "    return {'kind': 'pass'}\n\n"
        "def _dead(context):\n"
        "    return {'kind': 'fold'}\n\n"
        "class Helper:\n"
        "    def method(self, context):\n"
        "        return _dead(context)\n",
        encoding="utf-8",
    )

    graph, _digest = agent_master._source_symbol_graph(source_dir)

    assert graph["policy.py:get_baseline_decision"] == set()
    assert graph["policy.py:Helper"] == set()
    assert graph["policy.py:Helper.method"] == {"_dead"}
    assert "policy.py:_dead" not in agent_master._policy_abi_reachable_depths(
        graph
    )


def test_unreachable_dead_helper_chain_is_rejected_by_validator_and_hints(tmp_path):
    import agent_master

    graph = {
        "policy.py:get_baseline_decision": {"_live"},
        "policy.py:iter_decisions": set(),
        "policy.py:_live": set(),
        "policy.py:_dead": {"_live"},
    }
    payload = json.loads(
        _proposal("mechanism").split("```json\n", 1)[1].rsplit("\n```", 1)[0]
    )
    payload["source_symbols"] = ["policy.py:_dead", "policy.py:_live"]
    payload["reachable_chain"] = ["policy.py:_dead", "policy.py:_live"]
    payload["evidence_refs"] = [
        "source:policy.py:_dead",
        "source:policy.py:_live",
    ]
    raw = json.dumps(payload)

    assert agent_master._validated_master_proposal(
        raw,
        "mechanism",
        source_graph=graph,
        snapshot_dir=tmp_path,
        national_policy_only=True,
    ) is None
    assert (
        "proposal_reachable_chain_not_policy_abi_reachable"
        in agent_master._master_proposal_projection_hints(
            raw,
            source_graph=graph,
            snapshot_dir=tmp_path,
            national_policy_only=True,
        )
    )


def test_normal_proposal_requires_strength_snapshot_reference(tmp_path):
    import agent_master

    source_dir = tmp_path / "source"
    _write_source(source_dir)
    graph, _digest = agent_master._source_symbol_graph(source_dir)
    raw = _proposal("mechanism")

    assert agent_master._validated_master_proposal(
        raw,
        "mechanism",
        source_graph=graph,
        snapshot_dir=tmp_path / "snapshot",
        national_policy_only=True,
        require_snapshot_evidence=True,
        evidence_mode="frozen_strength_snapshot",
    ) is None
    assert "proposal_snapshot_evidence_required" in (
        agent_master._master_proposal_projection_hints(
            raw,
            source_graph=graph,
            snapshot_dir=tmp_path / "snapshot",
            national_policy_only=True,
            require_snapshot_evidence=True,
            evidence_mode="frozen_strength_snapshot",
        )
    )


def test_metadata_only_snapshot_pointer_is_rejected(tmp_path):
    import agent_master

    source_dir = tmp_path / "source"
    snapshot_dir = tmp_path / "snapshot"
    _write_source(source_dir)
    _write_strength_snapshot(snapshot_dir)
    snapshot_path = snapshot_dir / "head_to_head.json"
    snapshot_payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot_payload["schema_version"] = (
        "metadata-only-value-that-is-long-enough-to-look-substantive"
    )
    snapshot_path.write_text(json.dumps(snapshot_payload), encoding="utf-8")
    graph, _digest = agent_master._source_symbol_graph(source_dir)
    payload = json.loads(
        _proposal("mechanism").split("```json\n", 1)[1].rsplit("\n```", 1)[0]
    )
    metadata_ref = "snapshot:head_to_head.json#/schema_version"
    payload["evidence_refs"].append(metadata_ref)

    assert agent_master._validated_master_proposal(
        json.dumps(payload),
        "mechanism",
        source_graph=graph,
        snapshot_dir=snapshot_dir,
        national_policy_only=True,
        require_snapshot_evidence=True,
        evidence_mode="frozen_strength_snapshot",
    ) is None
    assert metadata_ref not in agent_master._snapshot_reference_prompt_index(
        snapshot_dir
    )


def test_strength_snapshot_node_is_digest_bound_with_resolved_projection(tmp_path):
    import agent_master

    source_dir = tmp_path / "source"
    snapshot_dir = tmp_path / "snapshot"
    _write_source(source_dir)
    _write_strength_snapshot(snapshot_dir)
    graph, _digest = agent_master._source_symbol_graph(source_dir)

    proposal = agent_master._validated_master_proposal(
        _proposal("mechanism", snapshot=True),
        "mechanism",
        source_graph=graph,
        snapshot_dir=snapshot_dir,
        national_policy_only=True,
        require_snapshot_evidence=True,
        evidence_mode="frozen_strength_snapshot",
    )

    assert proposal is not None
    assert len(proposal["snapshot_evidence"]) == 1
    binding = proposal["snapshot_evidence"][0]
    node = json.loads(
        (snapshot_dir / "head_to_head.json").read_text(encoding="utf-8")
    )["national_v143 vs national_v144"]
    canonical = json.dumps(
        node,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    projection = binding["resolved_projection"]
    assert binding["reference"] in proposal["evidence_refs"]
    assert binding["node_sha256"] == hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()
    assert binding["projection_sha256"] == hashlib.sha256(
        projection.encode("utf-8")
    ).hexdigest()
    assert '"games":36' in projection
    assert binding["projection_truncated"] is False


@pytest.mark.asyncio
async def test_no_strength_mode_rejects_fake_snapshot_reference(
    monkeypatch,
    tmp_path,
):
    import agent_master

    source_dir = tmp_path / "source"
    snapshot_dir = tmp_path / "snapshot"
    _write_source(source_dir)
    _write_strength_snapshot(snapshot_dir)
    roles = []

    async def fake_query(_prompt, _ctx, _ui, role_name, *_args, **_kwargs):
        roles.append(role_name)
        direction = next(
            name
            for name in ("mechanism", "counterfactual", "compute_memory")
            if name in role_name
        )
        return _proposal(direction, snapshot=True), 0.0, {}

    monkeypatch.setattr(agent_master, "get_bot_dir", lambda _v: source_dir)
    monkeypatch.setattr(agent_master, "run_claude_query", fake_query)
    packet = json.loads(await agent_master._run_master_proposal_ensemble(
        "singleton parent without published strength snapshot",
        source_v=143,
        next_v=144,
        ui=_UI(),
        log_dir=tmp_path,
        allowed_evidence_snapshot_dir=str(snapshot_dir),
        singleton_no_strength=True,
    ))

    assert packet["valid"] is False
    assert packet["reason"].endswith("got_0")
    assert len(roles) == 6
    assert not any("CRITIC" in role for role in roles)


def test_proposal_renderer_overrides_embedded_doc_reads_and_future_edges():
    import agent_master

    rendered = agent_master._render_master_proposal_provider_prompt({
        "planning_context": (
            "Embedded final-Master text says Read "
            "docs/official-terminal-settlement-oracle-2026-07-11.md and "
            "propose iter_decisions -> evaluate_seven as future wiring."
        ),
        "direction": "mechanism",
        "directive": "one structural mechanism",
        "source_v": 142,
        "next_v": 143,
        "protocol_bootstrap_prepared_only": True,
        "singleton_no_strength": False,
        "source_symbol_index": (
            "SYSTEM-VERIFIED PREFERRED CURRENT CHAINS\n"
            '- ["policy.py:get_baseline_decision","policy.py:_hole_ids"]'
        ),
        "repair_kind": "schema",
        "projection_hints": [
            "proposal_reachable_chain_edge_not_current",
        ],
        "invocation_id": "1" * 32,
    })
    prompt = rendered.text
    scope = prompt.rsplit("SCOUT TOOL/CHAIN SCOPE", 1)[1]

    assert "Do not call Read on any docs/" in scope
    assert "Use Read only inside the prepared target bots/national_v143/" in scope
    assert "Never use a future edge" in scope
    assert "A blocked Read grants no evidence" in scope
    assert "Copy one two-symbol SYSTEM-VERIFIED PREFERRED CURRENT ENTRY ANCHOR" in prompt
    assert "every chain symbol must also be present in source_symbols" in prompt
    assert "Every adjacent reachable_chain edge must exist in the current" in prompt
    assert "proposal_reachable_chain_edge_not_current" in prompt
    assert (
        "mechanism_target = row.mechanism_target = row.intervention_target = "
        "falsifier.intervention_target"
    ) in prompt
    assert "mechanism_target is NEVER the state_learning_primary label" in prompt
    assert '"mechanism_target":"opponent.rates"' in prompt
    assert "context['opponent']['rates'] does not replace" in prompt
    assert "opponent.rates.fold_to_raise belongs to the action-profile target" in prompt
    assert (
        "opponent.terminal_response.fold_to_raise belongs to the "
        "terminal-response target"
    ) in prompt

    normal = agent_master._render_master_proposal_provider_prompt({
        "planning_context": "Frozen snapshot path is evidence data only.",
        "direction": "mechanism",
        "directive": "one structural mechanism",
        "source_v": 143,
        "next_v": 144,
        "protocol_bootstrap_prepared_only": False,
        "singleton_no_strength": False,
        "source_symbol_index": "SYSTEM-VERIFIED SOURCE CALL INDEX",
        "repair_kind": "",
        "projection_hints": [],
        "invocation_id": "2" * 32,
    }).text.rsplit("SCOUT TOOL/CHAIN SCOPE", 1)[1]
    assert "bots/national_v143/" in normal
    assert "bots/national_v144/" in normal
    assert "one exact supplied frozen evidence snapshot" in normal
    assert "Other web/core/results paths" in normal

    many_hints = agent_master._render_master_proposal_provider_prompt({
        "planning_context": "Frozen facts.",
        "direction": "mechanism",
        "directive": "one structural mechanism",
        "source_v": 143,
        "next_v": 144,
        "protocol_bootstrap_prepared_only": False,
        "singleton_no_strength": False,
        "source_symbol_index": "SYSTEM-VERIFIED SOURCE CALL INDEX",
        "repair_kind": "schema",
        "projection_hints": [
            f"proposal_field_invalid:{index}" for index in range(17)
        ],
        "invocation_id": "3" * 32,
    }).text
    assert "proposal_field_invalid:16" in many_hints


@pytest.mark.parametrize(
    ("mutate", "expected"),
    (
        (
            lambda payload: (
                payload.pop("measurement"),
                payload.update({"measurement_plan": "wrong embedded schema"}),
            ),
            "proposal_required_text_invalid:measurement",
        ),
        (
            lambda payload: payload.update({
                "reachable_chain": [
                    "policy.py:_choose_intent",
                    "policy.py:get_baseline_decision",
                ],
            }),
            "proposal_reachable_chain_edge_not_current",
        ),
        (
            lambda payload: payload.update({
                "source_symbols": ["policy.py:get_baseline_decision"],
            }),
            "proposal_reachable_chain_member_not_in_source_symbols",
        ),
        (
            lambda payload: payload.update({
                "source_symbols": payload["source_symbols"] * 5,
            }),
            "proposal_source_symbols_count_invalid",
        ),
        (
            lambda payload: payload.update({
                "target_files": ["precompute.py"],
            }),
            "proposal_reachable_chain_target_file_missing",
        ),
        (
            lambda payload: payload["falsifier"].update({
                "test_name": "test_future_unbound_check",
            }),
            "proposal_falsifier_test_name_invalid",
        ),
        (
            lambda payload: payload.update({
                "evidence_refs": [
                    "source:policy.py:get_baseline_decision",
                    "source:policy.py:get_baseline_decision",
                ],
            }),
            "proposal_evidence_ref_invalid",
        ),
    ),
)
def test_proposal_projection_hints_explain_common_strict_rejections(
    tmp_path,
    mutate,
    expected,
):
    import agent_master

    source_dir = tmp_path / "source"
    _write_source(source_dir)
    graph, _digest = agent_master._source_symbol_graph(source_dir)
    payload = json.loads(
        _proposal("mechanism").split("```json\n", 1)[1].rsplit("\n```", 1)[0]
    )
    mutate(payload)
    raw = json.dumps(payload)

    assert agent_master._validated_master_proposal(
        raw,
        "mechanism",
        source_graph=graph,
        snapshot_dir=tmp_path,
        national_policy_only=True,
    ) is None
    hints = agent_master._master_proposal_projection_hints(
        raw,
        source_graph=graph,
        national_policy_only=True,
    )
    assert expected in hints


def test_valid_and_tolerantly_normalized_proposals_have_no_projection_hints(tmp_path):
    import agent_master

    source_dir = tmp_path / "source"
    _write_source(source_dir)
    graph, _digest = agent_master._source_symbol_graph(source_dir)
    payload = json.loads(
        _proposal("mechanism").split("```json\n", 1)[1].rsplit("\n```", 1)[0]
    )
    payload["source_symbols"][1] = "policy.py:_choose_inten"
    payload["reachable_chain"][1] = "policy.py:_choose_inten"
    payload["evidence_refs"] = {
        "entry": "code:policy.py:get_baseline_decision [current entry]",
        "consumer": "ref:policy.py:_choose_inten — fuzzy tolerated",
    }
    raw = json.dumps(payload)

    assert agent_master._validated_master_proposal(
        raw,
        "mechanism",
        source_graph=graph,
        snapshot_dir=tmp_path,
        national_policy_only=True,
    ) is not None
    assert agent_master._master_proposal_projection_hints(
        raw,
        source_graph=graph,
        national_policy_only=True,
    ) == []


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
    _write_strength_snapshot(snapshot_dir)
    observed = []

    async def fake_query(prompt, _ctx, _ui, role_name, *_args, **_kwargs):
        if role_name.startswith("MASTER PROPOSAL CRITIC"):
            ids = list(dict.fromkeys(re.findall(
                r'"proposal_id":"([0-9a-f]{16})"', prompt
            )))
            observed.append((role_name, ids))
            return _critic_output(agent_master, ids), 0.0, {}
        return _proposal(
            role_name.rsplit(" ", 1)[-1], snapshot=True
        ), 0.0, {}

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
async def test_two_critic_rejects_veto_one_proposal_and_parser_recomputes_ids(
    monkeypatch,
    tmp_path,
):
    import agent_master

    source_dir = tmp_path / "source"
    snapshot_dir = tmp_path / "snapshot"
    _write_source(source_dir)
    _write_strength_snapshot(snapshot_dir)
    vetoed = {"proposal_id": None}

    async def fake_query(prompt, _ctx, _ui, role_name, *_args, **_kwargs):
        if role_name.startswith("MASTER PROPOSAL CRITIC"):
            ids = set(re.findall(r'"proposal_id":"([0-9a-f]{16})"', prompt))
            if vetoed["proposal_id"] is None:
                vetoed["proposal_id"] = min(ids)
            return (
                _critic_output(
                    agent_master,
                    sorted(ids),
                    reject_ids={vetoed["proposal_id"]},
                ),
                0.0,
                {},
            )
        return _proposal(
            role_name.rsplit(" ", 1)[-1],
            snapshot=True,
        ), 0.0, {}

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
    assert len(packet["allowed_proposal_ids"]) == 2
    assert vetoed["proposal_id"] not in packet["allowed_proposal_ids"]
    assert packet["ordered_proposals"][-1]["proposal_id"] == vetoed["proposal_id"]
    parsed, errors = agent_master._parse_valid_proposal_packet(packet_text)
    assert parsed == packet
    assert errors == []

    tampered = json.loads(packet_text)
    tampered["allowed_proposal_ids"].append(vetoed["proposal_id"])
    parsed, errors = agent_master._parse_valid_proposal_packet(
        json.dumps(tampered)
    )
    assert parsed is None
    assert "proposal_packet_allowed_ids_veto_mismatch" in errors


@pytest.mark.asyncio
async def test_two_critic_rejects_of_all_three_fail_closed(monkeypatch, tmp_path):
    import agent_master

    source_dir = tmp_path / "source"
    snapshot_dir = tmp_path / "snapshot"
    _write_source(source_dir)
    _write_strength_snapshot(snapshot_dir)

    async def fake_query(prompt, _ctx, _ui, role_name, *_args, **_kwargs):
        if role_name.startswith("MASTER PROPOSAL CRITIC"):
            ids = set(re.findall(r'"proposal_id":"([0-9a-f]{16})"', prompt))
            return (
                _critic_output(agent_master, sorted(ids), reject_ids=ids),
                0.0,
                {},
            )
        return _proposal(
            role_name.rsplit(" ", 1)[-1],
            snapshot=True,
        ), 0.0, {}

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
    assert packet["reason"] == "all_three_proposals_unanimously_rejected"
    assert packet["allowed_proposal_ids"] == []


@pytest.mark.asyncio
async def test_ensemble_repairs_one_scout_and_critic_schema_failure(
    monkeypatch, tmp_path
):
    import agent_master

    source_dir = tmp_path / "source"
    snapshot_dir = tmp_path / "snapshot"
    _write_source(source_dir)
    _write_strength_snapshot(snapshot_dir)
    calls = []

    async def fake_query(prompt, _ctx, _ui, role_name, *_args, **_kwargs):
        calls.append((role_name, prompt))
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
            payload = json.loads(
                _proposal("mechanism", snapshot=True).split("```json\n", 1)[1].rsplit(
                    "\n```", 1
                )[0]
            )
            payload["measurement_plan"] = payload.pop("measurement")
            return json.dumps(payload), 0.0, {}
        direction = next(
            name
            for name in ("mechanism", "counterfactual", "compute_memory")
            if name in role_name
        )
        return _proposal(direction, snapshot=True), 0.0, {}

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
    roles = [role for role, _prompt in calls]
    assert len(roles) == 7
    assert "MASTER PROPOSAL mechanism SCHEMA RETRY" in roles
    assert "MASTER PROPOSAL CRITIC falsification SCHEMA RETRY" in roles
    mechanism_retry_prompt = next(
        prompt
        for role, prompt in calls
        if role == "MASTER PROPOSAL mechanism SCHEMA RETRY"
    )
    assert "proposal_required_text_invalid:measurement" in mechanism_retry_prompt


@pytest.mark.asyncio
async def test_duplicate_proposal_gets_one_causally_distinct_repair(
    monkeypatch, tmp_path
):
    import agent_master

    source_dir = tmp_path / "source"
    snapshot_dir = tmp_path / "snapshot"
    _write_source(source_dir)
    _write_strength_snapshot(snapshot_dir)
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
                return _proposal("counterfactual", snapshot=True), 0.0, {}
            return _proposal("mechanism", shared_claims=True, snapshot=True), 0.0, {}
        if "mechanism" in role_name:
            return _proposal("mechanism", shared_claims=True, snapshot=True), 0.0, {}
        return _proposal("compute_memory", snapshot=True), 0.0, {}

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
    _write_strength_snapshot(snapshot_dir)
    roles = []

    async def fake_query(_prompt, _ctx, _ui, role_name, *_args, **_kwargs):
        roles.append(role_name)
        if "mechanism" in role_name or "counterfactual" in role_name:
            return _proposal("mechanism", shared_claims=True, snapshot=True), 0.0, {}
        return _proposal("compute_memory", snapshot=True), 0.0, {}

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


def test_error_packet_parser_reports_only_the_primary_rejection():
    import agent_master

    reason = "three_distinct_schema_valid_scout_proposals_required:got_2"
    packet = agent_master._proposal_packet_error(
        reason,
        context_digest="c" * 64,
        source_code_digest="s" * 64,
    )

    parsed, errors = agent_master._parse_valid_proposal_packet(packet)

    assert parsed is None
    assert errors == [f"proposal_packet_invalid:{reason}"]
    assert "proposal_packet_evidence_mode_invalid" not in errors
    assert "proposal_packet_fields_mismatch" not in errors


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
            "master-proposal-v3",
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
                return _proposal("counterfactual", fresh=True), 0.0, {}
            return _proposal("mechanism", shared_claims=True, fresh=True), 0.0, {}
        if slot == "proposal:mechanism":
            return _proposal("mechanism", shared_claims=True, fresh=True), 0.0, {}
        return _proposal("compute_memory", fresh=True), 0.0, {}

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
                else _proposal("compute_memory", fresh=True)
            )
        elif slot.startswith("proposal:"):
            output = _proposal(slot.split(":", 1)[1], fresh=True)
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
        "singleton_no_strength": False,
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
    selected = next(
        item
        for item in second["ordered_proposals"]
        if item["falsifier"]["test_name"] == "fast_policy_baseline"
    )
    final_plan = _strict_prompt_plan()
    final_plan["selected_proposal_id"] = selected["proposal_id"]
    final_plan["targeted_failure"] = selected["targeted_failure"]
    final_plan["measurement_plan"] = selected["measurement"]
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
        tools=[],
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
        tools=[],
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
        "measurement": (
            "target=national_v143; primary=complete_70_hand_wld; "
            "expected_delta=0.03; samples=>=30_complete_matches; "
            "uncertainty=wilson_wld_interval; secondary=net_chip_ci"
        ),
        "target_files": ["policy.py"],
        "structural_change": "Replace one reachable branch with a deadline-bounded state mechanism.",
        "counterfactual": (
            "Hold the state, cards, legality, and seed fixed while toggling only "
            "the selected mechanism."
        ),
        "mechanism_target": "deadline",
        "expected_diff": "Wire that mechanism through the existing sanitized action path before the deadline.",
        "reachable_chain": [
            "policy.py:get_baseline_decision",
            "policy.py:_choose_intent",
        ],
        "source_symbols": [
            "policy.py:get_baseline_decision",
            "policy.py:_choose_intent",
        ],
        "falsifier": {
            "test_name": "fast_policy_baseline",
            "state_learning_primary": "sample_counted_candidate_batch",
            "intervention_target": "deadline",
            "control": "Keep the parent state and deterministic seed fixed for comparison.",
            "intervention": "Enable only the selected mechanism with a changed deadline on the same state and seed.",
            "expected_observation": "The selected action changes only under the intervention.",
        },
        "why_not_threshold_tuning": (
            "The change replaces state flow and its consumer instead of changing one number."
        ),
        "evidence_refs": [
            "source:policy.py:get_baseline_decision",
            "source:policy.py:_choose_intent",
        ],
        "risks": "The bounded mechanism may overfit and must preserve the legal fallback.",
    }
    packet = {
        "ordered_proposals": [proposal],
        "allowed_proposal_ids": [proposal["proposal_id"]],
    }
    assert agent_master._validate_final_proposal_binding(
        {"tasks": []}, packet
    ) == ["selected_proposal_id_must_be_one_string"]

    errors = agent_master._validate_final_proposal_binding({
            "selected_proposal_id": "a" * 16,
            "targeted_failure": proposal["targeted_failure"],
            "measurement_plan": proposal["measurement"],
        "tasks": [{"target_files": ["opponent.py"]}],
    }, packet)
    assert errors == ["selected_proposal_target_files_not_writable:['policy.py']"]


def test_proposal_falsifier_must_match_typed_state_learning_primary(tmp_path):
    import agent_master

    source_dir = tmp_path / "source"
    _write_source(source_dir)
    graph, _digest = agent_master._source_symbol_graph(source_dir)
    payload = json.loads(
        _proposal("mechanism").split("```json\n", 1)[1].rsplit("\n```", 1)[0]
    )
    payload["mechanism_target"] = "opponent.rates"
    payload["structural_change"] = (
        "Change only opponent.terminal_response.fold_to_raise in the live consumer."
    )
    payload["expected_diff"] = (
        "The decision changes only when opponent.terminal_response is changed."
    )
    payload["falsifier"].update({
        "test_name": "incremental_opponent_model",
        "state_learning_primary": "action_profile",
        "intervention_target": "opponent.rates",
        "intervention": (
            "Change only opponent.terminal_response.fold_to_raise in the paired state."
        ),
    })
    raw_mislabeled = json.dumps(payload)

    assert agent_master._validated_master_proposal(
        raw_mislabeled,
        "mechanism",
        source_graph=graph,
        snapshot_dir=tmp_path,
        national_policy_only=True,
    ) is None
    hints = agent_master._master_proposal_projection_hints(
        raw_mislabeled,
        source_graph=graph,
        snapshot_dir=tmp_path,
        national_policy_only=True,
    )
    assert any(
        item.startswith(
            "proposal_mechanism_target_missing_from_executable_fields:"
            "opponent.rates:"
        )
        for item in hints
    )
    assert (
        "proposal_mechanism_foreign_targets_in_executable_claim:"
        "opponent.terminal_response"
        in hints
    )

    payload["mechanism_target"] = "opponent.terminal_response"
    payload["structural_change"] = (
        "Consume opponent.terminal_response confidence through the bounded decision path."
    )
    payload["expected_diff"] = (
        "The paired intent changes only through opponent.terminal_response."
    )
    payload["falsifier"] = {
        "test_name": "terminal_response_adaptation",
        "state_learning_primary": "terminal_response",
        "intervention_target": "opponent.terminal_response",
        "control": (
            "Hold terminal_response confidence at the bounded prior for the paired state."
        ),
        "intervention": (
            "Change only opponent.terminal_response confidence in the paired decision context."
        ),
        "expected_observation": (
            "The typed intent changes only with the terminal_response confidence intervention."
        ),
    }
    accepted = agent_master._validated_master_proposal(
        json.dumps(payload),
        "mechanism",
        source_graph=graph,
        snapshot_dir=tmp_path,
        national_policy_only=True,
    )

    assert accepted is not None
    compilation = agent_master._selected_proposal_compilation_contract(accepted)
    assert compilation["state_learning_primary"] == "terminal_response"
    assert compilation["required_primary_checks"] == [
        "terminal_response_adaptation"
    ]


@pytest.mark.parametrize(
    "foreign_alias",
    (
        "terminal response",
        "terminal-response",
        "terminalresponse",
    ),
)
def test_typed_mechanism_rejects_natural_language_foreign_aliases(
    tmp_path,
    foreign_alias,
):
    import agent_master
    from output_schema import (
        STATE_LEARNING_INTERVENTION_TARGET_ALIASES,
        STATE_LEARNING_PRIMARY_INTERVENTION_TARGETS,
    )

    assert set(STATE_LEARNING_INTERVENTION_TARGET_ALIASES) == set(
        STATE_LEARNING_PRIMARY_INTERVENTION_TARGETS.values()
    )
    assert all(
        target in aliases
        for target, aliases in STATE_LEARNING_INTERVENTION_TARGET_ALIASES.items()
    )
    source_dir = tmp_path / "source"
    _write_source(source_dir)
    graph, _digest = agent_master._source_symbol_graph(source_dir)
    payload = json.loads(
        _proposal("mechanism").split("```json\n", 1)[1].rsplit("\n```", 1)[0]
    )
    payload.update({
        "mechanism_target": "opponent.rates",
        "structural_change": (
            "Route opponent.rates through the live decision consumer while the "
            f"actual mechanism varies {foreign_alias}."
        ),
        "expected_diff": (
            "The paired typed intent changes through opponent.rates only."
        ),
    })
    payload["falsifier"] = {
        "test_name": "incremental_opponent_model",
        "state_learning_primary": "action_profile",
        "intervention_target": "opponent.rates",
        "control": "Hold opponent rates at the bounded prior for the paired state.",
        "intervention": "Change only opponent.rates in the paired decision context.",
        "expected_observation": "The typed intent changes only under that rate intervention.",
    }

    assert agent_master._validated_master_proposal(
        json.dumps(payload),
        "mechanism",
        source_graph=graph,
        snapshot_dir=tmp_path,
        national_policy_only=True,
    ) is None
    hints = agent_master._master_proposal_projection_hints(
        json.dumps(payload),
        source_graph=graph,
        snapshot_dir=tmp_path,
        national_policy_only=True,
    )
    assert any(
        item.startswith(
            "proposal_mechanism_foreign_targets_in_executable_claim:"
            "opponent.terminal_response"
        )
        for item in hints
    )


def test_shared_fold_to_raise_leaf_is_bound_by_full_opponent_namespace(tmp_path):
    import agent_master

    source_dir = tmp_path / "source"
    _write_source(source_dir)
    graph, _digest = agent_master._source_symbol_graph(source_dir)
    payload = json.loads(
        _proposal("mechanism", fresh=True).split("```json\n", 1)[1].rsplit(
            "\n```", 1
        )[0]
    )
    payload.update({
        "mechanism_target": "opponent.rates",
        "structural_change": (
            "Route only opponent.rates.fold_to_raise through the bounded "
            "action_profile consumer in the live policy decision path."
        ),
        "expected_diff": (
            "The paired typed intent changes only when "
            "opponent.rates.fold_to_raise changes."
        ),
    })
    payload["falsifier"] = {
        "test_name": "incremental_opponent_model",
        "state_learning_primary": "action_profile",
        "intervention_target": "opponent.rates",
        "control": (
            "Hold opponent.rates.fold_to_raise at its bounded action-profile prior."
        ),
        "intervention": (
            "Change only opponent.rates.fold_to_raise in the paired decision context."
        ),
        "expected_observation": (
            "The typed intent changes only under that opponent action-profile "
            "intervention."
        ),
    }

    accepted = agent_master._validated_master_proposal(
        json.dumps(payload),
        "mechanism",
        source_graph=graph,
        snapshot_dir=tmp_path,
        national_policy_only=True,
        evidence_mode="fresh_strict_control_no_strength",
        execution_mode="fixed_blueprint_capability_audit",
        expected_measurement_target="fixed_blueprint_control",
    )
    assert accepted is not None

    payload["structural_change"] = (
        "Route opponent.rates through the live consumer while the actual "
        "mechanism varies opponent.terminal_response.fold_to_raise."
    )
    raw_foreign = json.dumps(payload)
    assert agent_master._validated_master_proposal(
        raw_foreign,
        "mechanism",
        source_graph=graph,
        snapshot_dir=tmp_path,
        national_policy_only=True,
        evidence_mode="fresh_strict_control_no_strength",
        execution_mode="fixed_blueprint_capability_audit",
        expected_measurement_target="fixed_blueprint_control",
    ) is None
    assert (
        "proposal_mechanism_foreign_targets_in_executable_claim:"
        "opponent.terminal_response"
        in agent_master._master_proposal_projection_hints(
            raw_foreign,
            source_graph=graph,
            snapshot_dir=tmp_path,
            national_policy_only=True,
            evidence_mode="fresh_strict_control_no_strength",
        )
    )

    payload["structural_change"] = (
        "Route only opponent.rates.fold_to_raise through the bounded "
        "action_profile consumer in the live policy decision path."
    )
    payload["expected_diff"] = (
        "The paired consumer reads context['opponent']['rates'] and changes the "
        "typed decision without emitting the exact target literal."
    )
    bracket_only = json.dumps(payload)
    assert any(
        item == (
            "proposal_mechanism_target_missing_from_executable_fields:"
            "opponent.rates:expected_diff"
        )
        for item in agent_master._master_proposal_projection_hints(
            bracket_only,
            source_graph=graph,
            snapshot_dir=tmp_path,
            national_policy_only=True,
            evidence_mode="fresh_strict_control_no_strength",
        )
    )


def test_selected_proposal_budget_boundary_and_primary_mapping_are_exact(tmp_path):
    import agent_master

    source_dir = tmp_path / "source"
    _write_source(source_dir)
    graph, _digest = agent_master._source_symbol_graph(source_dir)
    proposal = agent_master._validated_master_proposal(
        _proposal("mechanism"),
        "mechanism",
        source_graph=graph,
        snapshot_dir=tmp_path,
        national_policy_only=True,
    )
    assert proposal is not None
    packet = {
        "ordered_proposals": [proposal],
        "allowed_proposal_ids": [proposal["proposal_id"]],
    }
    root = Path(__file__).resolve().parents[2]
    prompt = (root / "web/core/prompts/master_prompt.md").read_text(encoding="utf-8")
    start = prompt.index('{\n  "analysis": "Strategic analysis as a single string.')
    end = prompt.index("\n\n- Do NOT include `branch_from`", start)
    plan = json.loads(prompt[start:end])
    plan.update({
        "selected_proposal_id": proposal["proposal_id"],
        "targeted_failure": proposal["targeted_failure"],
        "measurement_plan": proposal["measurement"],
    })
    compilation = agent_master._selected_proposal_compilation_contract(proposal)
    exact_limit = compilation["max_provider_chars"]
    plan["tasks"][0]["worker_prompt"] = "x" * exact_limit

    assert agent_master._validate_final_proposal_binding(plan, packet) == []

    plan["tasks"][0]["worker_prompt"] += "x"
    errors = agent_master._validate_final_proposal_binding(plan, packet)
    budget_error = next(
        item
        for item in errors
        if item.startswith("selected_proposal_worker_prompt_has_no_binding_budget:")
    )
    payload = json.loads(budget_error.split(":", 1)[1])
    assert payload == {
        "actual_provider_chars": exact_limit + 1,
        "character_metric": "python_unicode_code_points",
        "combined_chars": 12001,
        "global_cap_chars": 12000,
        "max_provider_chars": exact_limit,
        "overflow_chars": 1,
        "proposal_id": proposal["proposal_id"],
        "reserved_selected_contract_chars": compilation[
            "reserved_selected_contract_chars"
        ],
        "reserved_runtime_contract_max_chars": 2048,
        "separator_chars": 2,
        "worker_id": 1,
    }


def test_final_binding_rejects_wrong_primary_and_missing_typed_check(tmp_path):
    import agent_master

    source_dir = tmp_path / "source"
    _write_source(source_dir)
    graph, _digest = agent_master._source_symbol_graph(source_dir)
    payload = json.loads(
        _proposal("mechanism").split("```json\n", 1)[1].rsplit("\n```", 1)[0]
    )
    payload.update({
        "mechanism_target": "opponent.terminal_response",
        "structural_change": (
            "Route opponent.terminal_response through the bounded live decision consumer."
        ),
        "expected_diff": (
            "The typed decision changes only through opponent.terminal_response."
        ),
    })
    payload["falsifier"] = {
        "test_name": "terminal_response_adaptation",
        "state_learning_primary": "terminal_response",
        "intervention_target": "opponent.terminal_response",
        "control": "Hold opponent terminal-response confidence at its bounded prior.",
        "intervention": (
            "Change only opponent.terminal_response in the paired decision context."
        ),
        "expected_observation": (
            "The typed intent changes only under the terminal-response intervention."
        ),
    }
    proposal = agent_master._validated_master_proposal(
        json.dumps(payload),
        "mechanism",
        source_graph=graph,
        snapshot_dir=tmp_path,
        national_policy_only=True,
    )
    assert proposal is not None
    packet = {
        "ordered_proposals": [proposal],
        "allowed_proposal_ids": [proposal["proposal_id"]],
    }
    root = Path(__file__).resolve().parents[2]
    prompt = (root / "web/core/prompts/master_prompt.md").read_text(encoding="utf-8")
    start = prompt.index('{\n  "analysis": "Strategic analysis as a single string.')
    end = prompt.index("\n\n- Do NOT include `branch_from`", start)
    plan = json.loads(prompt[start:end])
    plan.update({
        "selected_proposal_id": proposal["proposal_id"],
        "targeted_failure": proposal["targeted_failure"],
        "measurement_plan": proposal["measurement"],
    })
    task = plan["tasks"][0]
    state_learning = task["runtime_contract"]["state_learning"]
    state_learning.update({
        "work_primitive": None,
        "profile_dimensions": ["action_profile"],
        "line_controls": [],
    })
    task["runtime_contract"]["reference_pack_id"] = ""
    task["checks_required"] = [
        "incremental_opponent_model",
        "terminal_response_adaptation",
    ]

    wrong_primary = agent_master._validate_final_proposal_binding(plan, packet)
    assert any(
        item.startswith(
            "selected_proposal_falsifier_not_bound_to_runtime_primary_check:"
        )
        and '"expected_state_learning_primary":"terminal_response"' in item
        and '"state_learning_primary":"action_profile"' in item
        for item in wrong_primary
    )

    state_learning["profile_dimensions"] = ["terminal_response"]
    task["checks_required"] = ["incremental_opponent_model"]
    missing_check = agent_master._validate_final_proposal_binding(plan, packet)
    assert any(
        item.startswith("selected_proposal_primary_checks_missing:")
        and '"missing_checks":["terminal_response_adaptation"]' in item
        for item in missing_check
    )

    task["checks_required"].append("terminal_response_adaptation")
    assert agent_master._validate_final_proposal_binding(plan, packet) == []


def test_oversized_scout_is_repaired_before_critics_and_packet_reproof(tmp_path):
    import agent_master
    from tests.test_master_success_return import _valid_proposal_packet

    source_dir = tmp_path / "source"
    snapshot_dir = tmp_path / "snapshot"
    _write_source(source_dir)
    _write_strength_snapshot(snapshot_dir)
    graph, _digest = agent_master._source_symbol_graph(source_dir)
    payload = json.loads(
        _proposal("mechanism", snapshot=True).split("```json\n", 1)[1].rsplit(
            "\n```", 1
        )[0]
    )
    payload.update({
        "targeted_failure": "failure " + "x" * 1592,
        "structural_change": "deadline " + "s" * 1591,
        "counterfactual": "counterfactual " + "c" * 1585,
        "why_not_threshold_tuning": "not threshold " + "n" * 1586,
        "expected_diff": "deadline " + "d" * 1591,
        "risks": "risk " + "r" * 1195,
    })
    payload["falsifier"].update({
        "control": "control " + "a" * 993,
        "intervention": "deadline " + "b" * 991,
        "expected_observation": "observation " + "o" * 988,
    })
    raw = json.dumps(payload)
    normalized_without_budget = agent_master._validated_master_proposal(
        raw,
        "mechanism",
        source_graph=graph,
        snapshot_dir=snapshot_dir,
        national_policy_only=True,
        require_snapshot_evidence=True,
        evidence_mode="frozen_strength_snapshot",
        enforce_bindability=False,
    )
    assert normalized_without_budget is not None
    assert agent_master._proposal_worker_bindability_error(
        normalized_without_budget
    ) is not None
    assert agent_master._validated_master_proposal(
        raw,
        "mechanism",
        source_graph=graph,
        snapshot_dir=snapshot_dir,
        national_policy_only=True,
        require_snapshot_evidence=True,
        evidence_mode="frozen_strength_snapshot",
    ) is None
    hints = agent_master._master_proposal_projection_hints(
        raw,
        source_graph=graph,
        snapshot_dir=snapshot_dir,
        national_policy_only=True,
        require_snapshot_evidence=True,
        evidence_mode="frozen_strength_snapshot",
    )
    assert any(
        item.startswith("proposal_worker_binding_cannot_fit_minimum_prompt:")
        for item in hints
    )

    packet = _valid_proposal_packet(
        agent_master,
        normalized_without_budget,
        tmp_path / "oversized_invocations",
        source_dir=source_dir,
    )
    parsed, packet_errors = agent_master._parse_valid_proposal_packet(
        json.dumps(packet)
    )
    assert parsed is None
    assert any(
        item.startswith("proposal_worker_binding_cannot_fit_minimum_prompt:")
        for item in packet_errors
    )


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
    assert "Do not substitute an unmeasured threshold-only edit" in prompt
    assert "generation hypothesis" in prompt


def test_selected_proposal_contract_digest_binds_counterfactual_and_measurement(
    tmp_path,
):
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
    baseline_digest = agent_master._selected_proposal_contract(
        proposal
    )["contract_digest"]

    changed_counterfactual = json.loads(json.dumps(proposal))
    changed_counterfactual["counterfactual"] += " Change only opponent posterior."
    changed_measurement = json.loads(json.dumps(proposal))
    changed_measurement["measurement"] = (
        "target=national_v143; primary=complete_70_hand_wld; "
        "expected_delta=0.05; samples=>=30_complete_matches; "
        "uncertainty=bootstrap_wld_interval; secondary=net_chip_ci"
    )

    assert agent_master._selected_proposal_contract(
        changed_counterfactual
    )["contract_digest"] != baseline_digest
    assert agent_master._selected_proposal_contract(
        changed_measurement
    )["contract_digest"] != baseline_digest


def test_fresh_worker_block_declares_fixed_blueprint_without_causal_claim(tmp_path):
    import agent_master

    source_dir = tmp_path / "source"
    _write_source(source_dir)
    graph, _digest = agent_master._source_symbol_graph(source_dir)
    proposal = agent_master._validated_master_proposal(
        _proposal("mechanism", fresh=True),
        "mechanism",
        source_graph=graph,
        snapshot_dir=None,
        national_policy_only=True,
        execution_mode="fixed_blueprint_capability_audit",
        evidence_mode="fresh_strict_control_no_strength",
    )
    assert proposal is not None

    block = agent_master._selected_proposal_worker_block(proposal)

    assert "execution_mode=fixed_blueprint_capability_audit" in block
    assert "fixed blueprint owns the v143 output bytes" in block
    assert "do not claim that its prose caused the implementation" in block
    assert "or proves poker strength" in block
    assert "Implement this one mechanism" not in block


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
        "schema_version": "master-proposal-packet-v5",
        "valid": True,
        "context_digest": "c" * 64,
        "source_code_digest": source_digest,
        "proposal_count": 3,
        "valid_critic_count": 2,
        "allowed_proposal_ids": [proposal["proposal_id"] for proposal in proposals],
        "ordered_proposals": proposals,
        "proposal_source_symbol_digests": {
            proposal["proposal_id"]: {
                symbol: "e" * 64 for symbol in proposal["source_symbols"]
            }
            for proposal in proposals
        },
        "critic_reviews": [],
    }
    malformed_falsifier = json.loads(json.dumps(packet))
    malformed_falsifier["ordered_proposals"][0]["falsifier"] = "not-an-object"
    parsed, errors = agent_master._parse_valid_proposal_packet(
        json.dumps(malformed_falsifier)
    )
    assert parsed is None
    assert any(error.startswith("proposal_falsifier_invalid:") for error in errors)

    packet["ordered_proposals"][0]["structural_change"] += " tampered"

    parsed, errors = agent_master._parse_valid_proposal_packet(json.dumps(packet))

    assert parsed is None
    assert any(error.startswith("proposal_identity_mismatch:") for error in errors)


@pytest.mark.parametrize(
    ("field", "invalid", "error_code"),
    (
        ("evidence_refs", 1, "proposal_packet_evidence_refs_shape_invalid:"),
        ("source_symbols", {}, "proposal_packet_source_symbols_shape_invalid:"),
        ("target_files", "policy.py", "proposal_packet_target_files_shape_invalid:"),
        ("reachable_chain", None, "proposal_packet_reachable_chain_shape_invalid:"),
        ("snapshot_evidence", 1, "proposal_packet_snapshot_evidence_shape_invalid:"),
    ),
)
def test_v5_packet_parser_is_total_for_malformed_proposal_collections(
    tmp_path,
    field,
    invalid,
    error_code,
):
    import agent_master
    from tests.test_master_success_return import (
        BOUND_PROPOSAL,
        _valid_proposal_packet,
    )

    packet = _valid_proposal_packet(
        agent_master,
        BOUND_PROPOSAL,
        tmp_path / f"malformed_packet_{field}",
        source_dir=agent_master.get_bot_dir(143),
    )
    packet["ordered_proposals"][0][field] = invalid

    parsed, errors = agent_master._parse_valid_proposal_packet(json.dumps(packet))

    assert parsed is None
    assert any(item.startswith(error_code) for item in errors)
    assert not any(item.startswith("proposal_packet_validation_error:") for item in errors)
