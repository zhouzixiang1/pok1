import asyncio
import json
from pathlib import Path

from official_certification import STATUS_CERTIFIED, STATUS_INCONCLUSIVE


def _native_bot(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "national_bot.py").write_text("import socket\n# native tcp entry\n", encoding="utf-8")
    return path


def test_official_full_commit_gate_requires_full_spec(tmp_path, monkeypatch):
    import official_certification
    import official_certification_job
    import tool_commit

    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _native_bot(tmp_path / "bots" / "national_v134")
    opponent = _native_bot(tmp_path / "bots" / "national_v70")
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
                "reason": "content_bound_grandfather_grant",
            },
            "considered": [],
        },
    )
    monkeypatch.setattr(
        official_certification,
        "official_full_certified",
        lambda _status, _candidate, **_kwargs: True,
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
            134,
            123,
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
    assert kwargs["source_v"] == 123
    assert kwargs["retry_terminal"] is False
    assert result["opponent_selection"]["opponent"]["reason"] == "content_bound_grandfather_grant"


def test_official_full_commit_gate_blocks_inconclusive_result(tmp_path, monkeypatch):
    import official_certification
    import official_certification_job
    import tool_commit

    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _native_bot(tmp_path / "bots" / "national_v134")
    opponent = _native_bot(tmp_path / "bots" / "national_v70")
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
                "reason": "content_bound_grandfather_grant",
            },
            "considered": [],
        },
    )
    monkeypatch.setattr(
        official_certification,
        "official_full_certified",
        lambda _status, _candidate, **_kwargs: False,
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
            134,
            123,
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
        {"next_v": 134, "source_v": 120, "stage": "verified"},
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
        {"next_v": 134, "source_v": 120, "stage": "verified"},
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
        {"next_v": 134, "source_v": 120, "stage": "official_certifying"},
        {
            "passed": False,
            "verdict": {"blocking": True, "classification": "protocol_violation"},
            "issues": ["illegal_check"],
        },
    )

    assert stage == ""


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
            "policy_id": "official-full-v4",
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


def test_commit_bot_never_invokes_git_when_official_gate_fails(monkeypatch, tmp_path):
    import tool_commit

    candidate = _native_bot(tmp_path / "bots" / "national_v143")
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
        "git_commit_bot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("git_commit_bot must not run after official gate failure")
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


def test_commit_bot_attaches_pending_official_job_without_git(monkeypatch, tmp_path):
    import tool_commit

    candidate = _native_bot(tmp_path / "bots" / "national_v143")
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
            "policy_id": "official-full-v4",
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
        "git_commit_bot",
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
    import official_certification
    import tool_commit

    candidate = _native_bot(tmp_path / "bots" / "national_v143")
    checkpoint = {
        "next_v": 143,
        "source_v": 142,
        "stage": "verified",
        "gate_results": {},
    }
    status = {
        "status": STATUS_CERTIFIED,
        "mode": "full",
        "policy_id": "official-full-v4",
        "certificate_digest": "cert-digest",
        "certification_identity": {"candidate_hash": "candidate-hash"},
    }
    tag_state = {"exists": False}
    checkpoint_cleared = []
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
            "checkpoint_stage": "verified",
        },
    )
    monkeypatch.setattr(
        tool_commit,
        "_run_official_full_commit_gate",
        lambda *_args, **_kwargs: asyncio.sleep(
            0,
            result={"passed": True, "status": status},
        ),
    )
    monkeypatch.setattr(
        tool_commit,
        "_record_official_full_pass_checkpoint",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(official_certification, "official_full_certified", lambda *_a, **_k: True)
    monkeypatch.setattr(tool_commit, "load_ratings", lambda: {})
    monkeypatch.setattr(tool_commit, "compute_h2h_avg_winrate", lambda *_a, **_k: None)
    monkeypatch.setattr(tool_commit, "git_has_tag", lambda _v: tag_state["exists"])

    def fake_commit(*_args, **_kwargs):
        tag_state["exists"] = True
        return False

    monkeypatch.setattr(tool_commit, "git_commit_bot", fake_commit)
    monkeypatch.setattr(tool_commit, "evolution_git_push_required", lambda: True)
    monkeypatch.setattr(tool_commit, "log_system_event", lambda *_a, **_k: None)
    monkeypatch.setattr(tool_commit, "clear_pipeline_checkpoint", lambda: checkpoint_cleared.append(True))

    result = asyncio.run(
        tool_commit.commit_bot.handler({
            "version": 143,
            "source_v": 142,
            "strategy": "test",
            "review_approved": True,
        })
    )
    payload = json.loads(result["content"][0]["text"])

    assert payload["committed"] is False
    assert payload["local_committed"] is True
    assert payload["checkpoint_preserved"] is True
    assert payload["completed_sentinel_written"] is False
    assert not (candidate / ".completed").exists()
    assert checkpoint_cleared == []


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
                "policy_id": "official-full-v4",
            },
        )

    assert not any(call and call[0] in {"add", "commit", "tag"} for call in git_calls)


def test_git_commit_bot_rejects_certificate_drift_while_staging(monkeypatch, tmp_path):
    import bot_artifact
    import evolution_infra

    candidate = _native_bot(tmp_path / "bots" / "national_v143")
    git_calls = []
    staged = []

    def fake_git(*args, **_kwargs):
        git_calls.append(args)
        if args == ("diff", "--cached", "--name-only"):
            return "\n".join(staged)
        if args == (
            "add",
            "--",
            "bots/national_v143",
            "official_certificates/national_v143.json",
        ):
            staged.extend([
                "bots/national_v143/national_bot.py",
                "official_certificates/national_v143.json",
            ])
        if args[:4] == ("restore", "--staged", "--", "bots/national_v143"):
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
            "relative_path": "official_certificates/national_v143.json",
        },
    )

    with __import__("pytest").raises(RuntimeError, match="changed while staging"):
        evolution_infra.git_commit_bot(
            143,
            142,
            "test",
            official_certificate={
                "certificate_digest": "cert-digest",
                "candidate_hash": "certified-hash",
                "policy_id": "official-full-v4",
            },
        )

    assert (
        "restore",
        "--staged",
        "--",
        "bots/national_v143",
        "official_certificates/national_v143.json",
    ) in git_calls
    assert not any(call and call[0] in {"commit", "tag"} for call in git_calls)
