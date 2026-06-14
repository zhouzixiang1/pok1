"""Tests for LLM infrastructure error classification and the Critic/Reviewer infra short-circuits.

Covers `web/core/llm_failure.py` (is_llm_infra_error / infra_payload) and the
`run_critic` / `run_review` infra short-circuits in `web/core/tool_gates.py`.

The infra short-circuit must:
- NOT increment generation_attempt
- NOT call _record_quality_failure / guardian / *_rejected log
- <3 attempts -> action "retry_critic"/"retry_review", >=3 -> "abandon_cycle"
- keep stage at its current value (reviewed for critic, quality_passed for reviewer)
- persist *_infra_retry count, reset to 0 on abandon
- write llm_failed marker gate (NOT an approved:False rejection gate)
"""

import asyncio
import json

import pytest


# ---------------------------------------------------------------------------
# is_llm_infra_error / infra_payload
# ---------------------------------------------------------------------------

class TestIsLlmInfraError:
    def test_claude_sdk_error_is_infra(self):
        from claude_agent_sdk import ClaudeSDKError
        from llm_failure import is_llm_infra_error
        assert is_llm_infra_error(ClaudeSDKError("signature error")) is True

    def test_timeout_error_is_infra(self):
        from llm_failure import is_llm_infra_error
        assert is_llm_infra_error(asyncio.TimeoutError()) is True

    def test_connection_error_is_infra(self):
        from llm_failure import is_llm_infra_error
        assert is_llm_infra_error(ConnectionError("refused")) is True

    def test_os_error_is_infra(self):
        from llm_failure import is_llm_infra_error
        assert is_llm_infra_error(OSError("broken pipe")) is True

    def test_value_error_is_not_infra(self):
        from llm_failure import is_llm_infra_error
        assert is_llm_infra_error(ValueError("bad value")) is False

    def test_json_decode_error_is_not_infra(self):
        from json import JSONDecodeError
        from llm_failure import is_llm_infra_error
        assert is_llm_infra_error(JSONDecodeError("msg", "doc", 0)) is False

    def test_key_error_is_not_infra(self):
        from llm_failure import is_llm_infra_error
        assert is_llm_infra_error(KeyError("missing")) is False

    def test_infra_payload_has_marker_and_fields(self):
        from claude_agent_sdk import ClaudeSDKError
        from llm_failure import infra_payload
        exc = ClaudeSDKError("boom")
        payload = infra_payload(exc, approved=False, foo=1)
        assert payload["llm_failed"] is True
        assert payload["infra_error"] is True
        assert payload["error"] == "boom"
        assert payload["approved"] is False
        assert payload["foo"] == 1


# ---------------------------------------------------------------------------
# run_review infra short-circuit
# ---------------------------------------------------------------------------

def _seed_checkpoint(next_v, source_v, stage="reviewed", generation_attempt=0,
                     critic_infra_retry=None):
    """Seed pipeline_state.json with quality+review gates passing and given critic retry state."""
    import evolution_infra
    import tempfile
    tmp = tempfile.mkdtemp()
    fake_results = __import__("pathlib").Path(tmp)
    evolution_infra.RESULTS_DIR = fake_results
    evolution_infra.PIPELINE_STATE_FILE = fake_results / "pipeline_state.json"

    critic_gate = {}
    if critic_infra_retry is not None:
        critic_gate["critic_infra_retry"] = critic_infra_retry

    ckpt = {
        "next_v": next_v,
        "source_v": source_v,
        "stage": stage,
        "master_plan": [],
        "reviewer_feedback": "",
        "generation_attempt": generation_attempt,
        "gate_results": {
            "quality": {"all_passed": True, "critical_scenarios_passed": True},
            "review": {"approved": True},
            "critic": critic_gate,
        },
    }
    evolution_infra.PIPELINE_STATE_FILE.write_text(json.dumps(ckpt))
    return evolution_infra.PIPELINE_STATE_FILE


