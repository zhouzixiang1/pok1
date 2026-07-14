import unittest

import hl_loop


class HlLoopAnalysisTests(unittest.TestCase):
    def test_aggregate_candidate_slot_zero(self):
        data = {
            "candidate_slot": 0,
            "bot0_wins": 2,
            "bot1_wins": 1,
            "draws": 0,
            "n_games_actual": 3,
            "games": [
                {"bot0_chips": 100, "bot1_chips": -100},
                {"bot0_chips": -40, "bot1_chips": 40},
            ],
        }

        metric = hl_loop.aggregate_matchup_metrics(data)

        self.assertEqual(metric["candidate_wins"], 2)
        self.assertEqual(metric["baseline_wins"], 1)
        self.assertEqual(metric["chip_sum"], 60)
        self.assertEqual(metric["avg_mirror_chip_diff"], 20.0)

    def test_aggregate_candidate_slot_one(self):
        data = {
            "candidate_slot": 1,
            "bot0_wins": 1,
            "bot1_wins": 2,
            "draws": 0,
            "n_games_actual": 3,
            "games": [
                {"bot0_chips": 100, "bot1_chips": -100},
                {"bot0_chips": -40, "bot1_chips": 40},
            ],
        }

        metric = hl_loop.aggregate_matchup_metrics(data)

        self.assertEqual(metric["candidate_wins"], 2)
        self.assertEqual(metric["baseline_wins"], 1)
        self.assertEqual(metric["chip_sum"], -60)
        self.assertEqual(metric["avg_mirror_chip_diff"], -20.0)

    def test_classify_red_on_invalid_output(self):
        self.assertEqual(hl_loop.classify_run(500, {"illegal_or_crash": 1}), "RED")

    def test_classify_control_run_as_observation(self):
        self.assertEqual(hl_loop.classify_run(500, {}, control_run=True), "YELLOW")

    def test_baseline_name_uses_parent_for_main_py(self):
        self.assertEqual(hl_loop.baseline_name_for_path(r"D:\x\bots\bot002\main.py"), "bot002")


if __name__ == "__main__":
    unittest.main()
