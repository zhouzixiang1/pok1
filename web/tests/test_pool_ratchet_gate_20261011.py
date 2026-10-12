"""Pool-ratchet admission gate regressions (2026-10-11).

Covers pool_ratchet_gate.pool_gate_blockers' full decision tree (majority
pass / majority loss block / exact tie broken by the paired net-chip
bootstrap CI lower bound), the explicit pool_gate_label_missing blocker, the
frozen-settings-only threshold source, the legacy no-gate plan, and the
SEMANTIC_PATHS binding of the gate module (must-fix 1 regression anchor).
"""

import pytest

import pool_ratchet_gate
from bot_namespace import bot_name


def _matchup(label, wins, losses, draws, net_chips=None):
    return {
        "opponent": label,
        "reason": "pool_weakest_gate",
        "wins": wins,
        "losses": losses,
        "draws": draws,
        "net_chips": list(net_chips or []),
    }


def _frozen_settings(**overrides):
    gate = {
        "label": bot_name(11),
        "min_samples": 6,
        "max_score": 0.50,
        "tie_ci_lower_min": 0.0,
    }
    gate.update(overrides)
    return {"pool_gate": gate}


def test_majority_win_passes_pool_gate():
    blockers, summary = pool_ratchet_gate.pool_gate_blockers(
        [_matchup(bot_name(11), 8, 4, 0)],
        bot_name(11),
        _frozen_settings(),
    )
    assert blockers == []
    assert summary["matched"] is True
    assert summary["min_samples"] == 6


def test_majority_loss_is_blocked_with_pool_reason():
    blockers, _summary = pool_ratchet_gate.pool_gate_blockers(
        [_matchup(bot_name(11), 4, 8, 0)],
        bot_name(11),
        _frozen_settings(),
    )
    assert [item["reason"] for item in blockers] == ["did_not_beat_pool_weakest"]
    assert blockers[0]["opponent"] == bot_name(11)


def test_exact_tie_passes_only_with_positive_chip_ci_lower():
    # 6W-6L exact tie with a confidently positive chip edge -> passes.
    chips_positive = [400, 500, 300, 600, 200, 800, 900, 100, 700, 300, 500, 400]
    blockers, _summary = pool_ratchet_gate.pool_gate_blockers(
        [_matchup(bot_name(11), 6, 6, 0, net_chips=chips_positive)],
        bot_name(11),
        _frozen_settings(),
    )
    assert blockers == []

    # Same tie with a clearly negative chip edge -> blocked.
    blockers, _summary = pool_ratchet_gate.pool_gate_blockers(
        [_matchup(bot_name(11), 6, 6, 0, net_chips=[-v for v in chips_positive])],
        bot_name(11),
        _frozen_settings(),
    )
    assert [item["reason"] for item in blockers] == ["did_not_beat_pool_weakest"]
    assert "net-chip CI lower" in blockers[0]["details"]


def test_insufficient_samples_does_not_block_but_records_summary():
    # Under min_samples the parent-gate structure does not fire (a sub-n
    # sample cannot prove a regression); the summary still reports n.
    blockers, summary = pool_ratchet_gate.pool_gate_blockers(
        [_matchup(bot_name(11), 1, 4, 0)],
        bot_name(11),
        _frozen_settings(),
    )
    assert blockers == []
    assert summary["per_matchup"][0]["samples"] == 5


def test_missing_label_is_explicit_blocker():
    blockers, summary = pool_ratchet_gate.pool_gate_blockers(
        [_matchup(bot_name(27), 8, 4, 0)],
        bot_name(11),
        _frozen_settings(),
    )
    assert [item["reason"] for item in blockers] == ["pool_gate_label_missing"]
    assert summary["matched"] is False


def test_thresholds_come_from_frozen_settings_not_live_env(monkeypatch):
    # A stricter frozen threshold blocks a majority win that the live default
    # would pass; flipping the live env afterwards cannot re-decide the gate.
    settings = _frozen_settings(max_score=0.75)
    monkeypatch.setenv("POK_POOL_GATE_MAX_SCORE", "0.30")
    blockers, _summary = pool_ratchet_gate.pool_gate_blockers(
        [_matchup(bot_name(11), 8, 4, 0)],
        bot_name(11),
        settings,
    )
    assert [item["reason"] for item in blockers] == ["did_not_beat_pool_weakest"]

    # And the frozen looser threshold keeps passing while the live default
    # (0.50) would also pass — the frozen value is the only input.
    settings = _frozen_settings(max_score=0.30, min_samples=2)
    blockers, _summary = pool_ratchet_gate.pool_gate_blockers(
        [_matchup(bot_name(11), 3, 6, 1)],
        bot_name(11),
        settings,
    )
    assert blockers == []


def test_legacy_plan_with_no_pool_gate_spec_uses_code_defaults(monkeypatch):
    monkeypatch.delenv("POK_POOL_GATE_MAX_SCORE", raising=False)
    # A legacy plan carries no pool_gate spec at all; when executed (only
    # reachable through an explicitly passed label) the code defaults apply.
    blockers, _summary = pool_ratchet_gate.pool_gate_blockers(
        [_matchup(bot_name(11), 8, 4, 0)],
        bot_name(11),
        {"pool_gate": None},
    )
    assert blockers == []


def test_env_overrides_are_validated_at_freeze_moment(monkeypatch):
    monkeypatch.setenv("POK_POOL_GATE_MIN_SAMPLES", "0")
    with pytest.raises(pool_ratchet_gate.PoolGateConfigError):
        pool_ratchet_gate.build_pool_gate_settings(bot_name(11))
    monkeypatch.setenv("POK_POOL_GATE_MIN_SAMPLES", "4")
    monkeypatch.setenv("POK_POOL_GATE_MAX_SCORE", "1.5")
    with pytest.raises(pool_ratchet_gate.PoolGateConfigError):
        pool_ratchet_gate.build_pool_gate_settings(bot_name(11))
    monkeypatch.setenv("POK_POOL_GATE_MAX_SCORE", "0.55")
    spec = pool_ratchet_gate.build_pool_gate_settings(bot_name(11))
    assert spec == {
        "label": bot_name(11),
        "min_samples": 4,
        "max_score": 0.55,
        "tie_ci_lower_min": 0.0,
    }


def test_first_strict_gate_spec_helper_never_builds_a_pool_gate():
    import tool_eval

    spec, error = tool_eval._build_pool_gate_spec(
        [{"name": bot_name(11), "reason": "pool_weakest_gate"}],
        first_strict=True,
    )
    assert spec is None and error is None


def test_semantic_paths_bind_the_gate_module():
    """must-fix 1 regression anchor: the gate bytes are identity-bound."""
    import precommit_eval_contract

    assert (
        "web/core/pool_ratchet_gate.py"
        in precommit_eval_contract.SEMANTIC_PATHS
    )
    # The tuning/mode module must stay OUT of every identity list.
    assert (
        "web/core/pool_ratchet.py"
        not in precommit_eval_contract.SEMANTIC_PATHS
    )
    import evaluation_data_identity

    assert (
        "web/core/pool_ratchet.py"
        not in evaluation_data_identity.SEMANTIC_PATHS
    )
    assert (
        "web/core/pool_ratchet_gate.py"
        not in evaluation_data_identity.SEMANTIC_PATHS
    )