def _seed_review_checkpoint(next_v, source_v, stage="quality_passed", generation_attempt=0,
                            review_infra_retry=None):
    """Seed pipeline_state.json with quality gates passing and given review retry state."""
    import evolution_infra
    import tempfile
    tmp = tempfile.mkdtemp()
    fake_results = __import__("pathlib").Path(tmp)
    evolution_infra.RESULTS_DIR = fake_results
    evolution_infra.PIPELINE_STATE_FILE = fake_results / "pipeline_state.json"

    review_gate = {}
    if review_infra_retry is not None:
        review_gate["review_infra_retry"] = review_infra_retry

    ckpt = {
        "next_v": next_v,
        "source_v": source_v,
        "stage": stage,
        "master_plan": [],
        "reviewer_feedback": "",
        "generation_attempt": generation_attempt,
        "gate_results": {
            "quality": {"all_passed": True, "critical_scenarios_passed": True},
            "review": review_gate,
        },
    }
    evolution_infra.PIPELINE_STATE_FILE.write_text(json.dumps(ckpt))
    return evolution_infra.PIPELINE_STATE_FILE


class TestRunReviewInfraShortCircuit:
    """Run run_review with a mocked run_claude_query raising ClaudeSDKError.

    Verifies the infra short-circuit: retry_review under 3, abandon_cycle at 3, no
    generation_attempt increment, no quality-failure record, stage stays quality_passed,
    review_infra_retry counter persisted + reset on abandon, and the review gate carries
    the llm_failed marker (NOT an approved:False rejection gate that would block pipeline).
    """

    def _patch_idempotency(self, monkeypatch):
        # Bypass the review-already-passed idempotency guard
        import tool_gates
        monkeypatch.setattr(tool_gates, "_idempotency_check", lambda *a, **k: None)

    def _run(self, monkeypatch, exc, review_retry=None):
        import tool_gates
        import asyncio

        _seed_review_checkpoint(101, 100, stage="quality_passed", generation_attempt=2,
                                review_infra_retry=review_retry)

        # Mock the Reviewer LLM call to raise an infra error
        async def fake_run_claude_query(*a, **kw):
            raise exc
        monkeypatch.setattr(tool_gates, "run_claude_query", fake_run_claude_query)

        # Track that _record_quality_failure is NEVER called
        calls = {"quality_failure": 0}
        monkeypatch.setattr(tool_gates, "_record_quality_failure",
                            lambda *a, **k: calls.__setitem__("quality_failure", calls["quality_failure"] + 1))

        self._patch_idempotency(monkeypatch)

        result = asyncio.run(tool_gates.run_review.handler({"version": 101, "source_v": 100, "plan": []}))
        return result, calls

    def _parse(self, result):
        text = result["content"][0]["text"]
        return json.loads(text)

    def test_retry_review_under_3(self, monkeypatch):
        from claude_agent_sdk import ClaudeSDKError
        result, calls = self._run(monkeypatch, ClaudeSDKError("signature error"), review_retry=0)
        data = self._parse(result)
        assert data["action"] == "retry_review"
        assert data["llm_failed"] is True
        assert calls["quality_failure"] == 0

    def test_abandon_cycle_at_3(self, monkeypatch):
        from claude_agent_sdk import ClaudeSDKError
        result, calls = self._run(monkeypatch, ClaudeSDKError("signature error"), review_retry=2)
        data = self._parse(result)
        assert data["action"] == "abandon_cycle"
        assert data["llm_failed"] is True
        assert calls["quality_failure"] == 0

    def test_no_quality_failure_recorded(self, monkeypatch):
        from claude_agent_sdk import ClaudeSDKError
        _, calls = self._run(monkeypatch, ClaudeSDKError("sig"), review_retry=1)
        assert calls["quality_failure"] == 0

    def test_generation_attempt_not_incremented(self, monkeypatch):
        from claude_agent_sdk import ClaudeSDKError
        import evolution_infra
        self._run(monkeypatch, ClaudeSDKError("sig"), review_retry=0)
        ckpt = json.loads(evolution_infra.PIPELINE_STATE_FILE.read_text())
        # started at generation_attempt=2; infra must NOT bump it
        assert ckpt["generation_attempt"] == 2

    def test_stage_stays_quality_passed(self, monkeypatch):
        from claude_agent_sdk import ClaudeSDKError
        import evolution_infra
        self._run(monkeypatch, ClaudeSDKError("sig"), review_retry=0)
        ckpt = json.loads(evolution_infra.PIPELINE_STATE_FILE.read_text())
        assert ckpt["stage"] == "quality_passed"

    def test_review_infra_retry_counter_advances(self, monkeypatch):
        from claude_agent_sdk import ClaudeSDKError
        import evolution_infra
        self._run(monkeypatch, ClaudeSDKError("sig"), review_retry=1)
        ckpt = json.loads(evolution_infra.PIPELINE_STATE_FILE.read_text())
        assert ckpt["gate_results"]["review"]["review_infra_retry"] == 2

    def test_review_infra_retry_resets_on_abandon(self, monkeypatch):
        from claude_agent_sdk import ClaudeSDKError
        import evolution_infra
        self._run(monkeypatch, ClaudeSDKError("sig"), review_retry=2)
        ckpt = json.loads(evolution_infra.PIPELINE_STATE_FILE.read_text())
        # abandon path resets counter to 0
        assert ckpt["gate_results"]["review"]["review_infra_retry"] == 0

    def test_review_gate_has_llm_failed_marker(self, monkeypatch):
        from claude_agent_sdk import ClaudeSDKError
        import evolution_infra
        self._run(monkeypatch, ClaudeSDKError("boom"), review_retry=0)
        ckpt = json.loads(evolution_infra.PIPELINE_STATE_FILE.read_text())
        review = ckpt["gate_results"]["review"]
        assert review["llm_failed"] is True
        assert review["approved"] is False


