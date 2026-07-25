import asyncio
import json
from pathlib import Path

from bot_artifact import canonical_digest
from bot_namespace import bot_name, bot_relpath
from conftest import STRICT_SOURCE_V, STRICT_TARGET_V
from official_certification import STATUS_CERTIFIED, STATUS_INCONCLUSIVE


def _native_bot(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "national_bot.py").write_text("import socket\n# native tcp entry\n", encoding="utf-8")
    return path


def _allow_tmp_candidate_publication_shape(monkeypatch) -> None:
    """Keep commit-flow tests focused past the repository-shape precondition.

    These candidates intentionally live under pytest's temporary directory,
    outside the Git checkout. Publication-shape behavior has dedicated tests;
    commit-flow cases below exercise official parking/jobs and push handling.
    """
    import bot_artifact

    monkeypatch.setattr(bot_artifact, "publication_shape_errors", lambda _path: [])


def _structural_quality_admission(candidate: Path, *, next_v: int) -> dict:
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
    payload = {
        "schema_version": FORMAL_QUALITY_ADMISSION_SCHEMA_VERSION,
        "kind": "official-formal-quality-admission",
        "candidate_path": str(candidate.resolve()),
        "candidate_hash": "a" * 64,
        "checkpoint": {
            "evaluation_epoch": "national_tcp_policy_v1",
            "workflow_run_id": "pytest-commit-workflow",
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


def test_strict_normal_full_commit_binds_admission_before_job_and_blocks_missing(
    tmp_path,
    monkeypatch,
):
    import official_certification
    import official_certification_job
    import official_platform_harness
    import tool_commit

    candidate = _native_bot(tmp_path / "bots" / "national_v144")
    opponent = _native_bot(tmp_path / "bots" / "national_v143")
    selection = {
        "selected": True,
        "opponent": {
            "path": str(opponent),
            "bot": opponent.name,
            "reason": "official_certified",
        },
        "considered": [],
    }
    monkeypatch.setattr(official_certification, "read_status", lambda _candidate: {})
    monkeypatch.setattr(
        official_certification,
        "official_full_certified",
        lambda status, _candidate, **_kwargs: status.get("status") == STATUS_CERTIFIED,
    )
    monkeypatch.setattr(
        official_certification,
        "select_official_opponent",
        lambda *_args, **_kwargs: selection,
    )
    monkeypatch.setattr(tool_commit, "get_active_bots", lambda: [str(opponent)])
    monkeypatch.setattr(
        official_certification_job,
        "start_or_poll_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("missing admission must block before a durable job")
        ),
    )
    monkeypatch.setattr(
        official_platform_harness,
        "build_formal_quality_admission",
        lambda *_args, **_kwargs: {
            "valid": False,
            "issues": ["official_formal_quality_gate_ledger_missing"],
            "admission": None,
        },
    )

    blocked = asyncio.run(
        tool_commit._run_official_full_commit_gate(
            144,
            143,
            candidate,
            {"national_execution_mode": "native_tcp"},
            {},
        )
    )
    assert blocked["outcome"] == "quality_admission_blocked"
    assert blocked["passed"] is False

    admission = _structural_quality_admission(candidate, next_v=144)
    seen = []
    monkeypatch.setattr(
        official_platform_harness,
        "build_formal_quality_admission",
        lambda *_args, **_kwargs: {"valid": True, "issues": [], "admission": admission},
    )
    monkeypatch.setattr(
        official_certification_job,
        "start_or_poll_job",
        lambda spec, **_kwargs: seen.append(spec) or {
            "state": "completed",
            "pending": False,
            "job_id": "job-admission",
            "status": {
                "status": STATUS_CERTIFIED,
                "mode": "full",
                "issues": [],
                "official_evidence_path": str(tmp_path / "evidence.json"),
                "official_evidence_summary": {"classification": "pass", "blocking": False},
            },
        },
    )
    accepted = asyncio.run(
        tool_commit._run_official_full_commit_gate(
            144,
            143,
            candidate,
            {"national_execution_mode": "native_tcp"},
            {},
        )
    )
    assert accepted["passed"] is True
    assert len(seen) == 1
    assert seen[0].quality_admission == admission

    monkeypatch.setattr(
        official_certification_job,
        "start_or_poll_job",
        lambda *_args, **_kwargs: {
            "state": "failed",
            "phase": "quality_admission",
            "failure_class": "quality",
            "pending": False,
            "issues": ["official_job_quality_admission_live_invalid:test_drift"],
        },
    )
    drifted = asyncio.run(
        tool_commit._run_official_full_commit_gate(
            144,
            143,
            candidate,
            {"national_execution_mode": "native_tcp"},
            {},
            retry_terminal=True,
        )
    )
    assert drifted["passed"] is False
    assert drifted["outcome"] == "quality_admission_blocked"
    assert drifted["failure_class"] == "quality"
    assert drifted["issues"] == [
        "official_job_quality_admission_live_invalid:test_drift"
    ]


def test_official_full_commit_gate_requires_full_spec(tmp_path, monkeypatch):
    import official_certification
    import official_certification_job
    import official_platform_harness
    import tool_commit

    target_v = STRICT_TARGET_V + 10
    source_v = STRICT_TARGET_V + 9
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _native_bot(tmp_path / "bots" / bot_name(target_v))
    opponent = _native_bot(tmp_path / "bots" / bot_name(STRICT_TARGET_V + 5))
    (opponent / ".completed").touch()
    monkeypatch.setenv("POK_OFFICIAL_OPPONENT", str(opponent))
    monkeypatch.setattr(tool_commit, "get_active_bots", lambda: [str(opponent)])
    monkeypatch.setattr(
        official_certification,
        "select_official_opponent",
        lambda *_a, **_k: {
            "selected": True,
            "opponent": {
                "path": str(opponent),
                "bot": opponent.name,
                "reason": "official_certified",
            },
            "considered": [],
        },
    )
    monkeypatch.setattr(
        official_certification,
        "official_full_certified",
        lambda status, _candidate, **_kwargs: status.get("status") == STATUS_CERTIFIED,
    )
    monkeypatch.setattr(
        official_platform_harness,
        "build_formal_quality_admission",
        lambda *_a, **_k: {"valid": True, "issues": [], "admission": _structural_quality_admission(candidate, next_v=target_v)},
    )
    calls = []

    def fake_start_or_poll(spec, **kwargs):
        calls.append((spec, kwargs))
        return {
            "state": "completed",
            "pending": False,
            "job_id": "job-1",
            "status": {
                "status": STATUS_CERTIFIED,
                "mode": "full",
                "issues": [],
                "cache_hit": False,
                "official_evidence_path": str(tmp_path / "evidence.json"),
                "official_evidence_summary": {"classification": "pass", "blocking": False},
            },
        }

    monkeypatch.setattr(official_certification_job, "start_or_poll_job", fake_start_or_poll)

    result = asyncio.run(
        tool_commit._run_official_full_commit_gate(
            target_v,
            source_v,
            candidate,
            {"national_execution_mode": "native_tcp"},
            {},
        )
    )

    assert result["passed"] is True
    assert calls
    spec, kwargs = calls[0]
    assert spec.mode == "full"
    assert spec.self_play_rounds == 5
    assert spec.opponent_rounds == 3
    assert spec.target_hands == 70
    assert kwargs["source_v"] == source_v
    assert kwargs["retry_terminal"] is False
    assert result["opponent_selection"]["opponent"]["reason"] == "official_certified"


def test_official_full_commit_gate_reuses_valid_bootstrap_certificate_before_selection(
    tmp_path,
    monkeypatch,
):
    import official_bootstrap
    import official_certification
    import official_certification_job
    import tool_commit

    candidate = _native_bot(tmp_path / "bots" / "national_v143")
    bootstrap_spec = {
        "mode": "full",
        "policy_id": "official-full-v5",
        "candidate": str(candidate.resolve()),
        "opponent": str((tmp_path / "controls" / "first_strict_control_v1").resolve()),
        "self_play_rounds": 5,
        "opponent_rounds": 3,
        "target_hands": 70,
        "round_timeout_sec": 900.0,
        "no_progress_timeout_sec": 75.0,
        "bootstrap_control_id": "first_strict_control_v1",
    }
    selection = {
        "selected": True,
        "bootstrap_control_id": bootstrap_spec["bootstrap_control_id"],
        "opponent": {
            "bot": "first_strict_control_v1",
            "reason": "first_strict_control_bootstrap",
        },
    }
    existing = {
        "status": STATUS_CERTIFIED,
        "mode": "full",
        "policy_id": "official-full-v5",
        "issues": [],
        "certificate_digest": "a" * 64,
        "certificate_path": str(tmp_path / "certificate.json"),
        "official_evidence_path": str(tmp_path / "evidence.json"),
        "official_evidence_summary": {"classification": "pass", "blocking": False},
        "certification_identity": {
            "candidate_hash": "b" * 64,
            "spec": bootstrap_spec,
        },
        "opponent_selection": selection,
    }
    monkeypatch.setattr(official_certification, "read_status", lambda _candidate: existing)
    validator_calls = []

    def valid_full(status, requested_candidate, **kwargs):
        validator_calls.append((status, requested_candidate, kwargs))
        return status is existing and Path(requested_candidate) == candidate

    monkeypatch.setattr(official_certification, "official_full_certified", valid_full)
    monkeypatch.setattr(
        official_bootstrap,
        "validate_completed_operator_bootstrap_authorization",
        lambda *_args, **_kwargs: {
            "valid": True,
            "issues": [],
            "certificate_digest": existing["certificate_digest"],
        },
    )
    monkeypatch.setattr(
        official_certification,
        "select_official_opponent",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("valid existing certificate must bypass normal opponent selection")
        ),
    )
    monkeypatch.setattr(
        official_certification_job,
        "start_or_poll_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("valid existing certificate must not start another official job")
        ),
    )

    result = asyncio.run(
        tool_commit._run_official_full_commit_gate(
            143,
            142,
            candidate,
            {"national_execution_mode": "native_tcp"},
            {},
        )
    )

    assert result["passed"] is True
    assert result["outcome"] == "passed"
    assert result["reused_existing_certificate"] is True
    assert result["bootstrap_certificate"] is True
    assert result["completed_bootstrap_authorization"]["valid"] is True
    assert result["spec"] == bootstrap_spec
    assert result["opponent_selection"] == selection
    assert len(validator_calls) == 1

    monkeypatch.setattr(
        official_bootstrap,
        "validate_completed_operator_bootstrap_authorization",
        lambda *_args, **_kwargs: {
            "valid": False,
            "issues": ["official_bootstrap_completed_authorization_drift"],
        },
    )
    blocked = asyncio.run(
        tool_commit._run_official_full_commit_gate(
            143,
            142,
            candidate,
            {"national_execution_mode": "native_tcp"},
            {},
        )
    )
    assert blocked["passed"] is False
    assert blocked["outcome"] == "completed_authorization_failure"
    assert blocked["failure_class"] == "authorization"


