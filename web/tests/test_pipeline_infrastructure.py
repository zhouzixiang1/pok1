import json
from concurrent.futures import ThreadPoolExecutor

import evolution_infra
from pipeline_infrastructure import (
    build_infrastructure_failure,
    infrastructure_attempt_key,
    infrastructure_failure_digest,
    infrastructure_route,
    normalize_checkpoint_infrastructure,
    validate_infrastructure_failure,
)


def _overlay(*, attempt_key="identity", max_attempts=3):
    return build_infrastructure_failure(
        None,
        component="national_runtime_probe",
        code="probe_unavailable",
        owner_tool="run_quality_gates",
        resume_stage="workers_done",
        attempt_key=attempt_key,
        issues=["sandbox unavailable"],
        max_attempts=max_attempts,
        now=1,
    )


def test_checkpoint_preserves_and_explicitly_clears_infrastructure_overlay(tmp_path, monkeypatch):
    state_file = tmp_path / "pipeline_state.json"
    monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", state_file)
    overlay = _overlay(attempt_key=infrastructure_attempt_key(component="probe", extra={"v": 2}))

    assert evolution_infra.write_pipeline_checkpoint(
        2,
        1,
        "workers_done",
        infra_failure=overlay,
        expected_infra_failure_digest="",
    )
    assert evolution_infra.write_pipeline_checkpoint(2, 1, "workers_done", timeout_extensions=1)
    stored = json.loads(state_file.read_text())
    assert stored["infra_failure"] == overlay

    assert evolution_infra.write_pipeline_checkpoint(2, 1, "quality_failed") is False
    assert evolution_infra.write_pipeline_checkpoint(
        2,
        1,
        "quality_passed",
        clear_infra_failure=True,
        infra_failure_owner="run_quality_gates",
        expected_infra_failure_digest=infrastructure_failure_digest(overlay),
    )
    assert json.loads(state_file.read_text())["infra_failure"] is None


def test_checkpoint_rejects_tampered_infrastructure_overlay(tmp_path, monkeypatch):
    monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", tmp_path / "pipeline_state.json")
    overlay = _overlay()
    overlay["attempt"] = 2

    assert evolution_infra.write_pipeline_checkpoint(
        2,
        1,
        "workers_done",
        infra_failure=overlay,
        expected_infra_failure_digest="",
    ) is False


def test_official_job_attachment_uses_cas_and_clears_on_rework(tmp_path, monkeypatch):
    state_file = tmp_path / "pipeline_state.json"
    monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", state_file)
    assert evolution_infra.write_pipeline_checkpoint(2, 1, "verified")
    job = {
        "schema_version": 1,
        "job_id": "job-a",
        "identity_digest": "identity-a",
        "candidate_hash": "candidate-a",
        "policy_id": "official-full-v5",
        "state": "running",
        "revision": 1,
    }
    assert evolution_infra.write_pipeline_checkpoint(
        2,
        1,
        "official_certifying",
        official_job=job,
        expected_official_job_id="",
    )
    updated = {**job, "revision": 2, "state": "finalizing"}
    assert evolution_infra.write_pipeline_checkpoint(
        2,
        1,
        "official_certifying",
        official_job=updated,
        expected_official_job_id="stale",
    ) is False
    assert evolution_infra.write_pipeline_checkpoint(
        2,
        1,
        "official_certifying",
        official_job=updated,
        expected_official_job_id="job-a",
    )
    assert json.loads(state_file.read_text())["official_job"]["revision"] == 2

    assert evolution_infra.write_pipeline_checkpoint(2, 1, "workers_done")
    assert json.loads(state_file.read_text())["official_job"] is None


