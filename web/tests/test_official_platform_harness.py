from pathlib import Path
import hashlib
import json
import sys
from types import SimpleNamespace

import pytest

from bot_artifact import hash_path
import official_platform_harness as harness
from official_platform_harness import (
    BotLaunchConfig,
    OfficialPlatformConfig,
    OfficialWireCapture,
    build_bot_command,
    parse_bot_log,
    run_official_acceptance_sync,
    summarize_round_logs,
    _format_wire_issues,
    _collect_new_thp_files,
    _canonical_thp_evidence,
    _sent_action_issue,
    _snapshot_platform_thp_files,
    _summarize_thp_files,
    _read_issue_file,
    _target_reached,
)


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_official_required_enables_short_durable_smoke(monkeypatch):
    import tool_gates

    monkeypatch.setenv("POK_OFFICIAL_REQUIRED", "1")
    assert tool_gates._official_gate_enabled("POK_OFFICIAL_SMOKE_GATE")


def test_pokctl_defaults_official_smoke_to_durable_job():
    script = (ROOT / "pokctl.sh").read_text(encoding="utf-8")

    assert 'POK_OFFICIAL_SMOKE_GATE:=1' in script
    assert 'POK_OFFICIAL_PRECOMMIT_TARGET_HANDS:=10' in script
    assert 'POK_OFFICIAL_JOB_RECONCILER:=1' in script


def test_official_platform_cli_defaults_to_manual_70_hand_rounds():
    from scripts.official_platform_acceptance import parse_args

    args = parse_args(["--candidate", "bots/national_v1"])

    assert args.self_play_rounds == 1
    assert args.opponent_rounds == 1
    assert args.target_hands == 70