def test_official_full_commit_gate_does_not_reuse_invalid_existing_certificate(
    tmp_path,
    monkeypatch,
):
    import official_certification
    import official_certification_job
    import tool_commit

    candidate = _native_bot(tmp_path / "bots" / "national_v143")
    tampered = {
        "status": STATUS_CERTIFIED,
        "mode": "full",
        "policy_id": "official-full-v5",
        "certificate_digest": "tampered",
        "certification_identity": {"candidate_hash": "wrong"},
    }
    monkeypatch.setattr(official_certification, "read_status", lambda _candidate: tampered)
    monkeypatch.setattr(
        official_certification,
        "official_full_certified",
        lambda status, _candidate, **_kwargs: False,
    )
    selection_calls = []

    def no_normal_opponent(*args, **kwargs):
        selection_calls.append((args, kwargs))
        return {"selected": False, "reason": "no_official_eligible_opponent"}

    monkeypatch.setattr(official_certification, "select_official_opponent", no_normal_opponent)
    monkeypatch.setattr(
        official_certification_job,
        "start_or_poll_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("no opponent means no official job")
        ),
    )
    monkeypatch.setattr(tool_commit, "get_active_bots", lambda: [])

    result = asyncio.run(
        tool_commit._run_official_full_commit_gate(
            143,
            142,
            candidate,
            {"national_execution_mode": "native_tcp"},
            {},
        )
    )

    assert result["passed"] is False
    assert result["outcome"] == "operator_bootstrap_required"
    assert result["operator_action_required"] is True
    assert result["action"] == "run_explicit_first_strict_bootstrap"
    assert result["opponent_selection"]["reason"] == "no_official_eligible_opponent"
    assert len(selection_calls) == 1


