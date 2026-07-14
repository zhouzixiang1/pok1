import importlib.util
import os
import pathlib
import time
import unittest


os.environ["GUOSAI_DEBUG"] = "0"
MODULE_PATH = pathlib.Path(__file__).with_name("国赛平台代码.py")
SPEC = importlib.util.spec_from_file_location("guosai_bot", MODULE_PATH)
GUOSAI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GUOSAI)
GUOSAI.GUOSAI_SEND_GAP_SECONDS = 0


class FakeSocket:
    def __init__(self):
        self.sent = []

    def sendall(self, data):
        self.sent.append(data)


class GuosaiProtocolTests(unittest.TestCase):
    def test_send_has_no_unlisted_delimiter(self):
        sock = FakeSocket()

        GUOSAI.send_guosai(sock, "raise 400")

        self.assertEqual(sock.sent, [b"raise 400"])

    def test_splits_preflop_and_appended_action(self):
        messages, remainder = GUOSAI.split_guosai_messages(
            "preflop|BIGBLIND|<0,6><1,6>raise 300"
        )

        self.assertEqual(
            messages,
            ["preflop|BIGBLIND|<0,6><1,6>", "raise 300"],
        )
        self.assertEqual(remainder, "")

    def test_splits_showdown_and_earnings(self):
        messages, remainder = GUOSAI.split_guosai_messages(
            "oppo_hands|<1,2><2,1>earnChips -200"
        )

        self.assertEqual(
            messages,
            ["oppo_hands|<1,2><2,1>", "earnChips -200"],
        )
        self.assertEqual(remainder, "")

    def test_keeps_incomplete_stage_message_for_next_recv(self):
        messages, remainder = GUOSAI.split_guosai_messages(
            "preflop|BIGBLIND|<0,6>"
        )

        self.assertEqual(messages, [])
        self.assertEqual(remainder, "preflop|BIGBLIND|<0,6>")

        messages, remainder = GUOSAI.split_guosai_messages(
            remainder + "<1,6>call"
        )
        self.assertEqual(
            messages,
            ["preflop|BIGBLIND|<0,6><1,6>", "call"],
        )
        self.assertEqual(remainder, "")

    def test_split_discards_noise_but_keeps_partial_message_prefix(self):
        messages, remainder = GUOSAI.split_guosai_messages("xyz???")
        self.assertEqual(messages, [])
        self.assertEqual(remainder, "")

        messages, remainder = GUOSAI.split_guosai_messages("pre")
        self.assertEqual(messages, [])
        self.assertEqual(remainder, "pre")

        messages, remainder = GUOSAI.split_guosai_messages(
            "noise-preflop|BIGBLIND|<0,6><1,6>call"
        )
        self.assertEqual(messages, ["preflop|BIGBLIND|<0,6><1,6>", "call"])
        self.assertEqual(remainder, "")

    def test_big_blind_responds_to_action_appended_after_hand(self):
        adapter = GUOSAI.GuosaiProtocolAdapter()
        adapter.timed_decide = lambda: "check"
        messages, _ = GUOSAI.split_guosai_messages(
            "preflop|BIGBLIND|<0,6><1,6>call"
        )

        responses = [
            response
            for message in messages
            if (response := adapter.handle_message(message)) is not None
        ]

        self.assertEqual(responses, ["check"])
        self.assertEqual(adapter.role, "BIGBLIND")
        self.assertEqual(adapter.round_bets, [100, 100])

    def test_consecutive_raise_minimum_is_previous_total_doubled(self):
        adapter = GUOSAI.GuosaiProtocolAdapter()
        adapter.start_hand("BIGBLIND", [0, 1])
        adapter.record_action(adapter.opponent_id, "raise", 200)

        self.assertEqual(adapter.min_raise_total(), 400)

    def test_reconstruct_state_uses_raise_target_for_next_minimum(self):
        state = GUOSAI.reconstruct_state({
            "my_id": 1,
            "dealer_id": 0,
            "public_cards": [],
            "history": [
                {
                    "round": 0,
                    "player_id": 1,
                    "action": 150,
                    "action_type": "raise",
                    "raise_total": 200,
                },
                {
                    "round": 0,
                    "player_id": 0,
                    "action": 300,
                    "action_type": "raise",
                    "raise_total": 400,
                },
            ],
        })

        self.assertEqual(state["my_round_bet"], 200)
        self.assertEqual(state["min_raise_action"], 600)
        self.assertEqual(state["my_round_bet"] + state["min_raise_action"], 800)

    def test_raise_below_true_allin_total_stays_raise(self):
        adapter = GUOSAI.GuosaiProtocolAdapter()
        adapter.start_hand("SMALLBLIND", [0, 1])
        state = GUOSAI.reconstruct_state(adapter.make_request())

        command = adapter.command_from_internal_action(19949, state)

        self.assertEqual(command, "raise 19999")

    def test_raise_at_or_above_allin_total_becomes_allin(self):
        adapter = GUOSAI.GuosaiProtocolAdapter()
        adapter.start_hand("SMALLBLIND", [0, 1])
        state = GUOSAI.reconstruct_state(adapter.make_request())

        self.assertEqual(adapter.command_from_internal_action(19950, state), "allin")
        self.assertEqual(adapter.command_from_internal_action(25000, state), "allin")
        self.assertEqual(adapter.sanitize_command("raise 20000", state), "allin")

    def test_opponent_allin_allows_only_call_or_fold(self):
        adapter = GUOSAI.GuosaiProtocolAdapter()
        adapter.start_hand("SMALLBLIND", [0, 1])
        adapter.record_action(adapter.opponent_id, "allin")
        state = GUOSAI.reconstruct_state(adapter.make_request())

        self.assertEqual(adapter.command_from_internal_action(500, state), "call")
        self.assertEqual(adapter.command_from_internal_action(-2, state), "call")
        self.assertEqual(adapter.command_from_internal_action(-1, state), "fold")
        self.assertEqual(adapter.sanitize_command("raise 400", state), "call")

    def test_passive_commands_match_platform_call_check_rules(self):
        small_blind = GUOSAI.GuosaiProtocolAdapter()
        small_blind.start_hand("SMALLBLIND", [0, 1])
        state = GUOSAI.reconstruct_state(small_blind.make_request())
        self.assertEqual(small_blind.command_from_internal_action(0, state), "call")

        big_blind = GUOSAI.GuosaiProtocolAdapter()
        big_blind.start_hand("BIGBLIND", [0, 1])
        big_blind.record_action(big_blind.opponent_id, "call")
        state = GUOSAI.reconstruct_state(big_blind.make_request())
        self.assertEqual(big_blind.command_from_internal_action(0, state), "check")

        flop_first = GUOSAI.GuosaiProtocolAdapter()
        flop_first.start_hand("BIGBLIND", [0, 1])
        flop_first.start_street("flop", [2, 3, 4])
        state = GUOSAI.reconstruct_state(flop_first.make_request())
        self.assertEqual(flop_first.command_from_internal_action(0, state), "check")

        flop_after_check = GUOSAI.GuosaiProtocolAdapter()
        flop_after_check.start_hand("SMALLBLIND", [0, 1])
        flop_after_check.start_street("flop", [2, 3, 4])
        flop_after_check.record_action(flop_after_check.opponent_id, "check")
        state = GUOSAI.reconstruct_state(flop_after_check.make_request())
        self.assertEqual(flop_after_check.command_from_internal_action(0, state), "call")

    def test_receives_bet_as_raise_but_send_format_stays_raise(self):
        self.assertEqual(GUOSAI.split_guosai_messages("bet 400"), (["bet 400"], ""))

        adapter = GUOSAI.GuosaiProtocolAdapter()
        adapter.start_hand("SMALLBLIND", [0, 1])
        self.assertTrue(adapter.handle_opponent_action("bet 400"))
        self.assertEqual(adapter.history[-1]["action_type"], "raise")
        self.assertEqual(adapter.history[-1]["action"], 300)
        self.assertEqual(adapter.history[-1]["raise_total"], 400)

        sock = FakeSocket()
        GUOSAI.send_guosai(sock, "raise 400")
        self.assertEqual(sock.sent, [b"raise 400"])

    def test_final_legal_command_blocks_bet_and_repairs_low_raise(self):
        adapter = GUOSAI.GuosaiProtocolAdapter()
        adapter.start_hand("SMALLBLIND", [0, 1])
        state = GUOSAI.reconstruct_state(adapter.make_request())

        self.assertEqual(GUOSAI.final_legal_command(adapter, "bet 400", state), "call")
        self.assertEqual(GUOSAI.final_legal_command(adapter, "raise 101", state), "raise 200")

    def test_final_legal_command_preserves_postflop_call_check_rules(self):
        first = GUOSAI.GuosaiProtocolAdapter()
        first.start_hand("BIGBLIND", [0, 1])
        first.start_street("flop", [2, 3, 4])
        state = GUOSAI.reconstruct_state(first.make_request())
        self.assertEqual(GUOSAI.final_legal_command(first, "call", state), "check")

        after_check = GUOSAI.GuosaiProtocolAdapter()
        after_check.start_hand("SMALLBLIND", [0, 1])
        after_check.start_street("flop", [2, 3, 4])
        after_check.record_action(after_check.opponent_id, "check")
        state = GUOSAI.reconstruct_state(after_check.make_request())
        self.assertEqual(GUOSAI.final_legal_command(after_check, "check", state), "call")

    def test_rejects_invalid_cards_and_out_of_order_streets(self):
        self.assertIsNone(GUOSAI.parse_guosai_cards("preflop|SMALLBLIND|<9,99><8,88>"))
        self.assertIsNone(GUOSAI.parse_guosai_cards("preflop|SMALLBLIND|<0,12><0,12>"))

        adapter = GUOSAI.GuosaiProtocolAdapter()
        self.assertIsNone(adapter.handle_message("preflop|SMALLBLIND|<9,99><8,88>"))
        self.assertEqual(adapter.hand, -1)

        adapter = GUOSAI.GuosaiProtocolAdapter()
        self.assertIsNone(adapter.handle_message("flop|<0,1><1,2><2,3>"))
        self.assertIsNone(adapter.handle_message("raise 200"))
        self.assertEqual(adapter.history, [])

    def test_ignores_illegal_opponent_raise_targets(self):
        adapter = GUOSAI.GuosaiProtocolAdapter()
        adapter.start_hand("SMALLBLIND", [48, 44])
        adapter.record_action(adapter.my_id, "raise", 314)

        self.assertFalse(adapter.handle_opponent_action("raise 0"))
        self.assertFalse(adapter.handle_opponent_action("raise 400"))
        self.assertEqual(len(adapter.history), 1)

    def test_ignores_illegal_opponent_call_and_check(self):
        facing_raise = GUOSAI.GuosaiProtocolAdapter()
        facing_raise.start_hand("SMALLBLIND", [48, 44])
        facing_raise.record_action(facing_raise.my_id, "raise", 314)
        self.assertFalse(facing_raise.handle_opponent_action("check"))
        self.assertEqual(len(facing_raise.history), 1)

        flop_first = GUOSAI.GuosaiProtocolAdapter()
        flop_first.start_hand("BIGBLIND", [0, 1])
        flop_first.start_street("flop", [2, 3, 4])
        self.assertFalse(flop_first.handle_opponent_action("call"))
        self.assertEqual(flop_first.history, [])

        flop_after_my_check = GUOSAI.GuosaiProtocolAdapter()
        flop_after_my_check.start_hand("BIGBLIND", [0, 1])
        flop_after_my_check.start_street("flop", [2, 3, 4])
        flop_after_my_check.record_action(flop_after_my_check.my_id, "check")
        self.assertFalse(flop_after_my_check.handle_opponent_action("call"))
        self.assertEqual(flop_after_my_check.history[-1]["action_type"], "call")

    def test_oversized_opponent_raise_is_treated_as_allin(self):
        adapter = GUOSAI.GuosaiProtocolAdapter()
        adapter.start_hand("SMALLBLIND", [48, 44])
        adapter.record_action(adapter.my_id, "raise", 314)

        self.assertTrue(adapter.handle_opponent_action("raise 999999"))
        self.assertTrue(adapter.opponent_allin)
        self.assertEqual(adapter.history[-1]["action_type"], "allin")
        state = GUOSAI.reconstruct_state(adapter.make_request())
        self.assertEqual(GUOSAI.final_legal_command(adapter, "raise 999999", state), "call")

    def test_timed_equity_updates_stats_before_deadline(self):
        combos = [(8, 9), (12, 13), (16, 17)]
        weights = [1.0, 0.8, 0.6]
        stats = {}

        win_rate = GUOSAI.estimate_weighted_win_rate(
            [0, 5],
            [20, 25, 30],
            combos,
            weights,
            5,
            deadline=time.monotonic() + 0.01,
            stats=stats,
        )

        self.assertGreaterEqual(win_rate, 0.0)
        self.assertLessEqual(win_rate, 1.0)
        self.assertGreater(stats["equity_samples"], 0)
        self.assertIn(stats["equity_mode"], ("monte_carlo_timed", "empty"))

    def test_close_pot_odds_gap_refines_even_when_not_otherwise_critical(self):
        state = {"to_call": 100, "opponent_allin": False}
        pot = 1000
        pot_odds = state["to_call"] / (pot + state["to_call"])

        plan = GUOSAI.equity_refinement_plan(
            3,
            state,
            pot,
            20,
            pot_odds + 0.03,
            pot_odds,
            time.monotonic() + 5,
            {"equity_mode": "monte_carlo_timed", "equity_samples": 5000},
        )

        self.assertIsNotNone(plan)
        self.assertEqual(plan["reason"], "pot_odds_close")
        self.assertEqual(plan["target_standard_error"], GUOSAI.MONTE_CARLO_CRITICAL_STANDARD_ERROR)

    def test_obvious_noncritical_pot_odds_gap_does_not_refine(self):
        state = {"to_call": 100, "opponent_allin": False}
        pot = 1000
        pot_odds = state["to_call"] / (pot + state["to_call"])

        plan = GUOSAI.equity_refinement_plan(
            3,
            state,
            pot,
            20,
            0.45,
            pot_odds,
            time.monotonic() + 5,
            {"equity_mode": "monte_carlo_timed", "equity_samples": 5000},
        )

        self.assertIsNone(plan)

    def test_completed_turn_exact_equity_is_not_refined_again(self):
        state = {"to_call": 500, "opponent_allin": False}
        pot = 3000
        pot_odds = state["to_call"] / (pot + state["to_call"])

        plan = GUOSAI.equity_refinement_plan(
            4,
            state,
            pot,
            4,
            pot_odds + 0.01,
            pot_odds,
            time.monotonic() + 5,
            {"equity_mode": "turn_exact", "equity_samples": 2000},
        )

        self.assertIsNone(plan)

    def test_get_action_runs_second_equity_pass_for_close_gap(self):
        req = {
            "num_players": 2,
            "dealer_id": 0,
            "my_id": 0,
            "my_chips": 19900,
            "my_cards": [48, 44],
            "public_cards": [0, 5, 10],
            "history": [
                {
                    "round": 1,
                    "player_id": 1,
                    "action": 100,
                    "action_type": "raise",
                    "raise_total": 100,
                },
            ],
            "hand": 20,
            "max_hand": 70,
            "total_win_chips": [0, 0],
            "total_win_games": [0, 0],
        }
        state = GUOSAI.reconstruct_state(req)
        pot = max(1, state["pot"])
        pot_odds = state["to_call"] / (pot + state["to_call"])
        calls = []
        original = GUOSAI.estimate_weighted_win_rate

        def fake_estimate(*args, **kwargs):
            calls.append(kwargs)
            stats = kwargs.get("stats")
            if stats is not None:
                stats.update({
                    "equity_mode": "monte_carlo_timed",
                    "equity_samples": 5000,
                    "equity_standard_error": 0.006,
                })
            return pot_odds + (0.03 if len(calls) == 1 else 0.04)

        try:
            GUOSAI.estimate_weighted_win_rate = fake_estimate
            trace = {}
            GUOSAI.get_action(req, [req], trace, decision_deadline=time.monotonic() + 3)
        finally:
            GUOSAI.estimate_weighted_win_rate = original

        self.assertEqual(len(calls), 2)
        self.assertTrue(trace["equity_refined"])
        self.assertEqual(trace["equity_refine_reason"], "pot_odds_close")
        self.assertEqual(trace["equity_initial_samples"], 5000)

    def test_preflop_strength_uses_heads_up_lookup_table(self):
        self.assertEqual(len(GUOSAI.HEADS_UP_PREFLOP_EQUITY), 169)
        self.assertEqual(GUOSAI.preflop_hand_key([48, 49]), "AA")
        self.assertEqual(GUOSAI.preflop_hand_key([48, 44]), "AKs")
        self.assertEqual(GUOSAI.preflop_hand_key([48, 45]), "AKo")

        self.assertAlmostEqual(GUOSAI.estimate_preflop_strength([48, 49]), 0.8520)
        self.assertAlmostEqual(GUOSAI.estimate_preflop_strength([48, 44]), 0.67035)
        self.assertAlmostEqual(GUOSAI.estimate_preflop_strength([48, 45]), 0.6531)
        self.assertAlmostEqual(GUOSAI.estimate_preflop_strength([20, 1]), 0.3458)

    def test_preflop_get_action_does_not_run_monte_carlo(self):
        req = {
            "num_players": 2,
            "dealer_id": 0,
            "my_id": 1,
            "my_chips": 19950,
            "my_cards": [48, 44],
            "public_cards": [],
            "history": [],
            "hand": 0,
            "max_hand": 70,
            "total_win_chips": [0, 0],
            "total_win_games": [0, 0],
        }
        trace = {}
        original = GUOSAI.estimate_weighted_win_rate

        def fail_if_called(*args, **kwargs):
            raise AssertionError("preflop should use lookup table, not Monte Carlo")

        try:
            GUOSAI.estimate_weighted_win_rate = fail_if_called
            GUOSAI.get_action(req, [req], trace)
        finally:
            GUOSAI.estimate_weighted_win_rate = original

        self.assertEqual(trace["equity_mode"], "preflop_lookup")
        self.assertEqual(trace["equity_samples"], 0)

    def test_early_lead_match_adjustment_cap_is_reduced(self):
        req = {"total_win_chips": [9999, 0]}

        self.assertAlmostEqual(GUOSAI.match_risk_adjustment(req, 0, 50), 0.025)
        self.assertAlmostEqual(GUOSAI.match_risk_adjustment(req, 0, 40), 0.035)
        self.assertAlmostEqual(GUOSAI.match_risk_adjustment(req, 0, 20), 0.05)

        behind_req = {"total_win_chips": [-9999, 0]}
        self.assertAlmostEqual(GUOSAI.match_risk_adjustment(behind_req, 0, 50), -0.05)

    def test_early_small_blind_open_is_only_slightly_wider(self):
        def make_req(hand):
            return {
                "num_players": 2,
                "dealer_id": 1,
                "my_id": 0,
                "my_chips": 19950,
                "my_cards": [32, 25],
                "public_cards": [],
                "history": [],
                "hand": hand,
                "max_hand": 70,
                "total_win_chips": [0, 0],
                "total_win_games": [0, 0],
            }

        early_req = make_req(20)
        later_req = make_req(40)

        early_action = GUOSAI.get_action(early_req, [early_req], {})
        later_action = GUOSAI.get_action(later_req, [later_req], {})

        self.assertEqual(GUOSAI.preflop_hand_key(early_req["my_cards"]), "T8o")
        self.assertAlmostEqual(GUOSAI.estimate_preflop_strength(early_req["my_cards"]), 0.4971)
        self.assertGreater(early_action, 0)
        self.assertEqual(later_action, 0)

    def test_new_hand_resets_chips_independent_of_match_total(self):
        adapter = GUOSAI.GuosaiProtocolAdapter()
        adapter.total_win = -750

        adapter.start_hand("SMALLBLIND", [0, 1])

        self.assertEqual(adapter.chips, [19950, 19900])
        self.assertEqual(adapter.total_win, -750)


if __name__ == "__main__":
    unittest.main()
