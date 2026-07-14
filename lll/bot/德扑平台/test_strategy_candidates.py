import importlib.util
import os
import unittest


PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_module(name, relative_path):
    path = os.path.join(PROJECT_DIR, relative_path)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


anti = load_module("anti_lock_v1", os.path.join("bots", "experiments", "anti_lock_v1.py"))
anti_v2 = load_module("anti_lock_v2", os.path.join("bots", "experiments", "anti_lock_v2.py"))
anti_v3 = load_module("anti_lock_v3", os.path.join("bots", "experiments", "anti_lock_v3.py"))
anti_minimal = load_module(
    "anti_lock_minimal_v1",
    os.path.join("bots", "experiments", "anti_lock_minimal_v1.py"),
)
value = load_module("value_tier_v1", os.path.join("bots", "experiments", "value_tier_v1.py"))
combined = load_module("combined_v1", os.path.join("bots", "experiments", "combined_v1.py"))


class AntiLockCandidateTests(unittest.TestCase):
    def test_low_equity_sticky_opponent_uses_call(self):
        state = {
            "opponent_allin": False,
            "min_raise_action": 1872,
            "round_raise": 1872,
            "my_round_bet": 0,
        }
        action = anti.choose_anti_lock_pressure_action(
            state=state,
            my_chips=20000,
            to_call=1872,
            pot=3908,
            round_idx=1,
            win_rate=0.2446,
            opponent_model={
                "fold_to_raise": 0.0926,
                "confidence": 1.0,
            },
            remaining_hands=36,
            value_profile={"tier": "none"},
            draw_info={"quality": 0.0, "semi_bluff": False},
            blocker_profile={"eligible": False},
            board_texture={"dynamic": False},
        )
        self.assertEqual(action, 0)

    def test_low_equity_large_bet_no_longer_forces_jam(self):
        state = {
            "opponent_allin": False,
            "min_raise_action": 6887,
            "round_raise": 6887,
            "my_round_bet": 0,
        }
        action = anti.choose_anti_lock_pressure_action(
            state=state,
            my_chips=20000,
            to_call=6887,
            pot=33111,
            round_idx=3,
            win_rate=0.117,
            opponent_model={
                "fold_to_raise": 0.055,
                "confidence": 1.0,
            },
            remaining_hands=9,
            value_profile={"tier": "none"},
            draw_info={"quality": 0.0, "semi_bluff": False},
            blocker_profile={"eligible": False},
            board_texture={"dynamic": True},
        )
        self.assertEqual(action, 0)


class ValueTierCandidateTests(unittest.TestCase):
    def test_dynamic_board_set_is_strong_not_nut(self):
        hole = [14, 12]
        board = [15, 9, 24, 3, 22]
        texture = value.board_texture_profile(board)
        pair_profile = value.pair_board_profile(hole, board)
        profile = value.value_hand_tier(hole, board, pair_profile, texture)

        self.assertEqual(profile["hand_class"], 3)
        self.assertTrue(profile["set_made"])
        self.assertEqual(profile["tier"], "strong")

    def test_river_set_raise_is_capped(self):
        hole = [14, 12]
        board = [15, 9, 24, 3, 22]
        texture = value.board_texture_profile(board)
        pair_profile = value.pair_board_profile(hole, board)
        profile = value.value_hand_tier(hole, board, pair_profile, texture)
        amount = value.choose_raise(
            min_raise=100,
            my_chips=20000,
            my_round_bet=0,
            to_call=0,
            pot=7744,
            win_rate=0.7699,
            round_idx=3,
            spot_name="other",
            preflop_strength=None,
            has_position=True,
            opponent_model={
                "confidence": 0.4,
                "fold_to_raise": 0.1956,
            },
            value_profile=profile,
            board_texture=texture,
        )

        self.assertIsNotNone(amount)
        self.assertLessEqual(amount, int(7744 * 0.70))


