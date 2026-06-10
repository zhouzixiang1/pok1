"""Tests for B3: Dynamic Regression Test Generation from worker diffs.

Verifies heuristic diff parsing, scenario generation, persistence, and merge logic.
"""

import json
import time
import pytest
from pathlib import Path


# ── parse_constant_changes ────────────────────────────────────────────────────

class TestParseConstantChanges:
    def test_changed_numeric_constant(self):
        from decision_tester import parse_constant_changes
        diff = """--- a/constants.py
+++ b/constants.py
-FOLD_FLOP_WEAK = 0.20
+FOLD_FLOP_WEAK = 0.30
"""
        changes = parse_constant_changes(diff)
        assert len(changes) == 1
        assert changes[0]["name"] == "FOLD_FLOP_WEAK"
        assert changes[0]["old_numeric"] == pytest.approx(0.20)
        assert changes[0]["new_numeric"] == pytest.approx(0.30)

    def test_new_constant(self):
        from decision_tester import parse_constant_changes
        diff = """--- a/constants.py
+++ b/constants.py
+NEW_THRESHOLD = 0.55
"""
        changes = parse_constant_changes(diff)
        assert len(changes) == 1
        assert changes[0]["name"] == "NEW_THRESHOLD"
        assert changes[0]["old_value"] is None
        assert changes[0]["new_numeric"] == pytest.approx(0.55)

    def test_unchanged_constant_not_reported(self):
        from decision_tester import parse_constant_changes
        diff = """--- a/constants.py
+++ b/constants.py
-BIG_BLIND = 100
+BIG_BLIND = 100
"""
        changes = parse_constant_changes(diff)
        assert len(changes) == 0

    def test_multiple_changes(self):
        from decision_tester import parse_constant_changes
        diff = """--- a/constants.py
+++ b/constants.py
-RAISE_RATIO_FLOP = 0.60
+RAISE_RATIO_FLOP = 0.75
-FOLD_TURN_WEAK = 0.25
+FOLD_TURN_WEAK = 0.30
+NEW_CONST = 42
"""
        changes = parse_constant_changes(diff)
        assert len(changes) == 3
        names = [c["name"] for c in changes]
        assert "RAISE_RATIO_FLOP" in names
        assert "FOLD_TURN_WEAK" in names
        assert "NEW_CONST" in names

    def test_integer_constant(self):
        from decision_tester import parse_constant_changes
        diff = """--- a/constants.py
+++ b/constants.py
-TOTAL_HANDS = 50
+TOTAL_HANDS = 70
"""
        changes = parse_constant_changes(diff)
        assert len(changes) == 1
        assert changes[0]["old_numeric"] == pytest.approx(50)
        assert changes[0]["new_numeric"] == pytest.approx(70)

    def test_empty_diff(self):
        from decision_tester import parse_constant_changes
        changes = parse_constant_changes("")
        assert changes == []

    def test_context_lines_ignored(self):
        """Lines starting with space (context) should not be parsed as changes."""
        from decision_tester import parse_constant_changes
        diff = """--- a/constants.py
+++ b/constants.py
 SOME_OTHER = 5
-FOLD_FLOP_WEAK = 0.20
+FOLD_FLOP_WEAK = 0.30
"""
        changes = parse_constant_changes(diff)
        assert len(changes) == 1
        assert changes[0]["name"] == "FOLD_FLOP_WEAK"


# ── parse_new_branches ────────────────────────────────────────────────────────

class TestParseNewBranches:
    def test_new_if_branch(self):
        from decision_tester import parse_new_branches
        diff = """--- a/postflop.py
+++ b/postflop.py
+        if board_wetness > 0.6:
+            return "wet"
"""
        branches = parse_new_branches(diff)
        assert len(branches) == 1
        assert branches[0]["keyword"] == "if"
        assert "board_wetness" in branches[0]["condition"]

    def test_new_elif_branch(self):
        from decision_tester import parse_new_branches
        diff = """--- a/strategy.py
+++ b/strategy.py
+        elif strength > 0.5:
+            sizing = 0.6
"""
        branches = parse_new_branches(diff)
        assert len(branches) == 1
        assert branches[0]["keyword"] == "elif"

    def test_multiple_branches(self):
        from decision_tester import parse_new_branches
        diff = """--- a/f.py
+++ b/f.py
+    if x > 1:
+        pass
+    elif x > 0:
+        pass
+    if y < 0:
+        pass
"""
        branches = parse_new_branches(diff)
        assert len(branches) == 3

    def test_existing_branch_not_counted(self):
        """Lines starting with space are context, not new additions."""
        from decision_tester import parse_new_branches
        diff = """--- a/f.py
+++ b/f.py
     if existing > 0:
-        return old
+        return new
"""
        branches = parse_new_branches(diff)
        assert len(branches) == 0


# ── generate_scenarios_from_diff ──────────────────────────────────────────────

