"""Master success/failure coverage for ``national_tcp_policy_v1``.

The fixtures use the checked-in strict Master example: target v143+,
candidate-owned ``policy.py`` only, and typed decision-context intents.  The
fresh v143 bootstrap case additionally exercises the current durable authority
dispatch and no-strength receipt boundary.
"""

import json
import asyncio
from pathlib import Path
import re

import pytest


ROOT = Path(__file__).resolve().parents[2]

BOUND_TARGETED_FAILURE = (
    "The selected evidence-bound mechanism fixes one reachable parent decision failure."
)
BOUND_PROPOSAL = {
    "schema_version": "master-proposal-v3",
    "targeted_failure": BOUND_TARGETED_FAILURE,
    "structural_change": "Replace one reachable parent branch with a deadline-bounded mechanism.",
    "counterfactual": "Hold cards, seed, state, and legality fixed while toggling only the mechanism.",
    "measurement": (
        "target=national_v143; primary=complete_70_hand_wld; "
        "expected_delta=0.03; samples=>=30_complete_matches; "
        "uncertainty=wilson_wld_interval; secondary=net_chip_ci"
    ),
    "why_not_threshold_tuning": "The change replaces state flow and its consumer rather than one numeric cutoff.",
    "mechanism_target": "deadline",
    "expected_diff": "The existing get_baseline_decision to _choose_intent path consumes the new mechanism before the deadline.",
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
        "control": "The frozen parent keeps the original decision with sample_count=1 before the deadline on the paired state.",
        "intervention": "Only the selected deadline changes on the paired state.",
        "expected_observation": "The intervention changes the target action and control does not.",
    },
    "evidence_refs": [
        "source:policy.py:get_baseline_decision",
        "source:policy.py:_choose_intent",
    ],
    "risks": "Sparse evidence can overfit, so the mechanism and fallback remain bounded.",
}
PROPOSAL_ID = "set-by-stable-generation-evidence-fixture"


def _valid_proposal_packet(
    agent_master,
    selected_proposal,
    log_dir,
    *,
    evidence_mode="frozen_strength_snapshot",
    source_dir=None,
):
    import hashlib

    from system_strict_bootstrap import record_llm_invocation_evidence

    directions = ("mechanism", "counterfactual", "compute_memory")
    structural_changes = (
        selected_proposal["structural_change"],
        "Add a bounded state accumulator before the same reachable decision consumer.",
        "Add a deterministic paired-feature path into the same reachable decision consumer.",
    )
    snapshot_projection = json.dumps(
        {"games": 36, "wins": 14, "losses": 20, "draws": 2},
        sort_keys=True,
        separators=(",", ":"),
    )
    snapshot_binding = {
        "reference": (
            "snapshot:head_to_head.json#/national_v143 vs national_v144"
        ),
        "node_sha256": hashlib.sha256(
            snapshot_projection.encode("utf-8")
        ).hexdigest(),
        "resolved_projection": snapshot_projection,
        "projection_sha256": hashlib.sha256(
            snapshot_projection.encode("utf-8")
        ).hexdigest(),
        "projection_truncated": False,
    }
    proposals = []
    for index, (direction, structural_change) in enumerate(
        zip(directions, structural_changes), start=1
    ):
        proposal = json.loads(json.dumps(selected_proposal))
        proposal["execution_mode"] = (
            "fixed_blueprint_capability_audit"
            if evidence_mode == "fresh_strict_control_no_strength"
            else "strategy_implementation"
        )
        if evidence_mode == "frozen_strength_snapshot":
            proposal["snapshot_evidence"] = [snapshot_binding]
            proposal.setdefault("evidence_refs", []).append(
                snapshot_binding["reference"]
            )
        else:
            proposal["snapshot_evidence"] = []
        if evidence_mode == "fresh_strict_control_no_strength":
            proposal["measurement"] = (
                "target=fixed_blueprint_control; "
                "primary=typed_falsifier_and_official_5_plus_3; "
                "expected_delta=not_applicable; samples=official_5_plus_3; "
                "uncertainty=no_strength_claim; secondary=none"
            )
        proposal["direction"] = direction
        proposal["structural_change"] = structural_change
        if index > 1:
            proposal["expected_diff"] = (
                f"Independent alternative {index} reaches the existing decision consumer."
            )
            proposal["falsifier"]["test_name"] = (
                "incremental_opponent_model"
                if index == 2
                else "showdown_range_adaptation"
            )
            if index == 2:
                proposal["mechanism_target"] = "opponent.rates"
                proposal["structural_change"] += (
                    " Route the bounded change only through opponent.rates."
                )
                proposal["expected_diff"] += " The consumer reads opponent.rates."
                proposal["falsifier"].update({
                    "state_learning_primary": "action_profile",
                    "intervention_target": "opponent.rates",
                    "control": (
                        "Hold the decision context and opponent action_profile fixed "
                        "at the prior baseline."
                    ),
                    "intervention": (
                        "Change only opponent.rates action_profile aggression evidence "
                        "inside the same decision context."
                    ),
                    "expected_observation": (
                        "The typed intent changes only when the opponent action_profile "
                        "intervention is active."
                    ),
                })
            else:
                proposal["mechanism_target"] = "opponent.showdown_range"
                proposal["structural_change"] += (
                    " Route the bounded change only through opponent.showdown_range."
                )
                proposal["expected_diff"] += (
                    " The consumer reads opponent.showdown_range."
                )
                proposal["falsifier"].update({
                    "state_learning_primary": "showdown_range",
                    "intervention_target": "opponent.showdown_range",
                    "control": (
                        "Hold showdown_range confidence at the bounded prior on the "
                        "paired decision context."
                    ),
                    "intervention": (
                        "Change only opponent.showdown_range confidence for the paired state."
                    ),
                    "expected_observation": (
                        "The typed intent changes only with the showdown_range "
                        "confidence intervention."
                    ),
                })
        proposal["proposal_id"] = agent_master._proposal_identity(proposal)
        proposals.append(proposal)
    proposal_ids = [proposal["proposal_id"] for proposal in proposals]

    log_dir.mkdir(parents=True, exist_ok=True)

    def invocation(index, *, purpose, role, role_result):
        return record_llm_invocation_evidence(
            invocation_id=f"{index:032x}",
            purpose=purpose,
            role=role,
            prompt_digest=hashlib.sha256(f"prompt:{index}".encode()).hexdigest(),
            raw_output_digest=hashlib.sha256(f"output:{index}".encode()).hexdigest(),
            result_digest=hashlib.sha256(f"result:{index}".encode()).hexdigest(),
            role_result=role_result,
            log_file=log_dir / f"invocation_{index}.txt",
        )

    proposal_invocations = {
        proposal["proposal_id"]: invocation(
            index,
            purpose=f"master_proposal_scout:{proposal['direction']}",
            role=f"MASTER PROPOSAL {proposal['direction']}",
            role_result=proposal,
        )
        for index, proposal in enumerate(proposals, start=1)
    }
    reviews = []
    proposal_id_set = set(proposal_ids)
    for index, critic_id in enumerate(("falsification", "scope"), start=4):
        raw_review = {
            "ballots": [
                {
                    "proposal_id": proposal_id,
                    "scores": {
                        criterion: 5
                        for criterion in agent_master._PROPOSAL_CRITIC_CRITERIA
                    },
                    "reject": False,
                    "reason": "The proposal is traceable, reachable, bounded, and falsifiable.",
                }
                for proposal_id in proposal_ids
            ]
        }
        review = agent_master._validated_proposal_critique(
            json.dumps(raw_review), proposal_id_set
        )
        assert review is not None
        review["critic_id"] = critic_id
        review["invocation_evidence"] = invocation(
            index,
            purpose=f"master_proposal_critic:{critic_id}",
            role=f"MASTER PROPOSAL CRITIC {critic_id}",
            role_result={key: value for key, value in review.items() if key != "critic_id"},
        )
        reviews.append(review)
    try:
        source_symbol_digests = agent_master._proposal_source_symbol_digests(
            proposals,
            source_dir if source_dir is not None else agent_master.get_bot_dir(143),
        )
    except (OSError, ValueError, KeyError):
        source_symbol_digests = {
            proposal["proposal_id"]: {
                symbol: hashlib.sha256(
                    f"test-baseline:{symbol}".encode("utf-8")
                ).hexdigest()
                for symbol in proposal["source_symbols"]
            }
            for proposal in proposals
        }
    return {
        "schema_version": "master-proposal-packet-v5",
        "valid": True,
        "authority": "ballots_rank_and_unanimous_reject_vetoes",
        "context_digest": "c" * 64,
        "source_code_digest": "d" * 64,
        "evidence_mode": evidence_mode,
        "proposal_count": 3,
        "valid_critic_count": 2,
        "critic_criteria": agent_master._PROPOSAL_CRITIC_CRITERIA,
        "allowed_proposal_ids": proposal_ids,
        "ordered_proposals": proposals,
        "proposal_source_symbol_digests": source_symbol_digests,
        "proposal_invocations": proposal_invocations,
        "critic_reviews": reviews,
    }

