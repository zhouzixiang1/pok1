from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from bots.research_native_lab.cfr_neural_search.blueprint.mccfr import (
    SolverConfig,
    SolverState,
    _sha256,
    apply_shards,
    average_policy,
    build_shard,
    load_checkpoint,
    load_shard,
    save_checkpoint,
    save_shard,
    train_batches,
)
from bots.research_native_lab.cfr_neural_search.blueprint.small_games import KuhnPoker


def _clone(state: SolverState) -> SolverState:
    return SolverState.from_payload(json.loads(json.dumps(state.to_payload())))


def _payload_bytes(state: SolverState) -> bytes:
    return json.dumps(
        state.to_payload(),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


class CheckpointShardTest(unittest.TestCase):
    def test_direct_in_memory_type_confusion_is_rejected_transactionally(self):
        game = KuhnPoker()
        state = SolverState(
            game.name,
            SolverConfig(seed=908, samples_per_player=2),
        )
        train_batches(game, state, batches=1)
        shard = build_shard(game, state, 0, 1)
        samples = list(shard.samples)
        sample_index = next(
            index for index, sample in enumerate(samples) if sample.regret_delta
        )
        sample = samples[sample_index]
        key = next(iter(sample.regret_delta))
        action = next(iter(sample.regret_delta[key]))

        bad_regret = {
            row_key: dict(vector)
            for row_key, vector in sample.regret_delta.items()
        }
        bad_regret[key][action] = True
        bad_strategy = {
            row_key: dict(vector)
            for row_key, vector in sample.strategy_delta.items()
        }
        strategy_key = next(iter(bad_strategy))
        strategy_action = next(iter(bad_strategy[strategy_key]))
        bad_strategy[strategy_key][strategy_action] = "1.0"

        sample_mutations = (
            replace(sample, regret_delta=bad_regret),
            replace(sample, strategy_delta=bad_strategy),
            replace(sample, traverser=False),
            replace(sample, sample_id=False),
            replace(sample, node_touches=True),
        )
        shard_mutations = (
            replace(shard, shard_index=False),
            replace(shard, shard_count=True),
            replace(shard, samples_per_player=True),
        )
        attacks = []
        for bad_sample in sample_mutations:
            bad_samples = list(samples)
            bad_samples[sample_index] = bad_sample
            attacks.append(replace(shard, samples=tuple(bad_samples)))
        attacks.extend(shard_mutations)

        for index, bad_shard in enumerate(attacks):
            with self.subTest(attack=index):
                before = _payload_bytes(state)
                with self.assertRaises(TypeError):
                    apply_shards(game, state, [bad_shard])
                self.assertEqual(_payload_bytes(state), before)

    def test_public_training_boundaries_reject_bool_integer_aliases(self):
        game = KuhnPoker()
        state = SolverState(game.name, SolverConfig(samples_per_player=2))
        with self.assertRaisesRegex(TypeError, "exact integers"):
            build_shard(game, state, False, 1)
        with self.assertRaisesRegex(TypeError, "exact integers"):
            build_shard(game, state, 0, True)
        with self.assertRaisesRegex(TypeError, "exact integers"):
            train_batches(game, state, batches=False)
        with self.assertRaisesRegex(TypeError, "exact integers"):
            train_batches(game, state, batches=1, shard_count=True)

    def test_rehashed_checkpoint_rejects_coerced_json_types_and_schema_drift(self):
        game = KuhnPoker()
        state = SolverState(game.name, SolverConfig(seed=88))
        train_batches(game, state, batches=1)
        base = state.to_payload()
        first_key = next(iter(base["actions"]))
        first_action = base["actions"][first_key][0]

        def mutate_batch_string(payload):
            payload["batch_index"] = "1"

        def mutate_trajectory_bool(payload):
            payload["trajectories"] = True

        def mutate_seed_string(payload):
            payload["config"]["seed"] = "88"

        def mutate_sample_float(payload):
            payload["config"]["samples_per_player"] = 1.0

        def mutate_action_number(payload):
            payload["actions"][first_key][0] = 7

        def mutate_regret_string(payload):
            payload["regrets"][first_key][first_action] = "0.0"

        def mutate_strategy_bool(payload):
            payload["strategy_sum"][first_key][first_action] = False

        def mutate_extra_key(payload):
            payload["unexpected"] = 1

        mutations = (
            mutate_batch_string,
            mutate_trajectory_bool,
            mutate_seed_string,
            mutate_sample_float,
            mutate_action_number,
            mutate_regret_string,
            mutate_strategy_bool,
            mutate_extra_key,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "strict-checkpoint.json"
            for mutation in mutations:
                with self.subTest(mutation=mutation.__name__):
                    payload = json.loads(json.dumps(base))
                    mutation(payload)
                    envelope = {"payload": payload, "sha256": _sha256(payload)}
                    path.write_text(json.dumps(envelope), encoding="utf-8")
                    with self.assertRaises((TypeError, ValueError)):
                        load_checkpoint(path)

    def test_rehashed_shard_rejects_bool_sample_identity(self):
        game = KuhnPoker()
        state = SolverState(game.name, SolverConfig(samples_per_player=2))
        shard = build_shard(game, state, 0, 1)
        payload = shard.to_payload()
        payload["samples"][0]["sample_id"] = False
        envelope = {"payload": payload, "sha256": _sha256(payload)}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "strict-shard.json"
            path.write_text(json.dumps(envelope), encoding="utf-8")
            with self.assertRaisesRegex(TypeError, "JSON integer"):
                load_shard(path)

    def test_rehashed_shard_cannot_drop_one_present_action_delta(self):
        game = KuhnPoker()
        state = SolverState(game.name, SolverConfig(samples_per_player=2))
        shard = build_shard(game, state, 0, 1)
        payload = shard.to_payload()
        sample_payload = next(
            sample for sample in payload["samples"] if sample["regret_delta"]
        )
        key = next(iter(sample_payload["regret_delta"]))
        action = next(iter(sample_payload["regret_delta"][key]))
        del sample_payload["regret_delta"][key][action]
        envelope = {"payload": payload, "sha256": _sha256(payload)}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "incomplete-vector-shard.json"
            path.write_text(json.dumps(envelope), encoding="utf-8")
            loaded = load_shard(path)
            with self.assertRaisesRegex(ValueError, "complete action set"):
                apply_shards(game, state, [loaded])

    def test_negative_strategy_sum_is_never_clipped_or_exported(self):
        game = KuhnPoker()
        state = SolverState(game.name, SolverConfig(samples_per_player=2))
        train_batches(game, state, batches=1)
        before = _payload_bytes(state)
        key = next(iter(state.strategy_sum))
        action = next(iter(state.strategy_sum[key]))
        state.strategy_sum[key][action] = -1.0
        corrupted_bytes = _payload_bytes(state)

        with self.assertRaisesRegex(ValueError, "strategy_sum has negative"):
            state.validate()
        with self.assertRaisesRegex(ValueError, "strategy_sum has negative"):
            average_policy(state)
        shard = build_shard(
            game,
            SolverState(game.name, SolverConfig(samples_per_player=2)),
            0,
            1,
        )
        with self.assertRaisesRegex(ValueError, "strategy_sum has negative"):
            apply_shards(game, state, [shard])
        # Failure must not mutate anything beyond the injected fault.
        self.assertNotEqual(_payload_bytes(state), before)
        self.assertEqual(_payload_bytes(state), corrupted_bytes)
        self.assertEqual(state.strategy_sum[key][action], -1.0)

    def test_rehashed_checkpoint_with_negative_strategy_sum_is_rejected(self):
        game = KuhnPoker()
        state = SolverState(game.name, SolverConfig())
        train_batches(game, state, batches=1)
        payload = state.to_payload()
        key = next(iter(payload["strategy_sum"]))
        action = next(iter(payload["strategy_sum"][key]))
        payload["strategy_sum"][key][action] = -0.01
        envelope = {"payload": payload, "sha256": _sha256(payload)}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "negative-checkpoint.json"
            path.write_text(json.dumps(envelope), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "strategy_sum has negative"):
                load_checkpoint(path)

    def test_shard_layout_is_digest_invariant(self):
        game = KuhnPoker()
        base = SolverState(
            game.name,
            SolverConfig(seed=41, samples_per_player=8, update_rule="dcfr"),
        )
        one = _clone(base)
        many = _clone(base)
        apply_shards(game, one, [build_shard(game, one, 0, 1)])
        shards = [build_shard(game, many, index, 4) for index in range(4)]
        apply_shards(game, many, reversed(shards))
        self.assertEqual(one.to_payload(), many.to_payload())
        self.assertEqual(one.digest, many.digest)

    def test_multi_batch_shard_layout_is_digest_invariant(self):
        game = KuhnPoker()
        config = SolverConfig(
            seed=410,
            samples_per_player=8,
            update_rule="linear",
        )
        one = SolverState(game.name, config)
        many = SolverState(game.name, config)
        train_batches(game, one, batches=40, shard_count=1)
        train_batches(game, many, batches=40, shard_count=4)
        self.assertEqual(one.to_payload(), many.to_payload())
        self.assertEqual(one.digest, many.digest)

    def test_checkpoint_resume_matches_uninterrupted(self):
        game = KuhnPoker()
        config = SolverConfig(seed=88, samples_per_player=3, update_rule="cfr_plus")
        uninterrupted = SolverState(game.name, config)
        resumed = SolverState(game.name, config)
        train_batches(game, uninterrupted, batches=80, shard_count=3)
        train_batches(game, resumed, batches=30, shard_count=3)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.json"
            digest = save_checkpoint(path, resumed)
            self.assertEqual(digest, resumed.digest)
            resumed = load_checkpoint(path)
        train_batches(game, resumed, batches=50, shard_count=3)
        self.assertEqual(uninterrupted.to_payload(), resumed.to_payload())
        self.assertEqual(uninterrupted.digest, resumed.digest)

    def test_resume_across_shard_layouts_for_linear_and_dcfr(self):
        game = KuhnPoker()
        for rule in ("linear", "dcfr"):
            with self.subTest(rule=rule):
                config = SolverConfig(
                    seed=881,
                    samples_per_player=8,
                    update_rule=rule,
                )
                uninterrupted = SolverState(game.name, config)
                resumed = SolverState(game.name, config)
                train_batches(game, uninterrupted, batches=50, shard_count=2)
                train_batches(game, resumed, batches=17, shard_count=1)
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "checkpoint.json"
                    save_checkpoint(path, resumed)
                    resumed = load_checkpoint(path)
                train_batches(game, resumed, batches=33, shard_count=4)
                self.assertEqual(uninterrupted.to_payload(), resumed.to_payload())
                self.assertEqual(uninterrupted.digest, resumed.digest)

    def test_shard_round_trip_and_hash_tamper_detection(self):
        game = KuhnPoker()
        state = SolverState(game.name, SolverConfig(samples_per_player=2))
        shard = build_shard(game, state, 0, 1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shard.json"
            save_shard(path, shard)
            self.assertEqual(load_shard(path), shard)
            envelope = json.loads(path.read_text(encoding="utf-8"))
            envelope["payload"]["batch_index"] = 99
            path.write_text(json.dumps(envelope), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                load_shard(path)

    def test_incomplete_or_duplicate_shards_are_rejected(self):
        game = KuhnPoker()
        state = SolverState(game.name, SolverConfig(samples_per_player=4))
        shard = build_shard(game, state, 0, 2)
        with self.assertRaisesRegex(ValueError, "every unique shard"):
            apply_shards(game, state, [shard])
        with self.assertRaisesRegex(ValueError, "every unique shard"):
            apply_shards(game, state, [shard, shard])

    def test_sample_moved_to_wrong_deterministic_shard_is_rejected(self):
        game = KuhnPoker()
        state = SolverState(game.name, SolverConfig(samples_per_player=4))
        shards = [build_shard(game, state, index, 2) for index in range(2)]
        bad_zero = replace(
            shards[0],
            samples=(shards[1].samples[0],) + shards[0].samples[1:],
        )
        bad_one = replace(
            shards[1],
            samples=(shards[0].samples[0],) + shards[1].samples[1:],
        )
        with self.assertRaisesRegex(ValueError, "wrong deterministic shard"):
            apply_shards(game, state, [bad_zero, bad_one])

    def test_rejected_action_drift_is_transactional(self):
        game = KuhnPoker()
        state = SolverState(game.name, SolverConfig(samples_per_player=2))
        train_batches(game, state, batches=1)
        before = state.digest
        before_bytes = _payload_bytes(state)
        shard = build_shard(game, state, 0, 1)
        sample = shard.samples[0]
        key = next(iter(sample.action_sets))
        bad_action_sets = dict(sample.action_sets)
        bad_action_sets[key] = tuple(reversed(bad_action_sets[key]))
        bad_sample = replace(sample, action_sets=bad_action_sets)
        bad_shard = replace(shard, samples=(bad_sample,) + shard.samples[1:])
        with self.assertRaisesRegex(ValueError, "action drift"):
            apply_shards(game, state, [bad_shard])
        self.assertEqual(state.digest, before)
        self.assertEqual(_payload_bytes(state), before_bytes)

    def test_rejected_nan_delta_is_transactional(self):
        game = KuhnPoker()
        state = SolverState(game.name, SolverConfig(samples_per_player=2))
        train_batches(game, state, batches=1)
        before = state.digest
        before_bytes = _payload_bytes(state)
        shard = build_shard(game, state, 0, 1)
        samples = list(shard.samples)
        sample_index = next(
            index for index, sample in enumerate(samples) if sample.regret_delta
        )
        sample = samples[sample_index]
        bad_regret_delta = {
            key: dict(vector) for key, vector in sample.regret_delta.items()
        }
        key = next(iter(bad_regret_delta))
        action = next(iter(bad_regret_delta[key]))
        bad_regret_delta[key][action] = float("nan")
        samples[sample_index] = replace(sample, regret_delta=bad_regret_delta)
        bad_shard = replace(shard, samples=tuple(samples))
        with self.assertRaisesRegex(ValueError, "non-finite"):
            apply_shards(game, state, [bad_shard])
        self.assertEqual(state.digest, before)
        self.assertEqual(_payload_bytes(state), before_bytes)

    def test_solver_state_rejects_orphan_information_state_tables(self):
        for table_name in ("regrets", "strategy_sum"):
            with self.subTest(table=table_name):
                state = SolverState(
                    game_name="kuhn",
                    config=SolverConfig(),
                    actions={"known": ("a", "b")},
                    regrets={"known": {"a": 0.0, "b": 0.0}},
                    strategy_sum={"known": {"a": 0.0, "b": 0.0}},
                )
                getattr(state, table_name)["orphan"] = {"a": 0.0}
                with self.assertRaisesRegex(ValueError, "absent from actions"):
                    state.validate()


if __name__ == "__main__":
    unittest.main()
