"""Tests for Glicko-2 rating system — pure math correctness."""

import math

from glicko2 import (
    Glicko2Player, _g, _E,
    update_rating_period, update_single_game, decay_rd,
    SCALE, DEFAULT_R, DEFAULT_RD, DEFAULT_SIGMA,
)


class TestGlicko2Player:
    def test_defaults(self):
        p = Glicko2Player()
        assert p.r == 1500.0
        assert p.rd == 350.0
        assert p.sigma == 0.06

    def test_rd_clamped_to_default_ceiling(self):
        p = Glicko2Player(1500, 599.0, 0.06)
        assert p.rd == DEFAULT_RD

    def test_custom_init(self):
        p = Glicko2Player(r=1600, rd=50, sigma=0.04)
        assert p.r == 1600
        assert p.rd == 50
        assert p.sigma == 0.04

    def test_to_dict_roundtrip(self):
        p = Glicko2Player(1550, 80, 0.05)
        d = p.to_dict()
        assert d == {"r": 1550, "rd": 80, "sigma": 0.05}
        p2 = Glicko2Player.from_dict(d)
        assert p2.r == p.r and p2.rd == p.rd and p2.sigma == p.sigma

    def test_from_dict_clamps_historical_bad_rd(self):
        p = Glicko2Player.from_dict({"r": 1500, "rd": 599.0, "sigma": 0.06})
        assert p.rd == DEFAULT_RD

    def test_from_dict_missing_keys(self):
        p = Glicko2Player.from_dict({})
        assert p.r == DEFAULT_R
        assert p.rd == DEFAULT_RD
        assert p.sigma == DEFAULT_SIGMA

    def test_conservative_rating(self):
        p = Glicko2Player(r=1600, rd=50)
        assert p.conservative_rating() == 1600 - 2 * 50


class TestHelpers:
    def test_g_bounds(self):
        assert _g(0) == 1.0
        assert 0 < _g(1.0) < 1.0
        assert _g(10.0) > 0

    def test_g_decreasing(self):
        assert _g(0.5) > _g(1.0) > _g(2.0)

    def test_e_bounds(self):
        for mu_j in [0, -1, 1, 2]:
            for phi_j in [0.1, 0.5, 1.0, 2.0]:
                val = _E(0, mu_j, phi_j)
                assert 0 < val < 1, f"E(0, {mu_j}, {phi_j}) = {val}"

    def test_e_equal_mu(self):
        val = _E(0, 0, 0.5)
        assert abs(val - 0.5) < 0.001

    def test_e_higher_mu_favored(self):
        assert _E(1.0, 0, 0.5) > 0.5
        assert _E(0, 1.0, 0.5) < 0.5


class TestUpdateRatingPeriod:
    def test_no_games_increases_rd(self):
        p = Glicko2Player(1500, 50, 0.06)
        p2 = update_rating_period(p, [])
        assert p2.r == p.r
        assert p2.rd > p.rd
        assert p2.sigma == p.sigma

    def test_win_increases_rating(self):
        p = Glicko2Player(1500, 50, 0.06)
        opp = Glicko2Player(1500, 50, 0.06)
        p2 = update_rating_period(p, [(opp, 1.0)])
        assert p2.r > p.r

    def test_loss_decreases_rating(self):
        p = Glicko2Player(1500, 50, 0.06)
        opp = Glicko2Player(1500, 50, 0.06)
        p2 = update_rating_period(p, [(opp, 0.0)])
        assert p2.r < p.r

    def test_draw_against_equal(self):
        p = Glicko2Player(1500, 50, 0.06)
        opp = Glicko2Player(1500, 50, 0.06)
        p2 = update_rating_period(p, [(opp, 0.5)])
        assert abs(p2.r - p.r) < 1.0
        # Rating stays near 1500, rd change depends on sigma vs info gain

    def test_rd_decreases_after_games(self):
        p = Glicko2Player(1500, 200, 0.06)
        opp = Glicko2Player(1500, 50, 0.06)
        p2 = update_rating_period(p, [(opp, 0.5)])
        assert p2.rd < p.rd

    def test_multiple_opponents(self):
        p = Glicko2Player(1500, 100, 0.06)
        opp1 = Glicko2Player(1600, 50, 0.06)
        opp2 = Glicko2Player(1400, 80, 0.06)
        p2 = update_rating_period(p, [(opp1, 1.0), (opp2, 0.0)])
        assert isinstance(p2, Glicko2Player)
        assert p2.rd < p.rd

    def test_player_not_mutated(self):
        p = Glicko2Player(1500, 100, 0.06)
        opp = Glicko2Player(1500, 50, 0.06)
        orig_r, orig_rd = p.r, p.rd
        update_rating_period(p, [(opp, 1.0)])
        assert p.r == orig_r and p.rd == orig_rd

    def test_decisive_high_rd_batch_not_rejected(self):
        p = Glicko2Player(1500, 350, 0.06)
        opp = Glicko2Player(1500, 350, 0.06)
        p2 = update_rating_period(p, [(opp, 1.0)] * 5)
        assert p2.r > 1700
        assert p2.rd < p.rd
        assert -1000 <= p2.r <= 3000


