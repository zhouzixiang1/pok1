import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PROMPTS = ROOT / "web" / "core" / "prompts"


def _prompt(name: str) -> str:
    return (PROMPTS / name).read_text(encoding="utf-8")


def _native_worker_prompt() -> str:
    return _prompt("worker_prompt.md").replace(
        "{execution_profile_contract}",
        _prompt("worker_profile_national_native.md"),
    )


def test_core_prompts_include_full_national_legality_rules():
    prompt_names = [
        "initial_prompt.md",
        "worker_profile_national_native.md",
        "master_prompt.md",
        "reviewer_prompt.md",
        "crossover_prompt.md",
    ]
    combined = "\n".join(_prompt(name) for name in prompt_names)

    required_phrases = [
        "sever/国赛平台",
        "raise-to-total",
        "Exact `prev * 2` is legal",
        "conservative",
        "postflop first action cannot be",
        "check is illegal",
        "Preflop BB cannot",
        "all remaining chips",
        "consecutive all-ins are illegal",
    ]
    for phrase in required_phrases:
        assert phrase in combined

    forbidden_legality_claims = [
        "strictly greater than 2x",
        "strictly >2x",
        "minimum valid re-raise after raise X is X*2+1",
    ]
    for claim in forbidden_legality_claims:
        assert claim not in combined


def test_active_generation_prompts_use_national_bot_namespace():
    prompt_names = [
        "initial_prompt.md",
        "combined_analyst.md",
        "stagnation_analyzer.md",
        "orchestrator.md",
        "master_plan_audit.md",
        "crossover_prompt.md",
        "reviewer_prompt.md",
        "critic_prompt.md",
    ]
    combined = "\n".join(_prompt(name) for name in prompt_names)

    forbidden_patterns = [
        r"bots/claude_v",
        r"\bclaude_vN\b",
        r'"claude_v',
        r"`bot-v",
        r"\sbot-v",
        r"(?<!national-)bot-vN",
        r"(?<!national-)bot-v\{",
    ]
    for pattern in forbidden_patterns:
        assert not re.search(pattern, combined), pattern

    required_fragments = [
        "bots/national_v",
        "national_vN",
        "national-bot-v",
        "national_bot.py",
        "sever/bot_adapter.py",
    ]
    for fragment in required_fragments:
        assert fragment in combined


def test_auxiliary_prompts_block_national_protocol_misleading_plans():
    assert "National rules safety" in _prompt("master_plan_audit.md")
    assert "national protocol legality assumptions" in _prompt("crossover_compatibility.md")

    dynamic_prompt = _prompt("dynamic_test_generator.md")
    assert "never `check/check`" in dynamic_prompt
    assert "postflop first action" in dynamic_prompt


def test_tuner_prompt_contract_matches_planning_hard_gate():
    master_prompt = _prompt("master_prompt.md")
    worker_prompt = _prompt("worker_prompt.md")

    assert '`target_files` must be exactly `["constants.py"]`' in master_prompt
    assert "strategy_helpers.py" in master_prompt
    assert "do not label that task as Tuner" in master_prompt

    assert "must target constants.py only" in worker_prompt
    assert "report BLOCKED instead of searching other .py files" in worker_prompt
    assert "search all .py files" not in worker_prompt


def test_prompts_require_structured_battle_memory_citations():
    master_prompt = _prompt("master_prompt.md")
    worker_prompt = _prompt("worker_prompt.md")

    assert "battle_lessons.jsonl" in master_prompt
    assert "battle_evidence.jsonl" in master_prompt
    assert "lesson_id" in master_prompt
    assert "evidence_id" in master_prompt
    assert "Pending Battle Summaries" in master_prompt

    assert "battle_lesson_*" in worker_prompt
    assert "ev_*" in worker_prompt


def test_prompts_require_national_runtime_architecture_contracts():
    master_prompt = _prompt("master_prompt.md")
    worker_prompt = _native_worker_prompt()
    reviewer_prompt = _prompt("reviewer_prompt.md")
    critic_prompt = _prompt("critic_prompt.md")

    assert "Decision-time budget" in master_prompt
    assert "Official EXE Compliance Feedback" in master_prompt
    assert "{official_feedback}" in master_prompt
    assert "National Runtime Architecture Feedback" in master_prompt
    assert "{runtime_feedback}" in master_prompt
    assert "`runtime_contract` object" in master_prompt
    assert '"runtime_contract":' in master_prompt
    assert "bounded module-import precomputation" in worker_prompt
    assert "process persists for all 70 hands" in worker_prompt
    assert "# Runtime Contract" in worker_prompt
    assert "Runtime architecture check" in reviewer_prompt
    assert "incremental" in reviewer_prompt
    assert "baseline_passed_check" in reviewer_prompt
    assert "sparse snapshot" in reviewer_prompt
    assert "get_baseline_action" in worker_prompt
    assert "late result" in worker_prompt
    assert "Official Windows EXE artifacts are compliance evidence only" in critic_prompt
    assert "Never use EXE" in critic_prompt


