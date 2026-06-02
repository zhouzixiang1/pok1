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
        p = Glicko2Player(1500, 50, 0.06)
        p1 = decay_rd(p, 1)
        p2 = decay_rd(p, 2)
        assert p2.rd > p1.rd

    def test_zero_periods_no_change(self):
        p = Glicko2Player(1500, 50, 0.06)
        p2 = decay_rd(p, 0)
        assert p2.rd == p.rd
