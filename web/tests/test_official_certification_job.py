import os
from pathlib import Path
import subprocess
import sys

import pytest

from bot_artifact import canonical_digest
from official_certification import build_spec
import official_certification_job as jobs


def _bot(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "national_bot.py").write_text("# native\n", encoding="utf-8")
    return path


def _spec(tmp_path: Path):
    return build_spec(
        "full",
        _bot(tmp_path / "bots" / "national_v200"),
        opponent=_bot(tmp_path / "bots" / "national_v142"),
    )


def _fake_spawn(directory, state, *, max_attempts, new_suite):
    del directory
    attempt = int(state.get("attempt", 0) or 0) + int(bool(new_suite))
    return {
        **state,
        "schema_version": jobs.JOB_SCHEMA_VERSION,
        "manager_version": jobs.JOB_MANAGER_VERSION,
        "state": "starting",
        "phase": "worker_handshake",
        "attempt": max(1, attempt),
        "max_attempts": max_attempts,
        "pid": 424242,
        "pgid": 424242,
        "boot_id": jobs._boot_id(),
        "claim_token": "test-claim",
        "pid_start_ticks": "123",
        "heartbeat_at_epoch": 10.0,
        "updated_at_epoch": 10.0,
        "failure": None,
        "waiting_for_resource": None,
        "last_progress_at_epoch": 10.0,
        "progress": {"rounds_requested": 8, "rounds_completed": 0},
        "worker_restart_count": (
            0 if new_suite else int(state.get("worker_restart_count", 0) or 0) + 1
        ),
    }


def test_worker_pythonpath_imports_core_and_repo_packages_from_arbitrary_cwd(tmp_path):
    env = os.environ.copy()
    env["PYTHONPATH"] = jobs._worker_pythonpath("/preserved/operator/path")

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import national_native; import sever; print('worker-imports-ok')",
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "worker-imports-ok"
    assert env["PYTHONPATH"].split(os.pathsep)[:2] == [
        str(jobs.SERVICE_PATH.parent),
        str(jobs.ROOT),
    ]
    assert env["PYTHONPATH"].split(os.pathsep)[-1] == "/preserved/operator/path"


def test_progress_scanner_reads_live_formal_execution_logs(tmp_path):
    directory = tmp_path / "job"
    run_dir = (
        directory
        / "suite_attempt_01"
        / "self_play_01"
        / "executions"
        / "run_0000000000000000001_123"
    )
    run_dir.mkdir(parents=True)
    log = "\n".join([
        "DISPATCH line='preflop|SMALLBLIND|<0,3><1,3>'",
        "DISPATCH line='earnChips 100'",
        "DISPATCH line='preflop|BIGBLIND|<2,3><3,3>'",
        "DISPATCH line='earnChips -100'",
        "DISPATCH line='preflop|SMALLBLIND|<4,3><5,3>'",
    ])
    (run_dir / "botA.log").write_text(log, encoding="utf-8")
    (run_dir / "botB.log").write_text(log, encoding="utf-8")
    request = {
        "spec": {
            "self_play_rounds": 1,
            "opponent_rounds": 0,
        },
    }

    progress = jobs._scan_progress(directory, request, attempt=1)

    assert progress["rounds_completed"] == 0
    assert progress["active_round"] == {
        "kind": "self_play",
        "index": 1,
        "passed": False,
        "hands_started": 3,
        "settlements": 2,
        "observed_bytes": len(log.encode("utf-8")) * 2,
        "duration_sec": None,
        "issue_count": 0,
    }