def test_official_platform_cli_writes_evidence_and_default_llm_analysis(tmp_path, monkeypatch, capsys):
    from scripts import official_platform_acceptance as cli

    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "national_bot.py").write_text("pass\n", encoding="utf-8")
    suite = tmp_path / "official" / "acceptance_fake"

    class FakeResult:
        passed = True
        issues = []

        def model_dump(self):
            summary = {
                "suite_dir": str(suite),
                "self_play_rounds": 1,
                "opponent_rounds": 0,
                "target_hands": 5,
                "rounds_requested": 1,
                "rounds_run": 1,
                "passed_rounds": 1,
                "failed_rounds": 0,
                "official_platform": True,
            }
            return {
                "passed": True,
                "issues": [],
                "summary": summary,
                "report": json.loads((suite / "summary.json").read_text(encoding="utf-8")),
            }

    def fake_acceptance(*_args, config, **_kwargs):
        suite.mkdir(parents=True)
        report = {
            "candidate": str(candidate),
            "opponent": None,
            "summary": {
                "suite_dir": str(suite),
                "rounds_requested": 1,
                "rounds_run": 1,
                "target_hands": 5,
            },
            "rounds": [
                {
                    "round_id": "self_play_01",
                    "round_kind": "self_play",
                    "round_index": 1,
                    "target_hands": 5,
                    "passed": True,
                    "issues": [],
                    "log_summary": {"hands_started_min": 5, "settlements_min": 4, "issues": []},
                    "artifacts": {"round_dir": str(suite / "self_play_01"), "thp_summaries": []},
                }
            ],
            "issues": [],
        }
        (suite / "summary.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        assert config.results_dir == tmp_path / "official"
        return FakeResult()

    monkeypatch.setattr(cli, "check_environment", lambda _config: {"ok": True, "issues": [], "warnings": []})
    monkeypatch.setattr(cli, "run_official_acceptance_sync", fake_acceptance)
    calls = {"llm": 0}

    def fake_llm(evidence, *, output_path=None, **_kwargs):
        calls["llm"] += 1
        payload = {
            "analysis_source": "llm",
            "compliance_verdict": "pass",
            "failure_class": "none",
            "blocking": False,
            "confidence": 0.9,
            "strength_evaluation": "not_applicable",
        }
        if output_path:
            Path(output_path).write_text(json.dumps(payload), encoding="utf-8")
        return payload

    monkeypatch.setattr(cli, "run_official_llm_analysis_sync", fake_llm)
    monkeypatch.delenv("POK_OFFICIAL_LLM_ANALYSIS", raising=False)

    rc = cli.main([
        "--candidate",
        str(candidate),
        "--self-play-rounds",
        "1",
        "--opponent-rounds",
        "0",
        "--target-hands",
        "5",
        "--results-dir",
        str(tmp_path / "official"),
    ])

    out = capsys.readouterr().out
    assert rc == 0
    assert "official_evidence_json=" in out
    assert "llm_official_analysis_json=" in out
    assert (suite / "official_evidence.json").exists()
    analysis = json.loads((suite / "llm_official_analysis.json").read_text(encoding="utf-8"))
    assert calls["llm"] == 1
    assert analysis["analysis_source"] == "llm"
    assert analysis["strength_evaluation"] == "not_applicable"


def test_parse_bot_log_counts_progress_and_issues(tmp_path):
    log = tmp_path / "bot.log"
    log.write_text(
        "\n".join(
            [
                "[10:00:00] DISPATCH line='name'",
                "[10:00:01] DISPATCH line='preflop|SMALLBLIND|<0,1><1,2>'",
                "[10:00:02] DECIDE done action=0 elapsed=0.250s",
                "[10:00:03] SEND name=BotA hand=1 stage=preflop msg='call'",
                "[10:00:05] DISPATCH line='earnChips 50'",
                "[10:00:09] DISPATCH line='preflop|BIGBLIND|<0,3><1,4>'",
                "[10:00:10] ERROR illegal action from strategy",
            ]
        ),
        encoding="utf-8",
    )

    stats = parse_bot_log(log)

    assert stats.preflop == 2
    assert stats.earnchips == 1
    assert stats.sends == 1
    assert stats.max_hand == 1
    assert stats.net_chips == 50
    assert stats.max_gap_sec == 4
    assert stats.max_decision_sec == 0.25
    assert len(stats.issues) == 1


def test_summarize_round_logs_flags_official_silent_timeout_gap(tmp_path):
    bot_a_log = tmp_path / "botA.log"
    bot_b_log = tmp_path / "botB.log"
    bot_a_log.write_text(
        "\n".join(
            [
                "[10:00:00] DISPATCH line='preflop|SMALLBLIND|<0,1><1,2>'",
                "[10:00:01] DECIDE done action=0 elapsed=0.050s",
                "[10:00:01] SEND name=BotA hand=1 stage=preflop msg='call'",
                "[10:01:03] DISPATCH line='earnChips 50'",
            ]
        ),
        encoding="utf-8",
    )
    bot_b_log.write_text(
        "\n".join(
            [
                "[10:00:00] DISPATCH line='preflop|BIGBLIND|<0,3><1,4>'",
                "[10:01:03] DISPATCH line='earnChips -50'",
            ]
        ),
        encoding="utf-8",
    )

    summary = summarize_round_logs(bot_a_log, bot_b_log)

    assert any("official_log_silent_timeout_gap" in issue for issue in summary["issues"])


def test_official_issue_file_ignores_benign_unknown_telemetry(tmp_path):
    stderr = tmp_path / "bot.stderr.log"
    stderr.write_text(
        "\n".join(
            [
                "OPP_OPEN_SIZING bucket=unknown avg_raise_bb=3.15 samples=1 conf=0.00",
                "BB_VS_RAISE_HIST_SIZING hist_bucket=unknown immediate_bucket=standard",
                "PREFLOP_JAM_DEFENSE decision=0 reason=standard_jam",
            ]
        ),
        encoding="utf-8",
    )

    assert _read_issue_file(stderr) == []


def test_official_issue_file_keeps_protocol_and_crash_errors(tmp_path):
    stderr = tmp_path / "bot.stderr.log"
    stderr.write_text(
        "\n".join(
            [
                "Traceback (most recent call last):",
                "RuntimeError: unknown action token 'bet'",
                "protocol error: unexpected wire message",
            ]
        ),
        encoding="utf-8",
    )

    issues = _read_issue_file(stderr)

    assert len(issues) == 3
    assert all(issue.startswith("bot.stderr.log:") for issue in issues)


def test_parse_bot_log_rejects_non_official_send_format(tmp_path):
    log = tmp_path / "bot.log"
    log.write_text(
        "\n".join(
            [
                "[10:00:01] SEND name=BotA hand=1 stage=preflop msg='raise 200'",
                "[10:00:02] SEND name=BotA hand=1 stage=preflop msg='raise  200'",
                "[10:00:03] SEND name=BotA hand=1 stage=preflop msg='bet 200'",
                "[10:00:04] SEND name=BotA hand=1 stage=preflop msg=' call'",
                "[10:00:05] SEND name_handshake name='BotA'",
            ]
        ),
        encoding="utf-8",
    )

    stats = parse_bot_log(log)

    assert stats.sends == 4
    assert stats.issues == [
        "protocol_raise_format: msg='raise  200'",
        "illegal_bet_action: msg='bet 200'",
        "protocol_action_whitespace: msg=' call'",
    ]


def test_sent_action_issue_accepts_exact_official_wire_actions():
    for message in ("call", "check", "fold", "allin", "raise 1", "raise 20000"):
        assert _sent_action_issue(message) is None


def test_build_bot_command_uses_native_launch_contract(tmp_path):
    bot_dir = tmp_path / "national_v1"
    bot_dir.mkdir()
    (bot_dir / "national_bot.py").write_text("pass\n", encoding="utf-8")

    cmd = build_bot_command(
        BotLaunchConfig(bot_dir, name="BotA", seat="upper", python="/usr/bin/python3"),
        host="127.0.0.1",
        port=10001,
        log_path=tmp_path / "botA.log",
    )

    assert cmd[:2] == ["/usr/bin/python3", str(bot_dir / "national_bot.py")]
    assert "--host" in cmd and "127.0.0.1" in cmd
    assert "--port" in cmd and "10001" in cmd
    assert "--name" in cmd and "BotA" in cmd
    assert "--seat" in cmd and "upper" in cmd
    assert "--log" in cmd and str(tmp_path / "botA.log") in cmd


def test_official_wire_capture_writes_round_artifacts(tmp_path):
    cfg = OfficialPlatformConfig(
        exe_path=tmp_path / "platform.exe",
        wineprefix=tmp_path / "wine",
        results_dir=tmp_path / "official",
        lock_path=tmp_path / "official.lock",
    )
    capture = OfficialWireCapture(tmp_path / "round", cfg)

    try:
        ports = capture.start()
        summary = capture.write_replay_summary()
    finally:
        capture.stop()

    if not ports and any("could not bind on any address" in issue for issue in capture.issues):
        pytest.skip("sandbox forbids loopback listener sockets")
    assert set(ports) == {"A", "B"}
    assert all(isinstance(port, int) and port > 0 for port in ports.values())
    assert (tmp_path / "round" / "wire_events.jsonl").exists()
    assert (tmp_path / "round" / "replay_summary.json").exists()
    assert summary["events_seen"] == 0


def test_format_wire_issues_preserves_replay_context():
    issues = _format_wire_issues({
        "issues": [
            {
                "kind": "illegal_check",
                "conn": "A",
                "hand": 3,
                "stage": "flop",
                "message": "check",
                "reason": "postflop check is illegal after the first action",
            }
        ]
    })

    assert issues == [
        "wire_illegal_check: conn=A hand=3 stage=flop "
        "msg='check' reason=postflop check is illegal after the first action"
    ]


def test_acceptance_scheduler_runs_self_and_opponent_rounds(tmp_path):
    candidate = tmp_path / "candidate"
    opponent = tmp_path / "opponent"
    candidate.mkdir()
    opponent.mkdir()
    (candidate / "national_bot.py").write_text("pass\n", encoding="utf-8")
    (opponent / "national_bot.py").write_text("pass\n", encoding="utf-8")
    calls = []

    def fake_round(bot_a, bot_b, *, target_hands, round_kind, round_index, config, out_dir):
        calls.append((bot_a, bot_b, target_hands, round_kind, round_index, Path(out_dir).name))
        return {
            "passed": True,
            "issues": [],
            "round_kind": round_kind,
            "round_index": round_index,
            "log_summary": {"hands_started_min": target_hands, "settlements_min": target_hands - 1},
        }

    result = run_official_acceptance_sync(
        candidate,
        opponent=opponent,
        self_play_rounds=2,
        opponent_rounds=3,
        target_hands=70,
        results_dir=tmp_path / "results",
        round_runner=fake_round,
    )

    assert result.passed
    assert result.summary["rounds_run"] == 5
    assert [call[3] for call in calls] == [
        "self_play", "self_play", "opponent", "opponent", "opponent"
    ]
    assert calls[0][0].seat == "upper"
    assert calls[0][1].seat == "lower"
    opponent_calls = calls[-3:]
    assert [(call[0].role, call[1].role) for call in opponent_calls] == [
        ("candidate", "opponent"),
        ("opponent", "candidate"),
        ("candidate", "opponent"),
    ]


def test_acceptance_scheduler_defaults_to_one_plus_one_compliance(tmp_path):
    candidate = tmp_path / "candidate"
    opponent = tmp_path / "opponent"
    candidate.mkdir()
    opponent.mkdir()
    (candidate / "national_bot.py").write_text("pass\n", encoding="utf-8")
    (opponent / "national_bot.py").write_text("pass\n", encoding="utf-8")
    calls = []

    def fake_round(bot_a, bot_b, *, target_hands, round_kind, round_index, config, out_dir):
        calls.append((round_kind, round_index, target_hands, bot_a.name, bot_b.name))
        return {
            "passed": True,
            "issues": [],
            "round_kind": round_kind,
            "round_index": round_index,
            "log_summary": {"hands_started_min": target_hands, "settlements_min": target_hands - 1},
        }

    result = run_official_acceptance_sync(
        candidate,
        opponent=opponent,
        results_dir=tmp_path / "results",
        round_runner=fake_round,
    )

    assert result.passed
    assert result.summary["self_play_rounds"] == 1
    assert result.summary["opponent_rounds"] == 1
    assert result.summary["rounds_requested"] == 2
    assert [(call[0], call[1]) for call in calls] == [("self_play", 1), ("opponent", 1)]


def test_acceptance_scheduler_reports_round_failure(tmp_path):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "national_bot.py").write_text("pass\n", encoding="utf-8")

    def fake_round(*args, **kwargs):
        return {"passed": False, "issues": ["no_progress_timeout"]}

    result = run_official_acceptance_sync(
        candidate,
        self_play_rounds=1,
        opponent_rounds=0,
        target_hands=70,
        results_dir=tmp_path / "results",
        round_runner=fake_round,
    )

    assert not result.passed
    assert result.issues == ["self_play_1: no_progress_timeout"]