# ---------------------------------------------------------------------------
# run_critic infra short-circuit
# ---------------------------------------------------------------------------

class TestRunCriticInfraShortCircuit:
    """Run run_critic with a mocked _run_critic returning {llm_failed: True}.

    We drive the tool handler directly (async) rather than via the HTTP client so we
    can assert on the exact returned dict and mock the quality-failure recorder.
    """

    def _patch_idempotency(self, monkeypatch):
        # Bypass the critic-already-passed idempotency guard
        import tool_gates
        monkeypatch.setattr(tool_gates, "_idempotency_check", lambda *a, **k: None)

    def _run(self, monkeypatch, llm_failed_payload, critic_retry=None):
        import tool_gates
        import asyncio

        _seed_checkpoint(101, 100, stage="reviewed", generation_attempt=2,
                         critic_infra_retry=critic_retry)

        # Mock the Critic LLM call to return an infra payload
        async def fake_run_critic(*a, **kw):
            return llm_failed_payload
        monkeypatch.setattr(tool_gates, "_run_critic", fake_run_critic)

        # Track that _record_quality_failure is NEVER called
        calls = {"quality_failure": 0, "guardian": 0}
        monkeypatch.setattr(tool_gates, "_record_quality_failure",
                            lambda *a, **k: calls.__setitem__("quality_failure", calls["quality_failure"] + 1))

        self._patch_idempotency(monkeypatch)

        result = asyncio.run(tool_gates.run_critic.handler({"version": 101, "source_v": 100, "plan": []}))
        return result, calls

    def _parse(self, result):
        # Handler returns MCP-formatted dict: {"content": [{"type":"text","text": "<json>"}]}
        text = result["content"][0]["text"]
        return json.loads(text)

    def test_retry_critic_under_3(self, monkeypatch):
        result, calls = self._run(monkeypatch,
                                  {"llm_failed": True, "infra_error": True, "error": "sig",
                                   "approved": False},
                                  critic_retry=0)
        data = self._parse(result)
        assert data["action"] == "retry_critic"
        assert data["llm_failed"] is True
        assert calls["quality_failure"] == 0

    def test_abandon_cycle_at_3(self, monkeypatch):
        result, calls = self._run(monkeypatch,
                                  {"llm_failed": True, "infra_error": True, "error": "sig",
                                   "approved": False},
                                  critic_retry=2)
        data = self._parse(result)
        assert data["action"] == "abandon_cycle"
        assert data["llm_failed"] is True
        assert calls["quality_failure"] == 0

    def test_no_quality_failure_recorded(self, monkeypatch):
        # retry path
        _, calls = self._run(monkeypatch,
                             {"llm_failed": True, "error": "sig", "approved": False},
                             critic_retry=1)
        assert calls["quality_failure"] == 0

    def test_generation_attempt_not_incremented(self, monkeypatch):
        import evolution_infra
        self._run(monkeypatch,
                  {"llm_failed": True, "error": "sig", "approved": False},
                  critic_retry=0)
        ckpt = json.loads(evolution_infra.PIPELINE_STATE_FILE.read_text())
        # started at generation_attempt=2; infra must NOT bump it
        assert ckpt["generation_attempt"] == 2

    def test_stage_stays_reviewed(self, monkeypatch):
        import evolution_infra
        self._run(monkeypatch,
                  {"llm_failed": True, "error": "sig", "approved": False},
                  critic_retry=0)
        ckpt = json.loads(evolution_infra.PIPELINE_STATE_FILE.read_text())
        assert ckpt["stage"] == "reviewed"

    def test_critic_infra_retry_counter_advances(self, monkeypatch):
        import evolution_infra
        self._run(monkeypatch,
                  {"llm_failed": True, "error": "sig", "approved": False},
                  critic_retry=1)
        ckpt = json.loads(evolution_infra.PIPELINE_STATE_FILE.read_text())
        assert ckpt["gate_results"]["critic"]["critic_infra_retry"] == 2

    def test_critic_infra_retry_resets_on_abandon(self, monkeypatch):
        import evolution_infra
        self._run(monkeypatch,
                  {"llm_failed": True, "error": "sig", "approved": False},
                  critic_retry=2)
        ckpt = json.loads(evolution_infra.PIPELINE_STATE_FILE.read_text())
        # abandon path resets counter to 0
        assert ckpt["gate_results"]["critic"]["critic_infra_retry"] == 0

    def test_critic_gate_has_llm_failed_marker(self, monkeypatch):
        import evolution_infra
        self._run(monkeypatch,
                  {"llm_failed": True, "error": "boom", "approved": False},
                  critic_retry=0)
        ckpt = json.loads(evolution_infra.PIPELINE_STATE_FILE.read_text())
        critic = ckpt["gate_results"]["critic"]
        assert critic["llm_failed"] is True
        assert critic["approved"] is False


