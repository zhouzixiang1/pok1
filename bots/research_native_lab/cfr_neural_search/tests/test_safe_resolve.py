from __future__ import annotations

import unittest

from bots.research_native_lab.cfr_neural_search.blueprint.small_games import (
    BET,
    CALL,
    CHECK,
    FOLD,
)
from bots.research_native_lab.cfr_neural_search.online_solver.safe_resolve import (
    KuhnSafetyConstraint,
    build_kuhn_check_safety_constraint,
    certify_kuhn_check_replacement,
    kuhn_check_replacement_policy,
    resolve_kuhn_check_subgame,
)


def _response_key(rank: int) -> str:
    return f"kuhn:p0:r{rank}:h={CHECK},{BET}"


class SafeResolveTest(unittest.TestCase):
    def test_constraint_is_digest_bound_to_normalized_blueprint(self):
        first = build_kuhn_check_safety_constraint({})
        second = build_kuhn_check_safety_constraint(
            {_response_key(1): {CALL: 2.0 / 3.0, FOLD: 1.0 / 3.0}}
        )
        repeated = build_kuhn_check_safety_constraint({})
        self.assertEqual(first.sha256, repeated.sha256)
        self.assertNotEqual(first.sha256, second.sha256)
        self.assertEqual(first.information_states, (
            "kuhn:p1:r0:h=check",
            "kuhn:p1:r1:h=check",
            "kuhn:p1:r2:h=check",
        ))

    def test_uniform_blueprint_has_certified_improving_replacement(self):
        result = resolve_kuhn_check_subgame(
            {},
            probability_grid=(0.0, 0.5, 1.0),
            mode="safe",
        )
        self.assertEqual(result.call_probabilities, (0.0, 1.0, 1.0))
        self.assertTrue(result.certificate.accepted)
        self.assertTrue(result.certificate.local_cbv_constraints_satisfied)
        self.assertTrue(result.certificate.global_exploitability_oracle_satisfied)
        self.assertTrue(result.certificate.resolver_best_response_invariant)
        self.assertEqual(
            type(result.certificate).__name__,
            "OracleCertifiedKuhnResolveCertificate",
        )
        self.assertEqual(
            result.certificate.constraint.blueprint_policy_sha256,
            build_kuhn_check_safety_constraint({}).blueprint_policy_sha256,
        )
        self.assertNotEqual(
            result.certificate.candidate_policy_sha256,
            result.certificate.constraint.blueprint_policy_sha256,
        )
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
        self.assertFalse(plain.certificate.local_cbv_constraints_satisfied)
        self.assertFalse(plain.certificate.global_exploitability_oracle_satisfied)
        self.assertLess(plain.certificate.minimum_margin, 0.0)
        self.assertGreater(
            plain.certificate.candidate_exploitability,
            plain.certificate.baseline_exploitability,
        )
        self.assertEqual(safe.call_probabilities, (0.0, 2.0 / 3.0, 1.0))
        self.assertTrue(safe.certificate.accepted)
        self.assertTrue(safe.certificate.local_cbv_constraints_satisfied)
        self.assertTrue(safe.certificate.global_exploitability_oracle_satisfied)
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

    def test_bool_and_numeric_string_inputs_are_not_coerced(self):
        invalid_profiles = (
            {_response_key(0): {CALL: True, FOLD: False}},
            {_response_key(0): {CALL: "0.5", FOLD: "0.5"}},
        )
        for profile in invalid_profiles:
            with self.subTest(profile=profile):
                with self.assertRaisesRegex(TypeError, "bool/string"):
                    build_kuhn_check_safety_constraint(profile)  # type: ignore[arg-type]
        for calls in ((True, 0.5, 1.0), ("0.0", 0.5, 1.0)):
            with self.subTest(calls=calls):
                with self.assertRaisesRegex(TypeError, "bool/string"):
                    kuhn_check_replacement_policy({}, calls)  # type: ignore[arg-type]
        for grid in ((False, 0.5, 1.0), ("0.0", 0.5, 1.0)):
            with self.subTest(grid=grid):
                with self.assertRaisesRegex(TypeError, "bool/string"):
                    resolve_kuhn_check_subgame({}, probability_grid=grid)  # type: ignore[arg-type]
        with self.assertRaisesRegex(TypeError, "bool/string"):
            certify_kuhn_check_replacement({}, {}, tolerance=True)

    def test_direct_safety_constraint_cannot_forge_identity_schema(self):
        valid = build_kuhn_check_safety_constraint({})
        baseline = {
            "blueprint_policy_sha256": valid.blueprint_policy_sha256,
            "opponent_player": valid.opponent_player,
            "information_states": valid.information_states,
            "maximum_counterfactual_values": valid.maximum_counterfactual_values,
        }
        attacks = (
            {**baseline, "opponent_player": True},
            {**baseline, "blueprint_policy_sha256": "0" * 63},
            {**baseline, "information_states": ("forged",)},
            {**baseline, "maximum_counterfactual_values": (True,) * 3},
        )
        for attack in attacks:
            with self.subTest(attack=attack):
                with self.assertRaises((TypeError, ValueError)):
                    KuhnSafetyConstraint(**attack)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
