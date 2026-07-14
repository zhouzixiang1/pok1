from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bots.research_native_lab.cfr_neural_search.blueprint.hunl_abstraction import (
    HUNLAbstractionConfig,
)
from bots.research_native_lab.cfr_neural_search.blueprint.hunl_game import (
    HUNLTrainingGame,
)
from bots.research_native_lab.cfr_neural_search.blueprint.hunl_training import (
    save_hunl_checkpoint,
)
from bots.research_native_lab.cfr_neural_search.blueprint.artifact import (
    compile_blueprint_payload,
)
from bots.research_native_lab.cfr_neural_search.blueprint.mccfr import (
    SolverConfig,
    SolverState,
)
from bots.research_native_lab.cfr_neural_search.core.identity import (
    file_sha256,
    payload_sha256,
)
from bots.research_native_lab.cfr_neural_search.core.run_journal import DurableRunJournal
from bots.research_native_lab.cfr_neural_search.core.strict_io import strict_json_read
from bots.research_native_lab.cfr_neural_search.core.strict_io import atomic_json_write
from bots.research_native_lab.cfr_neural_search.tools.select_hunl_scale import (
    _load_existing_trace,
    _selection_run_contract,
    _trace_payload,
    run_selection,
)
from bots.research_native_lab.cfr_neural_search.tools.train_hunl_blueprint import (
    DEFAULT_CONFIG,
    RUNTIME_ROOT_NAME,
    _formal_run_contract,
    _load_config,
    _route_root,
    build_scale_observation,
    capture_source_snapshot,
    require_frozen_final_observation,
)


