"""Strict-policy prompts must not reopen retired mutable evidence streams."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace


class _UI:
    costs = {}

    def log_history(self, *_args, **_kwargs):
        pass

    def clear_io(self):
        pass

    def set_status(self, *_args, **_kwargs):
        pass

    def log_io(self, *_args, **_kwargs):
        pass


def test_direction_audit_uses_only_strict_published_completion_commits(
    tmp_path,
    monkeypatch,
):
    import direction_auditor
    import evolution_infra

    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "direction_auditor_prompt.md").write_text(
        "STRICT HISTORY\n{generation_history}",
        encoding="utf-8",
    )
    captured = {}

    def fake_git(*args, **_kwargs):
        assert args[0] == "log", "raw tag enumeration is forbidden"
        tag = args[1]
        if tag == "national-bot-v143":
            return "strict v143 typed policy foundation"
        if tag == "national-bot-v145":
            return "strict v145 opponent evidence consumer"
        raise AssertionError(f"unexpected completion identity: {tag}")

    async def fake_query(prompt, *_args, **_kwargs):
        captured["prompt"] = prompt
        return (
            json.dumps({
                "last_directions": [],
                "repetition_detected": False,
                "repetition_count": 0,
                "exhausted_directions": [],
                "mandatory_constraints": None,
                "suggested_direction": None,
                "confidence": "high",
            }),
            None,
            None,
        )

    monkeypatch.setattr(direction_auditor, "PROMPTS_DIR", prompts)
    monkeypatch.setattr(
        direction_auditor,
        "strict_published_bot_names",
        lambda: ("national_v141", "national_v143", "national_v145"),
    )
    monkeypatch.setattr(direction_auditor, "get_logs_dir", lambda _v: tmp_path)
    monkeypatch.setattr(direction_auditor, "run_claude_query", fake_query)
    monkeypatch.setattr(evolution_infra, "_git", fake_git)
    monkeypatch.setattr(evolution_infra, "git_get_parent", lambda _v: None)

    result = asyncio.run(direction_auditor._run_direction_audit(145, _UI()))

    assert result["repetition_detected"] is False
    assert "v143" in captured["prompt"]
    assert "v145" in captured["prompt"]
    assert "v141" not in captured["prompt"]
    assert "critic" not in captured["prompt"].lower()
    assert "worker failure" not in captured["prompt"].lower()


def test_combined_prompt_is_built_only_from_supplied_frozen_bundle(
    tmp_path,
    monkeypatch,
):
    import combined_analyst
    import evolution_infra
    from glicko2 import Glicko2Player

    sentinel = "RETIRED_FAILURE_OR_CRITIC_SENTINEL"
    results = tmp_path / "results"
    results.mkdir()
    (results / "worker_failures.jsonl").write_text(
        json.dumps({"error": sentinel}) + "\n",
        encoding="utf-8",
    )
    (results / "archive").mkdir()
    (results / "archive" / "v142.json").write_text(
        json.dumps({"critic_data": {"strategic_assessment": sentinel}}),
        encoding="utf-8",
    )
    captured = {}

    async def fake_query(prompt, *_args, **_kwargs):
        captured["prompt"] = prompt
        return (
            json.dumps({
                "is_stagnant": False,
                "confidence": "high",
                "trend": "improving",
                "diversity_needed": False,
                "diversity_reason": None,
                "recommendation": "continue",
                "branch_from": None,
                "verified_improvements": [],
                "persistent_weaknesses": [],
                "reason": "frozen evidence is sufficient",
                "suggestion": None,
                "recommended_source": "national_v143",
                "source_rationale": "only strict frozen row",
                "causal_analysis": "no mutable sidecar was consulted",
            }),
            None,
            None,
        )

    row = {
        "name": "national_v143",
        "selection_score": 0.55,
        "leaderboard_score": 0.55,
        "h2h_avg_wr": 0.55,
        "h2h_coverage": 1.0,
        "h2h_opponents": 1,
        "h2h_opponents_total": 1,
        "h2h_games": 20,
        "games": 20,
        "win_rate": 0.55,
    }
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results)
    monkeypatch.setattr(evolution_infra, "git_get_parent", lambda _v: None)
    monkeypatch.setattr(combined_analyst, "get_logs_dir", lambda _v: tmp_path)
    monkeypatch.setattr(
        combined_analyst,
        "_statistical_stagnation_check",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(combined_analyst, "run_claude_query", fake_query)

    result = asyncio.run(combined_analyst._run_combined_analysis(
        source_v=143,
        active_bots=["national_v143"],
        ratings={"national_v143": Glicko2Player(r=1510, rd=80, sigma=0.06)},
        ui=_UI(),
        h2h_data={},
        bot_stats_data={"national_v143": {"games": 20, "win_rate": 0.55}},
        selection_rows_data=[row],
        rating_history_data=[],
    ))

    assert result["trend"] == "improving"
    assert sentinel not in captured["prompt"]
    assert "Recent Failures" not in captured["prompt"]
    assert "critic_insights" not in captured["prompt"]


def test_prompt_builders_have_no_retired_positive_read_chain():
    core = Path(__file__).resolve().parents[1] / "core"
    orchestrator = (core / "orchestrator_context.py").read_text(encoding="utf-8")
    combined = (core / "combined_analyst.py").read_text(encoding="utf-8")
    direction = (core / "direction_auditor.py").read_text(encoding="utf-8")
    workers = (core / "agent_workers.py").read_text(encoding="utf-8")
    planning = (core / "tool_planning.py").read_text(encoding="utf-8")
    scheduler = (core / "generation_scheduler.py").read_text(encoding="utf-8")
    master = (core / "agent_master.py").read_text(encoding="utf-8")

    assert "EvalRoundManager" not in orchestrator
    assert "_load_recent_failures" not in orchestrator
    assert "WORKER_FAILURES_FILE" not in combined
    assert "WORKER_FAILURES_FILE" not in direction
    assert "prev_critic_info" not in combined
    assert "prev_critic_info" not in scheduler
    assert "eval_round_summary" not in master
    assert "build_worker_execution_context" not in workers
    assert ".get(\"worker_execution_context\")" not in planning


def test_fresh_bootstrap_orchestrator_never_labels_v142_as_source(monkeypatch):
    import evolution_core
    import orchestrator_context

    monkeypatch.setattr(evolution_core, "get_active_bots", lambda: [])
    monkeypatch.setattr(evolution_core, "read_pipeline_checkpoint", lambda: None)
    generation = SimpleNamespace(
        current_v=142,
        next_v=143,
        source_v=142,
        strategy="fresh_policy_bootstrap",
        crossover_parents=(),
        stagnation_info="No retired strategy evidence is admissible.",
        match_analysis="",
        replay_spotlight="",
        performance_verification="",
    )

    prompt = orchestrator_context._build_context(gen_ctx=generation)

    assert "Source bot: NONE" in prompt
    assert "Source bot: national_v142" not in prompt
    assert "v142 is archived version authority only" in prompt
