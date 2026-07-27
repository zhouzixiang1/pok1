from pathlib import Path
import hashlib
import json
import sys
from types import SimpleNamespace

import pytest

from bot_artifact import canonical_digest, hash_path
from bot_namespace import bot_name
from conftest import STRICT_TARGET_V, strict_bot_name
from managed_bot_executor import IsolationIdentity
import official_platform_harness as harness
from web.tests.runtime_probe_fixtures import (
    passing_runtime_probe,
    seal_passing_runtime_probe,
)
from official_job_envelope import build_job_envelope, job_envelope_issues
from official_platform_harness import (
    BotLaunchConfig,
    OfficialPlatformConfig,
    OfficialWireCapture,
    parse_bot_log,
    run_official_acceptance_sync,
    summarize_round_logs,
    _format_wire_issues,
    _collect_new_thp_files,
    _canonical_thp_evidence,
    _sent_action_issue,
    _snapshot_platform_thp_files,
    _summarize_thp_files,
    _terminal_socket_boundary,
    _terminal_thp_observation,
    _omitted_allin_thp_bindings,
    _parse_thp_action_payload,
    _build_terminal_completion_evidence,
    _read_issue_file,
    _target_reached,
    round_completion_issues,
)


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.mark.parametrize(
    "actions",
    [
        "cc/cc/r216r443r925r1896r3537r17561c",
        "cr350c/r19550c",
        "r19950c",
        "f",
    ],
)
def test_strict_thp_action_grammar_accepts_exact_official_shapes(actions):
    parsed, issue = _parse_thp_action_payload(actions)

    assert issue == ""
    assert parsed is not None


@pytest.mark.parametrize(
    ("actions", "expected_issue"),
    [
        ("garbagec", "street_0_token_shape"),
        ("fc", "street_0_fold_not_terminal"),
        ("c-only", "street_0_token_shape"),
        ("cc//cc", "street_1_empty"),
        ("r0c", "street_0_token_shape"),
        ("r200", "street_0_terminal_missing"),
        ("f/cc", "street_0_not_closed"),
    ],
)
def test_strict_thp_action_grammar_rejects_garbage_and_open_streets(
    actions,
    expected_issue,
):
    parsed, issue = _parse_thp_action_payload(actions)

    assert parsed is None
    assert issue == expected_issue


@pytest.mark.parametrize(
    ("stage", "public_by_stage", "thp_cards", "actions", "expected_count"),
    [
        ("preflop", {}, "AhKh|QsQd", "r19950c", 0),
        (
            "flop",
            {"flop": [[0, 0], [1, 1], [2, 2]]},
            "AhKh|QsQd/2s3h4d",
            "cc/r19950c",
            3,
        ),
        (
            "turn",
            {"flop": [[0, 0], [1, 1], [2, 2]], "turn": [[3, 3]]},
            "AhKh|QsQd/2s3h4d/5c",
            "cc/cc/r19950c",
            4,
        ),
    ],
)
def test_nonterminal_called_allin_accepts_exact_thp_wire_prefix(
    stage,
    public_by_stage,
    thp_cards,
    actions,
    expected_count,
):
    hand = 6
    records = [{"index": index} for index in range(70)]
    records[hand - 1] = {
        "index": hand - 1,
        "actions": actions,
        "cards": thp_cards,
        "earnings": [-20000, 20000],
        "players": ["BotA", "BotB"],
    }
    boundaries = [
        {
            "conn": conn,
            "hand": hand,
            "stage": stage,
            "public_cards_observed": expected_count,
            "natural_hand_70": False,
        }
        for conn in ("A", "B")
    ]
    wire_summary = {
        "omitted_allin_runout_boundaries": boundaries,
        "seats": {
            "A": {
                "name": "BotA",
                "blind_records": [{"hand": hand, "blind": "BIGBLIND"}],
                "public_card_records": [{
                    "hand": hand,
                    "streets": public_by_stage,
                }],
                "showdown_records": [{
                    "hand": hand,
                    "opponent_cards": [[0, 10], [2, 10]],
                }],
            },
            "B": {
                "name": "BotB",
                "blind_records": [{"hand": hand, "blind": "SMALLBLIND"}],
                "public_card_records": [{
                    "hand": hand,
                    "streets": public_by_stage,
                }],
                "showdown_records": [{
                    "hand": hand,
                    "opponent_cards": [[1, 12], [1, 11]],
                }],
            },
        },
    }

    bindings, issues = _omitted_allin_thp_bindings(
        {"records": records},
        wire_summary,
        expected_hands=70,
        expected_names=("BotA", "BotB"),
    )

    assert issues == []
    assert bindings is not None
    assert bindings[0]["thp_public_card_count"] == expected_count
    assert bindings[0]["thp_board_scope"] == "observed_wire_prefix"


def _structural_quality_admission(candidate: Path) -> dict:
    """Build a complete receipt shape for boundary-only harness tests."""

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

    runtime_evidence = runtime_probe_native_template_evidence()

    payload = {
        "schema_version": harness.FORMAL_QUALITY_ADMISSION_SCHEMA_VERSION,
        "kind": "official-formal-quality-admission",
        "candidate_path": str(candidate.resolve()),
        "candidate_hash": hash_path(candidate),
        "checkpoint": {
            "evaluation_epoch": "national_tcp_policy_v1",
            "workflow_run_id": "pytest-harness-workflow",
            "next_v": 200,
            "source_v": 199,
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


def _current_formal_quality_checkpoint(tmp_path, monkeypatch):
    """Build a current, successful normal-quality receipt without an EXE."""

    import national_capability_contract
    import national_runtime_authority
    import national_runtime_probe
    import runtime_architecture_policy
    from workflow_profiles import get_workflow_profile

    repo = tmp_path / "repo"
    candidate = repo / "bots" / bot_name(200)
    candidate.mkdir(parents=True)
    (candidate / "policy.py").write_text("def decide(context):\n    return {'kind': 'pass'}\n", encoding="utf-8")
    candidate_hash = hash_path(candidate)
    ledger = runtime_architecture_policy.build_runtime_contract_ledger({"tasks": []})
    profile = get_workflow_profile()
    probe = passing_runtime_probe()
    probe.update({
        "runtime_contract_ledger_digest": ledger["ledger_digest"],
        "code_fingerprint": candidate_hash,
    })
    probe = seal_passing_runtime_probe(probe)
    managed_isolation_digest = probe["managed_isolation_digest"]
    capability = {
        "ok": True,
        "conclusive": True,
        "detector_version": national_capability_contract.NATIONAL_CAPABILITY_DETECTOR_VERSION,
        "epoch": runtime_architecture_policy.ACTIVE_EPOCH,
        "dynamic_runtime_probe": probe,
    }
    quality = {
        "all_passed": True,
        "critical_scenarios_passed": True,
        "national_native_contract_ok": True,
        "national_capability_contract_ok": True,
        "runtime_contract_identity_ok": True,
        "quality_infrastructure": {"active": False},
        "workflow_profile_id": profile.profile_id,
        "national_execution_mode": profile.national_execution_mode,
        "code_fingerprint": candidate_hash,
        "national_capability_contract": capability,
        "runtime_contract_ledger_digest": ledger["ledger_digest"],
        "runtime_probe_schema_version": national_runtime_probe.RUNTIME_PROBE_SCHEMA_VERSION,
        "runtime_probe_orchestrator_version": national_runtime_probe.RUNTIME_PROBE_ORCHESTRATOR_VERSION,
        "runtime_probe_scenario_digest": national_runtime_probe.RUNTIME_PROBE_SCENARIO_DIGEST,
        "runtime_probe_limits_digest": national_runtime_probe.RUNTIME_PROBE_LIMITS_DIGEST,
        "runtime_probe_identity_digest": national_runtime_probe.RUNTIME_PROBE_IDENTITY_DIGEST,
        "runtime_probe_managed_isolation_digest": managed_isolation_digest,
        **national_runtime_probe.runtime_probe_native_template_evidence(),
        "national_architecture_transition": {
            "ok": True,
            "conclusive": True,
            "policy_version": runtime_architecture_policy.RUNTIME_ARCHITECTURE_POLICY_VERSION,
            "detector_version": national_capability_contract.NATIONAL_CAPABILITY_DETECTOR_VERSION,
            "epoch": runtime_architecture_policy.ACTIVE_EPOCH,
        },
    }
    checkpoint = {
        "evaluation_epoch": runtime_architecture_policy.ACTIVE_EPOCH,
        "workflow_run_id": "quality-run-200",
        "next_v": 200,
        "source_v": 199,
        "workflow_profile_id": profile.profile_id,
        "national_execution_mode": profile.national_execution_mode,
        "runtime_contract_ledger": ledger,
        "master_plan": {"runtime_contract_ledger": ledger},
        "gate_results": {"quality": quality},
    }
    monkeypatch.setattr(
        national_runtime_authority,
        "current_system_native_runtime_errors",
        lambda _candidate: [],
    )
    return repo, candidate, checkpoint


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

    args = parse_args(["--candidate", f"bots/{bot_name(1)}"])

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
                    "log_summary": {"hands_started_min": 5, "settlements_min": 5, "issues": []},
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


def test_official_harness_exposes_only_managed_bot_launch_boundary():
    assert not hasattr(harness, "resolve_bot_entry")
    assert not hasattr(harness, "build_bot_command")
    source = __import__("inspect").getsource(harness._launch_bot)
    assert "EndpointLease.connect(" in source
    assert "seal_bot_artifact(" in source
    assert "launch_sandboxed_bot(" in source
    assert "subprocess.Popen(" not in source


def test_official_diagnostic_rejects_script_symlink_and_outside_namespace(
    tmp_path,
):
    repo = tmp_path / "repo"
    (repo / "bots").mkdir(parents=True)
    script = repo / "arbitrary.py"
    script.write_text("raise SystemExit('host execution')\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="strict_bot_directory"):
        harness._validate_active_diagnostic_bot(script, repo_root=repo)

    outside = repo / "outside" / strict_bot_name()
    outside.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="outside_active_namespace"):
        harness._validate_active_diagnostic_bot(outside, repo_root=repo)

    alias = repo / "bots" / strict_bot_name()
    alias.symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError, match="strict_bot_directory"):
        harness._validate_active_diagnostic_bot(alias, repo_root=repo)


def test_low_authority_bot_launch_still_uses_central_executor(
    tmp_path, monkeypatch
):
    bot_dir = tmp_path / bot_name(1)
    bot_dir.mkdir()
    (bot_dir / "national_bot.py").write_text("pass\n", encoding="utf-8")
    artifact_hash = hash_path(bot_dir)
    sealed = harness.SealedBotArtifact(
        source=bot_dir,
        root=tmp_path / "round" / "managed_inputs" / "botA.stdout",
        entry_relative="national_bot.py",
        artifact_hash=artifact_hash,
        manifest_digest="d" * 64,
    )
    process = SimpleNamespace()
    calls = {}
    monkeypatch.setattr(
        harness,
        "current_system_native_runtime_errors",
        lambda _path: [],
    )

    class FakeEndpoint:
        consumed = True
        closed = True

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        harness,
        "seal_bot_artifact",
        lambda source, destination, expected_hash: (
            calls.update({
                "source": source,
                "destination": destination,
                "expected_hash": expected_hash,
            })
            or sealed
        ),
    )
    monkeypatch.setattr(
        harness.EndpointLease,
        "connect",
        lambda *_args, **_kwargs: FakeEndpoint(),
    )
    monkeypatch.setattr(
        harness,
        "launch_sandboxed_bot",
        lambda artifact, _endpoint, **kwargs: (
            calls.update({"artifact": artifact, "launch_kwargs": kwargs})
            or SimpleNamespace(process=process, isolation=IsolationIdentity(
                policy_sha256="a" * 64,
                bpf_sha256="b" * 64,
                bpf_size=64,
            ))
        ),
    )
    monkeypatch.setattr(
        harness,
        "_popen",
        lambda *_args, **_kwargs: pytest.fail("bot launch bypassed central executor"),
    )
    round_dir = tmp_path / "round"
    round_dir.mkdir()
    config = OfficialPlatformConfig(
        exe_path=tmp_path / "platform.exe",
        wineprefix=tmp_path / "wine",
        results_dir=tmp_path / "results",
        lock_path=tmp_path / "lock",
    )

    launched = harness._launch_bot(
        BotLaunchConfig(bot_dir, name="BotA", seat="upper"),
        config=config,
        env={"HOST_SECRET": "must-not-be-forwarded"},
        log_path=round_dir / "botA.log",
        stdout_path=round_dir / "botA.stdout.log",
        stderr_path=round_dir / "botA.stderr.log",
    )

    assert launched is process
    assert calls["source"] == bot_dir
    assert calls["expected_hash"] == artifact_hash
    assert calls["artifact"] is sealed
    assert "environment" not in calls["launch_kwargs"]
    assert launched._pok_managed_artifact_hash == artifact_hash
    assert launched._pok_managed_isolation["network"] == (
        "isolated-netns-inherited-exact-peer-only"
    )
    harness._close_process_files(launched)


