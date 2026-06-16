"""AIVAT (All-In Value Adjustment) variance-reduction unit tests.

Covers:
- aivat_equity: river exact path, MC convergence to literature values, tie handling.
- _extract_allin_snapshot: eligibility detection (allin-showdown vs allin-fold vs fold).
- aivat_adjust_hand: math (equity*pot - contrib) for eligible hands; realized passthrough otherwise.
- Determinism: same seed → same adjusted value.
"""
import os
import sys

WEB_CORE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE_DIR = os.path.join(os.path.dirname(WEB_CORE), "engine")
if ENGINE_DIR not in sys.path:
    sys.path.insert(0, ENGINE_DIR)

import pytest

from aivat import (
    aivat_equity,
    _river_equity,
    _extract_allin_snapshot,
    aivat_adjust_hand,
    aivat_adjust_game,
    split_log_into_hands,
)


def _card(num, suit):
    """number 2..14, suit 0..3 → engine int."""
    return (num - 2) * 4 + suit


# ── Equity: river exact ──

class TestRiverEquity:
    def test_win(self):
        # AA vs air on a board with no pair/straight for the air hand
        board = [_card(13, 0), _card(12, 1), _card(9, 2), _card(8, 3), _card(4, 0)]
        aa = [_card(14, 1), _card(14, 2)]
        air = [_card(2, 2), _card(3, 3)]
        assert _river_equity(aa, air, board) == 1.0
        assert _river_equity(air, aa, board) == 0.0

    def test_tie_play_board(self):
        # Board straight A-K-Q-J-T: both play the board → tie 0.5
        board = [_card(14, 0), _card(13, 1), _card(12, 2), _card(11, 3), _card(10, 0)]
        h0 = [_card(2, 0), _card(3, 1)]
        h1 = [_card(4, 2), _card(5, 3)]
        assert _river_equity(h0, h1, board) == 0.5

    def test_dispatcher_river(self):
        board = [_card(13, 0), _card(12, 1), _card(9, 2), _card(8, 3), _card(4, 0)]
        aa = [_card(14, 1), _card(14, 2)]
        air = [_card(2, 2), _card(3, 3)]
        assert aivat_equity(aa, air, board, 3) == 1.0


# ── Equity: MC convergence to literature ──

class TestMCEquityConvergence:
    def test_aa_vs_kk_preflop(self):
        aa = [_card(14, 0), _card(14, 1)]
        kk = [_card(13, 0), _card(13, 1)]
        eq = aivat_equity(aa, kk, [], 0, n_sims=8000, seed=1)
        # Literature: ~0.821. Allow ±0.03 (≈ 3σ at n=8000).
        assert 0.79 <= eq <= 0.85

    def test_22_vs_ako_preflop_coinflip(self):
        two = [_card(2, 0), _card(2, 1)]
        ako = [_card(14, 0), _card(13, 1)]
        eq = aivat_equity(two, ako, [], 0, n_sims=8000, seed=2)
        # Literature: ~0.52
        assert 0.48 <= eq <= 0.56

    def test_ako_vs_qq_pair_favorite(self):
        qq = [_card(12, 1), _card(12, 2)]
        ako = [_card(14, 0), _card(13, 1)]
        eq = aivat_equity(ako, qq, [], 0, n_sims=8000, seed=3)
        # Literature: ~0.43 (QQ favored)
        assert 0.39 <= eq <= 0.47

    def test_mc_deterministic_with_seed(self):
        aa = [_card(14, 0), _card(14, 1)]
        kk = [_card(13, 0), _card(13, 1)]
        e1 = aivat_equity(aa, kk, [], 0, n_sims=2000, seed=42)
        e2 = aivat_equity(aa, kk, [], 0, n_sims=2000, seed=42)
        assert e1 == e2

    def test_equity_in_unit_interval(self):
        for a, b in [([0, 1], [2, 3]), ([48, 49], [44, 45]), ([0, 4], [48, 51])]:
            e = aivat_equity(a, b, [], 0, n_sims=500, seed=0)
            assert 0.0 <= e <= 1.0


# ── Snapshot eligibility detection (synthetic judge-log fragments) ──

