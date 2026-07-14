from __future__ import annotations

import copy
import hashlib
import unittest
from pathlib import Path

import bots.research_native_lab.common_contracts as common_contracts_package

from bots.research_native_lab.common_contracts import (
    Action,
    ActionKind,
    LegalActionSet,
    NationalGameState,
)
from bots.research_native_lab.cfr_neural_search.native_runtime.common_adapter import (
    COMMON_CONTRACT_COMMIT,
    COMMON_CONTRACT_GIT_TREE,
    COMMON_RUNTIME_FILE_SHA256,
    BoundNationalAction,
    CommonContractAdapterError,
    NationalDecisionSnapshot,
    adapt_national_decision,
    invoke_route_policy,
)


class CommonAdapterTest(unittest.TestCase):
    def test_direct_binding_rejects_equality_gadgets_and_string_subclasses(self):
        class AlwaysEqual:
            def __bool__(self):
                return True

            def __eq__(self, _other):
                return True

            def __ne__(self, _other):
                return False

        class StringSubclass(str):
            pass

        action = Action(ActionKind.CALL)
        for identity in (AlwaysEqual(), StringSubclass("0" * 64), "0" * 63):
            with self.subTest(identity=identity):
                with self.assertRaises((TypeError, CommonContractAdapterError)):
                    BoundNationalAction(identity, action)  # type: ignore[arg-type]

    def test_common_dependency_content_binding_has_not_drifted(self):
        self.assertEqual(
            COMMON_CONTRACT_COMMIT,
            "a938d7cbc36016cb7b5cb444a7eb2e0f00cae73e",
        )
        self.assertEqual(
            COMMON_CONTRACT_GIT_TREE,
            "8066a0741bfefc42026d098f0ffc46cbfb424f45",
        )
        package_root = Path(common_contracts_package.__file__).resolve().parent
        for relative_path, expected_sha256 in COMMON_RUNTIME_FILE_SHA256:
            with self.subTest(relative_path=relative_path):
                payload = (package_root / relative_path).read_bytes()
                self.assertEqual(hashlib.sha256(payload).hexdigest(), expected_sha256)

    def test_common_subclasses_cannot_override_state_or_wire_semantics(self):
        class EvilAction(Action):
            def to_wire(self):
                return "raise 200 malicious"

        class EvilState(NationalGameState):
            def full_state_id(self):
                return "forged-state-id"

        state = NationalGameState.new_hand(1, small_blind=0)
        snapshot = adapt_national_decision(state)
        evil_action = EvilAction(ActionKind.CALL)
        with self.assertRaisesRegex(TypeError, "exact shared Common Action"):
            snapshot.bind(evil_action, current_state=state)

        evil_state = EvilState.from_dict(state.to_dict())
        with self.assertRaisesRegex(TypeError, "exact shared Common NationalGameState"):
            adapt_national_decision(evil_state)

    def test_policy_entrypoint_requires_common_snapshot_and_action(self):
        state = NationalGameState.new_hand(1, small_blind=0)
        observed = []

        def policy(decision):
            observed.append(decision)
            self.assertIs(decision.state, state)
            self.assertIs(type(decision.legal_actions), LegalActionSet)
            return Action(ActionKind.CALL)

        bound = invoke_route_policy(state, policy)
        self.assertEqual(len(observed), 1)
        self.assertEqual(bound.wire_action, "call")
        self.assertEqual(bound.apply_to(state).street_bets, (100, 100))

        with self.assertRaisesRegex(TypeError, "Common Action"):
            invoke_route_policy(state, lambda _decision: "call")  # type: ignore[return-value]

    def test_snapshot_preserves_common_types_and_exact_raise_boundaries(self):
        state = NationalGameState.new_hand(
            1,
            small_blind=0,
            hole_cards=((0, 4), ()),
        )
        snapshot = adapt_national_decision(state)

        self.assertIs(snapshot.state, state)
        self.assertIs(type(snapshot.legal_actions), LegalActionSet)
        self.assertEqual(snapshot.full_state_id, state.full_state_id())
        self.assertEqual(snapshot.public_state_id, state.hand_public_state_id())
        self.assertEqual(snapshot.information_state_id, state.information_state_id(0))

        actions = snapshot.representative_actions()
        self.assertTrue(actions)
        self.assertTrue(all(type(action) is Action for action in actions))
        self.assertIn(Action(ActionKind.RAISE, 200), actions)
        self.assertIn(Action(ActionKind.RAISE, 19_999), actions)
        self.assertIn(Action(ActionKind.ALLIN), actions)

        with self.assertRaisesRegex(CommonContractAdapterError, "LegalActionSet"):
            snapshot.bind(Action(ActionKind.RAISE, 199), current_state=state)
        with self.assertRaisesRegex(CommonContractAdapterError, "LegalActionSet"):
            snapshot.bind(Action(ActionKind.RAISE, 20_000), current_state=state)

    def test_bound_action_uses_common_wire_and_rejects_stale_state(self):
        state = NationalGameState.new_hand(1, small_blind=0)
        snapshot = adapt_national_decision(state)
        action = Action.from_wire("raise 200")
        bound = snapshot.bind(action, current_state=state)

        self.assertIs(bound.action, action)
        self.assertEqual(bound.wire_action, "raise 200")
        advanced = bound.apply_to(state)
        self.assertEqual(advanced.street_bets, (200, 100))
        with self.assertRaisesRegex(CommonContractAdapterError, "stale"):
            bound.apply_to(advanced)
        with self.assertRaisesRegex(CommonContractAdapterError, "stale"):
            snapshot.bind(Action(ActionKind.FOLD), current_state=advanced)

    def test_common_round_trip_has_identical_route_projection(self):
        state = NationalGameState.new_hand(9, small_blind=1)
        first = adapt_national_decision(state, controlled_player=1)
        restored = NationalGameState.from_dict(state.to_dict())
        second = adapt_national_decision(restored, controlled_player=1)

        self.assertEqual(first.full_state_id, second.full_state_id)
        self.assertEqual(first.public_state_id, second.public_state_id)
        self.assertEqual(first.information_state_id, second.information_state_id)
        self.assertEqual(first.legal_actions, second.legal_actions)

    def test_nondecision_wrong_owner_and_forged_state_fail_closed(self):
        state = NationalGameState.new_hand(1, small_blind=1)
        with self.assertRaisesRegex(CommonContractAdapterError, "controlled_player"):
            adapt_national_decision(state)
        with self.assertRaisesRegex(CommonContractAdapterError, "0 or 1"):
            adapt_national_decision(state, controlled_player=2)
        for alias in (False, True):
            with self.subTest(controlled_player=alias):
                with self.assertRaisesRegex(CommonContractAdapterError, "0 or 1"):
                    adapt_national_decision(
                        NationalGameState.new_hand(1, small_blind=int(alias)),
                        controlled_player=alias,  # type: ignore[arg-type]
                    )
        with self.assertRaisesRegex(TypeError, "Common NationalGameState"):
            adapt_national_decision(object())  # type: ignore[arg-type]

        # Common deliberately strips its trusted replay guard from copies.
        forged = copy.copy(state)
        with self.assertRaisesRegex(ValueError, "replay validation"):
            adapt_national_decision(forged, controlled_player=1)

        folded = state.apply_action(Action(ActionKind.FOLD))
        with self.assertRaisesRegex(CommonContractAdapterError, "controlled_player"):
            adapt_national_decision(folded, controlled_player=1)

    def test_direct_snapshot_construction_cannot_forge_legality_or_identity(self):
        state = NationalGameState.new_hand(1, small_blind=0)
        legal = state.legal_actions()
        with self.assertRaisesRegex(CommonContractAdapterError, "0 or 1"):
            NationalDecisionSnapshot(
                state=state,
                controlled_player=False,  # type: ignore[arg-type]
                full_state_id=state.full_state_id(),
                public_state_id=state.hand_public_state_id(),
                information_state_id=state.information_state_id(0),
                legal_actions=legal,
            )
        with self.assertRaisesRegex(CommonContractAdapterError, "SHA-256"):
            NationalDecisionSnapshot(
                state=state,
                controlled_player=0,
                full_state_id="forged",
                public_state_id=state.hand_public_state_id(),
                information_state_id=state.information_state_id(0),
                legal_actions=legal,
            )
        with self.assertRaisesRegex(CommonContractAdapterError, "disagree"):
            NationalDecisionSnapshot(
                state=state,
                controlled_player=0,
                full_state_id=state.full_state_id(),
                public_state_id=state.hand_public_state_id(),
                information_state_id=state.information_state_id(0),
                legal_actions=LegalActionSet(
                    fold=True,
                    check=True,
                    call=True,
                    allin=True,
                    min_raise_to=1,
                    max_raise_to=20_000,
                ),
            )
        valid = {
            "state": state,
            "controlled_player": 0,
            "full_state_id": state.full_state_id(),
            "public_state_id": state.hand_public_state_id(),
            "information_state_id": state.information_state_id(0),
            "legal_actions": legal,
        }
        class StringSubclass(str):
            pass

        class LegalSubclass(LegalActionSet):
            pass

        attacks = (
            {**valid, "full_state_id": StringSubclass(valid["full_state_id"])},
            {**valid, "public_state_id": object()},
            {**valid, "information_state_id": "f" * 63},
            {
                **valid,
                "legal_actions": LegalSubclass(
                    fold=legal.fold,
                    check=legal.check,
                    call=legal.call,
                    allin=legal.allin,
                    min_raise_to=legal.min_raise_to,
                    max_raise_to=legal.max_raise_to,
                ),
            },
        )
        for attack in attacks:
            with self.subTest(attack=attack):
                with self.assertRaises((TypeError, CommonContractAdapterError)):
                    NationalDecisionSnapshot(**attack)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