@pytest.mark.parametrize(
    ("sealed_a", "sealed_b", "job_envelope", "expected_issue"),
    [
        (True, False, None, "official_formal_sandbox_asymmetric"),
        (False, False, {"schema_version": 1}, "official_formal_sandbox_required"),
    ],
)
def test_official_round_rejects_non_symmetric_formal_launch_contract(
    tmp_path, monkeypatch, sealed_a, sealed_b, job_envelope, expected_issue
):
    # The 70-hand default path (formal full certification) requires a sealed
    # sandbox on both bots when a job_envelope is present. See the companion
    # test_official_round_smoke_mode_does_not_require_formal_sandbox for the
    # sub-70-hand smoke/compliance path, which passes a job_envelope for
    # certification identity but must NOT be required to seal.
    bot_a_path = tmp_path / "bot_a"
    bot_b_path = tmp_path / "bot_b"
    bot_a_path.mkdir()
    bot_b_path.mkdir()
    (bot_a_path / "national_bot.py").write_text("pass\n", encoding="utf-8")
    (bot_b_path / "national_bot.py").write_text("pass\n", encoding="utf-8")

    def artifact(path):
        return harness.SealedBotArtifact(
            source=path,
            root=path,
            entry_relative="national_bot.py",
            artifact_hash=hash_path(path),
            manifest_digest="e" * 64,
        )

    config = OfficialPlatformConfig(
        exe_path=tmp_path / "platform.exe",
        wineprefix=tmp_path / "wine",
        results_dir=tmp_path / "results",
        lock_path=tmp_path / "lock",
    )
    monkeypatch.setattr(
        harness,
        "check_environment",
        lambda *_args, **_kwargs: {
            "ok": True,
            "issues": [],
            "warnings": [],
            "execution_profile": None,
        },
    )
    monkeypatch.setattr(
        harness,
        "_popen",
        lambda *_args, **_kwargs: pytest.fail("invalid formal round started a process"),
    )

    receipt = harness.run_official_round(
        BotLaunchConfig(
            bot_a_path,
            name="BotA",
            sealed_artifact=artifact(bot_a_path) if sealed_a else None,
        ),
        BotLaunchConfig(
            bot_b_path,
            name="BotB",
            sealed_artifact=artifact(bot_b_path) if sealed_b else None,
        ),
        config=config,
        out_dir=tmp_path / "round",
        job_envelope=job_envelope,
    )

    assert receipt["passed"] is False
    assert expected_issue in receipt["issues"]
    assert receipt["formal_execution"]["sandboxed"] is False


def test_official_round_smoke_mode_does_not_require_formal_sandbox(
    tmp_path, monkeypatch
):
    """Smoke (10-hand) and compliance rounds pass a job_envelope for
    certification identity, but the suite runner correctly skips bot sealing
    for them (formal_requested = job_envelope is not None and target_hands ==
    70, and smoke/compliance use target_hands=10). Requiring a formal sandbox
    for those sub-70-hand modes would deadlock every smoke/compliance round:
    the runner never seals, so the gate would always fire.

    This test pins the 70-hand gating of ``official_formal_sandbox_required``
    so a regression that re-broadens the check to any job_envelope is caught.
    """
    bot_a_path = tmp_path / "bot_a"
    bot_b_path = tmp_path / "bot_b"
    bot_a_path.mkdir()
    bot_b_path.mkdir()
    (bot_a_path / "national_bot.py").write_text("pass\n", encoding="utf-8")
    (bot_b_path / "national_bot.py").write_text("pass\n", encoding="utf-8")

    config = OfficialPlatformConfig(
        exe_path=tmp_path / "platform.exe",
        wineprefix=tmp_path / "wine",
        results_dir=tmp_path / "results",
        lock_path=tmp_path / "lock",
    )
    monkeypatch.setattr(
        harness,
        "check_environment",
        lambda *_args, **_kwargs: {
            "ok": True,
            "issues": [],
            "warnings": [],
            "execution_profile": None,
        },
    )
    # The round must reach the actual platform launch (no formal-sandbox gate
    # fires), so provide a popen stub rather than asserting no process starts.
    class _StubProc:
        poll = lambda *a, **k: 0
        wait = lambda *a, **k: 0
        returncode = 0
        stdout = iter([])
        stderr = iter([])
        pid = 12345

        def terminate(self):
            pass

        def kill(self):
            pass

    monkeypatch.setattr(harness, "_popen", lambda *a, **k: _StubProc())

    receipt = harness.run_official_round(
        BotLaunchConfig(bot_a_path, name="BotA"),
        BotLaunchConfig(bot_b_path, name="BotB"),
        target_hands=10,
        config=config,
        out_dir=tmp_path / "round",
        job_envelope={"schema_version": 1, "certification_mode": "smoke"},
    )

    # The formal-sandbox gate must NOT fire for the smoke path.
    assert "official_formal_sandbox_required" not in receipt["issues"]
    assert "official_formal_sandbox_asymmetric" not in receipt["issues"]