class AntiLockV2CandidateTests(unittest.TestCase):
    def test_sticky_opponent_air_checks_instead_of_betting(self):
        action = anti_v2.choose_anti_lock_pressure_action(
            state={
                "opponent_allin": False,
                "min_raise_action": 50,
                "round_raise": 50,
                "my_round_bet": 0,
            },
            my_chips=18057,
            to_call=0,
            pot=3886,
            round_idx=3,
            win_rate=0.5709,
            opponent_model={
                "fold_to_raise": 0.0587,
                "confidence": 1.0,
            },
            remaining_hands=29,
            value_profile={"tier": "none"},
            draw_info={"quality": 0.0, "semi_bluff": False},
            blocker_profile={"eligible": False},
            board_texture={"dynamic": True},
        )
        self.assertEqual(action, 0)

    def test_foldable_opponent_keeps_anti_lock_pressure(self):
        action = anti_v2.choose_anti_lock_pressure_action(
            state={
                "opponent_allin": False,
                "min_raise_action": 50,
                "round_raise": 50,
                "my_round_bet": 0,
            },
            my_chips=18057,
            to_call=0,
            pot=3886,
            round_idx=3,
            win_rate=0.5709,
            opponent_model={
                "fold_to_raise": 0.55,
                "confidence": 1.0,
            },
            remaining_hands=29,
            value_profile={"tier": "none"},
            draw_info={"quality": 0.0, "semi_bluff": False},
            blocker_profile={"eligible": False},
            board_texture={"dynamic": True},
        )
        self.assertIsNotNone(action)

    def test_strong_value_keeps_immediate_stackoff(self):
        action = anti_v2.choose_anti_lock_pressure_action(
            state={
                "opponent_allin": False,
                "min_raise_action": 9543,
                "round_raise": 9543,
                "my_round_bet": 2063,
            },
            my_chips=16987,
            to_call=7480,
            pot=13606,
            round_idx=2,
            win_rate=0.6759,
            opponent_model={
                "fold_to_raise": 0.11,
                "confidence": 1.0,
            },
            remaining_hands=45,
            value_profile={"tier": "strong"},
            draw_info={"quality": 0.0, "semi_bluff": False},
            blocker_profile={"eligible": False},
            board_texture={"dynamic": True},
        )
        self.assertEqual(action, -2)


class CombinedCandidateTests(unittest.TestCase):
    def test_combined_preserves_low_equity_anti_lock_call(self):
        action = combined.choose_anti_lock_pressure_action(
            state={
                "opponent_allin": False,
                "min_raise_action": 6887,
                "round_raise": 6887,
                "my_round_bet": 0,
            },
            my_chips=20000,
            to_call=6887,
            pot=33111,
            round_idx=3,
            win_rate=0.117,
            opponent_model={
                "fold_to_raise": 0.055,
                "confidence": 1.0,
            },
            remaining_hands=9,
            value_profile={"tier": "none"},
            draw_info={"quality": 0.0, "semi_bluff": False},
            blocker_profile={"eligible": False},
            board_texture={"dynamic": True},
        )
        self.assertEqual(action, 0)

    def test_combined_preserves_set_risk_control(self):
        hole = [14, 12]
        board = [15, 9, 24, 3, 22]
        texture = combined.board_texture_profile(board)
        pair_profile = combined.pair_board_profile(hole, board)
        profile = combined.value_hand_tier(hole, board, pair_profile, texture)

        self.assertEqual(profile["tier"], "strong")
        self.assertTrue(profile["set_made"])


