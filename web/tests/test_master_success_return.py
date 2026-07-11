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

import pytest

VALID_PLAN = {
    "analysis": "Deliver the check_raise_freq detector to fix the 0%-fold leak.",
    "targeted_failure": "0% postflop fold rate vs aggressive opponents (v109/v93/v77 at 30%).",
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


@pytest.mark.asyncio
async def test_master_returns_valid_plan_on_first_try(monkeypatch):
    import agent_master

    call_count = {"n": 0}
    captured_prompts = []

    async def fake_run_claude_query(prompt, ctx, ui, role_name, log_file, tools=None):
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
async def test_master_retries_on_genuinely_malformed_json(monkeypatch):
    """Sanity: when the LLM truly returns non-JSON, Master still retries and
    eventually returns None. Guards against over-correcting the fix."""
    import agent_master

    call_count = {"n": 0}

    async def fake_run_claude_query(prompt, ctx, ui, role_name, log_file, tools=None):
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

    async def fake_run_claude_query(prompt, ctx, ui, role_name, log_file, tools=None):
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