def test_official_full_commit_gate_blocks_inconclusive_result(tmp_path, monkeypatch):
    import official_certification
    import official_certification_job
    import official_platform_harness
    import tool_commit

    target_v = STRICT_TARGET_V + 10
    source_v = STRICT_TARGET_V + 9
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _native_bot(tmp_path / "bots" / bot_name(target_v))
    opponent = _native_bot(tmp_path / "bots" / bot_name(STRICT_TARGET_V + 5))
    (opponent / ".completed").touch()
    monkeypatch.setenv("POK_OFFICIAL_OPPONENT", str(opponent))
    monkeypatch.setattr(tool_commit, "get_active_bots", lambda: [str(opponent)])
    monkeypatch.setattr(
        official_certification,
        "select_official_opponent",
        lambda *_a, **_k: {
            "selected": True,
            "opponent": {
                "path": str(opponent),
                "bot": opponent.name,
                "reason": "official_certified",
            },
            "considered": [],
        },
    )
    monkeypatch.setattr(
        official_certification,
        "official_full_certified",
        lambda _status, _candidate, **_kwargs: False,
    )
    monkeypatch.setattr(
        official_platform_harness,
        "build_formal_quality_admission",
        lambda *_a, **_k: {"valid": True, "issues": [], "admission": _structural_quality_admission(candidate, next_v=target_v)},
    )

    def fake_start_or_poll(_spec, **_kwargs):
        return {
            "state": "completed",
            "pending": False,
            "job_id": "job-1",
            "status": {
                "status": STATUS_INCONCLUSIVE,
                "mode": "full",
                "issues": ["thp_incomplete_for_full_certification: hands=69 target=70"],
                "official_evidence_path": str(tmp_path / "evidence.json"),
                "official_evidence_summary": {"classification": "inconclusive", "blocking": False},
            },
        }

    monkeypatch.setattr(official_certification_job, "start_or_poll_job", fake_start_or_poll)

    result = asyncio.run(
        tool_commit._run_official_full_commit_gate(
            target_v,
            source_v,
            candidate,
            {"national_execution_mode": "native_tcp"},
            {},
        )
    )

    assert result["passed"] is False
    assert result["outcome"] == "infrastructure_failure"
    assert result["status"]["status"] == STATUS_INCONCLUSIVE
    assert result["issues"] == ["thp_incomplete_for_full_certification: hands=69 target=70"]


def test_official_full_commit_gate_blocks_non_native_workflow(monkeypatch, tmp_path):
    import official_certification
    import tool_commit

    monkeypatch.setattr(
        official_certification,
        "run_certification",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not run")),
    )

    result = asyncio.run(
        tool_commit._run_official_full_commit_gate(
            10,
            9,
            tmp_path / "bot",
            {"national_execution_mode": "adapter"},
            {},
        )
    )

    assert result["passed"] is False
    assert result["reason"] == "formal_submission_requires_native_tcp"
    assert "only national_native/native_tcp" in result["error"]


def test_git_commit_bot_rejects_missing_official_certificate_before_git(monkeypatch):
    import evolution_infra

    git_calls = []
    monkeypatch.setattr(evolution_infra, "_git", lambda *args, **kwargs: git_calls.append(args) or "")

    with __import__("pytest").raises(RuntimeError, match="official full certificate is required"):
        evolution_infra.git_commit_bot(
            143,
            142,
            "test",
            official_certificate=None,
        )

    assert git_calls == []


def test_official_full_gate_records_repairable_checkpoint_stage(monkeypatch, tmp_path):
    import tool_commit

    writes = []

    def fake_write_checkpoint(*args, **kwargs):
        writes.append((args, kwargs))
        return True

    monkeypatch.setattr(tool_commit, "write_pipeline_checkpoint", fake_write_checkpoint)
    stage = tool_commit._record_official_full_gate_checkpoint(
        134,
        120,
        {
            "next_v": 134,
            "source_v": 120,
            "stage": "verified",
            "checkpoint_revision": 7,
            "workflow_run_id": "generation:134:official-gate-test",
            "master_plan": {"strategy": "crossover"},
            "gate_results": {"precommit_eval": {"passed": True}},
            "parent2_v": 117,
        },
        {
            "passed": False,
            "status": {"status": "official-failed", "mode": "full"},
            "verdict": {"blocking": True, "classification": "official_full_incomplete"},
            "official_evidence_path": str(tmp_path / "evidence.json"),
            "official_evidence_summary": {"classification": "obvious_decision_error", "blocking": True},
            "issues": ["official_full_round_incomplete_after_progress: hands_started=33 target=70"],
        },
    )

    assert stage == "official_failed"
    args, kwargs = writes[0]
    assert args[:3] == (134, 120, "official_failed")
    assert kwargs["gate_results"]["official_full"]["repairable_by_workers"] is True
    assert "official_full_round_incomplete_after_progress" in kwargs["reviewer_feedback"]


def test_official_full_gate_records_inconclusive_checkpoint_stage(monkeypatch, tmp_path):
    import tool_commit

    writes = []
    monkeypatch.setattr(tool_commit, "write_pipeline_checkpoint", lambda *a, **k: writes.append((a, k)) or True)

    stage = tool_commit._record_official_full_gate_checkpoint(
        134,
        120,
        {
            "next_v": 134,
            "source_v": 120,
            "stage": "verified",
            "checkpoint_revision": 7,
            "workflow_run_id": "generation:134:official-gate-test",
        },
        {
            "passed": False,
            "status": {"status": "official-inconclusive", "mode": "full"},
            "verdict": {"blocking": False, "inconclusive": True, "classification": "inconclusive"},
            "official_evidence_summary": {"classification": "harness", "blocking": False, "inconclusive": True},
            "issues": ["official_full_round_no_game_progress: target=70"],
        },
    )

    assert stage == "official_inconclusive"
    args, kwargs = writes[0]
    assert args[:3] == (134, 120, "official_inconclusive")
    assert kwargs["gate_results"]["official_full"]["repairable_by_workers"] is False


def test_no_opponent_parks_candidate_for_explicit_operator_bootstrap(monkeypatch):
    import tool_commit
    import official_bootstrap

    writes = []
    monkeypatch.setattr(
        tool_commit,
        "write_pipeline_checkpoint",
        lambda *args, **kwargs: writes.append((args, kwargs)) or True,
    )
    monkeypatch.setattr(
        official_bootstrap,
        "build_operator_bootstrap_parked_request",
        lambda *_args, **_kwargs: {
            "valid": True,
            "issues": [],
            "request": {"request_digest": "a" * 64},
        },
    )
    gate = {
        "passed": False,
        "outcome": "operator_bootstrap_required",
        "operator_action_required": True,
        "action": "run_explicit_first_strict_bootstrap",
        "opponent_selection": {
            "selected": False,
            "reason": "no_official_eligible_opponent",
        },
    }

    ok = tool_commit._record_official_bootstrap_required_checkpoint(
        143,
        142,
        {
            "next_v": 143,
            "source_v": 142,
            "stage": "verified",
            "master_plan": {"strategy": "range-update"},
            "gate_results": {"precommit_eval": {"passed": True}},
        },
        gate,
        candidate_hash="b" * 64,
    )

    assert ok is True
    args, kwargs = writes[0]
    assert args[:3] == (143, 142, "official_bootstrap_required")
    recorded = kwargs["gate_results"]["official_full"]
    assert recorded["operator_action_required"] is True
    assert recorded["repairable_by_workers"] is False


