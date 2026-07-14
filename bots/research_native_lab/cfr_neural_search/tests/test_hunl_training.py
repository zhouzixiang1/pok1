from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bots.research_native_lab.cfr_neural_search.blueprint.hunl_abstraction import (
    HUNLAbstractionConfig,
)
from bots.research_native_lab.cfr_neural_search.blueprint.hunl_game import (
    HUNLTrainingGame,
)
from bots.research_native_lab.cfr_neural_search.blueprint.hunl_training import (
    apply_hunl_shards,
    build_independent_hunl_shards,
    load_hunl_checkpoint,
    load_hunl_checkpoint_with_digest,
    save_hunl_checkpoint,
)
from bots.research_native_lab.cfr_neural_search.blueprint.mccfr import (
    SolverConfig,
    SolverState,
)
from bots.research_native_lab.cfr_neural_search.core.identity import payload_sha256
from bots.research_native_lab.cfr_neural_search.core.strict_io import (
    atomic_json_write,
    load_hashed_json,
)


def _run_contract(target_batches: int) -> dict[str, object]:
    return {
        "schema": "route-b-test-formal-run-v1",
        "target_batches": target_batches,
        "pinned_config_sha256": "1" * 64,
        "cli_source_sha256": "2" * 64,
        "source_snapshot_sha256": "3" * 64,
    }


class HUNLIndependentTrainingTest(unittest.TestCase):
    def test_true_hunl_one_vs_n_and_resume_use_the_same_frozen_deltas(self):
        game = HUNLTrainingGame(HUNLAbstractionConfig(equity_samples=1))
        config = SolverConfig(
            seed=2026071401,
            samples_per_player=2,
        )
        base = SolverState.new_for_game(game, config)
        one = SolverState.from_payload(base.to_payload())
        many = SolverState.from_payload(base.to_payload())
        run_contract = _run_contract(2)

        one_shards = build_independent_hunl_shards(
            game,
            one,
            1,
            max_workers=1,
            run_contract=run_contract,
        )
        many_shards = build_independent_hunl_shards(
            game,
            many,
            2,
            max_workers=2,
            run_contract=run_contract,
        )
        apply_hunl_shards(game, one, one_shards, run_contract=run_contract)
        apply_hunl_shards(game, many, many_shards, run_contract=run_contract)
        self.assertEqual(one.to_payload(), many.to_payload())
        self.assertEqual(one.digest, many.digest)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = root / "checkpoint.json"
            saved_digest = save_hunl_checkpoint(
                checkpoint,
                game,
                many,
                root=root,
                run_contract=run_contract,
            )
            resumed, loaded_digest = load_hunl_checkpoint_with_digest(
                checkpoint,
                game,
                root=root,
                run_contract=run_contract,
            )
            self.assertEqual(saved_digest, loaded_digest)

            # Build batch 2 once from the common frozen digest, then apply the
            # immutable envelopes to uninterrupted and resumed copies.
            second_batch = build_independent_hunl_shards(
                game,
                many,
                2,
                max_workers=2,
                run_contract=run_contract,
            )
            apply_hunl_shards(
                game,
                many,
                second_batch,
                run_contract=run_contract,
            )
            apply_hunl_shards(
                game,
                resumed,
                second_batch,
                run_contract=run_contract,
            )
            self.assertEqual(many.to_payload(), resumed.to_payload())
            self.assertEqual(many.digest, resumed.digest)

    def test_rehashed_checkpoint_cannot_change_bound_target_contract(self):
        game = HUNLTrainingGame(HUNLAbstractionConfig(equity_samples=1))
        state = SolverState.new_for_game(
            game,
            SolverConfig(seed=2026071401, samples_per_player=1),
        )
        original_contract = _run_contract(8)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = root / "checkpoint.json"
            save_hunl_checkpoint(
                checkpoint,
                game,
                state,
                root=root,
                run_contract=original_contract,
            )
            payload = dict(load_hashed_json(checkpoint, root=root))
            nested_contract = dict(payload["contract"])
            nested_run = dict(nested_contract["run_contract"])
            nested_run["target_batches"] = 16
            nested_contract["run_contract"] = nested_run
            # Rehash only the outer file: the independently bound nested run
            # and training-contract digests must still reject the edit.
            payload["contract"] = nested_contract
            atomic_json_write(checkpoint, payload, root=root)
            with self.assertRaises(ValueError):
                load_hunl_checkpoint(
                    checkpoint,
                    game,
                    root=root,
                    run_contract=_run_contract(16),
                )
            self.assertNotEqual(payload_sha256(original_contract), payload_sha256(nested_run))


if __name__ == "__main__":
    unittest.main()