def _bootstrap_authorization_case(tmp_path):
    candidate = tmp_path / strict_bot_name()
    opponent = tmp_path / "first_strict_control_v1"
    candidate.mkdir()
    opponent.mkdir()
    (candidate / "national_bot.py").write_text("candidate\n", encoding="utf-8")
    (opponent / "national_bot.py").write_text("control\n", encoding="utf-8")
    candidate_hash = hash_path(candidate)
    opponent_hash = hash_path(opponent)
    receipt = {
        "kind": "official-first-strict-control-authorization-receipt",
        "bootstrap_control_id": "first_strict_control_v1",
    }
    receipt["receipt_digest"] = canonical_digest(receipt)
    selection = {
        "selected": True,
        "eligible": True,
        "reason": "first_strict_control_bootstrap",
        "kind": "official-first-strict-control-selection",
        "bootstrap_control_id": "first_strict_control_v1",
        "candidate": str(candidate),
        "candidate_binding": {"candidate_hash": candidate_hash},
        "bootstrap_control_receipt": receipt,
        "operator_bootstrap_authorization": {
            "authorization_digest": "d" * 64,
        },
        "opponent": {
            "bot": "first_strict_control_v1",
            "path": str(opponent),
            "artifact_hash": opponent_hash,
            "eligible": True,
            "reason": "first_strict_control_bootstrap",
            "eligibility_receipt": receipt,
        },
    }
    envelope = {
        "bootstrap_control_id": "first_strict_control_v1",
        "candidate_hash": candidate_hash,
        "opponent_hash": opponent_hash,
        "opponent_selection": selection,
        "opponent_selection_digest": canonical_digest(selection),
        "operator_bootstrap_authorization_digest": "d" * 64,
    }
    candidate_launch = BotLaunchConfig(candidate, name="Candidate", role="candidate")
    opponent_launch = BotLaunchConfig(
        opponent,
        name="Opponent",
        role="opponent",
        sealed_artifact=harness.SealedBotArtifact(
            source=opponent,
            root=opponent,
            entry_relative="national_bot.py",
            artifact_hash=opponent_hash,
            manifest_digest="c" * 64,
        ),
    )
    return candidate_launch, opponent_launch, envelope


def test_job_envelope_v4_binds_first_strict_selection_and_detects_tamper(
    tmp_path
):
    candidate, opponent, _ = _bootstrap_authorization_case(tmp_path)
    selection = _["opponent_selection"]
    envelope = build_job_envelope(
        {
            "job_id": "1" * 64,
            "request_digest": "2" * 64,
            "manager_sha256": "3" * 64,
            "identity": {
                "identity_digest": "4" * 64,
                "candidate_hash": hash_path(candidate.path),
                "opponent_hash": hash_path(opponent.path),
            },
            "opponent_selection": selection,
            "source_v": 199,
        },
        attempt=1,
        attempt_nonce="5" * 64,
        suite_dir=tmp_path / "suite",
    )

    assert envelope["schema_version"] == 5
    assert envelope["opponent_selection"] == selection
    assert envelope["bootstrap_control_id"] == "first_strict_control_v1"
    assert envelope["operator_bootstrap_authorization_digest"] == "d" * 64
    assert job_envelope_issues(envelope) == []

    tampered = json.loads(json.dumps(envelope))
    tampered["opponent_selection"]["opponent"]["artifact_hash"] = "f" * 64
    issues = job_envelope_issues(tampered)
    assert "official_job_envelope_digest_mismatch" in issues
    assert "official_job_envelope_opponent_selection_digest_mismatch" in issues

    arbitrary_v1 = {
        key: value
        for key, value in envelope.items()
        if key not in {"opponent_selection", "bootstrap_control_id", "envelope_digest"}
    }
    arbitrary_v1["schema_version"] = 1
    arbitrary_v1["envelope_digest"] = canonical_digest(arbitrary_v1)
    assert "official_job_envelope_schema_mismatch" in job_envelope_issues(
        arbitrary_v1
    )


def test_normal_formal_quality_admission_roundtrips_through_spec_and_job_envelope(
    tmp_path,
):
    import official_certification as certification

    candidate = tmp_path / "bots" / bot_name(200)
    opponent = tmp_path / "bots" / bot_name(199)
    candidate.mkdir(parents=True)
    opponent.mkdir(parents=True)
    (candidate / "national_bot.py").write_text("candidate\n", encoding="utf-8")
    (opponent / "national_bot.py").write_text("opponent\n", encoding="utf-8")
    admission = _structural_quality_admission(candidate)
    spec = certification.build_spec(
        "full",
        candidate,
        opponent=opponent,
        quality_admission=admission,
    )
    record = certification.spec_record(spec)
    restored = certification._spec_from_mapping(record)
    identity = certification.certification_identity(restored)
    envelope = build_job_envelope(
        {
            "job_id": "1" * 64,
            "request_digest": "2" * 64,
            "manager_sha256": "3" * 64,
            "identity": identity,
            "spec": record,
            "opponent_selection": None,
            "source_v": 199,
        },
        attempt=1,
        attempt_nonce="4" * 64,
        suite_dir=tmp_path / "suite",
    )

    assert restored.quality_admission == admission
    assert envelope["quality_admission"] == admission
    assert envelope["quality_admission_digest"] == canonical_digest(admission)
    assert job_envelope_issues(envelope) == []


def test_strict_normal_full_refuses_missing_or_tampered_admission_before_worker(
    tmp_path,
):
    """A normal strict job cannot make the harness mint a live receipt later."""

    import official_certification as certification

    candidate = tmp_path / "bots" / bot_name(200)
    opponent = tmp_path / "bots" / bot_name(199)
    candidate.mkdir(parents=True)
    opponent.mkdir(parents=True)
    (candidate / "national_bot.py").write_text("candidate\n", encoding="utf-8")
    (opponent / "national_bot.py").write_text("opponent\n", encoding="utf-8")

    with pytest.raises(ValueError, match="checkpoint-owned quality admission"):
        certification.build_spec("full", candidate, opponent=opponent)

    spec = certification.build_spec(
        "full",
        candidate,
        opponent=opponent,
        quality_admission=_structural_quality_admission(candidate),
    )
    identity = certification.certification_identity(spec)
    request = {
        "job_id": "1" * 64,
        "request_digest": "2" * 64,
        "manager_sha256": "3" * 64,
        "identity": identity,
        "spec": certification.spec_record(spec),
        "opponent_selection": None,
        "source_v": 199,
    }
    envelope = build_job_envelope(
        request,
        attempt=1,
        attempt_nonce="4" * 64,
        suite_dir=tmp_path / "suite",
    )
    assert job_envelope_issues(envelope) == []

    tampered = dict(envelope)
    tampered["quality_admission"] = None
    tampered["quality_admission_digest"] = None
    unsigned = {
        key: value for key, value in tampered.items() if key != "envelope_digest"
    }
    tampered["envelope_digest"] = canonical_digest(unsigned)
    assert "official_formal_quality_admission_missing" in job_envelope_issues(
        tampered
    )


def test_formal_quality_admission_binds_current_candidate_probe_and_runtime(
    tmp_path, monkeypatch
):
    repo, candidate, checkpoint = _current_formal_quality_checkpoint(
        tmp_path, monkeypatch
    )

    initial = harness.build_formal_quality_admission(
        candidate,
        checkpoint=checkpoint,
        repo_root=repo,
    )
    rebound = harness.build_formal_quality_admission(
        candidate,
        checkpoint=checkpoint,
        repo_root=repo,
        expected_admission=initial["admission"],
    )

    assert initial["valid"] is True
    assert rebound["valid"] is True
    assert initial["admission"]["candidate_hash"] == hash_path(candidate)
    assert initial["admission"]["quality_gate_digest"] == canonical_digest(
        checkpoint["gate_results"]["quality"]
    )
    assert initial["admission"]["runtime_contract_ledger_digest"] == (
        checkpoint["runtime_contract_ledger"]["ledger_digest"]
    )


def test_formal_quality_admission_rejects_missing_repeatability_receipt(
    tmp_path,
    monkeypatch,
):
    """Normal official admission never trusts the probe success booleans alone."""

    from copy import deepcopy

    repo, candidate, checkpoint = _current_formal_quality_checkpoint(
        tmp_path, monkeypatch
    )
    malformed = deepcopy(checkpoint)
    probe = malformed["gate_results"]["quality"][
        "national_capability_contract"
    ]["dynamic_runtime_probe"]
    probe.pop("repeatability")

    admitted = harness.build_formal_quality_admission(
        candidate,
        checkpoint=malformed,
        repo_root=repo,
    )

    assert admitted["valid"] is False
    assert (
        "official_formal_quality_dynamic_probe_repeatability_invalid:"
        "runtime_probe_repeatability_evidence_missing"
    ) in admitted["issues"]


