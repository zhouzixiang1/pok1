from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from bots.research_native_lab.common_contracts.cards import all_hole_combinations
from bots.research_native_lab.cfr_neural_search_m5.cfv.combo_index import (
    COMBOS,
    COMBO_COUNT,
    COMBO_REGISTRY_SHA256,
    COMBO_TO_INDEX,
    board_legal_mask,
)
from bots.research_native_lab.cfr_neural_search_m5.core.contracts import (
    M5_ROOT,
    _audit_untracked_m4_tree,
    _validate_cfv_semantics_payload,
    load_cfv_semantics,
    load_oracle_gate_contract,
    verify_m4_dependency,
)
from bots.research_native_lab.cfr_neural_search_m5.core.independence import (
    audit_route_b_source,
    read_validated_runtime_bytes,
    validate_runtime_input,
    verify_import_independence,
)
from bots.research_native_lab.cfr_neural_search_m5.tools.verify_foundation import (
    verify_foundation,
)


class M5FoundationTest(unittest.TestCase):
    def test_m4_full_tracked_byte_closure_and_original_gate(self):
        result = verify_m4_dependency()
        self.assertEqual(result["file_count"], 117)
        self.assertEqual(
            result["source_snapshot_sha256"],
            "d9f2067866e27a74766fb5cc5b1edabedd880b22efd48e8869e695853287d312",
        )

    def test_physical_combo_order_and_board_mask(self):
        self.assertEqual(len(COMBOS), COMBO_COUNT)
        self.assertEqual(len(COMBO_TO_INDEX), COMBO_COUNT)
        self.assertEqual(COMBOS[0], (0, 1))
        self.assertEqual(COMBOS[-1], (50, 51))
        self.assertEqual(COMBOS, tuple(sorted(COMBOS)))
        self.assertEqual(COMBOS, all_hole_combinations())
        self.assertEqual(
            COMBO_REGISTRY_SHA256,
            "4534e13c4bd7a32ebb621433f5b08344b2bb81a04f3c78c2840cd9362bddf89a",
        )
        mask = board_legal_mask((0, 1, 2))
        self.assertEqual(len(mask), COMBO_COUNT)
        self.assertEqual(sum(mask), 1176)
        self.assertFalse(mask[COMBO_TO_INDEX[(0, 51)]])
        self.assertTrue(mask[COMBO_TO_INDEX[(3, 4)]])

    def test_semantics_are_range_conditioned_vector_cfv(self):
        semantics = load_cfv_semantics()
        self.assertEqual(semantics["input"]["private_ranges_shape"], [2, 1326])
        self.assertEqual(semantics["target"]["shape"], [2, 1326])
        self.assertTrue(semantics["target"]["omit_own_reach"])
        self.assertTrue(semantics["mask"]["zero_own_reach_remains_evaluable"])
        self.assertEqual(
            semantics["zero_sum"]["equation"],
            "dot(beta_0,C_0)+dot(beta_1,C_1)=0",
        )
        self.assertIn("sampled_private_deal_scalar", semantics["forbidden_targets"])
        self.assertIn("scalar_equity_broadcast", semantics["forbidden_targets"])
        self.assertIn(
            "small_blind_player", semantics["input"]["public_state_fields"]
        )
        self.assertNotIn("button", semantics["input"]["public_state_fields"])

        changed = deepcopy(semantics)
        changed["chance_weighting"]["public_prefix_chance"] = "multiply_again"
        with self.assertRaisesRegex(ValueError, "chance-weighting"):
            _validate_cfv_semantics_payload(changed)
        changed = deepcopy(semantics)
        changed["action_slots"][0], changed["action_slots"][1] = (
            changed["action_slots"][1],
            changed["action_slots"][0],
        )
        with self.assertRaisesRegex(ValueError, "action-slot"):
            _validate_cfv_semantics_payload(changed)

    def test_independence_import_and_runtime_path_gate(self):
        result = verify_import_independence()
        self.assertGreaterEqual(result["source_file_count"], 8)
        self.assertEqual(validate_runtime_input(M5_ROOT), M5_ROOT)
        self.assertFalse(
            any("online_solver.depth_limited" in module for module in result["resolved_imports"])
        )
        semantics_path = M5_ROOT / "contracts" / "cfv_semantics_v1.json"
        self.assertTrue(read_validated_runtime_bytes(semantics_path).startswith(b"{"))
        prefix = "bots.research_native_lab.cfr_neural_search_m5"
        self.assertEqual(
            audit_route_b_source(
                "from .core import contracts\n",
                current_module=f"{prefix}.probe",
            ),
            (f"{prefix}.core",),
        )
        with self.assertRaisesRegex(ValueError, "non-allowlisted"):
            audit_route_b_source(
                "from ..cfr_neural_search import blueprint\n",
                current_module=f"{prefix}.probe",
            )
        with self.assertRaisesRegex(ValueError, "dynamic"):
            audit_route_b_source(
                "__import__('sever.bot_adapter')\n",
                current_module=f"{prefix}.probe",
            )
        with self.assertRaisesRegex(ValueError, "dynamic"):
            audit_route_b_source(
                "loader = __import__\nloader('sever.bot_adapter')\n",
                current_module=f"{prefix}.probe",
            )
        with self.assertRaisesRegex(ValueError, "dynamic"):
            audit_route_b_source(
                "__builtins__['__import__']('sever.bot_adapter')\n",
                current_module=f"{prefix}.probe",
            )
        with self.assertRaisesRegex(ValueError, "reflection|dynamic"):
            audit_route_b_source(
                "globals()['__builtins__']['__import__']('sever.bot_adapter')\n",
                current_module=f"{prefix}.probe",
            )
        with self.assertRaisesRegex(ValueError, "dynamic"):
            audit_route_b_source(
                "import importlib\nimportlib.import_module('sever.bot_adapter')\n",
                current_module=f"{prefix}.probe",
            )
        with self.assertRaisesRegex(ValueError, "forbidden dependency"):
            audit_route_b_source(
                "import sever.bot_adapter\n",
                current_module=f"{prefix}.probe",
            )
        with tempfile.TemporaryDirectory(dir=M5_ROOT) as directory:
            jump = Path(directory) / "jump"
            jump.symlink_to("/etc", target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink component"):
                validate_runtime_input(jump / "hosts")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "outside the repository"):
                validate_runtime_input(Path(directory))

    def test_m4_shadow_source_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tracked = root / "tracked.py"
            tracked.write_text("pass\n", encoding="utf-8")
            self.assertEqual(
                _audit_untracked_m4_tree(root, frozenset({"tracked.py"})), 0
            )
            shadow = root / "shadow.py"
            shadow.write_text("pass\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "shadows"):
                _audit_untracked_m4_tree(root, frozenset({"tracked.py"}))
            shadow.unlink()
            runtime = root / "runtime_outputs"
            runtime.mkdir()
            (runtime / "receipt.json").write_text("{}\n", encoding="utf-8")
            self.assertEqual(
                _audit_untracked_m4_tree(root, frozenset({"tracked.py"})), 1
            )
            (runtime / "payload.py").write_text("pass\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "importable code"):
                _audit_untracked_m4_tree(root, frozenset({"tracked.py"}))

    def test_foundation_gate_has_no_label_or_training_authority(self):
        result = verify_foundation()
        self.assertEqual(result["status"], "passed_no_labels_no_training")
        self.assertEqual(result["private_combo_count"], 1326)
        oracle = load_oracle_gate_contract()
        self.assertFalse(oracle["authority"]["label_generation_authorized"])
        self.assertFalse(oracle["authority"]["training_authorized"])
        self.assertFalse(oracle["authority"]["online_tcp_authorized"])


if __name__ == "__main__":
    unittest.main()
