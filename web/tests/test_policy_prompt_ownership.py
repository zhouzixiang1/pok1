from pathlib import Path

import pytest

import tool_planning
from output_schema import CrossoverCompatibilityResult, WorkerTask
from strategy_reference_pack import (
    get_reference_card,
    reference_pack_ids,
    validate_reference_task,
)


ROOT = Path(__file__).resolve().parents[2]
PROMPTS = ROOT / "web" / "core" / "prompts"
DECISION_CONTEXT_SECTIONS = {
    "cards",
    "hand",
    "betting",
    "history",
    "line",
    "legal",
    "opponent",
    "deadline",
}


def test_strategy_cards_use_current_context_and_policy_only():
    for reference_id in reference_pack_ids():
        card = get_reference_card(reference_id)
        assert card is not None
        assert card.allowed_files == ("policy.py",)
        fields = (
            *card.required_decision_context_fields,
            *card.required_any_decision_context_fields,
        )
        assert fields
        assert {field.split(".", 1)[0] for field in fields} <= DECISION_CONTEXT_SECTIONS
        assert not any(
            field.startswith(("hand_runtime.", "opponent_runtime."))
            for field in fields
        )

        prompt = " ".join(card.required_worker_terms)
        assert validate_reference_task(
            reference_id,
            card.primary_innovations[0],
            target_files=["policy.py"],
            worker_prompt=prompt,
        ) == []
        errors = validate_reference_task(
            reference_id,
            card.primary_innovations[0],
            target_files=["policy.py", "helper.py"],
            worker_prompt=prompt,
        )
        assert any("requires exactly ['policy.py']" in error for error in errors)


def test_worker_schema_rejects_every_non_policy_write_target():
    base = {
        "worker_id": 1,
        "role": "Algorithmic Logic Architect",
        "target_files": ["policy.py"],
        "skill_layer": "preflop_range",
        "worker_prompt": "Modify one reachable typed-intent branch in policy.py only.",
    }
    assert WorkerTask.model_validate(base).target_files == ["policy.py"]

    for forbidden in ("helper.py", "precompute.py", "national_bot.py"):
        with pytest.raises(ValueError, match="writable scope must be exactly"):
            WorkerTask.model_validate({**base, "target_files": [forbidden]})
    with pytest.raises(ValueError, match="writable scope must be exactly"):
        WorkerTask.model_validate({**base, "files_allowed": ["helper.py"]})


def test_master_and_repair_routing_are_policy_only_and_system_fail_closed():
    plan = {
        "tasks": [{
            "worker_id": 1,
            "role": "Algorithmic Logic Architect",
            "target_files": ["policy.py", "helper.py"],
            "skill_layer": "preflop_range",
            "worker_prompt": "Modify one reachable typed-intent branch in policy.py only.",
        }],
    }
    errors, _warnings = tool_planning._validate_master_plan(
        plan,
        next_v=143,
    )
    assert any("writable scope must be exactly ['policy.py']" in error for error in errors)

    assert tool_planning._precommit_filter_repair_targets(
        ["policy.py", "helper.py", "national_bot.py", "precompute.py"],
        allow_protocol_files=True,
    ) == ["policy.py"]
    assert tool_planning._national_native_contracts(
        {"national_native_contract_ok": False},
        ["national_bot.py missing"],
    ) == []
    assert tool_planning._official_smoke_contracts(
        {"official_smoke_blocking": True},
        ["protocol violation"],
    ) == []
    protocol_checkpoint = {
        "gate_results": {
            "precommit": {"failures": ["official platform illegal wire output"]},
            "official_full": {"issues": ["protocol violation in national_bot.py"]},
        }
    }
    assert tool_planning._precommit_repair_target_files(
        protocol_checkpoint,
        "official platform illegal wire output",
    ) == []
    assert tool_planning._official_repair_target_files(
        protocol_checkpoint,
        "protocol violation",
    ) == []


def test_crossover_schema_accepts_no_candidate_file_but_policy():
    valid = CrossoverCompatibilityResult(
        compatibility_score=7,
        files_to_take_from_a=["policy.py"],
        files_to_take_from_b=["policy.py"],
    )
    assert valid.files_to_take_from_a == ["policy.py"]
    with pytest.raises(ValueError, match="may select only policy.py"):
        CrossoverCompatibilityResult(
            compatibility_score=7,
            files_to_take_from_b=["constants.py"],
        )


def test_active_prompts_expose_only_the_current_candidate_abi():
    worker = (PROMPTS / "worker_prompt.md").read_text(encoding="utf-8")
    crossover = (PROMPTS / "crossover_prompt.md").read_text(encoding="utf-8")
    compatibility = (PROMPTS / "crossover_compatibility.md").read_text(
        encoding="utf-8"
    )
    orchestrator = (PROMPTS / "orchestrator.md").read_text(encoding="utf-8")

    assert "must target `policy.py` only" in worker
    assert "Edit only `bots/national_v{version}/policy.py`" in crossover
    assert "Compare only candidate-owned `policy.py`" in compatibility
    assert "constants.py" not in worker
    assert "constants.py" not in compatibility
    assert "bootstrap-first-strict" in orchestrator
    assert "`bootstrap-full`" not in orchestrator


def test_no_active_planning_path_calls_retired_fix_injection():
    for relative in (
        "web/core/tool_planning.py",
        "web/core/agent_review.py",
        "web/core/audit_agents.py",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "apply_known_fixes" not in text
        assert "from fix_injection" not in text
