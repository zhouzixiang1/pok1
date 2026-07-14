"""Adversarial HTTP tests for strict-epoch certification projections."""

from __future__ import annotations

import json
from pathlib import Path

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
            "eligibility_receipt": {"receipt_digest": "b" * 64},
        },
    }
    spec = build_spec(mode, candidate, opponent=opponent)
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
    assert payload["read_only"] is True
    assert payload["cancel_allowed"] is False
    # Cancellation deliberately resolves only checkpoint-attached normal jobs.
    assert cancel.status_code == 404


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

    assert client.get("/api/certification/jobs").json()["jobs"] == []
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

    assert client.get("/api/certification/jobs").json()["jobs"] == []


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

    assert client.get("/api/certification/jobs").json()["jobs"] == []
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

    assert client.get("/api/certification/jobs").json()["jobs"] == []
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