# ---------------------------------------------------------------------------
# _decide_strategy combined-analyst llm_failed (B-class control flow)
# ---------------------------------------------------------------------------

class TestDecideStrategyCombinedInfra:
    """When the Combined Analyst reports llm_failed, _decide_strategy must:

    - Return ("master", current_v, ()) — conservative, NO crossover.
    - Emit a pipeline.combined_analyst_infra warn event.

    A crashed analyst's safe-default "improving / not stagnant" verdict is a
    guess; acting on it (especially triggering crossover via the stagnation or
    diversity branches) would misread an infra failure as a business signal.
    The mechanical cross-gen backstop in run_master still provides diversity
    protection independent of this LLM gate.
    """

    def test_combined_llm_failed_returns_master_no_crossover(self, monkeypatch):
        import generation_scheduler as gs
        # Neutralize the source-loop / oscillation detectors so they cannot
        # independently force a crossover before the llm_failed guard runs.
        monkeypatch.setattr(gs, "_detect_source_loop", lambda n=3: None)
        monkeypatch.setattr(gs, "_detect_source_oscillation", lambda n=8, max_unique=3: None)
        combined = {
            "is_stagnant": True,
            "confidence": "high",
            "diversity_needed": True,
            "recommendation": "crossover",
            "llm_failed": True,
        }
        strategy, source_v, parents = gs._decide_strategy(combined, current_v=50, ratings={})
        assert strategy == "master"
        assert source_v == 50
        assert parents == ()

    def test_combined_llm_failed_emits_infra_event(self, monkeypatch):
        import generation_scheduler as gs
        events = []
        monkeypatch.setattr(gs, "log_system_event",
                            lambda et, sev, msg, data=None: events.append((et, sev, msg, data)))
        combined = {"is_stagnant": True, "confidence": "high", "llm_failed": True}
        gs._decide_strategy(combined, 50, {})
        assert any(et == "pipeline.combined_analyst_infra" and sev == "warn"
                   for et, sev, msg, data in events)

    def test_combined_llm_failed_guards_before_crossover_branches(self, monkeypatch):
        """Even when the (untrustworthy) combined result would otherwise trigger
        crossover via _pick_crossover_parents, llm_failed must short-circuit to
        master WITHOUT calling _pick_crossover_parents."""
        import generation_scheduler as gs
        monkeypatch.setattr(gs, "_detect_source_loop", lambda n=3: None)
        monkeypatch.setattr(gs, "_detect_source_oscillation", lambda n=8, max_unique=3: None)
        calls = {"pick": 0}
        def _spy(ratings, current_v):
            calls["pick"] += 1
            return (60, 30)
        monkeypatch.setattr(gs, "_pick_crossover_parents", _spy)
        from evolution_infra import Glicko2Player
        combined = {"is_stagnant": True, "confidence": "high",
                    "diversity_needed": True, "llm_failed": True}
        strategy, source_v, parents = gs._decide_strategy(
            combined, 50, {"claude_v60": Glicko2Player()})
        assert strategy == "master"
        assert parents == ()
        assert calls["pick"] == 0  # crossover parent selection never ran


