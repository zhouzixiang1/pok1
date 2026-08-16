import hashlib
import json
from pathlib import Path

import pytest

from bot_namespace import (
    FIRST_STRICT_POLICY_VERSION,
    STRICT_ARTIFACT_FILES,
    bot_name,
    parse_bot_version,
    refresh_policy_identity_documents,
    strict_artifact_layout_errors,
)
from conftest import STRICT_TARGET_V, strict_bot_name


IDENTITY = "a" * 64

# Branch-portable strict-policy bot labels for the two-bot match fixtures.
# BOT_A is the first strict candidate (the fresh-bootstrap floor); BOT_B is its
# strict child. These resolve to national_v143/national_v144 on main and
# national_cloud_v1/national_cloud_v2 on the tencent-cloud-runtime branch, so
# the same tests pass in either namespace.
BOT_A = strict_bot_name()
BOT_B = bot_name(STRICT_TARGET_V + 1)


@pytest.fixture(autouse=True)
def _unit_history_rows_have_a_verified_raw_replay(monkeypatch):
    """Keep strength-order tests focused on aggregation, not replay parsing."""

    import rating_snapshot

    monkeypatch.setattr(
        rating_snapshot,
        "_load_verified_history_replay",
        lambda entry, **_kwargs: dict(entry),
    )


def _rating_timing_plan():
    from national_native import build_native_match_timing_plan

    return build_native_match_timing_plan(hands=70, requested_timeout_sec=None)


def _timing_evidence(plan):
    return {
        "native_match_timing_plan": plan.snapshot(),
        "native_match_timing_plan_digest": plan.digest(),
        "native_full_match_liveness_budget": plan.liveness_budget_snapshot(),
        "native_match_timeout_phase": None,
        "native_terminal_abort": None,
    }


def _complete_native_game(
    *,
    bot_a,
    bot_b,
    artifact_execution,
    timing_plan,
    net_chips_a,
):
    """Small but complete 70-hand raw envelope for admission tests."""

    settlements = []
    hand_records = []
    for hand in range(1, 71):
        earnings = [net_chips_a, -net_chips_a] if hand == 1 else [0, 0]
        settlement = {
            "hand": hand,
            "earnings": earnings,
            "pot": max(150, abs(net_chips_a) * 2),
            "is_showdown": False,
            "winner_idx": 0 if earnings[0] > 0 else (1 if earnings[1] > 0 else None),
            "reason": "fold",
        }
        settlements.append(settlement)
        hand_records.append({
            "hand": hand,
            "sb_idx": hand % 2,
            "bb_idx": 1 - (hand % 2),
            "hole_cards": [
                ["<0,12>", "<2,11>"],
                ["<3,10>", "<1,9>"],
            ],
            "board": [],
            "actions": [],
            "settlement": {
                key: value for key, value in settlement.items() if key != "hand"
            },
        })
    return {
        "execution_mode": "native_tcp",
        "bot_a": bot_a,
        "bot_b": bot_b,
        "hands_requested": 70,
        "hands_played": 70,
        "net_chips_a": net_chips_a,
        "net_chips_b": -net_chips_a,
        "passed_compliance": True,
        "issues": [],
        "artifact_execution": artifact_execution,
        "settlements": settlements,
        "hand_records": hand_records,
        **_timing_evidence(timing_plan),
    }


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
        parent_versions=() if version == FIRST_STRICT_POLICY_VERSION else (version - 1,),
    )
    assert strict_artifact_layout_errors(root) == []


def _artifact_execution(*bots):
    from bot_artifact import canonical_digest, hash_path
    from bot_namespace import artifact_contract_digest
    from national_native import NativeBotSpec

    identities = {}
    for root in bots:
        runtime_manifest = json.loads(
            (root / "national_runtime_manifest.json").read_text(encoding="utf-8")
        )
        epoch_receipt = json.loads(
            (root / "policy_epoch_receipt.json").read_text(encoding="utf-8")
        )
        core_digests = dict(runtime_manifest.get("files") or {})
        identities[root.name] = NativeBotSpec(
            label=root.name,
            path=root,
            entry=root / "national_bot.py",
            artifact_hash=hash_path(root),
            entry_digest=str(core_digests["national_bot.py"]),
            policy_digest=str(core_digests["policy.py"]),
            precompute_digest=str(core_digests["precompute.py"]),
            runtime_manifest_digest=canonical_digest(runtime_manifest),
            artifact_contract_digest=artifact_contract_digest(runtime_manifest),
            epoch_receipt_digest=canonical_digest(epoch_receipt),
        ).execution_identity()
    return {
        "schema_version": 1,
        "mode": "direct_content_bound_policy_artifact",
        "by_player": identities,
    }