def _strict_prompt_plan() -> dict:
    prompt = (ROOT / "web/core/prompts/master_prompt.md").read_text(encoding="utf-8")
    start = prompt.index('{\n  "analysis": "Strategic analysis as a single string.')
    end = prompt.index("\n\n- Do NOT include `branch_from`", start)
    plan = json.loads(prompt[start:end])
    plan["targeted_failure"] = BOUND_TARGETED_FAILURE
    plan["selected_proposal_id"] = PROPOSAL_ID
    return plan


VALID_PLAN = _strict_prompt_plan()
VALID_PLAN["measurement_plan"] = BOUND_PROPOSAL["measurement"]


def _mock_llm_output():
    # Realistic: model wraps JSON in a ```json fence.
    return "```json\n" + json.dumps(VALID_PLAN) + "\n```\n"


class _MockUI:
    def __init__(self):
        self.history = []

    def clear_io(self):
        pass

    def log_history(self, msg, level="info"):
        self.history.append((level, msg))

    def get_output(self):
        return ""

    def update_cost(self, *a, **kw):
        pass


@pytest.fixture(autouse=True)
def _stable_generation_evidence(monkeypatch, tmp_path):
    import agent_master
    import evidence_snapshot

    fixture_bots = tmp_path / "_fixture_bots"
    baseline_policy = (
        "def get_baseline_decision(context):\n"
        "    return _choose_intent(context)\n\n"
        "def _choose_intent(context):\n"
        "    return {'kind': 'pass'}\n\n"
        "def iter_decisions(context, baseline=None, budget_ms=0):\n"
        "    return baseline\n"
    )
    def fixture_bot_dir(version):
        root = fixture_bots / f"national_v{int(version)}"
        root.mkdir(parents=True, exist_ok=True)
        policy_path = root / "policy.py"
        if not policy_path.exists():
            policy_path.write_text(baseline_policy, encoding="utf-8")
        return root

    monkeypatch.setattr(
        agent_master,
        "get_bot_dir",
        fixture_bot_dir,
    )

    snapshot_dir = tmp_path / "evidence_snapshot"
    snapshot_dir.mkdir()
    manifest_path = snapshot_dir / "manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    identity = {
        "available": True,
        "h2h_relpath": "web/core/results/v144/evidence_snapshot/head_to_head.json",
        "selection_relpath": "web/core/results/v144/evidence_snapshot/selection_snapshot.json",
        "manifest_path": str(manifest_path),
        "manifest_relpath": "web/core/results/v144/evidence_snapshot/manifest.json",
        "manifest_digest": "a" * 64,
        "sha256": "b" * 64,
        "cycle": {"manifest_digest": "c" * 64, "save_num": 1},
    }
    monkeypatch.setattr(
        evidence_snapshot,
        "load_generation_snapshot_identity",
        lambda _next_v: dict(identity),
    )
    monkeypatch.setattr(
        evidence_snapshot,
        "h2h_snapshot_contract_text",
        lambda *_args, **_kwargs: "Stable test evaluation snapshot contract.",
    )
    BOUND_PROPOSAL["direction"] = "mechanism"
    BOUND_PROPOSAL["proposal_id"] = agent_master._proposal_identity(BOUND_PROPOSAL)
    global PROPOSAL_ID
    PROPOSAL_ID = BOUND_PROPOSAL["proposal_id"]
    VALID_PLAN["selected_proposal_id"] = PROPOSAL_ID

    frozen_packet_payload = _valid_proposal_packet(
        agent_master,
        BOUND_PROPOSAL,
        tmp_path / "proposal_invocations",
        source_dir=fixture_bot_dir(143),
    )
    PROPOSAL_ID = frozen_packet_payload["ordered_proposals"][0]["proposal_id"]
    VALID_PLAN["selected_proposal_id"] = PROPOSAL_ID
    frozen_packet = json.dumps(frozen_packet_payload)

    async def no_ensemble(*_args, **_kwargs):
        return frozen_packet

    monkeypatch.setattr(agent_master, "_run_master_proposal_ensemble", no_ensemble)


@pytest.mark.asyncio
async def test_master_returns_valid_plan_on_first_try(monkeypatch):
    import agent_master

    call_count = {"n": 0}
    captured_prompts = []
    captured_kwargs = []

    async def fake_run_claude_query(prompt, ctx, ui, role_name, log_file, tools=None, **_kwargs):
        call_count["n"] += 1
        captured_prompts.append(prompt)
        captured_kwargs.append({"tools": tools, **_kwargs})
        return _mock_llm_output(), 0.0, {}

    # Patch the name as bound in agent_master's namespace (imported at top).
    monkeypatch.setattr(agent_master, "run_claude_query", fake_run_claude_query)

    ui = _MockUI()
    result = await agent_master._run_master_analysis(
        source_v=143, next_v=144, stagnation_info="declining", ui=ui
    )

    # 1. A valid plan must be returned (was None before the fix).
    assert result is not None, "Master discarded a valid plan (missing success-path return)"
    assert "tasks" in result, "Returned object is not a plan"
    assert len(result["tasks"]) == 1
    assert result["tasks"][0]["worker_id"] == 1

    # 2. Must succeed on the FIRST LLM call (was 3 before the fix).
    assert call_count["n"] == 1, (
        f"Master called the LLM {call_count['n']}x for a valid plan; expected 1 "
        "(a valid plan must return immediately, not retry-and-discard)"
    )
    rendered_prompt = captured_prompts[0]
    assert "{master_plan_executable_contract}" not in rendered_prompt
    assert "System-owned executable Master-plan contract" in rendered_prompt
    assert "tasks: 1..3 items" in rendered_prompt
    assert 'writable scope is exactly ["policy.py"]' in rendered_prompt
    assert 'build_phase="module_import"' in rendered_prompt
    assert "runtime_contract.match_memory" in rendered_prompt
    assert 'snapshot_field="opponent"' in rendered_prompt
    assert (
        "state_learning primary 'sample_counted_candidate_batch' requires "
        'checks_required: "fast_policy_baseline", "incremental_refinement_protocol", '
        '"budget_scaled_refinement".'
    ) in rendered_prompt
    assert "SYSTEM-DERIVED PER-PROPOSAL COMPILATION CONTRACTS" in rendered_prompt
    assert '"character_metric":"python_unicode_code_points"' in rendered_prompt
    assert '"max_provider_chars":' in rendered_prompt
    assert "National TCP policy capability contract:" in rendered_prompt
    assert "national_runtime_feedback_summary() got an unexpected" not in rendered_prompt
    assert captured_kwargs[0]["tools"] == []
    assert captured_kwargs[0].get("allowed_read_dirs") is None
    assert captured_kwargs[0].get("allowed_evidence_snapshot_dir") is None


