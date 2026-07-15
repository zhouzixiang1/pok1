import hashlib
import json
from pathlib import Path

import pytest

from bot_namespace import (
    STRICT_ARTIFACT_FILES,
    parse_bot_version,
    refresh_policy_identity_documents,
    strict_artifact_layout_errors,
)


IDENTITY = "a" * 64


def _write_jsonl(path, rows):
    path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )


def _write_strict_bot(root):
    root.mkdir(parents=True)
    payloads = {
        "national_bot.py": "from policy import decide\n",
        "precompute.py": "FACT = 1\n",
        "policy.py": "def decide(_context): return {'kind': 'pass'}\n",
        "national_runtime_manifest.json": "{}\n",
        "policy_epoch_receipt.json": "{}\n",
    }
    assert frozenset(payloads) == STRICT_ARTIFACT_FILES
    for relative, payload in payloads.items():
        (root / relative).write_text(payload, encoding="utf-8")
    version = parse_bot_version(root.name)
    assert version is not None
    refresh_policy_identity_documents(
        root,
        version,
        parent_versions=() if version == 143 else (version - 1,),
    )
    assert strict_artifact_layout_errors(root) == []


def _artifact_execution(*bots):
    from bot_artifact import hash_path
    from national_native import NativeBotSpec

    identities = {}
    for root in bots:
        identities[root.name] = NativeBotSpec(
            label=root.name,
            path=root,
            entry=root / "national_bot.py",
            artifact_hash=hash_path(root),
        ).execution_identity()
    return {
        "schema_version": 1,
        "mode": "direct_content_bound_policy_artifact",
        "by_player": identities,
    }


def _admitted_history_row(**overrides):
    row = {
        "bot0": "national_v143",
        "bot1": "national_v144",
        "bot0_wins": 1,
        "bot1_wins": 1,
        "draws": 0,
        "evaluation_epoch": "national_tcp_policy_v1",
        "execution_mode": "native_tcp",
        "evaluation_identity_digest": IDENTITY,
        "strength_sample_unit": "70_hand_match",
        "hands_per_strength_sample": 70,
        "strength_admitted": True,
        "strength_complete": True,
        "strength_compliance_passed": True,
        "strength_sample_count": 2,
        "net_chips_bot0": [500, -100],
    }
    row.update(overrides)
    return row


def test_70_hand_summary_uses_sign_first_and_amount_second():
    from strength_order import summarize_70_hand_net_chips

    summary = summarize_70_hand_net_chips([100, 1, -10_000, 0])

    assert summary["positive_matches"] == 2
    assert summary["negative_matches"] == 1
    assert summary["zero_matches"] == 1
    assert summary["primary_match_score"] == 0.625
    assert summary["secondary_net_chips_total"] == -9_899
    assert summary["secondary_net_chips_mean"] == -2_474.75


def test_match_score_counts_every_draw_as_half_a_point():
    from strength_order import match_score

    assert match_score(0, 8, 8) == 0.5
    assert match_score(2, 2, 5) == 0.6
    assert match_score(0, 0, 0) is None


def test_equal_primary_strength_is_broken_by_70_hand_chip_amount(tmp_path):
    from rating_snapshot import build_strength_rows

    ratings = {
        "national_v143": {"r": 1500, "rd": 80, "sigma": 0.06},
        "national_v144": {"r": 1500, "rd": 80, "sigma": 0.06},
    }
    stats = {
        "national_v143": {"games": 2, "win_rate": 0.5},
        "national_v144": {"games": 2, "win_rate": 0.5},
    }
    h2h = {
        "national_v143 vs national_v144": {
            "games": 2,
            "a_wins": 1,
            "b_wins": 1,
            "draws": 0,
        },
    }
    history = tmp_path / "match_history.jsonl"
    _write_jsonl(history, [_admitted_history_row()])

    rows = build_strength_rows(
        ratings,
        stats,
        h2h,
        active_bots=list(ratings),
        match_history_path=history,
        expected_evaluation_identity_digest=IDENTITY,
    )

    assert rows[0]["name"] == "national_v143"
    assert rows[0]["selection_score"] == rows[1]["selection_score"]
    assert rows[0]["secondary_net_chips_mean"] == 200.0
    assert rows[1]["secondary_net_chips_mean"] == -200.0
    assert rows[0]["strength_order_contract"] == [
        "70_hand_positive_result",
        "net_chips_magnitude",
    ]


def test_corrupt_chip_samples_do_not_enter_secondary_strength(tmp_path):
    from rating_snapshot import national_chip_metrics_from_match_history

    history = tmp_path / "match_history.jsonl"
    _write_jsonl(history, [_admitted_history_row(
        bot0_wins=2,
        bot1_wins=0,
        strength_sample_count=2,
        net_chips_bot0=[100, -50],
    )])

    assert national_chip_metrics_from_match_history(
        ["national_v143", "national_v144"],
        history,
        expected_evaluation_identity_digest=IDENTITY,
    ) == {}