def test_formal_quality_admission_integrity_rejects_stale_repeatability_contract(
    tmp_path,
):
    """A hand-written normal-job receipt cannot downgrade the probe contract."""

    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "national_bot.py").write_text("candidate\n", encoding="utf-8")
    admission = _structural_quality_admission(candidate)
    stale = dict(admission)
    stale_probe = dict(stale["runtime_probe_identity"])
    stale_probe["repeatability_schema_version"] = 1
    stale_probe["repeatability_view_contract"] = "legacy"
    stale["runtime_probe_identity"] = stale_probe
    payload = {key: value for key, value in stale.items() if key != "admission_digest"}
    stale["admission_digest"] = canonical_digest(payload)

    issues = harness.formal_quality_admission_integrity_issues(stale)

    assert (
        "official_formal_quality_admission_runtime_probe_identity_"
        "mismatch:repeatability_schema_version"
    ) in issues
    assert (
        "official_formal_quality_admission_runtime_probe_identity_"
        "mismatch:repeatability_view_contract"
    ) in issues


def test_formal_quality_admission_fails_closed_on_candidate_or_runtime_drift(
    tmp_path, monkeypatch
):
    from copy import deepcopy

    import national_runtime_authority
    import national_runtime_probe

    repo, candidate, checkpoint = _current_formal_quality_checkpoint(
        tmp_path, monkeypatch
    )
    admitted = harness.build_formal_quality_admission(
        candidate,
        checkpoint=checkpoint,
        repo_root=repo,
    )["admission"]

    (candidate / "policy.py").write_text("def decide(context):\n    return {'kind': 'fold'}\n", encoding="utf-8")
    changed = harness.build_formal_quality_admission(
        candidate,
        checkpoint=checkpoint,
        repo_root=repo,
        expected_admission=admitted,
    )
    assert changed["valid"] is False
    assert "official_formal_quality_candidate_hash_mismatch" in changed["issues"]

    # Restore the candidate byte identity, then change only the system-owned
    # precompute template identity.  A stale successful quality receipt cannot
    # cover a decision-changing system precompute edit.
    (candidate / "policy.py").write_text("def decide(context):\n    return {'kind': 'pass'}\n", encoding="utf-8")
    changed_identity = deepcopy(
        national_runtime_authority.current_system_native_runtime_identity()
    )
    changed_identity["artifacts"]["precompute.py"]["sha256"] = "c" * 64
    changed_identity["artifacts"]["precompute.py"]["size"] += 1
    changed_identity["combined_digest"] = (
        national_runtime_authority._canonical_digest({
            "schema_version": changed_identity["schema_version"],
            "kind": changed_identity["kind"],
            "artifacts": changed_identity["artifacts"],
        })
    )
    changed_template_digest = national_runtime_probe._canonical_digest(
        changed_identity
    )
    changed_probe_identity = national_runtime_probe._canonical_digest(
        national_runtime_probe._runtime_probe_identity_payload(
            changed_identity,
            changed_template_digest,
        )
    )
    monkeypatch.setattr(
        national_runtime_authority,
        "current_system_native_runtime_identity",
        lambda: changed_identity,
    )
    monkeypatch.setattr(
        national_runtime_probe,
        "RUNTIME_PROBE_NATIVE_TEMPLATE_IDENTITY",
        changed_identity,
    )
    monkeypatch.setattr(
        national_runtime_probe,
        "RUNTIME_PROBE_NATIVE_TEMPLATE_DIGEST",
        changed_template_digest,
    )
    monkeypatch.setattr(
        national_runtime_probe,
        "RUNTIME_PROBE_IDENTITY_DIGEST",
        changed_probe_identity,
    )
    runtime_changed = harness.build_formal_quality_admission(
        candidate,
        checkpoint=checkpoint,
        repo_root=repo,
        expected_admission=admitted,
    )
    assert runtime_changed["valid"] is False
    assert any(
        "native_template" in issue
        for issue in runtime_changed["issues"]
    )


def test_formal_harness_rejects_missing_normal_quality_admission_before_exe(
    tmp_path, monkeypatch
):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "national_bot.py").write_text("pass\n", encoding="utf-8")
    calls = []

    def fake_production_round(*_args, **_kwargs):
        calls.append("round")
        raise AssertionError("quality admission must block before a round launches")

    monkeypatch.setattr(harness, "_PRODUCTION_ROUND_RUNNER", fake_production_round)
    monkeypatch.setattr(
        harness,
        "_validate_active_diagnostic_bot",
        lambda path: Path(path).resolve(),
    )
    monkeypatch.setattr(
        harness,
        "build_formal_quality_admission",
        lambda *_args, **_kwargs: {
            "valid": False,
            "issues": ["official_formal_quality_admission_missing"],
            "admission": None,
        },
    )
    monkeypatch.setattr(
        harness,
        "validate_execution_profile",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("EXE profile must not be checked after failed admission")
        ),
    )

    # This is a job-admission failure, not a normal failed EXE suite.  It must
    # escape the broad suite-exception handler so the job manager can persist
    # the typed ``quality_admission`` terminal state instead of an
    # infrastructure retry/result artifact.
    with pytest.raises(harness.FormalQualityAdmissionError) as raised:
        run_official_acceptance_sync(
            candidate,
            self_play_rounds=1,
            opponent_rounds=0,
            target_hands=70,
            round_runner=fake_production_round,
            job_envelope={"bootstrap_control_id": None, "quality_admission": None},
            config=OfficialPlatformConfig(lock_path=tmp_path / "official.lock"),
        )

    assert calls == []
    assert raised.value.issues == [
        "official_formal_quality_admission_invalid:"
        "official_formal_quality_admission_missing"
    ]
    assert (
        "official_formal_quality_admission_invalid:"
        "official_formal_quality_admission_missing"
        in str(raised.value)
    )


def test_formal_harness_rechecks_expected_quality_admission_before_exe(
    tmp_path, monkeypatch
):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "national_bot.py").write_text("pass\n", encoding="utf-8")
    monkeypatch.setattr(harness, "_PRODUCTION_ROUND_RUNNER", lambda *_a, **_k: {})
    monkeypatch.setattr(
        harness,
        "_validate_active_diagnostic_bot",
        lambda path: Path(path).resolve(),
    )
    seen = {}
    monkeypatch.setattr(
        harness,
        "build_formal_quality_admission",
        lambda _candidate, *, expected_admission=None, **_kwargs: seen.update(
            {"expected_admission": expected_admission}
        ) or {
            "valid": False,
            "issues": ["official_formal_quality_admission_current_drift"],
            "admission": None,
        },
    )
    monkeypatch.setattr(
        harness,
        "validate_execution_profile",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("runtime identity drift must block before EXE preflight")
        ),
    )
    expected = _structural_quality_admission(candidate)

    with pytest.raises(harness.FormalQualityAdmissionError) as raised:
        run_official_acceptance_sync(
            candidate,
            self_play_rounds=1,
            opponent_rounds=0,
            target_hands=70,
            round_runner=harness._PRODUCTION_ROUND_RUNNER,
            job_envelope={"bootstrap_control_id": None, "quality_admission": expected},
            config=OfficialPlatformConfig(lock_path=tmp_path / "official.lock"),
        )

    assert seen["expected_admission"] == expected
    assert raised.value.issues == [
        "official_formal_quality_admission_invalid:"
        "official_formal_quality_admission_current_drift"
    ]
    assert (
        "official_formal_quality_admission_current_drift" in str(raised.value)
    )


def test_formal_harness_keeps_v143_bootstrap_on_its_distinct_authorization_path(
    tmp_path, monkeypatch
):
    import first_strict_control
    import official_bootstrap

    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "national_bot.py").write_text("pass\n", encoding="utf-8")
    control = tmp_path / "system-controls" / "control"
    control.mkdir(parents=True)
    (control / "national_bot.py").write_text("pass\n", encoding="utf-8")
    monkeypatch.setattr(harness, "_PRODUCTION_ROUND_RUNNER", lambda *_a, **_k: {})
    validated = []

    def validate_active(path):
        resolved = Path(path).resolve()
        validated.append(resolved)
        if resolved == control:
            raise RuntimeError("official_acceptance_bot_outside_active_namespace")
        return resolved

    monkeypatch.setattr(
        harness,
        "_validate_active_diagnostic_bot",
        validate_active,
    )
    monkeypatch.setattr(
        harness,
        "build_formal_quality_admission",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("first-strict bootstrap must not use normal quality admission")
        ),
    )
    monkeypatch.setattr(
        harness,
        "validate_execution_profile",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("stop-after-bootstrap-branch")),
    )
    monkeypatch.setattr(
        official_bootstrap,
        "validate_operator_bootstrap_authorized_selection",
        lambda selection, control_id, path: {
            "valid": control_id == first_strict_control.CONTROL_ID
            and isinstance(selection, dict)
            and Path(path) == candidate,
            "issues": [],
            "selection": selection,
        },
    )
    monkeypatch.setattr(
        first_strict_control,
        "validate_materialized_control",
        lambda path: [] if Path(path) == control else ["control_path_mismatch"],
    )

    result = run_official_acceptance_sync(
        candidate,
        opponent=control,
        self_play_rounds=1,
        opponent_rounds=1,
        target_hands=70,
        round_runner=harness._PRODUCTION_ROUND_RUNNER,
        job_envelope={
            "bootstrap_control_id": first_strict_control.CONTROL_ID,
            "opponent_selection": {
                "selected": True,
                "candidate_binding": {"candidate_hash": "a" * 64},
                "opponent": {
                    "path": str(control),
                    "artifact_hash": "b" * 64,
                },
            },
            "candidate_hash": "a" * 64,
            "opponent_hash": "b" * 64,
        },
        config=OfficialPlatformConfig(lock_path=tmp_path / "official.lock"),
    )

    assert result.passed is False
    assert any("stop-after-bootstrap-branch" in issue for issue in result.issues)
    assert validated == [candidate]