def test_profile_refresh_clears_official_job_and_old_full_gate(tmp_path, monkeypatch):
    state_file = tmp_path / "pipeline_state.json"
    monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", state_file)
    monkeypatch.setenv("POK_WORKFLOW_PROFILE", "national_primary")
    assert evolution_infra.write_pipeline_checkpoint(
        2,
        1,
        "verified",
        gate_results={
            "quality": {"all_passed": True},
            "official_full": {"passed": True},
        },
    )
    job = {
        "schema_version": 1,
        "job_id": "job-a",
        "identity_digest": "identity-a",
        "candidate_hash": "candidate-a",
        "policy_id": "official-full-v5",
        "state": "running",
        "revision": 1,
    }
    assert evolution_infra.write_pipeline_checkpoint(
        2,
        1,
        "official_certifying",
        official_job=job,
        expected_official_job_id="",
    )

    monkeypatch.setenv("POK_WORKFLOW_PROFILE", "national_native")
    assert evolution_infra.write_pipeline_checkpoint(
        2,
        1,
        "quality_passed",
        gate_results={"quality": {
            "all_passed": True,
            "workflow_profile_id": "national_native",
            "national_execution_mode": "native_tcp",
        }},
    )
    stored = json.loads(state_file.read_text())
    assert stored["official_job"] is None
    assert "official_full" not in stored["gate_results"]


def test_legacy_quality_infrastructure_stage_normalizes_to_overlay():
    normalized = normalize_checkpoint_infrastructure({
        "next_v": 2,
        "source_v": 1,
        "stage": "quality_inconclusive",
        "last_update_ts": 5,
        "gate_results": {
            "quality": {
                "failed_gates": ["bwrap unavailable"],
                "quality_infrastructure": {"attempt": 3, "max_attempts": 3},
            },
        },
    })

    assert normalized["stage"] == "workers_done"
    assert normalized["infra_failure"]["owner_tool"] == "run_quality_gates"
    assert normalized["infra_failure"]["attempt"] == 3
    assert normalized["infra_failure"]["exhausted"] is True


def test_overlay_fails_closed_for_non_mapping_and_owner_stage_mismatch():
    route = infrastructure_route({"infra_failure": "corrupt"})
    assert route["intent"] == "infra_invalid"
    assert route["allowed_tools"] == []

    overlay = _overlay()
    overlay["resume_stage"] = "quality_passed"
    overlay["identity_digest"] = infrastructure_failure_digest(overlay)
    assert "infra_failure_owner_stage_mismatch" in validate_infrastructure_failure(overlay)


def test_overlay_mutation_requires_owner_and_compare_and_swap(tmp_path, monkeypatch):
    state_file = tmp_path / "pipeline_state.json"
    monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", state_file)
    first = _overlay()
    assert evolution_infra.write_pipeline_checkpoint(
        2, 1, "workers_done", infra_failure=first,
        expected_infra_failure_digest="",
    )
    second = build_infrastructure_failure(
        first,
        component=first["component"],
        code=first["code"],
        owner_tool=first["owner_tool"],
        resume_stage=first["resume_stage"],
        attempt_key=first["attempt_key"],
        issues=first["issues"],
        now=2,
    )

    def update():
        return evolution_infra.write_pipeline_checkpoint(
            2, 1, "workers_done", infra_failure=second,
            expected_infra_failure_digest=infrastructure_failure_digest(first),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _index: update(), range(2)))
    assert sorted(outcomes) == [False, True]
    assert json.loads(state_file.read_text())["infra_failure"]["attempt"] == 2
    assert evolution_infra.write_pipeline_checkpoint(
        2, 1, "workers_done", clear_infra_failure=True,
        infra_failure_owner="run_review",
        expected_infra_failure_digest=infrastructure_failure_digest(second),
    ) is False
    assert evolution_infra.write_pipeline_checkpoint(
        2, 1, "workers_done", clear_infra_failure=True,
        infra_failure_owner="run_quality_gates",
        expected_infra_failure_digest="stale",
    ) is False


def test_malformed_legacy_attempt_does_not_hide_checkpoint():
    normalized = normalize_checkpoint_infrastructure({
        "next_v": 2,
        "source_v": 1,
        "stage": "quality_inconclusive",
        "gate_results": {"quality": {"quality_infrastructure": {
            "attempt": "not-an-int",
            "max_attempts": "also-bad",
        }}},
    })
    assert normalized["stage"] == "workers_done"
    assert normalized["infra_failure"]["attempt"] == 1
