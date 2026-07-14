"""Tests for the Range CFV neural network module."""

from __future__ import annotations

import math
import unittest

import torch
import torch.nn as nn

from bots.research_native_lab.common_contracts import Action, ActionKind, NationalGameState

from bots.research_native_lab.cfr_neural_search_m5.cfv.combo_index import (
    COMBO_COUNT,
    COMBO_TO_INDEX,
    COMBOS,
)
from bots.research_native_lab.cfr_neural_search_m5.cfv.public_state import (
    PublicHUNLState,
)
from bots.research_native_lab.cfr_neural_search_m5.cfv.range_cfv_network import (
    PUBLIC_FEATURE_DIM,
    RangeCFVDataset,
    RangeCFVNet,
    RangeCFVNetConfig,
    build_cfv_model,
    encode_public_state,
    predict_cfv,
)
from bots.research_native_lab.cfr_neural_search_m5.cfv.ranges import (
    uniform_reach_range,
)

FIRST_HAND = (0, 1)
SECOND_HAND = (2, 3)
BOARD = (4, 5, 6, 7, 8)


def _new_hand() -> NationalGameState:
    return NationalGameState.new_hand(
        1, small_blind=0, hole_cards=(FIRST_HAND, SECOND_HAND)
    )


def _to_flop(state: NationalGameState) -> NationalGameState:
    state = state.apply_action(Action(ActionKind.CALL))
    state = state.apply_action(Action(ActionKind.CHECK))
    return state.apply_chance(BOARD[:3])


def _close_checked_street(state: NationalGameState) -> NationalGameState:
    state = state.apply_action(Action(ActionKind.CHECK))
    return state.apply_action(Action(ActionKind.CALL))


def _to_turn(state: NationalGameState) -> NationalGameState:
    state = _close_checked_street(_to_flop(state))
    return state.apply_chance(BOARD[3:4])


def _to_river(state: NationalGameState) -> NationalGameState:
    state = _close_checked_street(_to_turn(state))
    return state.apply_chance(BOARD[4:5])


def _preflop_state() -> PublicHUNLState:
    return PublicHUNLState.from_common_state(_new_hand())


def _flop_state() -> PublicHUNLState:
    return PublicHUNLState.from_common_state(_to_flop(_new_hand()))


def _turn_state() -> PublicHUNLState:
    return PublicHUNLState.from_common_state(_to_turn(_new_hand()))


def _river_state() -> PublicHUNLState:
    return PublicHUNLState.from_common_state(_to_river(_new_hand()))


class PublicStateEncoderTest(unittest.TestCase):
    """Verify encode_public_state produces correct feature vectors."""

    def test_output_shape(self):
        feat = encode_public_state(_preflop_state())
        self.assertEqual(feat.dim(), 1)
        self.assertEqual(feat.shape[0], PUBLIC_FEATURE_DIM)
        self.assertEqual(feat.dtype, torch.float32)

    def test_street_one_hot_preflop(self):
        feat = encode_public_state(_preflop_state())
        # First 4 dims are street one-hot: preflop should be [1,0,0,0]
        self.assertAlmostEqual(feat[0].item(), 1.0, places=6)
        self.assertAlmostEqual(feat[1].item(), 0.0, places=6)

    def test_street_one_hot_flop(self):
        feat = encode_public_state(_flop_state())
        # Flop should be [0,1,0,0]
        self.assertAlmostEqual(feat[0].item(), 0.0, places=6)
        self.assertAlmostEqual(feat[1].item(), 1.0, places=6)

    def test_board_card_encoding_flop(self):
        feat = encode_public_state(_flop_state())
        # Board one-hot starts at offset STREET_DIM=4
        # Flop board cards are BOARD[:3] = (4,5,6)
        for card_id in BOARD[:3]:
            self.assertAlmostEqual(feat[4 + card_id].item(), 1.0, places=6)
        # Card 0 should not be on the board
        self.assertAlmostEqual(feat[4 + 0].item(), 0.0, places=6)

    def test_deterministic_encoding(self):
        state = _preflop_state()
        feat1 = encode_public_state(state)
        feat2 = encode_public_state(state)
        self.assertTrue(torch.equal(feat1, feat2))


