"""AIVAT (All-In Value Adjustment) variance-reduction unit tests.

Covers:
- aivat_equity: river exact path, MC convergence to literature values, tie handling.
- _extract_allin_snapshot: eligibility detection grounded in REAL judge-log
  structure (mutual allin, stack-mismatch allin [0,>0]/[>0,0] = the dominant
  real pattern, no-river not-eligible, allin-fold, fold-settled, explicit-fold).
- _hand_realized_delta: reads `temp_result` (per-hand mean-centered settlement,
  judge.py:570) with `final_result` as terminal-hand fallback.
- aivat_adjust_hand: math (equity*main_pot - matched_contrib) for eligible
  hands; realized passthrough otherwise. Side pots from stack-mismatch all-ins
  are excluded because they are returned deterministically, not runout variance.
- split_log_into_hands: off-by-one guard — hand N's settlement frame (rendered
  with hand=N+1, judge.py:577-578) stays in segment N.
- Real-structure integration: stack-mismatch allin [0,1]/pot=39999 + normal
  + terminal hands; verifies the three Phase-1 fixes compose end-to-end.
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
    _hand_realized_delta,
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
#
# NOTE on real-log structure (grounded in a 200-game mirror-replay scan):
#   judge.py settles a hand, increments `matchdata["hand"]` (judge.py:577-578),
#   then renders the settlement as `display.temp_result` on the FIRST display of
#   the NEXT hand number (make_request_json attaches the result). So the segment
#   for hand N contains: N's betting displays + one settlement display whose
#   `hand` field is N+1 and whose `player_chips` is the NEXT hand's reset state.
#   A snapshot helper must ignore settlement frames (see _extract_allin_snapshot).
#   Eligibility for an all-in-runout-to-showdown hand requires: ≥1 player at 0
#   chips (allin committed) AND no fold (-1 in round_player_bet) on the final
#   betting display AND the board run out to the river (5 public cards).


def _disp(hand=0, rpb=(0, 0), chips=(20000, 20000), pot=0, public=None,
          hole=None, round_=0, command="request", temp=None, final=None):
    if public is None:
        public = []
    if hole is None:
        hole = [[0, 1], [2, 3]]
    display = {
        "matchdata": {"hand": hand},
        "round": round_,
        "round_player_bet": list(rpb),
        "player_chips": list(chips),
        "pot": pot,
        "public_cards": list(public),
        "player_cards": [list(hole[0]), list(hole[1])],
    }
    if temp is not None:
        display["temp_result"] = temp
    if final is not None:
        display["final_result"] = final
    return {"output": {"command": command, "display": display}}


def _five_pub():
    """5 distinct public card ints for a river showdown display."""
    return [_card(13, 0), _card(12, 1), _card(9, 2), _card(8, 3), _card(4, 0)]


class TestSnapshotEligibility:
    def test_mutual_allin_eligible(self):
        # P0 shoves (chips[0]=0); river reached (5 public), no fold → mutual allin.
        log = [
            _disp(chips=(20000, 20000), pot=150, round_=0),
            _disp(chips=(0, 20000), pot=20100, round_=0),     # P0 allin
            _disp(chips=(0, 0), pot=40000, round_=3, public=_five_pub()),  # runout → showdown
        ]
        snap = _extract_allin_snapshot(log)
        assert snap is not None
        assert snap["pot"] == 40000
        assert snap["contrib_me"] == 20000
        assert snap["contrib_opp"] == 20000

    def test_stack_mismatch_allin_eligible(self):
        # P0 shoves, P1 covers with a stack-mismatch call: final chips [0, X>0].
        # This is the DOMINANT real-log all-in pattern (~95% of showdown allins).
        log = [
            _disp(chips=(20000, 20000), pot=150, round_=0),
            _disp(chips=(0, 20000), pot=20100, round_=0),     # P0 allin
            _disp(chips=(0, 1), pot=39999, round_=3, public=_five_pub(),
                  rpb=(0, 0)),                                # P1 covered → runout
        ]
        snap = _extract_allin_snapshot(log)
        assert snap is not None, "stack-mismatch allin must be eligible (the common case)"
        assert snap["pot"] == 39999
        assert snap["main_pot"] == 39998
        assert snap["matched_contrib"] == 19999
        assert snap["contrib_me"] == 20000
        assert snap["contrib_opp"] == 19999  # P1 only put in 19999 (kept 1)

    def test_allin_no_river_not_eligible(self):
        # Allin committed but board did NOT run to the river (preflop allin whose
        # runout never completed in this fragment) → no 5-card chance event here.
        log = [
            _disp(chips=(20000, 20000), pot=150, round_=0),
            _disp(chips=(0, 1), pot=39999, round_=0, public=[]),  # no public yet
        ]
        assert _extract_allin_snapshot(log) is None

    def test_allin_then_fold_not_eligible(self):
        # P0 allin, P1 folds → -1 in round_player_bet on the final display.
        log = [
            _disp(chips=(20000, 20000), pot=150, round_=0),
            _disp(chips=(0, 19900), pot=20100, round_=0),     # P0 allin, P1 didn't cover
            _disp(chips=(0, 19900), pot=20100, round_=3, public=_five_pub(),
                  rpb=(20000, -1)),                           # P1 folded
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
            _disp(chips=(0, 0), pot=40000, round_=3, public=_five_pub(), rpb=(20000, -1)),
        ]
        assert _extract_allin_snapshot(log) is None


# ── aivat_adjust_hand math ──

class TestAdjustHandMath:
    def test_eligible_hand_uses_expected_value(self):
        # AA vs KK allin preflop, mutual allin, pot=40000, contrib=20000 each,
        # runout to river.
        aa = [_card(14, 0), _card(14, 1)]
        kk = [_card(13, 0), _card(13, 1)]
        log = [
            _disp(chips=(20000, 20000), pot=150, round_=0, hole=[aa, kk]),
            _disp(chips=(0, 20000), pot=20100, round_=0, hole=[aa, kk]),
            _disp(chips=(0, 0), pot=40000, round_=3, hole=[aa, kk], public=_five_pub()),
        ]
        adj = aivat_adjust_hand(log, perspective=0, n_sims=2000, seed=7)
        eq = aivat_equity(aa, kk, [], 0, n_sims=2000, seed=7)
        expected = eq * 40000 - 20000
        assert abs(adj - expected) < 1e-6
        # AA should be a clear favorite → positive adjusted delta
        assert adj > 0

    def test_stack_mismatch_side_pot_excluded(self):
        # Only the matched main pot is subject to runout chance. The unmatched
        # side pot is returned to the covering player by settlement, so using the
        # full pot would bias a tie-equity hand by thousands of chips.
        board = [_card(14, 0), _card(13, 1), _card(12, 2), _card(11, 3), _card(10, 0)]
        h0 = [_card(2, 0), _card(3, 1)]
        h1 = [_card(4, 2), _card(5, 3)]
        log = [
            _disp(chips=(0, 14000), pot=26000, round_=3, public=board,
                  hole=[h0, h1], rpb=(0, 0)),
        ]
        snap = _extract_allin_snapshot(log)
        assert snap is not None
        assert snap["pot"] == 26000
        assert snap["main_pot"] == 12000
        assert snap["matched_contrib"] == 6000
        assert snap["contrib_me"] == 20000
        assert snap["contrib_opp"] == 6000

        adj = aivat_adjust_hand(log, perspective=0)
        # Both players play the board straight: equity=0.5, so main-pot EV is 0.
        assert adj == 0.0

    def test_fold_hand_passes_through_realized_temp_result(self):
        # Real-log structure: realized delta lives in `temp_result` (mean-centered
        # settlement, judge.py:570). A fold hand keeps its realized delta.
        log = [
            _disp(chips=(19950, 20100), pot=0, round_=0),
            _disp(chips=(19950, 20050), pot=0, round_=3,
                  temp=[{"win_chips": -50.0}, {"win_chips": 50.0}]),
        ]
        adj = aivat_adjust_hand(log, perspective=0)
        assert adj == -50.0

    def test_fold_hand_passes_through_realized_final_result_fallback(self):
        # Terminal hand: realized delta only on the finish frame's final_result.
        log = [
            _disp(chips=(19950, 20100), pot=0, round_=0),
            _disp(chips=(19950, 20050), pot=0, round_=3,
                  final=[{"win_chips": -50.0}, {"win_chips": 50.0}], command="finish"),
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

    def test_settlement_frame_stays_with_current_hand(self):
        # Real-log off-by-one guard: hand N's settlement is rendered on a display
        # whose `hand` field is N+1 (judge.py:577-578 increments before render).
        # That settlement display must stay in segment N, NOT open segment N+1.
        log = [
            _disp(hand=0, chips=(20000, 20000)),
            _disp(hand=0, chips=(19950, 20050)),
            # hand 0's settlement, rendered with hand=1:
            _disp(hand=1, chips=(20000, 19900), temp=[{"win_chips": 50.0}, {"win_chips": -50.0}]),
            _disp(hand=1, chips=(20050, 19950)),
            # hand 1's settlement, rendered with hand=2:
            _disp(hand=2, chips=(20000, 20000), temp=[{"win_chips": -100.0}, {"win_chips": 100.0}]),
            _disp(hand=2, chips=(19950, 20050)),
            # terminal finish frame for hand 2:
            _disp(hand=2, chips=(19950, 20050),
                  final=[{"win_chips": -50.0}, {"win_chips": 50.0}], command="finish"),
        ]
        hands = split_log_into_hands(log)
        assert len(hands) == 3
        # hand 0 segment owns its settlement (temp_result win_chips=+50 for P0)
        assert _hand_realized_delta(hands[0], 0) == 50.0
        # hand 1 segment owns its settlement (temp_result win_chips=-100 for P0)
        assert _hand_realized_delta(hands[1], 0) == -100.0
        # hand 2 (terminal) owns its finish-frame final_result (-50 for P0)
        assert _hand_realized_delta(hands[2], 0) == -50.0


# ── Real-structure integration: stack-mismatch allin + normal hands ──

class TestRealStructureIntegration:
    """Fixture built from the measured real-log display sequence (see explore
    diagnosis: 20260616 replay, hand 0 stack-mismatch allin chips [0,1] pot
    39999). Verifies the three Phase-1 fixes compose correctly end-to-end."""

    @staticmethod
    def _build_real_game():
        def _c(num, suit):
            return (num - 2) * 4 + suit

        aa = [_c(14, 0), _c(14, 1)]   # P0 will have aces (favorite)
        kk = [_c(13, 0), _c(13, 1)]   # P1 kings
        river = [_c(12, 0), _c(11, 0), _c(10, 0), _c(9, 0), _c(8, 0)]
        log = []
        # hand 0: stack-mismatch allin runout to river
        log.append(_disp(hand=0, round_=0, chips=(20000, 20000), pot=150, public=[]))
        log.append(_disp(hand=0, round_=0, chips=(0, 20000), pot=20100, public=[], hole=[aa, kk]))
        log.append(_disp(hand=0, round_=1, chips=(0, 1), pot=39999, public=[_c(12, 0)], hole=[aa, kk]))
        log.append(_disp(hand=0, round_=2, chips=(0, 1), pot=39999, public=[_c(12, 0), _c(11, 0)], hole=[aa, kk]))
        log.append(_disp(hand=0, round_=3, chips=(0, 1), pot=39999, public=river, hole=[aa, kk], rpb=(0, 0)))
        # hand 0 settlement on first display of hand 1 (P0 won the pot → +19999)
        log.append(_disp(hand=1, round_=0, chips=(39999, 1), pot=150, public=[],
                         hole=[aa, kk], temp=[{"win_chips": 19999.0}, {"win_chips": -19999.0}]))
        # hand 1: a normal fold/settled hand (small swing)
        log.append(_disp(hand=1, round_=0, chips=(39900, 96), pot=150, public=[]))
        log.append(_disp(hand=2, round_=0, chips=(39900, 96), pot=150, public=[],
                         temp=[{"win_chips": -99.0}, {"win_chips": 99.0}]))
        # hand 2: normal, then terminal finish
        log.append(_disp(hand=2, round_=0, chips=(39900, 96), pot=150, public=[]))
        log.append(_disp(hand=2, round_=0, chips=(39900, 96), pot=0, public=[],
                         final=[{"win_chips": 19900.0}, {"win_chips": -19900.0}], command="finish"))
        return log, aa, kk

    def test_split_into_three_hands(self):
        log, _, _ = self._build_real_game()
        hands = split_log_into_hands(log)
        assert len(hands) == 3

    def test_stack_mismatch_allin_detected_and_adjusted(self):
        from aivat import aivat_adjust_hand, _extract_allin_snapshot, aivat_equity
        log, aa, kk = self._build_real_game()
        hands = split_log_into_hands(log)
        snap = _extract_allin_snapshot(hands[0])
        assert snap is not None, "stack-mismatch [0,1] allin must be eligible"
        # captured at the all-in moment (preflop), with a one-chip unmatched side pot
        assert snap["pot"] == 39999
        assert snap["main_pot"] == 39998
        assert snap["matched_contrib"] == 19999
        assert snap["contrib_me"] == 20000
        assert snap["contrib_opp"] == 19999
        adj0 = aivat_adjust_hand(hands[0], perspective=0, n_sims=2000, seed=3)
        eq = aivat_equity(aa, kk, snap["public"], snap["round"], n_sims=2000, seed=3)
        expected = eq * snap["main_pot"] - snap["matched_contrib"]
        assert abs(adj0 - expected) < 1e-6
        # AA is a strong favorite → adjusted delta is clearly positive (skill EV)
        assert adj0 > 5000

    def test_unbiased_vs_demo_bias(self):
        """Sanity: a fold-then-allin game should NOT show the spurious -20460
        bias that the buggy (running-total-as-realized, both-0-only) version
        produced. The AIVAT-adjusted total must equal sum of per-hand skill EV
        on allin + realized on fold hands, and is bounded by the pot (not a
        huge negative blowup)."""
        from aivat import aivat_adjust_game, aivat_adjust_hand
        log, _, _ = self._build_real_game()
        hands = split_log_into_hands(log)
        hand0 = aivat_adjust_hand(hands[0], perspective=0, n_sims=2000, seed=3)
        total = aivat_adjust_game(log, perspective=0, n_sims=2000, seed=3)
        assert abs(total - (hand0 - 99.0)) < 1e-6
        assert total > 0

    def test_final_result_cumulative_total_is_converted_to_terminal_delta(self):
        # final_result is game-level cumulative total, not terminal-hand delta.
        # aivat_adjust_game must subtract prior temp_result deltas before adding
        # the last non-allin hand, otherwise it double-counts the whole game.
        log = [
            _disp(hand=0, chips=(19950, 20050)),
            _disp(hand=1, chips=(20000, 20000),
                  temp=[{"win_chips": 100.0}, {"win_chips": -100.0}]),
            _disp(hand=1, chips=(20050, 19950)),
            _disp(hand=1, chips=(20050, 19950), command="finish",
                  final=[{"win_chips": 150.0}, {"win_chips": -150.0}]),
        ]
        assert aivat_adjust_game(log, perspective=0) == 150.0