def test_prompts_never_promote_web_arena_to_official_or_strength_authority():
    orchestrator = _prompt("orchestrator.md")
    worker = _prompt("worker_profile_national_native.md")
    reviewer = _prompt("reviewer_prompt.md")
    critic = _prompt("critic_prompt.md")
    official_analyst = _prompt("official_platform_analysis.md")

    assert "Arena completion" in orchestrator
    assert "Only a valid content-bound certificate" in orchestrator
    assert "local debugging and presentation harness" in worker
    assert "do not claim Arena success proves official compliance" in worker
    assert "Arena THP" in reviewer
    assert "non-strength, local diagnostic evidence" in critic
    assert "deterministic EXE verdict" in official_analyst


def test_worker_execution_profiles_do_not_mix_native_and_botzone_verification():
    common = _prompt("worker_prompt.md")
    native = _native_worker_prompt()
    legacy = common.replace(
        "{execution_profile_contract}",
        _prompt("worker_profile_legacy_adapter.md"),
    )

    assert common.count("{execution_profile_contract}") == 1
    assert "current_request_view" in native
    assert "complete match history" in native
    assert "require_current_stream_decoder=True" in native
    assert "require_current_decision_runtime=True" in native
    assert "smoke_tester.py" not in native
    assert "emit exactly one JSON" not in native
    assert "smoke_tester.py" in legacy
    assert "emit exactly one JSON" in legacy


def test_master_and_crossover_prompts_do_not_embed_generation_case_history():
    for name in ("master_prompt.md", "crossover_prompt.md"):
        prompt = _prompt(name)
        assert "Known Mandatory Fixes" not in prompt
        assert not re.search(r"\bv\d{2,}\b", prompt), name
        assert not re.search(r"\bL\d{3,}\b", prompt), name
        assert "Deterministic Invariants" in prompt


def test_readonly_review_prompts_ban_temp_redirect_probes():
    for name in ("reviewer_prompt.md", "critic_prompt.md", "master_prompt.md", "master_plan_audit.md"):
        text = _prompt(name)
        assert "read-only" in text
        assert "Do not create temp files" in text
        assert "write redirects" in text
        assert "`/dev/null`" in text
        assert "diff -u" in text or "direct read-only commands" in text


def test_master_prompts_prioritize_h2h_snapshot_over_spotlight_samples():
    master_prompt = _prompt("master_prompt.md")
    audit_prompt = _prompt("master_plan_audit.md")
    critic_prompt = _prompt("critic_prompt.md")

    assert "The stable H2H snapshot is authoritative" in master_prompt
    assert "Replay Spotlight, match_history excerpts" in master_prompt
    assert "must not override" in master_prompt
    assert "`games`, `a_wins`, `b_wins`, and `win_rate`" in master_prompt
    assert "canonical_citation" in master_prompt
    assert "Never read `web/core/results/head_to_head.json`" in master_prompt

    assert "Reject plans that use replay spotlight" in audit_prompt
    assert "short-window samples" in audit_prompt
    assert "snapshot row key and exact `games`, `a_wins`, `b_wins`, and `win_rate`" in audit_prompt

    assert "Replay spotlight is hand-level evidence only" in critic_prompt
    assert "prefer H2H for matchup claims" in critic_prompt


def test_prompts_require_reachable_embedded_selftests():
    master_prompt = _prompt("master_prompt.md")
    worker_prompt = _prompt("worker_prompt.md")

    assert 'if __name__ == "__main__"' in master_prompt
    assert 'if __name__ == "__main__"' in worker_prompt
    assert "_self_test_*" in master_prompt
    assert "_self_test_*" in worker_prompt
    assert "Never leave a new top-level `_self_test_*`" in worker_prompt
    assert "standalone `_self_test_*` helpers uncalled" in master_prompt


def test_regression_guardian_prompt_matches_current_trigger_contract():
    guardian_prompt = _prompt("regression_guardian.md")
    tool_gates = (ROOT / "web" / "core" / "tool_gates.py").read_text(encoding="utf-8")

    assert "currently called only from `run_critic`" in guardian_prompt
    assert "advisory critic score is below 4" in guardian_prompt
    assert "Neither the Critic nor this Guardian can block or certify" in guardian_prompt
    assert "local native-TCP precommit evaluation is the strategy hard gate" in guardian_prompt
    assert "do not automatically invoke this Guardian" in guardian_prompt
    assert "Precommit eval blocks a commit" not in guardian_prompt
    assert "2+ consecutive generations show rating decline" not in guardian_prompt

    assert "_run_regression_guardian" in tool_gates
    assert "score_num < 4" in tool_gates


def test_decision_templates_use_call_to_pass_after_postflop_check():
    import sys

    sys.path.insert(0, str(ROOT / "web" / "core"))
    import decision_tester

    for scenario in decision_tester.TEMPLATE_SCENARIOS:
        by_round = {}
        for action in scenario["input"].get("history", []):
            by_round.setdefault(action.get("round"), []).append(action)
        for street, actions in by_round.items():
            if street in (1, 2, 3) and len(actions) >= 2 and actions[0].get("action_type") == "check":
                assert actions[1].get("action_type") != "check", scenario["id"]
                if actions[1].get("action") == 0:
                    assert actions[1].get("action_type") == "call", scenario["id"]