def test_mutable_evidence_summary_cannot_route_bot_repair(monkeypatch):
    import tool_commit

    writes = []
    monkeypatch.setattr(
        tool_commit,
        "write_pipeline_checkpoint",
        lambda *args, **kwargs: writes.append((args, kwargs)) or True,
    )

    stage = tool_commit._record_official_full_gate_checkpoint(
        134,
        120,
        {
            "next_v": 134,
            "source_v": 120,
            "stage": "verified",
            "checkpoint_revision": 7,
            "workflow_run_id": "generation:134:official-gate-test",
        },
        {
            "passed": False,
            "verdict": {
                "blocking": False,
                "inconclusive": True,
                "classification": "inconclusive",
            },
            "official_evidence_summary": {
                "classification": "protocol",
                "blocking": True,
                "inconclusive": False,
            },
            "issues": ["untrusted diagnostic text"],
        },
    )

    assert stage == "official_inconclusive"
    assert writes[0][1]["gate_results"]["official_full"]["repairable_by_workers"] is False


def test_official_terminal_checkpoint_reports_cas_failure(monkeypatch):
    import tool_commit

    monkeypatch.setattr(tool_commit, "write_pipeline_checkpoint", lambda *_a, **_k: False)

    stage = tool_commit._record_official_full_gate_checkpoint(
        134,
        120,
        {
            "next_v": 134,
            "source_v": 120,
            "stage": "official_certifying",
            "checkpoint_revision": 7,
            "workflow_run_id": "generation:134:official-gate-test",
        },
        {
            "passed": False,
            "verdict": {"blocking": True, "classification": "protocol_violation"},
            "issues": ["illegal_check"],
        },
    )

    assert stage == ""


def test_official_terminal_checkpoint_refuses_an_unbound_stale_snapshot(monkeypatch):
    """A final gate result may not overwrite a checkpoint without its CAS tuple."""

    import tool_commit

    calls = []
    monkeypatch.setattr(
        tool_commit,
        "write_pipeline_checkpoint",
        lambda *_args, **_kwargs: calls.append(True) or True,
    )

    stage = tool_commit._record_official_full_gate_checkpoint(
        134,
        120,
        {"next_v": 134, "source_v": 120, "stage": "official_certifying"},
        {
            "passed": False,
            "outcome": "quality_admission_blocked",
            "failure_class": "quality",
            "quality_admission_refresh": True,
        },
    )

    assert stage == ""
    assert calls == []


def test_official_full_pass_is_persisted_in_verified_gate_ledger(monkeypatch, tmp_path):
    import tool_commit

    writes = []
    monkeypatch.setattr(
        tool_commit,
        "write_pipeline_checkpoint",
        lambda *args, **kwargs: writes.append((args, kwargs)) or True,
    )
    gate = {
        "passed": True,
        "status": {
            "status": "official-certified",
            "mode": "full",
            "policy_id": "official-full-v5",
            "certificate_digest": "cert-digest",
        },
        "certificate_digest": "cert-digest",
        "certification_identity": {"candidate_hash": "candidate-hash"},
    }

    ok = tool_commit._record_official_full_pass_checkpoint(
        143,
        142,
        {
            "next_v": 143,
            "source_v": 142,
            "stage": "verified",
            "gate_results": {"precommit_eval": {"passed": True}},
        },
        gate,
    )

    assert ok is True
    args, kwargs = writes[0]
    assert args[:3] == (143, 142, "verified")
    assert kwargs["gate_results"]["official_full"]["passed"] is True
    assert kwargs["gate_results"]["official_full"]["certificate_digest"] == "cert-digest"


def test_first_strict_full_pass_preserves_parked_checkpoint_until_git_publish(monkeypatch):
    import tool_commit

    writes = []
    monkeypatch.setattr(
        tool_commit,
        "write_pipeline_checkpoint",
        lambda *args, **kwargs: writes.append((args, kwargs)) or True,
    )

    ok = tool_commit._record_official_full_pass_checkpoint(
        143,
        142,
        {
            "next_v": 143,
            "source_v": 142,
            "stage": "official_bootstrap_required",
            "gate_results": {"precommit_eval": {"passed": True}},
        },
        {
            "passed": True,
            "bootstrap_certificate": True,
            "status": {"status": STATUS_CERTIFIED, "mode": "full"},
            "certificate_digest": "bootstrap-cert-digest",
        },
    )

    assert ok is True
    args, kwargs = writes[0]
    assert args[:3] == (143, 142, "verified")
    assert kwargs["gate_results"]["official_full"]["passed"] is True