@pytest.mark.asyncio
async def test_master_binds_duplicate_selected_metadata_without_retry(
    monkeypatch,
):
    import agent_master

    invalid = json.loads(json.dumps(VALID_PLAN))
    invalid["measurement_plan"] = (
        "target=national_v143; primary=complete_70_hand_wld; "
        "expected_delta=0.99; samples=>=30_complete_matches; "
        "uncertainty=wilson_wld_interval; secondary=net_chip_ci"
    )
    prompts = []

    async def fake_run_claude_query(
        prompt,
        _ctx,
        _ui,
        _role_name,
        _log_file,
        **_kwargs,
    ):
        prompts.append(str(prompt))
        plan = invalid if len(prompts) == 1 else VALID_PLAN
        return "```json\n" + json.dumps(plan) + "\n```", 0.0, {}

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(agent_master, "run_claude_query", fake_run_claude_query)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    ui = _MockUI()
    result = await agent_master._run_master_analysis(
        source_v=143,
        next_v=144,
        stagnation_info="declining",
        ui=ui,
    )

    assert result is not None
    assert len(prompts) == 1
    assert result["measurement_plan"] == BOUND_PROPOSAL["measurement"]
    assert any(
        "selected-proposal metadata was rebound" in message
        for _level, message in ui.history
    )


@pytest.mark.asyncio
async def test_v51_style_master_binding_overflow_gets_bounded_repair_and_stays_inline(
    monkeypatch,
    tmp_path,
):
    import agent_master
    import plan_compiler

    invalid = json.loads(json.dumps(VALID_PLAN))
    invalid["tasks"][0]["worker_prompt"] = "x" * 12000
    prompts = []

    async def fake_run_claude_query(
        prompt,
        _ctx,
        _ui,
        _role_name,
        _log_file,
        **_kwargs,
    ):
        prompts.append(str(prompt))
        plan = invalid if len(prompts) == 1 else VALID_PLAN
        return "```json\n" + json.dumps(plan) + "\n```", 0.0, {}

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(agent_master, "run_claude_query", fake_run_claude_query)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    result = await agent_master._run_master_analysis(
        source_v=143,
        next_v=144,
        stagnation_info="declining",
        ui=_MockUI(),
    )

    assert result is not None
    assert len(prompts) == 2
    match = re.search(
        r"selected_proposal_worker_prompt_has_no_binding_budget:(\{[^\n]+\})",
        prompts[1],
    )
    assert match is not None
    payload = json.loads(match.group(1))
    assert payload["actual_provider_chars"] == 12000
    assert payload["global_cap_chars"] == 12000
    assert payload["max_provider_chars"] == (
        12000
        - payload["reserved_selected_contract_chars"]
        - payload["reserved_runtime_contract_max_chars"]
        - 2
    )
    assert payload["combined_chars"] == (
        12000
        + payload["reserved_selected_contract_chars"]
        + payload["reserved_runtime_contract_max_chars"]
        + 2
    )
    assert payload["overflow_chars"] == 12000 - payload["max_provider_chars"]

    # The retry returned a schema-valid plan.  It must now remain losslessly
    # inline through the default compiler instead of crossing a hidden 10k
    # compaction threshold after Master acceptance.
    compiled, meta = plan_compiler.compile_master_plan(
        result,
        next_v=144,
        target_dir=tmp_path / "national_v144",
        project_root=tmp_path,
    )
    assert meta["hard_prompt_chars"] == 12000
    assert meta["compiled"] is False
    assert "plan_compiler" not in compiled
    assert not (tmp_path / "national_v144" / ".task_context").exists()


def test_strict_projection_rejects_malformed_provider_worker_prompts(
    monkeypatch,
    tmp_path,
):
    import agent_master

    packet = _valid_proposal_packet(
        agent_master,
        BOUND_PROPOSAL,
        tmp_path / "strict_prompt_type_invocations",
        source_dir=agent_master.get_bot_dir(143),
    )
    selected = packet["ordered_proposals"][0]
    base_plan = json.loads(json.dumps(VALID_PLAN))
    base_plan.update({
        "selected_proposal_id": selected["proposal_id"],
        "targeted_failure": selected["targeted_failure"],
        "measurement_plan": selected["measurement"],
    })
    architecture_policy = {"schema_version": 1}

    for invalid in (None, [], {}, "", "x" * 19, " " * 20):
        plan = json.loads(json.dumps(base_plan))
        plan["tasks"][0]["worker_prompt"] = invalid
        projected, errors = agent_master._project_strict_final_master_result(
            json.dumps(plan),
            proposal_packet=packet,
            architecture_policy=architecture_policy,
        )
        assert projected is None
        expected_code = (
            "selected_proposal_worker_prompt_type_invalid:"
            if not isinstance(invalid, str)
            else "selected_proposal_worker_prompt_below_minimum:"
        )
        assert any(item.startswith(expected_code) for item in errors)
        bound = agent_master._bind_selected_proposal_workers(plan, selected)
        assert bound["tasks"][0]["worker_prompt"] == invalid


def test_strict_projection_binds_duplicate_selected_metadata(tmp_path):
    import agent_master

    packet = _valid_proposal_packet(
        agent_master,
        BOUND_PROPOSAL,
        tmp_path / "strict_metadata_binding_invocations",
        source_dir=agent_master.get_bot_dir(143),
    )
    selected = packet["ordered_proposals"][0]
    plan = json.loads(json.dumps(VALID_PLAN))
    plan.update({
        "selected_proposal_id": selected["proposal_id"],
        "targeted_failure": "Provider paraphrase must have no authority.",
        "measurement_plan": "Provider measurement paraphrase must have no authority.",
    })

    projected, errors = agent_master._project_strict_final_master_result(
        json.dumps(plan),
        proposal_packet=packet,
        architecture_policy={"schema_version": 1},
    )

    assert errors == []
    assert projected is not None
    assert projected["targeted_failure"] == selected["targeted_failure"]
    assert projected["measurement_plan"] == selected["measurement"]


