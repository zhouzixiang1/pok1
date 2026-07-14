from __future__ import annotations

import copy
import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from bots.research_native_lab.cfr_neural_search.core.identity import file_sha256
from bots.research_native_lab.cfr_neural_search.core import run_journal as journal_module
from bots.research_native_lab.cfr_neural_search.core.run_journal import (
    DurableRunJournal,
    load_event_log,
)
from bots.research_native_lab.cfr_neural_search.core.selector_invalidation import (
    assert_workspace_not_invalidated,
    invalidation_registry_snapshot,
)
from bots.research_native_lab.cfr_neural_search.core.strict_io import (
    atomic_json_write,
    load_hashed_json,
)
from bots.research_native_lab.cfr_neural_search.tools import select_hunl_scale as selector
from bots.research_native_lab.cfr_neural_search.tools import (
    train_hunl_blueprint as training_module,
)
from bots.research_native_lab.cfr_neural_search.tools.invalidate_selector_run import (
    invalidate_selector_run,
)
from bots.research_native_lab.cfr_neural_search.tools.train_hunl_blueprint import (
    RUNTIME_ROOT_NAME,
    _route_root,
)


def _identity() -> dict[str, str]:
    return {
        "run_contract_sha256": "1" * 64,
        "source_snapshot_sha256": "2" * 64,
        "config_payload_sha256": "3" * 64,
        "config_file_sha256": "4" * 64,
    }


def _uniform_compiled(_game, state, *, run_contract):
    del _game, run_contract
    return {
        "statistics": {
            "materially_nonuniform_all_rows": 0,
            "materially_nonuniform_exact_rows": 0,
            "materially_nonuniform_backoff_rows": 0,
            "max_l1_from_uniform": 0.0,
            "exact_row_count": 0,
            "backoff_row_count": 0,
        },
        "resources": {
            "training_trajectories": state.trajectories,
            "training_node_touches": state.node_touches,
            "solver_information_rows": len(state.actions),
        },
    }


def _validated_pending_discovery_fixture() -> tuple[dict, dict]:
    """Derive and strictly validate an isolated pre-freeze control-plane fixture."""

    route = _route_root()
    live_frozen = selector._load_config(route, require_frozen=True)
    if (
        live_frozen["scale_gate"]["selection_status"] != "frozen_first_pass"
        or live_frozen["scale_gate"]["frozen_selected_batches"] != 32
    ):
        raise AssertionError("selector journal tests require the live frozen M4 config")
    pending = copy.deepcopy(live_frozen)
    scale = pending["scale_gate"]
    pending["training"]["batches"] = scale["candidate_batches"][-1]
    scale["selection_status"] = "pending_discovery"
    scale["frozen_observations"] = []
    scale["frozen_selected_batches"] = None

    # Exercise the production parser/schema on a separate exact-path fixture.
    # The repository-pinned config itself is never edited or replaced.
    with tempfile.TemporaryDirectory() as directory:
        fixture_route = Path(directory)
        fixture_config = fixture_route / "configs/hunl_m4_blueprint.json"
        fixture_config.parent.mkdir()
        fixture_config.write_text(
            json.dumps(
                pending,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ),
            encoding="utf-8",
        )
        with mock.patch.object(training_module, "DEFAULT_CONFIG", fixture_config):
            validated = training_module._load_config(
                fixture_route,
                require_frozen=False,
            )
    if validated != pending:
        raise AssertionError("strict pending fixture validation changed its payload")
    return live_frozen, pending


@contextmanager
def _isolated_pending_discovery_loader():
    """Patch only the config return value after rechecking the real pinned file."""

    live_frozen, pending = _validated_pending_discovery_fixture()
    real_loader = selector._load_config

    def load_for_control_test(route: Path, *, require_frozen: bool = True):
        current = real_loader(route, require_frozen=True)
        if current != live_frozen:
            raise AssertionError("live frozen config changed during control-plane test")
        if require_frozen:
            return current
        return copy.deepcopy(pending)

    with mock.patch.object(
        selector,
        "_load_config",
        side_effect=load_for_control_test,
    ):
        yield


