"""Tests for CFV label generation and training."""

import unittest
from pathlib import Path

from bots.research_native_lab.cfr_neural_search_m5.cfv.label_generator import (
    CFVLabel,
    generate_labels,
    _safe_hands,
    _BOARDS,
)


class TestSafeHands(unittest.TestCase):
    def test_returns_two_non_overlapping_pairs(self):
        for board in _BOARDS[:3]:
            pairs = _safe_hands(board)
            self.assertEqual(len(pairs), 2)
            all_cards = set()
            for pair in pairs:
                self.assertEqual(len(pair), 2)
                self.assertNotIn(pair[0], board)
                self.assertNotIn(pair[1], board)
                all_cards.update(pair)
            self.assertEqual(len(all_cards), 4)


class TestLabelGeneration(unittest.TestCase):
    def test_fold_labels_have_correct_shapes(self):
        labels = generate_labels(5, seed=42, max_showdown=0)
        self.assertGreater(len(labels), 0)
        for label in labels:
            self.assertIsInstance(label, CFVLabel)
            self.assertEqual(len(label.range_p0), 1326)
            self.assertEqual(len(label.range_p1), 1326)
            self.assertEqual(len(label.target_cfv_p0), 1326)
            self.assertEqual(len(label.target_cfv_p1), 1326)

    def test_fold_cfv_values_are_finite(self):
        labels = generate_labels(3, seed=42, max_showdown=0)
        for label in labels:
            for v in label.target_cfv_p0:
                import math
                self.assertTrue(math.isfinite(v))


class TestTraining(unittest.TestCase):
    def test_training_reduces_loss(self):
        from bots.research_native_lab.cfr_neural_search_m5.tools.train_cfv_network import (
            train_cfv_network,
        )
        labels = generate_labels(10, seed=42, max_showdown=0)
        config = {"hidden_dim": 64, "layers": 2, "lr": 1e-2, "epochs": 5, "seed": 42}
        result = train_cfv_network(labels, config)
        self.assertGreater(len(result["loss_history"]), 0)
        self.assertLess(result["final_loss"], result["loss_history"][0])


if __name__ == "__main__":
    unittest.main()
