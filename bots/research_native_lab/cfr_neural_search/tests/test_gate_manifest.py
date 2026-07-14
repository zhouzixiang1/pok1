from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from bots.research_native_lab.cfr_neural_search.blueprint import mccfr as mccfr_module
from bots.research_native_lab.cfr_neural_search.tools import verify_m3_gate as gate_module

from bots.research_native_lab.cfr_neural_search.tools.verify_m3_gate import (
    M3GateVerificationError,
    render_m3_gate_manifest,
    verify_artifact_map,
    verify_m3_gate_manifest,
    write_m3_gate_manifest,
)


class GateManifestTest(unittest.TestCase):
    def test_foreign_tree_and_imported_module_mismatch_fail_before_render(self):
        manifest = (
            Path(__file__).parents[1] / "manifests" / "m3_gate_20260714.json"
        )
        with tempfile.TemporaryDirectory() as directory:
            foreign = Path(directory) / "manifests" / manifest.name
            foreign.parent.mkdir()
            foreign.write_bytes(manifest.read_bytes())
            with self.assertRaisesRegex(M3GateVerificationError, "different route tree"):
                render_m3_gate_manifest(foreign)

            forged_module = Path(directory) / "mccfr.py"
            forged_module.write_text("raise RuntimeError('forged')\n", encoding="utf-8")
            with mock.patch.object(mccfr_module, "__file__", str(forged_module)):
                with self.assertRaisesRegex(
                    M3GateVerificationError,
                    "module/root mismatch",
                ):
                    render_m3_gate_manifest(manifest)

    def test_renderer_detects_mixed_time_input_snapshot(self):
        manifest = (
            Path(__file__).parents[1] / "manifests" / "m3_gate_20260714.json"
        )
        route_root = manifest.parent.parent
        relative = manifest.relative_to(route_root).as_posix()
        stable = gate_module._capture_input_snapshot(
            route_root,
            manifest_relative=relative,
        )
        drifted = replace(stable, solver_input_digest="0" * 64)
        template = json.loads(manifest.read_text(encoding="utf-8"))
        with (
            mock.patch.object(
                gate_module,
                "_capture_input_snapshot",
                side_effect=(stable, drifted),
            ),
            mock.patch.object(
                gate_module,
                "_derive_frozen_training_runs",
                return_value=template["frozen_training_runs"],
            ),
            mock.patch.object(
                gate_module,
                "_render_independent_reference",
                return_value=template["independent_reference"],
            ),
            mock.patch.object(
                gate_module,
                "_derive_state_fixtures",
                return_value=template["deterministic_state_fixtures"],
            ),
        ):
            with self.assertRaisesRegex(M3GateVerificationError, "mixed-time"):
                render_m3_gate_manifest(manifest)

    def test_write_rolls_back_old_manifest_when_post_write_snapshot_drifts(self):
        manifest_path = (
            Path(__file__).parents[1] / "manifests" / "m3_gate_20260714.json"
        )
        route_root = manifest_path.parent.parent
        relative = manifest_path.relative_to(route_root).as_posix()
        stable = gate_module._capture_input_snapshot(
            route_root,
            manifest_relative=relative,
        )
        drifted = replace(stable, solver_input_digest="f" * 64)
        rendered = json.loads(manifest_path.read_text(encoding="utf-8"))
        rendered["artifact_scope"]["files"] = dict(stable.route_files)
        with (
            mock.patch.object(
                gate_module,
                "render_m3_gate_manifest",
                return_value=rendered,
            ),
            mock.patch.object(
                gate_module,
                "_capture_input_snapshot",
                side_effect=(stable, drifted),
            ),
            mock.patch.object(gate_module, "_atomic_json_write") as write_mock,
            mock.patch.object(gate_module, "_atomic_bytes_write") as restore_mock,
        ):
            with self.assertRaisesRegex(M3GateVerificationError, "mixed-time"):
                write_m3_gate_manifest(manifest_path)
        write_mock.assert_called_once()
        restore_mock.assert_called_once_with(
            manifest_path.resolve(),
            manifest_path.read_bytes(),
        )

    def test_historical_m3_renderer_rejects_changed_solver_semantics(self):
        manifest = (
            Path(__file__).parents[1] / "manifests" / "m3_gate_20260714.json"
        )
        with self.assertRaisesRegex(
            M3GateVerificationError,
            "frozen training state drifted",
        ):
            render_m3_gate_manifest(manifest)

    def test_historical_m3_manifest_fails_closed_on_current_m4_tree(self):
        manifest = (
            Path(__file__).parents[1] / "manifests" / "m3_gate_20260714.json"
        )
        with self.assertRaisesRegex(M3GateVerificationError, "file set mismatch"):
            verify_m3_gate_manifest(manifest)

    def test_artifact_map_rejects_extra_missing_and_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.txt"
            first.write_text("first\n", encoding="utf-8")
            digest = hashlib.sha256(first.read_bytes()).hexdigest()
            declared = {"first.txt": digest}
            self.assertEqual(
                verify_artifact_map(root, declared, excluded=frozenset()),
                1,
            )

            extra = root / "extra.txt"
            extra.write_text("extra\n", encoding="utf-8")
            with self.assertRaisesRegex(M3GateVerificationError, "extra"):
                verify_artifact_map(root, declared, excluded=frozenset())
            extra.unlink()

            with self.assertRaisesRegex(M3GateVerificationError, "missing"):
                verify_artifact_map(
                    root,
                    {**declared, "missing.txt": "0" * 64},
                    excluded=frozenset(),
                )

            link = root / "linked.txt"
            link.symlink_to(first.name)
            with self.assertRaisesRegex(M3GateVerificationError, "symlinks"):
                verify_artifact_map(
                    root,
                    {**declared, "linked.txt": digest},
                    excluded=frozenset(),
                )
            link.unlink()

            first.write_text("drift\n", encoding="utf-8")
            with self.assertRaisesRegex(M3GateVerificationError, "SHA-256"):
                verify_artifact_map(root, declared, excluded=frozenset())


if __name__ == "__main__":
    unittest.main()