class DurableRunJournalTest(unittest.TestCase):
    def test_event_chain_and_atomic_heartbeat_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = DurableRunJournal.open(root, _identity(), resume=False)
            first = journal.append(
                "selector_started",
                completed_batches=0,
                checkpoint_sha256=None,
            )
            journal.heartbeat(
                "started",
                detail="selector_started",
                completed_batches=0,
                checkpoint_sha256=None,
            )
            second = journal.append(
                "batch_completed",
                completed_batches=1,
                checkpoint_sha256="a" * 64,
            )
            journal.heartbeat(
                "running",
                detail="batch_completed",
                completed_batches=1,
                checkpoint_sha256="a" * 64,
            )
            self.assertEqual(second["previous_event_sha256"], first["event_sha256"])
            heartbeat_path = root / "heartbeat.json"
            original_heartbeat = dict(load_hashed_json(heartbeat_path, root=root))
            for field, value in (
                ("completed_batches", 2),
                ("checkpoint_sha256", "b" * 64),
            ):
                forged = dict(original_heartbeat)
                forged[field] = value
                atomic_json_write(heartbeat_path, forged, root=root)
                with self.subTest(field=field), self.assertRaisesRegex(
                    ValueError,
                    "durable pair",
                ):
                    DurableRunJournal.open(root, _identity(), resume=True)
            atomic_json_write(heartbeat_path, original_heartbeat, root=root)

            journal.append(
                "event_after_older_heartbeat",
                completed_batches=1,
                checkpoint_sha256="a" * 64,
            )
            resumed = DurableRunJournal.open(root, _identity(), resume=True)
            self.assertEqual(len(resumed.events), 3)
            self.assertEqual(resumed.previous_heartbeat["completed_batches"], 1)
            self.assertEqual(resumed.previous_heartbeat["last_event_sequence"], 1)
            self.assertEqual(resumed.last_event["sequence"], 2)
            heartbeat_path.unlink()
            missing_heartbeat = DurableRunJournal.open(
                root,
                _identity(),
                resume=True,
            )
            self.assertIsNone(missing_heartbeat.previous_heartbeat)
            self.assertEqual(missing_heartbeat.last_event["sequence"], 2)
            atomic_json_write(heartbeat_path, original_heartbeat, root=root)
            events = load_event_log(
                root / "events",
                root=root,
                run_identity=_identity(),
            )
            self.assertEqual([event["sequence"] for event in events], [0, 1, 2])

            partial = root / "events" / (
                ".000000000003.json." + "a" * 32 + ".tmp"
            )
            partial.write_bytes(b'{"partial":')
            recovered = DurableRunJournal.open(root, _identity(), resume=True)
            self.assertEqual(
                recovered.orphaned_event_temps,
                {partial.name: file_sha256(partial)},
            )
            third = recovered.append(
                "selector_resume_started",
                completed_batches=1,
                checkpoint_sha256="a" * 64,
                details={
                    "orphaned_event_temps": recovered.orphaned_event_temps,
                },
            )
            self.assertEqual(
                third["details"]["orphaned_event_temps"][partial.name],
                file_sha256(partial),
            )
            self.assertTrue(partial.is_file())


