import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import checkpoint_schema
import evolution_infra
import pytest

pytestmark = pytest.mark.usefixtures("synthetic_checkpoint_authority")
from pipeline_infrastructure import (
    build_infrastructure_failure,
    infrastructure_attempt_key,
    infrastructure_failure_digest,
    infrastructure_route,
    normalize_checkpoint_infrastructure,
    validate_infrastructure_failure,
)
from workflow_profiles import WorkflowProfileConfigurationError


@pytest.fixture(autouse=True)
def _strict_checkpoint_numbers(monkeypatch):
    real_write = evolution_infra.write_pipeline_checkpoint

    def write(next_v, source_v, *args, **kwargs):
        return real_write(
            144 if next_v == 2 else next_v,
            143 if source_v == 1 else source_v,
            *args,
            **kwargs,
        )

    monkeypatch.setattr(evolution_infra, "write_pipeline_checkpoint", write)
    monkeypatch.setattr(
        checkpoint_schema,
        "resolve_national_bot_spec",
        lambda *_args, **_kwargs: SimpleNamespace(
            eligible=True,
            version=143,
            issues=(),
            runtime_manifest={"epoch": "national_tcp_policy_v1"},
            epoch_receipt={"epoch": "national_tcp_policy_v1", "version": 143},
            publication_identity={"published": True, "version": 143},
            certificate_digest="a" * 64,
        ),
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


def test_retired_workflow_profile_fails_closed_without_checkpoint_upgrade(
    tmp_path, monkeypatch
):
    state_file = tmp_path / "pipeline_state.json"
    monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", state_file)
    monkeypatch.setenv("POK_WORKFLOW_PROFILE", "national_primary")
    with pytest.raises(WorkflowProfileConfigurationError):
        evolution_infra.write_pipeline_checkpoint(144, 143, "verified")
    assert not state_file.exists()


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


def test_worker_retry_directive_calls_owner_without_contradiction():
    overlay = build_infrastructure_failure(
        None,
        component="worker_llm",
        code="worker_llm_unavailable",
        owner_tool="execute_workers",
        resume_stage="rework_running",
        attempt_key="worker-identity",
        issues=["timeout"],
        max_attempts=3,
        now=1,
    )

    route = infrastructure_route({"infra_failure": overlay})

    assert route["next_tool"] == "execute_workers"
    assert "Retry execute_workers" in route["directive"]
    assert "do not call any other pipeline tool" in route["directive"]
    assert "do not call execute_workers" not in route["directive"]


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
