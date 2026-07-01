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


def test_auxiliary_prompts_block_national_protocol_misleading_plans():
    assert "National rules safety" in _prompt("master_plan_audit.md")
    assert "national protocol legality assumptions" in _prompt("crossover_compatibility.md")

    dynamic_prompt = _prompt("dynamic_test_generator.md")
    assert "never `check/check`" in dynamic_prompt
    assert "postflop first action" in dynamic_prompt


def test_decision_templates_use_call_to_pass_after_postflop_check():
    source = (ROOT / "web" / "core" / "decision_tester.py").read_text(encoding="utf-8")

    assert not re.search(
        r'"round": 2,\s*"player_id": 1,\s*"action": 0,\s*"action_type": "check"',
        source,
    )
    assert re.search(
        r'"round": 2,\s*"player_id": 1,\s*"action": 0,\s*"action_type": "call"',
        source,
    )
