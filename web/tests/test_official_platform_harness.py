from pathlib import Path
import sys

from official_platform_harness import (
    BotLaunchConfig,
    build_bot_command,
    parse_bot_log,
    run_official_acceptance_sync,
    summarize_round_logs,
    _collect_new_thp_files,
    _sent_action_issue,
    _snapshot_platform_thp_files,
    _summarize_thp_files,
    _read_issue_file,
    _target_reached,
)


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_official_required_does_not_enable_quality_long_acceptance(monkeypatch):
    import tool_gates

    monkeypatch.setenv("POK_OFFICIAL_REQUIRED", "1")
    monkeypatch.delenv("POK_OFFICIAL_ACCEPTANCE_GATE", raising=False)

    assert tool_gates._official_gate_enabled("POK_OFFICIAL_SMOKE_GATE")
    assert not tool_gates._official_gate_enabled(
        "POK_OFFICIAL_ACCEPTANCE_GATE",
        include_required=False,
    )


def test_pokctl_defaults_official_smoke_to_queue():
    script = (ROOT / "pokctl.sh").read_text(encoding="utf-8")

    assert 'POK_OFFICIAL_SMOKE_GATE:=queue' in script
    assert 'POK_OFFICIAL_PRECOMMIT_TARGET_HANDS:=10' in script
    assert 'POK_OFFICIAL_ACCEPTANCE_GATE:=0' in script


def test_official_platform_cli_defaults_to_manual_70_hand_rounds():
    from scripts.official_platform_acceptance import parse_args

    args = parse_args(["--candidate", "bots/national_v1"])

    assert args.self_play_rounds == 1
    assert args.opponent_rounds == 1
    assert args.target_hands == 70


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
        opponent_rounds=1,
        target_hands=70,
        results_dir=tmp_path / "results",
        round_runner=fake_round,
    )

    assert result.passed
    assert result.summary["rounds_run"] == 3
    assert [call[3] for call in calls] == ["self_play", "self_play", "opponent"]
    assert calls[0][0].seat == "upper"
    assert calls[0][1].seat == "lower"
    assert calls[-1][0].name == "Candidate"
    assert calls[-1][1].name == "Opponent"


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
    }]


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
