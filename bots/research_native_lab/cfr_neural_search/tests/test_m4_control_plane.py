from __future__ import annotations

import multiprocessing
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from bots.research_native_lab.cfr_neural_search.core import run_journal as journal_module
from bots.research_native_lab.cfr_neural_search.core.identity import (
    file_sha256,
    payload_sha256,
)
from bots.research_native_lab.cfr_neural_search.core.run_journal import (
    DurableRunJournal,
    load_event_log,
)
from bots.research_native_lab.cfr_neural_search.core.strict_io import atomic_json_write
from bots.research_native_lab.cfr_neural_search.tools import verify_m4_gate as gate
from bots.research_native_lab.cfr_neural_search.tools import (
    invalidate_selector_run as invalidator,
)
from bots.research_native_lab.cfr_neural_search.tools.train_hunl_blueprint import (
    RUNTIME_ROOT_NAME,
    _route_root,
    run_training,
)


def _verify_manifest_in_external_process(
    begin: multiprocessing.synchronize.Event,
    attempted: multiprocessing.synchronize.Event,
    completed: multiprocessing.synchronize.Event,
    result_queue: multiprocessing.queues.Queue,
) -> None:
    """Run the public verifier after the parent exposes a locked write window."""

    if not begin.wait(5.0):
        result_queue.put(("error", "timed out waiting for verifier start"))
        completed.set()
        return
    attempted.set()
    try:
        result_queue.put(("ok", gate.verify_m4_manifest()))
    except BaseException as exc:  # pragma: no cover - asserted in parent process
        result_queue.put(("error", f"{type(exc).__name__}: {exc}"))
    finally:
        completed.set()


