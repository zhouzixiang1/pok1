import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PROMPTS = ROOT / "web" / "core" / "prompts"


def _prompt(name: str) -> str:
    return (PROMPTS / name).read_text(encoding="utf-8")


def test_core_prompts_include_full_national_legality_rules():
    prompt_names = [
        "initial_prompt.md",
        "worker_prompt.md",
        "master_prompt.md",
        "reviewer_prompt.md",
        "crossover_prompt.md",
    ]
    combined = "\n".join(_prompt(name) for name in prompt_names)

    required_phrases = [
        "sever/国赛平台",
        "raise-to-total",
        "prev * 2 + 1",
        "postflop first action cannot be",
        "check is illegal",
        "Preflop BB cannot",
        "all remaining chips",
        "consecutive all-ins are illegal",
    ]
    for phrase in required_phrases:
        assert phrase in combined


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


def test_regression_guardian_prompt_matches_current_trigger_contract():
    guardian_prompt = _prompt("regression_guardian.md")
    tool_gates = (ROOT / "web" / "core" / "tool_gates.py").read_text(encoding="utf-8")

    assert "currently called only from `run_critic`" in guardian_prompt
    assert "advisory critic score is below 4" in guardian_prompt
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