class RangeCFVNetForwardTest(unittest.TestCase):
    """Verify the network forward pass produces correct output shapes."""

    def setUp(self):
        self.config = RangeCFVNetConfig(
            trunk_hidden=32,
            trunk_layers=2,
            range_hidden=64,
            head_hidden=64,
            head_layers=2,
        )
        self.model = build_cfv_model(self.config, seed=42)
        self.state = _preflop_state()
        self.pub_feat = encode_public_state(self.state).unsqueeze(0)
        self.r0 = torch.tensor(
            uniform_reach_range(self.state.board_card_ids), dtype=torch.float32
        ).unsqueeze(0)
        self.r1 = torch.tensor(
            uniform_reach_range(self.state.board_card_ids), dtype=torch.float32
        ).unsqueeze(0)

    def test_output_shape_single(self):
        out = self.model(self.pub_feat, self.r0, self.r1)
        self.assertEqual(out.shape, (1, 2, COMBO_COUNT))

    def test_output_shape_batch(self):
        batch = 8
        pub_batch = self.pub_feat.expand(batch, -1)
        r0_batch = self.r0.expand(batch, -1)
        r1_batch = self.r1.expand(batch, -1)
        out = self.model(pub_batch, r0_batch, r1_batch)
        self.assertEqual(out.shape, (batch, 2, COMBO_COUNT))

    def test_output_is_finite(self):
        out = self.model(self.pub_feat, self.r0, self.r1)
        self.assertTrue(torch.isfinite(out).all())

    def test_batch_mismatch_raises(self):
        with self.assertRaises(ValueError):
            self.model(
                self.pub_feat,
                torch.zeros(2, COMBO_COUNT),
                self.r1,
            )


class DeterminismTest(unittest.TestCase):
    """Verify model is deterministic given the same seed."""

    def test_same_seed_same_weights(self):
        m1 = build_cfv_model(seed=123)
        m2 = build_cfv_model(seed=123)
        for p1, p2 in zip(m1.parameters(), m2.parameters()):
            self.assertTrue(torch.equal(p1, p2))

    def test_different_seed_different_weights(self):
        m1 = build_cfv_model(seed=1)
        m2 = build_cfv_model(seed=2)
        all_same = all(
            torch.equal(p1, p2)
            for p1, p2 in zip(m1.parameters(), m2.parameters())
        )
        self.assertFalse(all_same)

    def test_eval_mode_deterministic(self):
        model = build_cfv_model(seed=99)
        state = _flop_state()
        pub = encode_public_state(state).unsqueeze(0)
        r0 = torch.tensor(
            uniform_reach_range(state.board_card_ids), dtype=torch.float32
        ).unsqueeze(0)
        r1 = torch.tensor(
            uniform_reach_range(state.board_card_ids), dtype=torch.float32
        ).unsqueeze(0)
        with torch.no_grad():
            out1 = model(pub, r0, r1)
            out2 = model(pub, r0, r1)
        self.assertTrue(torch.allclose(out1, out2, atol=1e-6))


class PredictCFVTest(unittest.TestCase):
    """Verify the high-level predict_cfv helper."""

    def test_returns_correct_dimensions(self):
        model = build_cfv_model(seed=7)
        state = _flop_state()
        r0 = uniform_reach_range(state.board_card_ids)
        r1 = uniform_reach_range(state.board_card_ids)
        cfv0, cfv1 = predict_cfv(model, state, r0, r1)
        self.assertEqual(len(cfv0), COMBO_COUNT)
        self.assertEqual(len(cfv1), COMBO_COUNT)

    def test_masked_combos_are_zero(self):
        """Combos blocked by the board should be exactly zero."""
        model = build_cfv_model(seed=7)
        state = _flop_state()
        r0 = uniform_reach_range(state.board_card_ids)
        r1 = uniform_reach_range(state.board_card_ids)
        cfv0, cfv1 = predict_cfv(model, state, r0, r1)
        board = state.board_card_ids
        for i, (c1, c2) in enumerate(COMBOS):
            if c1 in board or c2 in board:
                self.assertEqual(cfv0[i], 0.0, f"cfv0[{i}] should be masked to zero")
                self.assertEqual(cfv1[i], 0.0, f"cfv1[{i}] should be masked to zero")

    def test_all_values_finite(self):
        model = build_cfv_model(seed=7)
        state = _preflop_state()
        r0 = uniform_reach_range(state.board_card_ids)
        r1 = uniform_reach_range(state.board_card_ids)
        cfv0, cfv1 = predict_cfv(model, state, r0, r1)
        for v in cfv0:
            self.assertTrue(math.isfinite(v))
        for v in cfv1:
            self.assertTrue(math.isfinite(v))