def test_acceptance_resume_reuses_only_complete_identity_bound_rounds(tmp_path):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "national_bot.py").write_text("pass\n", encoding="utf-8")
    suite = tmp_path / "suite"
    round_dir = suite / "self_play_01"
    round_dir.mkdir(parents=True)
    artifact_paths = {}
    for name in (
        "platform_log",
        "bot_a_log",
        "bot_b_log",
        "bot_a_stdout",
        "bot_a_stderr",
        "bot_b_stdout",
        "bot_b_stderr",
    ):
        path = round_dir / f"{name}.log"
        path.write_text("", encoding="utf-8")
        artifact_paths[name] = str(path)
    receipt_path = round_dir / "receipt.json"
    thp_path = round_dir / "match.txt"
    thp_path.write_text(
        "\n".join(f"STATE:{index}:x:y:z:p;" for index in range(70)) + "\n",
        encoding="gb2312",
    )
    thp_bytes = thp_path.read_bytes()
    thp_sha256 = hashlib.sha256(thp_bytes).hexdigest()
    receipt = {
        "passed": True,
        "issues": [],
        "duration_sec": 12.0,
        "round_kind": "self_play",
        "round_index": 1,
        "target_hands": 70,
        "bot_a": {"path": str(candidate), "role": "candidate"},
        "bot_b": {"path": str(candidate), "role": "candidate"},
        "log_summary": {"hands_started_min": 70, "settlements_min": 69},
        "artifacts": {
            "receipt": str(receipt_path),
            **artifact_paths,
            "thp_files": [str(thp_path)],
            "thp_summaries": [{
                "path": str(thp_path),
                "exists": True,
                "hand_records": 70,
                "bytes": len(thp_bytes),
                "sha256": thp_sha256,
            }],
            "canonical_thp": {
                "path": str(thp_path),
                "sha256": thp_sha256,
                "bytes": len(thp_bytes),
                "hand_records": 70,
                "duplicate_paths": [],
            },
        },
    }
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    calls = []

    def fake_round(_bot_a, _bot_b, *, target_hands, round_kind, round_index, **_kwargs):
        calls.append((round_kind, round_index))
        return {
            "passed": True,
            "issues": [],
            "round_kind": round_kind,
            "round_index": round_index,
            "target_hands": target_hands,
            "log_summary": {"hands_started_min": target_hands, "settlements_min": target_hands - 1},
        }

    result = run_official_acceptance_sync(
        candidate,
        self_play_rounds=2,
        opponent_rounds=0,
        target_hands=70,
        suite_dir=suite,
        round_runner=fake_round,
    )

    assert result.passed is True
    assert result.summary["resumed_rounds"] == 1
    assert calls == [("self_play", 2)]