def test_v51_10017_char_strict_master_prompt_stays_inline_and_replays(
    monkeypatch,
    tmp_path,
):
    """A v51-sized bound prompt has one lossless strict replay shape.

    v51 accepted a 10,017-character bound prompt under the visible 12,000
    character contract, then an unrelated hidden 10,000-character compiler
    threshold externalized it and caused the strict projection mismatch.  The
    default compiler must retain this exact inline authority form; only an
    explicit non-strict caller may request compaction.
    """

    import agent_master
    import plan_compiler
    import strict_authority_workflow as strict
    from runtime_architecture_policy import attach_runtime_contract_ledger
    from tool_planning import _normalize_master_plan_paths

    packet = _valid_proposal_packet(
        agent_master,
        BOUND_PROPOSAL,
        tmp_path / "strict_replay_invocations",
        source_dir=agent_master.get_bot_dir(143),
    )
    selected = packet["ordered_proposals"][0]
    provider_plan = json.loads(json.dumps(VALID_PLAN))
    provider_plan.update({
        "selected_proposal_id": selected["proposal_id"],
        "targeted_failure": selected["targeted_failure"],
        "measurement_plan": selected["measurement"],
    })
    base_prompt = provider_plan["tasks"][0]["worker_prompt"]
    # Derive the deterministic system-owned binding delta first, then choose a
    # provider-owned suffix that produces the exact v51 post-bind length.
    base_accepted, base_errors = agent_master._project_strict_final_master_result(
        json.dumps(provider_plan),
        proposal_packet=packet,
        architecture_policy={"schema_version": 1},
    )
    assert base_errors == []
    assert base_accepted is not None
    binding_delta = (
        len(base_accepted["tasks"][0]["worker_prompt"]) - len(base_prompt)
    )
    provider_budget = agent_master._selected_proposal_compilation_contract(
        selected
    )["max_provider_chars"]
    target_bound_chars = 10_017
    provider_chars = target_bound_chars - binding_delta
    assert len(base_prompt) <= provider_chars <= provider_budget
    provider_plan["tasks"][0]["worker_prompt"] = (
        base_prompt
        + "x" * (provider_chars - len(base_prompt))
    )
    architecture_policy = {"schema_version": 1}
    accepted_final, errors = agent_master._project_strict_final_master_result(
        json.dumps(provider_plan),
        proposal_packet=packet,
        architecture_policy=architecture_policy,
    )
    assert errors == []
    assert accepted_final is not None
    assert len(accepted_final["tasks"][0]["worker_prompt"]) == target_bound_chars
    assert target_bound_chars <= plan_compiler.HARD_WORKER_PROMPT_CHARS

    project_root = tmp_path / "operator"
    candidate_dir = project_root / "bots" / "national_v143"
    candidate_dir.mkdir(parents=True)
    production_input, _normalization = _normalize_master_plan_paths(
        {**accepted_final, "architecture_policy": architecture_policy},
        142,
        143,
    )
    assert isinstance(production_input, dict)
    production_plan, compiler = plan_compiler.compile_master_plan(
        production_input,
        next_v=143,
        target_dir=candidate_dir,
        project_root=project_root,
    )
    assert compiler["hard_prompt_chars"] == 12000
    assert compiler["compiled"] is False, json.dumps(compiler, sort_keys=True)
    assert compiler["contract_binding"]["bound"] is True
    assert not (candidate_dir / ".task_context").exists()
    production_plan = attach_runtime_contract_ledger(
        production_plan,
        replace=True,
    )

    accepted_event = type("AcceptedEvent", (), {
        "payload": {
            "slot": "master:final",
            "role_result": accepted_final,
            "role_result_digest": strict.content_digest(accepted_final),
        },
    })()
    monkeypatch.setattr(
        strict,
        "validate_receipts",
        lambda *_args, **_kwargs: ({"master:final": {"receipt_digest": "r"}}, []),
    )
    monkeypatch.setattr(strict, "expected_master_role_results", lambda _plan: {})
    monkeypatch.setattr(strict, "expected_master_contexts", lambda _plan: {})
    monkeypatch.setattr(
        strict,
        "expected_master_invocation_evidence",
        lambda _plan: {},
    )
    monkeypatch.setattr(strict, "_accepted_events", lambda _checkpoint: ([accepted_event], []))

    proof, replay_errors = strict.validate_master_final_projection(
        {"source_v": 142, "next_v": 143},
        production_plan,
        candidate_dir=candidate_dir,
        project_root=project_root,
    )

    assert replay_errors == []
    assert proof["compiled_plan_digest"] == strict.content_digest(production_plan)

    # Defense in depth: even a caller that manufactures a task-brief-shaped
    # checkpoint cannot route it through strict projection.
    externalized = json.loads(json.dumps(production_plan))
    externalized["tasks"][0]["worker_prompt_compiled"] = True
    externalized["tasks"][0]["task_brief_file"] = ".task_context/w1.md"
    _proof, externalized_errors = strict.validate_master_final_projection(
        {"source_v": 142, "next_v": 143},
        externalized,
        candidate_dir=candidate_dir,
        project_root=project_root,
    )
    assert externalized_errors == [
        "strict_authority_master_projection_externalization_forbidden"
    ]


def test_strict_projection_rejects_all_provider_reserved_markers(tmp_path):
    import agent_master
    import plan_compiler

    packet = _valid_proposal_packet(
        agent_master,
        BOUND_PROPOSAL,
        tmp_path / "strict_reserved_marker_invocations",
        source_dir=agent_master.get_bot_dir(143),
    )
    selected = packet["ordered_proposals"][0]
    for marker in (
        plan_compiler.SELECTED_PROPOSAL_BEGIN,
        plan_compiler.SELECTED_PROPOSAL_END,
        plan_compiler.SYSTEM_OWNED_CONTRACT_BEGIN,
        plan_compiler.SYSTEM_OWNED_CONTRACT_END,
    ):
        plan = json.loads(json.dumps(VALID_PLAN))
        plan.update({
            "selected_proposal_id": selected["proposal_id"],
            "targeted_failure": selected["targeted_failure"],
            "measurement_plan": selected["measurement"],
        })
        original = plan["tasks"][0]["worker_prompt"] + "\n" + marker
        plan["tasks"][0]["worker_prompt"] = original

        projected, errors = agent_master._project_strict_final_master_result(
            json.dumps(plan),
            proposal_packet=packet,
            architecture_policy={"schema_version": 1},
        )

        assert projected is None
        assert any(
            item.startswith("selected_proposal_worker_prompt_reserved_marker:")
            and marker in item
            for item in errors
        )
        bound = agent_master._bind_selected_proposal_workers(plan, selected)
        assert bound["tasks"][0]["worker_prompt"] == original


def test_selected_and_system_blocks_survive_repeated_plan_compilation(tmp_path):
    import agent_master
    import plan_compiler

    packet = _valid_proposal_packet(
        agent_master,
        BOUND_PROPOSAL,
        tmp_path / "strict_recompile_invocations",
        source_dir=agent_master.get_bot_dir(143),
    )
    selected = packet["ordered_proposals"][0]
    plan = json.loads(json.dumps(VALID_PLAN))
    plan.update({
        "selected_proposal_id": selected["proposal_id"],
        "targeted_failure": selected["targeted_failure"],
        "measurement_plan": selected["measurement"],
    })
    projected, errors = agent_master._project_strict_final_master_result(
        json.dumps(plan),
        proposal_packet=packet,
        architecture_policy={"schema_version": 1},
    )
    assert errors == []
    assert projected is not None

    first, first_meta = plan_compiler.compile_master_plan(
        projected,
        next_v=143,
        target_dir=tmp_path / "candidate",
        project_root=tmp_path,
    )
    second, second_meta = plan_compiler.compile_master_plan(
        first,
        next_v=143,
        target_dir=tmp_path / "candidate",
        project_root=tmp_path,
    )
    for compiled, meta in ((first, first_meta), (second, second_meta)):
        prompt = compiled["tasks"][0]["worker_prompt"]
        assert prompt.count(plan_compiler.SELECTED_PROPOSAL_BEGIN) == 1
        assert prompt.count(plan_compiler.SELECTED_PROPOSAL_END) == 1
        assert prompt.count(plan_compiler.SYSTEM_OWNED_CONTRACT_BEGIN) == 1
        assert prompt.count(plan_compiler.SYSTEM_OWNED_CONTRACT_END) == 1
        assert meta["contract_binding"]["invalid_prompt_tasks"] == []