def test_commit_bot_revalidates_completed_bootstrap_immediately_before_git(
    monkeypatch, tmp_path
):
    import official_bootstrap
    import official_certification
    import tool_commit

    candidate = _native_bot(tmp_path / "bots" / "national_v143")
    _allow_tmp_candidate_publication_shape(monkeypatch)
    checkpoint = {
        "next_v": 143,
        "source_v": 142,
        "stage": "official_bootstrap_required",
        "gate_results": {},
    }
    status = {
        "status": STATUS_CERTIFIED,
        "mode": "full",
        "policy_id": "official-full-v5",
        "certificate_digest": "a" * 64,
        "certification_identity": {
            "candidate_hash": "b" * 64,
            "spec": {
                "candidate": str(candidate.resolve()),
                "bootstrap_control_id": "first_strict_control_v1",
            },
        },
    }
    gate = {
        "passed": True,
        "outcome": "passed",
        "bootstrap_certificate": True,
        "status": status,
    }
    git_calls = []
    validation_calls = []
    monkeypatch.setattr(tool_commit, "get_bot_dir", lambda _version: candidate)
    monkeypatch.setattr(tool_commit, "_matching_checkpoint", lambda *_args: checkpoint)
    monkeypatch.setattr(tool_commit, "read_pipeline_checkpoint", lambda: checkpoint)
    monkeypatch.setattr(
        tool_commit,
        "validate_commit_gate_ledger",
        lambda *_args, **_kwargs: {
            "missing_gates": [],
            "failed_gates": [],
            "gate_results": {},
            "checkpoint_stage": "official_bootstrap_required",
            "current_code_fingerprint": "b" * 64,
        },
    )
    monkeypatch.setattr(
        tool_commit,
        "_run_official_full_commit_gate",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=gate),
    )
    monkeypatch.setattr(
        tool_commit,
        "_record_official_full_pass_checkpoint",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        official_certification,
        "official_full_certified",
        lambda *_args, **_kwargs: True,
    )

    def drifted_rebind(*_args, **_kwargs):
        validation_calls.append(True)
        return {
            "valid": False,
            "issues": ["official_bootstrap_completed_evaluation_contract_drift"],
        }

    monkeypatch.setattr(
        official_bootstrap,
        "validate_completed_operator_bootstrap_authorization",
        drifted_rebind,
    )
    monkeypatch.setattr(tool_commit, "load_ratings", lambda: {})
    monkeypatch.setattr(tool_commit, "compute_h2h_avg_winrate", lambda *_a: None)
    monkeypatch.setattr(tool_commit, "git_has_tag", lambda _v: False)
    monkeypatch.setattr(
        tool_commit,
            "ensure_bot_git_publication",
        lambda *_args, **_kwargs: git_calls.append(True) or True,
    )

    raw = asyncio.run(
        tool_commit.commit_bot.handler({
            "version": 143,
            "source_v": 142,
            "strategy": "bootstrap-test",
            "review_approved": True,
        })
    )
    payload = json.loads(raw["content"][0]["text"])

    assert payload["failure_class"] == "authorization"
    assert payload["checkpoint_preserved"] is True
    assert "immediately before Git publication" in payload["error"]
    assert validation_calls == [True]
    assert git_calls == []


def test_commit_bot_replays_final_ledger_after_official_pass_before_git(
    monkeypatch,
    tmp_path,
):
    import official_certification
    import tool_commit

    candidate = _native_bot(tmp_path / "bots" / "national_v147")
    _allow_tmp_candidate_publication_shape(monkeypatch)
    checkpoint = {
        "next_v": 147,
        "source_v": 142,
        "stage": "verified",
        "gate_results": {},
    }
    status = {
        "status": STATUS_CERTIFIED,
        "mode": "full",
        "policy_id": "official-full-v5",
        "certificate_digest": "a" * 64,
        "certification_identity": {"candidate_hash": "b" * 64},
    }
    ledger_calls = []
    git_calls = []

    def ledger(*_args, **_kwargs):
        ledger_calls.append(True)
        failed = [] if len(ledger_calls) == 1 else [{
            "gate": "first_strict_control_final_ledger",
            "reason": "strict pool changed during official certification",
        }]
        return {
            "missing_gates": [],
            "failed_gates": failed,
            "gate_results": {},
            "checkpoint_stage": "verified",
            "current_code_fingerprint": "b" * 64,
        }

    monkeypatch.setattr(tool_commit, "get_bot_dir", lambda _version: candidate)
    monkeypatch.setattr(tool_commit, "_matching_checkpoint", lambda *_a: checkpoint)
    monkeypatch.setattr(tool_commit, "read_pipeline_checkpoint", lambda: checkpoint)
    monkeypatch.setattr(tool_commit, "validate_commit_gate_ledger", ledger)
    monkeypatch.setattr(
        tool_commit,
        "_run_official_full_commit_gate",
        lambda *_a, **_k: asyncio.sleep(0, result={
            "passed": True,
            "outcome": "passed",
            "status": status,
        }),
    )
    monkeypatch.setattr(
        tool_commit,
        "_record_official_full_pass_checkpoint",
        lambda *_a, **_k: True,
    )
    monkeypatch.setattr(
        official_certification,
        "official_full_certified",
        lambda *_a, **_k: True,
    )
    monkeypatch.setattr(tool_commit, "load_ratings", lambda: {})
    monkeypatch.setattr(tool_commit, "compute_h2h_avg_winrate", lambda *_a: None)
    monkeypatch.setattr(tool_commit, "git_has_tag", lambda _v: False)
    monkeypatch.setattr(
        tool_commit,
            "ensure_bot_git_publication",
        lambda *_a, **_k: git_calls.append(True) or True,
    )

    raw = asyncio.run(tool_commit.commit_bot.handler({
        "version": 147,
        "source_v": 142,
        "strategy": "final-ledger-race-test",
        "review_approved": True,
    }))
    payload = json.loads(raw["content"][0]["text"])

    assert ledger_calls == [True, True]
    assert git_calls == []
    assert "after official certification" in payload["error"]
    assert payload["failed_gates"][0]["gate"] == (
        "first_strict_control_final_ledger"
    )


def test_commit_bot_parks_no_opponent_without_git(monkeypatch, tmp_path):
    import tool_commit

    candidate = _native_bot(tmp_path / "bots" / "national_v143")
    _allow_tmp_candidate_publication_shape(monkeypatch)
    checkpoint = {"next_v": 143, "source_v": 142, "stage": "verified"}
    gate = {
        "passed": False,
        "outcome": "operator_bootstrap_required",
        "operator_action_required": True,
        "action": "run_explicit_first_strict_bootstrap",
        "opponent_selection": {
            "selected": False,
            "reason": "no_official_eligible_opponent",
        },
    }
    parked = []
    monkeypatch.setattr(tool_commit, "get_bot_dir", lambda _version: candidate)
    monkeypatch.setattr(tool_commit, "_matching_checkpoint", lambda *_args: checkpoint)
    monkeypatch.setattr(
        tool_commit,
        "validate_commit_gate_ledger",
        lambda *_args, **_kwargs: {
            "missing_gates": [],
            "failed_gates": [],
            "gate_results": {},
            "checkpoint_stage": "verified",
        },
    )
    monkeypatch.setattr(
        tool_commit,
        "_run_official_full_commit_gate",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=gate),
    )
    monkeypatch.setattr(
        tool_commit,
        "_record_official_bootstrap_required_checkpoint",
        lambda *args, **kwargs: parked.append((args, kwargs)) or True,
    )
    monkeypatch.setattr(tool_commit, "log_system_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        tool_commit,
            "ensure_bot_git_publication",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("operator bootstrap parking must not mutate Git")
        ),
    )

    result = asyncio.run(tool_commit.commit_bot.handler({
        "version": 143,
        "source_v": 142,
        "strategy": "test",
        "review_approved": True,
    }))
    payload = json.loads(result["content"][0]["text"])

    assert payload["checkpoint_stage"] == "official_bootstrap_required"
    assert payload["paused"] is True
    assert payload["committed"] is False
    assert "error" not in payload
    assert payload["operator_action_required"] is True
    assert payload["automatic_bootstrap_forbidden"] is True
    assert parked