# ---------------------------------------------------------------------------
# direction_auditor llm_failed marker
# ---------------------------------------------------------------------------

class TestDirectionAuditorInfraMarker:
    """When the Direction Auditor LLM call raises an infra error, the returned
    dict must carry llm_failed=True so run_master skips injecting its
    (untrustworthy) mandatory_constraints block."""

    def test_infra_exception_returns_llm_failed_marker(self, monkeypatch):
        import direction_auditor
        from claude_agent_sdk import ClaudeSDKError

        async def fake_query(*a, **kw):
            raise ClaudeSDKError("signature error")
        monkeypatch.setattr(direction_auditor, "run_claude_query", fake_query)

        class _UI:
            def log_history(self, *a, **k):
                pass
        import evolution_infra
        monkeypatch.setattr(direction_auditor, "PROMPTS_DIR", evolution_infra.PROMPTS_DIR)

        import asyncio
        result = asyncio.run(direction_auditor._run_direction_audit(50, _UI()))
        assert result["llm_failed"] is True
        assert result["repetition_detected"] is False

    def test_non_infra_exception_no_llm_failed_marker(self, monkeypatch):
        import direction_auditor

        async def fake_query(*a, **kw):
            raise ValueError("parse error")
        monkeypatch.setattr(direction_auditor, "run_claude_query", fake_query)

        class _UI:
            def log_history(self, *a, **k):
                pass
        import evolution_infra
        monkeypatch.setattr(direction_auditor, "PROMPTS_DIR", evolution_infra.PROMPTS_DIR)

        import asyncio
        result = asyncio.run(direction_auditor._run_direction_audit(50, _UI()))
        assert result.get("llm_failed") is not True
        assert result["repetition_detected"] is False


