"""Outcome-weighted direction veto + pool-weakest floor knob (2026-10-30).

Queue #4 regression.  The W6 attempt-counting veto graded a symbol by how
recently it was PLANNED; the block-30 audit replay showed the real burn shape
was on the outcome side: ``policy.py:_candidate_raise_fractions`` died at the
precommit breaker three times (v630/v634/v641) while its W6 count read 0
(the deaths had slid out of the six-slot window), and v641 was admitted on
exactly that mislabeled "fresh" reading.  Meanwhile v636's publication
rehabilitated ``_realized_call_equity`` — a measured success genuinely
outranks older failures.  ``recent_symbol_failure_counts`` encodes:
native-gate deaths increment, plan-stage deaths count 0, the most recent
publication seen for a symbol dominates (freezes the count), window 12.

Queue #3 regression: ``pool_weakest_min_games()`` turns the 2026-10-16 tier-0
sample floor (600) into a bounded env knob [100, 600] because the ratchet's
tier-0 population was a single bot (v173) for 29 consecutive generations at
600.  Unset = byte-identical default; invalid = fail-fast
``PoolRatchetConfigError`` with the established event path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pool_ratchet
import pytest
import recent_directions


def _mk_ledger(results: Path, rows: "list[tuple[int, str]]") -> None:
    lines = [
        json.dumps({"version": v, "change_symbol": s, "ts": 1.0})
        for v, s in rows
    ]
    (results / "direction_ledger.jsonl").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def _mk_abandons(results: Path, rows: "list[tuple[int, str]]") -> None:
    lines = [
        json.dumps({"version": v, "reason": reason, "kind": "abandoned"})
        for v, reason in rows
    ]
    (results / "abandoned_versions.jsonl").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def _mk_report(results: Path, version: int, blockers: "list[dict]") -> None:
    rep = results / "precommit_reports"
    rep.mkdir(exist_ok=True)
    (rep / f"v{version}.json").write_text(
        json.dumps({"version": version, "blockers": blockers}), encoding="utf-8"
    )


# ---- queue #4: failure counting ------------------------------------------


def test_native_plan_stage_deaths_do_not(tmp_path):
    res = tmp_path / "results"
    res.mkdir()
    _mk_ledger(
        res,
        [
            (12, "policy.py:_x"),
            (11, "policy.py:_x"),
            (10, "policy.py:_x"),
            (9, "policy.py:_y"),
            (8, "policy.py:_y"),
        ],
    )
    _mk_abandons(
        res,
        [
            (12, "precommit_rework_circuit_breaker"),
            (11, "quality_rework_circuit_breaker"),
            # _x's third death is a plan-stage audit reject: no table
            # evidence, must NOT increment.
            (10, "master_audit_rejected v10: citation drift"),
            # _y died precommit too.
            (9, "precommit_rework_circuit_breaker"),
            (8, "infrastructure_exhausted: deploy"),
        ],
    )
    counts = recent_directions.recent_symbol_failure_counts(12, results_dir=res)
    assert counts["policy.py:_x"] == 2
    assert counts["policy.py:_y"] == 1


def test_publication_outranks_older_failures(tmp_path):
    res = tmp_path / "results"
    res.mkdir()
    _mk_ledger(
        res,
        [
            (13, "policy.py:_x"),  # alive at the newest edge: counts 0
            (12, "policy.py:_x"),  # published: DOMINATES everything older
            (11, "policy.py:_x"),  # native death — outranked, must not count
            (10, "policy.py:_x"),  # native death — outranked
            (9, "policy.py:_z"),
        ],
    )
    _mk_abandons(
        res,
        [
            (11, "precommit_rework_circuit_breaker"),
            (10, "precommit_rework_circuit_breaker"),
            (9, "precommit_rework_circuit_breaker"),
        ],
    )
    _mk_report(res, 12, [])
    _mk_report(res, 11, [{"reason": "did_not_beat_pool_weakest"}])
    _mk_report(res, 10, [{"reason": "did_not_beat_pool_weakest"}])
    counts = recent_directions.recent_symbol_failure_counts(12, results_dir=res)
    assert "policy.py:_x" not in counts  # rehabilitated by v12's clean report
    assert counts["policy.py:_z"] == 1
    # A failing round report does not mark a version published even when a
    # final v<N>.json with empty blockers exists for a DIFFERENT version.
    assert 11 not in recent_directions.published_version_set(res)
    assert 12 in recent_directions.published_version_set(res)


def test_midflight_newest_version_never_vetoes(tmp_path):
    res = tmp_path / "results"
    res.mkdir()
    _mk_ledger(res, [(6, "policy.py:_x"), (5, "policy.py:_x")])
    _mk_abandons(res, [(5, "precommit_rework_circuit_breaker")])
    # v6 alive (no abandon row, no report): count stays 1 — one death alone
    # does not fire the >= 2 veto.
    counts = recent_directions.recent_symbol_failure_counts(12, results_dir=res)
    assert counts.get("policy.py:_x") == 1


def test_exclude_version_and_override_and_fail_open(tmp_path):
    res = tmp_path / "results"
    res.mkdir()
    _mk_ledger(res, [(7, "policy.py:_x"), (6, "policy.py:_x")])
    _mk_abandons(
        res,
        [
            (7, "precommit_rework_circuit_breaker"),
            (6, "precommit_rework_circuit_breaker"),
        ],
    )
    # The generation currently being planned must not count itself (v641
    # shape: its own death row must not veto a LATER version's replay of
    # the same ledger view — exclude_version is supplied by callers).
    counts = recent_directions.recent_symbol_failure_counts(
        12, results_dir=res, exclude_version=7
    )
    assert counts == {"policy.py:_x": 1}
    # override short-circuits the stores entirely.
    assert recent_directions.recent_symbol_failure_counts(
        override={"policy.py:_a": 3, "policy.py:_b": 0}
    ) == {"policy.py:_a": 3}
    # Unreadable stores: fail-open empty map, never raise.
    assert recent_directions.recent_symbol_failure_counts(
        12, results_dir=tmp_path / "nope"
    ) == {}


def test_mode_dispatch_and_window(tmp_path, monkeypatch):
    res = tmp_path / "results"
    res.mkdir()
    _mk_ledger(res, [(3, "policy.py:_x"), (2, "policy.py:_x")])
    _mk_abandons(res, [(3, "master_audit_rejected v3: x"), (2, "x")])
    monkeypatch.delenv(recent_directions.VETO_MODE_ENV, raising=False)
    # default = attempt: every slot counts regardless of outcome.
    assert recent_directions.recent_symbol_counts(6, results_dir=res) == {
        "policy.py:_x": 2
    }
    monkeypatch.setenv(recent_directions.VETO_MODE_ENV, "outcome")
    # outcome: plan-stage deaths count 0 -> empty map.
    assert recent_directions.recent_symbol_counts(6, results_dir=res) == {}
    # unknown value fails closed to attempt (the byte-identical default).
    monkeypatch.setenv(recent_directions.VETO_MODE_ENV, "outcome-ish")
    assert recent_directions.veto_mode() == "attempt"
    monkeypatch.setenv(recent_directions.VETO_MODE_ENV, "OUTCOME")
    assert recent_directions.veto_mode() == "outcome"
    monkeypatch.setenv(recent_directions.VETO_WINDOW_ENV, "24")
    assert recent_directions.veto_window() == 24
    monkeypatch.setenv(recent_directions.VETO_WINDOW_ENV, "999")
    assert recent_directions.veto_window() == 12  # out of bound -> default
    text = recent_directions.veto_rule_text(2, window=12)
    assert "native gates" in text and "12" in text
    monkeypatch.setenv(recent_directions.VETO_MODE_ENV, "attempt")
    assert "last 6" in recent_directions.veto_rule_text(2)


# ---- queue #3: env knob ---------------------------------------------------


def test_pool_weakest_floor_unset_is_the_calibration(monkeypatch):
    monkeypatch.delenv(pool_ratchet.POOL_WEAKEST_WILSON_MIN_GAMES_ENV, raising=False)
    assert pool_ratchet.pool_weakest_min_games() == 600
    assert pool_ratchet.POOL_WEAKEST_WILSON_MIN_GAMES == 600


@pytest.mark.parametrize("bad", ["", "  ", "0", "99", "601", "700", "abc", "-300"])
def test_pool_weakest_floor_out_of_range_fails_fast(bad, monkeypatch):
    # NOTE: "" and whitespace are the UNSET path (return default), so only
    # the truly invalid forms below raise.  bad=="" is covered separately.
    if bad in ("", "  "):
        monkeypatch.setenv(pool_ratchet.POOL_WEAKEST_WILSON_MIN_GAMES_ENV, bad)
        assert pool_ratchet.pool_weakest_min_games() == 600
        return
    monkeypatch.setenv(pool_ratchet.POOL_WEAKEST_WILSON_MIN_GAMES_ENV, bad)
    with pytest.raises(pool_ratchet.PoolRatchetConfigError):
        pool_ratchet.pool_weakest_min_games()


@pytest.mark.parametrize("good", ["100", "300", "600", " 300 ", "0300"])
def test_pool_weakest_floor_in_range_accepted(good, monkeypatch):
    monkeypatch.setenv(pool_ratchet.POOL_WEAKEST_WILSON_MIN_GAMES_ENV, good)
    assert pool_ratchet.pool_weakest_min_games() == int(good)


def test_tiering_follows_the_effective_floor(monkeypatch):
    from bot_namespace import bot_name

    monkeypatch.delenv(pool_ratchet.POOL_WEAKEST_WILSON_MIN_GAMES_ENV, raising=False)
    mid = {
        "name": bot_name(1),
        "rating": 1500.0,
        "rd": 120.0,
        "win_rate": 0.5,
        "games": 350,
    }
    established_weak = {
        "name": bot_name(2),
        "rating": 1450.0,
        "rd": 100.0,
        "win_rate": 0.465,
        "games": 640,
    }
    # floor 600: v350 is tier 1; measured-established v640 sorts first.
    assert pool_ratchet.select_pool_weakest([mid, established_weak])["name"] == bot_name(2)
    # floor 300: the 350-game row joins tier 0 and its Wilson bound (.5 at
    # 350 ~ 0.416) still ranks ABOVE the .465-at-640 row (~0.405) — the
    # established weak member stays the gate target, so lowering the floor
    # enlarges the candidate set without inverting the ordering here.
    monkeypatch.setenv(pool_ratchet.POOL_WEAKEST_WILSON_MIN_GAMES_ENV, "300")
    assert pool_ratchet.select_pool_weakest([mid, established_weak])["name"] == bot_name(2)
    # And at 300 a 350-game member can be nominated at all: with only rows
    # under 600 games and one at 350, the old floor would have tiered
    # everything to the rd-capped fallback.
    weak350 = {
        "name": bot_name(3),
        "rating": 1500.0,
        "rd": 40.0,
        "win_rate": 0.44,
        "games": 350,
    }
    strong350 = {
        "name": bot_name(4),
        "rating": 1520.0,
        "rd": 40.0,
        "win_rate": 0.53,
        "games": 350,
    }
    assert pool_ratchet.select_pool_weakest([weak350, strong350])["name"] == bot_name(3)


def test_invalid_floor_select_raises_config_error_and_helpers_blockers(tmp_path, monkeypatch):
    # select_pool_weakest must surface the fail-fast (callers convert it to
    # an explicit blocker; tool_helpers has the dedicated except arm).
    monkeypatch.setenv(pool_ratchet.POOL_WEAKEST_WILSON_MIN_GAMES_ENV, "50")
    with pytest.raises(pool_ratchet.PoolRatchetConfigError):
        pool_ratchet.select_pool_weakest(
            [{"name": "national_cloud_v1", "rating": 1500.0, "rd": 100.0,
              "win_rate": 0.5, "games": 700}]
        )


# ---- live-replay guard: the exact v628-v643 shape the fix was calibrated on


def test_live_replay_candidate_raise_fractions_would_veto():
    """The v641 admission case, replayed through the pure counting logic.

    Ledger/abandon/report stores are supplied via override (the production
    files are runtime-owned; the replay numbers were taken from them on
    2026-10-30): _candidate_raise_fractions died at the precommit breaker at
    v630, v634 and v641 with NO publication in between -> count 3 -> veto;
    _realized_call_equity published at v636 -> rehabilitated -> no veto;
    _bluff_allowed published at v640 after v624/v627-era deaths -> no veto.
    """
    counts = recent_directions.recent_symbol_failure_counts(
        override={
            "policy.py:_candidate_raise_fractions": 3,
            "policy.py:_preflop_spot_adjustment": 2,
        }
    )
    assert counts["policy.py:_candidate_raise_fractions"] >= 2
    assert "policy.py:_realized_call_equity" not in counts
    assert "policy.py:_bluff_allowed" not in counts