def test_commit_bot_never_invokes_git_when_official_gate_fails(monkeypatch, tmp_path):
    import tool_commit

    candidate = _native_bot(tmp_path / "bots" / "national_v143")
    _allow_tmp_candidate_publication_shape(monkeypatch)
    checkpoint = {"next_v": 143, "source_v": 142, "stage": "verified"}
    monkeypatch.setattr(tool_commit, "get_bot_dir", lambda _version: candidate)
    monkeypatch.setattr(tool_commit, "_matching_checkpoint", lambda *_args: checkpoint)
    monkeypatch.setattr(
        tool_commit,
        "validate_commit_gate_ledger",
        lambda *_args, **_kwargs: {
            "missing_gates": [],
            "failed_gates": [],
            "gate_results": {},
            "checkpoint_stage": "verified",
        },
    )
    monkeypatch.setattr(
        tool_commit,
        "_run_official_full_commit_gate",
        lambda *_args, **_kwargs: asyncio.sleep(
            0,
            result={
                "passed": False,
                "status": {"status": STATUS_INCONCLUSIVE, "mode": "full"},
                "verdict": {"blocking": False, "inconclusive": True},
                "issues": ["official_full_round_no_game_progress: target=70"],
            },
        ),
    )
    monkeypatch.setattr(
        tool_commit,
        "_record_official_full_gate_checkpoint",
        lambda *_args, **_kwargs: "official_inconclusive",
    )
    monkeypatch.setattr(tool_commit, "log_system_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        tool_commit,
            "ensure_bot_git_publication",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("Git publication must not run after official gate failure")
        ),
    )

    result = asyncio.run(
        tool_commit.commit_bot.handler(
            {
                "version": 143,
                "source_v": 142,
                "strategy": "test",
                "review_approved": True,
            }
        )
    )
    payload = json.loads(result["content"][0]["text"])

    assert payload["checkpoint_stage"] == "official_inconclusive"
    assert payload["official_full_gate"]["passed"] is False


def test_commit_bot_keeps_quality_admission_failure_out_of_infrastructure_retry(
    monkeypatch,
    tmp_path,
):
    """Live quality drift is terminal evidence failure, never an infra retry."""

    import tool_commit

    candidate = _native_bot(tmp_path / "bots" / "national_v144")
    _allow_tmp_candidate_publication_shape(monkeypatch)
    checkpoint = {
        "next_v": 144,
        "source_v": 143,
        "stage": "verified",
        "reviewer_feedback": "keep reviewer-owned feedback",
        "official_job": {"job_id": "stale-official-job"},
    }
    gate = {
        "passed": False,
        "pending": False,
        "outcome": "quality_admission_blocked",
        "failure_class": "quality",
        "issues": ["official_job_quality_admission_live_invalid:test_drift"],
    }
    checkpoint_updates = []
    infrastructure_calls = []
    git_calls = []

    monkeypatch.setattr(tool_commit, "get_bot_dir", lambda _version: candidate)
    monkeypatch.setattr(tool_commit, "_matching_checkpoint", lambda *_args: checkpoint)
    monkeypatch.setattr(
        tool_commit,
        "_owned_infrastructure_failure",
        lambda *_args: (None, None),
    )

    async def no_exhausted(*_args, **_kwargs):
        return None

    async def infrastructure_retry(*_args, **_kwargs):
        infrastructure_calls.append(True)
        raise AssertionError("quality admission must not record infrastructure retry")

    monkeypatch.setattr(
        tool_commit,
        "_execute_exhausted_infrastructure_failure",
        no_exhausted,
    )
    monkeypatch.setattr(
        tool_commit,
        "_record_infrastructure_failure",
        infrastructure_retry,
    )
    monkeypatch.setattr(
        tool_commit,
        "validate_commit_gate_ledger",
        lambda *_args, **_kwargs: {
            "missing_gates": [],
            "failed_gates": [],
            "gate_results": {},
            "checkpoint_stage": "verified",
            "current_code_fingerprint": "candidate-hash",
        },
    )
    monkeypatch.setattr(
        tool_commit,
        "_run_official_full_commit_gate",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=gate),
    )
    monkeypatch.setattr(
        tool_commit,
        "_record_official_full_gate_checkpoint",
        lambda *_args, **_kwargs: checkpoint_updates.append((_args, _kwargs))
        or "official_certifying",
    )
    monkeypatch.setattr(tool_commit, "log_system_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        tool_commit,
        "ensure_bot_git_publication",
        lambda *_args, **_kwargs: git_calls.append(True)
        or (_ for _ in ()).throw(
            AssertionError("quality admission failure must not publish Git")
        ),
    )

    raw = asyncio.run(tool_commit.commit_bot.handler({
        "version": 144,
        "source_v": 143,
        "strategy": "quality-drift-test",
        "review_approved": True,
    }))
    payload = json.loads(raw["content"][0]["text"])

    assert payload["checkpoint_stage"] == "official_certifying"
    assert payload["official_full_gate"]["outcome"] == "quality_admission_blocked"
    assert payload["official_full_gate"]["failure_class"] == "quality"
    assert infrastructure_calls == []
    assert len(checkpoint_updates) == 1
    assert checkpoint_updates[0][1]["clear_official_job"] is True
    assert git_calls == []