@pytest.mark.parametrize("field", ("target_files", "files_allowed"))
@pytest.mark.parametrize("invalid", (None, 1, {}, "policy.py", [1]))
def test_strict_projection_rejects_malformed_worker_scope(
    tmp_path,
    field,
    invalid,
):
    import agent_master

    packet = _valid_proposal_packet(
        agent_master,
        BOUND_PROPOSAL,
        tmp_path / f"strict_scope_{field}_{type(invalid).__name__}",
        source_dir=agent_master.get_bot_dir(143),
    )
    selected = packet["ordered_proposals"][0]
    plan = json.loads(json.dumps(VALID_PLAN))
    plan.update({
        "selected_proposal_id": selected["proposal_id"],
        "targeted_failure": selected["targeted_failure"],
        "measurement_plan": selected["measurement"],
    })
    plan["tasks"][0][field] = invalid

    projected, errors = agent_master._project_strict_final_master_result(
        json.dumps(plan),
        proposal_packet=packet,
        architecture_policy={"schema_version": 1},
    )

    assert projected is None
    assert any(
        item.startswith("selected_proposal_worker_scope_type_invalid:")
        and f'"field":"{field}"' in item
        for item in errors
    )


def test_strict_projection_fails_closed_when_system_block_exceeds_reserve(
    monkeypatch,
    tmp_path,
):
    import agent_master
    import plan_compiler

    packet = _valid_proposal_packet(
        agent_master,
        BOUND_PROPOSAL,
        tmp_path / "strict_system_block_invocations",
        source_dir=agent_master.get_bot_dir(143),
    )
    selected = packet["ordered_proposals"][0]
    plan = json.loads(json.dumps(VALID_PLAN))
    plan.update({
        "selected_proposal_id": selected["proposal_id"],
        "targeted_failure": selected["targeted_failure"],
        "measurement_plan": selected["measurement"],
    })
    monkeypatch.setattr(plan_compiler, "SYSTEM_OWNED_CONTRACT_MAX_CHARS", 1)

    projected, errors = agent_master._project_strict_final_master_result(
        json.dumps(plan),
        proposal_packet=packet,
        architecture_policy={"schema_version": 1},
    )

    assert projected is None
    assert any(
        item.startswith("system_owned_worker_contract_binding_overflow:")
        for item in errors
    )


@pytest.mark.asyncio
async def test_proposal_context_excludes_final_master_tutorial(monkeypatch, tmp_path):
    import agent_master

    captured = {}
    packet = json.dumps(_valid_proposal_packet(
        agent_master,
        BOUND_PROPOSAL,
        tmp_path / "scoped_proposal_logs",
    ))

    async def capture_context(planning_context, **_kwargs):
        captured["planning_context"] = planning_context
        return packet

    async def final_master(*_args, **_kwargs):
        return _mock_llm_output(), 0.0, {}

    monkeypatch.setattr(
        agent_master,
        "_run_master_proposal_ensemble",
        capture_context,
    )
    monkeypatch.setattr(agent_master, "run_claude_query", final_master)

    result = await agent_master._run_master_analysis(
        source_v=143,
        next_v=144,
        stagnation_info="declining frozen selection signal",
        ui=_MockUI(),
    )

    assert result is not None
    context = captured["planning_context"]
    assert "SYSTEM-OWNED PROPOSAL CONTEXT" in context
    assert "# Stagnation diagnosis\ndeclining frozen selection signal" in context
    assert "You are the Master Bot Architect" not in context
    assert "Strategic analysis as a single string" not in context
    assert "<output_format>" not in context


@pytest.mark.asyncio
async def test_protocol_bootstrap_master_never_loads_or_injects_strength_history(
    monkeypatch,
    tmp_path,
):
    import agent_master
    import evidence_snapshot
    import evolution_infra
    import generation_evidence
    import official_certification
    import strict_authority_workflow
    from claude_agent_sdk import ResultMessage
    from runtime_architecture_policy import native_policy_runtime_contract
    from system_strict_bootstrap import build_fresh_bootstrap_receipt
    from workflow_kernel import WorkflowStore

    captured = []
    captured_kwargs = []
    captured_strict_logs = []
    fresh_packet_payload = _valid_proposal_packet(
        agent_master,
        BOUND_PROPOSAL,
        tmp_path / "fresh_proposal_invocations",
        evidence_mode="fresh_strict_control_no_strength",
        source_dir=agent_master.get_bot_dir(143),
    )
    fresh_selected = fresh_packet_payload["ordered_proposals"][0]
    fresh_plan = json.loads(json.dumps(VALID_PLAN))
    fresh_plan["selected_proposal_id"] = fresh_selected["proposal_id"]
    fresh_plan["targeted_failure"] = fresh_selected["targeted_failure"]
    fresh_plan["measurement_plan"] = fresh_selected["measurement"]

    async def fake_run_claude_query(prompt, *_args, **_kwargs):
        captured.append(prompt)
        captured_kwargs.append(dict(_kwargs))
        output = "```json\n" + json.dumps(fresh_plan) + "\n```\n"
        strict_call = _kwargs.get("strict_authority")
        if strict_call is not None:
            captured_strict_logs.append((
                strict_call["invocation_id"],
                Path(_args[3]),
            ))
            strict_authority_workflow.dispatch_call(
                strict_call,
                full_prompt=prompt,
                tools=_kwargs["tools"],
                owner="pytest",
                actual_role=str(_args[2]),
            )
            if strict_call.get("replay_provider"):
                return (
                    strict_call["replay_raw_output"],
                    float(strict_call.get("replay_cost_usd") or 0.0),
                    strict_call.get("replay_usage") or {},
                )
            provider_result = ResultMessage(
                subtype="success",
                duration_ms=10,
                duration_api_ms=10,
                is_error=False,
                num_turns=1,
                session_id="master-strength-quarantine-session",
                total_cost_usd=0.0,
                usage={},
                result=output,
            )
            strict_authority_workflow._observe_provider_result(
                provider_result,
                invocation_id=strict_call["invocation_id"],
                effect_id=strict_call["effect_id"],
            )
            strict_authority_workflow.complete_provider_call(
                strict_call,
                raw_output=output,
                provider_results=[provider_result],
            )
        return output, 0.0, {}

    def forbidden_loader(*_args, **_kwargs):
        raise AssertionError("protocol bootstrap strength loader was called")

    monkeypatch.setattr(agent_master, "run_claude_query", fake_run_claude_query)
    async def fresh_ensemble(*_args, **_kwargs):
        return json.dumps(fresh_packet_payload)

    monkeypatch.setattr(
        agent_master,
        "_run_master_proposal_ensemble",
        fresh_ensemble,
    )
    store = WorkflowStore(tmp_path / "strict-authority.sqlite3")
    monkeypatch.setattr(strict_authority_workflow, "_store", lambda: store)
    bootstrap_receipt = build_fresh_bootstrap_receipt(
        active_bots=(), epoch_reset_receipt_digest="a" * 64
    )
    strict_checkpoint = {
        "workflow_run_id": "master-strength-quarantine-test",
        "source_v": 142,
        "next_v": 143,
        "stage": "direction_audited",
        "checkpoint_revision": 7,
        "audit_context": {
            "protocol_bootstrap": bootstrap_receipt,
            "selection": {
                "bootstrap_without_strength_evidence": True,
                "strategy": "fresh_policy_bootstrap",
            },
            "prepared_artifact_contract": {
                "contract_digest": "b" * 64,
                "prepared_artifact_hash": "c" * 64,
            },
        },
    }
    monkeypatch.setattr(
        evolution_infra,
        "read_pipeline_checkpoint",
        lambda: strict_checkpoint,
    )
    monkeypatch.setattr(
        evidence_snapshot,
        "load_generation_snapshot_identity",
        forbidden_loader,
    )
    monkeypatch.setattr(
        official_certification,
        "official_feedback_summary",
        forbidden_loader,
    )

    sentinels = {
        "stagnation_info": "FORBIDDEN_STAGNATION_STRENGTH",
        "match_analysis": "FORBIDDEN_MATCH_ANALYSIS",
        "performance_verification": "FORBIDDEN_CRITIC_CONCLUSION",
        "replay_spotlight": "FORBIDDEN_REPLAY",
        "bot_action_stats": "FORBIDDEN_ACTION_PROFILE",
        "opponent_profiles": "FORBIDDEN_OPPONENT_PROFILE",
        "research_proposals": "FORBIDDEN_MATCH_DERIVED_RESEARCH",
    }
    # Exercise the real final-Master deterministic projection.  Strict
    # production calls always carry the system architecture policy; an empty
    # test policy would now (correctly) fail closed before role acceptance.
    architecture_policy = {
        "epoch": "national_tcp_policy_v1",
        "policy_abi": native_policy_runtime_contract()["policy_abi"],
    }
    result = await agent_master._run_master_analysis(
        source_v=142,
        next_v=143,
        ui=_MockUI(),
        protocol_bootstrap=bootstrap_receipt,
        architecture_policy=architecture_policy,
        **sentinels,
    )

    assert result is not None
    rendered = "\n".join(captured)
    assert "PROTOCOL BOOTSTRAP NO-STRENGTH" in rendered
    for forbidden in sentinels.values():
        assert forbidden not in rendered
    assert "Historical official-certification feedback was not loaded" in rendered
    assert "bots/national_v142/" not in rendered
    assert "system-owned source is fixed at v142" not in rendered
    assert "numeric completion high-water v142" in rendered
    assert "bots/national_v143/" in rendered
    assert captured_kwargs
    final_calls = [
        call for call in captured_kwargs
        if call.get("tools") == []
    ]
    assert len(final_calls) == 1
    assert final_calls[0].get("allowed_read_dirs") is None
    assert final_calls[0].get("allowed_evidence_snapshot_dir") is None
    assert all(
        call.get("allowed_read_dirs") == [agent_master.get_bot_dir(143)]
        for call in captured_kwargs
        if call.get("tools") == ["Read"]
    )
    assert all(
        agent_master.get_bot_dir(142) not in (call.get("allowed_read_dirs") or [])
        for call in captured_kwargs
    )
    assert "{planning_code_input_contract}" not in rendered
    assert "{source_selection_contract}" not in rendered
    assert "{target_path_contract}" not in rendered
    assert "Historical lineage source directory: quarantined" in rendered
    assert len(captured_strict_logs) == 1
    invocation_id, final_log = captured_strict_logs[0]
    assert final_log == (
        final_log.parents[2]
        / "strict_invocations"
        / invocation_id
        / "master_io.txt"
    )

    # ``fresh_ensemble`` deliberately bypasses the real five role dispatches
    # so this strength-quarantine test owns only final-Master rendering.  It
    # cannot be replayed as a complete authority packet; full six-slot replay
    # is exercised by the strict workflow/ensemble recovery regressions.
    assert len(captured_strict_logs) == 1


