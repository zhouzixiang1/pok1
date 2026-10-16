"""Pool-weakest selection-key calibration regressions (2026-10-16).

The old ``select_pool_weakest`` key was the bare conservative rating
``r - 2*rd``; rd inflation (snapshot rd up to 298.8) made it nominate a
strong-but-rusty bot (v11: 993 games, 0.5156 observed win rate — 3rd
strongest by measurement) as pool-weakest instead of the genuinely weak
v173 (0.4651/645) / v83 (0.4668).  The new key is the Wilson-95% LOWER
bound of the observed win rate once ``games >= 600``; below that floor rows
fall back to the conservative score with rd capped at 150, ordered after
every measured row (tier 1).  Ties resolve through the total order
``(tier, key, games, version)``.
"""

import math

import pytest

import pool_ratchet
import tool_helpers
from bot_namespace import bot_name


def _row(
    version,
    *,
    rating,
    rd,
    games,
    win_rate=None,
    on_disk=True,
):
    return {
        "name": bot_name(version),
        "rating": float(rating),
        "rd": float(rd),
        "conservative_rating": round(float(rating) - 2.0 * float(rd), 1),
        "win_rate": win_rate,
        "games": games,
        "leaderboard_score": 0.5,
        "selection_score": 0.5,
        "on_disk": on_disk,
    }


def _wilson_reference(p, n, z=1.959963984540054):
    """Independent Wilson lower-bound implementation for cross-checking."""
    denom = 1.0 + z * z / n
    center = p + z * z / (2.0 * n)
    margin = z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n))
    return (center - margin) / denom


def test_wilson_lower_bound_matches_reference():
    assert pool_ratchet.wilson_lower_bound(0.4651, 645) == pytest.approx(
        _wilson_reference(0.4651, 645), abs=1e-12
    )
    assert pool_ratchet.wilson_lower_bound(0.5156, 993) == pytest.approx(
        _wilson_reference(0.5156, 993), abs=1e-12
    )
    # Production snapshot figures: the measured-weakest LBs sit clearly below
    # the strong-but-rusty bot's LB despite its inflated rd.
    assert pool_ratchet.wilson_lower_bound(0.4651, 645) < (
        pool_ratchet.wilson_lower_bound(0.5156, 993)
    )


def test_wilson_key_selects_measured_weakest_not_high_rd_strong():
    """The exact production failure: v11 (0.5156/993, rd 298.8) must NOT be
    nominated over v173 (0.4651/645) / v83 (0.4668)."""
    v11 = _row(11, rating=1487.6, rd=298.8, games=993, win_rate=0.5156)
    v173 = _row(173, rating=1490.2, rd=120.0, games=645, win_rate=0.4651)
    v83 = _row(83, rating=1470.1, rd=110.0, games=640, win_rate=0.4668)

    selected = pool_ratchet.select_pool_weakest([v11, v173, v83])

    assert selected["name"] == bot_name(173)
    # The OLD key would have misselected v11: its inflated-rd conservative
    # rating is the pool minimum.
    assert min(
        v11["conservative_rating"], v173["conservative_rating"],
        v83["conservative_rating"],
    ) == v11["conservative_rating"]


def test_under_sampled_rows_use_capped_rd_conservative_fallback():
    """Below the 600-game floor the conservative score applies with rd capped
    at 150, so an inflated rd cannot dominate the fallback ordering either."""
    high_rd = _row(5, rating=1500.0, rd=298.0, games=300, win_rate=0.50)
    genuinely_weak = _row(11, rating=1250.0, rd=80.0, games=280, win_rate=0.44)

    selected = pool_ratchet.select_pool_weakest([high_rd, genuinely_weak])

    assert selected["name"] == bot_name(11)
    # Uncapped, the old key would have picked the high-rd row instead.
    assert (
        high_rd["rating"] - 2.0 * high_rd["rd"]
        < genuinely_weak["rating"] - 2.0 * genuinely_weak["rd"]
    )
    # The fallback key is exactly rating - 2*min(rd, 150).
    assert (
        1500.0 - 2.0 * min(298.0, pool_ratchet.POOL_WEAKEST_FALLBACK_RD_CAP)
        > 1250.0 - 2.0 * min(80.0, pool_ratchet.POOL_WEAKEST_FALLBACK_RD_CAP)
    )


def test_measured_rows_sort_before_under_sampled_rows():
    """Tier 0 (games >= 600 with win_rate) is always nominated first: a row
    whose measured strength is unknown is never weakest while a measured row
    exists, even if its conservative rating is far lower."""
    measured_mid = _row(5, rating=1500.0, rd=100.0, games=700, win_rate=0.55)
    unknown_weak = _row(11, rating=800.0, rd=60.0, games=90, win_rate=0.40)

    selected = pool_ratchet.select_pool_weakest([measured_mid, unknown_weak])

    assert selected["name"] == bot_name(5)


def test_games_floor_without_win_rate_falls_back_to_tier_one():
    """A games>=600 row without a usable win_rate cannot use the Wilson key;
    it falls to the capped-conservative tier instead of failing or guessing."""
    no_wr = _row(5, rating=1500.0, rd=100.0, games=700, win_rate=None)
    weaker = _row(11, rating=1100.0, rd=100.0, games=90, win_rate=0.40)

    selected = pool_ratchet.select_pool_weakest([no_wr, weaker])

    assert selected["name"] == bot_name(11)