def test_match_replay_persists_primary_and_secondary_contract(tmp_path, monkeypatch):
    import elo_daemon
    import evaluation_data_identity

    replay_dir = tmp_path / "replays"
    results_dir = tmp_path / "results"
    bots_dir = tmp_path / "bots"
    history = results_dir / "match_history.jsonl"
    _write_strict_bot(bots_dir / "national_v143")
    _write_strict_bot(bots_dir / "national_v144")
    artifact_execution = _artifact_execution(
        bots_dir / "national_v143",
        bots_dir / "national_v144",
    )
    monkeypatch.setattr(elo_daemon, "REPLAY_DIR", replay_dir)
    monkeypatch.setattr(elo_daemon, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(elo_daemon, "MATCH_HISTORY_FILE", history)
    monkeypatch.setattr(elo_daemon, "BOTS_DIR", bots_dir)
    monkeypatch.setattr(
        evaluation_data_identity,
        "current_evaluation_digest",
        lambda _root: "evaluation-digest",
    )

    name = elo_daemon.save_match_replay(
        "national_v143",
        "national_v144",
        1,
        1,
        0,
        [
            {
                "hands_played": 70,
                "passed_compliance": True,
                "artifact_execution": artifact_execution,
            },
            {
                "hands_played": 70,
                "passed_compliance": True,
                "artifact_execution": artifact_execution,
            },
        ],
        [500, -100],
        "70_hand_match",
    )

    replay = json.loads((replay_dir / name).read_text(encoding="utf-8"))
    summary = json.loads(history.read_text(encoding="utf-8"))
    assert replay["strength_order"]["primary_match_score"] == 0.5
    assert replay["strength_order"]["secondary_net_chips_mean"] == 200.0
    assert summary["net_chips_bot0"] == [500, -100]
    assert summary["strength_admitted"] is True
    assert summary["hands_per_strength_sample"] == 70


def _stage_current_identity_match(tmp_path, monkeypatch):
    import elo_daemon
    import evaluation_data_identity

    replay_dir = tmp_path / "replays"
    results_dir = tmp_path / "results"
    bots_dir = tmp_path / "bots"
    results_dir.mkdir()
    identity = evaluation_data_identity.ensure_evaluation_data_identity(
        results_dir
    )["manifest_digest"]
    _write_strict_bot(bots_dir / "national_v143")
    _write_strict_bot(bots_dir / "national_v144")
    artifact_execution = _artifact_execution(
        bots_dir / "national_v143",
        bots_dir / "national_v144",
    )
    monkeypatch.setattr(elo_daemon, "REPLAY_DIR", replay_dir)
    monkeypatch.setattr(elo_daemon, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(
        elo_daemon,
        "MATCH_HISTORY_FILE",
        results_dir / "match_history.jsonl",
    )
    monkeypatch.setattr(elo_daemon, "BOTS_DIR", bots_dir)
    monkeypatch.setattr(
        elo_daemon,
        "daemon_evaluation_identity_digest",
        identity,
    )
    admission = elo_daemon.save_match_replay(
        "national_v143",
        "national_v144",
        1,
        0,
        0,
        [{
            "hands_played": 70,
            "passed_compliance": True,
            "artifact_execution": artifact_execution,
        }],
        [500],
        "70_hand_match",
        expected_evaluation_identity_digest=identity,
        stage_only=True,
    )
    result = (
        "national_v143",
        "national_v144",
        1,
        0,
        0,
        1,
        None,
        [500],
        admission,
    )
    return elo_daemon, results_dir, replay_dir, identity, result


def test_staged_native_match_commits_exact_epoch_mode_and_identity(
    tmp_path,
    monkeypatch,
):
    from glicko2 import Glicko2Player

    elo_daemon, results_dir, replay_dir, identity, result = (
        _stage_current_identity_match(tmp_path, monkeypatch)
    )
    ratings = {
        name: Glicko2Player(r=1500, rd=350, sigma=0.06)
        for name in ("national_v143", "national_v144")
    }
    h2h = {}
    bot_stats = {}

    admitted = elo_daemon.admit_internal_match_result(
        result,
        ratings,
        h2h,
        bot_stats,
    )

    assert admitted == 1
    summary = json.loads(
        (results_dir / "match_history.jsonl").read_text(encoding="utf-8")
    )
    assert summary["evaluation_epoch"] == "national_tcp_policy_v1"
    assert summary["execution_mode"] == "native_tcp"
    assert summary["evaluation_identity_digest"] == identity
    assert (replay_dir / summary["id"]).is_file()
    assert h2h["national_v143 vs national_v144"]["a_wins"] == 1


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("evaluation_epoch", "national_native_v1", "evaluation epoch mismatch"),
        ("execution_mode", "official_exe", "execution mode mismatch"),
        ("execution_mode", "national_arena", "execution mode mismatch"),
    ],
)
def test_staged_match_rejects_resigned_foreign_epoch_or_mode(
    tmp_path,
    monkeypatch,
    field,
    value,
    error,
):
    from glicko2 import Glicko2Player

    elo_daemon, results_dir, _replay_dir, _identity, result = (
        _stage_current_identity_match(tmp_path, monkeypatch)
    )
    admission = result[8]
    pending = Path(admission["pending_path"])
    payload = json.loads(pending.read_text(encoding="utf-8"))
    payload[field] = value
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    pending.write_bytes(raw)
    admission["replay_bytes"] = len(raw)
    admission["replay_sha256"] = hashlib.sha256(raw).hexdigest()
    admission["summary"][field] = value
    ratings = {
        name: Glicko2Player(r=1500, rd=350, sigma=0.06)
        for name in ("national_v143", "national_v144")
    }

    with pytest.raises(RuntimeError, match=error):
        elo_daemon.admit_internal_match_result(result, ratings, {}, {})

    assert not (results_dir / "match_history.jsonl").exists()