def test_master_official_feedback_requires_exact_current_artifact_identity(
    monkeypatch,
    tmp_path,
):
    from types import SimpleNamespace

    import agent_master
    import bot_artifact
    import bot_namespace
    import official_certification

    baseline = tmp_path / "national_v143"
    baseline.mkdir()
    exact_hash = "a" * 64
    monkeypatch.setattr(agent_master, "get_bot_dir", lambda _v: baseline)
    monkeypatch.setattr(bot_artifact, "hash_path", lambda _path: exact_hash)
    monkeypatch.setattr(
        bot_namespace,
        "resolve_national_bot_spec",
        lambda *_args, **_kwargs: SimpleNamespace(eligible=True),
    )
    poisoned = {
        "bot": "national_v142",
        "mode": "full",
        "policy_id": official_certification.FULL_POLICY_ID,
        "status": official_certification.STATUS_FAILED,
        "issues": ["RETIRED_OFFICIAL_SENTINEL"],
        "certification_identity": {
            "policy_id": official_certification.FULL_POLICY_ID,
            "candidate_hash": "b" * 64,
        },
    }
    monkeypatch.setattr(official_certification, "read_status", lambda _path: poisoned)
    rejected = agent_master._exact_official_compliance_feedback(143)
    assert "RETIRED_OFFICIAL_SENTINEL" not in rejected
    assert "other epochs, versions, or artifact hashes is excluded" in rejected

    exact = {
        **poisoned,
        "bot": "national_v143",
        "issues": ["MUTABLE_STATUS_POISON"],
        "official_llm_repair_guidance": "UNTRUSTED_ADVISORY_REPAIR",
        "official_deterministic_status_receipt": {
            "verdict": {
                "classification": "protocol",
                "blocking": True,
                "inconclusive": False,
                "issues": ["exact_protocol_action_format"],
            },
        },
        "certification_identity": {
            "policy_id": official_certification.FULL_POLICY_ID,
            "candidate_hash": exact_hash,
        },
    }
    monkeypatch.setattr(official_certification, "read_status", lambda _path: exact)
    monkeypatch.setattr(
        official_certification,
        "_deterministic_status_receipt_issues",
        lambda *_args, **_kwargs: [],
    )
    admitted = agent_master._exact_official_compliance_feedback(143)
    assert exact_hash in admitted
    assert "exact_protocol_action_format" in admitted
    assert "MUTABLE_STATUS_POISON" not in admitted
    assert "UNTRUSTED_ADVISORY_REPAIR" not in admitted
    assert "wins, losses, chips, THP earnings" in admitted

    monkeypatch.setattr(
        official_certification,
        "_deterministic_status_receipt_issues",
        lambda *_args, **_kwargs: ["evidence_digest_mismatch"],
    )
    receipt_rejected = agent_master._exact_official_compliance_feedback(143)
    assert "exact_protocol_action_format" not in receipt_rejected
    assert "other epochs, versions, or artifact hashes is excluded" in receipt_rejected