def test_formal_bootstrap_rejects_outside_path_not_bound_by_authorization(
    tmp_path, monkeypatch
):
    import first_strict_control
    import official_bootstrap

    candidate = tmp_path / "candidate"
    candidate.mkdir()
    authorized = tmp_path / "system-controls" / "authorized"
    authorized.mkdir(parents=True)
    supplied = tmp_path / "system-controls" / "substituted"
    supplied.mkdir(parents=True)
    monkeypatch.setattr(harness, "_PRODUCTION_ROUND_RUNNER", lambda *_a, **_k: {})
    monkeypatch.setattr(
        harness,
        "_validate_active_diagnostic_bot",
        lambda path: Path(path).resolve(),
    )
    monkeypatch.setattr(
        official_bootstrap,
        "validate_operator_bootstrap_authorized_selection",
        lambda selection, _control_id, _path: {
            "valid": True,
            "issues": [],
            "selection": selection,
        },
    )
    monkeypatch.setattr(
        first_strict_control,
        "validate_materialized_control",
        lambda _path: (_ for _ in ()).throw(
            AssertionError("a substituted path must fail before byte validation")
        ),
    )
    monkeypatch.setattr(
        harness,
        "validate_execution_profile",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a substituted path must fail before EXE preflight")
        ),
    )

    result = run_official_acceptance_sync(
        candidate,
        opponent=supplied,
        self_play_rounds=0,
        opponent_rounds=1,
        target_hands=70,
        round_runner=harness._PRODUCTION_ROUND_RUNNER,
        job_envelope={
            "bootstrap_control_id": first_strict_control.CONTROL_ID,
            "opponent_selection": {
                "selected": True,
                "candidate_binding": {"candidate_hash": "a" * 64},
                "opponent": {
                    "path": str(authorized),
                    "artifact_hash": "b" * 64,
                },
            },
            "candidate_hash": "a" * 64,
            "opponent_hash": "b" * 64,
        },
        config=OfficialPlatformConfig(lock_path=tmp_path / "official.lock"),
    )

    assert result.passed is False
    assert any(
        "official_formal_bootstrap_authorization_invalid:opponent_path_mismatch"
        in issue
        for issue in result.issues
    )


def test_formal_bootstrap_rejects_symlink_alias_of_authorized_control(
    tmp_path, monkeypatch
):
    import first_strict_control
    import official_bootstrap

    candidate = tmp_path / "candidate"
    candidate.mkdir()
    authorized = tmp_path / "system-controls" / "authorized"
    authorized.mkdir(parents=True)
    alias = tmp_path / "control-alias"
    alias.symlink_to(authorized, target_is_directory=True)
    monkeypatch.setattr(harness, "_PRODUCTION_ROUND_RUNNER", lambda *_a, **_k: {})
    monkeypatch.setattr(
        harness,
        "_validate_active_diagnostic_bot",
        lambda path: Path(path).resolve(),
    )
    monkeypatch.setattr(
        official_bootstrap,
        "validate_operator_bootstrap_authorized_selection",
        lambda selection, _control_id, _path: {
            "valid": True,
            "issues": [],
            "selection": selection,
        },
    )
    monkeypatch.setattr(
        first_strict_control,
        "validate_materialized_control",
        lambda _path: (_ for _ in ()).throw(
            AssertionError("a symlink alias must fail before byte validation")
        ),
    )

    result = run_official_acceptance_sync(
        candidate,
        opponent=alias,
        self_play_rounds=0,
        opponent_rounds=1,
        target_hands=70,
        round_runner=harness._PRODUCTION_ROUND_RUNNER,
        job_envelope={
            "bootstrap_control_id": first_strict_control.CONTROL_ID,
            "opponent_selection": {
                "selected": True,
                "candidate_binding": {"candidate_hash": "a" * 64},
                "opponent": {
                    "path": str(authorized),
                    "artifact_hash": "b" * 64,
                },
            },
            "candidate_hash": "a" * 64,
            "opponent_hash": "b" * 64,
        },
        config=OfficialPlatformConfig(lock_path=tmp_path / "official.lock"),
    )

    assert result.passed is False
    assert any(
        "official_formal_bootstrap_authorization_invalid:opponent_path_mismatch"
        in issue
        for issue in result.issues
    )


def test_formal_harness_rejects_an_untrusted_bootstrap_label_before_exe(
    tmp_path, monkeypatch
):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "national_bot.py").write_text("pass\n", encoding="utf-8")
    monkeypatch.setattr(harness, "_PRODUCTION_ROUND_RUNNER", lambda *_a, **_k: {})
    monkeypatch.setattr(
        harness,
        "_validate_active_diagnostic_bot",
        lambda path: Path(path).resolve(),
    )
    monkeypatch.setattr(
        harness,
        "build_formal_quality_admission",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("an untrusted bootstrap label must not fall back to normal admission")
        ),
    )
    monkeypatch.setattr(
        harness,
        "validate_execution_profile",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("untrusted bootstrap label reached EXE preflight")
        ),
    )

    result = run_official_acceptance_sync(
        candidate,
        self_play_rounds=1,
        opponent_rounds=0,
        target_hands=70,
        round_runner=harness._PRODUCTION_ROUND_RUNNER,
        job_envelope={"bootstrap_control_id": "not-a-real-control"},
        config=OfficialPlatformConfig(lock_path=tmp_path / "official.lock"),
    )

    assert result.passed is False
    assert any(
        "official_formal_bootstrap_authorization_invalid" in issue
        for issue in result.issues
    )


def test_untrusted_bootstrap_label_does_not_defer_outside_namespace_gate(
    tmp_path, monkeypatch
):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    outside = tmp_path / "system-controls" / "untrusted"
    outside.mkdir(parents=True)
    validated = []

    def validate_active(path):
        resolved = Path(path).resolve()
        validated.append(resolved)
        if resolved == outside:
            raise RuntimeError("official_acceptance_bot_outside_active_namespace")
        return resolved

    monkeypatch.setattr(harness, "_PRODUCTION_ROUND_RUNNER", lambda *_a, **_k: {})
    monkeypatch.setattr(harness, "_validate_active_diagnostic_bot", validate_active)
    monkeypatch.setattr(
        harness,
        "validate_execution_profile",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("unknown control reached EXE preflight")
        ),
    )

    result = run_official_acceptance_sync(
        candidate,
        opponent=outside,
        self_play_rounds=0,
        opponent_rounds=1,
        target_hands=70,
        round_runner=harness._PRODUCTION_ROUND_RUNNER,
        job_envelope={"bootstrap_control_id": "not-a-real-control"},
        config=OfficialPlatformConfig(lock_path=tmp_path / "official.lock"),
    )

    assert result.passed is False
    assert validated == [candidate, outside]
    assert any("outside_active_namespace" in issue for issue in result.issues)


def test_formal_bootstrap_cross_binds_envelope_hashes_before_exe(
    tmp_path, monkeypatch
):
    import first_strict_control
    import official_bootstrap

    candidate = tmp_path / "candidate"
    candidate.mkdir()
    control = tmp_path / "system-controls" / "control"
    control.mkdir(parents=True)
    monkeypatch.setattr(harness, "_PRODUCTION_ROUND_RUNNER", lambda *_a, **_k: {})
    monkeypatch.setattr(
        harness,
        "_validate_active_diagnostic_bot",
        lambda path: Path(path).resolve(),
    )
    selection = {
        "selected": True,
        "candidate_binding": {"candidate_hash": "a" * 64},
        "opponent": {
            "path": str(control),
            "artifact_hash": "b" * 64,
        },
    }
    monkeypatch.setattr(
        official_bootstrap,
        "validate_operator_bootstrap_authorized_selection",
        lambda *_args, **_kwargs: {
            "valid": True,
            "issues": [],
            "selection": selection,
        },
    )
    monkeypatch.setattr(
        first_strict_control,
        "validate_materialized_control",
        lambda _path: (_ for _ in ()).throw(
            AssertionError("hash drift must fail before byte validation")
        ),
    )
    monkeypatch.setattr(
        harness,
        "validate_execution_profile",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("hash drift reached EXE preflight")
        ),
    )

    result = run_official_acceptance_sync(
        candidate,
        opponent=control,
        self_play_rounds=0,
        opponent_rounds=1,
        target_hands=70,
        round_runner=harness._PRODUCTION_ROUND_RUNNER,
        job_envelope={
            "bootstrap_control_id": first_strict_control.CONTROL_ID,
            "opponent_selection": selection,
            "candidate_hash": "a" * 64,
            "opponent_hash": "c" * 64,
        },
        config=OfficialPlatformConfig(lock_path=tmp_path / "official.lock"),
    )

    assert result.passed is False
    assert any(
        "official_formal_bootstrap_authorization_invalid:opponent_hash_mismatch"
        in issue
        for issue in result.issues
    )