class AntiLockV3CandidateTests(unittest.TestCase):
    def test_river_air_checks_against_sticky_opponent(self):
        action = anti_v3.choose_anti_lock_pressure_action(
            state={
                "opponent_allin": False,
                "min_raise_action": 50,
                "round_raise": 50,
                "my_round_bet": 0,
            },
            my_chips=18057,
            to_call=0,
            pot=3886,
            round_idx=3,
            win_rate=0.5709,
            opponent_model={"fold_to_raise": 0.0587, "confidence": 1.0},
            remaining_hands=29,
            value_profile={"tier": "none"},
            draw_info={"quality": 0.0, "semi_bluff": False},
            blocker_profile={"eligible": False},
            board_texture={"dynamic": True},
        )
        self.assertEqual(action, 0)

    def test_flop_pressure_is_not_disabled_by_river_guard(self):
        action = anti_v3.choose_anti_lock_pressure_action(
            state={
                "opponent_allin": False,
                "min_raise_action": 50,
                "round_raise": 50,
                "my_round_bet": 0,
            },
            my_chips=19670,
            to_call=0,
            pot=660,
            round_idx=1,
            win_rate=0.465,
            opponent_model={"fold_to_raise": 0.0629, "confidence": 1.0},
            remaining_hands=29,
            value_profile={"tier": "none"},
            draw_info={"quality": 0.0, "semi_bluff": False},
            blocker_profile={"eligible": False},
            board_texture={"dynamic": False},
        )
        self.assertIsNotNone(action)
        self.assertGreater(action, 0)

    def test_strong_value_still_stacks_off(self):
        action = anti_v3.choose_anti_lock_pressure_action(
            state={
                "opponent_allin": False,
                "min_raise_action": 9543,
                "round_raise": 9543,
                "my_round_bet": 2063,
            },
            my_chips=16987,
            to_call=7480,
            pot=13606,
            round_idx=2,
            win_rate=0.6759,
            opponent_model={"fold_to_raise": 0.11, "confidence": 1.0},
            remaining_hands=45,
            value_profile={"tier": "strong"},
            draw_info={"quality": 0.0, "semi_bluff": False},
            blocker_profile={"eligible": False},
            board_texture={"dynamic": True},
        )
        self.assertEqual(action, -2)


class AntiLockMinimalCandidateTests(unittest.TestCase):
    def test_low_equity_facing_bet_calls_against_sticky_opponent(self):
        action = anti_minimal.choose_anti_lock_pressure_action(
            state={
                "opponent_allin": False,
                "min_raise_action": 6887,
                "round_raise": 6887,
                "my_round_bet": 0,
            },
            my_chips=20000,
            to_call=6887,
            pot=33111,
            round_idx=3,
            win_rate=0.117,
            opponent_model={"fold_to_raise": 0.055, "confidence": 1.0},
            remaining_hands=9,
            value_profile={"tier": "none"},
            draw_info={"quality": 0.0, "semi_bluff": False},
            blocker_profile={"eligible": False},
            board_texture={"dynamic": True},
        )
        self.assertEqual(action, 0)

    def test_river_air_checks_against_extremely_sticky_opponent(self):
        action = anti_minimal.choose_anti_lock_pressure_action(
            state={
                "opponent_allin": False,
                "min_raise_action": 50,
                "round_raise": 50,
                "my_round_bet": 0,
            },
            my_chips=18057,
            to_call=0,
            pot=3886,
            round_idx=3,
            win_rate=0.5709,
            opponent_model={"fold_to_raise": 0.0587, "confidence": 1.0},
            remaining_hands=29,
            value_profile={"tier": "none"},
            draw_info={"quality": 0.0, "semi_bluff": False},
            blocker_profile={"eligible": False},
            board_texture={"dynamic": True},
        )
        self.assertEqual(action, 0)

    def test_original_strong_value_stackoff_is_unchanged(self):
        action = anti_minimal.choose_anti_lock_pressure_action(
            state={
                "opponent_allin": False,
                "min_raise_action": 9543,
                "round_raise": 9543,
                "my_round_bet": 2063,
            },
            my_chips=16987,
            to_call=7480,
            pot=13606,
            round_idx=2,
            win_rate=0.6759,
            opponent_model={"fold_to_raise": 0.11, "confidence": 1.0},
            remaining_hands=45,
            value_profile={"tier": "strong"},
            draw_info={"quality": 0.0, "semi_bluff": False},
            blocker_profile={"eligible": False},
            board_texture={"dynamic": True},
        )
        self.assertEqual(action, -2)


if __name__ == "__main__":
    unittest.main()