def test_acceptance_resume_preserves_completed_failed_round(tmp_path):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "national_bot.py").write_text("pass\n", encoding="utf-8")
    suite = tmp_path / "suite"
    round_dir = suite / "self_play_01"
    round_dir.mkdir(parents=True)
    receipt = {
        "passed": False,
        "issues": ["protocol_illegal_check"],
        "duration_sec": 8.0,
        "round_kind": "self_play",
        "round_index": 1,
        "target_hands": 70,
        "bot_a": {"path": str(candidate), "role": "candidate"},
        "bot_b": {"path": str(candidate), "role": "candidate"},
        "log_summary": {"hands_started_min": 3, "settlements_min": 2},
        "artifacts": {},
    }
    (round_dir / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")

    def must_not_rerun(*_args, **_kwargs):
        raise AssertionError("completed failed round must not be cherry-picked away")

    result = run_official_acceptance_sync(
        candidate,
        self_play_rounds=1,
        opponent_rounds=0,
        target_hands=70,
        suite_dir=suite,
        round_runner=must_not_rerun,
    )

    assert result.passed is False
    assert result.summary["resumed_rounds"] == 1
    assert result.report["rounds"][0]["issues"] == ["protocol_illegal_check"]
    assert result.outcome == "infrastructure_failure"


def test_formal_full_reruns_user_writable_passed_receipt_in_fresh_directory(
    tmp_path,
    monkeypatch,
):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "national_bot.py").write_text("pass\n", encoding="utf-8")
    suite = tmp_path / "suite"
    round_slot = suite / "self_play_01"
    round_slot.mkdir(parents=True)
    envelope = {
        "candidate_hash": hash_path(candidate),
        "opponent_hash": "",
    }
    forged = {
        "passed": True,
        "issues": [],
        "duration_sec": 1.0,
        "round_kind": "self_play",
        "round_index": 1,
        "target_hands": 70,
        "bot_a": {"path": str(candidate), "role": "candidate"},
        "bot_b": {"path": str(candidate), "role": "candidate"},
        "job_envelope": envelope,
        "log_summary": {"hands_started_min": 70, "settlements_min": 69},
        "artifacts": {},
    }
    (round_slot / "receipt.json").write_text(json.dumps(forged), encoding="utf-8")
    calls = []

    def fake_production_round(bot_a, bot_b, **kwargs):
        calls.append(kwargs["out_dir"])
        return {
            "passed": True,
            "issues": [],
            "duration_sec": 2.0,
            "round_kind": kwargs["round_kind"],
            "round_index": kwargs["round_index"],
            "target_hands": kwargs["target_hands"],
            "bot_a": {"path": str(bot_a.path), "role": bot_a.role},
            "bot_b": {"path": str(bot_b.path), "role": bot_b.role},
            "job_envelope": kwargs["job_envelope"],
            "log_summary": {"hands_started_min": 70, "settlements_min": 69},
            "artifacts": {},
        }

    monkeypatch.setattr(harness, "_PRODUCTION_ROUND_RUNNER", fake_production_round)
    monkeypatch.setattr(
        harness,
        "validate_execution_profile",
        lambda *_a, **_k: {"ok": True, "issues": []},
    )
    monkeypatch.setattr(
        harness,
        "seal_bot_artifact",
        lambda _path, _destination, *, expected_hash: SimpleNamespace(
            artifact_hash=expected_hash
        ),
    )

    result = run_official_acceptance_sync(
        candidate,
        self_play_rounds=1,
        opponent_rounds=0,
        target_hands=70,
        suite_dir=suite,
        round_runner=fake_production_round,
        job_envelope=envelope,
        config=OfficialPlatformConfig(lock_path=tmp_path / "official.lock"),
    )

    assert result.passed is True
    assert result.summary["resumed_rounds"] == 0
    assert len(calls) == 1
    assert calls[0].parent.parent == round_slot
    assert calls[0].name.startswith("run_")
    assert json.loads((round_slot / "receipt.json").read_text(encoding="utf-8"))[
        "duration_sec"
    ] == 2.0


