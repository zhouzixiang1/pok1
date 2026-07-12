"""Regression test for the v107–v127 Master deadlock root cause.

BUG (fixed): `_run_master_analysis` had no `return data` on the success path.
When the Master LLM produced a plan that parsed (had `tasks`), carried no
`branch_from` override, and passed `validate_agent_output` with no errors,
execution fell through to the `ui.log_history("Master output malformed JSON")`
branch, burned all MAX_MASTER_RETRIES, and returned None. Every valid Master
plan for 11+ generations was silently discarded — misdiagnosed the entire time
as schema / SDK-signature / direction-audit failures.

This test feeds a VALID plan through the analysis function (with run_claude_query
mocked) and asserts:
  1. the plan is returned (not None), and
  2. the LLM is called exactly ONCE (success on first try) — before the fix it
     was called 3× (every valid plan retried) and still returned None.
"""

import json
import asyncio
import hashlib
from pathlib import Path

import pytest

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
    "expected_diff": "The existing get_action to choose_action path consumes the new mechanism.",
    "target_files": ["strategy.py"],
    "source_symbols": ["strategy.py:get_action", "strategy.py:choose_action"],
    "reachable_chain": ["strategy.py:get_action", "strategy.py:choose_action"],
    "falsifier": {
        "test_name": "test_selected_mechanism",
        "control": "The frozen parent keeps the original decision on the paired state.",
        "intervention": "Only the selected mechanism changes on the paired state.",
        "expected_observation": "The intervention changes the target action and control does not.",
    },
    "evidence_refs": [
        "source:strategy.py:get_action",
        "source:strategy.py:choose_action",
    ],
    "risks": "Sparse evidence can overfit, so the mechanism and fallback remain bounded.",
}
PROPOSAL_ID = hashlib.sha256(json.dumps(
    BOUND_PROPOSAL,
    sort_keys=True,
    ensure_ascii=False,
    separators=(",", ":"),
).encode("utf-8")).hexdigest()[:16]
BOUND_PROPOSAL["proposal_id"] = PROPOSAL_ID

VALID_PLAN = {
    "analysis": "Deliver the check_raise_freq detector to fix the 0%-fold leak.",
    "targeted_failure": BOUND_TARGETED_FAILURE,
    "expected_behavior_change": "Bot folds marginal one-pair hands facing a live check-raise.",
    "do_not_touch": ["card_utils.py", "constants.py"],
    "measurement_plan": "Mirror battles vs v109/v93; paired net-chips CI lower bound > 0.",
    "tasks": [
        {
            "worker_id": 1,
            "role": "Algorithmic Logic Architect",
            "target_files": ["opponent.py", "strategy.py"],
            "difficulty": "medium",
            "skill_layer": "spr",
            "worker_prompt": "Add check_raise_trap_severity() to opponent.py and wire into strategy.py fold sites.",
        }
    ],
    "selected_proposal_id": PROPOSAL_ID,
}


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
        "h2h_relpath": "web/core/results/v127/evidence_snapshot/head_to_head.json",
        "selection_relpath": "web/core/results/v127/evidence_snapshot/selection_snapshot.json",
        "manifest_path": str(manifest_path),
        "manifest_relpath": "web/core/results/v127/evidence_snapshot/manifest.json",
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
    async def no_ensemble(*_args, **_kwargs):
        return json.dumps({
            "schema_version": "master-proposal-packet-v2",
            "valid": True,
            "authority": "advisory_only",
            "context_digest": "c" * 64,
            "source_code_digest": "d" * 64,
            "proposal_count": 1,
            "valid_critic_count": 2,
            "allowed_proposal_ids": [PROPOSAL_ID],
            "ordered_proposals": [BOUND_PROPOSAL],
            "critic_reviews": [],
        })

    monkeypatch.setattr(agent_master, "_run_master_proposal_ensemble", no_ensemble)


@pytest.mark.asyncio
async def test_master_returns_valid_plan_on_first_try(monkeypatch):
    import agent_master

    call_count = {"n": 0}
    captured_prompts = []

    async def fake_run_claude_query(prompt, ctx, ui, role_name, log_file, tools=None, **_kwargs):
        call_count["n"] += 1
        captured_prompts.append(prompt)
        return _mock_llm_output(), 0.0, {}

    # Patch the name as bound in agent_master's namespace (imported at top).
    monkeypatch.setattr(agent_master, "run_claude_query", fake_run_claude_query)

    ui = _MockUI()
    result = await agent_master._run_master_analysis(
        source_v=111, next_v=127, stagnation_info="declining", ui=ui
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
    assert "task.target_files: 1..3 files (never more than 3)" in rendered_prompt
    assert 'build_phase="module_import"' in rendered_prompt
    assert "runtime_contract.match_memory" in rendered_prompt
    assert '"memory", "confidence", "opponent_runtime"' in rendered_prompt


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
        source_v=111,
        next_v=127,
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
        "Implement the selected valid structured runtime mechanism in strategy.py "
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
        source_v=142,
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
        source_v=142,
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
        source_v=111, next_v=127, stagnation_info="declining", ui=ui
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
        source_v=111,
        next_v=127,
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
            source_v=111,
            next_v=127,
            stagnation_info="declining",
            ui=_MockUI(),
        )

    assert caught.value.source_v == 111
    assert caught.value.next_v == 127
    assert len(caught.value.prompt_digest) == 64