def _disp(hand=0, rpb=(0, 0), chips=(20000, 20000), pot=0, public=None,
          hole=None, round_=0, command="request"):
    if public is None:
        public = []
    if hole is None:
        hole = [[0, 1], [2, 3]]
    return {
        "output": {
            "command": command,
            "display": {
                "matchdata": {"hand": hand},
                "round": round_,
                "round_player_bet": list(rpb),
                "player_chips": list(chips),
                "pot": pot,
                "public_cards": list(public),
                "player_cards": [list(hole[0]), list(hole[1])],
            },
        }
    }


class TestSnapshotEligibility:
    def test_mutual_allin_eligible(self):
        # First display: P0 shoves (chips[0]=0). Final: both at 0, pot=40000.
        log = [
            _disp(chips=(20000, 20000), pot=150, round_=0),
            _disp(chips=(0, 20000), pot=20100, round_=0),     # P0 allin
            _disp(chips=(0, 0), pot=40000, round_=1),         # P1 called → mutual allin
        ]
        snap = _extract_allin_snapshot(log)
        assert snap is not None
        assert snap["pot"] == 40000
        assert snap["contrib_me"] == 20000
        assert snap["contrib_opp"] == 20000

    def test_allin_then_fold_not_eligible(self):
        # P0 allin, P1 folds → only P0 at 0 chips.
        log = [
            _disp(chips=(20000, 20000), pot=150, round_=0),
            _disp(chips=(0, 19900), pot=20100, round_=0),     # P0 allin, P1 didn't cover
        ]
        assert _extract_allin_snapshot(log) is None

    def test_fold_settled_not_eligible(self):
        # No allin: nobody at 0 chips.
        log = [
            _disp(chips=(19950, 20100), pot=0, round_=0),
        ]
        assert _extract_allin_snapshot(log) is None

    def test_explicit_fold_excluded(self):
        # Both chips 0 but a fold sentinel present → defensive exclusion.
        log = [
            _disp(chips=(0, 0), pot=40000, round_=1, rpb=(20000, -1)),
        ]
        assert _extract_allin_snapshot(log) is None


# ── aivat_adjust_hand math ──

class TestAdjustHandMath:
    def test_eligible_hand_uses_expected_value(self):
        # AA vs KK allin preflop, mutual allin, pot=40000, contrib=20000 each.
        aa = [_card(14, 0), _card(14, 1)]
        kk = [_card(13, 0), _card(13, 1)]
        log = [
            _disp(chips=(20000, 20000), pot=150, round_=0, hole=[aa, kk]),
            _disp(chips=(0, 20000), pot=20100, round_=0, hole=[aa, kk]),
            _disp(chips=(0, 0), pot=40000, round_=1, hole=[aa, kk]),
        ]
        # Add a final_result display so realized delta is parseable
        log.append({
            "output": {
                "command": "request",
                "display": {
                    "matchdata": {"hand": 0},
                    "round": 3,
                    "round_player_bet": [0, 0],
                    "player_chips": [0, 0],
                    "pot": 40000,
                    "public_cards": [],
                    "player_cards": [aa, kk],
                    "final_result": [{"win_chips": 20000}, {"win_chips": -20000}],
                },
            }
        })
        adj = aivat_adjust_hand(log, perspective=0, n_sims=2000, seed=7)
        eq = aivat_equity(aa, kk, [], 0, n_sims=2000, seed=7)
        expected = eq * 40000 - 20000
        assert abs(adj - expected) < 1e-6
        # AA should be a clear favorite → positive adjusted delta
        assert adj > 0

    def test_fold_hand_passes_through_realized(self):
        log = [
            _disp(chips=(19950, 20100), pot=0, round_=0),
            {
                "output": {
                    "command": "request",
                    "display": {
                        "matchdata": {"hand": 0},
                        "round": 3,
                        "round_player_bet": [50, 100],
                        "player_chips": [19950, 20050],
                        "pot": 0,
                        "public_cards": [],
                        "player_cards": [[0, 1], [2, 3]],
                        "final_result": [{"win_chips": -50}, {"win_chips": 50}],
                    },
                }
            },
        ]
        adj = aivat_adjust_hand(log, perspective=0)
        assert adj == -50.0


# ── split_log_into_hands ──

class TestSplitHands:
    def test_splits_on_hand_increment(self):
        log = [
            _disp(hand=0, chips=(20000, 20000)),
            _disp(hand=0, chips=(19950, 20050)),
            _disp(hand=1, chips=(20000, 20000)),   # new hand
            _disp(hand=1, chips=(20050, 19950)),
        ]
        hands = split_log_into_hands(log)
        assert len(hands) == 2