def test_target_reached_accepts_official_final_settlement_quirk(tmp_path):
    bot_a_log = tmp_path / "botA.log"
    bot_b_log = tmp_path / "botB.log"
    bot_a_log.write_text(
        "\n".join(
            [f"[10:00:{i % 60:02d}] DISPATCH line='preflop|SMALLBLIND|<0,1><1,2>'" for i in range(70)]
            + [f"[10:01:{i % 60:02d}] DISPATCH line='earnChips 50'" for i in range(69)]
        ),
        encoding="utf-8",
    )
    bot_b_log.write_text(
        "\n".join(
            [f"[10:02:{i % 60:02d}] DISPATCH line='preflop|BIGBLIND|<0,1><1,2>'" for i in range(70)]
            + [f"[10:03:{i % 60:02d}] DISPATCH line='earnChips -50'" for i in range(69)]
        ),
        encoding="utf-8",
    )

    summary = summarize_round_logs(bot_a_log, bot_b_log)

    assert summary["hands_started_min"] == 70
    assert summary["settlements_min"] == 69
    assert _target_reached(summary, 70)


def test_collect_new_thp_files_keeps_platform_dir_clean(tmp_path):
    platform_dir = tmp_path / "platform"
    artifact_dir = tmp_path / "artifacts"
    platform_dir.mkdir()
    old_thp = platform_dir / "THP-old.txt"
    old_thp.write_text("old", encoding="gb2312")
    before = _snapshot_platform_thp_files(platform_dir)

    new_thp = platform_dir / "THP-new.txt"
    new_thp.write_text("new", encoding="gb2312")

    artifacts, issues = _collect_new_thp_files(
        platform_dir,
        before=before,
        artifact_dir=artifact_dir,
    )

    assert issues == []
    assert artifacts == [str(artifact_dir / "THP-new.txt")]
    assert old_thp.exists()
    assert not new_thp.exists()
    assert (artifact_dir / "THP-new.txt").read_text(encoding="gb2312") == "new"