# ---------------------------------------------------------------------------
# C-class sentinel (match analyst + performance analyst)
# ---------------------------------------------------------------------------

LLM_INFRA_SENTINEL = "[LLM_INFRA_ERROR: analysis unavailable]"


class _UI:
    """Minimal UI stub: records log_history calls instead of emitting."""
    def __init__(self):
        self.logs = []

    def log_history(self, msg, level="info", **kw):
        self.logs.append((level, msg))


class TestMatchAnalystSentinel:
    """When the match analyst LLM call crashes with an infra error, it must
    return the sentinel string (NOT "") so _run_master_analysis can surface
    "analysis unavailable due to LLM failure" to the Master."""

    def test_infra_error_returns_sentinel(self, monkeypatch):
        from claude_agent_sdk import ClaudeSDKError
        import agent_master

        async def fake_query(*a, **kw):
            raise ClaudeSDKError("signature error")
        monkeypatch.setattr(agent_master, "run_claude_query", fake_query)

        # Provide enough match-history data so the function reaches the LLM call.
        import evolution_infra
        history = evolution_infra.MATCH_HISTORY_FILE
        history.parent.mkdir(parents=True, exist_ok=True)
        history.write_text(json.dumps({
            "id": "replay-x", "bot0": "claude_v50", "bot1": "claude_v40",
            "bot0_wins": 1, "bot1_wins": 5,
        }) + "\n")

        result = asyncio.run(agent_master._analyze_recent_matches(50, _UI()))
        assert result == LLM_INFRA_SENTINEL

    def test_non_infra_error_returns_empty(self, monkeypatch):
        """Non-infra exceptions still return "" (unchanged behaviour)."""
        import agent_master

        async def fake_query(*a, **kw):
            raise ValueError("parse error")
        monkeypatch.setattr(agent_master, "run_claude_query", fake_query)

        import evolution_infra
        history = evolution_infra.MATCH_HISTORY_FILE
        history.parent.mkdir(parents=True, exist_ok=True)
        history.write_text(json.dumps({
            "id": "replay-x", "bot0": "claude_v50", "bot1": "claude_v40",
            "bot0_wins": 1, "bot1_wins": 5,
        }) + "\n")

        result = asyncio.run(agent_master._analyze_recent_matches(50, _UI()))
        assert result == ""


class TestPerformanceAnalystSentinel:
    """When the performance-verification LLM call crashes with an infra error,
    it must return the sentinel string (NOT "") so _run_master_analysis can
    surface "analysis unavailable due to LLM failure"."""

    def test_infra_error_returns_sentinel(self, monkeypatch):
        from claude_agent_sdk import ClaudeSDKError
        import agent_review

        async def fake_query(*a, **kw):
            raise ClaudeSDKError("signature error")
        monkeypatch.setattr(agent_review, "run_claude_query", fake_query)

        result = asyncio.run(agent_review._run_performance_verification(50, {}, _UI()))
        assert result == LLM_INFRA_SENTINEL

    def test_non_infra_error_returns_empty(self, monkeypatch):
        import agent_review

        async def fake_query(*a, **kw):
            raise ValueError("parse error")
        monkeypatch.setattr(agent_review, "run_claude_query", fake_query)

        result = asyncio.run(agent_review._run_performance_verification(50, {}, _UI()))
        assert result == ""


class TestMasterPromptSentinelDetection:
    """_render_analysis_section maps the sentinel into an explicit warning and
    leaves ordinary text / empty strings unchanged."""

    def test_sentinel_becomes_warning(self):
        import agent_master
        rendered = agent_master._render_analysis_section(LLM_INFRA_SENTINEL, "default")
        assert rendered is agent_master.LLM_INFRA_SENTINEL_MSG

    def test_empty_becomes_default(self):
        import agent_master
        assert agent_master._render_analysis_section("", "default") == "default"
        assert agent_master._render_analysis_section(None, "default") == "default"

    def test_real_text_passes_through(self):
        import agent_master
        assert agent_master._render_analysis_section("real analysis", "default") == "real analysis"


