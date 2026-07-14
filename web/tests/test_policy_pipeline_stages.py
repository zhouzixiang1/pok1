"""Current-epoch pipeline boundary checks for the strict policy ABI."""

from bot_namespace import STRICT_ARTIFACT_FILES, strict_artifact_layout_errors
from tool_planning import _validate_master_plan


def _task(*targets):
    return {
        "worker_id": 1,
        "role": "Algorithmic Logic Architect",
        "target_files": list(targets),
        "skill_layer": "spr",
        "worker_prompt": (
            "Change the reachable turn decision branch while preserving typed "
            "intent legality and the system-owned runtime boundary."
        ),
    }


def test_current_master_stage_accepts_only_the_policy_write_boundary():
    plan = {"tasks": [_task("policy.py")]}
    errors, _warnings = _validate_master_plan(plan, next_v=143)
    assert errors == []

    for forbidden in ("strategy.py", "national_bot.py", "precompute.py"):
        errors, _warnings = _validate_master_plan(
            {"tasks": [_task(forbidden)]},
            next_v=143,
        )
        assert any("writable scope must be exactly ['policy.py']" in item for item in errors)


def test_current_candidate_stage_requires_the_exact_five_file_artifact(tmp_path):
    bot = tmp_path / "national_v143"
    bot.mkdir()
    for relative in STRICT_ARTIFACT_FILES:
        (bot / relative).write_text("{}\n" if relative.endswith(".json") else "# test\n")
    assert strict_artifact_layout_errors(bot) == []

    (bot / "strategy.py").write_text("# retired ABI\n")
    assert "artifact_extra_file_forbidden:strategy.py" in strict_artifact_layout_errors(bot)
