"""Replay spotlight accepts only strict native replay artifacts."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

import replay_spotlight
from test_logic_replay_analysis import IDENTITY, make_strict_replay


def test_spotlight_ranks_native_hand_and_binds_payload(tmp_path):
    replay_dir = tmp_path / "match_replay"
    replay_dir.mkdir()
    replay = make_strict_replay("strict.json")
    replay_dir.joinpath("strict.json").write_text(json.dumps(replay), encoding="utf-8")
    evidence = replay_spotlight.build_critical_hands_evidence(
        "national_v143",
        replay_dir,
        max_hands=1,
        allowed_replay_ids=["strict.json"],
        expected_evaluation_identity_digest=IDENTITY,
    )

    assert "G1H1#" in evidence["text"]
    assert "delta=+100" in evidence["text"]
    assert evidence["schema_version"] == 2
    assert evidence["epoch"] == "national_tcp_policy_v1"
    assert evidence["evaluation_identity_digest"] == IDENTITY
    citation = evidence["citations"][0]
    assert citation["replay_file"] == "strict.json"
    assert len(citation["replay_sha256"]) == 64
    assert set(citation["artifact_identity_digests"]) == {
        "national_v143", "national_v144"
    }


def test_pure_spotlight_builder_returns_hash_bound_payload_without_shared_write(
    tmp_path,
):
    replay_dir = tmp_path / "match_replay"
    replay_dir.mkdir()
    replay_dir.joinpath("strict.json").write_text(
        json.dumps(make_strict_replay("strict.json")), encoding="utf-8"
    )

    evidence = replay_spotlight.build_critical_hands_evidence(
        "national_v143",
        replay_dir,
        max_hands=1,
        allowed_replay_ids=["strict.json"],
        expected_evaluation_identity_digest=IDENTITY,
    )

    assert "G1H1#" in evidence["text"]
    assert evidence["bot"] == "national_v143"
    assert evidence["evaluation_identity_digest"] == IDENTITY
    assert len(evidence["source_replays"]["strict.json"]["sha256"]) == 64
    assert not (tmp_path / "results" / "spotlight_manifest.json").exists()


def test_spotlight_skips_retired_or_wrong_identity_replay(tmp_path):
    replay_dir = tmp_path / "match_replay"
    replay_dir.mkdir()
    replay_dir.joinpath("retired.json").write_text(json.dumps({
        "bot0": "national_v143",
        "bot1": "national_v144",
        "games": [{"logs": [{"output": {"response": 0}}]}],
    }), encoding="utf-8")
    strict = make_strict_replay("strict.json")
    replay_dir.joinpath("strict.json").write_text(json.dumps(strict), encoding="utf-8")
    evidence = replay_spotlight.build_critical_hands_evidence(
        "national_v143",
        replay_dir,
        allowed_replay_ids=["retired.json", "strict.json"],
        expected_evaluation_identity_digest="f" * 64,
    )

    assert evidence["text"] == ""
    assert evidence["citations"] == []
    assert not (tmp_path / "results" / "spotlight_manifest.json").exists()


def test_spotlight_fails_closed_without_proven_current_identity(tmp_path):
    replay_dir = tmp_path / "match_replay"
    replay_dir.mkdir()
    replay_dir.joinpath("strict.json").write_text(
        json.dumps(make_strict_replay("strict.json")), encoding="utf-8"
    )
    evidence = replay_spotlight.build_critical_hands_evidence(
        "national_v143",
        replay_dir,
        expected_evaluation_identity_digest="",
    )

    assert evidence["text"] == ""
    assert evidence["evaluation_identity_digest"] == "unavailable"
    assert evidence["citations"] == []
    assert not (tmp_path / "results" / "spotlight_manifest.json").exists()