def _admitted_history_row(**overrides):
    timing_plan = _rating_timing_plan()
    row = {
        "bot0": BOT_A,
        "bot1": BOT_B,
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
        "native_match_timing_plan": timing_plan.snapshot(),
        "native_match_timing_plan_digest": timing_plan.digest(),
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
        BOT_A: {"r": 1500, "rd": 80, "sigma": 0.06},
        BOT_B: {"r": 1500, "rd": 80, "sigma": 0.06},
    }
    stats = {
        BOT_A: {"games": 2, "win_rate": 0.5},
        BOT_B: {"games": 2, "win_rate": 0.5},
    }
    h2h = {
        f"{BOT_A} vs {BOT_B}": {
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

    assert rows[0]["name"] == BOT_A
    # Since 2026-08-16 net chips are a weighted selection-score component
    # (fold-heavy W/L winners no longer outrank chip earners), so equal-W/L
    # bots with different chip means now differ slightly in the score itself;
    # the chip tiebreaker in strength_order_key remains as the backstop.
    assert rows[0]["selection_score"] > rows[1]["selection_score"]
    assert rows[0]["rank_basis"].endswith("_plus_net_chips")
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
        [BOT_A, BOT_B],
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
    _write_strict_bot(bots_dir / BOT_A)
    _write_strict_bot(bots_dir / BOT_B)
    artifact_execution = _artifact_execution(
        bots_dir / BOT_A,
        bots_dir / BOT_B,
    )
    monkeypatch.setattr(elo_daemon, "REPLAY_DIR", replay_dir)
    monkeypatch.setattr(elo_daemon, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(elo_daemon, "MATCH_HISTORY_FILE", history)
    monkeypatch.setattr(elo_daemon, "BOTS_DIR", bots_dir)
    monkeypatch.setattr(
        evaluation_data_identity,
        "current_evaluation_digest",
        lambda _root: IDENTITY,
    )
    timing_plan = _rating_timing_plan()

    name = elo_daemon.save_match_replay(
        BOT_A,
        BOT_B,
        1,
        1,
        0,
        [
            _complete_native_game(
                bot_a=BOT_A,
                bot_b=BOT_B,
                artifact_execution=artifact_execution,
                timing_plan=timing_plan,
                net_chips_a=500,
            ),
            _complete_native_game(
                bot_a=BOT_A,
                bot_b=BOT_B,
                artifact_execution=artifact_execution,
                timing_plan=timing_plan,
                net_chips_a=-100,
            ),
        ],
        [500, -100],
        "70_hand_match",
        expected_native_match_timing_plan=timing_plan.snapshot(),
    )

    replay = json.loads((replay_dir / name).read_text(encoding="utf-8"))
    summary = json.loads(history.read_text(encoding="utf-8"))
    assert replay["strength_order"]["primary_match_score"] == 0.5
    assert replay["strength_order"]["secondary_net_chips_mean"] == 200.0
    assert summary["net_chips_bot0"] == [500, -100]
    assert summary["strength_admitted"] is True
    assert summary["hands_per_strength_sample"] == 70


def test_rating_admission_rejects_typed_native_terminal_abort(tmp_path, monkeypatch):
    import elo_daemon
    import evaluation_data_identity

    timing_plan = _rating_timing_plan()
    monkeypatch.setattr(elo_daemon, "REPLAY_DIR", tmp_path / "replays")
    monkeypatch.setattr(elo_daemon, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(
        evaluation_data_identity,
        "current_evaluation_digest",
        lambda _root: IDENTITY,
    )
    aborted = {
        "hands_played": 70,
        "passed_compliance": True,
        **_timing_evidence(timing_plan),
        "native_terminal_abort": {"code": "engine_betting_round_action_limit_exceeded"},
    }
    with pytest.raises(ValueError, match="timing evidence invalid"):
        elo_daemon.save_match_replay(
            BOT_A,
            BOT_B,
            1,
            0,
            0,
            [aborted],
            [10],
            "70_hand_match",
            expected_native_match_timing_plan=timing_plan.snapshot(),
        )


def test_rating_replay_producer_rejects_nonstrength_receipt_before_staging(
    tmp_path, monkeypatch
):
    import elo_daemon
    import evaluation_data_identity

    monkeypatch.setattr(elo_daemon, "REPLAY_DIR", tmp_path / "replays")
    monkeypatch.setattr(elo_daemon, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(
        evaluation_data_identity,
        "current_evaluation_digest",
        lambda _root: IDENTITY,
    )

    with pytest.raises(ValueError, match="requires an exact 70_hand_match"):
        elo_daemon.save_match_replay(
            BOT_A,
            BOT_B,
            1,
            0,
            0,
            [],
            [],
            None,
            stage_only=True,
        )

    assert not (tmp_path / "replays" / ".pending").exists()


def test_rating_replay_producer_rejects_symlinked_replay_root(
    tmp_path, monkeypatch
):
    import elo_daemon
    import evaluation_data_identity

    target = tmp_path / "outside"
    target.mkdir()
    replay_link = tmp_path / "replays"
    replay_link.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(elo_daemon, "REPLAY_DIR", replay_link)
    monkeypatch.setattr(elo_daemon, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(
        evaluation_data_identity,
        "current_evaluation_digest",
        lambda _root: IDENTITY,
    )

    with pytest.raises(RuntimeError, match="replay directory is unsafe"):
        elo_daemon.save_match_replay(
            BOT_A,
            BOT_B,
            1,
            0,
            0,
            [],
            [],
            "70_hand_match",
            stage_only=True,
        )

    assert list(target.iterdir()) == []


def test_stage_rejects_incomplete_raw_70_hand_envelope(tmp_path, monkeypatch):
    import elo_daemon
    import evaluation_data_identity

    replay_dir = tmp_path / "replays"
    results_dir = tmp_path / "results"
    bots_dir = tmp_path / "bots"
    _write_strict_bot(bots_dir / BOT_A)
    _write_strict_bot(bots_dir / BOT_B)
    artifact_execution = _artifact_execution(
        bots_dir / BOT_A,
        bots_dir / BOT_B,
    )
    timing_plan = _rating_timing_plan()
    broken = _complete_native_game(
        bot_a=BOT_A,
        bot_b=BOT_B,
        artifact_execution=artifact_execution,
        timing_plan=timing_plan,
        net_chips_a=1,
    )
    broken["hand_records"].pop()
    monkeypatch.setattr(elo_daemon, "REPLAY_DIR", replay_dir)
    monkeypatch.setattr(elo_daemon, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(elo_daemon, "BOTS_DIR", bots_dir)
    monkeypatch.setattr(
        evaluation_data_identity,
        "current_evaluation_digest",
        lambda _root: IDENTITY,
    )

    with pytest.raises(ValueError, match="strict validation failed:game_1:hand_records_incomplete"):
        elo_daemon.save_match_replay(
            BOT_A,
            BOT_B,
            1,
            0,
            0,
            [broken],
            [1],
            "70_hand_match",
            expected_native_match_timing_plan=timing_plan.snapshot(),
            stage_only=True,
        )

    assert not (replay_dir / ".pending").exists()


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
    _write_strict_bot(bots_dir / BOT_A)
    _write_strict_bot(bots_dir / BOT_B)
    artifact_execution = _artifact_execution(
        bots_dir / BOT_A,
        bots_dir / BOT_B,
    )
    monkeypatch.setattr(elo_daemon, "REPLAY_DIR", replay_dir)
    monkeypatch.setattr(elo_daemon, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(
        elo_daemon,
        "MATCH_HISTORY_FILE",
        results_dir / "match_history.jsonl",
    )
    timing_plan = _rating_timing_plan()
    monkeypatch.setattr(elo_daemon, "BOTS_DIR", bots_dir)
    monkeypatch.setattr(
        elo_daemon,
        "daemon_evaluation_identity_digest",
        identity,
    )
    admission = elo_daemon.save_match_replay(
        BOT_A,
        BOT_B,
        1,
        0,
        0,
        [_complete_native_game(
            bot_a=BOT_A,
            bot_b=BOT_B,
            artifact_execution=artifact_execution,
            timing_plan=timing_plan,
            net_chips_a=500,
        )],
        [500],
        "70_hand_match",
        expected_evaluation_identity_digest=identity,
        expected_native_match_timing_plan=timing_plan.snapshot(),
        stage_only=True,
    )
    result = (
        BOT_A,
        BOT_B,
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
        for name in (BOT_A, BOT_B)
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
    assert h2h[f"{BOT_A} vs {BOT_B}"]["a_wins"] == 1


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
        for name in (BOT_A, BOT_B)
    }

    with pytest.raises(RuntimeError, match=error):
        elo_daemon.admit_internal_match_result(result, ratings, {}, {})

    assert not (results_dir / "match_history.jsonl").exists()


def test_staged_nonstrength_receipt_cannot_enter_rating_or_history(
    tmp_path,
    monkeypatch,
):
    from glicko2 import Glicko2Player

    elo_daemon, results_dir, _replay_dir, _identity, result = (
        _stage_current_identity_match(tmp_path, monkeypatch)
    )
    admission = result[8]
    pending = Path(admission["pending_path"])
    payload = json.loads(pending.read_text(encoding="utf-8"))
    payload.update({
        "strength_sample_unit": None,
        "hands_per_strength_sample": None,
        "strength_admitted": False,
        "strength_complete": False,
        "strength_compliance_passed": False,
        "strength_sample_count": 0,
        "net_chips_bot0": [],
        "strength_order": None,
    })
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    pending.write_bytes(raw)
    admission["replay_bytes"] = len(raw)
    admission["replay_sha256"] = hashlib.sha256(raw).hexdigest()
    for field in (
        "strength_sample_unit", "hands_per_strength_sample",
        "strength_admitted", "strength_complete",
        "strength_compliance_passed", "strength_sample_count",
        "net_chips_bot0", "strength_order",
    ):
        admission["summary"][field] = payload[field]
    admission["summary"]["replay_sha256"] = admission["replay_sha256"]
    ratings = {
        name: Glicko2Player(r=1500, rd=350, sigma=0.06)
        for name in (BOT_A, BOT_B)
    }

    with pytest.raises(RuntimeError, match="not an admitted 70-hand"):
        elo_daemon.admit_internal_match_result(result, ratings, {}, {})

    assert not (results_dir / "match_history.jsonl").exists()


def test_admission_rechecks_current_artifact_bytes_after_staging(tmp_path, monkeypatch):
    """A syntactically valid but foreign artifact hash cannot reach Glicko."""

    from bot_artifact import canonical_digest
    from glicko2 import Glicko2Player

    elo_daemon, results_dir, _replay_dir, _identity, result = (
        _stage_current_identity_match(tmp_path, monkeypatch)
    )
    admission = result[8]
    pending = Path(admission["pending_path"])
    payload = json.loads(pending.read_text(encoding="utf-8"))
    forged = payload["games"][0]["artifact_execution"]["by_player"][BOT_A]
    forged["artifact_hash"] = "f" * 64
    forged["identity_digest"] = canonical_digest(
        {key: value for key, value in forged.items() if key != "identity_digest"}
    )
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    pending.write_bytes(raw)
    admission["replay_bytes"] = len(raw)
    admission["replay_sha256"] = hashlib.sha256(raw).hexdigest()
    # Artifact identity is deliberately not a mutable summary field; a forged
    # raw payload would otherwise pass the receipt projection comparison.
    admission["summary"]["replay_sha256"] = admission["replay_sha256"]
    ratings = {
        name: Glicko2Player(r=1500, rd=350, sigma=0.06)
        for name in (BOT_A, BOT_B)
    }

    with pytest.raises(RuntimeError, match="artifact identity does not match current bot bytes"):
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
        "did_not_beat_parent",
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
    # 9/16 = 0.5625 > 0.50, so the candidate clearly beat its parent and passes.
    assert blockers == []


def test_precommit_parent_gate_loss_is_blocked():
    """The strength gate requires the candidate to BEAT its parent (>50% score)
    over at least PRECOMMIT_PARENT_MIN_SAMPLES (6) matches.  A genuine loss
    (score < 0.50) is always blocked, regardless of chip magnitude."""
    from strength_order import precommit_outcome_blockers, PRECOMMIT_PARENT_MAX_SCORE, PRECOMMIT_PARENT_MIN_SAMPLES

    assert PRECOMMIT_PARENT_MAX_SCORE == 0.50
    assert PRECOMMIT_PARENT_MIN_SAMPLES == 6
    # 3W-5L-0D over 8 matches = score 0.375 — a genuine loss, blocked even with
    # a strongly positive chip vector (the escape hatch is tie-only).
    blockers, _ = precommit_outcome_blockers([{
        "opponent": "national_v143",
        "wins": 3,
        "losses": 5,
        "draws": 0,
        "net_chips": [10_000] * 8,
    }], parent_label="national_v143")
    assert any(r["reason"] == "did_not_beat_parent" for r in blockers)


def test_precommit_parent_gate_tie_blocks_when_chip_ci_negative():
    """An exact tie (score == 0.50) with a negative net-chip CI upper bound is
    still a regression and is blocked."""
    from strength_order import precommit_outcome_blockers

    # 4W-4L-0D = score 0.50 (exact tie); strongly negative chips.
    blockers, _ = precommit_outcome_blockers([{
        "opponent": "national_v143",
        "wins": 4,
        "losses": 4,
        "draws": 0,
        "net_chips": [-5000, -3000, -2000, -1000, -500, -400, -300, -200],
    }], parent_label="national_v143")
    assert any(r["reason"] == "did_not_beat_parent" for r in blockers)


def test_precommit_parent_gate_tie_passes_when_chip_ci_positive():
    """An exact tie (score == 0.50) vs the parent PASSES when the paired
    net-chip bootstrap 95% CI upper bound is positive — the tie is reclassified
    as 'not a regression'.  This is the statistical-power fix: at small n an
    equal-strength candidate ties ~50% of the time, and the chip magnitude
    (far more informative than the binary W/L signs) breaks the tie."""
    from strength_order import precommit_outcome_blockers

    # 4W-4L-0D = score 0.50 (exact tie); positive chips so CI upper > 0.
    blockers, _ = precommit_outcome_blockers([{
        "opponent": "national_v143",
        "wins": 4,
        "losses": 4,
        "draws": 0,
        "net_chips": [5000, 3000, 2000, 1000, 500, 400, 300, 200],
    }], parent_label="national_v143")
    assert not any(r["reason"] == "did_not_beat_parent" for r in blockers)


def test_precommit_parent_gate_tie_without_chip_samples_blocks():
    """An exact tie with no net-chip samples cannot invoke the CI escape hatch
    and is still blocked (fail-closed when the tie-breaker is unavailable)."""
    from strength_order import precommit_outcome_blockers

    # 3W-3L-0D = score 0.50 (exact tie); no net_chips key at all.
    blockers, _ = precommit_outcome_blockers([{
        "opponent": "national_v143",
        "wins": 3,
        "losses": 3,
        "draws": 0,
    }], parent_label="national_v143")
    assert any(r["reason"] == "did_not_beat_parent" for r in blockers)


def test_precommit_parent_gate_passes_on_clear_majority_win():
    """A candidate that wins a clear majority (>50%) over enough samples passes."""
    from strength_order import precommit_outcome_blockers

    # 5W-2L-0D over 7 matches = score 5/7 ≈ 0.714 > 0.50 → passes parent gate.
    blockers, _ = precommit_outcome_blockers([{
        "opponent": "national_v143",
        "wins": 5,
        "losses": 2,
        "draws": 0,
    }], parent_label="national_v143")
    assert not any(r["reason"] == "did_not_beat_parent" for r in blockers)


def test_precommit_parent_gate_samples_threshold():
    """Below PRECOMMIT_PARENT_MIN_SAMPLES (6), the parent gate does not fire
    even on a collapse — too few samples to judge strength reliably."""
    from strength_order import precommit_outcome_blockers, PRECOMMIT_PARENT_MIN_SAMPLES

    assert PRECOMMIT_PARENT_MIN_SAMPLES == 6
    # Only 5 samples (below threshold) with a collapse — parent gate does not fire.
    blockers, _ = precommit_outcome_blockers([{
        "opponent": "national_v143",
        "wins": 0,
        "losses": 5,
        "draws": 0,
    }], parent_label="national_v143")
    assert not any(r["reason"] == "did_not_beat_parent" for r in blockers)


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
        [BOT_A, BOT_B],
        history,
        expected_evaluation_identity_digest=IDENTITY,
    )
    assert rebuilt[f"{BOT_A} vs {BOT_B}"] == {
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
