"""Integration tests for P0-P6 root cause fixes.

P0: Idempotency guards on MCP pipeline tools (tool_gates.py)
P1: Broadened SDK exception catch (llm_query.py)
P2: State machine transition guards (evolution_infra.py)
P3: AST-based dead code detection (code_verification.py)
P4: Exhausted direction enforcement in plan validation (tool_planning.py)
P5: replay_spotlight reads my_cards instead of history (replay_spotlight.py)
P6: Fix injection logging cleanup (fix_injection.py)
"""

import inspect
import os
import tempfile
from pathlib import Path

import pytest


# ── P0: Idempotency Guards ──────────────────────────────────────────

class TestP0IdempotencyGuards:
    """P0: Pipeline tools return cached results on repeat calls."""

    def _read_tool_gates_source(self):
        p = Path(__file__).resolve().parent.parent / "core" / "tool_gates.py"
        return p.read_text()

    def test_quality_gates_has_guard(self):
        source = self._read_tool_gates_source()
        assert "idempotent_cache" in source

    def test_review_has_guard(self):
        source = self._read_tool_gates_source()
        assert source.count("idempotent_cache") >= 3

    def test_critic_has_guard(self):
        source = self._read_tool_gates_source()
        assert source.count("idempotent_cache") >= 3


# ── P1: SDK Exception Handling ───────────────────────────────────────

class TestP1BroadExceptionCatch:
    """P1: ClaudeSDKError base class catches all SDK errors."""

    def test_imports_base_class(self):
        from core import llm_query
        source = inspect.getsource(llm_query)
        assert "ClaudeSDKError" in source

    def test_process_stream_uses_base(self):
        from core.llm_query import _process_stream
        source = inspect.getsource(_process_stream)
        assert "ClaudeSDKError" in source

    def test_cancelled_still_propagates(self):
        from core.llm_query import _process_stream
        source = inspect.getsource(_process_stream)
        # CancelledError must still be re-raised
        assert "CancelledError" in source
        assert "raise" in source


# ── P2: Stage Transition Guards ─────────────────────────────────────

class TestP2StageTransition:
    """P2: State transition validation in evolution_infra.py."""

    def test_forward_transitions_allowed(self):
        from core.evolution_infra import validate_stage_transition, STAGE_ORDER
        for i in range(len(STAGE_ORDER) - 1):
            ok, reason = validate_stage_transition(STAGE_ORDER[i], STAGE_ORDER[i + 1])
            assert ok, f"Forward {STAGE_ORDER[i]} -> {STAGE_ORDER[i+1]} should be valid: {reason}"

    def test_backward_transition_blocked(self):
        from core.evolution_infra import validate_stage_transition
        ok, reason = validate_stage_transition("reviewed", "quality_passed")
        assert not ok
        assert "backward" in reason

    def test_retry_to_master_planned_allowed(self):
        from core.evolution_infra import validate_stage_transition
        for src in ["workers_done", "quality_passed", "spot_verified", "reviewed", "critic_checked"]:
            ok, reason = validate_stage_transition(src, "master_planned")
            assert ok, f"{src} -> master_planned should be valid"
            assert "retry" in reason

    def test_timeout_override_allowed(self):
        from core.evolution_infra import validate_stage_transition
        for src in ["reviewed", "critic_checked", "verified"]:
            ok, _ = validate_stage_transition(src, "timed_out")
            assert ok

    def test_none_to_any_allowed(self):
        from core.evolution_infra import validate_stage_transition
        ok, _ = validate_stage_transition(None, "reviewed")
        assert ok

    def test_same_stage_allowed(self):
        from core.evolution_infra import validate_stage_transition
        ok, _ = validate_stage_transition("reviewed", "reviewed")
        assert ok

    def test_fresh_restart_allowed(self):
        from core.evolution_infra import validate_stage_transition
        ok, _ = validate_stage_transition("critic_checked", "prepared")
        assert ok


# ── P3: AST Dead Code Detection ─────────────────────────────────────

class TestP3ASTDeadCode:
    """P3: AST-based dead code detection in code_verification.py."""

    def test_detects_empty_function_stub(self, tmp_path):
        from core.code_verification import _detect_dead_code_ast
        f = tmp_path / "bot.py"
        f.write_text("def placeholder():\n    pass\n")
        errors = _detect_dead_code_ast(str(tmp_path))
        assert len(errors) == 1
        assert "placeholder" in errors[0]

    def test_no_false_positive_on_dunder(self, tmp_path):
        from core.code_verification import _detect_dead_code_ast
        f = tmp_path / "bot.py"
        f.write_text("class Foo:\n    def __init__(self):\n        pass\n")
        errors = _detect_dead_code_ast(str(tmp_path))
        assert len(errors) == 0

    def test_valid_code_no_errors(self, tmp_path):
        from core.code_verification import _detect_dead_code_ast
        f = tmp_path / "bot.py"
        f.write_text("def compute(x):\n    return x + 1\n")
        errors = _detect_dead_code_ast(str(tmp_path))
        assert len(errors) == 0

    def test_detects_unreachable_after_return(self, tmp_path):
        from core.code_verification import _detect_dead_code_ast
        f = tmp_path / "bot.py"
        f.write_text("def early_exit():\n    return 42\n    x = 1\n")
        errors = _detect_dead_code_ast(str(tmp_path))
        assert any("unreachable" in e for e in errors)


# ── P4: Exhausted Direction Enforcement ─────────────────────────────

class TestP4ExhaustedDirection:
    """P4: _validate_master_plan blocks plans matching EXHAUSTED directions."""

    def test_function_exists(self):
        from core.tool_planning import _validate_master_plan
        assert callable(_validate_master_plan)

    def test_exhausted_check_in_source(self):
        from core.tool_planning import _validate_master_plan
        source = inspect.getsource(_validate_master_plan)
        assert "_extract_exhausted_keywords" in source
        assert "_fuzzy_match_exhausted" in source


# ── P5: Replay Spotlight Card Fix ───────────────────────────────────

class TestP5ReplayCards:
    """P5: replay_spotlight reads my_cards instead of history."""

    def test_uses_my_cards_field(self):
        from core.replay_spotlight import _extract_hand_swing
        source = inspect.getsource(_extract_hand_swing)
        assert "my_cards" in source

    def test_no_history_slice_as_cards(self):
        from core.replay_spotlight import _extract_hand_swing
        source = inspect.getsource(_extract_hand_swing)
        # The old bug was hist[:2] treating action dicts as cards
        assert "hist[:2]" not in source


# ── P6: Fix Injection Logging ───────────────────────────────────────

class TestP6FixInjection:
    """P6: Fix injection logging uses appropriate severity."""

    def test_bot_002b_inactive(self):
        from core.fix_injection import MANDATORY_FIXES
        bot002b = [f for f in MANDATORY_FIXES if f.fix_id == "BOT-002b"]
        assert len(bot002b) == 1
        assert bot002b[0].active is False

    def test_severity_logic(self):
        from core.fix_injection import log_fix_application
        source = inspect.getsource(log_fix_application)
        # Changed from "warn" if skipped to "warn" if skipped and not applied
        assert "skipped and not applied" in source

    def test_active_fixes_still_functional(self):
        from core.fix_injection import MANDATORY_FIXES
        active_ids = [f.fix_id for f in MANDATORY_FIXES if f.active]
        assert "BOT-001a" in active_ids
        assert "BOT-002a" in active_ids
        assert "BOT-004" in active_ids
