from tool_planning import _validate_master_plan


def test_master_plan_rejects_tuner_files_allowed_escape_hatch():
    plan = {
        "tasks": [{
            "worker_id": 1,
            "role": "Hyperparameter Tuner",
            "target_files": ["constants.py"],
            "files_allowed": ["strategy.py"],
            "worker_prompt": "Tune numeric constants only.",
        }]
    }

    errors, warnings = _validate_master_plan(plan, next_v=250)

    assert warnings == []
    assert errors
    assert "target_files/files_allowed" in errors[0]
    assert "strategy.py" in errors[0]
