from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bots.research_native_lab.common_contracts import Action, ActionKind, NationalGameState
from bots.research_native_lab.common_contracts.protocol import StreamDecoder
from bots.research_native_lab.cfr_neural_search.blueprint.artifact import (
    BlueprintPolicy,
    compile_blueprint_payload,
    load_blueprint_artifact,
    save_blueprint_artifact,
)
from bots.research_native_lab.cfr_neural_search.blueprint.hunl_abstraction import (
    information_descriptor,
)
from bots.research_native_lab.cfr_neural_search.blueprint.hunl_game import HUNLTrainingGame
from bots.research_native_lab.cfr_neural_search.blueprint.mccfr import (
    SolverConfig,
    SolverState,
)
from bots.research_native_lab.cfr_neural_search.native_runtime.local_evidence import (
    BACKEND_RELATIVE_FILES,
    load_reproducibility_evidence,
    run_reproducibility_gate,
    save_reproducibility_evidence,
)
from bots.research_native_lab.cfr_neural_search.native_runtime.national_bot import (
    _official_delay,
)
from bots.research_native_lab.cfr_neural_search.native_runtime.socket_client import (
    NativeBlueprintClient,
)


def _synthetic_trained_state(game: HUNLTrainingGame) -> tuple[SolverState, object]:
    state = SolverState.new_for_game(
        game,
        SolverConfig(seed=2026071401, samples_per_player=1),
    )
    hand = game.new_initial_state().child("small_blind:0")
    for card in (48, 44, 49, 45):
        hand = hand.child(card)
    descriptor = information_descriptor(hand.common_state, 0, game.abstraction)
    actions = descriptor.action_labels
    state.actions[descriptor.exact_key] = actions
    state.regrets[descriptor.exact_key] = {action: 0.0 for action in actions}
    state.strategy_sum[descriptor.exact_key] = {
        action: 10.0 if action == "fold" else 1.0 for action in actions
    }
    state.batch_index = 2
    state.trajectories = 4
    state.node_touches = 10
    state.validate()
    return state, hand


class BlueprintArtifactTest(unittest.TestCase):
    def test_sparse_artifact_reports_material_rows_and_training_seed_only(self):
        game = HUNLTrainingGame()
        state, _hand = _synthetic_trained_state(game)
        payload = compile_blueprint_payload(game, state)
        self.assertEqual(
            payload["seeds"],
            {"training": 2026071401, "domain": "training-only-counter-root-v1"},
        )
        self.assertNotIn("smoke", str(payload["seeds"]).lower())
        self.assertEqual(payload["statistics"]["exact_row_count"], 1)
        self.assertEqual(payload["statistics"]["backoff_row_count"], 4)
        self.assertGreater(payload["statistics"]["materially_nonuniform_all_rows"], 0)
        self.assertGreater(payload["statistics"]["max_l1_from_uniform"], 0.0)

    def test_exact_backoff_uniform_order_and_separate_counters(self):
        game = HUNLTrainingGame()
        state, exact_hand = _synthetic_trained_state(game)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "blueprint.rbbp"
            save_blueprint_artifact(path, game, state, root=directory)
            loaded = load_blueprint_artifact(path, game, root=directory)
        policy = BlueprintPolicy(loaded)
        exact = policy.decide(
            exact_hand.common_state,
            0,
            policy_seed=7,
            decision_counter=0,
        )
        self.assertEqual(exact.source, "exact")
        different = game.new_initial_state().child("small_blind:0")
        for card in (40, 36, 41, 37):
            different = different.child(card)
        backoff = policy.decide(
            different.common_state,
            0,
            policy_seed=7,
            decision_counter=1,
        )
        self.assertTrue(backoff.source.startswith("backoff"))
        after_call = different.child("call")
        emergency = policy.decide(
            after_call.common_state,
            1,
            policy_seed=7,
            decision_counter=2,
        )
        self.assertEqual(emergency.source, "uniform_emergency")
        self.assertEqual(policy.counters.exact_hits, 1)
        self.assertEqual(policy.counters.backoff_hits, 1)
        self.assertEqual(policy.counters.uniform_emergency, 1)
        self.assertEqual(policy.counters.materially_nonuniform_decisions, 2)

    def test_loader_rejects_compressed_corruption_and_symlink(self):
        game = HUNLTrainingGame()
        state, _ = _synthetic_trained_state(game)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "blueprint.rbbp"
            save_blueprint_artifact(path, game, state, root=root)
            content = bytearray(path.read_bytes())
            content[-1] ^= 1
            path.write_bytes(content)
            with self.assertRaises(ValueError):
                load_blueprint_artifact(path, game, root=root)
            path.unlink()
            target = root / "target"
            target.write_bytes(b"not-an-artifact")
            path.symlink_to(target)
            with self.assertRaises(ValueError):
                load_blueprint_artifact(path, game, root=root)

    def test_external_policy_seed_cannot_change_frozen_artifact_bytes(self):
        game = HUNLTrainingGame()
        state, _ = _synthetic_trained_state(game)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.rbbp"
            second = root / "second.rbbp"
            first_sha = save_blueprint_artifact(first, game, state, root=root)
            second_sha = save_blueprint_artifact(second, game, state, root=root)
            self.assertEqual(first_sha, second_sha)
            self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_boundary_inference_audit_flag_cannot_change_infoset_or_policy(self):
        game = HUNLTrainingGame()

        def pending_flop(inferred: bool) -> NationalGameState:
            state = NationalGameState.new_hand(
                1,
                small_blind=0,
                hole_cards=((48, 44), (49, 45)),
            )
            state = state.apply_action(
                Action(ActionKind.CALL),
                inferred_from_boundary=inferred,
            )
            state = state.apply_action(Action(ActionKind.CHECK))
            state = state.apply_chance((0, 5, 10))
            return state.apply_action(Action(ActionKind.CHECK))

        explicit = pending_flop(False)
        inferred = pending_flop(True)
        self.assertFalse(explicit.hand_history[0].inferred_from_boundary)
        self.assertTrue(inferred.hand_history[0].inferred_from_boundary)
        explicit_descriptor = information_descriptor(explicit, 0, game.abstraction)
        inferred_descriptor = information_descriptor(inferred, 0, game.abstraction)
        self.assertEqual(explicit_descriptor.exact_key, inferred_descriptor.exact_key)
        self.assertEqual(explicit_descriptor.backoff_keys, inferred_descriptor.backoff_keys)

        state = SolverState.new_for_game(
            game,
            SolverConfig(seed=2026071401, samples_per_player=1),
        )
        actions = explicit_descriptor.action_labels
        state.actions[explicit_descriptor.exact_key] = actions
        state.regrets[explicit_descriptor.exact_key] = {
            action: 0.0 for action in actions
        }
        state.strategy_sum[explicit_descriptor.exact_key] = {
            action: float(index + 1) for index, action in enumerate(actions)
        }
        state.batch_index = 2
        state.trajectories = 4
        state.node_touches = 10
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "blueprint.rbbp"
            save_blueprint_artifact(path, game, state, root=directory)
            loaded = load_blueprint_artifact(path, game, root=directory)
        first = BlueprintPolicy(loaded).decide(
            explicit,
            0,
            policy_seed=123,
            decision_counter=7,
        )
        second = BlueprintPolicy(loaded).decide(
            inferred,
            0,
            policy_seed=123,
            decision_counter=7,
        )
        self.assertEqual(first, second)


