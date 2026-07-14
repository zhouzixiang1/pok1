from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bots.research_native_lab.common_contracts import ActionKind
from bots.research_native_lab.common_contracts.protocol import NationalProtocolSession
from bots.research_native_lab.cfr_neural_search.core.strict_io import (
    append_jsonl_bytes,
    atomic_create_bytes,
    atomic_write_bytes,
    read_regular_bytes,
    stable_flat_directory_manifest,
    stable_tree_manifest,
    strict_json_loads,
    validate_real_directory,
)
from bots.research_native_lab.cfr_neural_search.native_runtime.common_adapter import (
    adapt_national_decision,
)
from bots.research_native_lab.cfr_neural_search.native_runtime.decision_lease import (
    RouteDecisionLease,
)


class StrictIOTest(unittest.TestCase):
    def test_flat_manifest_binds_hash_and_shape_to_one_held_directory_fd(self):
        from bots.research_native_lab.cfr_neural_search.core import strict_io

        for leave_replacement in (False, True):
            with self.subTest(leave_replacement=leave_replacement), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                root = base / "events"
                alternate = base / "alternate"
                root.mkdir()
                alternate.mkdir()
                (root / "000000000000.json").write_bytes(b"authority-a")
                (alternate / "000000000000.json").write_bytes(b"authority-b")
                (alternate / "empty").mkdir()
                detached_a = base / "detached-a"
                detached_b = base / "detached-b"
                real_hash = strict_io._hash_openat_regular
                fired = False

                def swap_path_after_held_hash(
                    parent_fd,
                    name,
                    metadata,
                    *,
                    max_file_bytes,
                ):
                    nonlocal fired
                    result = real_hash(
                        parent_fd,
                        name,
                        metadata,
                        max_file_bytes=max_file_bytes,
                    )
                    if not fired:
                        fired = True
                        root.rename(detached_a)
                        alternate.rename(root)
                        if not leave_replacement:
                            root.rename(detached_b)
                            detached_a.rename(root)
                    return result

                with mock.patch.object(
                    strict_io,
                    "_hash_openat_regular",
                    side_effect=swap_path_after_held_hash,
                ):
                    with self.assertRaisesRegex(
                        ValueError,
                        "identity|reachable|directory changed",
                    ):
                        stable_flat_directory_manifest(root)

    def test_flat_manifest_rejects_empty_nested_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "000000000000.json").write_bytes(b"event")
            (root / "empty").mkdir()
            with self.assertRaisesRegex(ValueError, "non-regular"):
                stable_flat_directory_manifest(root)

    def test_atomic_create_is_no_clobber_and_publishes_complete_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "events" / "000000000000.json"
            atomic_create_bytes(path, b"first", root=root)
            self.assertEqual(path.read_bytes(), b"first")
            with self.assertRaisesRegex(ValueError, "already exists"):
                atomic_create_bytes(path, b"second", root=root)
            self.assertEqual(path.read_bytes(), b"first")

    def test_durable_jsonl_append_is_additive_and_rejects_symlink_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "events.jsonl"
            append_jsonl_bytes(path, b'{"sequence":0}\n', root=root)
            append_jsonl_bytes(path, b'{"sequence":1}\n', root=root)
            self.assertEqual(
                path.read_bytes(),
                b'{"sequence":0}\n{"sequence":1}\n',
            )
            target = root / "outside"
            target.write_bytes(b"untouched")
            path.unlink()
            path.symlink_to(target)
            with self.assertRaises(ValueError):
                append_jsonl_bytes(path, b'{"attack":true}\n', root=root)
            self.assertEqual(target.read_bytes(), b"untouched")

    def test_escape_rejection_has_no_directory_side_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "root"
            root.mkdir()
            outside = base / "not-created" / "payload"
            with self.assertRaises(ValueError):
                atomic_write_bytes(outside, b"forbidden", root=root)
            self.assertFalse(outside.parent.exists())

    def test_symlink_root_parent_target_and_input_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "root"
            root.mkdir()
            root_link = base / "root-link"
            root_link.symlink_to(root, target_is_directory=True)
            with self.assertRaises(ValueError):
                atomic_write_bytes(root_link / "x", b"x", root=root_link)

            parent_link = root / "parent-link"
            parent_link.symlink_to(base, target_is_directory=True)
            with self.assertRaises(ValueError):
                atomic_write_bytes(parent_link / "x", b"x", root=root)
            self.assertFalse((base / "x").exists())

            target = root / "target"
            external = base / "external"
            external.write_bytes(b"safe")
            target.symlink_to(external)
            with self.assertRaises(ValueError):
                atomic_write_bytes(target, b"attack", root=root)
            with self.assertRaises(ValueError):
                read_regular_bytes(target, root=root)
            self.assertEqual(external.read_bytes(), b"safe")

    def test_symlink_in_root_ancestry_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            real = base / "real"
            root = real / "root"
            root.mkdir(parents=True)
            linked_ancestor = base / "linked"
            linked_ancestor.symlink_to(real, target_is_directory=True)
            with self.assertRaises(ValueError):
                atomic_write_bytes(
                    linked_ancestor / "root" / "x",
                    b"forbidden",
                    root=linked_ancestor / "root",
                )
            self.assertFalse((root / "x").exists())

    def test_directory_validation_rejects_precanonical_symlink_and_parent_steps(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            real = base / "runtime" / "job"
            real.mkdir(parents=True)
            linked = base / "runtime-link"
            linked.symlink_to(base / "runtime", target_is_directory=True)
            self.assertEqual(validate_real_directory(real), real)
            with self.assertRaises(ValueError):
                validate_real_directory(linked / "job")
            with self.assertRaisesRegex(ValueError, "parent traversal"):
                validate_real_directory(real / ".." / "job")

    def test_atomic_target_regular_inode_swap_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.write_bytes(b"original")
            real_fsync = __import__("os").fsync
            fired = False

            def swap_after_temp_sync(descriptor):
                nonlocal fired
                real_fsync(descriptor)
                if not fired:
                    fired = True
                    target.unlink()
                    target.write_bytes(b"attacker")

            with mock.patch(
                "bots.research_native_lab.cfr_neural_search.core.strict_io.os.fsync",
                side_effect=swap_after_temp_sync,
            ):
                with self.assertRaisesRegex(ValueError, "identity changed"):
                    atomic_write_bytes(target, b"replacement", root=root)
            self.assertEqual(target.read_bytes(), b"attacker")

    def test_atomic_absent_target_creation_race_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            real_fsync = __import__("os").fsync
            fired = False

            def create_after_temp_sync(descriptor):
                nonlocal fired
                real_fsync(descriptor)
                if not fired:
                    fired = True
                    target.write_bytes(b"attacker")

            with mock.patch(
                "bots.research_native_lab.cfr_neural_search.core.strict_io.os.fsync",
                side_effect=create_after_temp_sync,
            ):
                with self.assertRaisesRegex(ValueError, "identity changed"):
                    atomic_write_bytes(target, b"replacement", root=root)
            self.assertEqual(target.read_bytes(), b"attacker")

    def test_atomic_parent_rename_and_same_name_replacement_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / "parent"
            parent.mkdir()
            target = parent / "target"
            target.write_bytes(b"original")
            detached = root / "detached"
            real_fsync = __import__("os").fsync
            fired = False

            def replace_parent_after_temp_sync(descriptor):
                nonlocal fired
                real_fsync(descriptor)
                if not fired:
                    fired = True
                    parent.rename(detached)
                    parent.mkdir()
                    (parent / "target").write_bytes(b"attacker")

            with mock.patch(
                "bots.research_native_lab.cfr_neural_search.core.strict_io.os.fsync",
                side_effect=replace_parent_after_temp_sync,
            ):
                with self.assertRaisesRegex(ValueError, "ancestry identity"):
                    atomic_write_bytes(target, b"replacement", root=root)
            self.assertEqual((parent / "target").read_bytes(), b"attacker")
            self.assertEqual((detached / "target").read_bytes(), b"original")

    def test_atomic_round_trip_and_size_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "nested" / "data"
            atomic_write_bytes(path, b"payload", root=root)
            self.assertEqual(read_regular_bytes(path, root=root), b"payload")
            with self.assertRaises(ValueError):
                read_regular_bytes(path, root=root, max_bytes=3)

    def test_content_read_rejects_in_place_rewrite_with_restored_mtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "source.py"
            target.write_bytes(b"original")
            original_mtime = target.stat().st_mtime_ns
            real_read = __import__("os").read
            fired = False

            def rewrite_after_first_read(descriptor, count):
                nonlocal fired
                chunk = real_read(descriptor, count)
                if not fired:
                    fired = True
                    target.write_bytes(b"modified")
                    __import__("os").utime(
                        target,
                        ns=(target.stat().st_atime_ns, original_mtime),
                    )
                return chunk

            with mock.patch(
                "bots.research_native_lab.cfr_neural_search.core.strict_io.os.read",
                side_effect=rewrite_after_first_read,
            ):
                with self.assertRaisesRegex(ValueError, "changed during"):
                    read_regular_bytes(target, root=root)

    def test_strict_json_rejects_duplicate_nonstandard_and_overflow_numbers(self):
        for raw in (
            b'{"x":1,"x":2}',
            b'{"x":NaN}',
            b'{"x":Infinity}',
            b'{"x":-Infinity}',
            b'{"x":1e999}',
            b'{"x":-1e999}',
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    strict_json_loads(raw)
        self.assertEqual(strict_json_loads(b'{"x":1.25e2}'), {"x":125.0})

    def test_tree_snapshot_rejects_old_a_plus_new_b_mixed_time_view(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "a.py"
            second = root / "b.py"
            first.write_bytes(b"old")
            second.write_bytes(b"bbb")
            from bots.research_native_lab.cfr_neural_search.core import strict_io

            real_hash = strict_io._hash_openat_regular
            fired = False

            def rewrite_after_a(parent_fd, name, metadata, *, max_file_bytes):
                nonlocal fired
                result = real_hash(
                    parent_fd,
                    name,
                    metadata,
                    max_file_bytes=max_file_bytes,
                )
                if name == "a.py" and not fired:
                    fired = True
                    first.write_bytes(b"new")
                return result

            with mock.patch.object(
                strict_io,
                "_hash_openat_regular",
                side_effect=rewrite_after_a,
            ):
                with self.assertRaisesRegex(ValueError, "snapshot passes"):
                    stable_tree_manifest(root)

    def test_tree_snapshot_rejects_symlinks_and_honors_explicit_runtime_exclusion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "source.py").write_bytes(b"source")
            runtime = root / "runtime_outputs"
            runtime.mkdir()
            (runtime / "checkpoint.json").write_bytes(b"volatile")
            manifest = stable_tree_manifest(
                root,
                excluded_paths=frozenset({"runtime_outputs"}),
            )
            self.assertEqual(set(manifest), {"source.py"})
            link = root / "linked.py"
            link.symlink_to(root / "source.py")
            with self.assertRaisesRegex(ValueError, "symlink"):
                stable_tree_manifest(
                    root,
                    excluded_paths=frozenset({"runtime_outputs"}),
                )

    def test_read_and_tree_reject_parent_replacement_of_held_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            parent = base / "parent"
            parent.mkdir()
            target = parent / "source.py"
            target.write_bytes(b"original")
            detached = base / "detached"
            real_read = __import__("os").read
            fired = False

            def replace_parent_after_read(descriptor, count):
                nonlocal fired
                chunk = real_read(descriptor, count)
                if not fired:
                    fired = True
                    parent.rename(detached)
                    parent.mkdir()
                    (parent / "source.py").write_bytes(b"attacker")
                return chunk

            with mock.patch(
                "bots.research_native_lab.cfr_neural_search.core.strict_io.os.read",
                side_effect=replace_parent_after_read,
            ):
                with self.assertRaisesRegex(ValueError, "ancestry identity"):
                    read_regular_bytes(target, root=parent)

            parent_target = parent / "source.py"
            parent_target.unlink()
            parent.rmdir()
            detached.rename(parent)
            fired = False
            from bots.research_native_lab.cfr_neural_search.core import strict_io

            real_hash = strict_io._hash_openat_regular

            def replace_parent_after_hash(parent_fd, name, metadata, *, max_file_bytes):
                nonlocal fired
                result = real_hash(
                    parent_fd,
                    name,
                    metadata,
                    max_file_bytes=max_file_bytes,
                )
                if not fired:
                    fired = True
                    parent.rename(detached)
                    parent.mkdir()
                    (parent / "source.py").write_bytes(b"attacker")
                return result

            with mock.patch.object(
                strict_io,
                "_hash_openat_regular",
                side_effect=replace_parent_after_hash,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "ancestry identity|directory changed",
                ):
                    stable_tree_manifest(parent)


class RouteDecisionLeaseTest(unittest.TestCase):
    @staticmethod
    def _pending_session() -> NationalProtocolSession:
        session = NationalProtocolSession("RouteLease")
        session.receive("name")
        session.name_response()
        session.receive("preflop|SMALLBLIND|<0,12><1,11>")
        return session

    def test_bool_decision_id_is_rejected_before_common_submit(self):
        session = self._pending_session()
        snapshot = adapt_national_decision(session.current)
        action = next(
            action
            for action in snapshot.representative_actions()
            if action.kind is ActionKind.CALL
        )
        bound = snapshot.bind(action, current_state=session.current)
        with self.assertRaises(TypeError):
            RouteDecisionLease(False, snapshot.full_state_id, bound)
        self.assertIsNotNone(session.pending_decision_id)

    def test_full_state_bound_lease_consumes_exactly_once(self):
        session = self._pending_session()
        snapshot = adapt_national_decision(session.current)
        action = next(
            action
            for action in snapshot.representative_actions()
            if action.kind is ActionKind.CALL
        )
        bound = snapshot.bind(action, current_state=session.current)
        lease = RouteDecisionLease(
            session.pending_decision_id,
            snapshot.full_state_id,
            bound,
        )
        lease.consume(session)
        with self.assertRaisesRegex(Exception, "already consumed"):
            lease.consume(session)

    def test_official_hand70_terminal_with_69_settlements_requires_thp(self):
        session = NationalProtocolSession("EOFShape")
        session.receive("name")
        session.name_response()
        for hand in range(1, 71):
            hero_sb = hand % 2 == 1
            role = "SMALLBLIND" if hero_sb else "BIGBLIND"
            session.receive(f"preflop|{role}|<0,12><1,11>")
            if hero_sb:
                decision_id = session.pending_decision_id
                session.submit_action(decision_id, "fold")
                earn = -50
            else:
                session.receive("fold")
                earn = 50
            if hand < 70:
                session.receive(f"earnChips {earn}")
        evidence = session.connection_close_evidence()
        self.assertTrue(evidence["natural_70_boundary"])
        self.assertTrue(evidence["hand_70_terminal_wire_state"])
        self.assertTrue(evidence["requires_thp_state_69"])
        self.assertFalse(evidence["wire_alone_proves_complete"])


if __name__ == "__main__":
    unittest.main()
