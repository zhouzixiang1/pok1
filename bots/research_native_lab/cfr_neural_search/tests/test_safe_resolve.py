from __future__ import annotations

import unittest

from bots.research_native_lab.cfr_neural_search.blueprint.small_games import (
    BET,
    CALL,
    CHECK,
    FOLD,
)
from bots.research_native_lab.cfr_neural_search.online_solver.safe_resolve import (
    certify_kuhn_check_replacement,
    kuhn_check_replacement_policy,
    resolve_kuhn_check_subgame,
)


def _response_key(rank: int) -> str:
    return f"kuhn:p0:r{rank}:h={CHECK},{BET}"


class SafeResolveTest(unittest.TestCase):
    def test_uniform_blueprint_has_certified_improving_replacement(self):
        result = resolve_kuhn_check_subgame(
            {},
            probability_grid=(0.0, 0.5, 1.0),
            mode="safe",
        )
        self.assertEqual(result.call_probabilities, (0.0, 1.0, 1.0))
        self.assertTrue(result.certificate.accepted)
        self.assertGreaterEqual(result.certificate.minimum_margin, 0.0)
        self.assertLess(
            result.certificate.candidate_exploitability,
            result.certificate.baseline_exploitability,
        )
        self.assertEqual(result.candidates_considered, 27)
        self.assertGreater(result.safe_candidates, 0)

    def test_plain_resolve_can_be_unsafe_and_safe_mode_falls_back(self):
        blueprint = {
            _response_key(0): {CALL: 0.0, FOLD: 1.0},
            _response_key(1): {CALL: 2.0 / 3.0, FOLD: 1.0 / 3.0},
            _response_key(2): {CALL: 1.0, FOLD: 0.0},
        }
        plain = resolve_kuhn_check_subgame(
            blueprint,
            probability_grid=(0.0, 0.5, 1.0),
            mode="plain",
        )
        safe = resolve_kuhn_check_subgame(
            blueprint,
            probability_grid=(0.0, 0.5, 1.0),
            mode="safe",
        )
        self.assertEqual(plain.call_probabilities, (0.0, 1.0, 1.0))
        self.assertFalse(plain.certificate.accepted)
        self.assertLess(plain.certificate.minimum_margin, 0.0)
        self.assertGreater(
            plain.certificate.candidate_exploitability,
            plain.certificate.baseline_exploitability,
        )
        self.assertEqual(safe.call_probabilities, (0.0, 2.0 / 3.0, 1.0))
        self.assertTrue(safe.certificate.accepted)
        self.assertAlmostEqual(
            safe.certificate.candidate_exploitability,
            safe.certificate.baseline_exploitability,
            places=12,
        )

    def test_adversarial_leaf_style_candidate_is_rejected(self):
        candidate = kuhn_check_replacement_policy({}, (1.0, 0.0, 0.0))
        certificate = certify_kuhn_check_replacement({}, candidate)
        self.assertFalse(certificate.accepted)
        self.assertAlmostEqual(certificate.minimum_margin, -0.25, places=12)

    def test_candidate_cannot_change_policy_outside_public_subtree(self):
        candidate = kuhn_check_replacement_policy({}, (0.0, 0.5, 1.0))
        candidate["kuhn:p0:r0:h=root"] = {CHECK: 1.0, BET: 0.0}
        with self.assertRaisesRegex(ValueError, "outside the Kuhn check subtree"):
            certify_kuhn_check_replacement({}, candidate)

    def test_invalid_probability_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "in \\[0, 1\\]"):
            kuhn_check_replacement_policy({}, (-0.1, 0.5, 1.0))


if __name__ == "__main__":
    unittest.main()