def test_tie_break_total_order_is_deterministic():
    """Equal keys resolve by (tier, key, games, version): same win_rate and
    games ties break on the lower version, and repeated calls agree."""
    a = _row(17, rating=1500.0, rd=100.0, games=700, win_rate=0.48)
    b = _row(4, rating=1400.0, rd=90.0, games=700, win_rate=0.48)

    rows = [a, b]
    first = pool_ratchet.select_pool_weakest(rows)
    second = pool_ratchet.select_pool_weakest(list(reversed(rows)))
    third = pool_ratchet.select_pool_weakest(rows)

    # Same (tier, key, games): the lower version wins regardless of input
    # order, and the selection is repeatable.
    assert first["name"] == second["name"] == third["name"] == bot_name(4)
    # More measured games win the (key, games) tie before version applies.
    c = _row(4, rating=1500.0, rd=100.0, games=650, win_rate=0.48)
    d = _row(2, rating=1400.0, rd=90.0, games=700, win_rate=0.48)
    assert pool_ratchet.select_pool_weakest([c, d])["name"] == bot_name(4)


def test_fallback_tier_missing_rating_fails_closed():
    row = {
        "name": bot_name(5),
        "games": 300,
        "win_rate": None,
        "rd": 100.0,
        "leaderboard_score": 0.5,
    }
    with pytest.raises(pool_ratchet.PoolRatchetSelectionError):
        pool_ratchet.select_pool_weakest([row])


def test_exclusion_skips_to_next_weakest():
    v11 = _row(11, rating=1487.6, rd=298.8, games=993, win_rate=0.5156)
    v173 = _row(173, rating=1490.2, rd=120.0, games=645, win_rate=0.4651)

    selected = pool_ratchet.select_pool_weakest(
        [v11, v173], exclude_names={bot_name(173)}
    )

    assert selected["name"] == bot_name(11)


def _setup_snapshot(monkeypatch, tmp_path, rows):
    import evidence_snapshot

    names = [row["name"] for row in rows]
    for row in rows:
        bot_dir = tmp_path / "bots" / row["name"]
        bot_dir.mkdir(parents=True, exist_ok=True)
        (bot_dir / "national_bot.py").write_text("# native\n", encoding="utf-8")
    monkeypatch.setattr(tool_helpers, "PROJECT_ROOT", tmp_path)
    frozen = {
        "available": True,
        "selection": {"rows": rows, "active_bots": list(names)},
        "h2h": {},
        "manifest": {"manifest_digest": "d" * 64},
    }
    monkeypatch.setattr(
        evidence_snapshot,
        "load_generation_evaluation_snapshot",
        lambda _version: frozen,
    )


def test_binding_site_injects_measured_weakest(monkeypatch, tmp_path):
    """The tool_helpers binding (frozen snapshot rows -> pool_weakest_gate
    opponent) nominates the measured-weakest member under the new key."""
    rows = [
        _row(8, rating=1500.0, rd=100.0, games=900, win_rate=0.53),
        _row(11, rating=1487.6, rd=298.8, games=993, win_rate=0.5156),
        _row(173, rating=1490.2, rd=120.0, games=645, win_rate=0.4651),
    ]
    _setup_snapshot(monkeypatch, tmp_path, rows)
    monkeypatch.setenv("POK_PUBLISH_MODE", "pool_ratchet")

    selected = tool_helpers._select_precommit_opponents(9, 8, checkpoint={})

    by_name = {item["name"]: item["reason"] for item in selected}
    assert by_name[bot_name(8)] == "parent"
    assert by_name[bot_name(173)] == "pool_weakest_gate"
    assert by_name.get(bot_name(11)) != "pool_weakest_gate"


def test_nan_win_rate_degrades_to_tier_one_deterministically():
    # A NaN cell (JSON can carry one) must not break total-order
    # transitivity: the row falls to the conservative fallback and the
    # selection is independent of input row order (red-team 2026-10-16).
    strong_high_rd = {
        "name": "national_cloud_v9",
        "rating": 1800.0,
        "rd": 40.0,
        "games": 900,
        "win_rate": float("nan"),
    }
    weak_low_rd = {
        "name": "national_cloud_v2",
        "rating": 1500.0,
        "rd": 60.0,
        "games": 900,
        "win_rate": 0.44,
    }
    first = pool_ratchet.select_pool_weakest([strong_high_rd, weak_low_rd])
    second = pool_ratchet.select_pool_weakest([weak_low_rd, strong_high_rd])
    assert first["name"] == second["name"] == "national_cloud_v2"


def test_clustered_rating_fallback_keeps_documented_residual_shape():
    # Pins the DOCUMENTED residual (module comment): when two tier-1 rows'
    # plain-rating gap is smaller than twice their capped-rd difference, the
    # discount still decides the order — the clustered strong-high-rd member
    # is nominated weakest. Fail-safe direction (gate targets a too-strong
    # member); this test exists so any future fallback change is deliberate.
    clustered_strong = {
        "name": "national_cloud_v11",
        "rating": 1487.6,
        "rd": 298.8,
        "games": 500,
    }
    clustered_weak = {
        "name": "national_cloud_v173",
        "rating": 1490.2,
        "rd": 120.0,
        "games": 500,
    }
    picked = pool_ratchet.select_pool_weakest(
        [clustered_strong, clustered_weak]
    )
    assert picked["name"] == "national_cloud_v11"