def test_quality_admission_checkpoint_record_preserves_stage_and_reviewer_feedback(
    monkeypatch,
):
    """A stale formal receipt returns to quality, never infra or worker repair."""

    import tool_commit

    checkpoint = {
        "next_v": 144,
        "source_v": 143,
        "stage": "official_certifying",
        "checkpoint_revision": 7,
        "workflow_run_id": "generation:144:official-gate-test",
        "reviewer_feedback": "reviewer evidence remains reviewer-owned",
        "official_job": {"job_id": "old-formal-job"},
        "gate_results": {"quality": {"all_passed": True}},
    }
    gate = {
        "passed": False,
        "pending": False,
        "outcome": "quality_admission_blocked",
        "failure_class": "quality",
        "issues": ["official_job_quality_admission_live_invalid:test_drift"],
    }
    writes = []

    def capture_checkpoint(*args, **kwargs):
        writes.append((args, kwargs))
        return True

    monkeypatch.setattr(tool_commit, "write_pipeline_checkpoint", capture_checkpoint)

    stage = tool_commit._record_official_full_gate_checkpoint(
        144,
        143,
        checkpoint,
        gate,
        clear_official_job=True,
    )

    assert stage == "official_certifying"
    assert len(writes) == 1
    args, kwargs = writes[0]
    assert args[:3] == (144, 143, "official_certifying")
    assert kwargs["reviewer_feedback"] == checkpoint["reviewer_feedback"]
    assert kwargs["clear_infra_failure"] is False
    assert kwargs["clear_official_job"] is True
    assert kwargs["expected_official_job_id"] == "old-formal-job"
    assert kwargs["touch_stage_timestamp"] is True
    assert kwargs["expected_checkpoint_revision"] == 7
    assert kwargs["expected_checkpoint_stage"] == "official_certifying"
    assert kwargs["expected_workflow_run_id"] == "generation:144:official-gate-test"
    recorded = kwargs["gate_results"]["official_full"]
    assert recorded["passed"] is False
    assert recorded["failure_class"] == "quality"
    assert recorded["quality_admission_refresh"] is True
    assert recorded["repairable_by_workers"] is False


def test_commit_bot_attaches_pending_official_job_without_git(monkeypatch, tmp_path):
    import tool_commit

    candidate = _native_bot(tmp_path / "bots" / "national_v143")
    _allow_tmp_candidate_publication_shape(monkeypatch)
    checkpoint = {"next_v": 143, "source_v": 142, "stage": "verified"}
    monkeypatch.setattr(tool_commit, "get_bot_dir", lambda _version: candidate)
    monkeypatch.setattr(tool_commit, "_matching_checkpoint", lambda *_args: checkpoint)
    monkeypatch.setattr(
        tool_commit,
        "validate_commit_gate_ledger",
        lambda *_args, **_kwargs: {
            "missing_gates": [],
            "failed_gates": [],
            "gate_results": {},
            "checkpoint_stage": "verified",
            "current_code_fingerprint": "candidate-hash",
        },
    )
    pending_gate = {
        "passed": False,
        "pending": True,
        "outcome": "pending",
        "spec": {
            "mode": "full",
            "policy_id": "official-full-v5",
            "candidate": str(candidate),
            "opponent": str(candidate),
            "self_play_rounds": 5,
            "opponent_rounds": 3,
            "target_hands": 70,
            "round_timeout_sec": 900.0,
            "no_progress_timeout_sec": 75.0,
        },
        "job": {
            "job_id": "job-1",
            "state": "running",
            "attempt": 1,
            "progress": {"rounds_completed": 2, "rounds_requested": 8},
        },
        "opponent_selection": {"opponent": {"bot": "national_v142"}},
    }
    monkeypatch.setattr(
        tool_commit,
        "_run_official_full_commit_gate",
        lambda *_a, **_k: asyncio.sleep(0, result=pending_gate),
    )
    monkeypatch.setattr(tool_commit, "_record_official_job_checkpoint", lambda *_a, **_k: True)
    monkeypatch.setattr(tool_commit, "log_system_event", lambda *_a, **_k: None)
    monkeypatch.setattr(
        tool_commit,
            "ensure_bot_git_publication",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("pending job must not mutate Git")),
    )

    result = asyncio.run(tool_commit.commit_bot.handler({
        "version": 143,
        "source_v": 142,
        "strategy": "test",
        "review_approved": True,
    }))
    payload = json.loads(result["content"][0]["text"])

    assert payload["pending"] is True
    assert payload["action"] == "poll_commit_bot"
    assert payload["checkpoint_stage"] == "official_certifying"


