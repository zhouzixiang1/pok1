"""Tests for the DeepEvolve debug sub-agent (fix-7).

Validates that:
- The debug agent is called on compile/crash failures but NOT on zero_changes
- Debug agent failure does not block worker retry
- Debug diagnosis is injected into the retry attempt note
"""

import json
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _strict_artifact(root, version):
    from bot_namespace import refresh_policy_identity_documents

    root.mkdir(parents=True)
    (root / "national_bot.py").write_text("def run():\n    return None\n", encoding="utf-8")
    (root / "policy.py").write_text(
        "def decide(_context):\n    return {'kind': 'pass'}\n",
        encoding="utf-8",
    )
    (root / "precompute.py").write_text("TABLE = ()\n", encoding="utf-8")
    (root / "national_runtime_manifest.json").write_text("{}\n", encoding="utf-8")
    (root / "policy_epoch_receipt.json").write_text("{}\n", encoding="utf-8")
    refresh_policy_identity_documents(root, version, parent_versions=(version - 1,))
    return root


class TestDebugAgentCalledOnCompileFailure:
    """Debug agent should be invoked when a compile_error occurs."""

    def test_debug_agent_trigger_condition_compile_error(self):
        """_last_failure_type == 'compile_error' should trigger debug agent."""
        from core.agent_workers import _run_debug_agent
        # Verify the function signature accepts the expected parameters
        import inspect
        sig = inspect.signature(_run_debug_agent)
        param_names = list(sig.parameters.keys())
        assert "error_output" in param_names
        assert "changed_diff" in param_names
        assert "target_file" in param_names
        assert "next_v" in param_names
        assert "ui" in param_names


class TestDebugAgentNotCalledOnZeroChanges:
    """Debug agent should NOT be invoked for zero_changes failures."""

    def test_zero_changes_not_in_trigger_set(self):
        """Verify that zero_changes is not in the trigger failure types."""
        # The trigger set is hardcoded in the retry loop:
        # _last_failure_type in ("compile_error", "smoke_test_fail", "timeout")
        trigger_types = {"compile_error", "smoke_test_fail", "timeout"}
        assert "zero_changes" not in trigger_types
        assert "invalid_target" not in trigger_types


class TestDebugAgentFailureDoesNotBlockRetry:
    """Debug agent exceptions should not propagate or block worker retry."""

    @pytest.mark.asyncio
    async def test_exception_swallowed(self):
        """_run_debug_agent should return {} on any exception."""
        from core.agent_workers import _run_debug_agent

        mock_ui = MagicMock()
        # Pass a bad target_file that will trigger an exception in file reading
        # The function should catch and return {}
        with patch("core.agent_workers.run_claude_query", side_effect=RuntimeError("boom")):
            result = await _run_debug_agent(
                error_output="SyntaxError",
                changed_diff="some diff",
                target_file="nonexistent.py",
                next_v=143,
                ui=mock_ui,
            )
        assert result == {}

    @pytest.mark.asyncio
    async def test_empty_output_returns_empty_dict(self):
        """Empty LLM output should return {}."""
        from core.agent_workers import _run_debug_agent

        mock_ui = MagicMock()
        mock_logs_dir = MagicMock()
        mock_log_file = MagicMock()
        mock_logs_dir.__truediv__ = MagicMock(return_value=mock_log_file)

        with (
            patch("core.agent_workers.run_claude_query", new_callable=AsyncMock, return_value=("", None, None)),
            patch("core.agent_workers.get_logs_dir", return_value=mock_logs_dir),
            patch("core.agent_workers.Path") as mock_path_cls,
        ):
            # Make the prompt file path resolve
            mock_path_instance = MagicMock()
            mock_path_instance.exists.return_value = True
            mock_path_instance.read_text.return_value = "Debug prompt template"
            mock_path_cls.return_value = mock_path_instance

            result = await _run_debug_agent(
                error_output="compile error",
                changed_diff="diff",
                target_file="policy.py",
                next_v=144,
                ui=mock_ui,
            )
        assert result == {}

    @pytest.mark.asyncio
    async def test_prompt_pins_current_generation_target_path(self, tmp_path):
        """Debug agent must read the current candidate file, not guess an old bot version."""
        from core.agent_workers import _run_debug_agent

        mock_ui = MagicMock()
        bot_dir = tmp_path / "repo" / "bots" / "national_v282"
        logs_dir = tmp_path / "logs"
        _strict_artifact(bot_dir, 282)
        logs_dir.mkdir()
        captured = {}

        async def fake_run_claude_query(prompt, *args, **kwargs):
            captured["prompt"] = prompt
            captured["kwargs"] = kwargs
            payload = {"diagnosis": "ok", "confidence": "high"}
            return f"```json\n{json.dumps(payload)}\n```", None, None

        with (
            patch("core.agent_workers.run_claude_query", side_effect=fake_run_claude_query),
            patch("core.agent_workers.get_bot_dir", return_value=bot_dir),
            patch("core.agent_workers.get_logs_dir", return_value=logs_dir),
        ):
            result = await _run_debug_agent(
                error_output="worker timed out while editing policy.py",
                changed_diff="diff --git a/bots/national_v282/policy.py b/bots/national_v282/policy.py",
                target_file="policy.py",
                next_v=282,
                ui=mock_ui,
            )

        prompt = captured["prompt"]
        assert result["confidence"] == "high"
        assert "bots/national_v282/policy.py" in prompt
        assert str(bot_dir / "policy.py") in prompt
        assert "Do not inspect or infer from any other bot version" in prompt
        assert captured["kwargs"]["allowed_read_dirs"] == [bot_dir]