def test_formal_bootstrap_rejects_valid_flag_without_authorized_selection(
    tmp_path, monkeypatch
):
    import first_strict_control
    import official_bootstrap

    candidate = tmp_path / "candidate"
    candidate.mkdir()
    control = tmp_path / "system-controls" / "control"
    control.mkdir(parents=True)
    forged_selection = {
        "selected": True,
        "candidate_binding": {"candidate_hash": "a" * 64},
        "opponent": {
            "path": str(control),
            "artifact_hash": "b" * 64,
        },
    }
    monkeypatch.setattr(harness, "_PRODUCTION_ROUND_RUNNER", lambda *_a, **_k: {})
    monkeypatch.setattr(
        harness,
        "_validate_active_diagnostic_bot",
        lambda path: Path(path).resolve(),
    )
    monkeypatch.setattr(
        official_bootstrap,
        "validate_operator_bootstrap_authorized_selection",
        lambda *_args, **_kwargs: {"valid": True, "issues": []},
    )
    monkeypatch.setattr(
        first_strict_control,
        "validate_materialized_control",
        lambda _path: (_ for _ in ()).throw(
            AssertionError("missing authorized selection reached byte validation")
        ),
    )
    monkeypatch.setattr(
        harness,
        "validate_execution_profile",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("missing authorized selection reached EXE preflight")
        ),
    )
    monkeypatch.setattr(
        harness,
        "seal_bot_artifact",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("missing authorized selection reached sealing")
        ),
    )

    result = run_official_acceptance_sync(
        candidate,
        opponent=control,
        self_play_rounds=0,
        opponent_rounds=1,
        target_hands=70,
        round_runner=harness._PRODUCTION_ROUND_RUNNER,
        job_envelope={
            "bootstrap_control_id": first_strict_control.CONTROL_ID,
            "opponent_selection": forged_selection,
            "candidate_hash": "a" * 64,
            "opponent_hash": "b" * 64,
        },
        config=OfficialPlatformConfig(lock_path=tmp_path / "official.lock"),
    )

    assert result.passed is False
    assert any(
        "official_formal_bootstrap_authorization_invalid:"
        "authorized_selection_missing" in issue
        for issue in result.issues
    )


def test_formal_bootstrap_keeps_authorized_hash_across_profile_preflight(
    tmp_path, monkeypatch
):
    import first_strict_control
    import official_bootstrap

    candidate = tmp_path / "candidate"
    candidate.mkdir()
    control = tmp_path / "system-controls" / "control"
    control.mkdir(parents=True)
    monkeypatch.setattr(harness, "_PRODUCTION_ROUND_RUNNER", lambda *_a, **_k: {})
    monkeypatch.setattr(
        harness,
        "_validate_active_diagnostic_bot",
        lambda path: Path(path).resolve(),
    )
    selection = {
        "selected": True,
        "candidate_binding": {"candidate_hash": "a" * 64},
        "opponent": {
            "path": str(control),
            "artifact_hash": "b" * 64,
        },
    }
    envelope = {
        "bootstrap_control_id": first_strict_control.CONTROL_ID,
        "opponent_selection": selection,
        "candidate_hash": "a" * 64,
        "opponent_hash": "b" * 64,
    }
    monkeypatch.setattr(
        official_bootstrap,
        "validate_operator_bootstrap_authorized_selection",
        lambda *_args, **_kwargs: {
            "valid": True,
            "issues": [],
            "selection": selection,
        },
    )
    monkeypatch.setattr(
        first_strict_control,
        "validate_materialized_control",
        lambda _path: [],
    )

    def preflight(*_args, **_kwargs):
        envelope["opponent_hash"] = "c" * 64
        return {"ok": True, "issues": []}

    sealed = []

    def seal(path, _destination, *, expected_hash):
        sealed.append((Path(path), expected_hash))
        if Path(path) == control:
            raise RuntimeError("stop-after-seal-binding")
        return object()

    monkeypatch.setattr(harness, "validate_execution_profile", preflight)
    monkeypatch.setattr(harness, "seal_bot_artifact", seal)

    result = run_official_acceptance_sync(
        candidate,
        opponent=control,
        self_play_rounds=0,
        opponent_rounds=1,
        target_hands=70,
        round_runner=harness._PRODUCTION_ROUND_RUNNER,
        job_envelope=envelope,
        config=OfficialPlatformConfig(lock_path=tmp_path / "official.lock"),
    )

    assert result.passed is False
    assert any("stop-after-seal-binding" in issue for issue in result.issues)
    assert sealed == [(candidate, "a" * 64), (control, "b" * 64)]


def test_harness_exposes_no_archived_runtime_bootstrap_waiver():
    assert not hasattr(harness, "_official_quarantine_authorization")
    parameters = __import__("inspect").signature(harness._launch_bot).parameters
    assert "quarantine_authorization" not in parameters


def test_manual_official_round_rejects_archived_runtime_before_process_start(
    tmp_path, monkeypatch
):
    archived = tmp_path / "archived-runtime"
    archived.mkdir()
    (archived / "national_bot.py").write_text("legacy\n", encoding="utf-8")
    peer = tmp_path / "peer"
    peer.mkdir()
    (peer / "national_bot.py").write_text("pass\n", encoding="utf-8")
    config = OfficialPlatformConfig(
        exe_path=tmp_path / "platform.exe",
        wineprefix=tmp_path / "wine",
        results_dir=tmp_path / "results",
        lock_path=tmp_path / "lock",
    )
    monkeypatch.setattr(
        harness,
        "check_environment",
        lambda *_args, **_kwargs: {
            "ok": True,
            "issues": [],
            "warnings": [],
            "execution_profile": None,
        },
    )
    monkeypatch.setattr(
        harness,
        "_popen",
        lambda *_args, **_kwargs: pytest.fail("archived raw process was started"),
    )
    monkeypatch.setattr(
        harness,
        "current_system_native_runtime_errors",
        lambda path: ["system_owned_native_runtime_identity_mismatch"]
        if Path(path) == archived
        else [],
    )

    receipt = harness.run_official_round(
        BotLaunchConfig(archived, name="Legacy", role="opponent"),
        BotLaunchConfig(peer, name="Peer", role="candidate"),
        config=config,
        out_dir=tmp_path / "round-archived",
    )

    assert receipt["passed"] is False
    assert any(
        "non_system_owned_native_runtime_forbidden" in issue
        for issue in receipt["issues"]
    )


def test_official_wire_capture_writes_round_artifacts(tmp_path):
    cfg = OfficialPlatformConfig(
        exe_path=tmp_path / "platform.exe",
        wineprefix=tmp_path / "wine",
        results_dir=tmp_path / "official",
        lock_path=tmp_path / "official.lock",
    )
    capture = OfficialWireCapture(tmp_path / "round", cfg)

    final_summary = {}
    try:
        ports = capture.start()
        summary = capture.write_replay_summary()
    finally:
        final_summary = capture.stop()

    if not ports and any("could not bind on any address" in issue for issue in capture.issues):
        pytest.skip("sandbox forbids loopback listener sockets")
    assert set(ports) == {"A", "B"}
    assert all(isinstance(port, int) and port > 0 for port in ports.values())
    assert (tmp_path / "round" / "wire_events.jsonl").exists()
    assert (tmp_path / "round" / "replay_summary.json").exists()
    assert summary["events_seen"] == 0
    stored = json.loads(
        (tmp_path / "round" / "replay_summary.json").read_text(encoding="utf-8")
    )
    events = [
        json.loads(line)
        for line in (tmp_path / "round" / "wire_events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert events[-1]["event_type"] == "capture_finalized"
    assert final_summary == stored
    assert final_summary["issues"] == []


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

    assert _format_wire_issues({
        "issues": [],
        "warnings": [{
            "kind": "provisional_street_boundary_unproved",
            "strict_issue_kind": "street_boundary_unproved",
            "conn": "A",
        }],
    }) == []


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
            "log_summary": {"hands_started_min": target_hands, "settlements_min": target_hands},
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
            "log_summary": {"hands_started_min": target_hands, "settlements_min": target_hands},
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
        "log_summary": {"hands_started_min": 70, "settlements_min": 70},
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
            "log_summary": {"hands_started_min": target_hands, "settlements_min": target_hands},
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
        "quality_admission": _structural_quality_admission(candidate),
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
            "log_summary": {"hands_started_min": 70, "settlements_min": 70},
            "artifacts": {},
        }

    monkeypatch.setattr(harness, "_PRODUCTION_ROUND_RUNNER", fake_production_round)
    monkeypatch.setattr(
        harness,
        "_validate_active_diagnostic_bot",
        lambda path: Path(path).resolve(),
    )
    monkeypatch.setattr(
        harness,
        "build_formal_quality_admission",
        lambda *_args, **_kwargs: {
            "valid": True,
            "issues": [],
            "admission": {"admission_digest": "a" * 64},
        },
    )
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


def test_target_reached_requires_the_final_official_settlement(tmp_path):
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
    assert not _target_reached(summary, 70)

    with bot_a_log.open("a", encoding="utf-8") as stream:
        stream.write("\n[10:04:00] DISPATCH line='earnChips 50'\n")
    with bot_b_log.open("a", encoding="utf-8") as stream:
        stream.write("\n[10:04:00] DISPATCH line='earnChips -50'\n")

    complete = summarize_round_logs(bot_a_log, bot_b_log)
    assert complete["settlements_min"] == 70
    assert _target_reached(complete, 70)


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
        "hand_indices": [1, 2],
        "bytes": thp.stat().st_size,
        "sha256": hashlib.sha256(thp.read_bytes()).hexdigest(),
    }]