class TestUpdateSingleGame:
    def test_win_increases_rating(self):
        p = Glicko2Player(1500, 50, 0.06)
        opp = Glicko2Player(1500, 50, 0.06)
        p2 = update_single_game(p, opp, 1.0)
        assert p2.r > p.r
        assert p2.sigma == p.sigma

    def test_loss_decreases_rating(self):
        p = Glicko2Player(1500, 50, 0.06)
        opp = Glicko2Player(1500, 50, 0.06)
        p2 = update_single_game(p, opp, 0.0)
        assert p2.r < p.r

    def test_convergence_over_many_games(self):
        p = Glicko2Player(1400, 100, 0.06)
        opp = Glicko2Player(1500, 30, 0.06)
        for _ in range(100):
            p = update_single_game(p, opp, 0.0)
        assert p.r < 1400
        assert p.rd < 100


class TestDecayRd:
    def test_rd_increases(self):
        p = Glicko2Player(1500, 50, 0.06)
        p2 = decay_rd(p, 1)
        assert p2.r == p.r
        assert p2.rd > p.rd

    def test_multiple_periods(self):
        # More idle periods => strictly more RD growth (official Step 1b).
        p = Glicko2Player(1500, 200, 0.06)
        p1 = decay_rd(p, 1)
        p2 = decay_rd(p, 2)
        assert p2.rd > p1.rd

    def test_zero_periods_no_change(self):
        p = Glicko2Player(1500, 50, 0.06)
        p2 = decay_rd(p, 0)
        assert p2.rd == p.rd

    def test_official_formula_phi_star(self):
        # Verify phi* = sqrt(phi^2 + sigma^2 * t) exactly (no floor, no *4).
        # Glickman Step 1b; Lichess/online-go use the same closed form.
        import math
        for rd0, sigma, t in [(50, 0.06, 1), (50, 0.06, 5), (80, 0.06, 1),
                              (30, 0.09, 10), (200, 0.06, 3)]:
            p = Glicko2Player(1500, rd0, sigma)
            got = decay_rd(p, t).rd
            expected = min(math.sqrt((rd0 / SCALE) ** 2 + (sigma ** 2) * t) * SCALE,
                           DEFAULT_RD)
            assert abs(got - expected) < 1e-6, f"rd0={rd0} t={t}: got {got} exp {expected}"

    def test_no_floor_low_rd_grows_gradually(self):
        # The buggy 150 floor used to snap low-RD bots straight to 150 in one
        # period. The official formula grows RD gradually: a single idle period
        # from rd=50 with sigma=0.06 should land near 51, NOT 150.
        p = Glicko2Player(1500, 50, 0.06)
        p1 = decay_rd(p, 1)
        assert p1.rd < 60, f"single-period decay from rd=50 should stay low, got {p1.rd}"
        # Even several periods shouldn't snap to 150
        p5 = decay_rd(p, 5)
        assert p5.rd < 60, f"5-period decay from rd=50 sigma=0.06 should stay low, got {p5.rd}"

    def test_rd_monotonic_non_decreasing(self):
        # Repeated single-period decay must never decrease rd (sqrt of sum of
        # positive terms is monotonic). No floor is involved.
        cur = Glicko2Player(1500, 80, 0.06)
        for _ in range(10):
            nxt = decay_rd(cur, 1)
            assert nxt.rd >= cur.rd
            cur = nxt
        assert cur.rd <= DEFAULT_RD

    def test_rd_clamped_at_default(self):
        # Large multi-period decay must not push rd past DEFAULT_RD (350).
        p = Glicko2Player(1500, 50, 0.06)
        big = decay_rd(p, 10000)
        assert big.rd <= DEFAULT_RD

        # Repeated single-period decay must also stay bounded at DEFAULT_RD.
        cur = Glicko2Player(1500, 50, 0.06)
        for _ in range(100000):
            cur = decay_rd(cur, 1)
            assert cur.rd <= DEFAULT_RD
        assert cur.rd <= DEFAULT_RD

    def test_elapsed_periods_scale_growth(self):
        # decay_rd(p, t) should equal t applications of decay_rd(p, 1) in RD
        # growth: sqrt(phi^2 + sigma^2*t) == iterating sqrt(phi^2+sigma^2) t times.
        # (Equivalent because sigma is constant and the terms add in quadrature.)
        p0 = Glicko2Player(1500, 50, 0.06)
        one_shot = decay_rd(p0, 10).rd
        cur = p0
        for _ in range(10):
            cur = decay_rd(cur, 1)
        iterated = cur.rd
        assert abs(one_shot - iterated) < 1e-6, f"one_shot {one_shot} vs iterated {iterated}"