class NativeBlueprintEvidenceTest(unittest.TestCase):
    def test_fixed_real_tcp_70_hand_projection_replays_exactly(self):
        game = HUNLTrainingGame()
        state, _ = _synthetic_trained_state(game)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "blueprint.rbbp"
            save_blueprint_artifact(path, game, state, root=root)
            first, second = run_reproducibility_gate(
                path,
                deck_root_seed=2026071499001,
                policy_seeds=(2026071501001, 2026071501002),
            )
            evidence_path = root / "local-evidence.json"
            save_reproducibility_evidence(
                evidence_path,
                first,
                second,
                root=root,
            )
            stored = load_reproducibility_evidence(evidence_path, root=root)
        self.assertEqual(first.semantic_projection, second.semantic_projection)
        self.assertEqual(first.hands, 70)
        self.assertEqual(first.illegal_actions, 0)
        self.assertEqual(first.timeouts, 0)
        self.assertTrue(first.acceptance["both_sides_materially_nonuniform"])
        self.assertEqual(set(first.backend_files), set(BACKEND_RELATIVE_FILES))
        self.assertEqual(first.earnings[0] + first.earnings[1], 0)
        self.assertEqual(first.acceptance["chips_have_zero_acceptance_weight"], True)
        self.assertEqual(stored["run_count"], 2)
        self.assertEqual(
            stored["runs"][0]["semantic_projection"],
            stored["runs"][1]["semantic_projection"],
        )

    def test_local_lf_and_official_raw_have_separate_outbound_framing(self):
        game = HUNLTrainingGame()
        state, _ = _synthetic_trained_state(game)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "blueprint.rbbp"
            save_blueprint_artifact(path, game, state, root=root)
            blueprint = load_blueprint_artifact(path, game, root=root)

        class FakeSocket:
            def __init__(self):
                self.payloads = []

            def sendall(self, payload):
                self.payloads.append(payload)

        local = NativeBlueprintClient(
            bot_name="Local",
            policy=BlueprintPolicy(blueprint),
            policy_seed=1,
            wire_mode="local-sever-lf",
            action_delay_sec=0.0,
        )
        local_socket = FakeSocket()
        local._socket = local_socket
        local._send("raise 200")
        self.assertEqual(local_socket.payloads, [b"raise 200\n"])

        official = NativeBlueprintClient(
            bot_name="Official",
            policy=BlueprintPolicy(blueprint),
            policy_seed=1,
            wire_mode="official-raw",
            action_delay_sec=0.30,
        )
        official_socket = FakeSocket()
        official._socket = official_socket
        official._send("raise 200")
        self.assertEqual(official_socket.payloads, [b"raise 200"])

    def test_official_delay_defaults_to_safe_value_and_rejects_nonfinite(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(_official_delay(), 0.30)
        for value in ("nan", "inf", "-inf", "-0.1"):
            with self.subTest(value=value), mock.patch.dict(
                "os.environ",
                {"POK_OFFICIAL_ACTION_DELAY": value},
                clear=True,
            ):
                with self.assertRaises(ValueError):
                    _official_delay()

    def test_raw_decoder_sticky_split_numeric_and_allin_tokens(self):
        decoder = StreamDecoder()
        tokens = decoder.feed(b"earnChips -50preflop|SMALLBLIND|<0,12><1,11>all")
        self.assertEqual(
            tokens,
            ["earnChips -50", "preflop|SMALLBLIND|<0,12><1,11>"],
        )
        self.assertEqual(decoder.feed(b"inraise 4"), ["allin"])
        self.assertEqual(decoder.feed(b"00"), [])
        self.assertEqual(decoder.flush_numeric(), ["raise 400"])

    def test_route_ast_has_no_top_level_engine_import(self):
        route = Path(__file__).parents[1]
        violations = []
        for path in route.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                if any(name == "engine" or name.startswith("engine.") for name in names):
                    violations.append((path, node.lineno))
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
