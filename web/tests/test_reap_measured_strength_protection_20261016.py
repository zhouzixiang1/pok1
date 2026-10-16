"""Reap measured-strength protection regressions (2026-10-16 half-alignment).

The admission gate now targets the Wilson-95% measured weakest
(pool_ratchet 2026-10-16 calibration), while reap victims are still ordered
by the conservative score.  Without protection, a measured-STRONG member
whose inflated rd sinks its conservative score gets reaped while the gate
makes candidates beat a different, measured-weak member — an anti-ratchet.
The half-alignment: members whose Wilson lower bound (at the gate's 600-game
floor) sits strictly above the pool median lower bound join the protection
set, and the extension yields whenever it would strand the pool without a
victim.
"""

import json

import pytest

from bot_namespace import bot_name


def _rewrite_bot_stats(monkeypatch, tmp_path, stats_by_version):
    import tool_bot_management as tbm

    results_dir = tbm.RESULTS_DIR
    payload = {
        bot_name(v): stats for v, stats in stats_by_version.items()
    }
    (results_dir / "bot_stats.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def test_measured_strong_member_is_protected_from_reap(monkeypatch, tmp_path):
    import test_pool_reap_target_size_20261011 as helper
    import tool_commit

    monkeypatch.setenv("POK_PUBLISH_MODE", "pool_ratchet")
    monkeypatch.setenv("POK_POOL_RATCHET_TARGET_SIZE", "12")
    versions = list(range(1, 14))  # 13 bots, cap 12 -> exactly 1 reap
    # Plain ratings: v1/v2 bottom-half (plain-median protection leaves both
    # reapable); everyone else top-half and plain-protected.
    ratings = {v: (1500.0 + v * 10.0, 80.0) for v in versions}
    ratings[1] = (1200.0, 300.0)  # conservative 600 — lowest by far
    ratings[2] = (1250.0, 60.0)  # conservative 1130
    helper._setup_pool(
        monkeypatch,
        tmp_path,
        versions,
        rating_by_version=ratings,
        priority_version=13,
    )
    # Measured strength from bot_stats (games >= 600 floor): v1 is the
    # measured 3rd-strongest shape (high win rate, huge rd), v2 the
    # measured-weakest shape; the filler sits between so v1's LB is the
    # unique one strictly above the median LB.
    _rewrite_bot_stats(
        monkeypatch,
        tmp_path,
        {
            **{v: {"games": 700, "win_rate": 0.49} for v in range(3, 14)},
            1: {"games": 993, "win_rate": 0.5156},
            2: {"games": 645, "win_rate": 0.4651},
        },
    )

    plan = tool_commit._build_pool_reap_plan(
        {"identity": {"version": 13, "publication_id": "a" * 64,
                      "commit_oid": "b" * 40,
                      "remote_publication": {"remote_main_oid": "c" * 40}}}
    )
    snapshot = plan["selection_snapshot"]
    # The pool-ratchet snapshot now freezes the measured input per row.
    assert all(
        "win_rate_hex" in row for row in snapshot["bot_inputs"]
    )
    # v1 (conservative-weakest BUT measured-strong) is protected; the victim
    # is the conservative-weakest among the unprotected — v2.
    assert [row["candidate"] for row in plan["targets"]] == [bot_name(2)]


def test_equal_measured_strength_never_strands_the_pool(monkeypatch, tmp_path):
    """Victim-exists invariant under degenerate measured data.

    With every member at the SAME Wilson lower bound the measured-strong
    set is empty (strictly-above-median), so the alignment extension is a
    no-op and the pool still yields its conservative-weakest victim. The
    deeper guard — the extension yields whenever it would strand the pool —
    is structurally unreachable with plain-median + LB-median parity (the
    median always lands inside the plain-unprotected half); this test pins
    the victim-exists property under the degenerate tie shape.
    """
    import test_pool_reap_target_size_20261011 as helper
    import tool_commit

    monkeypatch.setenv("POK_PUBLISH_MODE", "pool_ratchet")
    monkeypatch.setenv("POK_POOL_RATCHET_TARGET_SIZE", "12")
    versions = list(range(1, 14))
    ratings = {v: (1200.0 + (v - 1) * 50.0, 100.0) for v in versions}
    ratings[1] = (1200.0, 300.0)  # conservative 600 — weakest by far
    helper._setup_pool(
        monkeypatch,
        tmp_path,
        versions,
        rating_by_version=ratings,
        priority_version=13,
    )
    _rewrite_bot_stats(
        monkeypatch,
        tmp_path,
        {v: {"games": 993, "win_rate": 0.50} for v in versions},
    )

    plan = tool_commit._build_pool_reap_plan(
        {"identity": {"version": 13, "publication_id": "a" * 64,
                      "commit_oid": "b" * 40,
                      "remote_publication": {"remote_main_oid": "c" * 40}}}
    )
    assert [row["candidate"] for row in plan["targets"]] == [bot_name(1)]


def test_snapshot_row_shape_is_profile_gated(monkeypatch, tmp_path):
    """pool_ratchet rows must freeze win_rate_hex; legacy rows must not."""

    import tool_bot_management as tbm
    import tool_bot_management_reap as tbmr
    import test_pool_reap_target_size_20261011 as helper

    monkeypatch.setenv("POK_PUBLISH_MODE", "pool_ratchet")
    monkeypatch.setenv("POK_POOL_RATCHET_TARGET_SIZE", "12")
    versions = list(range(1, 14))
    ratings = {v: (1200.0 + (v - 1) * 50.0, 100.0) for v in versions}
    helper._setup_pool(
        monkeypatch, tmp_path, versions, rating_by_version=ratings,
        priority_version=13,
    )

    pool_snapshot = tbmr._capture_reap_selection_snapshot(
        protection_profile="pool_ratchet",
    )
    assert all(
        "win_rate_hex" in row for row in pool_snapshot["bot_inputs"]
    )
    # Round-trips through the validator (decoded rows carry win_rate).
    decoded = tbm._validate_reap_selection_snapshot(pool_snapshot)
    assert all("win_rate" in row for row in decoded.values())

    legacy_snapshot = tbmr._capture_reap_selection_snapshot(
        selection_policy="conservative_glicko_v1",
    )
    assert all(
        "win_rate_hex" not in row for row in legacy_snapshot["bot_inputs"]
    )
    decoded_legacy = tbm._validate_reap_selection_snapshot(legacy_snapshot)
    assert all("win_rate" not in row for row in decoded_legacy.values())


def test_pool_ratchet_row_missing_measured_key_fails_closed(
    monkeypatch, tmp_path
):
    import tool_bot_management as tbm
    import tool_bot_management_reap as tbmr
    import test_pool_reap_target_size_20261011 as helper

    monkeypatch.setenv("POK_PUBLISH_MODE", "pool_ratchet")
    monkeypatch.setenv("POK_POOL_RATCHET_TARGET_SIZE", "12")
    versions = list(range(1, 14))
    ratings = {v: (1200.0 + (v - 1) * 50.0, 100.0) for v in versions}
    helper._setup_pool(
        monkeypatch, tmp_path, versions, rating_by_version=ratings,
        priority_version=13,
    )
    snapshot = tbmr._capture_reap_selection_snapshot(
        protection_profile="pool_ratchet",
    )
    snapshot["bot_inputs"][0].pop("win_rate_hex")
    snapshot["bot_inputs_digest"] = tbm.canonical_digest(
        snapshot["bot_inputs"]
    )
    unsigned = {
        key: value
        for key, value in snapshot.items()
        if key != "snapshot_digest"
    }
    snapshot["snapshot_digest"] = tbm.canonical_digest(unsigned)
    with pytest.raises(RuntimeError, match="input_keys"):
        tbm._validate_reap_selection_snapshot(snapshot)
