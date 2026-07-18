"""Adversarial HTTP tests for strict-epoch certification projections."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bot_artifact import canonical_digest, hash_path
from bot_namespace import (
    NATIONAL_RUNTIME_MANIFEST,
    POLICY_EPOCH_RECEIPT,
    bot_name,
    build_policy_epoch_receipt,
    build_runtime_manifest,
)
from official_certification import build_spec
import official_certification_job as jobs


def _write_strict_bot(root: Path, version: int) -> Path:
    bot = root / "bots" / bot_name(version)
    bot.mkdir(parents=True, exist_ok=True)
    (bot / "national_bot.py").write_text("# strict native runtime\n", encoding="utf-8")
    (bot / "policy.py").write_text(
        "def get_baseline_decision(context):\n"
        "    return {'kind': 'pass'}\n\n"
        "def iter_decisions(context, baseline, deadline):\n"
        "    return ()\n",
        encoding="utf-8",
    )
    (bot / "precompute.py").write_text("TABLE = ()\n", encoding="utf-8")
    manifest = build_runtime_manifest(bot)
    (bot / NATIONAL_RUNTIME_MANIFEST).write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    parents = () if version == 143 else (version - 1,)
    receipt = build_policy_epoch_receipt(bot, version, parent_versions=parents)
    (bot / POLICY_EPOCH_RECEIPT).write_text(
        json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return bot


def test_cancel_uses_the_checkpoint_data_sidecar(monkeypatch):
    import fcntl
    from contextlib import contextmanager

    import evolution_infra
    import server.routes.certification as route

    observed = []

    @contextmanager
    def guard(path, *, lock_type):
        observed.append((Path(path), lock_type))
        yield

    monkeypatch.setattr(evolution_infra, "_locked_state_sidecar", guard)
    monkeypatch.setattr(
        route,
        "_projection",
        lambda: {
            "initialized": False,
            "reset_receipt_valid": False,
            "active_generation": None,
        },
    )

    result = route._cancel_exact_job_sync(
        "job-1",
        workflow_run_id="generation:143:workflow-v1",
        candidate_version=143,
        checkpoint_revision=4,
    )

    assert result == {"state": "identity_mismatch", "job_id": "job-1"}
    assert observed == [
        (Path(evolution_infra.PIPELINE_STATE_FILE), fcntl.LOCK_EX)
    ]


def _projection(
    *,
    version: int = 144,
    workflow: str = "generation:144:strict-http-test",
    stage: str = "official_certifying",
) -> dict:
    return {
        "state": "strict_published",
        "initialized": True,
        "reset_receipt_valid": True,
        "active_bots": ["national_v143"],
        "active_generation": {
            "next_v": version,
            "source_v": version - 1,
            "stage": stage,
            "workflow_run_id": workflow,
        },
    }


def _structural_quality_admission(candidate: Path) -> dict:
    """Build a schema-valid normal-full receipt for projection-only tests."""

    from national_native import NATIONAL_DECISION_RUNTIME_VERSION
    from national_runtime_authority import current_system_native_runtime_identity
    from national_runtime_probe import (
        RUNTIME_PROBE_ORCHESTRATOR_VERSION,
        RUNTIME_PROBE_IDENTITY_DIGEST,
        RUNTIME_PROBE_LIMITS_DIGEST,
        RUNTIME_PROBE_REPEATABILITY_SCHEMA_VERSION,
        RUNTIME_PROBE_REPEATABILITY_VIEW_CONTRACT,
        RUNTIME_PROBE_SCENARIO_DIGEST,
        RUNTIME_PROBE_SCHEMA_VERSION,
        runtime_probe_native_template_evidence,
    )
    from official_platform_harness import FORMAL_QUALITY_ADMISSION_SCHEMA_VERSION

    runtime_evidence = runtime_probe_native_template_evidence()
    next_v = int(candidate.name.removeprefix("national_v") or 0)
    payload = {
        "schema_version": FORMAL_QUALITY_ADMISSION_SCHEMA_VERSION,
        "kind": "official-formal-quality-admission",
        "candidate_path": str(candidate.resolve()),
        "candidate_hash": hash_path(candidate),
        "checkpoint": {
            "evaluation_epoch": "national_tcp_policy_v1",
            "workflow_run_id": "pytest-certification-route-workflow",
            "next_v": next_v,
            "source_v": next_v - 1,
        },
        "quality_gate_digest": "1" * 64,
        "capability_digest": "2" * 64,
        "dynamic_probe_digest": "3" * 64,
        "runtime_contract_ledger_digest": "4" * 64,
        "runtime_probe_identity": {
            "schema_version": RUNTIME_PROBE_SCHEMA_VERSION,
            "orchestrator_version": RUNTIME_PROBE_ORCHESTRATOR_VERSION,
            "scenario_digest": RUNTIME_PROBE_SCENARIO_DIGEST,
            "limits_digest": RUNTIME_PROBE_LIMITS_DIGEST,
            "probe_identity_digest": RUNTIME_PROBE_IDENTITY_DIGEST,
            "managed_isolation_digest": "8" * 64,
            "repeatability_schema_version": (
                RUNTIME_PROBE_REPEATABILITY_SCHEMA_VERSION
            ),
            "repeatability_view_contract": (
                RUNTIME_PROBE_REPEATABILITY_VIEW_CONTRACT
            ),
            "repeatability_evidence_digest": "9" * 64,
            **runtime_evidence,
        },
        "system_runtime_identity": current_system_native_runtime_identity(),
        "system_decision_runtime_version": NATIONAL_DECISION_RUNTIME_VERSION,
    }
    return {**payload, "admission_digest": canonical_digest(payload)}


def _job_fixture(
    tmp_path: Path,
    monkeypatch,
    *,
    mode: str = "full",
) -> tuple[dict, dict, dict]:
    """Create a manager-valid request/state without launching a worker."""

    from server.routes import certification as route

    candidate = _write_strict_bot(tmp_path, 144)
    opponent = _write_strict_bot(tmp_path, 143)
    eligibility_receipt = {
        "schema_version": 1,
        "kind": "official_full_certificate",
        "role": "official_opponent",
        "bot": opponent.name,
        "artifact_hash": hash_path(opponent),
        "policy_id": "official-full-v5",
        "certificate_digest": "c" * 64,
    }
    eligibility_receipt["receipt_digest"] = canonical_digest(
        eligibility_receipt
    )
    selection = {
        "selected": True,
        "candidate": str(candidate.resolve()),
        "opponent": {
            "bot": opponent.name,
            "path": str(opponent.resolve()),
            "artifact_hash": hash_path(opponent),
            "tag": "national-bot-v143",
            "tag_object": "a" * 40,
            "eligible": True,
            "reason": "official_certified",
            "eligibility_receipt": eligibility_receipt,
        },
    }
    spec = build_spec(
        mode,
        candidate,
        opponent=opponent,
        quality_admission=(
            _structural_quality_admission(candidate) if mode == "full" else None
        ),
    )
    request = jobs._request_payload(
        spec,
        opponent_selection=selection,
        source_v=143,
    )
    job_dir = tmp_path / "official-jobs" / request["job_id"]
    job_dir.mkdir(parents=True)
    (job_dir / "request.json").write_text(
        json.dumps(request, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    state = {
        "schema_version": jobs.JOB_SCHEMA_VERSION,
        "manager_version": jobs.JOB_MANAGER_VERSION,
        "job_id": request["job_id"],
        "candidate": str(candidate.resolve()),
        "request_digest": request["request_digest"],
        "state": "running",
        "phase": "certification",
        "attempt": 1,
        "revision": 3,
        "updated_at_epoch": 100.0,
    }
    progress = {
        "suite_attempt": 1,
        "rounds_requested": 8,
        "rounds_completed": 0,
        "rounds_passed": 0,
        "active_round": None,
        "rounds": [],
    }
    state["progress"] = progress
    state["progress_digest"] = canonical_digest(progress)
    (job_dir / "state.json").write_text(
        json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    monkeypatch.setenv("POK_OFFICIAL_JOB_DIR", str(tmp_path / "official-jobs"))
    monkeypatch.setattr(route, "BOTS_DIR", tmp_path / "bots")
    monkeypatch.setattr(route, "published_bot_identity", lambda _path: {
        "published": True,
        "label": opponent.name,
        "path": str(opponent.resolve()),
        "artifact_hash": hash_path(opponent),
        "tag": "national-bot-v143",
        "tag_object": "a" * 40,
        "issues": [],
    })
    monkeypatch.setattr(route, "official_opponent_eligibility", lambda _path: {
        "eligible": True,
        "reason": "official_certified",
        "eligibility_receipt": eligibility_receipt,
    })

    identity = request["identity"]
    attached = {
        "schema_version": 1,
        "job_id": request["job_id"],
        "identity_digest": identity["identity_digest"],
        "candidate_hash": identity["candidate_hash"],
        "opponent_hash": identity["opponent_hash"],
        "opponent": opponent.name,
        "policy_id": "official-full-v5",
        "state": "running",
        "phase": "certification",
        "revision": 3,
        "attempt": 1,
        "heartbeat_at_epoch": 100.0,
        "rounds_completed": 0,
        "rounds_requested": 8,
    }
    workflow = "generation:144:strict-http-test"
    checkpoint = {
        "next_v": 144,
        "source_v": 143,
        "stage": "official_certifying",
        "workflow_run_id": workflow,
        "checkpoint_revision": 17,
        "national_execution_mode": "native_tcp",
        "official_job": attached,
    }
    context = {
        "projection": _projection(workflow=workflow),
        "checkpoint": checkpoint,
        "version": 144,
        "workflow_run_id": workflow,
        "candidate": candidate.resolve(),
        "candidate_hash": hash_path(candidate),
    }
    return request, context, state


def _write_manager_job(root: Path, request: dict, *, state_name: str = "running") -> Path:
    directory = root / request["job_id"]
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "request.json").write_text(
        json.dumps(request, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    progress = {
        "suite_attempt": 1,
        "rounds_requested": 8,
        "rounds_completed": 0,
        "rounds_passed": 0,
        "active_round": None,
        "rounds": [],
    }
    state = {
        "schema_version": jobs.JOB_SCHEMA_VERSION,
        "manager_version": jobs.JOB_MANAGER_VERSION,
        "job_id": request["job_id"],
        "candidate": request["spec"]["candidate"],
        "request_digest": request["request_digest"],
        "state": state_name,
        "phase": "certification",
        "attempt": 1,
        "revision": 3,
        "updated_at_epoch": 100.0,
        "progress": progress,
        "progress_digest": canonical_digest(progress),
    }
    (directory / "state.json").write_text(
        json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    return directory


def _rekey_request(request: dict) -> dict:
    request = json.loads(json.dumps(request))
    request["request_digest"] = canonical_digest({
        key: value
        for key, value in request.items()
        if key not in {"request_digest", "job_id"}
    })
    request["job_id"] = canonical_digest({
        "request_digest": request["request_digest"]
    })
    return request


def _bootstrap_job_fixture(tmp_path: Path, monkeypatch) -> tuple[dict, dict, Path]:
    """Build a manager-valid manual v143 request without starting Wine."""

    from first_strict_control import CONTROL_ID
    from server.routes import certification as route
    import official_bootstrap

    candidate = _write_strict_bot(tmp_path, 143)
    control = tmp_path / "first_strict_control_v1"
    control.mkdir()
    (control / "national_bot.py").write_text("# system control\n", encoding="utf-8")
    workflow = "generation:143:first-strict-http-test"
    candidate_hash = hash_path(candidate)
    parked = {
        "schema_version": 1,
        "kind": "official-first-strict-control-parked-request",
        "candidate_path": str(candidate.resolve()),
        "candidate_label": "national_v143",
        "candidate_version": 143,
        "candidate_hash": candidate_hash,
        "source_v": 142,
        "workflow_run_id": workflow,
        "active_bots": [],
        "strict_published_bots": [],
        "bootstrap_control_id": CONTROL_ID,
    }
    parked["request_digest"] = canonical_digest(parked)
    authorization = {
        "schema_version": 1,
        "kind": "official-first-strict-control-operator-authorization",
        "bootstrap_control_id": CONTROL_ID,
        "parked_request_digest": parked["request_digest"],
        "workflow_run_id": workflow,
        "candidate_path": str(candidate.resolve()),
        "candidate_version": 143,
        "candidate_hash": candidate_hash,
    }
    authorization["authorization_digest"] = canonical_digest(authorization)
    selection = {
        "selected": True,
        "eligible": True,
        "reason": "first_strict_control_bootstrap",
        "kind": "official-first-strict-control-selection",
        "bootstrap_control_id": CONTROL_ID,
        "candidate": str(candidate.resolve()),
        "candidate_binding": {"candidate_hash": candidate_hash},
        "bootstrap_control_receipt": {"receipt_digest": "b" * 64},
        "operator_bootstrap_authorization": authorization,
        "opponent": {
            "bot": CONTROL_ID,
            "path": str(control.resolve()),
            "artifact_hash": hash_path(control),
            "eligible": True,
            "reason": "first_strict_control_bootstrap",
            "eligibility_receipt": {"receipt_digest": "c" * 64},
            "authority": "system-owned-first-strict-control",
            "normal_official_opponent": False,
            "strength_admitted": False,
            "rating_eligible": False,
        },
    }
    spec = build_spec(
        "full",
        candidate,
        opponent=control,
        bootstrap_control_id=CONTROL_ID,
    )
    request = jobs._request_payload(
        spec,
        opponent_selection=selection,
        source_v=None,
    )
    root = tmp_path / "official-jobs"
    _write_manager_job(root, request)
    projection = _projection(
        version=143,
        workflow=workflow,
        stage="official_bootstrap_required",
    )
    projection["active_bots"] = []
    checkpoint = {
        "next_v": 143,
        "source_v": 142,
        "stage": "official_bootstrap_required",
        "workflow_run_id": workflow,
        "checkpoint_revision": 21,
        "national_execution_mode": "native_tcp",
        "audit_context": {"official_bootstrap_request": parked},
    }
    context = {
        "projection": projection,
        "checkpoint": checkpoint,
        "version": 143,
        "workflow_run_id": workflow,
        "candidate": candidate.resolve(),
        "candidate_hash": candidate_hash,
    }
    monkeypatch.setenv("POK_OFFICIAL_JOB_DIR", str(root))
    monkeypatch.setattr(route, "BOTS_DIR", tmp_path / "bots")
    monkeypatch.setattr(route, "_projection", lambda: projection)
    monkeypatch.setattr(
        route,
        "_current_candidate_context",
        lambda _projection=None: context,
    )
    monkeypatch.setattr(
        official_bootstrap,
        "validate_operator_bootstrap_authorized_selection",
        lambda supplied, *_a, **_k: {
            "valid": supplied == request["opponent_selection"],
            "issues": [],
        },
    )
    return request, context, root


def _write_completed_result(directory: Path, request: dict, status: dict) -> None:
    state_path = directory / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    result = {
        "schema_version": jobs.JOB_SCHEMA_VERSION,
        "job_id": request["job_id"],
        "request_digest": request["request_digest"],
        "attempt": 1,
        "job_envelope_digest": status["official_job_envelope"]["envelope_digest"],
        "status": status,
    }
    result["result_digest"] = canonical_digest(result)
    result_path = directory / "result_attempt_01.json"
    result_path.write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    state.update({
        "state": "completed",
        "phase": "completed",
        "result_path": str(result_path),
        "result_digest": result["result_digest"],
    })
    state_path.write_text(
        json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )


def test_uninitialized_epoch_hides_jobs_and_candidate_status(client, monkeypatch):
    from server.routes import certification as route

    monkeypatch.setattr(route, "strict_epoch_projection", lambda: {
        "state": "reset_required",
        "initialized": False,
        "reset_receipt_valid": False,
        "active_bots": [],
        "active_generation": None,
    })
    monkeypatch.setattr(
        route,
        "get_job",
        lambda _job_id: (_ for _ in ()).throw(AssertionError("old job was read")),
    )

    queue = client.get("/api/certification/jobs")
    status = client.get("/api/certification/143")

    assert queue.status_code == 200
    assert queue.json()["jobs"] == []
    assert queue.json()["epoch_initialized"] is False
    assert status.status_code == 404


def test_untracked_v155_directory_is_never_a_certification_subject(
    client, monkeypatch, tmp_path
):
    from server.routes import certification as route

    (tmp_path / "bots" / "national_v155").mkdir(parents=True)
    monkeypatch.setattr(route, "BOTS_DIR", tmp_path / "bots")
    monkeypatch.setattr(route, "strict_epoch_projection", lambda: {
        "state": "fresh_bootstrap_ready",
        "initialized": True,
        "reset_receipt_valid": True,
        "active_bots": [],
        "active_generation": None,
    })

    response = client.get("/api/certification/155")

    assert response.status_code == 404


def test_current_candidate_rejects_wrong_workflow_and_invalid_checkpoint(
    monkeypatch, tmp_path
):
    from server.routes import certification as route
    import evolution_infra

    _write_strict_bot(tmp_path, 144)
    monkeypatch.setattr(route, "BOTS_DIR", tmp_path / "bots")
    checkpoint = {
        "next_v": 144,
        "source_v": 143,
        "stage": "official_certifying",
        "workflow_run_id": "generation:144:current",
    }
    monkeypatch.setattr(evolution_infra, "read_pipeline_checkpoint", lambda: checkpoint)
    monkeypatch.setattr(
        route,
        "strict_checkpoint_event_identity",
        lambda *_a, **_k: {
            "gen": 144,
            "evaluation_epoch": "national_tcp_policy_v1",
            "workflow_run_id": checkpoint["workflow_run_id"],
        },
    )

    assert route._current_candidate_context(
        _projection(workflow="generation:144:stale")
    ) is None

    def invalid(*_args, **_kwargs):
        raise route.CheckpointSchemaError(["checkpoint_epoch_binding_mismatch"])

    monkeypatch.setattr(route, "strict_checkpoint_event_identity", invalid)
    assert route._current_candidate_context(
        _projection(workflow="generation:144:current")
    ) is None


def test_old_job_and_wrong_stage_are_hidden(client, monkeypatch, tmp_path):
    from server.routes import certification as route

    request, context, _state = _job_fixture(tmp_path, monkeypatch)
    original_prerequisites = route._official_prerequisite_issues
    monkeypatch.setattr(route, "_official_prerequisite_issues", lambda _ctx: [])
    monkeypatch.setattr(route, "_projection", lambda: context["projection"])
    monkeypatch.setattr(
        route,
        "_current_candidate_context",
        lambda _projection=None: context,
    )

    old_id = "f" * 64
    old = client.get(f"/api/certification/jobs/{old_id}")
    jobs = client.get("/api/certification/jobs")

    assert old.status_code == 404
    assert [row["job_id"] for row in jobs.json()["jobs"]] == [request["job_id"]]
    assert jobs.json()["workflow_run_id"] == context["workflow_run_id"]

    context["checkpoint"]["stage"] = "verified"
    # Stage rejection happens before the expensive shared ledger validator.
    assert original_prerequisites(context) == [
        "checkpoint_not_official_certifying"
    ]
    context["checkpoint"].update({
        "stage": "official_certifying",
        "national_execution_mode": "legacy_adapter",
    })
    assert original_prerequisites(context) == [
        "checkpoint_execution_mode_not_native_tcp"
    ]


def test_wrong_smoke_mode_cannot_appear_as_formal_job(monkeypatch, tmp_path):
    from server.routes import certification as route

    _request, context, _state = _job_fixture(tmp_path, monkeypatch, mode="smoke")
    monkeypatch.setattr(route, "_official_prerequisite_issues", lambda _ctx: [])

    assert route._attached_job_view(context) is None


def test_missing_precommit_gate_hides_attached_job(monkeypatch, tmp_path):
    from server.routes import certification as route
    import tool_commit

    _request, context, _state = _job_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        tool_commit,
        "validate_commit_gate_ledger",
        lambda *_a, **_k: {
            "ok": False,
            "missing_gates": ["precommit_eval"],
            "failed_gates": [],
            "current_code_fingerprint": context["candidate_hash"],
        },
    )

    assert route._official_prerequisite_issues(context) == ["precommit_eval"]
    assert route._attached_job_view(context) is None


def test_smoke_status_is_projected_as_diagnostic_not_formal(
    client, monkeypatch, tmp_path
):
    from server.routes import certification as route

    candidate = tmp_path / "bots" / "national_v144"
    candidate.mkdir(parents=True)
    projection = _projection()
    monkeypatch.setattr(
        route,
        "_visible_subject",
        lambda _version: (
            candidate,
            "active_candidate",
            projection,
            "generation:144:strict-http-test",
        ),
    )
    monkeypatch.setattr(route, "status_payload", lambda _candidate: {
        "status": "official-smoke-pass",
        "mode": "smoke",
        "policy_id": "official-smoke-v1",
        "compliance_verdict": {"ok": True},
    })
    monkeypatch.setattr(
        route,
        "official_full_certified",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("smoke status entered full validator")
        ),
    )

    response = client.get("/api/certification/144")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "official-uncertified"
    assert payload["mode"] == "full"
    assert payload["formal_certified"] is False
    assert payload["formal_authority"] == "none"
    assert payload["diagnostic_evidence"] == {
        "status": "official-smoke-pass",
        "mode": "smoke",
        "policy_id": "official-smoke-v1",
        "authority": "diagnostic_only",
    }


def test_certification_route_replaces_self_reported_profile_with_signed_projection(
    client,
    monkeypatch,
    tmp_path,
):
    from server.routes import certification as route

    candidate = tmp_path / "bots" / "national_v144"
    candidate.mkdir(parents=True)
    monkeypatch.setattr(
        route,
        "_visible_subject",
        lambda _version: (
            candidate,
            "strict_published",
            _projection(),
            None,
        ),
    )
    monkeypatch.setattr(route, "status_payload", lambda _candidate: {
        "status": "official-certified",
        "mode": "full",
        "policy_id": "official-full-v5",
        "certification_profile": "FORGED",
        "opponent_authority": "archive",
        "strength_evidence_weight": 1,
        "strategy_evidence_weight": 1,
    })
    monkeypatch.setattr(route, "official_full_certified", lambda *_a, **_k: True)
    canonical = {
        "certification_profile": "official-full-v5",
        "opponent_authority": "strict_published_pool",
        "strength_evidence_weight": 0,
        "strategy_evidence_weight": 0,
        "formal_summary": {
            "self_play_rounds": 5,
            "opponent_rounds": 3,
            "target_hands": 70,
            "rounds_requested": 8,
            "rounds_run": 8,
            "passed_rounds": 8,
            "failed_rounds": 0,
        },
    }
    monkeypatch.setattr(
        route,
        "official_certification_profile_projection",
        lambda *_a, **_k: canonical,
    )

    payload = client.get("/api/certification/144").json()

    assert payload["formal_certified"] is True
    assert {key: payload[key] for key in canonical} == canonical


def test_certification_route_removes_profile_when_signed_projection_is_unavailable(
    client,
    monkeypatch,
    tmp_path,
):
    from server.routes import certification as route

    candidate = tmp_path / "bots" / "national_v144"
    candidate.mkdir(parents=True)
    monkeypatch.setattr(
        route,
        "_visible_subject",
        lambda _version: (candidate, "strict_published", _projection(), None),
    )
    monkeypatch.setattr(route, "status_payload", lambda _candidate: {
        "status": "official-certified",
        "mode": "full",
        "policy_id": "official-full-v5",
        "certification_profile": "FORGED",
        "opponent_authority": "archive",
    })
    monkeypatch.setattr(route, "official_full_certified", lambda *_a, **_k: True)
    monkeypatch.setattr(
        route,
        "official_certification_profile_projection",
        lambda *_a, **_k: {},
    )

    payload = client.get("/api/certification/144").json()

    assert payload["formal_certified"] is False
    assert payload["formal_authority"] == "none"
    assert "certification_profile" not in payload
    assert "opponent_authority" not in payload


def test_exact_full_job_projects_epoch_workflow_and_candidate(
    client, monkeypatch, tmp_path
):
    from server.routes import certification as route

    request, context, _state = _job_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(route, "_official_prerequisite_issues", lambda _ctx: [])
    monkeypatch.setattr(route, "_projection", lambda: context["projection"])
    monkeypatch.setattr(
        route,
        "_current_candidate_context",
        lambda _projection=None: context,
    )

    response = client.get(f"/api/certification/jobs/{request['job_id']}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["job_id"] == request["job_id"]
    assert payload["evaluation_epoch"] == "national_tcp_policy_v1"
    assert payload["workflow_run_id"] == context["workflow_run_id"]
    assert payload["candidate_version"] == 144
    assert payload["formal_mode"] == "full"
    assert payload["formal_policy_id"] == "official-full-v5"
    assert payload["certification_profile"] == "official-full-v5"
    assert payload["opponent_authority"] == "strict_published_pool"
    assert payload["formal_profile"] == {
        "self_play_rounds": 5,
        "opponent_rounds": 3,
        "target_hands": 70,
    }
    assert payload["strength_evidence_weight"] == 0
    assert payload["strategy_evidence_weight"] == 0


def test_certification_get_routes_isolate_complete_read_projections(
    client,
    monkeypatch,
    tmp_path,
):
    from server.routes import certification as route

    request, context, _state = _job_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(route, "_official_prerequisite_issues", lambda _ctx: [])
    monkeypatch.setattr(route, "_projection", lambda: context["projection"])
    monkeypatch.setattr(
        route,
        "_current_candidate_context",
        lambda _projection=None: context,
    )
    monkeypatch.setattr(
        route,
        "_visible_subject",
        lambda _version: (
            context["candidate"],
            "active_candidate",
            context["projection"],
            context["workflow_run_id"],
        ),
    )
    monkeypatch.setattr(route, "status_payload", lambda _candidate: {
        "status": "official-uncertified",
        "mode": None,
        "policy_id": None,
        "issues": [],
    })
    expected_jobs = route._jobs_payload()
    expected_job = route._certification_job_payload(request["job_id"])
    expected_status = route._certification_payload(144)
    calls = []

    async def isolated(function, *args, thread_name_prefix, **kwargs):
        calls.append((function.__name__, args, thread_name_prefix))
        return function(*args, **kwargs)

    monkeypatch.setattr(route, "run_blocking_isolated", isolated)

    jobs_response = client.get("/api/certification/jobs")
    job_response = client.get(f"/api/certification/jobs/{request['job_id']}")
    status_response = client.get("/api/certification/144")
    missing_response = client.get(f"/api/certification/jobs/{'f' * 64}")
    monkeypatch.setattr(
        route,
        "_visible_subject",
        lambda _version: (_ for _ in ()).throw(
            route.HTTPException(status_code=404, detail="Bot v999 not found")
        ),
    )
    missing_status_response = client.get("/api/certification/999")

    assert jobs_response.json() == expected_jobs
    assert job_response.json() == expected_job
    assert status_response.json() == expected_status
    assert missing_response.status_code == 404
    assert missing_status_response.status_code == 404
    assert missing_status_response.json()["detail"] == "Bot v999 not found"
    assert calls == [
        ("_jobs_payload", (), "official-certification-jobs"),
        (
            "_certification_job_payload",
            (request["job_id"],),
            "official-certification-job",
        ),
        ("_certification_payload", (144,), "official-certification-status"),
        (
            "_certification_job_payload",
            ("f" * 64,),
            "official-certification-job",
        ),
        ("_certification_payload", (999,), "official-certification-status"),
    ]


def test_normal_job_requires_live_published_opponent_certificate_and_receipt(
    monkeypatch,
    tmp_path,
):
    from server.routes import certification as route

    request, context, _state = _job_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(route, "_official_prerequisite_issues", lambda _ctx: [])

    assert route._attached_job_view(context) is not None

    monkeypatch.setattr(route, "published_bot_identity", lambda _path: {
        "published": False,
        "label": "national_v143",
        "path": request["spec"]["opponent"],
        "artifact_hash": request["identity"]["opponent_hash"],
        "tag": "national-bot-v143",
        "tag_object": "a" * 40,
        "issues": ["missing_annotated_completion_tag"],
    })
    assert route._attached_job_view(context) is None

    opponent = request["opponent_selection"]["opponent"]
    monkeypatch.setattr(route, "published_bot_identity", lambda _path: {
        "published": True,
        "label": opponent["bot"],
        "path": opponent["path"],
        "artifact_hash": opponent["artifact_hash"],
        "tag": opponent["tag"],
        "tag_object": opponent["tag_object"],
        "issues": [],
    })
    monkeypatch.setattr(route, "official_opponent_eligibility", lambda _path: {
        "eligible": False,
        "reason": "official_full_certificate_required",
        "eligibility_receipt": request["opponent_selection"]["opponent"][
            "eligibility_receipt"
        ],
    })
    assert route._attached_job_view(context) is None

    receipt = json.loads(json.dumps(
        request["opponent_selection"]["opponent"]["eligibility_receipt"]
    ))
    receipt["certificate_digest"] = "e" * 64
    receipt["receipt_digest"] = canonical_digest({
        key: value for key, value in receipt.items() if key != "receipt_digest"
    })
    monkeypatch.setattr(route, "official_opponent_eligibility", lambda _path: {
        "eligible": True,
        "reason": "official_certified",
        "eligibility_receipt": receipt,
    })
    assert route._attached_job_view(context) is None


def test_normal_noncompleted_job_rejects_injected_status(monkeypatch, tmp_path):
    from server.routes import certification as route

    request, context, _state = _job_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(route, "_official_prerequisite_issues", lambda _ctx: [])
    state_path = tmp_path / "official-jobs" / request["job_id"] / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["status"] = {
        "status": "official-certified",
        "mode": "full",
        "policy_id": "official-full-v5",
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")

    assert route._attached_job_view(context) is None


def test_normal_completed_status_is_exactly_bound_to_request_and_selection(
    monkeypatch,
    tmp_path,
):
    from official_job_envelope import build_job_envelope
    from server.routes import certification as route

    request, context, _state = _job_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(route, "_official_prerequisite_issues", lambda _ctx: [])
    monkeypatch.setattr(route, "authoritative_verdict_status_issues", lambda _status: [])
    directory = tmp_path / "official-jobs" / request["job_id"]
    envelope = build_job_envelope(
        request,
        attempt=1,
        attempt_nonce="a" * 64,
        suite_dir=directory / "suite_attempt_01",
    )
    status = {
        "status": "official-inconclusive",
        "mode": "full",
        "policy_id": "official-full-v5",
        "certification_identity": request["identity"],
        "opponent_selection": request["opponent_selection"],
        "official_job_envelope": envelope,
    }
    _write_completed_result(directory, request, status)
    assert route._attached_job_view(context) is not None

    drifted = json.loads(json.dumps(status))
    drifted["certification_identity"]["identity_digest"] = "f" * 64
    _write_completed_result(directory, request, drifted)
    assert route._attached_job_view(context) is None

    drifted = json.loads(json.dumps(status))
    drifted["opponent_selection"]["opponent"]["tag_object"] = "b" * 40
    _write_completed_result(directory, request, drifted)
    assert route._attached_job_view(context) is None

    drifted = json.loads(json.dumps(status))
    drifted["official_job_envelope"]["certification_identity_digest"] = "0" * 64
    drifted["official_job_envelope"]["envelope_digest"] = canonical_digest({
        key: value
        for key, value in drifted["official_job_envelope"].items()
        if key != "envelope_digest"
    })
    _write_completed_result(directory, request, drifted)
    assert route._attached_job_view(context) is None


@pytest.mark.parametrize(
    "stage",
    ("official_certifying", "official_failed", "official_inconclusive", "publishing"),
)
def test_normal_attached_job_is_visible_in_all_owning_checkpoint_stages(
    client,
    monkeypatch,
    tmp_path,
    stage,
):
    from server.routes import certification as route
    import tool_commit

    request, context, _state = _job_fixture(tmp_path, monkeypatch)
    context["checkpoint"]["stage"] = stage
    context["projection"]["active_generation"]["stage"] = stage
    monkeypatch.setattr(
        tool_commit,
        "validate_commit_gate_ledger",
        lambda *_a, **_k: {
            "ok": True,
            "missing_gates": [],
            "failed_gates": [],
            "current_code_fingerprint": context["candidate_hash"],
        },
    )
    monkeypatch.setattr(route, "_projection", lambda: context["projection"])
    monkeypatch.setattr(
        route,
        "_current_candidate_context",
        lambda _projection=None: context,
    )

    payload = client.get("/api/certification/jobs").json()

    assert [row["job_id"] for row in payload["jobs"]] == [request["job_id"]]


@pytest.mark.parametrize(
    ("self_play_rounds", "opponent_rounds", "target_hands"),
    ((4, 4, 70), (5, 3, 69)),
)
def test_normal_job_rejects_digest_consistent_nonformal_profile(
    monkeypatch,
    tmp_path,
    self_play_rounds,
    opponent_rounds,
    target_hands,
):
    from server.routes import certification as route

    request, context, _state = _job_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(route, "_official_prerequisite_issues", lambda _ctx: [])
    request = json.loads(json.dumps(request))
    request["spec"].update({
        "self_play_rounds": self_play_rounds,
        "opponent_rounds": opponent_rounds,
        "target_hands": target_hands,
    })
    request["identity"]["spec"] = request["spec"]
    request["identity"]["identity_digest"] = canonical_digest({
        key: value
        for key, value in request["identity"].items()
        if key != "identity_digest"
    })
    request = _rekey_request(request)
    _write_manager_job(tmp_path / "official-jobs", request)
    context["checkpoint"]["official_job"].update({
        "job_id": request["job_id"],
        "identity_digest": request["identity"]["identity_digest"],
    })

    assert route._attached_job_view(context) is None


def test_normal_full_job_with_corrupt_progress_is_not_visible(
    monkeypatch, tmp_path
):
    from server.routes import certification as route

    request, context, _state = _job_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(route, "_official_prerequisite_issues", lambda _ctx: [])
    state_path = (
        tmp_path / "official-jobs" / request["job_id"] / "state.json"
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["progress"]["rounds_requested"] = 7
    state["progress_digest"] = canonical_digest(state["progress"])
    state_path.write_text(json.dumps(state), encoding="utf-8")

    assert route._attached_job_view(context) is None


def test_cancel_requires_operator_authority_and_exact_current_job(
    client, monkeypatch, tmp_path
):
    from server.routes import certification as route

    request, context, state = _job_fixture(tmp_path, monkeypatch)
    job = {
        **state,
        "job_dir": str(
            tmp_path / "official-jobs" / request["job_id"]
        ),
        "pending": True,
    }
    monkeypatch.setattr(route, "_current_attached_job", lambda: (job, context))
    monkeypatch.setattr(
        route,
        "_cancel_exact_job_sync",
        lambda job_id, **_kwargs: {
            "job_id": job_id,
            "state": "cancelled",
            "pending": False,
        },
    )
    monkeypatch.setenv("POK_CONTROL_TOKEN", "certification-token")

    forbidden = client.post(
        f"/api/certification/jobs/{request['job_id']}/cancel",
        headers={"Origin": "https://attacker.example"},
    )
    wrong = client.post(
        f"/api/certification/jobs/{'e' * 64}/cancel",
        headers={"X-Control-Token": "certification-token"},
    )
    valid = client.post(
        f"/api/certification/jobs/{request['job_id']}/cancel",
        headers={"X-Control-Token": "certification-token"},
    )

    assert forbidden.status_code == 403
    assert wrong.status_code == 404
    assert valid.status_code == 200
    assert valid.json()["state"] == "cancelled"
    assert valid.json()["workflow_run_id"] == context["workflow_run_id"]


def test_cancel_sync_rechecks_workflow_stage_revision_and_job_under_cas_lock(
    monkeypatch, tmp_path
):
    from server.routes import certification as route
    import evolution_infra

    job_id = "d" * 64
    candidate_hash = "c" * 64
    checkpoint = {
        "next_v": 144,
        "stage": "official_certifying",
        "workflow_run_id": "generation:144:current",
        "checkpoint_revision": 9,
        "official_job": {
            "job_id": job_id,
            "candidate_hash": candidate_hash,
        },
    }
    monkeypatch.setattr(
        evolution_infra,
        "PIPELINE_STATE_FILE",
        tmp_path / "pipeline_state.json",
    )
    monkeypatch.setattr(evolution_infra, "read_pipeline_checkpoint", lambda: checkpoint)
    monkeypatch.setattr(
        route,
        "_projection",
        lambda: _projection(
            workflow="generation:144:current",
            stage="official_certifying",
        ),
    )
    monkeypatch.setattr(route, "hash_path", lambda _path: candidate_hash)
    monkeypatch.setattr(
        route,
        "strict_checkpoint_event_identity",
        lambda *_a, **_k: {
            "gen": 144,
            "evaluation_epoch": "national_tcp_policy_v1",
            "workflow_run_id": checkpoint["workflow_run_id"],
        },
    )
    calls: list[str] = []
    monkeypatch.setattr(
        route,
        "cancel_job",
        lambda value, **_kwargs: calls.append(value) or {
            "job_id": value,
            "state": "cancelled",
        },
    )

    stale = route._cancel_exact_job_sync(
        job_id,
        workflow_run_id="generation:144:stale",
        candidate_version=144,
        checkpoint_revision=9,
    )
    valid = route._cancel_exact_job_sync(
        job_id,
        workflow_run_id="generation:144:current",
        candidate_version=144,
        checkpoint_revision=9,
    )

    assert stale["state"] == "identity_mismatch"
    assert valid["state"] == "cancelled"
    assert calls == [job_id]


def test_exact_manual_v143_bootstrap_job_is_read_only_visible(
    client, monkeypatch, tmp_path
):
    from server.routes import certification as route

    request, context, _root = _bootstrap_job_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        route,
        "cancel_job",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("bootstrap job entered cancel authority")
        ),
    )

    jobs_response = client.get("/api/certification/jobs")
    detail = client.get(f"/api/certification/jobs/{request['job_id']}")
    cancel = client.post(f"/api/certification/jobs/{request['job_id']}/cancel")

    assert jobs_response.status_code == detail.status_code == 200
    assert [item["job_id"] for item in jobs_response.json()["jobs"]] == [
        request["job_id"]
    ]
    payload = detail.json()
    assert payload["candidate_version"] == 143
    assert payload["workflow_run_id"] == context["workflow_run_id"]
    assert payload["formal_authority"] == "operator_bootstrap_full_v5_job"
    assert payload["certification_profile"] == "first_strict_control_v1"
    assert payload["opponent_authority"] == "system_control"
    assert payload["formal_profile"] == {
        "self_play_rounds": 5,
        "opponent_rounds": 3,
        "target_hands": 70,
    }
    assert payload["strength_evidence_weight"] == 0
    assert payload["strategy_evidence_weight"] == 0
    assert payload["read_only"] is True
    assert payload["cancel_allowed"] is False
    transition = jobs_response.json()["operator_transition"]
    assert transition["state"] == "bootstrap_running"
    assert transition["job_id"] == request["job_id"]
    assert transition["command"] is None
    # Cancellation deliberately resolves only checkpoint-attached normal jobs.
    assert cancel.status_code == 404


def test_bootstrap_transition_requires_operator_start_when_no_job_exists(
    client,
    monkeypatch,
    tmp_path,
):
    request, _context, root = _bootstrap_job_fixture(tmp_path, monkeypatch)
    directory = root / request["job_id"]
    for path in directory.iterdir():
        path.unlink()
    directory.rmdir()

    payload = client.get("/api/certification/jobs").json()

    assert payload["jobs"] == []
    transition = payload["operator_transition"]
    assert transition["state"] == "bootstrap_required"
    assert transition.get("job_id") is None
    assert "bootstrap-first-strict" in transition["command"]
    assert "--force" not in transition["command"]


@pytest.mark.parametrize("corruption", ("invalid_digest", "partial_request"))
def test_malformed_bootstrap_attempt_never_reopens_nonforce_start(
    client,
    monkeypatch,
    tmp_path,
    corruption,
):
    request, _context, root = _bootstrap_job_fixture(tmp_path, monkeypatch)
    request_path = root / request["job_id"] / "request.json"
    if corruption == "invalid_digest":
        payload = json.loads(request_path.read_text(encoding="utf-8"))
        payload["manager_sha256"] = "e" * 64
        request_path.write_text(json.dumps(payload), encoding="utf-8")
    else:
        request_path.unlink()

    payload = client.get("/api/certification/jobs").json()

    assert payload["jobs"] == []
    transition = payload["operator_transition"]
    assert transition["state"] == "bootstrap_failed"
    assert transition["reason"] == "authorized_bootstrap_job_validation_failed"
    assert transition["command"].endswith(" --force")


def test_failed_bootstrap_job_projects_explicit_force_retry(
    client,
    monkeypatch,
    tmp_path,
):
    request, _context, root = _bootstrap_job_fixture(tmp_path, monkeypatch)
    state_path = root / request["job_id"] / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.update({
        "state": "failed",
        "phase": "failed",
        "failure": {
            "code": "official_exe_failed",
            "message": "deterministic failure",
        },
    })
    state_path.write_text(json.dumps(state), encoding="utf-8")

    payload = client.get("/api/certification/jobs").json()

    assert [row["job_id"] for row in payload["jobs"]] == [request["job_id"]]
    assert any(
        item.startswith("failure_code:official_exe_failed")
        for item in payload["jobs"][0]["issues"]
    )
    transition = payload["operator_transition"]
    assert transition["state"] == "bootstrap_failed"
    assert transition["job_id"] == request["job_id"]
    assert transition["command"].endswith(" --force")


def test_retired_jsonl_queue_route_is_absent(client):
    paths = {getattr(route, "path", None) for route in client.app.routes}
    assert "/api/certification/queue" not in paths
    assert client.get("/api/certification/queue").status_code == 404


def test_bootstrap_job_hides_invalid_parked_digest_and_selection(
    client, monkeypatch, tmp_path
):
    request, context, _root = _bootstrap_job_fixture(tmp_path, monkeypatch)
    parked = context["checkpoint"]["audit_context"]["official_bootstrap_request"]
    parked["candidate_hash"] = "d" * 64

    drifted_projection = client.get("/api/certification/jobs").json()
    assert drifted_projection["jobs"] == []
    assert drifted_projection["operator_transition"]["state"] == "bootstrap_failed"
    assert drifted_projection["operator_transition"]["command"].endswith(" --force")
    assert client.get(
        f"/api/certification/jobs/{request['job_id']}"
    ).status_code == 404

    # Restore a digest-valid parked request, then drift the signed operator
    # selection while keeping the durable request's outer digests valid.
    parked["candidate_hash"] = context["candidate_hash"]
    parked["request_digest"] = canonical_digest({
        key: value for key, value in parked.items() if key != "request_digest"
    })
    request["opponent_selection"]["operator_bootstrap_authorization"][
        "candidate_hash"
    ] = "e" * 64
    auth = request["opponent_selection"]["operator_bootstrap_authorization"]
    auth["authorization_digest"] = canonical_digest({
        key: value for key, value in auth.items() if key != "authorization_digest"
    })
    drifted = _rekey_request(request)
    _write_manager_job(_root, drifted)

    projection = client.get("/api/certification/jobs").json()
    assert projection["jobs"] == []
    assert projection["operator_transition"]["state"] == "bootstrap_failed"
    assert projection["operator_transition"]["command"].endswith(" --force")


def test_bootstrap_job_hides_candidate_hash_drift_even_with_valid_outer_digests(
    client, monkeypatch, tmp_path
):
    request, _context, root = _bootstrap_job_fixture(tmp_path, monkeypatch)
    identity = request["identity"]
    identity["candidate_hash"] = "f" * 64
    identity["identity_digest"] = canonical_digest({
        key: value for key, value in identity.items() if key != "identity_digest"
    })
    drifted = _rekey_request(request)
    # Remove the original exact job so this specifically tests the drifted
    # identity rather than ambiguity handling.
    original = root / request["job_id"]
    for path in original.iterdir():
        path.unlink()
    original.rmdir()
    _write_manager_job(root, drifted)

    payload = client.get("/api/certification/jobs").json()
    assert payload["jobs"] == []
    assert payload["operator_transition"]["state"] == "bootstrap_failed"
    assert payload["operator_transition"]["command"].endswith(" --force")
    assert client.get(
        f"/api/certification/jobs/{drifted['job_id']}"
    ).status_code == 404


def test_bootstrap_projection_fails_closed_on_multiple_exact_authorized_jobs(
    client, monkeypatch, tmp_path
):
    from first_strict_control import CONTROL_ID
    import official_bootstrap

    request, context, root = _bootstrap_job_fixture(tmp_path, monkeypatch)
    second_spec = build_spec(
        "full",
        context["candidate"],
        opponent=request["spec"]["opponent"],
        round_timeout_sec=901.0,
        bootstrap_control_id=CONTROL_ID,
    )
    second = jobs._request_payload(
        second_spec,
        opponent_selection=request["opponent_selection"],
        source_v=None,
    )
    _write_manager_job(root, second)
    monkeypatch.setattr(
        official_bootstrap,
        "validate_operator_bootstrap_authorized_selection",
        lambda *_a, **_k: {"valid": True, "issues": []},
    )

    projection = client.get("/api/certification/jobs").json()
    assert projection["jobs"] == []
    assert projection["operator_transition"]["state"] == "bootstrap_failed"
    assert projection["operator_transition"]["reason"] == (
        "multiple_authorized_bootstrap_jobs"
    )
    assert client.get(
        f"/api/certification/jobs/{request['job_id']}"
    ).status_code == 404
    assert client.get(
        f"/api/certification/jobs/{second['job_id']}"
    ).status_code == 404


def test_bootstrap_projection_ignores_old_v155_and_non_bootstrap_jobs(
    client, monkeypatch, tmp_path
):
    request, _context, root = _bootstrap_job_fixture(tmp_path, monkeypatch)
    stale = _write_strict_bot(tmp_path, 155)
    stale_spec = build_spec(
        "full",
        stale,
        opponent=request["spec"]["opponent"],
        quality_admission=_structural_quality_admission(stale),
    )
    stale_request = jobs._request_payload(
        stale_spec,
        opponent_selection={
            "selected": True,
            "candidate": str(stale.resolve()),
            "opponent": request["opponent_selection"]["opponent"],
        },
        source_v=154,
    )
    _write_manager_job(root, stale_request)

    queue = client.get("/api/certification/jobs")

    assert queue.status_code == 200
    assert [item["job_id"] for item in queue.json()["jobs"]] == [request["job_id"]]
    assert client.get(
        f"/api/certification/jobs/{stale_request['job_id']}"
    ).status_code == 404


def test_completed_bootstrap_job_requires_exact_envelope_and_certificate_identity(
    client, monkeypatch, tmp_path
):
    from official_job_envelope import build_job_envelope
    from server.routes import certification as route
    import official_bootstrap

    request, context, root = _bootstrap_job_fixture(tmp_path, monkeypatch)
    directory = root / request["job_id"]
    envelope = build_job_envelope(
        request,
        attempt=1,
        attempt_nonce="a" * 64,
        suite_dir=directory / "suite_attempt_01",
    )
    status = {
        "status": "official-certified",
        "mode": "full",
        "policy_id": "official-full-v5",
        "certificate_digest": "d" * 64,
        "certification_identity": request["identity"],
        "opponent_selection": request["opponent_selection"],
        "official_job_envelope": envelope,
    }
    _write_completed_result(directory, request, status)
    monkeypatch.setattr(
        route,
        "authoritative_verdict_status_issues",
        lambda _status: (_ for _ in ()).throw(
            AssertionError("post-consumption pre-ledger validator was reused")
        ),
    )
    monkeypatch.setattr(route, "official_full_certified", lambda *_a, **_k: True)
    monkeypatch.setattr(route, "official_compliance_verdict", lambda _status: {
        "ok": True,
        "classification": "pass",
        "blocking": False,
        "inconclusive": False,
    })
    monkeypatch.setattr(
        official_bootstrap,
        "validate_completed_operator_bootstrap_authorization",
        lambda supplied, candidate, **_k: {
            "valid": (
                supplied == status
                and Path(candidate).resolve() == context["candidate"]
            ),
            "issues": [],
        },
    )

    exact = client.get(f"/api/certification/jobs/{request['job_id']}")
    assert exact.status_code == 200
    queue = client.get("/api/certification/jobs").json()
    assert queue["operator_transition"]["state"] == "ready_to_finalize"
    assert "finalize-first-strict" in queue["operator_transition"]["command"]
    assert queue["operator_transition"]["certificate_digest"] == (
        exact.json()["certificate_digest"]
    )

    # A result file can be internally digest-consistent and still not belong
    # to this durable request.  The read projection must reject that state.
    drifted = json.loads(json.dumps(status))
    drifted["official_job_envelope"]["request_digest"] = "9" * 64
    unsigned = {
        key: value
        for key, value in drifted["official_job_envelope"].items()
        if key != "envelope_digest"
    }
    drifted["official_job_envelope"]["envelope_digest"] = canonical_digest(unsigned)
    _write_completed_result(directory, request, drifted)
    assert client.get(
        f"/api/certification/jobs/{request['job_id']}"
    ).status_code == 404

    stale_identity = json.loads(json.dumps(status))
    stale_identity["certification_identity"]["candidate_hash"] = "8" * 64
    stale_identity["certification_identity"]["identity_digest"] = canonical_digest({
        key: value
        for key, value in stale_identity["certification_identity"].items()
        if key != "identity_digest"
    })
    _write_completed_result(directory, request, stale_identity)
    assert client.get(
        f"/api/certification/jobs/{request['job_id']}"
    ).status_code == 404
