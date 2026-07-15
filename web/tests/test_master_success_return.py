"""Master success/failure coverage for ``national_tcp_policy_v1``.

The fixtures use the checked-in strict Master example: target v143+,
candidate-owned ``policy.py`` only, and typed decision-context intents.  The
fresh v143 bootstrap case additionally exercises the current durable authority
dispatch and no-strength receipt boundary.
"""

import json
import asyncio
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]

BOUND_TARGETED_FAILURE = (
    "The selected evidence-bound mechanism fixes one reachable parent decision failure."
)
BOUND_PROPOSAL = {
    "schema_version": "master-proposal-v2",
    "targeted_failure": BOUND_TARGETED_FAILURE,
    "structural_change": "Replace one reachable parent branch with a bounded mechanism.",
    "counterfactual": "Hold cards, seed, state, and legality fixed while toggling only the mechanism.",
    "measurement": "Run paired positive and control decisions before the native regression gate.",
    "why_not_threshold_tuning": "The change replaces state flow and its consumer rather than one numeric cutoff.",
    "expected_diff": "The existing get_baseline_decision to _choose_intent path consumes the new mechanism.",
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
        "test_name": "test_selected_mechanism",
        "control": "The frozen parent keeps the original decision on the paired state.",
        "intervention": "Only the selected mechanism changes on the paired state.",
        "expected_observation": "The intervention changes the target action and control does not.",
    },
    "evidence_refs": [
        "source:policy.py:get_baseline_decision",
        "source:policy.py:_choose_intent",
    ],
    "risks": "Sparse evidence can overfit, so the mechanism and fallback remain bounded.",
}
PROPOSAL_ID = "set-by-stable-generation-evidence-fixture"


def _valid_proposal_packet(agent_master, selected_proposal, log_dir):
    import hashlib

    from system_strict_bootstrap import record_llm_invocation_evidence

    directions = ("mechanism", "counterfactual", "compute_memory")
    structural_changes = (
        selected_proposal["structural_change"],
        "Add a bounded state accumulator before the same reachable decision consumer.",
        "Add a deterministic paired-feature path into the same reachable decision consumer.",
    )
    proposals = []
    for index, (direction, structural_change) in enumerate(
        zip(directions, structural_changes), start=1
    ):
        proposal = json.loads(json.dumps(selected_proposal))
        proposal["direction"] = direction
        proposal["structural_change"] = structural_change
        if index > 1:
            proposal["expected_diff"] = (
                f"Independent alternative {index} reaches the existing decision consumer."
            )
            proposal["falsifier"]["test_name"] = (
                f"test_alternative_{index}_mechanism"
            )
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
    return {
        "schema_version": "master-proposal-packet-v2",
        "valid": True,
        "authority": "advisory_only",
        "context_digest": "c" * 64,
        "source_code_digest": "d" * 64,
        "proposal_count": 3,
        "valid_critic_count": 2,
        "critic_criteria": agent_master._PROPOSAL_CRITIC_CRITERIA,
        "allowed_proposal_ids": proposal_ids,
        "ordered_proposals": proposals,
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

    async def no_ensemble(*_args, **_kwargs):
        return json.dumps(
            _valid_proposal_packet(
                agent_master,
                BOUND_PROPOSAL,
                tmp_path / "proposal_invocations",
            )
        )

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
    assert captured_kwargs[0]["allowed_read_dirs"] == [
        agent_master.get_bot_dir(143),
        agent_master.get_bot_dir(144),
    ]


@pytest.mark.asyncio
async def test_protocol_bootstrap_master_never_loads_or_injects_strength_history(
    monkeypatch,
    tmp_path,
):
    import agent_master
    import evidence_snapshot
    import evolution_infra
    import official_certification
    import strict_authority_workflow
    from claude_agent_sdk import ResultMessage
    from runtime_architecture_policy import native_policy_runtime_contract
    from system_strict_bootstrap import build_fresh_bootstrap_receipt
    from workflow_kernel import WorkflowStore

    captured = []
    captured_kwargs = []

    async def fake_run_claude_query(prompt, *_args, **_kwargs):
        captured.append(prompt)
        captured_kwargs.append(dict(_kwargs))
        output = _mock_llm_output()
        strict_call = _kwargs.get("strict_authority")
        if strict_call is not None:
            strict_authority_workflow.dispatch_call(
                strict_call,
                full_prompt=prompt,
                tools=["Read"],
                owner="pytest",
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

    async def unavailable(*_args, **_kwargs):
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
