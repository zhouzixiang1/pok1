import json
from pathlib import Path

from official_evidence import build_official_evidence_bundle, build_official_evidence_from_summary


def _write_jsonl(path: Path, rows):
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


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