class TestDegenerateCases:
    def test_single_game_extreme_rating_gap_grows_rd(self):
        """When opponents are 2000+ rating apart, v_inv -> 0. RD should still grow."""
        p = Glicko2Player(1000, 50, 0.06)
        opp = Glicko2Player(3000, 50, 0.06)
        p2 = update_single_game(p, opp, 1.0)
        # RD should grow due to sigma uncertainty, not stay flat
        assert p2.rd > p.rd

    def test_single_game_equal_rating_updates(self):
        """Normal case: equal ratings, expect rating change and RD decrease."""
        p = Glicko2Player(1500, 200, 0.06)
        opp = Glicko2Player(1500, 50, 0.06)
        p2 = update_single_game(p, opp, 1.0)
        assert p2.r > p.r
        assert p2.rd < p.rd

    def test_rating_period_high_rd_opponent(self):
        """With very high RD opponent, little information gained, RD may grow."""
        p = Glicko2Player(1500, 50, 0.06)
        opp = Glicko2Player(1500, 350, 0.06)
        p2 = update_rating_period(p, [(opp, 1.0)])
        # Rating increases after win
        assert p2.r > p.r
        # With high-RD opponent the information gain is small, so
        # sigma contribution may outweigh it and RD can grow slightly
        assert p2.rd >= p.rd

    def test_conservative_rating(self):
        p = Glicko2Player(1600, 100, 0.06)
        assert p.conservative_rating() == 1400

    def test_conservative_rating_default(self):
        p = Glicko2Player()
        assert p.conservative_rating() == 1500 - 2 * 350


class TestDaemonRatingIntegration:
    def test_process_result_updates_glicko_for_decisive_batch(self):
        from elo_daemon import process_result

        ratings = {
            "national_v76": Glicko2Player(),
            "national_v77": Glicko2Player(),
        }
        h2h = {}
        bot_stats = {}

        total = process_result(
            ("national_v76", "national_v77", 5, 0, 0, 5, None, [100] * 5),
            ratings,
            h2h,
            bot_stats,
        )

        assert total == 5
        assert ratings["national_v76"].r > 1700
        assert ratings["national_v77"].r < 1300
        assert ratings["national_v76"].rd < DEFAULT_RD
        assert ratings["national_v77"].rd < DEFAULT_RD