@pytest.mark.asyncio
async def test_post_abandon_singleton_successor_master_uses_durable_strict_journal(
    monkeypatch,
    tmp_path,
):
    import agent_master
    import checkpoint_schema
    import evidence_snapshot
    import evolution_infra
    import generation_evidence
    import strict_authority_workflow
    from claude_agent_sdk import ResultMessage
    from runtime_architecture_policy import native_policy_runtime_contract
    from tests.test_logic_mcp import TestSelectPrecommitOpponents
    from workflow_kernel import WorkflowStore

    checkpoint = TestSelectPrecommitOpponents._retarget_singleton_checkpoint(
        TestSelectPrecommitOpponents._singleton_checkpoint(),
        147,
    )
    checkpoint["stage"] = "direction_audited"
    checkpoint["audit_context"]["prepared_artifact_contract"] = {
        "contract_digest": "b" * 64,
        "prepared_artifact_hash": "c" * 64,
    }
    receipt = checkpoint["audit_context"]["protocol_bootstrap"]
    source = tmp_path / "national_v143"
    target = tmp_path / "national_v147"
    policy = (
        "def get_baseline_decision(context):\n"
        "    return _choose_intent(context)\n\n"
        "def _choose_intent(context):\n"
        "    return {'kind': 'pass'}\n\n"
        "def iter_decisions(context, baseline, budget_ms):\n"
        "    if False:\n"
        "        yield baseline\n"
    )
    for root in (source, target):
        root.mkdir()
        (root / "policy.py").write_text(policy, encoding="utf-8")

    packet = _valid_proposal_packet(
        agent_master,
        BOUND_PROPOSAL,
        tmp_path / "singleton_proposal_invocations",
        evidence_mode="singleton_parent_no_strength",
        source_dir=target,
    )
    selected = packet["ordered_proposals"][0]
    plan = json.loads(json.dumps(VALID_PLAN))
    plan["selected_proposal_id"] = selected["proposal_id"]
    plan["targeted_failure"] = selected["targeted_failure"]
    plan["measurement_plan"] = selected["measurement"]
    ensemble_kwargs = {}
    final_kwargs = []

    async def singleton_ensemble(*_args, **kwargs):
        ensemble_kwargs.update(kwargs)
        return json.dumps(packet)

    store = WorkflowStore(tmp_path / "singleton-strict-authority.sqlite3")
    monkeypatch.setattr(strict_authority_workflow, "_store", lambda: store)
    results_dir = tmp_path / "results"
    singleton_logs = results_dir / "v147" / "logs"
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results_dir)

    async def final_query(prompt, *_args, **kwargs):
        final_kwargs.append(kwargs)
        output = "```json\n" + json.dumps(plan) + "\n```"
        strict_call = kwargs.get("strict_authority")
        assert strict_call is not None
        strict_authority_workflow.dispatch_call(
            strict_call,
            full_prompt=str(prompt),
            tools=kwargs["tools"],
            owner="pytest-singleton-master-final",
            actual_role=str(_args[2]),
        )
        if strict_call.get("replay_provider"):
            return (
                strict_call["replay_raw_output"],
                float(strict_call.get("replay_cost_usd") or 0.0),
                strict_call.get("replay_usage") or {},
            )
        provider_result = ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="singleton-master-final",
            total_cost_usd=0.0,
            usage={},
            result=output,
        )
        strict_authority_workflow._observe_provider_result(
            provider_result,
            invocation_id=strict_call["invocation_id"],
            effect_id=strict_call["effect_id"],
        )
        strict_authority_workflow.complete_provider_call(
            strict_call,
            raw_output=output,
            provider_results=[provider_result],
        )
        return output, 0.0, {}

    def bot_dir(version):
        return source if int(version) == 143 else target

    monkeypatch.setattr(agent_master, "get_bot_dir", bot_dir)
    monkeypatch.setattr(agent_master, "get_logs_dir", lambda _v: singleton_logs)
    monkeypatch.setattr(
        agent_master,
        "_run_master_proposal_ensemble",
        singleton_ensemble,
    )
    monkeypatch.setattr(agent_master, "run_claude_query", final_query)
    monkeypatch.setattr(
        agent_master,
        "_exact_official_compliance_feedback",
        lambda _v: "Published v143 official compliance receipt is valid.",
    )
    monkeypatch.setattr(
        evolution_infra,
        "read_pipeline_checkpoint",
        lambda: checkpoint,
    )
    monkeypatch.setattr(
        evolution_infra,
        "get_active_bots",
        lambda: ["national_v143"],
    )
    monkeypatch.setattr(
        checkpoint_schema,
        "live_checkpoint_parent_authority_errors",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        generation_evidence,
        "live_protocol_bootstrap_allocation_errors",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        evidence_snapshot,
        "load_generation_snapshot_identity",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("singleton Master must not load a strength snapshot")
        ),
    )

    architecture_policy = {
        "epoch": "national_tcp_policy_v1",
        "policy_abi": native_policy_runtime_contract()["policy_abi"],
    }
    result = await agent_master._run_master_analysis(
        source_v=143,
        next_v=147,
        stagnation_info="caller strength text must be quarantined",
        ui=_MockUI(),
        protocol_bootstrap=receipt,
        architecture_policy=architecture_policy,
    )

    assert result is not None
    assert ensemble_kwargs["protocol_bootstrap_prepared_only"] is False
    assert ensemble_kwargs["singleton_no_strength"] is True
    assert final_kwargs and final_kwargs[0]["strict_authority"] is not None
    assert final_kwargs[0]["strict_authority"]["slot"] == "master:final"
    assert final_kwargs[0]["tools"] == []
    assert final_kwargs[0].get("allowed_read_dirs") is None
    assert final_kwargs[0].get("allowed_evidence_snapshot_dir") is None
    assert result["proposal_ensemble"]["evidence_mode"] == (
        "singleton_parent_no_strength"
    )
    assert result["proposal_binding"]["execution_mode"] == (
        "strategy_implementation"
    )


@pytest.mark.asyncio
async def test_singleton_master_live_allocation_drift_blocks_before_provider(
    monkeypatch,
):
    import agent_master
    import checkpoint_schema
    import evolution_infra
    import generation_evidence
    from tests.test_logic_mcp import TestSelectPrecommitOpponents

    checkpoint = TestSelectPrecommitOpponents._retarget_singleton_checkpoint(
        TestSelectPrecommitOpponents._singleton_checkpoint(),
        147,
    )
    checkpoint["stage"] = "direction_audited"
    receipt = checkpoint["audit_context"]["protocol_bootstrap"]
    monkeypatch.setattr(
        evolution_infra,
        "read_pipeline_checkpoint",
        lambda: checkpoint,
    )
    monkeypatch.setattr(
        evolution_infra,
        "get_active_bots",
        lambda: ["national_v143"],
    )
    monkeypatch.setattr(
        checkpoint_schema,
        "live_checkpoint_parent_authority_errors",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        generation_evidence,
        "live_protocol_bootstrap_allocation_errors",
        lambda *_args, **_kwargs: [
            "protocol_bootstrap_live_allocation:checkpoint_abandoned_receipt_head_changed"
        ],
    )
    monkeypatch.setattr(
        agent_master,
        "_run_master_proposal_ensemble",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("provider dispatch must not occur")
        ),
    )

    with pytest.raises(agent_master.MasterAuthorityError) as caught:
        await agent_master._run_master_analysis(
            source_v=143,
            next_v=147,
            stagnation_info="",
            ui=_MockUI(),
            protocol_bootstrap=receipt,
        )

    assert "checkpoint_abandoned_receipt_head_changed" in caught.value.errors[0]


@pytest.mark.asyncio
async def test_master_fails_closed_without_generation_evidence(monkeypatch):
    import agent_master
    import evidence_snapshot

    async def must_not_call(*_args, **_kwargs):
        raise AssertionError("Master must not run without the frozen evidence bundle")

    monkeypatch.setattr(agent_master, "run_claude_query", must_not_call)
    monkeypatch.setattr(
        evidence_snapshot,
        "load_generation_snapshot_identity",
        lambda *_args, **_kwargs: {
            "available": False,
            "reason": "cycle_manifest_missing",
        },
    )
    ui = _MockUI()

    result = await agent_master._run_master_analysis(
        source_v=143,
        next_v=144,
        stagnation_info="declining",
        ui=ui,
    )

    assert result is None
    assert any(
        "stable evaluation snapshot unavailable" in msg
        for _level, msg in ui.history
    )