class TestGenerateScenariosFromDiff:
    def test_generates_from_constant_change(self):
        from decision_tester import generate_scenarios_from_diff
        diff = """--- a/constants.py
+++ b/constants.py
-FOLD_FLOP_WEAK = 0.20
+FOLD_FLOP_WEAK = 0.30
"""
        scenarios = generate_scenarios_from_diff(diff)
        assert len(scenarios) >= 1
        s = scenarios[0]
        assert "dyn_const_" in s["id"]
        assert "FOLD_FLOP_WEAK" in s["description"]
        assert "input" in s
        assert "forbidden_actions" in s
        assert s["severity"] == "advisory"

    def test_generates_from_new_branch(self):
        from decision_tester import generate_scenarios_from_diff
        diff = """--- a/postflop.py
+++ b/postflop.py
+    if board_wetness > 0.6:
+        return "wet"
"""
        scenarios = generate_scenarios_from_diff(diff)
        assert len(scenarios) >= 1
        branch_scenarios = [s for s in scenarios if "dyn_branch_" in s["id"]]
        assert len(branch_scenarios) >= 1

    def test_mixed_diff(self):
        from decision_tester import generate_scenarios_from_diff
        diff = """--- a/constants.py
+++ b/constants.py
-RAISE_RATIO_FLOP = 0.60
+RAISE_RATIO_FLOP = 0.75
--- a/strategy.py
+++ b/strategy.py
+    if strength > 0.8:
+        sizing *= 1.5
"""
        scenarios = generate_scenarios_from_diff(diff)
        const_scenarios = [s for s in scenarios if "dyn_const_" in s["id"]]
        branch_scenarios = [s for s in scenarios if "dyn_branch_" in s["id"]]
        assert len(const_scenarios) >= 1
        assert len(branch_scenarios) >= 1

    def test_empty_diff(self):
        from decision_tester import generate_scenarios_from_diff
        scenarios = generate_scenarios_from_diff("")
        assert scenarios == []

    def test_scenario_has_valid_input(self):
        from decision_tester import generate_scenarios_from_diff
        diff = """--- a/constants.py
+++ b/constants.py
+EQR_AIR_IP = 0.65
"""
        scenarios = generate_scenarios_from_diff(diff)
        assert len(scenarios) >= 1
        inp = scenarios[0]["input"]
        assert "my_id" in inp
        assert "my_cards" in inp
        assert "public_cards" in inp
        assert "history" in inp
        assert "num_players" in inp
        assert inp["num_players"] == 2

    def test_max_20_scenarios(self):
        """Should cap at 20 scenarios even from a large diff."""
        from decision_tester import generate_scenarios_from_diff
        lines = []
        for i in range(30):
            lines.append(f"-CONST_{i} = {i * 0.1:.1f}")
            lines.append(f"+CONST_{i} = {i * 0.1 + 0.05:.2f}")
        diff = "\n".join(lines)
        scenarios = generate_scenarios_from_diff(diff)
        assert len(scenarios) <= 20


# ── load/save/merge dynamic scenarios ─────────────────────────────────────────

class TestDynamicScenarioPersistence:
    def test_save_and_load(self, tmp_path, monkeypatch):
        from decision_tester import save_dynamic_scenarios, load_dynamic_scenarios
        import decision_tester
        monkeypatch.setattr(decision_tester, "DYNAMIC_SCENARIOS_FILE", tmp_path / "dyn.json")
        monkeypatch.setattr(decision_tester, "RESULTS_DIR", tmp_path)

        scenarios = [
            {"id": "test_1", "description": "Test scenario 1"},
            {"id": "test_2", "description": "Test scenario 2"},
        ]
        save_dynamic_scenarios(scenarios)
        loaded = load_dynamic_scenarios()
        assert len(loaded) == 2
        assert loaded[0]["id"] == "test_1"

    def test_load_missing_file(self, tmp_path, monkeypatch):
        import decision_tester
        from decision_tester import load_dynamic_scenarios
        monkeypatch.setattr(decision_tester, "DYNAMIC_SCENARIOS_FILE", tmp_path / "nonexistent.json")
        assert load_dynamic_scenarios() == []

    def test_load_corrupt_json(self, tmp_path, monkeypatch):
        import decision_tester
        from decision_tester import load_dynamic_scenarios
        f = tmp_path / "bad.json"
        f.write_text("not valid json{{{")
        monkeypatch.setattr(decision_tester, "DYNAMIC_SCENARIOS_FILE", f)
        assert load_dynamic_scenarios() == []

    def test_max_100_scenarios(self, tmp_path, monkeypatch):
        import decision_tester
        from decision_tester import save_dynamic_scenarios, load_dynamic_scenarios
        monkeypatch.setattr(decision_tester, "DYNAMIC_SCENARIOS_FILE", tmp_path / "dyn.json")
        monkeypatch.setattr(decision_tester, "RESULTS_DIR", tmp_path)

        # Create 150 scenarios — should be trimmed to 100
        scenarios = [{"id": f"s_{i}", "description": f"Scenario {i}"} for i in range(150)]
        save_dynamic_scenarios(scenarios)
        loaded = load_dynamic_scenarios()
        assert len(loaded) == 100
        # Should keep the LAST 100
        assert loaded[0]["id"] == "s_50"
        assert loaded[-1]["id"] == "s_149"

    def test_merge_deduplicates(self):
        from decision_tester import merge_dynamic_scenarios
        base = [{"id": "a"}, {"id": "b"}]
        dynamic = [{"id": "b", "new": True}, {"id": "c"}]
        merged = merge_dynamic_scenarios(base, dynamic)
        # "b" is already in base, so dynamic "b" is NOT appended (base wins)
        # "c" is new, so it gets appended
        assert len(merged) == 3
        ids = [s["id"] for s in merged]
        assert "a" in ids
        assert "b" in ids
        assert "c" in ids

    def test_merge_empty_dynamic(self):
        from decision_tester import merge_dynamic_scenarios
        base = [{"id": "a"}]
        merged = merge_dynamic_scenarios(base, [])
        assert merged == base

    def test_merge_none_dynamic(self):
        from decision_tester import merge_dynamic_scenarios
        base = [{"id": "a"}]
        merged = merge_dynamic_scenarios(base, None)
        assert merged == base