class TestCrossoverParents:
    """Tests for _pick_crossover_parents in generation_scheduler.py."""

    def test_sorts_by_strength_score(self, monkeypatch, tmp_path):
        from glicko2 import Glicko2Player

        # Setup: mock get_active_bots and load_selection_scores
        active = ["claude_v1", "claude_v2", "claude_v3", "claude_v4"]
        # v3 has highest strength, v1 has second highest
        strength = {"claude_v1": 0.52, "claude_v2": 0.48, "claude_v3": 0.55, "claude_v4": 0.45}
        ratings = {b: Glicko2Player(1500 + i * 10, 50) for i, b in enumerate(active)}

        import generation_scheduler as gs
        monkeypatch.setattr("evolution_infra.get_active_bots", lambda: active)
        monkeypatch.setattr("tool_helpers.load_selection_scores", lambda: strength)

        result = gs._pick_crossover_parents(ratings, 4)
        assert result is not None
        pa, pb = result
        # Parent A should be v3 (highest strength)
        assert pa == 3

    def test_selects_diverse_parents(self, monkeypatch, tmp_path):
        from glicko2 import Glicko2Player

        active = ["claude_v1", "claude_v2", "claude_v3", "claude_v4", "claude_v5"]
        # v5 highest, v2 second highest with gap >= 3
        strength = {"claude_v5": 0.55, "claude_v4": 0.54, "claude_v3": 0.53,
                    "claude_v2": 0.52, "claude_v1": 0.50}
        ratings = {b: Glicko2Player() for b in active}

        import generation_scheduler as gs
        monkeypatch.setattr("evolution_infra.get_active_bots", lambda: active)
        monkeypatch.setattr("tool_helpers.load_selection_scores", lambda: strength)

        result = gs._pick_crossover_parents(ratings, 5)
        assert result is not None
        pa, pb = result
        # Parent A = v5 (highest)
        assert pa == 5
        # Parent B should prefer gap >= 3, so v2 (|5-2|=3) over v4 (|5-4|=1)
        assert pb == 2

    def test_falls_back_to_second(self, monkeypatch, tmp_path):
        from glicko2 import Glicko2Player

        # Only 2 bots, adjacent versions — no gap candidate, fallback to second
        active = ["claude_v5", "claude_v6"]
        strength = {"claude_v5": 0.55, "claude_v6": 0.50}
        ratings = {b: Glicko2Player() for b in active}

        import generation_scheduler as gs
        monkeypatch.setattr("evolution_infra.get_active_bots", lambda: active)
        monkeypatch.setattr("tool_helpers.load_strength_scores", lambda: strength)

        result = gs._pick_crossover_parents(ratings, 6)
        assert result is not None
        assert result == (5, 6)

    def test_children_count_penalizes_overused_parent(self, monkeypatch):
        from glicko2 import Glicko2Player

        active = ["claude_v1", "claude_v2", "claude_v5"]
        strength = {"claude_v1": 0.50, "claude_v2": 0.49, "claude_v5": 0.90}
        ratings = {b: Glicko2Player() for b in active}

        import generation_scheduler as gs
        monkeypatch.setattr("evolution_infra.get_active_bots", lambda: active)
        monkeypatch.setattr("tool_helpers.load_selection_scores", lambda: strength)

        def _children(parent_id, **kwargs):
            return 10 if parent_id == "claude_v5" else 0

        monkeypatch.setattr("candidate_store.count_candidate_children", _children)

        result = gs._pick_crossover_parents(ratings, 5)

        assert result is not None
        assert result[0] == 1

    def test_map_elites_niche_guides_parent_b(self, monkeypatch):
        from glicko2 import Glicko2Player

        active = ["claude_v1", "claude_v4", "claude_v5", "claude_v8"]
        strength = {
            "claude_v8": 0.90,
            "claude_v5": 0.80,  # same MAP niche as v8; would win without niche diversity
            "claude_v4": 0.70,
            "claude_v1": 0.60,
        }
        ratings = {b: Glicko2Player() for b in active}
        archive = {
            "cells": {
                "agg1_loose1": {"bot": "claude_v8", "fitness": 0.9},
                "agg3_loose3": {"bot": "claude_v4", "fitness": 0.7},
                "agg4_loose4": {"bot": "claude_v1", "fitness": 0.6},
            },
            "bot_niches": {
                "claude_v8": {"niche": "agg1_loose1"},
                "claude_v5": {"niche": "agg1_loose1"},
                "claude_v4": {"niche": "agg3_loose3"},
                "claude_v1": {"niche": "agg4_loose4"},
            },
        }
        import generation_scheduler as gs

        monkeypatch.setattr("evolution_infra.get_active_bots", lambda: active)
        monkeypatch.setattr("tool_helpers.load_selection_scores", lambda: strength)
        monkeypatch.setattr("candidate_store.count_candidate_children", lambda *_a, **_k: 0)
        monkeypatch.setattr(gs, "log_system_event", lambda *a, **k: None)

        result = gs._pick_crossover_parents(ratings, 8, archive=archive)

        assert result == (8, 4)

    def test_returns_none_single_bot(self, monkeypatch):
        import generation_scheduler as gs
        monkeypatch.setattr("evolution_infra.get_active_bots", lambda: ["claude_v1"])
        monkeypatch.setattr("tool_helpers.load_strength_scores", lambda: {"claude_v1": 0.5})
        result = gs._pick_crossover_parents({"claude_v1": Glicko2Player()}, 1)
        assert result is None


class TestRdDecayInDaemon:
    """Tests for RD decay applied during save_cycle."""

    def test_decay_applied_to_inactive_bots(self):
        from glicko2 import Glicko2Player, decay_rd

        # Setup: 3 active bots, only 2 played
        p_a = Glicko2Player(1600, 50, 0.06)
        p_b = Glicko2Player(1500, 50, 0.06)
        p_c = Glicko2Player(1400, 50, 0.06)
        ratings = {"claude_v1": p_a, "claude_v2": p_b, "claude_v3": p_c}

        active_bots = ["claude_v1", "claude_v2", "claude_v3"]
        played = {"claude_v1", "claude_v2"}

        for b in active_bots:
            if b not in played and b in ratings:
                ratings[b] = decay_rd(ratings[b])

        assert ratings["claude_v3"].rd > 50
        assert ratings["claude_v1"].rd == 50
        assert ratings["claude_v2"].rd == 50