def test_terminal_hand_completion_requires_exact_wire_boundary_and_thp(tmp_path):
    platform_dir = tmp_path / "platform"
    platform_dir.mkdir()
    before = _snapshot_platform_thp_files(platform_dir)
    thp = platform_dir / "THP-BotA-vs-BotB.txt"
    thp.write_text(
        "".join(
            f"STATE:{index}:f:AhKh|QsQd:50|-50:BotA|BotB;"
            for index in range(69)
        )
        + "STATE:69:cc/cc/r20000c:AhKh|QsQd/2s3h4d/5c/6s:"
        "20000|-20000:BotA|BotB;"
        + "{[THP][BotA][BotB][BotA赢得23450个筹码]"
        "[2026-07-11 17:22 合肥][2018 CCGC]}",
        encoding="gb2312",
    )
    log_summary = {"hands_started_min": 70, "settlements_min": 69}
    wire_summary = {
        "hands_started_min": 70,
        "settlements_min": 69,
        "pending_expected_actions": [],
        "omitted_allin_runout_boundaries": [
            {
                "conn": conn,
                "hand": 70,
                "stage": "turn",
                "public_cards_observed": 4,
                "settlement_amount": None,
                "natural_hand_70": True,
            }
            for conn in ("A", "B")
        ],
        "seats": {
            "A": {
                "name": "BotA",
                "hands_started": 70,
                "settlements": 69,
                "settlement_records": [
                    {"hand": hand, "amount": 50}
                    for hand in range(1, 70)
                ],
                "pending_expected_action": False,
                "blind_records": [{"hand": 70, "blind": "BIGBLIND"}],
                "public_card_records": [{
                    "hand": 70,
                    "streets": {
                        "flop": [[0, 0], [1, 1], [2, 2]],
                        "turn": [[3, 3]],
                    },
                }],
                "showdown_records": [{
                    "hand": 70,
                    "opponent_cards": [[0, 10], [2, 10]],
                }],
            },
            "B": {
                "name": "BotB",
                "hands_started": 70,
                "settlements": 69,
                "settlement_records": [
                    {"hand": hand, "amount": -50}
                    for hand in range(1, 70)
                ],
                "pending_expected_action": False,
                "blind_records": [{"hand": 70, "blind": "SMALLBLIND"}],
                "public_card_records": [{
                    "hand": 70,
                    "streets": {
                        "flop": [[0, 0], [1, 1], [2, 2]],
                        "turn": [[3, 3]],
                    },
                }],
                "showdown_records": [{
                    "hand": 70,
                    "opponent_cards": [[1, 12], [1, 11]],
                }],
            },
        },
    }

    assert _terminal_socket_boundary(log_summary, wire_summary, 70) is True
    missing_thp_dir = tmp_path / "missing-thp"
    missing_thp_dir.mkdir()
    missing_observation, missing_issues = _terminal_thp_observation(
        missing_thp_dir,
        before=_snapshot_platform_thp_files(missing_thp_dir),
        expected_hands=70,
        expected_names=("BotA", "BotB"),
        wire_summary=wire_summary,
    )
    assert missing_observation is None
    assert "thp_missing_for_full_70_hand_round" in missing_issues
    observation, issues = _terminal_thp_observation(
        platform_dir,
        before=before,
        expected_hands=70,
        expected_names=("BotA", "BotB"),
        wire_summary=wire_summary,
    )
    assert issues == []
    summaries = _summarize_thp_files([str(thp)])
    canonical, canonical_issues = _canonical_thp_evidence(
        summaries,
        expected_hands=70,
    )
    assert canonical_issues == []
    receipt = {
        "bot_a": {"name": "BotA"},
        "bot_b": {"name": "BotB"},
        "log_summary": log_summary,
        "wire_replay_summary": wire_summary,
        "artifacts": {
            "canonical_thp": canonical,
            "wire_events": str(tmp_path / "wire_events.jsonl"),
        },
    }
    (tmp_path / "wire_events.jsonl").write_text("{}\n", encoding="utf-8")
    receipt["completion_evidence"] = _build_terminal_completion_evidence(
        receipt,
        observation,
        canonical,
        target_hands=70,
    )

    assert round_completion_issues(receipt, 70) == []

    def observe_variant(
        label,
        thp_text,
        summary,
        *,
        allow_provisional_wire=False,
    ):
        variant_dir = tmp_path / label
        variant_dir.mkdir()
        variant_before = _snapshot_platform_thp_files(variant_dir)
        (variant_dir / f"THP-{label}.txt").write_text(
            thp_text,
            encoding="gb2312",
        )
        return _terminal_thp_observation(
            variant_dir,
            before=variant_before,
            expected_hands=70,
            expected_names=("BotA", "BotB"),
            wire_summary=summary,
            allow_provisional_wire=allow_provisional_wire,
        )

    valid_thp_text = thp.read_text(encoding="gb2312")
    provisional_summary = json.loads(json.dumps(wire_summary))
    provisional_summary[
        "provisional_omitted_allin_runout_boundaries"
    ] = provisional_summary.pop("omitted_allin_runout_boundaries")
    unfinalized, unfinalized_issues = observe_variant(
        "unfinalized-wire",
        valid_thp_text,
        provisional_summary,
    )
    assert unfinalized is None
    assert unfinalized_issues == ["omitted_allin_runout_wire_not_finalized"]
    live_observation, live_issues = observe_variant(
        "live-provisional-wire",
        valid_thp_text,
        provisional_summary,
        allow_provisional_wire=True,
    )
    assert live_issues == []
    assert live_observation is not None
    assert (
        live_observation["omitted_allin_runout_bindings"]
        == observation["omitted_allin_runout_bindings"]
    )

    omitted_thp_suffix, omitted_thp_suffix_issues = observe_variant(
        "official-omitted-thp-suffix",
        valid_thp_text.replace(
            "AhKh|QsQd/2s3h4d/5c/6s",
            "AhKh|QsQd/2s3h4d/5c",
        ),
        wire_summary,
    )
    assert omitted_thp_suffix_issues == []
    assert omitted_thp_suffix is not None
    assert omitted_thp_suffix["omitted_allin_runout_bindings"][0][
        "thp_public_cards"
    ] == [[0, 0], [1, 1], [2, 2], [3, 3]]
    assert omitted_thp_suffix["omitted_allin_runout_bindings"][0][
        "thp_public_card_count"
    ] == 4
    assert omitted_thp_suffix["omitted_allin_runout_bindings"][0][
        "thp_board_scope"
    ] == "observed_wire_prefix"
    assert observation["omitted_allin_runout_bindings"][0][
        "thp_board_scope"
    ] == "complete_runout"

    missing_terminal_wire = json.loads(json.dumps(wire_summary))
    missing_terminal_wire["omitted_allin_runout_boundaries"] = []
    for seat in missing_terminal_wire["seats"].values():
        seat["showdown_records"] = []
    missing_terminal, missing_terminal_issues = observe_variant(
        "missing-terminal-wire",
        valid_thp_text,
        missing_terminal_wire,
    )
    assert missing_terminal is None
    assert missing_terminal_issues == [
        "terminal_thp_showdown_holes_invalid:A"
    ]

    full_board_wire = json.loads(json.dumps(wire_summary))
    full_board_wire["omitted_allin_runout_boundaries"] = []
    for seat in full_board_wire["seats"].values():
        seat["public_card_records"][0]["streets"]["river"] = [[0, 4]]
    full_board_observation, full_board_issues = observe_variant(
        "full-board-terminal-wire",
        valid_thp_text,
        full_board_wire,
    )
    assert full_board_issues == []
    assert full_board_observation is not None
    assert full_board_observation["terminal_wire_binding"][
        "terminal_kind"
    ] == "full_board_showdown"

    missing_runout, missing_runout_issues = observe_variant(
        "missing-runout",
        valid_thp_text.replace(
            "AhKh|QsQd/2s3h4d/5c/6s",
            "AhKh|QsQd",
        ),
        wire_summary,
    )
    assert missing_runout is None
    assert missing_runout_issues == [
        "omitted_allin_runout_thp_board_shape_invalid:70:observed=4:thp=0"
    ]

    partial_extra_summary = json.loads(json.dumps(wire_summary))
    for boundary in partial_extra_summary["omitted_allin_runout_boundaries"]:
        boundary["stage"] = "flop"
        boundary["public_cards_observed"] = 3
    for seat in partial_extra_summary["seats"].values():
        seat["public_card_records"][0]["streets"].pop("turn")
    partial_extra, partial_extra_issues = observe_variant(
        "partial-extra-thp-board",
        valid_thp_text.replace(
            "AhKh|QsQd/2s3h4d/5c/6s",
            "AhKh|QsQd/2s3h4d/5c",
        ).replace("cc/cc/r20000c", "cc/r20000c"),
        partial_extra_summary,
    )
    assert partial_extra is None
    assert partial_extra_issues == [
        "omitted_allin_runout_thp_board_shape_invalid:70:observed=3:thp=4"
    ]

    contradictory_action, contradictory_action_issues = observe_variant(
        "contradictory-thp-action",
        valid_thp_text.replace("STATE:69:cc/cc/r20000c:", "STATE:69:f:"),
        wire_summary,
    )
    assert contradictory_action is None
    assert contradictory_action_issues == [
        "omitted_allin_runout_thp_action_invalid:70"
    ]

    garbage_action, garbage_action_issues = observe_variant(
        "garbage-thp-action",
        valid_thp_text.replace(
            "STATE:69:cc/cc/r20000c:",
            "STATE:69:garbagec:",
        ),
        wire_summary,
    )
    assert garbage_action is None
    assert garbage_action_issues == [
        "thp_record_actions_invalid:69:street_0_token_shape"
    ]

    changed_prefix, changed_prefix_issues = observe_variant(
        "changed-prefix",
        valid_thp_text.replace("2s3h4d", "2s3h7d"),
        wire_summary,
    )
    assert changed_prefix is None
    assert changed_prefix_issues == [
        "omitted_allin_runout_public_prefix_mismatch:70:A"
    ]

    reveal_mismatch_summary = json.loads(json.dumps(wire_summary))
    reveal_mismatch_summary["seats"]["A"]["showdown_records"][0][
        "opponent_cards"
    ] = [[0, 9], [2, 10]]
    reveal_mismatch, reveal_mismatch_issues = observe_variant(
        "reveal-mismatch",
        valid_thp_text,
        reveal_mismatch_summary,
    )
    assert reveal_mismatch is None
    assert reveal_mismatch_issues == [
        "omitted_allin_runout_showdown_binding_invalid:70:A"
    ]

    missing = {key: value for key, value in receipt.items() if key != "completion_evidence"}
    assert round_completion_issues(missing, 70) == [
        "official_terminal_completion_evidence_missing"
    ]
    tampered = json.loads(json.dumps(receipt))
    tampered["completion_evidence"]["final_hand"]["earnings"] = [49, -49]
    assert "official_terminal_completion_evidence_digest_mismatch" in round_completion_issues(
        tampered,
        70,
    )
    resigned_binding = json.loads(json.dumps(receipt))
    resigned_binding["completion_evidence"][
        "omitted_allin_runout_bindings"
    ][0]["thp_board_scope"] = "observed_wire_prefix"
    resigned_payload = {
        key: value
        for key, value in resigned_binding["completion_evidence"].items()
        if key != "evidence_digest"
    }
    resigned_binding["completion_evidence"]["evidence_digest"] = canonical_digest(
        resigned_payload
    )
    resigned_issues = round_completion_issues(resigned_binding, 70)
    assert (
        "official_terminal_completion_omitted_runout_bindings_mismatch"
        in resigned_issues
    )
    assert "official_terminal_completion_observation_digest_mismatch" in resigned_issues