def test_summarize_thp_files_counts_state_records(tmp_path):
    thp = tmp_path / "THP-test.txt"
    thp.write_text("STATE:1:x:y:z:p;\nSTATE:2:x:y:z:p;\n", encoding="gb2312")

    summaries = _summarize_thp_files([str(thp)])

    assert summaries == [{
        "path": str(thp),
        "exists": True,
        "hand_records": 2,
        "bytes": thp.stat().st_size,
        "sha256": hashlib.sha256(thp.read_bytes()).hexdigest(),
    }]


def test_canonical_thp_requires_exact_match_length_and_one_content_identity(tmp_path):
    first = tmp_path / "THP-first.txt"
    duplicate = tmp_path / "THP-duplicate.txt"
    overrun = tmp_path / "THP-overrun.txt"
    exact_text = "\n".join(f"STATE:{index}:x:y:z:p;" for index in range(70)) + "\n"
    first.write_text(exact_text, encoding="gb2312")
    duplicate.write_text(exact_text, encoding="gb2312")
    overrun.write_text(
        "\n".join(f"STATE:{index}:x:y:z:p;" for index in range(71)) + "\n",
        encoding="gb2312",
    )

    canonical, issues = _canonical_thp_evidence(
        _summarize_thp_files([str(first), str(duplicate)]),
        expected_hands=70,
    )
    assert issues == []
    assert canonical["hand_records"] == 70
    assert sorted([canonical["path"], *canonical["duplicate_paths"]]) == sorted(
        [str(first), str(duplicate)]
    )

    canonical, issues = _canonical_thp_evidence(
        _summarize_thp_files([str(first), str(overrun)]),
        expected_hands=70,
    )
    assert canonical is None
    assert any("thp_ambiguous_multiple_outputs" in issue for issue in issues)

    canonical, issues = _canonical_thp_evidence(
        _summarize_thp_files([str(overrun)]),
        expected_hands=70,
    )
    assert canonical["hand_records"] == 71
    assert any("thp_hand_count_mismatch" in issue for issue in issues)


def test_collect_new_thp_files_scans_platform_root_and_exe_dir(tmp_path):
    platform_root = tmp_path / "国赛平台"
    exe_dir = platform_root / "德州扑克对弈平台限时一分钟2021版"
    artifact_dir = tmp_path / "artifacts"
    exe_dir.mkdir(parents=True)
    before = _snapshot_platform_thp_files([exe_dir, platform_root])

    root_thp = platform_root / "THP-root.txt"
    root_thp.write_text("root", encoding="gb2312")

    artifacts, issues = _collect_new_thp_files(
        [exe_dir, platform_root],
        before=before,
        artifact_dir=artifact_dir,
    )

    assert issues == []
    assert artifacts == [str(artifact_dir / "THP-root.txt")]
    assert not root_thp.exists()
    assert (artifact_dir / "THP-root.txt").read_text(encoding="gb2312") == "root"