class SelectorControlIntegrationTest(unittest.TestCase):
    @staticmethod
    def _run_pending_selection(
        workspace: Path,
        *,
        resume: bool,
        max_new_batches: int | None = None,
    ):
        with _isolated_pending_discovery_loader():
            return selector.run_selection(
                workspace,
                resume=resume,
                discover=True,
                max_new_batches=max_new_batches,
            )

    @staticmethod
    def _fake_training_patches():
        def fake_build(*args, **kwargs):
            del args, kwargs
            return (object(),)

        def fake_apply(_game, state, _shards, *, run_contract):
            del _game, _shards, run_contract
            state.batch_index += 1
            state.trajectories += 2
            state.node_touches += 10
            state.validate()
            return state

        return (
            mock.patch.object(selector, "build_independent_hunl_shards", fake_build),
            mock.patch.object(selector, "apply_hunl_shards", fake_apply),
            mock.patch.object(selector, "compile_blueprint_payload", _uniform_compiled),
        )

    def _prepare_one_batch(self, workspace: Path) -> None:
        patches = self._fake_training_patches()
        with patches[0], patches[1], patches[2]:
            result = self._run_pending_selection(
                workspace,
                resume=False,
                max_new_batches=1,
            )
        self.assertEqual(result["status"], "paused_after_checkpoint")
        heartbeat = load_hashed_json(workspace / "heartbeat.json", root=workspace)
        self.assertEqual(heartbeat["completed_batches"], 1)

    def test_live_frozen_config_rejects_discovery(self):
        route = _route_root()
        config = selector._load_config(route, require_frozen=True)
        self.assertEqual(
            config["scale_gate"]["selection_status"],
            "frozen_first_pass",
        )
        self.assertEqual(config["scale_gate"]["frozen_selected_batches"], 32)
        runtime = route / RUNTIME_ROOT_NAME
        with tempfile.TemporaryDirectory(dir=runtime) as directory:
            workspace = Path(directory)
            with self.assertRaisesRegex(
                ValueError,
                "--discover requires the explicitly pending config",
            ):
                selector.run_selection(
                    workspace,
                    resume=False,
                    discover=True,
                )
            self.assertEqual(list(workspace.iterdir()), [])

    def test_cancel_boundary_resume_and_content_derived_invalidation(self):
        runtime = _route_root() / RUNTIME_ROOT_NAME
        with tempfile.TemporaryDirectory(dir=runtime) as directory:
            workspace = Path(directory)
            create_cancel = True

            def fake_build(*args, **kwargs):
                del args, kwargs
                return (object(),)

            def fake_apply(_game, state, _shards, *, run_contract):
                nonlocal create_cancel
                del _game, _shards, run_contract
                state.batch_index += 1
                state.trajectories += 2
                state.node_touches += 10
                state.validate()
                if create_cancel and state.batch_index == 1:
                    (workspace / "CANCEL").write_text("test-boundary\n", encoding="ascii")
                return state

            patches = (
                mock.patch.object(selector, "build_independent_hunl_shards", fake_build),
                mock.patch.object(selector, "apply_hunl_shards", fake_apply),
                mock.patch.object(selector, "compile_blueprint_payload", _uniform_compiled),
            )
            with patches[0], patches[1], patches[2]:
                first = self._run_pending_selection(
                    workspace,
                    resume=False,
                )
            self.assertEqual(first["status"], "cancelled_at_batch_boundary")
            heartbeat = load_hashed_json(workspace / "heartbeat.json", root=workspace)
            self.assertEqual(heartbeat["status"], "cancelled")
            self.assertEqual(heartbeat["completed_batches"], 1)
            events = load_event_log(
                workspace / "events",
                root=workspace,
                run_identity=heartbeat["run_identity"],
            )
            self.assertIn("batch_completed", [event["event"] for event in events])
            self.assertEqual(events[-1]["event"], "selector_cancelled")

            (workspace / "CANCEL").unlink()
            partial = workspace / "events" / (
                f".{len(events):012d}.json." + "b" * 32 + ".tmp"
            )
            partial.write_bytes(b'{"host_killed_mid_event":')
            partial_sha256 = file_sha256(partial)
            create_cancel = False
            with patches[0], patches[1], patches[2]:
                second = self._run_pending_selection(
                    workspace,
                    resume=True,
                    max_new_batches=1,
                )
            self.assertEqual(second["status"], "paused_after_checkpoint")
            self.assertEqual([row["batches"] for row in second["observations"]], [2])
            heartbeat = load_hashed_json(workspace / "heartbeat.json", root=workspace)
            self.assertEqual(heartbeat["status"], "cancelled")
            self.assertEqual(heartbeat["detail"], "paused_after_checkpoint")
            self.assertEqual(heartbeat["completed_batches"], 2)
            events = load_event_log(
                workspace / "events",
                root=workspace,
                run_identity=heartbeat["run_identity"],
            )
            names = [event["event"] for event in events]
            self.assertIn("selector_resume_started", names)
            self.assertIn("resume_replay_batch", names)
            self.assertIn("candidate_observed", names)
            resume_event = next(
                event for event in events if event["event"] == "selector_resume_started"
            )
            self.assertEqual(
                resume_event["details"]["orphaned_event_temps"],
                {partial.name: partial_sha256},
            )
            self.assertEqual(file_sha256(partial), partial_sha256)

            with tempfile.TemporaryDirectory() as registry_directory:
                registry = Path(registry_directory)
                marker = invalidate_selector_run(
                    workspace,
                    "unit_test_invalidation",
                    registry_root=registry,
                )
                self.assertEqual(marker["last_complete_batch"], 2)
                self.assertEqual(marker["last_observed_candidate"], 2)
                self.assertEqual(
                    marker["checkpoint"]["file_sha256"],
                    file_sha256(workspace / "selector_checkpoint.json"),
                )
                stored_marker = load_hashed_json(
                    workspace / "INVALIDATED.json",
                    root=workspace,
                )
                self.assertEqual(stored_marker, marker)
                self.assertEqual(
                    invalidate_selector_run(
                        workspace,
                        "unit_test_invalidation",
                        registry_root=registry,
                    ),
                    marker,
                )
                snapshot = invalidation_registry_snapshot(
                    _route_root(),
                    registry_root=registry,
                )
                self.assertEqual(snapshot["file_count"], 1)
                with self.assertRaisesRegex(ValueError, "invalidated"):
                    self._run_pending_selection(
                        workspace,
                        resume=True,
                        max_new_batches=0,
                    )
                (workspace / "INVALIDATED.json").unlink()
                with self.assertRaisesRegex(ValueError, "permanent registry"):
                    assert_workspace_not_invalidated(
                        _route_root(),
                        workspace,
                        registry_root=registry,
                    )

    def test_exception_updates_failed_event_and_heartbeat(self):
        runtime = _route_root() / RUNTIME_ROOT_NAME
        with tempfile.TemporaryDirectory(dir=runtime) as directory:
            workspace = Path(directory)
            with mock.patch.object(
                selector,
                "build_independent_hunl_shards",
                side_effect=RuntimeError("synthetic worker failure"),
            ):
                with self.assertRaisesRegex(RuntimeError, "synthetic worker failure"):
                    self._run_pending_selection(
                        workspace,
                        resume=False,
                    )
            heartbeat = load_hashed_json(workspace / "heartbeat.json", root=workspace)
            self.assertEqual(heartbeat["status"], "failed")
            events = load_event_log(
                workspace / "events",
                root=workspace,
                run_identity=heartbeat["run_identity"],
            )
            self.assertEqual(events[-1]["event"], "selector_failed")
            self.assertEqual(
                events[-1]["details"]["exception_type"],
                "RuntimeError",
            )

    def test_faults_never_advance_heartbeat_beyond_durable_checkpoint(self):
        runtime = _route_root() / RUNTIME_ROOT_NAME
        real_save = selector.save_hunl_checkpoint
        real_heartbeat = DurableRunJournal.heartbeat
        for scenario in ("before_save", "after_save", "after_event_before_heartbeat"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory(
                dir=runtime
            ) as directory:
                workspace = Path(directory)
                self._prepare_one_batch(workspace)
                patches = self._fake_training_patches()

                if scenario == "before_save":
                    fault = mock.patch.object(
                        selector,
                        "save_hunl_checkpoint",
                        side_effect=RuntimeError("injected before save"),
                    )
                elif scenario == "after_save":
                    def save_then_fail(*args, **kwargs):
                        real_save(*args, **kwargs)
                        raise RuntimeError("injected after atomic save")

                    fault = mock.patch.object(
                        selector,
                        "save_hunl_checkpoint",
                        side_effect=save_then_fail,
                    )
                else:
                    fired = False

                    def heartbeat_then_fail_once(self, status, **kwargs):
                        nonlocal fired
                        if (
                            not fired
                            and status == "running"
                            and kwargs.get("detail") == "batch_completed"
                        ):
                            fired = True
                            raise RuntimeError("injected after event before heartbeat")
                        return real_heartbeat(self, status, **kwargs)

                    fault = mock.patch.object(
                        DurableRunJournal,
                        "heartbeat",
                        heartbeat_then_fail_once,
                    )

                with patches[0], patches[1], patches[2], fault:
                    with self.assertRaisesRegex(RuntimeError, "injected"):
                        self._run_pending_selection(
                            workspace,
                            resume=True,
                            max_new_batches=1,
                        )

                checkpoint = load_hashed_json(
                    workspace / "selector_checkpoint.json",
                    root=workspace,
                )
                durable_checkpoint_batch = checkpoint["solver"]["batch_index"]
                heartbeat = load_hashed_json(
                    workspace / "heartbeat.json",
                    root=workspace,
                )
                self.assertLessEqual(
                    heartbeat["completed_batches"],
                    durable_checkpoint_batch,
                )
                if scenario in {"before_save", "after_save"}:
                    self.assertEqual(heartbeat["completed_batches"], 1)
                else:
                    self.assertEqual(heartbeat["completed_batches"], 2)
                events = load_event_log(
                    workspace / "events",
                    root=workspace,
                    run_identity=heartbeat["run_identity"],
                )
                self.assertEqual(events[-1]["event"], "selector_failed")
                self.assertEqual(
                    events[-1]["details"]["durable_batch_index"],
                    heartbeat["completed_batches"],
                )
                self.assertEqual(events[-1]["details"]["in_memory_batch_index"], 2)

                patches = self._fake_training_patches()
                with patches[0], patches[1], patches[2]:
                    recovered = self._run_pending_selection(
                        workspace,
                        resume=True,
                        max_new_batches=1,
                    )
                self.assertEqual(recovered["status"], "paused_after_checkpoint")
                recovered_heartbeat = load_hashed_json(
                    workspace / "heartbeat.json",
                    root=workspace,
                )
                self.assertGreaterEqual(
                    recovered_heartbeat["completed_batches"],
                    durable_checkpoint_batch,
                )
                recovered_events = load_event_log(
                    workspace / "events",
                    root=workspace,
                    run_identity=recovered_heartbeat["run_identity"],
                )
                if scenario == "after_save":
                    self.assertIn(
                        "batch_checkpoint_recovered",
                        [event["event"] for event in recovered_events],
                    )

    def test_event_publish_and_failure_event_can_both_fail_then_resume_checkpoint(self):
        runtime = _route_root() / RUNTIME_ROOT_NAME
        with tempfile.TemporaryDirectory(dir=runtime) as directory:
            workspace = Path(directory)
            self._prepare_one_batch(workspace)
            real_create = journal_module.atomic_json_create
            partials: list[Path] = []

            def fail_two_authoritative_events(path, payload, *, root):
                if payload["event"] in {"batch_completed", "selector_failed"}:
                    target = Path(path)
                    token = ("c" if not partials else "d") * 32
                    partial = target.parent / (
                        f".{target.name}." + token + ".tmp"
                    )
                    partial.write_bytes(
                        b'{"killed_during_authoritative_event":',
                    )
                    partials.append(partial)
                    raise RuntimeError(f"injected atomic create {payload['event']}")
                return real_create(path, payload, root=root)

            patches = self._fake_training_patches()
            with (
                patches[0],
                patches[1],
                patches[2],
                mock.patch.object(
                    journal_module,
                    "atomic_json_create",
                    side_effect=fail_two_authoritative_events,
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "batch_completed"):
                    self._run_pending_selection(
                        workspace,
                        resume=True,
                        max_new_batches=1,
                    )
            self.assertEqual(len(partials), 2)
            self.assertTrue(all(path.is_file() for path in partials))
            checkpoint = load_hashed_json(
                workspace / "selector_checkpoint.json",
                root=workspace,
            )
            self.assertEqual(checkpoint["solver"]["batch_index"], 2)
            heartbeat = load_hashed_json(workspace / "heartbeat.json", root=workspace)
            self.assertEqual(heartbeat["completed_batches"], 1)
            events = load_event_log(
                workspace / "events",
                root=workspace,
                run_identity=heartbeat["run_identity"],
            )
            self.assertFalse(
                any(
                    event["completed_batches"] == 2
                    and event["event"] in {"batch_completed", "selector_failed"}
                    for event in events
                )
            )

            patches = self._fake_training_patches()
            with patches[0], patches[1], patches[2]:
                recovered = self._run_pending_selection(
                    workspace,
                    resume=True,
                    max_new_batches=0,
                )
            self.assertEqual(recovered["status"], "paused_after_checkpoint")
            recovered_heartbeat = load_hashed_json(
                workspace / "heartbeat.json",
                root=workspace,
            )
            recovered_events = load_event_log(
                workspace / "events",
                root=workspace,
                run_identity=recovered_heartbeat["run_identity"],
            )
            self.assertIn(
                "batch_checkpoint_recovered",
                [event["event"] for event in recovered_events],
            )
            resume_event = [
                event
                for event in recovered_events
                if event["event"] == "selector_resume_started"
            ][-1]
            orphaned = resume_event["details"]["orphaned_event_temps"]
            self.assertEqual(set(orphaned), {path.name for path in partials})
            for partial in partials:
                self.assertEqual(orphaned[partial.name], file_sha256(partial))


if __name__ == "__main__":
    unittest.main()
