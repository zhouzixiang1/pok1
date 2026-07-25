"""Strict-policy regressions for rollback, roles, and Master-plan authority."""

import pytest

import agent_workers as workers
from output_schema import MasterPlan, WorkerTask
from tool_helpers import normalize_worker_role
from tool_planning import _validate_master_plan


def _setup_dirs(tmp_path):
    source = tmp_path / "bots" / "national_v143"
    candidate = tmp_path / "bots" / "national_v144"
    source.mkdir(parents=True)
    candidate.mkdir(parents=True)
    for root, policy in ((source, "SOURCE"), (candidate, "SOURCE + SIBLING EDIT")):
        (root / "national_bot.py").write_text("# system runtime\n")
        (root / "precompute.py").write_text("FACT = 1\n")
        (root / "policy.py").write_text(policy)
        (root / "national_runtime_manifest.json").write_text("{}\n")
        (root / "policy_epoch_receipt.json").write_text("{}\n")
    return source, candidate


def test_policy_retry_restores_its_pre_worker_baseline(tmp_path, monkeypatch):
    _source, candidate = _setup_dirs(tmp_path)
    (candidate / "policy.py").write_text("PARTIAL FAILED EDIT")
    monkeypatch.setattr(
        workers,
        "get_bot_dir",
        lambda version: tmp_path / "bots" / f"national_v{version}",
    )

    workers._reset_target_files_to_source(
        {"target_files": ["policy.py"]},
        143,
        candidate,
        144,
        baseline_snapshots={(0, "policy.py"): "SOURCE + SIBLING EDIT"},
        task_idx=0,
    )
    assert (candidate / "policy.py").read_text() == "SOURCE + SIBLING EDIT"


def test_policy_retry_without_snapshot_restores_parent(tmp_path, monkeypatch):
    _source, candidate = _setup_dirs(tmp_path)
    (candidate / "policy.py").write_text("PARTIAL FAILED EDIT")
    monkeypatch.setattr(
        workers,
        "get_bot_dir",
        lambda version: tmp_path / "bots" / f"national_v{version}",
    )

    workers._reset_target_files_to_source(
        {"target_files": ["policy.py"]}, 143, candidate, 144
    )
    assert (candidate / "policy.py").read_text() == "SOURCE"


def test_undeclared_helper_is_removed_without_touching_strict_files(tmp_path):
    _source, candidate = _setup_dirs(tmp_path)
    before = {path.name for path in candidate.glob("*.py")}
    (candidate / "helper.py").write_text("# forbidden worker output\n")

    workers._unlink_undeclared_new_files(candidate, before)

    assert not (candidate / "helper.py").exists()
    assert (candidate / "policy.py").is_file()
    assert (candidate / "national_bot.py").is_file()
    assert (candidate / "precompute.py").is_file()


def test_mixed_tuner_role_uses_the_stricter_tuner_boundary():
    assert normalize_worker_role("Algorithmic Logic Architect + Tuner") == "tuner"
    assert normalize_worker_role("Opponent Modeler") == "other"


def _valid_task(**overrides):
    task = {
        "worker_id": 1,
        "role": "Algorithmic Logic Architect",
        "target_files": ["policy.py"],
        "skill_layer": "spr",
        "worker_prompt": (
            "Make one focused reachable decision change in policy.py while "
            "preserving typed-intent legality."
        ),
    }
    task.update(overrides)
    return task


@pytest.mark.parametrize("field", ["branch_from", "source_override"])
def test_master_plan_rejects_source_override_fields(field):
    errors, _warnings = _validate_master_plan(
        {field: "national_v143", "tasks": [_valid_task()]},
        next_v=144,
    )
    assert any("source-override" in error.lower() for error in errors)


def test_master_plan_rejects_every_non_policy_target():
    errors, _warnings = _validate_master_plan(
        {"tasks": [_valid_task(target_files=["helper.py"])]},
        next_v=144,
    )
    assert any("writable scope must be exactly ['policy.py']" in error for error in errors)


def test_runtime_layer_accepts_closed_policy_abi_and_deadline_contract():
    prompt = (
        "Use decision_context and return a typed intent using raise_to or pass. "
        "Build a legal baseline before the deadline, enforce the decision budget, "
        "and use the legal fallback on timeout."
    )
    task = _valid_task(
        skill_layer="runtime_architecture",
        worker_prompt=prompt,
        runtime_contract={
            "policy_abi": {},
            "decision": {
                "clock": "time.monotonic",
                "hard_deadline_ms": 55_000,
                "baseline_target_ms": 250,
                "refinement_budget_ms": 54_000,
                "baseline_path": "existing legal policy baseline",
                "fallback_action": "system legal fallback",
                "refinement_bound": "at most sixty-four samples",
                "max_samples": 64,
            },
            "precompute_artifacts": [],
            "match_memory": None,
            "official_feedback_refs": [],
            "forbidden_runtime_work": ["file_io_in_decision"],
        },
    )
    errors, _warnings = _validate_master_plan({"tasks": [task]}, next_v=144)
    assert not any("runtime_contract" in error for error in errors), errors


def _plan(tasks):
    return {
        "analysis": "Stagnation requires one policy-level value correction.",
        "targeted_failure": "river overfold against passive opponents",
        "measurement_plan": "Compare complete native outcomes and the declared typed runtime checks.",
        "tasks": tasks,
    }


def test_master_plan_rejects_duplicate_worker_ids_on_shared_policy():
    with pytest.raises(Exception, match="Duplicate worker_id"):
        MasterPlan(
            **_plan(
                [
                    WorkerTask(**_valid_task(worker_id=1)),
                    WorkerTask(**_valid_task(worker_id=1, role="Hyperparameter Tuner")),
                ]
            )
        )


def test_master_plan_accepts_distinct_worker_ids_on_shared_policy():
    MasterPlan(
        **_plan(
            [
                WorkerTask(**_valid_task(worker_id=1)),
                WorkerTask(**_valid_task(worker_id=2, role="Hyperparameter Tuner")),
            ]
        )
    )