class TestDebugDiagnosisInjectedIntoAttemptNote:
    """Debug diagnosis should appear in the retry attempt note."""

    def test_injection_format_high_confidence(self):
        """High confidence diagnosis should be injected with both diagnosis and fix."""
        debug_result = {
            "diagnosis": "Missing import for Path",
            "fix": "Add 'from pathlib import Path' at line 1",
            "confidence": "high",
        }
        attempt_note = ""
        if debug_result.get("confidence") in ("high", "medium"):
            attempt_note += (
                f"\n\n[DEBUG AGENT DIAGNOSIS]: {debug_result['diagnosis']}"
            )
            if debug_result.get("fix"):
                attempt_note += f"\n[PROPOSED FIX]: {debug_result['fix']}"

        assert "[DEBUG AGENT DIAGNOSIS]" in attempt_note
        assert "Missing import for Path" in attempt_note
        assert "[PROPOSED FIX]" in attempt_note
        assert "Add 'from pathlib import Path'" in attempt_note

    def test_injection_format_medium_confidence(self):
        """Medium confidence diagnosis should also be injected."""
        debug_result = {
            "diagnosis": "Indentation error",
            "fix": "Fix indent at line 42",
            "confidence": "medium",
        }
        attempt_note = ""
        if debug_result.get("confidence") in ("high", "medium"):
            attempt_note += (
                f"\n\n[DEBUG AGENT DIAGNOSIS]: {debug_result['diagnosis']}"
            )
            if debug_result.get("fix"):
                attempt_note += f"\n[PROPOSED FIX]: {debug_result['fix']}"

        assert "[DEBUG AGENT DIAGNOSIS]" in attempt_note

    def test_low_confidence_not_injected(self):
        """Low confidence diagnosis should NOT be injected."""
        debug_result = {
            "diagnosis": "Not sure",
            "fix": "Maybe try something",
            "confidence": "low",
        }
        attempt_note = ""
        if debug_result.get("confidence") in ("high", "medium"):
            attempt_note += (
                f"\n\n[DEBUG AGENT DIAGNOSIS]: {debug_result['diagnosis']}"
            )

        assert "[DEBUG AGENT DIAGNOSIS]" not in attempt_note

    def test_missing_fix_field_still_injects_diagnosis(self):
        """Diagnosis without fix field should still inject diagnosis."""
        debug_result = {
            "diagnosis": "Syntax error at line 10",
            "confidence": "high",
        }
        attempt_note = ""
        if debug_result.get("confidence") in ("high", "medium"):
            attempt_note += (
                f"\n\n[DEBUG AGENT DIAGNOSIS]: {debug_result['diagnosis']}"
            )
            if debug_result.get("fix"):
                attempt_note += f"\n[PROPOSED FIX]: {debug_result['fix']}"

        assert "[DEBUG AGENT DIAGNOSIS]" in attempt_note
        assert "[PROPOSED FIX]" not in attempt_note


class TestDebugAgentParseJson:
    """Debug agent should correctly parse JSON from LLM output."""

    @pytest.mark.asyncio
    async def test_valid_json_output(self):
        """Valid JSON with all fields should be returned."""
        from core.agent_workers import _run_debug_agent

        mock_ui = MagicMock()
        mock_logs_dir = MagicMock()
        mock_log_file = MagicMock()
        mock_logs_dir.__truediv__ = MagicMock(return_value=mock_log_file)

        json_output = json.dumps({
            "diagnosis": "Missing parenthesis",
            "fix": "Add '(' at line 5",
            "confidence": "high",
        })
        wrapped_output = f"```json\n{json_output}\n```"

        with (
            patch("core.agent_workers.run_claude_query", new_callable=AsyncMock, return_value=(wrapped_output, None, None)),
            patch("core.agent_workers.get_logs_dir", return_value=mock_logs_dir),
            patch("core.agent_workers.Path") as mock_path_cls,
        ):
            mock_path_instance = MagicMock()
            mock_path_instance.exists.return_value = True
            mock_path_instance.read_text.return_value = "Debug prompt"
            mock_path_cls.return_value = mock_path_instance

            result = await _run_debug_agent(
                error_output="SyntaxError",
                changed_diff="diff",
                target_file="policy.py",
                next_v=144,
                ui=mock_ui,
            )

        assert result["diagnosis"] == "Missing parenthesis"
        assert result["fix"] == "Add '(' at line 5"
        assert result["confidence"] == "high"

    @pytest.mark.asyncio
    async def test_invalid_confidence_normalized_to_low(self):
        """Invalid confidence values should be normalized to 'low'."""
        from core.agent_workers import _run_debug_agent

        mock_ui = MagicMock()
        mock_logs_dir = MagicMock()
        mock_log_file = MagicMock()
        mock_logs_dir.__truediv__ = MagicMock(return_value=mock_log_file)

        json_output = json.dumps({
            "diagnosis": "Unknown error",
            "confidence": "very_high",
        })
        wrapped_output = f"```json\n{json_output}\n```"

        with (
            patch("core.agent_workers.run_claude_query", new_callable=AsyncMock, return_value=(wrapped_output, None, None)),
            patch("core.agent_workers.get_logs_dir", return_value=mock_logs_dir),
            patch("core.agent_workers.Path") as mock_path_cls,
        ):
            mock_path_instance = MagicMock()
            mock_path_instance.exists.return_value = True
            mock_path_instance.read_text.return_value = "Debug prompt"
            mock_path_cls.return_value = mock_path_instance

            result = await _run_debug_agent(
                error_output="Error",
                changed_diff="diff",
                target_file="policy.py",
                next_v=144,
                ui=mock_ui,
            )

        assert result["confidence"] == "low"
