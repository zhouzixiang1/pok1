from tool_planning import _validate_master_plan


def test_master_plan_accepts_policy_only_tuner_scope():
    plan = {
        "tasks": [{
            "worker_id": 1,
            "role": "Hyperparameter Tuner",
            "target_files": ["policy.py"],
            "files_allowed": [],
            "worker_prompt": "Tune existing numeric values in policy.py only.",
        }]
    }

    errors, warnings = _validate_master_plan(plan, next_v=143)

    assert errors == []
    assert warnings == []


def test_master_plan_rejects_retired_module_escape_hatch():
    plan = {
        "tasks": [{
            "worker_id": 1,
            "role": "Hyperparameter Tuner",
            "target_files": ["constants.py"],
            "files_allowed": ["strategy.py"],
            "worker_prompt": "Tune numeric constants only.",
        }]
    }

    errors, warnings = _validate_master_plan(plan, next_v=143)

    assert warnings == []
    assert errors
    assert "writable scope must be exactly ['policy.py']" in errors[0]
    assert "constants.py" in errors[0]
    assert "strategy.py" in errors[0]


def test_master_plan_rejects_system_owned_runtime_target():
    plan = {
        "tasks": [{
            "worker_id": 1,
            "role": "Algorithmic Logic Architect",
            "target_files": ["national_bot.py"],
            "files_allowed": [],
            "worker_prompt": "Rewrite the runtime.",
        }]
    }

    errors, warnings = _validate_master_plan(plan, next_v=143)

    assert warnings == []
    assert errors
    assert "System files" in errors[0]