def test_job_start_and_poll_are_identity_stable(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_JOB_DIR", str(tmp_path / "jobs"))
    monkeypatch.setattr(jobs, "_spawn_worker", _fake_spawn)
    monkeypatch.setattr(jobs, "_process_alive", lambda *_args: True)
    spec = _spec(tmp_path)

    first = jobs.start_or_poll_job(spec, source_v=142)
    second = jobs.start_or_poll_job(spec, source_v=142)

    assert first["pending"] is True
    assert second["pending"] is True
    assert first["job_id"] == second["job_id"]
    assert second["attempt"] == 1


def test_bootstrap_job_delayed_state_drift_fails_before_worker_spawn(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("POK_OFFICIAL_JOB_DIR", str(tmp_path / "jobs"))
    candidate = _bot(tmp_path / "bots" / "national_v143")
    opponent = _bot(tmp_path / "controls" / "first_strict_control_v1")
    spec = build_spec(
        "full",
        candidate,
        opponent=opponent,
        bootstrap_control_id="first_strict_control_v1",
    )
    selection = {
        "selected": True,
        "bootstrap_control_id": spec.bootstrap_control_id,
        "candidate": str(candidate),
        "opponent": {"path": str(opponent), "eligible": True},
        "operator_bootstrap_authorization": {
            "authorization_digest": "a" * 64,
        },
    }
    monkeypatch.setattr(
        jobs,
        "_bootstrap_authorization_issues",
        lambda _request: ["official_bootstrap_candidate_hash_mismatch"],
    )
    monkeypatch.setattr(
        jobs,
        "_spawn_worker",
        lambda *_args, **_kwargs: pytest.fail("drifted bootstrap spawned a worker"),
    )

    result = jobs.start_or_poll_job(spec, opponent_selection=selection)

    assert result["state"] == "failed"
    assert result["phase"] == "bootstrap_authorization"
    assert result["failure_class"] == "authorization"
    assert result["issues"] == ["official_bootstrap_candidate_hash_mismatch"]


def test_dead_worker_restarts_without_losing_job_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_JOB_DIR", str(tmp_path / "jobs"))
    monkeypatch.setattr(jobs, "_spawn_worker", _fake_spawn)
    alive = {"value": True}
    monkeypatch.setattr(jobs, "_process_alive", lambda *_args: alive["value"])
    spec = _spec(tmp_path)
    first = jobs.start_or_poll_job(spec)
    alive["value"] = False

    restarted = jobs.start_or_poll_job(spec)

    assert restarted["job_id"] == first["job_id"]
    assert restarted["state"] == "starting"
    assert restarted["attempt"] == 1
    assert restarted["worker_restart_count"] == 1


def test_completed_job_is_verified_and_retryable(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_JOB_DIR", str(tmp_path / "jobs"))
    monkeypatch.setattr(jobs, "_spawn_worker", _fake_spawn)
    spec = _spec(tmp_path)
    pending = jobs.start_or_poll_job(spec)
    directory = Path(pending["job_dir"])
    result = {
        "schema_version": jobs.JOB_SCHEMA_VERSION,
        "job_id": pending["job_id"],
        "request_digest": jobs._read_json(directory / "request.json")["request_digest"],
        "attempt": 1,
        "attempt_nonce": "a" * 64,
        "status": {"status": "official-inconclusive", "mode": "full"},
    }
    result["result_digest"] = canonical_digest(result)
    result_path = jobs._result_path(directory, 1)
    jobs._write_json(result_path, result)
    state = jobs._read_json(directory / "state.json")
    state.update({
        "state": "completed",
        "result_path": str(result_path),
        "result_digest": result["result_digest"],
    })
    jobs._write_json(directory / "state.json", state)

    completed = jobs.start_or_poll_job(spec)
    retried = jobs.start_or_poll_job(spec, retry_terminal=True)

    assert completed["status"]["status"] == "official-inconclusive"
    assert retried["state"] == "starting"
    assert retried["attempt"] == 2


def test_worker_writes_content_bound_terminal_result(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_JOB_DIR", str(tmp_path / "jobs"))
    spec = _spec(tmp_path)
    request = jobs._request_payload(spec, opponent_selection=None, source_v=142)
    directory = Path(os.environ["POK_OFFICIAL_JOB_DIR"]) / request["job_id"]
    directory.mkdir(parents=True)
    jobs._write_json(directory / "request.json", request)
    claim_token = "unit-worker-claim"
    jobs._write_json(directory / "state.json", {
        "schema_version": jobs.JOB_SCHEMA_VERSION,
        "manager_version": jobs.JOB_MANAGER_VERSION,
        "job_id": request["job_id"],
        "request_digest": request["request_digest"],
        "state": "starting",
        "attempt": 1,
        "max_attempts": 3,
        "pid": os.getpid(),
        "pgid": os.getpgrp(),
        "boot_id": jobs._boot_id(),
        "claim_token": claim_token,
        "pid_start_ticks": jobs._proc_start_ticks(os.getpid()),
    })
    import official_certification

    monkeypatch.setattr(official_certification, "certification_identity", lambda _spec: request["identity"])
    monkeypatch.setattr(
        official_certification,
        "run_identity_bound_certification_job",
        lambda *_args, **_kwargs: {"status": "official-certified", "mode": "full"},
    )

    assert jobs._worker_main(directory, claim_token) == 0
    state = jobs._read_json(directory / "state.json")
    result = jobs._read_json(jobs._result_path(directory, 1))
    assert state["state"] == "completed"
    assert state["result_digest"] == result["result_digest"]
    assert result["status"]["status"] == "official-certified"


def test_completed_result_is_adopted_after_worker_dies(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_JOB_DIR", str(tmp_path / "jobs"))
    monkeypatch.setattr(jobs, "_spawn_worker", _fake_spawn)
    monkeypatch.setattr(jobs, "_process_alive", lambda *_args: False)
    spec = _spec(tmp_path)
    pending = jobs.start_or_poll_job(spec)
    directory = Path(pending["job_dir"])
    request = jobs._read_json(directory / "request.json")
    result = {
        "schema_version": jobs.JOB_SCHEMA_VERSION,
        "job_id": pending["job_id"],
        "request_digest": request["request_digest"],
        "attempt": 1,
        "status": {"status": "official-certified", "mode": "full"},
    }
    result["result_digest"] = canonical_digest(result)
    jobs._write_json(jobs._result_path(directory, 1), result)

    adopted = jobs.start_or_poll_job(spec)

    assert adopted["state"] == "completed"
    assert adopted["status"]["status"] == "official-certified"
    assert adopted["attempt"] == 1


def test_cancelled_job_does_not_implicitly_respawn(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_JOB_DIR", str(tmp_path / "jobs"))
    calls = {"spawn": 0}

    def fake_spawn(*args, **kwargs):
        calls["spawn"] += 1
        return _fake_spawn(*args, **kwargs)

    monkeypatch.setattr(jobs, "_spawn_worker", fake_spawn)
    monkeypatch.setattr(jobs, "_process_alive", lambda *_args: True)
    monkeypatch.setattr(jobs, "_terminate_worker_group", lambda *_args, **_kwargs: None)
    spec = _spec(tmp_path)
    pending = jobs.start_or_poll_job(spec)
    cancelled = jobs.cancel_job(pending["job_id"])
    polled = jobs.start_or_poll_job(spec, retry_terminal=True)

    assert cancelled["state"] == "cancelled"
    assert polled["state"] == "cancelled"
    assert calls["spawn"] == 1


def test_global_slot_queues_second_job(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_JOB_DIR", str(tmp_path / "jobs"))
    monkeypatch.setattr(jobs, "_spawn_worker", _fake_spawn)
    monkeypatch.setattr(jobs, "_process_alive", lambda *_args: True)
    first_spec = _spec(tmp_path / "first")
    second_spec = _spec(tmp_path / "second")

    first = jobs.start_or_poll_job(first_spec)
    second = jobs.start_or_poll_job(second_spec)

    assert first["state"] == "starting"
    assert second["state"] == "queued"
    assert second["pending"] is True


def test_busy_official_platform_keeps_job_queued_without_spawning(
    tmp_path, monkeypatch
):
    import official_certification

    monkeypatch.setenv("POK_OFFICIAL_JOB_DIR", str(tmp_path / "jobs"))
    monkeypatch.setattr(official_certification, "official_lock_busy", lambda: True)
    monkeypatch.setattr(
        jobs,
        "_spawn_worker",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("must not spawn while Arena owns official port")
        ),
    )

    result = jobs.start_or_poll_job(_spec(tmp_path))

    assert result["state"] == "queued"
    assert result["phase"] == "waiting_for_official_platform"
    assert result["pending"] is True


def test_terminal_retry_preserves_new_suite_intent_while_platform_is_busy(
    tmp_path, monkeypatch
):
    import official_certification

    monkeypatch.setenv("POK_OFFICIAL_JOB_DIR", str(tmp_path / "jobs"))
    monkeypatch.setattr(jobs, "_spawn_worker", _fake_spawn)
    spec = _spec(tmp_path)
    first = jobs.start_or_poll_job(spec)
    directory = Path(first["job_dir"])
    state = jobs._read_json(directory / "state.json")
    state.update({"state": "failed", "phase": "worker_exception", "attempt": 1})
    jobs._write_json(directory / "state.json", state)

    busy = {"value": True}
    monkeypatch.setattr(
        official_certification,
        "official_lock_busy",
        lambda: busy["value"],
    )
    waiting = jobs.start_or_poll_job(spec, retry_terminal=True)
    assert waiting["state"] == "queued"
    assert waiting["phase"] == "retry_queued"
    assert waiting["attempt"] == 1

    busy["value"] = False
    restarted = jobs.start_or_poll_job(spec, retry_terminal=True)
    assert restarted["state"] == "starting"
    assert restarted["attempt"] == 2
    assert restarted["waiting_for_resource"] is None


def test_progress_reads_live_logs_before_receipt_exists(tmp_path):
    directory = tmp_path / "job"
    round_dir = directory / "suite_attempt_01" / "self_play_01"
    round_dir.mkdir(parents=True)
    log = "\n".join([
        "DISPATCH line='preflop|SMALLBLIND|<0,3><1,4>'",
        "DISPATCH line='earnChips 100'",
    ])
    (round_dir / "botA.log").write_text(log, encoding="utf-8")
    (round_dir / "botB.log").write_text(log, encoding="utf-8")
    request = {
        "spec": {"self_play_rounds": 5, "opponent_rounds": 3},
    }

    progress = jobs._scan_progress(directory, request, 1)

    assert progress["rounds_completed"] == 0
    assert progress["active_round"]["kind"] == "self_play"
    assert progress["active_round"]["hands_started"] == 1
    assert progress["active_round"]["settlements"] == 1
    assert progress["active_round"]["observed_bytes"] > 0


def test_dead_worker_does_not_adopt_result_for_other_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_JOB_DIR", str(tmp_path / "jobs"))
    monkeypatch.setattr(jobs, "_spawn_worker", _fake_spawn)
    monkeypatch.setattr(jobs, "_process_alive", lambda *_args: False)
    spec = _spec(tmp_path)
    pending = jobs.start_or_poll_job(spec)
    directory = Path(pending["job_dir"])
    request = jobs._read_json(directory / "request.json")
    result = {
        "schema_version": jobs.JOB_SCHEMA_VERSION,
        "job_id": "different-job",
        "request_digest": request["request_digest"],
        "attempt": 1,
        "status": {"status": "official-certified", "mode": "full"},
    }
    result["result_digest"] = canonical_digest(result)
    jobs._write_json(jobs._result_path(directory, 1), result)

    reconciled = jobs.start_or_poll_job(spec)

    assert reconciled["state"] == "starting"
    assert reconciled["worker_restart_count"] == 1
    assert "status" not in reconciled


def test_second_suite_attempt_forces_complete_exe_rerun(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_JOB_DIR", str(tmp_path / "jobs"))
    spec = _spec(tmp_path)
    request = jobs._request_payload(spec, opponent_selection=None, source_v=142)
    directory = Path(os.environ["POK_OFFICIAL_JOB_DIR"]) / request["job_id"]
    directory.mkdir(parents=True)
    jobs._write_json(directory / "request.json", request)
    claim_token = "retry-worker-claim"
    jobs._write_json(directory / "state.json", {
        "schema_version": jobs.JOB_SCHEMA_VERSION,
        "manager_version": jobs.JOB_MANAGER_VERSION,
        "job_id": request["job_id"],
        "request_digest": request["request_digest"],
        "state": "starting",
        "attempt": 2,
        "attempt_nonce": "b" * 64,
        "max_attempts": 3,
        "pid": os.getpid(),
        "pgid": os.getpgrp(),
        "boot_id": jobs._boot_id(),
        "claim_token": claim_token,
        "pid_start_ticks": jobs._proc_start_ticks(os.getpid()),
    })
    import official_certification

    seen = {}
    monkeypatch.setattr(official_certification, "certification_identity", lambda _spec: request["identity"])

    def fake_run(*_args, **kwargs):
        seen.update(kwargs)
        return {"status": "official-certified", "mode": "full"}

    monkeypatch.setattr(official_certification, "run_identity_bound_certification_job", fake_run)

    assert jobs._worker_main(directory, claim_token) == 0
    assert seen["force"] is True
    assert seen["suite_dir"] == jobs._suite_dir(directory, 2)
    assert len(seen["job_envelope"]["envelope_digest"]) == 64


def test_result_payload_rejects_attempt_mismatch(tmp_path):
    directory = tmp_path / "job"
    directory.mkdir()
    request = {"job_id": "job", "request_digest": "request"}
    jobs._write_json(directory / "request.json", request)
    payload = {
        "schema_version": jobs.JOB_SCHEMA_VERSION,
        "job_id": "job",
        "request_digest": "request",
        "attempt": 1,
        "status": {},
    }
    payload["result_digest"] = canonical_digest(payload)
    jobs._write_json(jobs._result_path(directory, 2), payload)

    with pytest.raises(ValueError, match="attempt_mismatch"):
        jobs._result_payload(directory, {
            "job_id": "job",
            "request_digest": "request",
            "attempt": 2,
            "result_path": str(jobs._result_path(directory, 2)),
        })


def test_reconcile_prioritizes_active_owner_over_queued_prefix(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_JOB_DIR", str(tmp_path / "jobs"))
    root = Path(os.environ["POK_OFFICIAL_JOB_DIR"])
    seen = []
    for index in range(5):
        directory = root / f"a-queued-{index}"
        directory.mkdir(parents=True)
        spec = _spec(tmp_path / f"queued-{index}")
        request = jobs._request_payload(spec, opponent_selection=None, source_v=142)
        jobs._write_json(directory / "request.json", request)
        jobs._write_json(directory / "state.json", {
            "state": "queued",
            "job_id": request["job_id"],
            "request_digest": request["request_digest"],
            "created_at_epoch": 1.0 + index,
        })
    active_dir = root / "z-active-owner"
    active_dir.mkdir(parents=True)
    active_spec = _spec(tmp_path / "active")
    active_request = jobs._request_payload(active_spec, opponent_selection=None, source_v=142)
    jobs._write_json(active_dir / "request.json", active_request)
    jobs._write_json(active_dir / "state.json", {
        "state": "running",
        "job_id": active_request["job_id"],
        "request_digest": active_request["request_digest"],
        "heartbeat_at_epoch": 99.0,
    })

    def fake_start(spec, **_kwargs):
        seen.append(spec.candidate)
        return {"state": "queued", "pending": True}

    monkeypatch.setattr(jobs, "start_or_poll_job", fake_start)

    jobs.reconcile_jobs(limit=4)

    assert seen[0] == active_spec.candidate
    assert len(seen) == 4


def test_stranded_operator_cancel_request_finalizes_cancelled(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_JOB_DIR", str(tmp_path / "jobs"))
    monkeypatch.setattr(jobs, "_spawn_worker", _fake_spawn)
    monkeypatch.setattr(jobs, "_process_alive", lambda *_args: False)
    monkeypatch.setattr(jobs, "_terminate_worker_group", lambda *_args, **_kwargs: None)
    spec = _spec(tmp_path)
    pending = jobs.start_or_poll_job(spec)
    directory = Path(pending["job_dir"])
    state = jobs._read_json(directory / "state.json")
    state.update({
        "state": "cancel_requested",
        "phase": "operator_cancel",
        "cancel_reason": "operator_cancelled",
    })
    jobs._write_json(directory / "state.json", state)

    finalized = jobs.start_or_poll_job(spec)

    assert finalized["state"] == "cancelled"
    assert finalized["pending"] is False


def test_stranded_reconcile_cancel_request_resumes_worker(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_JOB_DIR", str(tmp_path / "jobs"))
    monkeypatch.setattr(jobs, "_spawn_worker", _fake_spawn)
    monkeypatch.setattr(jobs, "_process_alive", lambda *_args: False)
    monkeypatch.setattr(jobs, "_terminate_worker_group", lambda *_args, **_kwargs: None)
    spec = _spec(tmp_path)
    pending = jobs.start_or_poll_job(spec)
    directory = Path(pending["job_dir"])
    state = jobs._read_json(directory / "state.json")
    state.update({"state": "cancel_requested", "phase": "reconcile_worker"})
    jobs._write_json(directory / "state.json", state)

    resumed = jobs.start_or_poll_job(spec)

    assert resumed["state"] == "starting"
    assert resumed["worker_restart_count"] == 1


def test_concurrent_cancel_cannot_be_resurrected_by_reconcile(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_JOB_DIR", str(tmp_path / "jobs"))
    calls = {"spawn": 0}

    def fake_spawn(*args, **kwargs):
        calls["spawn"] += 1
        return _fake_spawn(*args, **kwargs)

    monkeypatch.setattr(jobs, "_spawn_worker", fake_spawn)
    monkeypatch.setattr(jobs, "_process_alive", lambda *_args: False)
    spec = _spec(tmp_path)
    pending = jobs.start_or_poll_job(spec)
    directory = Path(pending["job_dir"])

    def concurrent_cancel(state, target_directory, **_kwargs):
        current = jobs._read_json(target_directory / "state.json")
        current.update({
            "state": "cancelled",
            "phase": "cancelled",
            "claim_token": state["claim_token"],
        })
        jobs._write_json(target_directory / "state.json", current)

    monkeypatch.setattr(jobs, "_terminate_worker_group", concurrent_cancel)

    result = jobs.start_or_poll_job(spec)

    assert result["state"] == "cancelled"
    assert calls["spawn"] == 1


def test_volatile_selection_diagnostics_do_not_change_job_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_JOB_DIR", str(tmp_path / "jobs"))
    monkeypatch.setattr(jobs, "_spawn_worker", _fake_spawn)
    monkeypatch.setattr(jobs, "_process_alive", lambda *_args: True)
    spec = _spec(tmp_path)
    opponent = Path(spec.opponent)
    from bot_artifact import hash_path

    stable_opponent = {
        "bot": opponent.name,
        "path": str(opponent.resolve()),
        "artifact_hash": hash_path(opponent),
        "eligible": True,
        "reason": "official_certified",
    }
    receipt_payload = {
        "schema_version": 1,
        "kind": "official_full_certificate",
        "role": "official_opponent",
        "bot": opponent.name,
        "artifact_hash": stable_opponent["artifact_hash"],
        "policy_id": "official-full-v5",
        "certificate_digest": "a" * 64,
    }
    stable_opponent["eligibility_receipt"] = {
        **receipt_payload,
        "receipt_digest": canonical_digest(receipt_payload),
    }
    first_selection = {
        "selected": True,
        "candidate": spec.candidate,
        "opponent": stable_opponent,
        "considered": [{"bot": "old"}],
        "readiness": {"certified_alternatives": 1},
    }
    second_selection = {
        **first_selection,
        "considered": [{"bot": "new"}, {"bot": "old"}],
        "readiness": {"certified_alternatives": 2},
    }

    first = jobs.start_or_poll_job(spec, opponent_selection=first_selection, source_v=142)
    second = jobs.start_or_poll_job(spec, opponent_selection=second_selection, source_v=142)

    assert first["job_id"] == second["job_id"]
    assert second["state"] == "starting"


def test_authorization_receipt_changes_official_job_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_JOB_DIR", str(tmp_path / "jobs"))
    monkeypatch.setattr(jobs, "_spawn_worker", _fake_spawn)
    monkeypatch.setattr(jobs, "_process_alive", lambda *_args: True)
    spec = _spec(tmp_path)
    opponent = Path(spec.opponent)
    from bot_artifact import hash_path

    artifact_hash = hash_path(opponent)

    def selection(certificate_digest: str) -> dict:
        receipt_payload = {
            "schema_version": 1,
            "kind": "official_full_certificate",
            "role": "official_opponent",
            "bot": opponent.name,
            "artifact_hash": artifact_hash,
            "policy_id": "official-full-v5",
            "certificate_digest": certificate_digest,
        }
        return {
            "selected": True,
            "candidate": spec.candidate,
            "opponent": {
                "bot": opponent.name,
                "path": str(opponent.resolve()),
                "artifact_hash": artifact_hash,
                "eligible": True,
                "reason": "official_certified",
                "eligibility_receipt": {
                    **receipt_payload,
                    "receipt_digest": canonical_digest(receipt_payload),
                },
            },
        }

    first = jobs.start_or_poll_job(
        spec,
        opponent_selection=selection("a" * 64),
        source_v=142,
    )
    second = jobs.start_or_poll_job(
        spec,
        opponent_selection=selection("b" * 64),
        source_v=142,
    )

    assert first["job_id"] != second["job_id"]