def test_natural_terminal_mode_rejects_paired_70_wire_settlements():
    paired = {
        "log_summary": {"hands_started_min": 70, "settlements_min": 70},
        "wire_replay_summary": {
            "hands_started_min": 70,
            "settlements_min": 70,
            "pending_expected_actions": [],
        },
    }

    # Manual/non-EXE diagnostic acceptance retains its generic completion
    # shape, but a formal fixed-EXE run cannot use it for full-v5 authority.
    assert round_completion_issues(paired, 70) == []
    assert round_completion_issues(
        paired,
        70,
        natural_terminal_only=True,
    ) == ["official_terminal_socket_boundary_invalid"]


def test_terminal_thp_rejects_wire_prefix_and_footer_mismatches(tmp_path):
    wire_summary = {
        "hands_started_min": 70,
        "settlements_min": 69,
        "pending_expected_actions": [],
        "seats": {
            "A": {
                "name": "BotA",
                "hands_started": 70,
                "settlements": 69,
                "settlement_records": [
                    {"hand": hand, "amount": 50}
                    for hand in range(1, 70)
                ],
                "pending_expected_action": False,
            },
            "B": {
                "name": "BotB",
                "hands_started": 70,
                "settlements": 69,
                "settlement_records": [
                    {"hand": hand, "amount": -50}
                    for hand in range(1, 70)
                ],
                "pending_expected_action": False,
            },
        },
    }
    prefix_dir = tmp_path / "prefix"
    prefix_dir.mkdir()
    prefix_before = _snapshot_platform_thp_files(prefix_dir)
    (prefix_dir / "THP-prefix.txt").write_text(
        "STATE:0:f:AhKh|QsQd:60|-60:BotA|BotB;"
        + "".join(
            f"STATE:{index}:f:AhKh|QsQd:50|-50:BotA|BotB;"
            for index in range(1, 70)
        )
        + "{[THP][BotA][BotB][BotA赢得3510个筹码][2026-07-11 17:22 合肥][2018 CCGC]}",
        encoding="gb2312",
    )

    observation, issues = _terminal_thp_observation(
        prefix_dir,
        before=prefix_before,
        expected_hands=70,
        expected_names=("BotA", "BotB"),
        wire_summary=wire_summary,
    )
    assert observation is None
    assert issues == ["terminal_thp_wire_prefix_earnings_mismatch"]

    footer_dir = tmp_path / "footer"
    footer_dir.mkdir()
    footer_before = _snapshot_platform_thp_files(footer_dir)
    (footer_dir / "THP-footer.txt").write_text(
        "".join(
            f"STATE:{index}:f:AhKh|QsQd:50|-50:BotA|BotB;"
            for index in range(70)
        )
        + "{[THP][BotA][BotB][BotA赢得3499个筹码][2026-07-11 17:22 合肥][2018 CCGC]}",
        encoding="gb2312",
    )

    observation, issues = _terminal_thp_observation(
        footer_dir,
        before=footer_before,
        expected_hands=70,
        expected_names=("BotA", "BotB"),
        wire_summary=wire_summary,
    )
    assert observation is None
    assert any("thp_footer_result_mismatch" in issue for issue in issues)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda summary: summary.update(settlements_min=68),
        lambda summary: summary.update(hands_started_min=69),
        lambda summary: summary.update(pending_expected_actions=[{"conn": "A"}]),
        lambda summary: summary["seats"]["A"].update(pending_expected_action=True),
    ],
)
def test_terminal_socket_boundary_rejects_non_exact_or_pending_states(mutation):
    logs = {"hands_started_min": 70, "settlements_min": 69}
    wire = {
        "hands_started_min": 70,
        "settlements_min": 69,
        "pending_expected_actions": [],
        "seats": {
            "A": {
                "name": "BotA",
                "hands_started": 70,
                "settlements": 69,
                "settlement_records": [
                    {"hand": hand, "amount": 50}
                    for hand in range(1, 70)
                ],
                "pending_expected_action": False,
            },
            "B": {
                "name": "BotB",
                "hands_started": 70,
                "settlements": 69,
                "settlement_records": [
                    {"hand": hand, "amount": -50}
                    for hand in range(1, 70)
                ],
                "pending_expected_action": False,
            },
        },
    }
    mutation(wire)

    assert _terminal_socket_boundary(logs, wire, 70) is False


def test_canonical_thp_requires_exact_match_length_and_one_content_identity(tmp_path):
    first = tmp_path / "THP-first.txt"
    duplicate = tmp_path / "THP-duplicate.txt"
    overrun = tmp_path / "THP-overrun.txt"
    duplicate_index = tmp_path / "THP-duplicate-index.txt"
    exact_text = "\n".join(f"STATE:{index}:x:y:z:p;" for index in range(70)) + "\n"
    first.write_text(exact_text, encoding="gb2312")
    duplicate.write_text(exact_text, encoding="gb2312")
    overrun.write_text(
        "\n".join(f"STATE:{index}:x:y:z:p;" for index in range(71)) + "\n",
        encoding="gb2312",
    )
    duplicate_index.write_text(
        "\n".join(
            f"STATE:{index if index < 69 else 68}:x:y:z:p;"
            for index in range(70)
        )
        + "\n",
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

    canonical, issues = _canonical_thp_evidence(
        _summarize_thp_files([str(duplicate_index)]),
        expected_hands=70,
    )
    assert canonical["hand_records"] == 70
    assert any("thp_hand_index_sequence_mismatch" in issue for issue in issues)


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