# ---------------------------------------------------------------------------
# 8 advisory agents: llm_failed marker on infra error
# ---------------------------------------------------------------------------

class TestAdvisoryAgentInfraMarkers:
    """Each advisory agent must add llm_failed=True to its safe-default when the
    LLM call raises an infrastructure error (ClaudeSDKError/timeout/connection).

    Control flow is unchanged: the safe-default pass behaviour is preserved —
    only the marker is added so the orchestrator can tell "gate untrustworthy
    due to infra crash" from "gate ran and judged OK"."""

    def _patch_query_to_raise(self, monkeypatch, module, exc):
        async def fake_query(*a, **kw):
            raise exc
        monkeypatch.setattr(module, "run_claude_query", fake_query)

    def test_master_plan_audit_infra_marker(self, monkeypatch):
        from claude_agent_sdk import ClaudeSDKError
        import audit_agents
        self._patch_query_to_raise(monkeypatch, audit_agents, ClaudeSDKError("sig"))
        result = asyncio.run(audit_agents._run_master_plan_audit({"tasks": []}, 50, _UI()))
        assert result["llm_failed"] is True
        assert result["overall_pass"] is True  # safe default preserved

    def test_master_plan_audit_non_infra_no_marker(self, monkeypatch):
        import audit_agents
        self._patch_query_to_raise(monkeypatch, audit_agents, ValueError("parse"))
        result = asyncio.run(audit_agents._run_master_plan_audit({"tasks": []}, 50, _UI()))
        assert result.get("llm_failed") is not True
        assert result["overall_pass"] is True

    def test_precommit_semantic_infra_marker(self, monkeypatch):
        from claude_agent_sdk import ClaudeSDKError
        import audit_agents
        self._patch_query_to_raise(monkeypatch, audit_agents, ClaudeSDKError("sig"))
        result = asyncio.run(audit_agents._run_precommit_semantic(51, 50, [], {"tasks": []}, _UI()))
        assert result["llm_failed"] is True
        assert result["recommended_action"] == "proceed"

    def test_degeneration_diagnosis_infra_marker(self, monkeypatch):
        from claude_agent_sdk import ClaudeSDKError
        import audit_agents
        self._patch_query_to_raise(monkeypatch, audit_agents, ClaudeSDKError("sig"))
        result = asyncio.run(audit_agents._run_degeneration_diagnosis(
            50, "commits", "changes", "curve", _UI()))
        assert result["llm_failed"] is True
        assert result["is_degenerating"] is False

    def test_experience_pool_audit_infra_marker(self, monkeypatch):
        from claude_agent_sdk import ClaudeSDKError
        import audit_agents
        self._patch_query_to_raise(monkeypatch, audit_agents, ClaudeSDKError("sig"))
        result = asyncio.run(audit_agents._run_experience_pool_audit("pool", {}, _UI()))
        assert result["llm_failed"] is True
        assert result["overall_health"] == "healthy"

    def test_regression_guardian_infra_marker(self, monkeypatch):
        from claude_agent_sdk import ClaudeSDKError
        import audit_agents
        self._patch_query_to_raise(monkeypatch, audit_agents, ClaudeSDKError("sig"))
        result = asyncio.run(audit_agents._run_regression_guardian(
            51, 50, [], "trigger", _UI()))
        assert result["llm_failed"] is True
        assert result["severity"] == "minor"

    def test_crossover_compat_infra_marker(self, monkeypatch):
        from claude_agent_sdk import ClaudeSDKError
        import audit_agents
        import evolution_infra
        self._patch_query_to_raise(monkeypatch, audit_agents, ClaudeSDKError("sig"))
        # Minimal parent bot dirs so the audit reaches the LLM call.
        for v in (60, 30):
            d = evolution_infra.BOTS_DIR / f"claude_v{v}"
            d.mkdir(parents=True, exist_ok=True)
            (d / "strategy.py").write_text("# stub")
        result = asyncio.run(audit_agents._run_crossover_compatibility_audit(60, 30, _UI()))
        assert result["llm_failed"] is True
        assert result["compatible"] is True

    def test_worker_cot_check_infra_marker(self, monkeypatch):
        from claude_agent_sdk import ClaudeSDKError
        import audit_agents
        import evolution_infra
        from pathlib import Path
        self._patch_query_to_raise(monkeypatch, audit_agents, ClaudeSDKError("sig"))

        next_v, w_id = 51, 1
        # Worker log must exist + non-empty to reach the LLM call.
        wlog = evolution_infra.get_logs_dir(next_v) / f"worker_{w_id}_io.txt"
        wlog.parent.mkdir(parents=True, exist_ok=True)
        wlog.write_text("worker did some edits")

        # Target file with a non-empty diff vs its snapshot.
        next_dir = evolution_infra.BOTS_DIR / f"claude_v{next_v}"
        next_dir.mkdir(parents=True, exist_ok=True)
        rel = "strategy.py"
        (next_dir / rel).write_text("after")
        snapshots = {(0, rel): "before"}

        task = {"worker_id": w_id, "target_files": [str(next_dir / rel)], "role": "Worker"}
        result = asyncio.run(audit_agents._run_worker_cot_check(
            task, 0, next_v, 50, next_dir, snapshots, _UI()))
        assert result["llm_failed"] is True
        assert result["cot_consistent"] is True

    def test_stagnation_analyzer_infra_marker(self, monkeypatch):
        """_analyze_stagnation returns a marked dict (not None) when every retry
        crashes with an infra error. Neutralise the backoff sleeps and stub the
        coverage helpers so the function reaches the LLM path."""
        from claude_agent_sdk import ClaudeSDKError
        import stagnation_analyzer
        import tool_helpers

        async def fake_query(*a, **kw):
            raise ClaudeSDKError("sig")
        monkeypatch.setattr(stagnation_analyzer, "run_claude_query", fake_query)

        async def no_sleep(*a, **kw):
            return None
        monkeypatch.setattr(asyncio, "sleep", no_sleep)

        # Bypass the data-sufficiency (<0.8 coverage) early-return by stubbing
        # the coverage helper imported inside _analyze_stagnation.
        monkeypatch.setattr(tool_helpers, "load_h2h_avg_winrates",
                            lambda: {"claude_v50": 0.5})
        monkeypatch.setattr(tool_helpers, "load_h2h_avg_winrates_with_coverage",
                            lambda: {"claude_v50": {"opponent_coverage": 1.0,
                                                    "opponents_evaluated": 4,
                                                    "opponents_total": 4,
                                                    "h2h_avg_wr": 0.5}})

        result = asyncio.run(stagnation_analyzer._analyze_stagnation(
            50, ["claude_v40", "claude_v41", "claude_v42", "claude_v43"], {}, _UI()))
        assert result is not None
        assert result["llm_failed"] is True
        assert result["is_stagnant"] is False

    def test_advisory_infra_emits_system_event(self, monkeypatch):
        """An infra crash must also emit a pipeline.<agent>_infra warn event."""
        from claude_agent_sdk import ClaudeSDKError
        import audit_agents
        events = []

        async def fake_query(*a, **kw):
            raise ClaudeSDKError("sig")
        monkeypatch.setattr(audit_agents, "run_claude_query", fake_query)
        monkeypatch.setattr(audit_agents, "log_system_event",
                            lambda et, sev, msg, data=None: events.append((et, sev, msg, data)))
        asyncio.run(audit_agents._run_master_plan_audit({"tasks": []}, 50, _UI()))
        assert any(et == "pipeline.master_plan_audit_infra" and sev == "warn"
                   for et, sev, msg, data in events)