class PublishedSelectorJournalTest(unittest.TestCase):
    def _completed_journal(self, root: Path):
        route = _route_root()
        config = {
            "schema": "unit-selector-config",
            "scale_gate": {"frozen_selected_batches": 2},
        }
        snapshot = SimpleNamespace(digest="2" * 64)
        selection = {
            "run_contract_sha256": "1" * 64,
            "checkpoint_sha256": "a" * 64,
        }
        identity = {
            "run_contract_sha256": selection["run_contract_sha256"],
            "source_snapshot_sha256": snapshot.digest,
            "config_payload_sha256": payload_sha256(config),
            "config_file_sha256": file_sha256(
                route / "configs" / "hunl_m4_blueprint.json"
            ),
        }
        journal = DurableRunJournal.open(root, identity, resume=False)
        journal.append(
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
        journal.append(
            "selector_completed",
            completed_batches=2,
            checkpoint_sha256=selection["checkpoint_sha256"],
        )
        journal.heartbeat(
            "completed",
            detail="first_passing_candidate_completed",
            completed_batches=2,
            checkpoint_sha256=selection["checkpoint_sha256"],
        )
        return route, config, snapshot, selection, journal

    def _validate(self, root: Path, route, config, snapshot, selection):
        return gate._selector_journal_evidence(
            route=route,
            config=config,
            snapshot=snapshot,
            selection=selection,
            root=root,
            event_directory=root / "events",
            events_path=root / "events.jsonl",
            heartbeat_path=root / "heartbeat.json",
        )

    def test_completed_heartbeat_must_be_exact_authoritative_tip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            route, config, snapshot, selection, journal = self._completed_journal(root)
            events, heartbeat, manifest = self._validate(
                root,
                route,
                config,
                snapshot,
                selection,
            )
            self.assertEqual(heartbeat["last_event_sequence"], events[-1]["sequence"])
            self.assertEqual(len(manifest), len(events))

            # A lagging heartbeat is valid for runtime recovery, but never for
            # a formal completed publication.
            journal.append(
                "selector_completed",
                completed_batches=2,
                checkpoint_sha256=selection["checkpoint_sha256"],
            )
            resumed = DurableRunJournal.open(root, journal.run_identity, resume=True)
            self.assertLess(
                resumed.previous_heartbeat["last_event_sequence"],
                resumed.last_event["sequence"],
            )
            with self.assertRaisesRegex(ValueError, "completed frozen run"):
                self._validate(root, route, config, snapshot, selection)

    def test_event_created_before_export_crash_is_not_publishable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            route, config, snapshot, selection, journal = self._completed_journal(root)
            with mock.patch.object(
                journal_module,
                "atomic_write_bytes",
                side_effect=RuntimeError("crash before derived export"),
            ):
                with self.assertRaisesRegex(RuntimeError, "derived export"):
                    journal.append(
                        "selector_completed",
                        completed_batches=2,
                        checkpoint_sha256=selection["checkpoint_sha256"],
                    )
            authoritative = load_event_log(
                root / "events",
                root=root,
                run_identity=journal.run_identity,
            )
            self.assertEqual(len(authoritative), 3)
            with self.assertRaisesRegex(ValueError, "byte-identical"):
                self._validate(root, route, config, snapshot, selection)


class InvalidationBoundaryTest(unittest.TestCase):
    def test_formal_training_and_publisher_reject_any_marker_entry(self):
        route = _route_root()
        runtime = route / RUNTIME_ROOT_NAME
        with (
            tempfile.TemporaryDirectory(dir=runtime) as training_directory,
            tempfile.TemporaryDirectory(dir=runtime) as selector_directory,
        ):
            training_workspace = Path(training_directory)
            selector_workspace = Path(selector_directory)
            (training_workspace / "INVALIDATED.json").symlink_to("missing-marker")
            with self.assertRaisesRegex(ValueError, "invalidated"):
                run_training(
                    training_workspace,
                    resume=False,
                    max_new_batches=0,
                )
            (training_workspace / "INVALIDATED.json").unlink()
            (selector_workspace / "INVALIDATED.json").mkdir()
            with self.assertRaisesRegex(ValueError, "invalidated"):
                gate.publish_m4_outputs(training_workspace, selector_workspace)

    def test_publisher_rechecks_marker_created_during_selector_read(self):
        route = _route_root()
        runtime = route / RUNTIME_ROOT_NAME
        with (
            tempfile.TemporaryDirectory(dir=runtime) as training_directory,
            tempfile.TemporaryDirectory(dir=runtime) as selector_directory,
        ):
            training_workspace = Path(training_directory)
            selector_workspace = Path(selector_directory)
            selection = {"checkpoint_sha256": "a" * 64}
            atomic_json_write(
                selector_workspace / "selection.json",
                selection,
                root=selector_workspace,
            )

            def invalidate_during_read(**_kwargs):
                (selector_workspace / "INVALIDATED.json").mkdir()
                return [], {}, {}

            with (
                mock.patch.object(
                    gate,
                    "_load_config",
                    return_value={"diagnostic_evidence": {}},
                ),
                mock.patch.object(gate, "_validate_selection"),
                mock.patch.object(gate, "_require_checkpoint_not_invalidated"),
                mock.patch.object(
                    gate,
                    "_selector_journal_evidence",
                    side_effect=invalidate_during_read,
                ),
            ):
                with self.assertRaisesRegex(ValueError, "invalidated"):
                    gate.publish_m4_outputs(training_workspace, selector_workspace)

    def test_verifier_rejects_checkpoint_named_by_registry(self):
        selection = {"checkpoint_sha256": "a" * 64}
        registry = {
            "entries": [
                {
                    "workspace_relative": "invalid-selector-run",
                    "selector_checkpoint_payload_sha256": "a" * 64,
                }
            ]
        }
        with self.assertRaisesRegex(ValueError, "invalidated run"):
            gate._require_checkpoint_not_invalidated(selection, registry)

    def test_render_end_boundary_recaptures_source_and_registry(self):
        route = _route_root()
        selection = {"checkpoint_sha256": "a" * 64}
        initial_snapshot = SimpleNamespace(digest="1" * 64)
        changed_snapshot = SimpleNamespace(digest="2" * 64)
        initial_registry = {"entries": []}
        invalidating_registry = {
            "entries": [
                {
                    "workspace_relative": "late-invalidated-run",
                    "selector_checkpoint_payload_sha256": "a" * 64,
                }
            ]
        }
        with (
            mock.patch.object(
                gate,
                "capture_source_snapshot",
                return_value=initial_snapshot,
            ),
            mock.patch.object(
                gate,
                "invalidation_registry_snapshot",
                return_value=invalidating_registry,
            ),
        ):
            with self.assertRaisesRegex(ValueError, "invalidated run"):
                gate._require_unchanged_render_boundary(
                    route,
                    selection,
                    initial_snapshot,
                    initial_registry,
                )
        with (
            mock.patch.object(
                gate,
                "capture_source_snapshot",
                return_value=changed_snapshot,
            ),
            mock.patch.object(
                gate,
                "invalidation_registry_snapshot",
                return_value=initial_registry,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "changed during M4 render"):
                gate._require_unchanged_render_boundary(
                    route,
                    selection,
                    initial_snapshot,
                    initial_registry,
                )


class PublicationTransactionTest(unittest.TestCase):
    @staticmethod
    def _isolated_route(root: Path) -> Path:
        route = root / "cfr_neural_search"
        for relative in (
            RUNTIME_ROOT_NAME,
            gate.SELECTOR_EVENT_DIRECTORY_RELATIVE,
            "manifests/invalidated_selector_runs",
        ):
            (route / relative).mkdir(parents=True, exist_ok=True)
        return route

    @staticmethod
    def _prepare_runtime_inputs(training_workspace: Path, selector_workspace: Path):
        (training_workspace / "blueprint.rbbp").write_bytes(b"new-blueprint")
        selection = {"checkpoint_sha256": "a" * 64}
        atomic_json_write(
            selector_workspace / "selection.json",
            selection,
            root=selector_workspace,
        )
        event_directory = selector_workspace / "events"
        event_directory.mkdir()
        (event_directory / "000000000000.json").write_bytes(b"new-event-zero")
        (event_directory / "000000000001.json").write_bytes(b"new-event-one")
        (selector_workspace / "events.jsonl").write_bytes(b"new-jsonl\n")
        (selector_workspace / "heartbeat.json").write_bytes(b"new-heartbeat\n")
        event_manifest = {
            name: file_sha256(event_directory / name)
            for name in ("000000000000.json", "000000000001.json")
        }
        return selection, event_manifest

    @staticmethod
    def _seed_prior_publication(route: Path) -> None:
        for relative in gate._published_scalar_relatives():
            gate.atomic_write_bytes(
                route / relative,
                f"old::{relative}".encode("ascii"),
                root=route,
            )
        event_directory = route / gate.SELECTOR_EVENT_DIRECTORY_RELATIVE
        if event_directory.exists():
            for name in gate.stable_flat_directory_manifest(event_directory):
                gate.remove_regular_file(event_directory / name, root=route)
        gate.atomic_write_bytes(
            event_directory / "000000000000.json",
            b"old-event-zero",
            root=route,
        )

    def test_publisher_end_boundary_rejects_new_registry_checkpoint(self):
        route = _route_root()
        runtime = route / RUNTIME_ROOT_NAME
        with (
            tempfile.TemporaryDirectory(dir=runtime) as training_directory,
            tempfile.TemporaryDirectory(dir=runtime) as selector_directory,
        ):
            training_workspace = Path(training_directory)
            selector_workspace = Path(selector_directory)
            selection, event_manifest = self._prepare_runtime_inputs(
                training_workspace,
                selector_workspace,
            )
            initial_registry = {"entries": []}
            invalidating_registry = {
                "entries": [
                    {
                        "workspace_relative": "same-checkpoint-other-workspace",
                        "selector_checkpoint_payload_sha256": selection[
                            "checkpoint_sha256"
                        ],
                    }
                ]
            }
            with (
                mock.patch.object(
                    gate,
                    "_load_config",
                    return_value={"diagnostic_evidence": {}},
                ),
                mock.patch.object(gate, "_validate_selection"),
                mock.patch.object(
                    gate,
                    "_selector_journal_evidence",
                    return_value=([{"event": "complete"}], {"status": "complete"}, event_manifest),
                ),
                mock.patch.object(
                    gate,
                    "invalidation_registry_snapshot",
                    side_effect=(initial_registry, invalidating_registry),
                ),
            ):
                with self.assertRaisesRegex(ValueError, "invalidated run"):
                    gate.publish_m4_outputs(training_workspace, selector_workspace)

    def test_export_and_heartbeat_raw_aba_are_rejected_before_overwrite(self):
        route = _route_root()
        runtime = route / RUNTIME_ROOT_NAME
        for relative in ("events.jsonl", "heartbeat.json"):
            with (
                self.subTest(relative=relative),
                tempfile.TemporaryDirectory(dir=runtime) as training_directory,
                tempfile.TemporaryDirectory(dir=runtime) as selector_directory,
            ):
                training_workspace = Path(training_directory)
                selector_workspace = Path(selector_directory)
                _selection, event_manifest = self._prepare_runtime_inputs(
                    training_workspace,
                    selector_workspace,
                )
                target = selector_workspace / relative
                real_read = gate.read_regular_bytes
                captured = False

                def aba_read(path, *, root=None, **kwargs):
                    nonlocal captured
                    if Path(path) == target and not captured:
                        captured = True
                        return b"transient-unverified-b\n"
                    return real_read(path, root=root, **kwargs)

                with (
                    mock.patch.object(
                        gate,
                        "_load_config",
                        return_value={"diagnostic_evidence": {}},
                    ),
                    mock.patch.object(gate, "_validate_selection"),
                    mock.patch.object(
                        gate,
                        "_selector_journal_evidence",
                        return_value=(
                            [{"event": "complete"}],
                            {"status": "complete"},
                            event_manifest,
                        ),
                    ),
                    mock.patch.object(
                        gate,
                        "read_regular_bytes",
                        side_effect=aba_read,
                    ),
                ):
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "journal view bytes changed",
                    ):
                        gate.publish_m4_outputs(
                            training_workspace,
                            selector_workspace,
                        )
                self.assertTrue(captured)

    def test_two_publishers_and_invalidator_share_one_reentrant_authority_lock(self):
        first_entered = threading.Event()
        release_first = threading.Event()
        rollback_done = threading.Event()
        secondary_entered = threading.Event()
        errors: list[BaseException] = []
        order: list[str] = []

        def fake_publish(_route, training_workspace, _selector_workspace):
            if Path(training_workspace).name == "first":
                order.append("first_enter")
                first_entered.set()
                if not release_first.wait(2.0):
                    raise RuntimeError("test did not release first publisher")
                order.append("first_rollback_complete")
                rollback_done.set()
                return {"publisher": "first"}
            if not rollback_done.is_set():
                raise RuntimeError("second publisher entered before rollback completed")
            order.append("second_publisher_enter")
            secondary_entered.set()
            return {"publisher": "second"}

        def fake_invalidate(_route, _workspace, _reason, *, registry_root):
            del registry_root
            if not rollback_done.is_set():
                raise RuntimeError("invalidator entered before publication rollback completed")
            order.append("invalidator_enter")
            secondary_entered.set()
            return {"invalidated": True}

        def run(callable_):
            try:
                callable_()
            except BaseException as exc:  # pragma: no branch - assertion below
                errors.append(exc)

        with (
            mock.patch.object(
                gate,
                "_publish_m4_outputs_locked",
                side_effect=fake_publish,
            ),
            mock.patch.object(
                invalidator,
                "_invalidate_selector_run_locked",
                side_effect=fake_invalidate,
            ),
        ):
            first = threading.Thread(
                target=run,
                args=(lambda: gate.publish_m4_outputs(Path("first"), Path("s1")),),
            )
            second = threading.Thread(
                target=run,
                args=(lambda: gate.publish_m4_outputs(Path("second"), Path("s2")),),
            )
            invalidation = threading.Thread(
                target=run,
                args=(
                    lambda: invalidator.invalidate_selector_run(
                        Path("invalidated"),
                        "unit_test_lock_order",
                    ),
                ),
            )
            first.start()
            self.assertTrue(first_entered.wait(1.0))
            second.start()
            invalidation.start()
            self.assertFalse(secondary_entered.wait(0.1))
            release_first.set()
            for thread in (first, second, invalidation):
                thread.join(2.0)
                self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertLess(
            order.index("first_rollback_complete"),
            order.index("second_publisher_enter"),
        )
        self.assertLess(
            order.index("first_rollback_complete"),
            order.index("invalidator_enter"),
        )

    def test_every_publication_overwrite_rolls_back_prior_bytes_and_tree(self):
        real_route = _route_root()
        with gate.m4_authority_lock(real_route):
            real_publication = gate._capture_published_output_backup(real_route)
        real_atomic = gate.atomic_write_bytes
        try:
            with tempfile.TemporaryDirectory() as directory:
                route = self._isolated_route(Path(directory))
                runtime = route / RUNTIME_ROOT_NAME
                stable_snapshot = SimpleNamespace(digest="e" * 64)
                with (
                    mock.patch.object(gate, "_route_root", return_value=route),
                    mock.patch.object(
                        gate,
                        "capture_source_snapshot",
                        return_value=stable_snapshot,
                    ),
                ):
                    # Capture, seed, every injected failure, the successful
                    # replacement, and restoration all share one reentrant
                    # authority lock rooted only in this temporary route.
                    with gate.m4_authority_lock(route):
                        original = gate._capture_published_output_backup(route)
                        try:
                            self._seed_prior_publication(route)
                            prior = gate._capture_published_output_backup(route)
                            with (
                                tempfile.TemporaryDirectory(
                                    dir=runtime
                                ) as training_directory,
                                tempfile.TemporaryDirectory(
                                    dir=runtime
                                ) as selector_directory,
                            ):
                                training_workspace = Path(training_directory)
                                selector_workspace = Path(selector_directory)
                                _selection, event_manifest = self._prepare_runtime_inputs(
                                    training_workspace,
                                    selector_workspace,
                                )
                                fake_events = [{"event": "complete"}]
                                fake_heartbeat = {"status": "complete"}
                                config = {
                                    "diagnostic_evidence": {
                                        "deck_root_seed": 7,
                                        "policy_seeds": [11, 13],
                                    }
                                }
                                target_by_stage = {
                                    "blueprint": route / gate.BLUEPRINT_RELATIVE,
                                    "event": route
                                    / gate.SELECTOR_EVENT_DIRECTORY_RELATIVE
                                    / "000000000001.json",
                                    "selection": route / gate.SELECTION_RELATIVE,
                                    "jsonl": route / gate.SELECTOR_EVENTS_RELATIVE,
                                    "heartbeat": route
                                    / gate.SELECTOR_HEARTBEAT_RELATIVE,
                                }

                                for stage in (
                                    "blueprint",
                                    "event",
                                    "selection",
                                    "jsonl",
                                    "heartbeat",
                                    "evidence",
                                    "manifest",
                                ):
                                    with self.subTest(stage=stage):
                                        fired = False

                                        def faulting_atomic(
                                            path,
                                            content,
                                            *,
                                            root=None,
                                            **kwargs,
                                        ):
                                            nonlocal fired
                                            real_atomic(
                                                path,
                                                content,
                                                root=root,
                                                **kwargs,
                                            )
                                            if (
                                                not fired
                                                and Path(path)
                                                == target_by_stage.get(stage)
                                            ):
                                                fired = True
                                                raise RuntimeError(
                                                    f"fault after {stage}"
                                                )

                                        def save_evidence(
                                            path,
                                            *_args,
                                            root,
                                            **_kwargs,
                                        ):
                                            nonlocal fired
                                            real_atomic(
                                                path,
                                                b"new-evidence",
                                                root=root,
                                            )
                                            if stage == "evidence" and not fired:
                                                fired = True
                                                raise RuntimeError(
                                                    "fault after evidence"
                                                )

                                        def write_manifest():
                                            nonlocal fired
                                            real_atomic(
                                                route / gate.MANIFEST_RELATIVE,
                                                b"new-manifest",
                                                root=route,
                                            )
                                            if stage == "manifest" and not fired:
                                                fired = True
                                                raise RuntimeError(
                                                    "fault after manifest"
                                                )
                                            return {"published": True}

                                        with (
                                            mock.patch.object(
                                                gate,
                                                "_load_config",
                                                return_value=config,
                                            ),
                                            mock.patch.object(
                                                gate,
                                                "_validate_selection",
                                            ),
                                            mock.patch.object(
                                                gate,
                                                "_selector_journal_evidence",
                                                return_value=(
                                                    fake_events,
                                                    fake_heartbeat,
                                                    event_manifest,
                                                ),
                                            ),
                                            mock.patch.object(
                                                gate,
                                                "run_reproducibility_gate",
                                                return_value=(object(), object()),
                                            ),
                                            mock.patch.object(
                                                gate,
                                                "save_reproducibility_evidence",
                                                side_effect=save_evidence,
                                            ),
                                            mock.patch.object(
                                                gate,
                                                "write_m4_manifest",
                                                side_effect=write_manifest,
                                            ),
                                            mock.patch.object(
                                                gate,
                                                "atomic_write_bytes",
                                                side_effect=faulting_atomic,
                                            ),
                                        ):
                                            with self.assertRaisesRegex(
                                                RuntimeError,
                                                f"after {stage}",
                                            ):
                                                gate.publish_m4_outputs(
                                                    training_workspace,
                                                    selector_workspace,
                                                )
                                        self.assertTrue(fired)
                                        gate._require_published_output_backup(
                                            route,
                                            prior,
                                        )

                                def save_success(path, *_args, root, **_kwargs):
                                    real_atomic(path, b"new-evidence", root=root)

                                def write_success():
                                    real_atomic(
                                        route / gate.MANIFEST_RELATIVE,
                                        b"new-manifest",
                                        root=route,
                                    )
                                    return {"published": True}

                                with (
                                    mock.patch.object(
                                        gate,
                                        "_load_config",
                                        return_value=config,
                                    ),
                                    mock.patch.object(
                                        gate,
                                        "_validate_selection",
                                    ),
                                    mock.patch.object(
                                        gate,
                                        "_selector_journal_evidence",
                                        return_value=(
                                            fake_events,
                                            fake_heartbeat,
                                            event_manifest,
                                        ),
                                    ),
                                    mock.patch.object(
                                        gate,
                                        "run_reproducibility_gate",
                                        return_value=(object(), object()),
                                    ),
                                    mock.patch.object(
                                        gate,
                                        "save_reproducibility_evidence",
                                        side_effect=save_success,
                                    ),
                                    mock.patch.object(
                                        gate,
                                        "write_m4_manifest",
                                        side_effect=write_success,
                                    ),
                                ):
                                    self.assertEqual(
                                        gate.publish_m4_outputs(
                                            training_workspace,
                                            selector_workspace,
                                        ),
                                        {"published": True},
                                    )
                                self.assertEqual(
                                    gate.stable_flat_directory_manifest(
                                        route
                                        / gate.SELECTOR_EVENT_DIRECTORY_RELATIVE
                                    ),
                                    event_manifest,
                                )
                        finally:
                            gate._restore_published_output_backup(route, original)
        finally:
            # The test's real publication tree is observation-only. Any byte
            # or event-tree change here is a hard isolation regression.
            with gate.m4_authority_lock(real_route):
                gate._require_published_output_backup(
                    real_route,
                    real_publication,
                )

    def test_external_verifier_never_observes_locked_transient_manifest(self):
        context = multiprocessing.get_context("fork")
        with tempfile.TemporaryDirectory() as directory:
            route = self._isolated_route(Path(directory))
            manifest_path = route / gate.MANIFEST_RELATIVE
            complete_manifest = {"publication": "old-complete-tree"}
            atomic_json_write(manifest_path, complete_manifest, root=route)
            complete_bytes = gate.read_regular_bytes(manifest_path, root=route)

            begin = context.Event()
            attempted = context.Event()
            completed = context.Event()
            result_queue = context.Queue()
            with (
                mock.patch.object(gate, "_route_root", return_value=route),
                mock.patch.object(
                    gate,
                    "render_m4_manifest",
                    return_value=complete_manifest,
                ),
            ):
                process = context.Process(
                    target=_verify_manifest_in_external_process,
                    args=(begin, attempted, completed, result_queue),
                )
                process.start()
                try:
                    with gate.m4_authority_lock(route):
                        gate.atomic_write_bytes(manifest_path, b"", root=route)
                        begin.set()
                        self.assertTrue(attempted.wait(2.0))
                        self.assertFalse(
                            completed.wait(0.2),
                            "external verifier crossed the authority lock",
                        )
                        gate.atomic_write_bytes(
                            manifest_path,
                            complete_bytes,
                            root=route,
                        )
                    self.assertTrue(completed.wait(2.0))
                    process.join(2.0)
                    self.assertFalse(process.is_alive())
                    self.assertEqual(process.exitcode, 0)
                    self.assertEqual(
                        result_queue.get(timeout=2.0),
                        ("ok", complete_manifest),
                    )
                    self.assertEqual(
                        gate.read_regular_bytes(manifest_path, root=route),
                        complete_bytes,
                    )
                finally:
                    if process.is_alive():
                        process.terminate()
                        process.join(2.0)
                    result_queue.close()
                    result_queue.join_thread()


if __name__ == "__main__":
    unittest.main()