def test_required_push_failure_keeps_checkpoint_and_candidate_incomplete(monkeypatch, tmp_path):
    import national_runtime_authority
    import official_certification
    import post_publication_handoff
    import publication_transaction
    import tool_commit

    candidate = _native_bot(tmp_path / "bots" / "national_v143")
    status = {
        "status": STATUS_CERTIFIED,
        "mode": "full",
        "policy_id": "official-full-v5",
        "certificate_digest": "b" * 64,
        "certification_identity": {"candidate_hash": "a" * 64},
    }
    intent = {
        "publication_id": "f" * 64,
        "remote_publication_required": True,
        "final_gate_ledger_digest": "e" * 64,
    }
    checkpoint = {
        "next_v": 143,
        "source_v": 142,
        "parent2_v": None,
        "workflow_run_id": "generation:143:test",
        "checkpoint_revision": 12,
        "stage": "publishing",
        "publication_intent": intent,
        "gate_results": {"official_full": {"status": status}},
    }
    checkpoint_cleared = []
    ensure_calls = []
    remote_calls = []
    handoff_calls = []
    pending_proofs = []
    strict_pool = ["national_v143"]
    monkeypatch.setattr(tool_commit, "get_bot_dir", lambda _version: candidate)
    monkeypatch.setattr(tool_commit, "read_pipeline_checkpoint", lambda: checkpoint)
    monkeypatch.setattr(tool_commit, "git_has_tag", lambda _v: True)
    monkeypatch.setattr(
        tool_commit,
        "_existing_local_bot_tag_matches_certificate",
        lambda *_a, **_k: (True, ""),
    )

    def gate_ledger(*_args, **kwargs):
        pending_proofs.append(kwargs.get("pending_local_publication"))
        return {"missing_gates": [], "failed_gates": []}

    monkeypatch.setattr(
        tool_commit,
        "validate_commit_gate_ledger",
        gate_ledger,
    )
    monkeypatch.setattr(
        publication_transaction,
        "publication_gate_ledger_digest",
        lambda _ledger: intent["final_gate_ledger_digest"],
    )
    monkeypatch.setattr(
        publication_transaction, "publication_intent_live_errors", lambda *_a, **_k: []
    )
    monkeypatch.setattr(
        publication_transaction,
        "publication_intent_checkpoint_errors",
        lambda *_a, **_k: [],
    )
    proof = {"bot": "national_v143", "proof_digest": "proof"}
    monkeypatch.setattr(
            national_runtime_authority,
        "build_pending_local_publication_proof",
        lambda _path: proof,
    )
    monkeypatch.setattr(
            national_runtime_authority,
        "strict_published_bot_names",
        lambda **_k: tuple(strict_pool),
    )
    monkeypatch.setattr(official_certification, "official_full_certified", lambda *_a, **_k: True)

    def ensure(*_args, **_kwargs):
        ensure_calls.append(True)
        return {
            "commit_oid": "1" * 40,
            "local_refs": {},
            "push_ok": len(ensure_calls) > 1,
        }

    def verify(*_args, **_kwargs):
        remote_calls.append(True)
        if len(remote_calls) <= 2:
            return {"valid": False, "issues": ["remote_main_missing"]}
        return {"valid": True, "remote_main_oid": "1" * 40}

    monkeypatch.setattr(tool_commit, "ensure_bot_git_publication", ensure)
    monkeypatch.setattr(tool_commit, "verify_remote_bot_publication", verify)
    monkeypatch.setattr(tool_commit, "evolution_git_push_required", lambda: True)
    def ensure_handoff(**kwargs):
        # This test isolates remote publication recovery from the handoff's
        # filesystem journal, but it must still prove that checkpoint clearing
        # is downstream of the exact durable-handoff contract.
        assert kwargs["version"] == 143
        assert kwargs["source_v"] == 142
        assert kwargs["publishing_checkpoint"] is checkpoint
        assert kwargs["allow_local_only"] is False
        publication = kwargs["publication_result"]
        assert publication["committed"] is True
        assert publication["publication_id"] == intent["publication_id"]
        assert publication["completed_sentinel_written"] is True
        assert publication["remote_proof"]["valid"] is True
        handoff_calls.append(kwargs)
        return {"identity_digest": "d" * 64, "state": "pending"}

    monkeypatch.setattr(
        post_publication_handoff,
        "ensure_post_publication_handoff",
        ensure_handoff,
    )
    monkeypatch.setattr(
        tool_commit,
        "clear_pipeline_checkpoint",
        lambda **kwargs: checkpoint_cleared.append(kwargs) or True,
    )

    first = tool_commit._resume_publication_transaction(143, 142, checkpoint)

    assert first["committed"] is False
    assert first["local_committed"] is True
    assert first["checkpoint_preserved"] is True
    assert first["completed_sentinel_written"] is False
    assert first["remote_proof"]["issues"] == ["remote_main_missing"]
    assert not (candidate / ".completed").exists()
    assert checkpoint_cleared == []
    assert pending_proofs == [proof]
    assert handoff_calls == []

    # This transaction is now independently proven on the remote.  A later
    # strict publication must not reopen its dynamic pre-push authority and
    # strand sentinel/checkpoint recovery.
    strict_pool.append("national_v144")
    recovered = tool_commit._resume_publication_transaction(143, 142, checkpoint)

    assert recovered["committed"] is True
    assert recovered["push_ok"] is True
    assert recovered["checkpoint_cleared"] is True
    assert (candidate / ".completed").read_text(encoding="utf-8") == (
        f"publication_id={intent['publication_id']}\n"
    )
    assert len(ensure_calls) == 2
    assert len(remote_calls) == 3
    assert pending_proofs == [proof]
    assert len(checkpoint_cleared) == 1
    assert checkpoint_cleared[0]["expected_checkpoint_stage"] == "publishing"
    assert len(handoff_calls) == 1
    assert recovered["post_publication_handoff_identity_digest"] == "d" * 64


def test_git_commit_bot_rejects_certificate_drift_before_staging(monkeypatch, tmp_path):
    import bot_artifact
    import evolution_infra

    candidate = _native_bot(tmp_path / "bots" / "national_v143")
    git_calls = []
    monkeypatch.setattr(evolution_infra, "_git_ensure_main_branch", lambda: None)
    monkeypatch.setattr(evolution_infra, "_require_national_epoch_registry_for_commit", lambda: None)
    monkeypatch.setattr(evolution_infra, "get_bot_dir", lambda _version: candidate)
    monkeypatch.setattr(evolution_infra, "_git", lambda *args, **_kwargs: git_calls.append(args) or "")
    monkeypatch.setattr(bot_artifact, "hash_path", lambda _path: "changed-hash")

    with __import__("pytest").raises(RuntimeError, match="changed after official certification"):
        evolution_infra.git_commit_bot(
            143,
            142,
            "test",
            official_certificate={
                "certificate_digest": "cert-digest",
                "candidate_hash": "certified-hash",
                "policy_id": "official-full-v5",
            },
        )

    assert not any(call and call[0] in {"add", "commit", "tag"} for call in git_calls)


def test_git_commit_bot_rejects_certificate_drift_while_staging(monkeypatch, tmp_path):
    import bot_artifact
    import evolution_infra

    target_v = STRICT_TARGET_V
    source_v = STRICT_SOURCE_V
    candidate = _native_bot(tmp_path / "bots" / bot_name(target_v))
    bot_rel = bot_relpath(target_v)
    cert_rel = f"official_certificates/{bot_name(target_v)}.json"
    git_calls = []
    staged = []

    def fake_git(*args, **_kwargs):
        git_calls.append(args)
        if args == ("diff", "--cached", "--name-only"):
            return "\n".join(staged)
        if args == (
            "add",
            "--",
            bot_rel,
            cert_rel,
        ):
            staged.extend([
                f"{bot_rel}/national_bot.py",
                cert_rel,
            ])
        if args[:4] == ("restore", "--staged", "--", bot_rel):
            staged.clear()
        return ""

    hashes = iter(["certified-hash", "changed-hash"])
    monkeypatch.setattr(evolution_infra, "_git_ensure_main_branch", lambda: None)
    monkeypatch.setattr(evolution_infra, "_require_national_epoch_registry_for_commit", lambda: None)
    monkeypatch.setattr(evolution_infra, "get_bot_dir", lambda _version: candidate)
    monkeypatch.setattr(evolution_infra, "_git", fake_git)
    monkeypatch.setattr(bot_artifact, "hash_path", lambda _path: next(hashes))
    monkeypatch.setattr(
        "official_certification.publish_certificate_attestation",
        lambda *_a, **_k: {
            "certificate_digest": "cert-digest",
            "relative_path": cert_rel,
        },
    )

    with __import__("pytest").raises(RuntimeError, match="changed while staging"):
        evolution_infra.git_commit_bot(
            target_v,
            source_v,
            "test",
            official_certificate={
                "certificate_digest": "cert-digest",
                "candidate_hash": "certified-hash",
                "policy_id": "official-full-v5",
            },
        )

    assert (
        "restore",
        "--staged",
        "--",
        bot_rel,
        cert_rel,
    ) in git_calls
    assert not any(call and call[0] in {"commit", "tag"} for call in git_calls)