# ── Template matching ─────────────────────────────────────────────────────────

class TestTemplateMatching:
    def test_find_template_for_fold_constant(self):
        from decision_tester import _find_template_for_constant
        tpl = _find_template_for_constant("FOLD_FLOP_WEAK")
        assert tpl is not None
        assert "flop" in tpl.get("_covers", "")

    def test_find_template_for_preflop_constant(self):
        from decision_tester import _find_template_for_constant
        tpl = _find_template_for_constant("SB_OPEN_THRESHOLD")
        assert tpl is not None
        assert "preflop" in tpl.get("_covers", "")

    def test_find_template_for_unknown_constant(self):
        from decision_tester import _find_template_for_constant
        tpl = _find_template_for_constant("ZEBRA_UNKNOWN_XYZ")
        assert tpl is not None
        # Should get a default template
        assert "input" in tpl

    def test_infer_expectations_fold_threshold_raised(self):
        from decision_tester import _infer_expectations_from_change
        change = {"name": "FOLD_FLOP_WEAK", "old_numeric": 0.20, "new_numeric": 0.30}
        expected, forbidden = _infer_expectations_from_change(change)
        # Threshold raised → expected ["call", "fold"], no specific forbidden
        assert "call" in expected
        assert "fold" in expected

    def test_infer_expectations_fold_threshold_lowered(self):
        from decision_tester import _infer_expectations_from_change
        change = {"name": "FOLD_FLOP_WEAK", "old_numeric": 0.30, "new_numeric": 0.20}
        expected, forbidden = _infer_expectations_from_change(change)
        # Threshold lowered → tighter play, forbid allin
        assert "allin" in forbidden

    def test_infer_expectations_raise_change(self):
        from decision_tester import _infer_expectations_from_change
        change = {"name": "RAISE_RATIO_FLOP", "old_numeric": 0.60, "new_numeric": 0.75}
        expected, forbidden = _infer_expectations_from_change(change)
        assert "fold" in forbidden

    def test_infer_template_from_condition_preflop(self):
        from decision_tester import _infer_template_from_condition
        tpl = _infer_template_from_condition("if preflop_strength > 0.5:")
        assert "preflop" in tpl.get("_covers", "")

    def test_infer_template_from_condition_river(self):
        from decision_tester import _infer_template_from_condition
        tpl = _infer_template_from_condition("if river_bet_size > pot:")
        assert "river" in tpl.get("_covers", "")


# ── _make_base_input ──────────────────────────────────────────────────────────

class TestMakeBaseInput:
    def test_default_values(self):
        from decision_tester import _make_base_input
        inp = _make_base_input()
        assert inp["my_id"] == 0
        assert inp["num_players"] == 2
        assert inp["my_cards"] == []
        assert inp["public_cards"] == []
        assert inp["max_hand"] == 70

    def test_custom_values(self):
        from decision_tester import _make_base_input
        inp = _make_base_input(
            my_id=1, dealer_id=0, my_chips=19900,
            my_cards=[48, 44], public_cards=[4, 12],
            history=[{"round": 0, "player_id": 0, "action": 250}],
        )
        assert inp["my_id"] == 1
        assert inp["my_chips"] == 19900
        assert inp["my_cards"] == [48, 44]
        assert len(inp["history"]) == 1


# ── Integration: scenario format compatibility ────────────────────────────────

class TestScenarioCompatibility:
    def test_generated_scenario_runs_through_classify(self):
        """Verify generated scenario action format matches classify_action."""
        from decision_tester import classify_action
        assert classify_action(-1) == "fold"
        assert classify_action(0) == "call"
        assert classify_action(-2) == "allin"
        assert classify_action(100) == "raise"

    def test_template_scenarios_have_valid_structure(self):
        from decision_tester import TEMPLATE_SCENARIOS
        for tpl in TEMPLATE_SCENARIOS:
            assert "id" in tpl
            assert "input" in tpl
            assert "forbidden_actions" in tpl
            assert "_covers" in tpl
            inp = tpl["input"]
            assert inp["num_players"] == 2
            assert isinstance(inp["my_cards"], list)
            assert isinstance(inp["public_cards"], list)
            assert isinstance(inp["history"], list)