class DatasetTest(unittest.TestCase):
    """Verify RangeCFVDataset basics."""

    def test_empty_rejected(self):
        with self.assertRaises(ValueError):
            RangeCFVDataset([])

    def test_len_and_getitem(self):
        pub = torch.randn(PUBLIC_FEATURE_DIM)
        r0 = torch.randn(COMBO_COUNT)
        r1 = torch.randn(COMBO_COUNT)
        target = torch.randn(2, COMBO_COUNT)
        ds = RangeCFVDataset([(pub, r0, r1, target)])
        self.assertEqual(len(ds), 1)
        got = ds[0]
        self.assertEqual(len(got), 4)
        self.assertTrue(torch.equal(got[0], pub))


class GradientFlowTest(unittest.TestCase):
    """Verify gradients flow through the network."""

    def test_backward_updates_parameters(self):
        config = RangeCFVNetConfig(
            trunk_hidden=16, trunk_layers=1, range_hidden=32, head_hidden=32, head_layers=1
        )
        model = build_cfv_model(config, seed=1)
        model.train()

        state = _preflop_state()
        pub = encode_public_state(state).unsqueeze(0)
        r0 = torch.tensor(
            uniform_reach_range(state.board_card_ids), dtype=torch.float32
        ).unsqueeze(0)
        r1 = torch.tensor(
            uniform_reach_range(state.board_card_ids), dtype=torch.float32
        ).unsqueeze(0)

        target = torch.zeros(1, 2, COMBO_COUNT)
        loss = nn.functional.mse_loss(model(pub, r0, r1), target)
        loss.backward()

        has_grad = any(
            p.grad is not None and p.grad.abs().sum().item() > 0
            for p in model.parameters()
        )
        self.assertTrue(has_grad)


class CrossStreetTest(unittest.TestCase):
    """Verify network works across all four streets."""

    def test_all_streets_forward(self):
        model = build_cfv_model(seed=55)
        states = {
            "preflop": _preflop_state(),
            "flop": _flop_state(),
            "turn": _turn_state(),
            "river": _river_state(),
        }
        for street, state in states.items():
            with self.subTest(street=street):
                r0 = uniform_reach_range(state.board_card_ids)
                r1 = uniform_reach_range(state.board_card_ids)
                cfv0, cfv1 = predict_cfv(model, state, r0, r1)
                self.assertEqual(len(cfv0), COMBO_COUNT)
                self.assertEqual(len(cfv1), COMBO_COUNT)
                # Check all values finite
                for v in cfv0:
                    self.assertTrue(math.isfinite(v))
                for v in cfv1:
                    self.assertTrue(math.isfinite(v))


class ParamCountTest(unittest.TestCase):
    """Sanity check on model parameter count."""

    def test_small_config_param_count(self):
        config = RangeCFVNetConfig(
            trunk_hidden=32, trunk_layers=2, range_hidden=64, head_hidden=64, head_layers=2
        )
        model = build_cfv_model(config, seed=0)
        total = sum(p.numel() for p in model.parameters())
        # Should be in a reasonable range for an 8GB GPU
        self.assertGreater(total, 10_000)
        self.assertLess(total, 5_000_000)


if __name__ == "__main__":
    unittest.main()