def test_precommit_outcome_gate_rejects_tiny_0w_8l_collapse():
    from strength_order import precommit_outcome_blockers

    blockers, summary = precommit_outcome_blockers([{
        "opponent": "national_v143",
        "wins": 0,
        "losses": 8,
        "draws": 0,
        "net_chips": [-1] * 8,
    }], parent_label="national_v143")

    assert summary["primary_match_score"] == 0.0
    assert {row["reason"] for row in blockers} == {
        "lost_to_parent",
        "aggregate_native_regression",
    }


def test_precommit_outcome_gate_keeps_9w_7l_despite_huge_negative_chips():
    from strength_order import precommit_outcome_blockers

    blockers, summary = precommit_outcome_blockers([{
        "opponent": "national_v143",
        "wins": 9,
        "losses": 7,
        "draws": 0,
        "net_chips": [1] * 9 + [-100_000] * 7,
    }], parent_label="national_v143")

    assert summary["primary_match_score"] == 9 / 16
    assert blockers == []


def test_legacy_magic_reason_cannot_bypass_primary_outcome_gate():
    from strength_order import precommit_outcome_blockers

    blockers, summary = precommit_outcome_blockers([
        {
            "opponent": "national_v143",
            "reason": "parent",
            "wins": 8,
            "losses": 0,
            "draws": 0,
        },
        {
            "opponent": "national_v144",
            "reason": "nemesis_probe",
            "wins": 0,
            "losses": 100,
            "draws": 0,
        },
    ], parent_label="national_v143")

    assert {row["reason"] for row in blockers} == {"aggregate_native_regression"}
    assert summary["wins"] == 8
    assert summary["losses"] == 100
    assert summary["samples"] == 108


def test_draws_score_half_in_bot_stats():
    from evolution_infra import update_bot_stats

    stats = {}
    update_bot_stats(stats, "national_v143", wins=1, losses=1, draws=2)

    assert stats["national_v143"] == {
        "wins": 1,
        "losses": 1,
        "draws": 2,
        "games": 4,
        "win_rate": 0.5,
    }


def test_history_reconstruction_rejects_unproven_incomplete_or_failed_rows(tmp_path):
    from rating_snapshot import reconstruct_h2h_from_match_history

    history = tmp_path / "match_history.jsonl"
    _write_jsonl(history, [
        _admitted_history_row(strength_complete=False),
        _admitted_history_row(strength_compliance_passed=False),
        _admitted_history_row(hands_per_strength_sample=69),
        _admitted_history_row(strength_admitted=False),
        _admitted_history_row(),
    ])

    rebuilt = reconstruct_h2h_from_match_history(
        ["national_v143", "national_v144"],
        history,
        expected_evaluation_identity_digest=IDENTITY,
    )
    assert rebuilt["national_v143 vs national_v144"] == {
        "games": 2,
        "a_wins": 1,
        "b_wins": 1,
        "draws": 0,
        "win_rate": 0.5,
    }


def test_status_h2h_treats_all_draws_as_neutral(tmp_path, monkeypatch):
    import asyncio
    import evolution_infra
    import tool_status

    h2h_file = tmp_path / "head_to_head.json"
    h2h_file.write_text(json.dumps({
        "national_v143 vs national_v144": {
            "games": 10,
            "a_wins": 0,
            "b_wins": 0,
            "draws": 10,
            "win_rate": 0.0,
        },
    }), encoding="utf-8")
    monkeypatch.setattr(tool_status, "_infra_path", lambda _name: h2h_file)
    monkeypatch.setattr(evolution_infra, "read_pipeline_checkpoint", lambda: {})

    wrapped = asyncio.run(tool_status.get_h2h.handler({"bot_name": "national_v143"}))
    result = json.loads(wrapped["content"][0]["text"])

    assert result["opponents"]["national_v144"]["win_rate"] == 0.5
    assert result["opponents"]["national_v144"]["draws"] == 10
    assert result["opponents"]["national_v144"]["tag"] == "neutral"
