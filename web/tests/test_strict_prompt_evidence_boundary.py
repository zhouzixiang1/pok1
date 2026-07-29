"""Strict-policy prompts must not reopen retired mutable evidence streams."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from bot_namespace import bot_name, bot_tag
from conftest import STRICT_SOURCE_V


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
        if tag == bot_tag(143):
            return "strict v143 typed policy foundation"
        if tag == bot_tag(145):
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

    # The archived/below-floor bot must never appear in the completion history.
    # On main the floor is 142 so v141 is archived; on cloud the floor is 0 so
    # we use the branch-aware STRICT_SOURCE_V (0 on cloud -> name does not parse
    # as an active bot and is skipped by the auditor).
    archived_v = STRICT_SOURCE_V if STRICT_SOURCE_V > 0 else 141
    archived_name = bot_name(archived_v) if STRICT_SOURCE_V > 0 else f"archived_legacy_v{STRICT_SOURCE_V}"
    monkeypatch.setattr(direction_auditor, "PROMPTS_DIR", prompts)
    monkeypatch.setattr(
        direction_auditor,
        "strict_published_bot_names",
        lambda: (archived_name, bot_name(143), bot_name(145)),
    )
    monkeypatch.setattr(direction_auditor, "get_logs_dir", lambda _v: tmp_path)
    monkeypatch.setattr(direction_auditor, "run_claude_query", fake_query)
    monkeypatch.setattr(evolution_infra, "_git", fake_git)
    monkeypatch.setattr(evolution_infra, "git_get_parent", lambda _v: None)

    result = asyncio.run(direction_auditor._run_direction_audit(145, _UI()))

    assert result["repetition_detected"] is False
    assert "v143" in captured["prompt"]
    assert "v145" in captured["prompt"]
    assert f"v{archived_v}" not in captured["prompt"]
    assert archived_name not in captured["prompt"]
    # The canonical template itself explains that archived critic prose is
    # forbidden; assert the injected history contains only completion commits
    # instead of treating that policy word as evidence contamination.
    assert "raw untagged critic output" not in captured["prompt"].lower()
    assert "raw worker failure payload" not in captured["prompt"].lower()


def test_master_plan_audit_history_uses_only_annotated_strict_completions(
    monkeypatch,
):
    import audit_agents
    import evolution_infra
    import national_runtime_authority
    from bot_namespace import bot_name, bot_tag
    from conftest import STRICT_SOURCE_V, STRICT_TARGET_V

    # Two current-epoch strict publications plus one archived/below-floor name
    # that must never appear in the completion history.
    v_first = STRICT_TARGET_V
    v_later = STRICT_TARGET_V + 2
    first_name = bot_name(v_first)
    later_name = bot_name(v_later)
    archived_name = bot_name(STRICT_SOURCE_V) if STRICT_SOURCE_V > 0 else "archived_legacy_v0"

    monkeypatch.setattr(
        national_runtime_authority,
        "strict_published_bot_names",
        lambda: (archived_name, first_name, later_name),
    )

    first_commit = "a" * 40
    later_commit = "b" * 40

    def fake_git(*args, **_kwargs):
        assert args[0] != "log", "ordinary Git commit windows are forbidden"
        if args[:2] == ("cat-file", "-t"):
            return "tag"
        if args[0] == "rev-parse":
            tag = args[1].split("^{", 1)[0]
            return first_commit if tag == bot_tag(v_first) else later_commit
        if args[:3] == ("show", "-s", "--format=%B"):
            return (
                "strict first typed policy foundation"
                if args[3] == first_commit
                else "strict later opponent evidence consumer"
            )
        raise AssertionError(f"unexpected Git query: {args}")

    monkeypatch.setattr(evolution_infra, "_git", fake_git)

    history = audit_agents._strict_completion_commit_history(limit=5)

    assert f"v{v_first} [{bot_tag(v_first)}]" in history
    assert f"v{v_later} [{bot_tag(v_later)}]" in history
    assert f"v{STRICT_SOURCE_V} " not in history if STRICT_SOURCE_V > 0 else True
    assert "archived_legacy_v0" not in history
    assert "infrastructure" not in history


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
                "recommended_source": bot_name(143),
                "source_rationale": "only strict frozen row",
                "causal_analysis": (
                    "policy.py threshold change definitely caused the rating gain"
                ),
            }),
            None,
            None,
        )

    row = {
        "name": bot_name(143),
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
        active_bots=[bot_name(143)],
        ratings={bot_name(143): Glicko2Player(r=1510, rd=80, sigma=0.06)},
        ui=_UI(),
        h2h_data={},
        bot_stats_data={bot_name(143): {"games": 20, "win_rate": 0.55}},
        selection_rows_data=[row],
        rating_history_data=[],
    ))

    assert result["trend"] == "improving"
    assert sentinel not in captured["prompt"]
    assert "Recent Failures" not in captured["prompt"]
    assert "critic_insights" not in captured["prompt"]
    assert result["causal_analysis"] == combined_analyst.CAUSAL_ANALYSIS_UNKNOWN
    assert "no content-bound artifact/diff digest" in captured["prompt"]


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
    assert "_strict_completion_commit_history" in scheduler
    assert "subprocess.run" not in scheduler
    assert '["git", "log", bot_tag' not in scheduler
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
        stagnation_info="POISON_RETIRED_STRENGTH",
        match_analysis="POISON_RETIRED_MATCH",
        replay_spotlight="POISON_RETIRED_REPLAY",
        performance_verification="POISON_RETIRED_OFFICIAL",
    )

    prompt = orchestrator_context._build_context(gen_ctx=generation)

    assert "Source bot: NONE" in prompt
    assert "Source bot: national_v142" not in prompt
    assert "v142 is archived version authority only" in prompt
    assert "PROTOCOL BOOTSTRAP NO-STRENGTH" in prompt
    assert "POISON_RETIRED" not in prompt


def test_strict_llm_gate_identity_never_fingerprints_numeric_high_water(
    monkeypatch,
    tmp_path,
):
    import tool_gates

    candidate = tmp_path / bot_name(143)
    candidate.mkdir()
    seen = []

    def fingerprint(path):
        seen.append(path)
        return "c" * 64

    monkeypatch.setattr(tool_gates, "_bot_code_fingerprint", fingerprint)
    numeric_identity = "n" * 64
    _key, metadata = tool_gates._llm_gate_infrastructure_identity(
        component="reviewer_llm",
        role="LEAD CODE REVIEWER",
        candidate_dir=candidate,
        source_dir=None,
        prompt_text="strict prepared target only",
        checkpoint={},
        source_fingerprint_override=numeric_identity,
    )

    assert seen == [candidate]
    assert metadata["source_fingerprint"] == numeric_identity