class HUNLCLIContractTest(unittest.TestCase):
    def test_formal_final_observation_crosscheck_binds_every_field(self):
        game = HUNLTrainingGame(HUNLAbstractionConfig(equity_samples=1))
        state = SolverState.new_for_game(
            game,
            SolverConfig(seed=2026071401, samples_per_player=1),
        )
        state.batch_index = 2
        state.trajectories = 4
        state.node_touches = 10
        compiled = compile_blueprint_payload(game, state)
        frozen = build_scale_observation(state, compiled, threshold=1)
        self.assertEqual(
            require_frozen_final_observation(state, compiled, frozen, 1),
            frozen,
        )
        for field, value in (
            ("batches", 1),
            ("passed", True),
            ("materially_nonuniform_all_rows", 1),
            ("materially_nonuniform_exact_rows", 1),
            ("materially_nonuniform_backoff_rows", 1),
            ("max_l1_from_uniform", 0.25),
            ("exact_row_count", 1),
            ("backoff_row_count", 1),
            ("training_trajectories", 1),
            ("training_node_touches", 1),
            ("solver_information_rows", 1),
            ("solver_sha256", "f" * 64),
        ):
            drifted = dict(frozen)
            drifted[field] = value
            with self.subTest(field=field), self.assertRaises((ValueError, RuntimeError)):
                require_frozen_final_observation(state, compiled, drifted, 1)

    def test_formal_run_contract_binds_full_config_target_cli_and_source_snapshot(self):
        route = _route_root()
        config = _load_config(route, require_frozen=False)
        snapshot = capture_source_snapshot(route)
        contract = _formal_run_contract(config, snapshot)
        self.assertEqual(contract["pinned_config"], config)
        self.assertEqual(contract["target_batches"], config["training"]["batches"])
        self.assertEqual(contract["source_snapshot"], snapshot.to_payload())
        self.assertEqual(len(contract["cli_source_sha256"]), 64)
        self.assertIn("excluded_route_paths", contract["source_snapshot"])

    def test_config_rejects_bool_and_float_aliases_for_every_resource_integer(self):
        route = _route_root()
        original = strict_json_read(DEFAULT_CONFIG, root=route)
        mutations = (
            ("batches", True),
            ("shard_count", 1.0),
            ("max_workers", False),
            ("checkpoint_every_complete_batches", 1.0),
        )
        for field, value in mutations:
            payload = copy.deepcopy(original)
            payload["training"][field] = value
            with self.subTest(field=field), mock.patch(
                "bots.research_native_lab.cfr_neural_search.tools."
                "train_hunl_blueprint.strict_json_read",
                return_value=payload,
            ):
                with self.assertRaises((TypeError, ValueError)):
                    _load_config(route, require_frozen=False)
        for field in (
            "correctness_gate_max_batches",
            "correctness_gate_max_samples_per_player",
        ):
            payload = copy.deepcopy(original)
            payload["scale_gate"][field] = True
            with self.subTest(field=field), mock.patch(
                "bots.research_native_lab.cfr_neural_search.tools."
                "train_hunl_blueprint.strict_json_read",
                return_value=payload,
            ):
                with self.assertRaises((TypeError, ValueError)):
                    _load_config(route, require_frozen=False)

    def test_selector_resume_rejects_checkpoint_that_skips_candidate_two(self):
        route = _route_root()
        config = _load_config(route, require_frozen=False)
        snapshot = capture_source_snapshot(route)
        run_contract = _selection_run_contract(config, snapshot)
        training = config["training"]
        game = HUNLTrainingGame(HUNLAbstractionConfig(**training["abstraction"]))
        state = SolverState.new_for_game(
            game,
            SolverConfig.from_payload(training["solver"]),
        )
        state.batch_index = 3
        runtime_root = route / RUNTIME_ROOT_NAME
        with tempfile.TemporaryDirectory(dir=runtime_root) as directory:
            workspace = Path(directory)
            checkpoint_sha256 = save_hunl_checkpoint(
                workspace / "selector_checkpoint.json",
                game,
                state,
                root=workspace,
                run_contract=run_contract,
            )
            journal = DurableRunJournal.open(
                workspace,
                {
                    "run_contract_sha256": payload_sha256(run_contract),
                    "source_snapshot_sha256": snapshot.digest,
                    "config_payload_sha256": payload_sha256(config),
                    "config_file_sha256": file_sha256(DEFAULT_CONFIG),
                },
                resume=False,
            )
            journal.append(
                "test_checkpoint_created",
                completed_batches=3,
                checkpoint_sha256=checkpoint_sha256,
            )
            journal.heartbeat(
                "cancelled",
                detail="test_checkpoint_created",
                completed_batches=3,
                checkpoint_sha256=checkpoint_sha256,
            )
            discover = config["scale_gate"]["selection_status"] == "pending_discovery"
            with self.assertRaisesRegex(ValueError, "exact prior prefix"):
                run_selection(workspace, resume=True, discover=discover)

    def test_selector_trace_rejects_duplicate_out_of_order_and_extra_batches(self):
        route = _route_root()
        config = _load_config(route, require_frozen=False)
        snapshot = capture_source_snapshot(route)
        run_contract = _selection_run_contract(config, snapshot)

        def row(batch: int) -> dict[str, object]:
            return {
                "batches": batch,
                "passed": False,
                "materially_nonuniform_all_rows": 0,
                "materially_nonuniform_exact_rows": 0,
                "materially_nonuniform_backoff_rows": 0,
                "max_l1_from_uniform": 0.0,
                "exact_row_count": 1,
                "backoff_row_count": 1,
                "training_trajectories": batch * 2,
                "training_node_touches": batch,
                "solver_information_rows": 1,
                "solver_sha256": f"{batch % 10}" * 64,
            }

        mutations = (
            [row(2), row(2)],
            [row(4), row(2)],
            [row(2), row(3)],
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "selection.json"
            for index, observations in enumerate(mutations):
                with self.subTest(index=index):
                    payload = _trace_payload(
                        status="candidate_failed",
                        config=config,
                        run_contract=run_contract,
                        source_snapshot=snapshot,
                        observations=observations,
                        checkpoint_sha256="a" * 64,
                    )
                    atomic_json_write(path, payload, root=root)
                    with self.assertRaisesRegex(ValueError, "ordered candidate prefix"):
                        _load_existing_trace(
                            path,
                            root,
                            config=config,
                            run_contract=run_contract,
                            source_snapshot_sha256=snapshot.digest,
                            checkpoint_sha256="a" * 64,
                        )

    def test_selector_trace_status_and_selected_are_derived_from_observations(self):
        route = _route_root()
        config = _load_config(route, require_frozen=False)
        snapshot = capture_source_snapshot(route)
        run_contract = _selection_run_contract(config, snapshot)
        passing = {
            "batches": 2,
            "passed": True,
            "materially_nonuniform_all_rows": 1,
            "materially_nonuniform_exact_rows": 1,
            "materially_nonuniform_backoff_rows": 0,
            "max_l1_from_uniform": 0.5,
            "exact_row_count": 1,
            "backoff_row_count": 1,
            "training_trajectories": 4,
            "training_node_touches": 10,
            "solver_information_rows": 1,
            "solver_sha256": "4" * 64,
        }
        payload = _trace_payload(
            status="complete",
            config=config,
            run_contract=run_contract,
            source_snapshot=snapshot,
            observations=[passing],
            checkpoint_sha256="b" * 64,
        )
        payload["selected_batches"] = None
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "selection.json"
            atomic_json_write(path, payload, root=root)
            with self.assertRaisesRegex(ValueError, "not derived"):
                _load_existing_trace(
                    path,
                    root,
                    config=config,
                    run_contract=run_contract,
                    source_snapshot_sha256=snapshot.digest,
                    checkpoint_sha256="b" * 64,
                )

    def test_selector_trace_checkpoint_digest_must_match_loaded_checkpoint(self):
        route = _route_root()
        config = _load_config(route, require_frozen=False)
        snapshot = capture_source_snapshot(route)
        run_contract = _selection_run_contract(config, snapshot)
        payload = _trace_payload(
            status="paused_after_checkpoint",
            config=config,
            run_contract=run_contract,
            source_snapshot=snapshot,
            observations=[],
            checkpoint_sha256="c" * 64,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "selection.json"
            atomic_json_write(path, payload, root=root)
            with self.assertRaisesRegex(ValueError, "differs from checkpoint file"):
                _load_existing_trace(
                    path,
                    root,
                    config=config,
                    run_contract=run_contract,
                    source_snapshot_sha256=snapshot.digest,
                    checkpoint_sha256="d" * 64,
                )


if __name__ == "__main__":
    unittest.main()
