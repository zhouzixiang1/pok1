import json
from pathlib import Path

from official_evidence import build_official_evidence_bundle, build_official_evidence_from_summary
from official_wire_probe import WireEventRecorder, replay_events


def _write_jsonl(path: Path, rows):
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


def _causal_evidence_fixture(root: Path):
    round_dir = root / "suite" / "self_play_01"
    round_dir.mkdir(parents=True)
    wire_events = round_dir / "wire_events.jsonl"
    recorder = WireEventRecorder(wire_events)
    try:
        recorder.record(
            conn="A",
            direction="server_to_bot",
            raw=b"preflop|SMALLBLIND|<0,3><1,4>",
            messages=["preflop|SMALLBLIND|<0,3><1,4>"],
            remaining="",
        )
        recorder.record(
            conn="*",
            direction="probe_lifecycle",
            raw=b"",
            messages=[],
            remaining="",
            event_type="capture_finalized",
        )
        events = list(recorder.events)
        stored = replay_events(events, finalized=True)
    finally:
        recorder.close()
    replay_summary = round_dir / "replay_summary.json"
    replay_summary.write_text(
        json.dumps(stored, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    report = {
        "candidate": "/tmp/national_v1",
        "opponent": "/tmp/national_v2",
        "summary": {
            "suite_dir": str(root / "suite"),
            "rounds_requested": 1,
            "rounds_run": 1,
            "target_hands": 70,
        },
        "rounds": [{
            "round_id": "self_play_01",
            "round_kind": "self_play",
            "round_index": 1,
            "target_hands": 70,
            "passed": False,
            "issues": [],
            "wire_probe": {
                "enabled": True,
                "issues": [],
                "causal_order_schema_version": 1,
                "finalized_replay_required": True,
            },
            "log_summary": {
                "hands_started_min": 1,
                "settlements_min": 0,
                "issues": [],
            },
            "artifacts": {
                "round_dir": str(round_dir),
                "wire_events": str(wire_events),
                "replay_summary": str(replay_summary),
                "thp_files": [],
                "thp_summaries": [],
            },
        }],
        "issues": [],
    }
    return report, events, stored, wire_events, replay_summary


def test_evidence_bundle_replays_wire_events_and_classifies_protocol_issue(tmp_path):
    round_dir = tmp_path / "suite" / "self_play_01"
    round_dir.mkdir(parents=True)
    wire_events = round_dir / "wire_events.jsonl"
    bot_a_log = round_dir / "botA.log"
    bot_b_log = round_dir / "botB.log"
    thp_file = round_dir / "thp" / "THP-BotA vs BotB-BotA胜-202607101200-CCGC.txt"
    thp_file.parent.mkdir()
    thp_file.write_bytes("STATE:1:r200:AhKd:50:BotA|BotB;\n".encode("gb2312"))
    bot_a_log.write_text("[10:00:00] DISPATCH line='preflop|SMALLBLIND|<0,3><1,4>'\n", encoding="utf-8")
    bot_b_log.write_text("[10:00:00] DISPATCH line='preflop|BIGBLIND|<0,5><1,6>'\n", encoding="utf-8")
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
    report = {
        "candidate": "/tmp/national_v1",
        "opponent": "/tmp/national_v2",
        "summary": {
            "suite_dir": str(tmp_path / "suite"),
            "rounds_requested": 1,
            "rounds_run": 1,
            "target_hands": 70,
        },
        "rounds": [
            {
                "round_id": "self_play_01",
                "round_kind": "self_play",
                "round_index": 1,
                "target_hands": 70,
                "passed": False,
                "issues": [],
                "log_summary": {"hands_started_min": 1, "settlements_min": 0, "issues": []},
                "artifacts": {
                    "round_dir": str(round_dir),
                    "bot_a_log": str(bot_a_log),
                    "bot_b_log": str(bot_b_log),
                    "wire_events": str(wire_events),
                    "thp_files": [str(thp_file)],
                    "thp_summaries": [{"path": str(thp_file), "hand_records": 1, "bytes": thp_file.stat().st_size}],
                },
            }
        ],
        "issues": [],
    }

    bundle = build_official_evidence_bundle(report, output_path=tmp_path / "official_evidence.json")

    assert bundle["strength_evaluation"] == "not_applicable"
    assert bundle["deterministic"]["blocking"] is True
    assert bundle["deterministic"]["classification"] == "protocol"
    assert any("wire_replay: illegal_check" in issue for issue in bundle["deterministic"]["issues"])
    assert bundle["rounds"][0]["wire_replay_summary"]["issues"][0]["kind"] == "illegal_check"
    assert (round_dir / "replay_summary.json").exists()
    assert (tmp_path / "official_evidence.json").exists()


def test_evidence_bundle_replays_idle_commit_in_raw_wire_causal_order(tmp_path):
    round_dir = tmp_path / "suite" / "self_play_01"
    round_dir.mkdir(parents=True)
    wire_events = round_dir / "wire_events.jsonl"
    recorder = WireEventRecorder(wire_events)
    try:
        recorder.record(
            conn="A",
            direction="server_to_bot",
            raw=b"preflop|SMALLBLIND|<0,3><1,4>",
            messages=["preflop|SMALLBLIND|<0,3><1,4>"],
            remaining="",
        )
        raw_call = recorder.record(
            conn="A",
            direction="bot_to_server",
            raw=b"call",
            messages=[],
            remaining="call",
        )
        recorder.record(
            conn="A",
            direction="server_to_bot",
            raw=b"flop|<0,5><1,6><2,7>",
            messages=["flop|<0,5><1,6><2,7>"],
            remaining="",
        )
        recorder.record(
            conn="A",
            direction="bot_to_server",
            raw=b"",
            messages=["call"],
            remaining="",
            event_type="idle_flush",
            observation_seq=raw_call["observation_seq"],
            observation_t=raw_call["observation_t"],
            deferred_parser_mode="client_action",
        )
        recorder.record(
            conn="*",
            direction="probe_lifecycle",
            raw=b"",
            messages=[],
            remaining="",
            event_type="capture_finalized",
        )
        stored_summary = replay_events(recorder.events, finalized=True)
    finally:
        recorder.close()
    replay_summary_path = round_dir / "replay_summary.json"
    replay_summary_path.write_text(
        json.dumps(stored_summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    report = {
        "candidate": "/tmp/national_v1",
        "opponent": "/tmp/national_v2",
        "summary": {
            "suite_dir": str(tmp_path / "suite"),
            "rounds_requested": 1,
            "rounds_run": 1,
            "target_hands": 70,
        },
        "rounds": [
            {
                "round_id": "self_play_01",
                "round_kind": "self_play",
                "round_index": 1,
                "target_hands": 70,
                "passed": False,
                "issues": [],
                "wire_probe": {
                    "enabled": True,
                    "issues": [],
                    "causal_order_schema_version": 1,
                    "finalized_replay_required": True,
                },
                "log_summary": {
                    "hands_started_min": 1,
                    "settlements_min": 0,
                    "issues": [],
                },
                "artifacts": {
                    "round_dir": str(round_dir),
                    "wire_events": str(wire_events),
                    "replay_summary": str(replay_summary_path),
                    "thp_files": [],
                    "thp_summaries": [],
                },
            }
        ],
        "issues": [],
    }

    bundle = build_official_evidence_bundle(
        report,
        output_path=tmp_path / "official_evidence.json",
    )

    replay = bundle["rounds"][0]["wire_replay_summary"]
    assert replay["issues"] == []
    assert replay["seats"]["A"]["current_stage"] == "flop"
    assert replay["seats"]["A"]["hand_actions"][0]["stage"] == "preflop"
    assert replay["seats"]["A"]["hand_actions"][0]["action_type"] == "call"


def test_causal_evidence_rejects_stored_summary_mismatch(tmp_path):
    report, _events, stored, _wire_events, replay_summary = (
        _causal_evidence_fixture(tmp_path)
    )
    tampered = dict(stored)
    tampered["warnings"] = [
        {"kind": "fabricated_clean_warning", "reason": "not raw-derived"}
    ]
    replay_summary.write_text(json.dumps(tampered), encoding="utf-8")

    replay = build_official_evidence_bundle(report)["rounds"][0][
        "wire_replay_summary"
    ]

    assert any(
        issue.get("kind") == "wire_replay_stored_summary_mismatch"
        for issue in replay["issues"]
    )


def test_causal_evidence_rejects_fabricated_omitted_runout_boundary(tmp_path):
    report, _events, stored, _wire_events, replay_summary = (
        _causal_evidence_fixture(tmp_path)
    )
    tampered = dict(stored)
    tampered["omitted_allin_runout_boundaries"] = [{
        "conn": "A",
        "hand": 1,
        "stage": "preflop",
        "public_cards_observed": 0,
        "natural_hand_70": False,
    }]
    replay_summary.write_text(json.dumps(tampered), encoding="utf-8")

    replay = build_official_evidence_bundle(report)["rounds"][0][
        "wire_replay_summary"
    ]

    assert any(
        issue.get("kind") == "wire_replay_stored_summary_mismatch"
        for issue in replay["issues"]
    )


def test_causal_evidence_rejects_receipt_marker_stripping(tmp_path):
    report, _events, _stored, _wire_events, _replay_summary = (
        _causal_evidence_fixture(tmp_path)
    )
    report["rounds"][0]["wire_probe"] = {"enabled": True, "issues": []}

    replay = build_official_evidence_bundle(report)["rounds"][0][
        "wire_replay_summary"
    ]

    assert replay["issues"][0]["kind"] == (
        "wire_probe_causal_receipt_marker_missing"
    )


def test_schema_v1_receipt_rejects_legacy_or_mixed_raw_events(tmp_path):
    report, events, _stored, wire_events, _replay_summary = (
        _causal_evidence_fixture(tmp_path)
    )
    legacy = {
        "t": 2.0,
        "dt": 2.0,
        "conn": "A",
        "direction": "bot_to_server",
        "messages": ["call"],
    }
    _write_jsonl(wire_events, [events[0], legacy, events[-1]])

    replay = build_official_evidence_bundle(report)["rounds"][0][
        "wire_replay_summary"
    ]

    assert replay["issues"][0]["kind"] == (
        "wire_probe_causal_event_envelope_missing"
    )


def test_evidence_from_raw_summary_infers_pass_without_strength_rating(tmp_path):
    suite = tmp_path / "suite"
    round_dir = suite / "self_play_01"
    round_dir.mkdir(parents=True)
    summary_path = suite / "summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "candidate": "/tmp/national_v1",
                "opponent": None,
                "summary": {
                    "suite_dir": str(suite),
                    "rounds_requested": 1,
                    "rounds_run": 1,
                    "target_hands": 10,
                },
                "rounds": [
                    {
                        "round_id": "self_play_01",
                        "round_kind": "self_play",
                        "round_index": 1,
                        "target_hands": 10,
                        "passed": True,
                        "issues": [],
                        "log_summary": {"hands_started_min": 10, "settlements_min": 9, "issues": []},
                        "artifacts": {"round_dir": str(round_dir), "thp_summaries": []},
                    }
                ],
                "issues": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    bundle = build_official_evidence_from_summary(summary_path)

    assert bundle["summary"]["passed"] is True
    assert bundle["deterministic"]["classification"] == "pass"
    assert bundle["deterministic"]["blocking"] is False
    assert "rating" not in json.dumps(bundle, ensure_ascii=False).lower()


def test_full_evidence_requires_wire_probe_artifacts_when_probe_enabled(tmp_path):
    suite = tmp_path / "suite"
    round_dir = suite / "self_play_01"
    round_dir.mkdir(parents=True)
    report = {
        "candidate": "/tmp/national_v1",
        "opponent": None,
        "passed": True,
        "summary": {
            "suite_dir": str(suite),
            "rounds_requested": 1,
            "rounds_run": 1,
            "target_hands": 70,
        },
        "rounds": [
            {
                "round_id": "self_play_01",
                "round_kind": "self_play",
                "round_index": 1,
                "target_hands": 70,
                "passed": True,
                "issues": [],
                "wire_probe": {"enabled": True, "issues": []},
                "log_summary": {"hands_started_min": 70, "settlements_min": 69, "issues": []},
                "artifacts": {
                    "round_dir": str(round_dir),
                    "thp_summaries": [{"path": "fake.thp", "hand_records": 70}],
                },
            }
        ],
        "issues": [],
    }

    bundle = build_official_evidence_bundle(report)

    assert bundle["summary"]["raw_passed"] is True
    assert bundle["summary"]["passed"] is False
    assert bundle["summary"]["wire_evidence_required_rounds"] == 1
    assert bundle["summary"]["wire_evidence_complete_rounds"] == 0
    assert bundle["deterministic"]["classification"] == "harness"
    assert bundle["deterministic"]["inconclusive"] is True
    assert any("wire_probe_missing_wire_events_artifact" in issue for issue in bundle["deterministic"]["issues"])
    assert any("wire_probe_missing_replay_summary_artifact" in issue for issue in bundle["rounds"][0]["issues"])


def test_evidence_rejects_artifact_outside_suite(tmp_path):
    suite = tmp_path / "suite"
    round_dir = suite / "self_play_01"
    round_dir.mkdir(parents=True)
    external = tmp_path / "external-platform.log"
    external.write_text("platform output", encoding="utf-8")
    report = {
        "candidate": "/tmp/national_v1",
        "passed": True,
        "summary": {
            "suite_dir": str(suite),
            "rounds_requested": 1,
            "rounds_run": 1,
            "target_hands": 10,
        },
        "rounds": [{
            "round_id": "self_play_01",
            "round_kind": "self_play",
            "round_index": 1,
            "target_hands": 10,
            "passed": True,
            "issues": [],
            "log_summary": {"hands_started_min": 10, "settlements_min": 9},
            "artifacts": {"platform_log": str(external)},
        }],
        "issues": [],
    }

    bundle = build_official_evidence_bundle(report)

    artifact = bundle["rounds"][0]["artifacts"]["platform_log"]
    assert artifact["issue"] == "artifact_outside_suite"
    assert "archive_path" not in artifact
    assert bundle["summary"]["passed"] is False
    assert bundle["deterministic"]["inconclusive"] is True
    assert any(
        "evidence_artifact_platform_log:artifact_outside_suite" in issue
        for issue in bundle["deterministic"]["issues"]
    )


def test_evidence_keeps_ambiguous_full_incomplete_round_inconclusive(tmp_path):
    report = {
        "candidate": "/tmp/national_v134",
        "summary": {
            "suite_dir": str(tmp_path / "suite"),
            "rounds_requested": 1,
            "rounds_run": 1,
            "target_hands": 70,
        },
        "rounds": [
            {
                "round_id": "self_play_02",
                "round_kind": "self_play",
                "round_index": 2,
                "target_hands": 70,
                "passed": False,
                "issues": [
                    "BotA_exited_early: rc=0",
                    "thp_missing_for_full_70_hand_round",
                    "official_full_round_incomplete_after_progress: hands_started=33 settlements=32 target=70 max_abs_net_chips=19466",
                ],
                "log_summary": {
                    "hands_started_min": 33,
                    "settlements_min": 32,
                    "bot_a": {"net_chips": -19466},
                    "bot_b": {"net_chips": 19466},
                    "issues": [],
                },
                "artifacts": {"round_dir": str(tmp_path / "suite" / "self_play_02"), "thp_summaries": []},
            }
        ],
        "issues": [],
    }

    bundle = build_official_evidence_bundle(report)

    assert bundle["deterministic"]["classification"] == "harness"
    assert bundle["deterministic"]["blocking"] is False
    assert bundle["deterministic"]["inconclusive"] is True
    assert bundle["deterministic"]["round_classifications"][0]["classification"] == "harness"
    assert bundle["deterministic"]["attribution"]["candidate_verdict"] == "inconclusive"
    assert bundle["strength_evaluation"] == "not_applicable"


def test_evidence_classifies_zero_hand_connection_reset_as_harness_inconclusive(tmp_path):
    report = {
        "candidate": "/tmp/national_v134",
        "summary": {
            "suite_dir": str(tmp_path / "suite"),
            "rounds_requested": 1,
            "rounds_run": 1,
            "target_hands": 70,
        },
        "rounds": [
            {
                "round_id": "self_play_04",
                "round_kind": "self_play",
                "round_index": 4,
                "target_hands": 70,
                "passed": False,
                "issues": [
                    "BotA_exited_early: rc=2",
                    "botA.stderr.log: ConnectionResetError: [Errno 104] Connection reset by peer",
                    "official_full_round_no_game_progress: target=70",
                ],
                "log_summary": {"hands_started_min": 0, "settlements_min": 0, "issues": []},
                "artifacts": {"round_dir": str(tmp_path / "suite" / "self_play_04"), "thp_summaries": []},
            }
        ],
        "issues": [],
    }

    bundle = build_official_evidence_bundle(report)

    assert bundle["deterministic"]["classification"] == "harness"
    assert bundle["deterministic"]["blocking"] is False
    assert bundle["deterministic"]["inconclusive"] is True
    assert bundle["deterministic"]["round_classifications"][0]["classification"] == "harness"
