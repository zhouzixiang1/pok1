import fcntl
import json
from pathlib import Path

import pytest

from official_platform_harness import OfficialPlatformConfig
from official_certification import (
    STATUS_COMPLIANCE_PASS,
    STATUS_CERTIFIED,
    STATUS_FAILED,
    STATUS_INCONCLUSIVE,
    STATUS_PENDING,
    STATUS_SMOKE_PASS,
    STATUS_UNCERTIFIED,
    build_spec,
    cache_key,
    certificate_validation,
    enqueue_certification,
    official_feedback_summary,
    official_full_certified,
    official_compliance_verdict,
    official_failure_blocks_parent,
    official_opponent_eligibility,
    process_certification_queue,
    queue_snapshot,
    record_grandfathered,
    record_local_pass,
    report_validation_issues,
    report_valid_for_spec,
    run_certification,
    read_status,
    select_official_opponent,
    write_status,
)


@pytest.fixture(autouse=True)
def _disable_live_official_llm(monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_LLM_ANALYSIS", "0")


class FakeResult:
    def __init__(self, payload):
        self.payload = payload

    def model_dump(self):
        return self.payload


def _bot(path: Path, body: str = "def act():\n    return 0\n") -> Path:
    path.mkdir(parents=True)
    (path / "main.py").write_text(body, encoding="utf-8")
    (path / "national_bot.py").write_text("import socket\n# raise fold call check allin sock.recv _split_messages\n", encoding="utf-8")
    return path


def _config(tmp_path: Path) -> OfficialPlatformConfig:
    exe = tmp_path / "platform.exe"
    exe.write_bytes(b"fake-exe")
    wine = tmp_path / "wine"
    wine.mkdir()
    return OfficialPlatformConfig(
        exe_path=exe,
        wineprefix=wine,
        results_dir=tmp_path / "official",
        lock_path=tmp_path / "official.lock",
    )


def _report(*, target_hands: int, rounds: int, passed=True, issues=None, thp_hands=None):
    receipts = []
    for idx in range(rounds):
        receipts.append({
            "passed": passed,
            "issues": issues or [],
            "target_hands": target_hands,
            "artifacts": {
                "thp_summaries": [{"hand_records": target_hands if thp_hands is None else thp_hands}],
            },
        })
    return {
        "passed": passed,
        "issues": issues or [],
        "report": {
            "summary": {"suite_dir": "/tmp/suite", "rounds_run": rounds},
            "rounds": receipts,
        },
    }


def _full_report(
    tmp_path: Path,
    candidate: Path,
    opponent: Path,
    *,
    passed: bool = True,
    issues=None,
    thp_hands: int = 70,
):
    suite = tmp_path / "full-suite"
    receipts = []
    for kind, count in (("self_play", 5), ("opponent", 3)):
        for round_index in range(1, count + 1):
            round_dir = suite / f"{kind}_{round_index:02d}"
            round_dir.mkdir(parents=True, exist_ok=True)
            wire_events = round_dir / "wire_events.jsonl"
            replay_summary = round_dir / "replay_summary.json"
            wire_events.write_text("{}\n", encoding="utf-8")
            replay_summary.write_text(
                json.dumps({"events_seen": 1, "issues": [], "warnings": []}),
                encoding="utf-8",
            )
            artifact_paths = {}
            for artifact_name in (
                "receipt",
                "platform_log",
                "bot_a_log",
                "bot_b_log",
                "bot_a_stdout",
                "bot_a_stderr",
                "bot_b_stdout",
                "bot_b_stderr",
            ):
                suffix = ".json" if artifact_name == "receipt" else ".log"
                artifact_path = round_dir / f"{artifact_name}{suffix}"
                artifact_path.write_text("{}\n", encoding="utf-8")
                artifact_paths[artifact_name] = str(artifact_path)
            thp_path = round_dir / "match.txt"
            screenshot_path = round_dir / "platform.png"
            thp_path.write_text("STATE:0:::", encoding="gb2312")
            screenshot_path.write_bytes(b"fake-png")
            bot_b_path = candidate if kind == "self_play" else opponent
            receipts.append({
                "round_id": f"{kind}_{round_index:02d}",
                "round_kind": kind,
                "round_index": round_index,
                "passed": passed,
                "issues": issues or [],
                "target_hands": 70,
                "bot_a": {"path": str(candidate)},
                "bot_b": {"path": str(bot_b_path)},
                "wire_probe": {"enabled": True, "issues": []},
                "artifacts": {
                    "round_dir": str(round_dir),
                    **artifact_paths,
                    "wire_events": str(wire_events),
                    "replay_summary": str(replay_summary),
                    "thp_files": [str(thp_path)],
                    "screenshots": [str(screenshot_path)],
                    "thp_summaries": [{"hand_records": thp_hands}],
                },
            })
    return {
        "passed": passed,
        "issues": issues or [],
        "report": {
            "summary": {"suite_dir": str(suite), "rounds_run": 8},
            "rounds": receipts,
        },
    }


def _smoke_report_without_thp(*, target_hands: int = 10, rounds: int = 2):
    receipts = []
    for _idx in range(rounds):
        receipts.append({
            "passed": True,
            "issues": [],
            "target_hands": target_hands,
            "log_summary": {
                "hands_started_min": target_hands,
                "settlements_min": target_hands - 1,
            },
            "artifacts": {
                "thp_summaries": [],
            },
        })
    return {
        "passed": True,
        "issues": [],
        "report": {
            "summary": {"suite_dir": "/tmp/suite", "rounds_run": rounds},
            "rounds": receipts,
        },
    }


def _write_jsonl(path: Path, rows):
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


def _smoke_report_with_wire_replay_blocker(tmp_path: Path):
    suite = tmp_path / "suite"
    bad_round = suite / "self_play_01"
    good_round = suite / "opponent_01"
    bad_round.mkdir(parents=True)
    good_round.mkdir(parents=True)
    wire_events = bad_round / "wire_events.jsonl"
    _write_jsonl(
        wire_events,
        [
            {
                "t": 1.0,
                "dt": 0.1,
                "conn": "A",
                "direction": "server_to_bot",
                "messages": ["preflop|SMALLBLIND|<0,3><1,4>"],
            },
            {
                "t": 1.1,
                "dt": 0.2,
                "conn": "A",
                "direction": "bot_to_server",
                "messages": ["check"],
            },
        ],
    )
    receipts = [
        {
            "round_id": "self_play_01",
            "round_kind": "self_play",
            "passed": True,
            "issues": [],
            "target_hands": 10,
            "log_summary": {"hands_started_min": 10, "settlements_min": 10, "issues": []},
            "artifacts": {
                "round_dir": str(bad_round),
                "wire_events": str(wire_events),
                "thp_summaries": [],
            },
        },
        {
            "round_id": "opponent_01",
            "round_kind": "opponent",
            "passed": True,
            "issues": [],
            "target_hands": 10,
            "log_summary": {"hands_started_min": 10, "settlements_min": 10, "issues": []},
            "artifacts": {"round_dir": str(good_round), "thp_summaries": []},
        },
    ]
    return {
        "passed": True,
        "issues": [],
        "report": {
            "summary": {"suite_dir": str(suite), "rounds_run": 2},
            "rounds": receipts,
        },
    }


def test_cache_key_changes_when_inputs_change(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    cfg = _config(tmp_path)

    spec = build_spec("smoke", candidate, opponent=opponent)
    first = cache_key(spec, cfg)
    (candidate / "main.py").write_text("def act():\n    return 1\n", encoding="utf-8")
    changed_candidate = cache_key(spec, cfg)
    changed_mode = cache_key(build_spec("full", candidate, opponent=opponent), cfg)

    assert first != changed_candidate
    assert changed_candidate != changed_mode


def test_full_profile_cannot_be_downgraded_or_run_without_opponent(tmp_path):
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")

    with pytest.raises(ValueError, match="profile is immutable"):
        build_spec("full", candidate, opponent=opponent, self_play_rounds=0)
    with pytest.raises(ValueError, match="profile is immutable"):
        build_spec("full", candidate, opponent=opponent, target_hands=1)
    with pytest.raises(ValueError, match="requires an opponent"):
        build_spec("full", candidate)


def test_full_report_requires_round_identity_and_wire_artifacts(tmp_path):
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    spec = build_spec("full", candidate, opponent=opponent)
    report = _full_report(tmp_path, candidate, opponent)
    report["report"]["rounds"][0]["round_kind"] = "opponent"
    report["report"]["rounds"][1].pop("wire_probe")

    issues = report_validation_issues(report, spec)

    assert any("round_kind_mismatch" in issue for issue in issues)
    assert any("full_wire_probe_missing_or_disabled" in issue for issue in issues)


def test_candidate_change_during_certification_is_inconclusive(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    cfg = _config(tmp_path)
    spec = build_spec("compliance", candidate, opponent=opponent)

    def mutating_runner(*_args, **_kwargs):
        (candidate / "main.py").write_text("def act():\n    return -1\n", encoding="utf-8")
        return FakeResult(_report(target_hands=10, rounds=2))

    result = run_certification(
        spec,
        config=cfg,
        runner=mutating_runner,
        queue_on_busy=False,
    )

    assert result["status"] == STATUS_INCONCLUSIVE
    assert "candidate_changed_during_official_certification" in result["issues"]


def test_smoke_receipt_cannot_satisfy_full_certification(tmp_path):
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    smoke = build_spec("smoke", candidate, opponent=opponent)
    full = build_spec("full", candidate, opponent=opponent)
    payload = _report(target_hands=10, rounds=2)

    assert report_valid_for_spec(payload, smoke) is True
    assert report_valid_for_spec(payload, full) is False


def test_compliance_mode_requires_two_short_protocol_rounds(tmp_path):
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    compliance = build_spec("compliance", candidate, opponent=opponent)

    assert compliance.self_play_rounds == 1
    assert compliance.opponent_rounds == 1
    assert compliance.target_hands == 10
    assert report_valid_for_spec(_report(target_hands=10, rounds=2), compliance) is True
    assert report_valid_for_spec(_report(target_hands=70, rounds=2), compliance) is False


def test_bad_receipts_are_not_valid_for_cache(tmp_path):
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    spec = build_spec("full", candidate, opponent=opponent)

    assert report_valid_for_spec(_report(target_hands=70, rounds=8, issues=["illegal"]), spec) is False
    assert report_valid_for_spec(_report(target_hands=70, rounds=8, thp_hands=69), spec) is False


def test_official_silent_timeout_gap_blocks_parent_selection():
    status = {
        "status": STATUS_FAILED,
        "issues": ["opponent_1: official_log_silent_timeout_gap: bot_a max_gap_sec=62 max_decision_sec=0.050"],
    }

    verdict = official_compliance_verdict(status)

    assert verdict["blocking"] is True
    assert verdict["classification"] == "protocol_violation"
    assert official_failure_blocks_parent(status) is True


def test_short_smoke_can_use_log_progress_when_thp_is_absent(tmp_path):
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    smoke = build_spec("smoke", candidate, opponent=opponent)
    full = build_spec("full", candidate, opponent=opponent)
    payload = _smoke_report_without_thp(target_hands=10, rounds=2)

    assert report_valid_for_spec(payload, smoke) is True
    assert report_valid_for_spec(payload, full) is False
    assert any("round_count_mismatch" in issue for issue in report_validation_issues(payload, full))


def test_full_certification_requires_thp_records(tmp_path):
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    spec = build_spec("full", candidate, opponent=opponent)
    payload = _smoke_report_without_thp(target_hands=70, rounds=8)

    assert report_valid_for_spec(payload, spec) is False
    assert any("thp_incomplete_for_full_certification" in issue for issue in report_validation_issues(payload, spec))


def test_run_certification_uses_valid_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    cfg = _config(tmp_path)
    spec = build_spec("smoke", candidate, opponent=opponent)

    def runner(*_args, **_kwargs):
        return FakeResult(_report(target_hands=10, rounds=2))

    first = run_certification(spec, config=cfg, runner=runner, queue_on_busy=False)
    second = run_certification(
        spec,
        config=cfg,
        runner=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("cache miss")),
        queue_on_busy=False,
    )

    assert first["status"] == STATUS_SMOKE_PASS
    assert second["status"] == STATUS_SMOKE_PASS
    assert second["cache_hit"] is True


def test_full_certification_requires_successful_llm_analysis(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    cfg = _config(tmp_path)
    spec = build_spec("full", candidate, opponent=opponent)

    result = run_certification(
        spec,
        config=cfg,
        runner=lambda *_args, **_kwargs: FakeResult(_full_report(tmp_path, candidate, opponent)),
        queue_on_busy=False,
    )

    assert result["status"] == STATUS_INCONCLUSIVE
    assert any("official_full_llm_analysis_incomplete" in issue for issue in result["issues"])
    assert official_full_certified(result, candidate, config=cfg) is False


def test_full_certification_persists_llm_repair_feedback(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    monkeypatch.setenv("POK_OFFICIAL_LLM_ANALYSIS", "1")
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    cfg = _config(tmp_path)
    spec = build_spec("full", candidate, opponent=opponent)

    def fake_llm(evidence, *, output_path=None, **_kwargs):
        payload = {
            "analysis_source": "llm",
            "compliance_verdict": "pass",
            "failure_class": "none",
            "blocking": False,
            "confidence": 0.91,
            "repair_guidance": "Keep pending-action validation before every send.",
            "prompt_feedback": "Require wire send checks in worker tasks.",
            "strength_evaluation": "not_applicable",
        }
        if output_path:
            Path(output_path).write_text(json.dumps(payload), encoding="utf-8")
        return payload

    monkeypatch.setattr("official_llm_analysis.run_official_llm_analysis_sync", fake_llm)

    result = run_certification(
        spec,
        config=cfg,
        runner=lambda *args, **kwargs: FakeResult(_full_report(tmp_path, candidate, opponent)),
    )
    feedback = official_feedback_summary()

    assert result["status"] == STATUS_CERTIFIED
    assert official_full_certified(result, candidate, config=cfg) is True
    assert Path(result["certificate_path"]).is_file()
    assert result["official_llm_repair_guidance"] == "Keep pending-action validation before every send."
    assert result["official_llm_prompt_feedback"] == "Require wire send checks in worker tasks."
    assert "compliance-only" in feedback
    assert "pending-action validation" in feedback

    analysis_path = Path(result["official_llm_analysis_path"])
    analysis_bytes = analysis_path.read_bytes()
    analysis_path.unlink()
    validation = certificate_validation(result, candidate=candidate, config=cfg)
    assert validation["valid"] is False
    assert "certificate_llm_analysis_missing" in validation["issues"]
    analysis_path.write_bytes(analysis_bytes)

    evidence_path = Path(result["official_evidence_path"])
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    retained_path = Path(evidence["rounds"][0]["artifacts"]["wire_events"]["path"])
    retained_bytes = retained_path.read_bytes()
    retained_path.unlink()
    validation = certificate_validation(result, candidate=candidate, config=cfg)
    assert validation["valid"] is False
    assert any(
        issue.startswith("certificate_retained_artifact_") and "wire_events" in issue
        for issue in validation["issues"]
    )
    retained_path.write_bytes(retained_bytes)

    evidence_bytes = evidence_path.read_bytes()
    evidence_path.unlink()
    validation = certificate_validation(result, candidate=candidate, config=cfg)
    assert validation["valid"] is False
    assert "certificate_evidence_missing" in validation["issues"]
    evidence_path.write_bytes(evidence_bytes)

    (candidate / "main.py").write_text("def act():\n    return -1\n", encoding="utf-8")
    validation = certificate_validation(result, candidate=candidate, config=cfg)
    assert validation["valid"] is False
    assert "certificate_identity_stale" in validation["issues"]


def test_compliance_certification_has_distinct_status(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    cfg = _config(tmp_path)
    spec = build_spec("compliance", candidate, opponent=opponent)

    result = run_certification(
        spec,
        config=cfg,
        runner=lambda *_args, **_kwargs: FakeResult(_report(target_hands=10, rounds=2)),
        queue_on_busy=False,
    )

    assert result["status"] == STATUS_COMPLIANCE_PASS
    assert result["mode"] == "compliance"


def test_run_certification_writes_official_evidence_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    cfg = _config(tmp_path)
    spec = build_spec("smoke", candidate, opponent=opponent)

    result = run_certification(
        spec,
        config=cfg,
        runner=lambda *_args, **_kwargs: FakeResult(_report(target_hands=10, rounds=2)),
        queue_on_busy=False,
    )

    evidence_path = Path(result["official_evidence_path"])
    assert evidence_path.exists()
    assert result["official_evidence_summary"]["classification"] == "pass"
    assert result["official_evidence_summary"]["blocking"] is False
    assert result["official_evidence_summary"]["strength_evaluation"] == "not_applicable"
    analysis_path = Path(result["official_llm_analysis_path"])
    assert analysis_path.exists()
    analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
    assert analysis["analysis_source"] == "default"
    assert analysis["notes"] == ["llm_disabled"]
    assert result["official_llm_analysis_summary"]["strength_evaluation"] == "not_applicable"


def test_official_verdict_prefers_evidence_summary_blocking_over_status_value():
    status = {
        "status": STATUS_CERTIFIED,
        "mode": "full",
        "issues": [],
        "official_evidence_summary": {
            "classification": "communication",
            "blocking": True,
            "inconclusive": False,
            "violation": True,
        },
    }

    verdict = official_compliance_verdict(status)

    assert verdict["ok"] is False
    assert verdict["blocking"] is True
    assert verdict["classification"] == "communication"
    assert official_full_certified(status) is False


def test_run_certification_evidence_blocking_overrides_raw_pass(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    cfg = _config(tmp_path)
    spec = build_spec("smoke", candidate, opponent=opponent)

    result = run_certification(
        spec,
        config=cfg,
        runner=lambda *_args, **_kwargs: FakeResult(_smoke_report_with_wire_replay_blocker(tmp_path)),
        queue_on_busy=False,
    )

    assert result["status"] == STATUS_FAILED
    assert result["official_evidence_summary"]["blocking"] is True
    assert result["official_evidence_summary"]["classification"] == "protocol"
    assert any("wire_replay: illegal_check" in issue for issue in result["issues"])
    assert official_failure_blocks_parent(result)


def test_run_certification_evidence_error_is_inconclusive_not_certified(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    cfg = _config(tmp_path)
    spec = build_spec("full", candidate, opponent=opponent)

    def boom(*_args, **_kwargs):
        raise RuntimeError("evidence disk failure")

    monkeypatch.setattr("official_certification.build_official_evidence_bundle", boom)

    result = run_certification(
        spec,
        config=cfg,
        runner=lambda *_args, **_kwargs: FakeResult(_report(target_hands=70, rounds=8)),
        queue_on_busy=False,
    )

    assert result["status"] == STATUS_INCONCLUSIVE
    assert official_full_certified(result) is False
    assert any("official_evidence_error" in issue for issue in result["issues"])


def test_run_certification_optional_llm_analysis_is_advisory(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    monkeypatch.setenv("POK_OFFICIAL_LLM_ANALYSIS", "1")
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    cfg = _config(tmp_path)
    spec = build_spec("smoke", candidate, opponent=opponent)

    def fake_llm_analysis(evidence, *, output_path=None, **_kwargs):
        payload = {
            "compliance_verdict": "pass",
            "failure_class": "none",
            "blocking": False,
            "confidence": 0.51,
            "strength_evaluation": "not_applicable",
        }
        if output_path:
            Path(output_path).write_text(json.dumps(payload), encoding="utf-8")
        return payload

    import official_llm_analysis

    monkeypatch.setattr(official_llm_analysis, "run_official_llm_analysis_sync", fake_llm_analysis)

    result = run_certification(
        spec,
        config=cfg,
        runner=lambda *_args, **_kwargs: FakeResult(_report(target_hands=10, rounds=2)),
        queue_on_busy=False,
    )

    assert result["status"] == STATUS_SMOKE_PASS
    assert Path(result["official_llm_analysis_path"]).exists()
    assert result["official_llm_analysis_summary"]["compliance_verdict"] == "pass"
    assert result["official_llm_analysis_summary"]["strength_evaluation"] == "not_applicable"


def test_run_certification_runs_llm_analysis_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    monkeypatch.delenv("POK_OFFICIAL_LLM_ANALYSIS", raising=False)
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    cfg = _config(tmp_path)
    spec = build_spec("smoke", candidate, opponent=opponent)
    calls = {"count": 0}

    def fake_llm_analysis(evidence, *, output_path=None, **_kwargs):
        calls["count"] += 1
        payload = {
            "analysis_source": "llm",
            "compliance_verdict": "pass",
            "failure_class": "none",
            "blocking": False,
            "confidence": 0.88,
            "strength_evaluation": "not_applicable",
        }
        if output_path:
            Path(output_path).write_text(json.dumps(payload), encoding="utf-8")
        return payload

    import official_llm_analysis

    monkeypatch.setattr(official_llm_analysis, "run_official_llm_analysis_sync", fake_llm_analysis)

    result = run_certification(
        spec,
        config=cfg,
        runner=lambda *_args, **_kwargs: FakeResult(_report(target_hands=10, rounds=2)),
        queue_on_busy=False,
    )

    assert calls["count"] == 1
    assert result["status"] == STATUS_SMOKE_PASS
    assert result["official_llm_analysis_summary"]["analysis_source"] == "llm"
    assert result["official_llm_analysis_summary"]["confidence"] == 0.88


def test_inconclusive_status_includes_non_violation_validation_issues(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    cfg = _config(tmp_path)
    spec = build_spec("full", candidate, opponent=opponent)

    result = run_certification(
        spec,
        config=cfg,
        runner=lambda *_args, **_kwargs: FakeResult(_smoke_report_without_thp(target_hands=70, rounds=8)),
        queue_on_busy=False,
    )

    assert result["status"] == STATUS_INCONCLUSIVE
    assert result["issues"]
    assert any("thp_incomplete_for_full_certification" in issue for issue in result["issues"])
    assert result["official_evidence_summary"]["classification"] == "harness"
    assert result["official_evidence_summary"]["inconclusive"] is True


def test_full_round_incomplete_after_progress_is_blocking_official_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    cfg = _config(tmp_path)
    spec = build_spec("full", candidate, opponent=opponent)
    receipt = {
        "passed": False,
        "issues": ["BotA_exited_early: rc=0", "thp_missing_for_full_70_hand_round"],
        "target_hands": 70,
        "log_summary": {
            "hands_started_min": 33,
            "settlements_min": 32,
            "bot_a": {"net_chips": -19466},
            "bot_b": {"net_chips": 19466},
            "issues": [],
        },
        "artifacts": {"thp_summaries": []},
    }
    report = {
        "passed": False,
        "issues": [],
        "report": {
            "summary": {"suite_dir": str(tmp_path / "suite"), "rounds_run": 8},
            "rounds": [receipt for _ in range(8)],
        },
    }

    result = run_certification(
        spec,
        config=cfg,
        runner=lambda *_args, **_kwargs: FakeResult(report),
        queue_on_busy=False,
    )
    verdict = official_compliance_verdict(result)

    assert result["status"] == STATUS_FAILED
    assert verdict["blocking"] is True
    assert verdict["classification"] == "official_full_incomplete"
    assert result["official_evidence_summary"]["classification"] == "obvious_decision_error"
    assert result["official_evidence_summary"]["blocking"] is True
    assert any("official_full_round_incomplete_after_progress" in issue for issue in result["issues"])


def test_protocol_violation_result_uses_failed_status(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    cfg = _config(tmp_path)
    spec = build_spec("smoke", candidate, opponent=opponent)

    result = run_certification(
        spec,
        config=cfg,
        runner=lambda *_args, **_kwargs: FakeResult(
            _report(
                target_hands=10,
                rounds=2,
                passed=False,
                issues=["self_play_1: protocol_raise_format: msg='raise  200'"],
            )
        ),
        queue_on_busy=False,
    )

    assert result["status"] == STATUS_FAILED
    assert official_failure_blocks_parent(result)
    assert official_compliance_verdict(result)["classification"] == "protocol_violation"


def test_record_local_pass_does_not_clear_failed_status(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v1")

    write_status(candidate, STATUS_FAILED, mode="smoke", issues=["protocol_raise_format"])
    result = record_local_pass(candidate)

    assert result["status"] == STATUS_FAILED
    assert result["issues"] == ["protocol_raise_format"]


def test_read_status_without_file_is_uncertified(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v1")

    result = read_status(candidate)
    verdict = official_compliance_verdict(result)

    assert result["status"] == STATUS_UNCERTIFIED
    assert result["updated_at"] is None
    assert verdict["classification"] == "uncertified"
    assert verdict["blocking"] is False


def test_record_local_pass_writes_local_status_for_uncertified(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v1")

    result = record_local_pass(candidate)
    verdict = official_compliance_verdict(result)

    assert result["status"] == "local-pass"
    assert result["issues"] == []
    assert verdict["classification"] == "local_pass"


def test_mutable_grandfather_status_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v70")

    with pytest.raises(RuntimeError, match="official_grandfathering.json"):
        record_grandfathered(candidate, reason="bootstrap active pool", source="test")


def test_official_opponent_eligibility_rejects_untracked_bootstrap_and_blocking_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    monkeypatch.setattr(
        "official_certification.epoch_lifecycle_eligibility",
        lambda version: {"eligible": True, "reason": "national_epoch_active", "version": version},
    )
    historical = _bot(tmp_path / "national_v70")

    bootstrap = official_opponent_eligibility(historical)
    assert bootstrap["eligible"] is False
    assert bootstrap["reason"] in {"not_published_artifact", "no_grandfather_grant"}

    write_status(historical, STATUS_FAILED, mode="smoke", issues=["protocol_raise_format"])
    failed = official_opponent_eligibility(historical)
    assert failed["eligible"] is False
    assert failed["reason"] == "blocking_official_failure"


def test_select_official_opponent_prefers_certified_over_content_bound_grant(tmp_path, monkeypatch):
    import evolution_infra
    import national_native
    import official_certification

    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v134")
    bootstrap = _bot(tmp_path / "national_v70")
    certified = _bot(tmp_path / "national_v120")
    (bootstrap / ".completed").touch()
    (certified / ".completed").touch()
    monkeypatch.setattr(evolution_infra, "load_reaped_bot_versions", lambda: set())
    monkeypatch.setattr(national_native, "check_native_contract", lambda _path: [])
    monkeypatch.setattr(
        official_certification,
        "published_bot_identity",
        lambda path: {
            "published": True,
            "artifact_hash": f"hash-{Path(path).name}",
            "tag": f"tag-{Path(path).name}",
            "tag_object": f"tag-object-{Path(path).name}",
            "issues": [],
        },
    )
    monkeypatch.setattr(
        official_certification,
        "official_opponent_eligibility",
        lambda path, **_kwargs: {
            "eligible": True,
            "reason": "official_certified" if Path(path).name == "national_v120" else "content_bound_grandfather_grant",
            "priority": 0 if Path(path).name == "national_v120" else 1,
        },
    )

    result = select_official_opponent(
        candidate,
        [str(bootstrap), str(certified)],
        allow_bootstrap_grandfather=False,
    )

    assert result["selected"] is True
    assert result["opponent"]["bot"] == "national_v120"
    assert result["opponent"]["reason"] == "official_certified"


def test_record_local_pass_preserves_inconclusive_official_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v1")

    write_status(candidate, STATUS_INCONCLUSIVE, mode="smoke", issues=["self_play_1: port_busy_before_start: 127.0.0.1:10001"])
    result = record_local_pass(candidate)

    assert result["status"] == STATUS_INCONCLUSIVE
    assert result["issues"] == ["self_play_1: port_busy_before_start: 127.0.0.1:10001"]


def test_only_protocol_official_failures_block_parent_selection():
    protocol_failure = {
        "status": STATUS_FAILED,
        "issues": ["self_play_1: protocol_raise_format: msg='raise  200'"],
    }
    infra_failure = {
        "status": STATUS_INCONCLUSIVE,
        "issues": ["self_play_1: port_busy_before_start: 127.0.0.1:10001"],
    }
    legacy_infra_failure = {
        "status": STATUS_FAILED,
        "issues": ["official_acceptance_suite_exception: FileNotFoundError: wine"],
    }
    empty_failure = {"status": STATUS_INCONCLUSIVE, "issues": []}

    assert official_failure_blocks_parent(protocol_failure)
    assert official_compliance_verdict(protocol_failure)["classification"] == "protocol_violation"
    assert not official_failure_blocks_parent(infra_failure)
    assert official_compliance_verdict(infra_failure)["classification"] == "inconclusive"
    assert not official_failure_blocks_parent(legacy_infra_failure)
    assert official_compliance_verdict(legacy_infra_failure)["classification"] == "inconclusive"
    assert not official_failure_blocks_parent(empty_failure)
    assert official_compliance_verdict(empty_failure)["inconclusive"] is True


def test_smoke_enqueue_does_not_downgrade_certified_status(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")

    write_status(candidate, STATUS_CERTIFIED, mode="full", cache_key="full-key", issues=[])
    result = enqueue_certification(build_spec("smoke", candidate, opponent=opponent), reason="manual_smoke")

    assert result["status"] == STATUS_CERTIFIED
    assert queue_snapshot()["pending"] == 0


def test_process_certification_queue_consumes_pending_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    cfg = _config(tmp_path)
    spec = build_spec("smoke", candidate, opponent=opponent)

    enqueue_certification(spec, reason="test")
    assert queue_snapshot()["pending"] == 1

    result = process_certification_queue(
        config=cfg,
        runner=lambda *_args, **_kwargs: FakeResult(_report(target_hands=10, rounds=2)),
    )

    assert result["processed"] == 1
    assert result["remaining"] == 0
    assert result["results"][0]["status"] == STATUS_SMOKE_PASS
    assert queue_snapshot()["pending"] == 0


def test_process_certification_queue_respects_official_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    cfg = _config(tmp_path)
    spec = build_spec("smoke", candidate, opponent=opponent)
    enqueue_certification(spec, reason="test")
    cfg.lock_path.touch()

    with cfg.lock_path.open("r+", encoding="utf-8") as lock_fp:
        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            result = process_certification_queue(
                config=cfg,
                runner=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("should not run")),
            )
        finally:
            fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)

    assert result["processed"] == 0
    assert result["lock_busy"] is True
    assert queue_snapshot()["pending"] == 1


def test_official_lock_busy_queues_without_running(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    cfg = _config(tmp_path)
    spec = build_spec("smoke", candidate, opponent=opponent)
    cfg.lock_path.touch()

    with cfg.lock_path.open("r+", encoding="utf-8") as lock_fp:
        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            result = run_certification(
                spec,
                config=cfg,
                runner=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("should not run")),
                queue_on_busy=True,
            )
        finally:
            fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)

    assert result["status"] == STATUS_PENDING
    assert result["queued"] is True
    assert (tmp_path / "cert" / "queue.jsonl").exists()
