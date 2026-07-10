import asyncio
from pathlib import Path

from official_certification import STATUS_CERTIFIED, STATUS_INCONCLUSIVE


def _native_bot(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "national_bot.py").write_text("import socket\n# native tcp entry\n", encoding="utf-8")
    return path


def test_official_full_commit_gate_requires_full_spec(tmp_path, monkeypatch):
    import official_certification
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

    def fake_run_certification(spec, **kwargs):
        calls.append((spec, kwargs))
        return {
            "status": STATUS_CERTIFIED,
            "mode": "full",
            "issues": [],
            "cache_hit": False,
            "official_evidence_path": str(tmp_path / "evidence.json"),
            "official_evidence_summary": {"classification": "pass", "blocking": False},
        }

    monkeypatch.setattr(official_certification, "run_certification", fake_run_certification)

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
    assert kwargs["queue_on_busy"] is False
    assert result["opponent_selection"]["opponent"]["reason"] == "content_bound_grandfather_grant"


def test_official_full_commit_gate_blocks_inconclusive_result(tmp_path, monkeypatch):
    import official_certification
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

    def fake_run_certification(_spec, **_kwargs):
        return {
            "status": STATUS_INCONCLUSIVE,
            "mode": "full",
            "issues": ["thp_incomplete_for_full_certification: hands=69 target=70"],
            "official_evidence_path": str(tmp_path / "evidence.json"),
            "official_evidence_summary": {"classification": "inconclusive", "blocking": False},
        }

    monkeypatch.setattr(official_certification, "run_certification", fake_run_certification)

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
    assert result["status"]["status"] == STATUS_INCONCLUSIVE
    assert result["issues"] == ["thp_incomplete_for_full_certification: hands=69 target=70"]


def test_official_full_commit_gate_skips_non_native_workflow(monkeypatch, tmp_path):
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

    assert result["passed"] is True
    assert result["skipped"] is True
    assert result["reason"] == "non_native_tcp_workflow"


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
            "policy_id": "official-full-v2",
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