@pytest.mark.asyncio
async def test_master_binds_valid_structured_contract_without_lexical_retry(monkeypatch):
    import agent_master
    from output_schema import RuntimeContract, runtime_contract_worker_prompt_terms
    from plan_compiler import SYSTEM_OWNED_CONTRACT_HEADER

    root = Path(__file__).resolve().parents[2]
    prompt = (root / "web/core/prompts/master_prompt.md").read_text(encoding="utf-8")
    start = prompt.index('{\n  "analysis": "Strategic analysis as a single string.')
    end = prompt.index("\n\n- Do NOT include `branch_from`", start)
    structured_plan = json.loads(prompt[start:end])
    structured_plan["targeted_failure"] = BOUND_TARGETED_FAILURE
    structured_plan["selected_proposal_id"] = PROPOSAL_ID
    structured_plan["measurement_plan"] = BOUND_PROPOSAL["measurement"]
    structured_plan["tasks"][0]["worker_prompt"] = (
        "Implement the selected valid structured runtime mechanism in policy.py "
        "and run only the declared checks."
    )
    call_count = {"n": 0}

    async def fake_run_claude_query(prompt, ctx, ui, role_name, log_file, tools=None, **_kwargs):
        call_count["n"] += 1
        return "```json\n" + json.dumps(structured_plan) + "\n```", 0.0, {}

    monkeypatch.setattr(agent_master, "run_claude_query", fake_run_claude_query)

    # This deliberately repeats the real v146 outer-agent contradiction.  It is
    # evidence text, not authority over the selected structured reference card.
    contradictory_context = (
        "Do not invent custom worker terms like range_weighted_candidate_batch_v1."
    )
    result = await agent_master._run_master_analysis(
        source_v=143,
        next_v=146,
        stagnation_info=contradictory_context,
        ui=_MockUI(),
    )

    assert result is not None
    assert call_count["n"] == 1
    task = result["tasks"][0]
    bound_prompt = task["worker_prompt"].lower()
    assert SYSTEM_OWNED_CONTRACT_HEADER.lower() in bound_prompt
    contract = RuntimeContract.model_validate(task["runtime_contract"])
    for term in runtime_contract_worker_prompt_terms(contract):
        assert term.lower() in bound_prompt


@pytest.mark.asyncio
async def test_master_does_not_bind_invalid_work_primitive(monkeypatch):
    import agent_master

    root = Path(__file__).resolve().parents[2]
    prompt = (root / "web/core/prompts/master_prompt.md").read_text(encoding="utf-8")
    start = prompt.index('{\n  "analysis": "Strategic analysis as a single string.')
    end = prompt.index("\n\n- Do NOT include `branch_from`", start)
    invalid_plan = json.loads(prompt[start:end])
    invalid_plan["targeted_failure"] = BOUND_TARGETED_FAILURE
    invalid_plan["selected_proposal_id"] = PROPOSAL_ID
    invalid_plan["tasks"][0]["runtime_contract"]["state_learning"]["work_primitive"] = []
    original_worker_prompt = invalid_plan["tasks"][0]["worker_prompt"]
    outputs = []

    async def fake_run_claude_query(prompt, ctx, ui, role_name, log_file, tools=None, **_kwargs):
        outputs.append(prompt)
        return "```json\n" + json.dumps(invalid_plan) + "\n```", 0.0, {}

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(agent_master, "run_claude_query", fake_run_claude_query)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    result = await agent_master._run_master_analysis(
        source_v=143,
        next_v=146,
        stagnation_info="stagnant",
        ui=_MockUI(),
    )

    assert result is None
    assert len(outputs) == agent_master.MAX_MASTER_RETRIES
    assert invalid_plan["tasks"][0]["worker_prompt"] == original_worker_prompt


@pytest.mark.asyncio
async def test_master_retries_on_genuinely_malformed_json(monkeypatch):
    """Sanity: when the LLM truly returns non-JSON, Master still retries and
    eventually returns None. Guards against over-correcting the fix."""
    import agent_master

    call_count = {"n": 0}

    async def fake_run_claude_query(prompt, ctx, ui, role_name, log_file, tools=None, **_kwargs):
        call_count["n"] += 1
        # Pure prose, no JSON anywhere.
        return "I cannot produce a plan right now.", 0.0, {}

    monkeypatch.setattr(agent_master, "run_claude_query", fake_run_claude_query)

    ui = _MockUI()
    result = await agent_master._run_master_analysis(
        source_v=143, next_v=144, stagnation_info="declining", ui=ui
    )

    assert result is None, "Genuinely malformed output should yield None"
    assert call_count["n"] == agent_master.MAX_MASTER_RETRIES


@pytest.mark.asyncio
async def test_master_fails_closed_after_structured_schema_errors(monkeypatch):
    """Valid JSON with an invalid worker contract must never be returned raw."""
    import agent_master

    invalid_plan = json.loads(json.dumps(VALID_PLAN))
    invalid_plan["tasks"][0].pop("skill_layer")
    call_count = {"n": 0}

    async def fake_run_claude_query(prompt, ctx, ui, role_name, log_file, tools=None, **_kwargs):
        call_count["n"] += 1
        return "```json\n" + json.dumps(invalid_plan) + "\n```", 0.0, {}

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(agent_master, "run_claude_query", fake_run_claude_query)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    result = await agent_master._run_master_analysis(
        source_v=143,
        next_v=144,
        stagnation_info="declining",
        ui=_MockUI(),
    )

    assert result is None
    assert call_count["n"] == agent_master.MAX_MASTER_RETRIES


@pytest.mark.asyncio
async def test_master_transport_failure_is_not_an_invalid_plan(monkeypatch):
    import agent_master

    roles = []

    async def unavailable(*args, **_kwargs):
        roles.append(args[3])
        raise ConnectionError("sdk backend unavailable")

    monkeypatch.setattr(agent_master, "run_claude_query", unavailable)

    with pytest.raises(agent_master.MasterInfrastructureError) as caught:
        await agent_master._run_master_analysis(
            source_v=143,
            next_v=144,
            stagnation_info="declining",
            ui=_MockUI(),
        )

    assert caught.value.source_v == 143
    assert caught.value.next_v == 144
    assert len(caught.value.prompt_digest) == 64
    assert roles == ["MASTER (Try 1)"]
    assert not any("SCHEMA RETRY" in role for role in roles)


@pytest.mark.asyncio
async def test_master_proposal_authority_failure_is_not_wrapped(monkeypatch):
    import agent_master
    from strict_authority_workflow import StrictAuthorityError

    async def authority_failure(*_args, **_kwargs):
        raise StrictAuthorityError("proposal authority drift")

    monkeypatch.setattr(
        agent_master,
        "_run_master_proposal_ensemble",
        authority_failure,
    )

    with pytest.raises(StrictAuthorityError, match="proposal authority drift"):
        await agent_master._run_master_analysis(
            source_v=143,
            next_v=144,
            stagnation_info="declining",
            ui=_MockUI(),
        )


@pytest.mark.asyncio
async def test_master_final_authority_failure_is_not_wrapped(monkeypatch):
    import agent_master
    from strict_authority_workflow import StrictAuthorityError

    async def authority_failure(*_args, **_kwargs):
        raise StrictAuthorityError("final authority drift")

    monkeypatch.setattr(agent_master, "run_claude_query", authority_failure)

    with pytest.raises(StrictAuthorityError, match="final authority drift"):
        await agent_master._run_master_analysis(
            source_v=143,
            next_v=144,
            stagnation_info="declining",
            ui=_MockUI(),
        )
