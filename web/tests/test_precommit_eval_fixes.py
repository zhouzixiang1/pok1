"""Integration tests for P0, P1, and P3 fixes.

P0: Reap signal + priority eval written BEFORE archive in commit_bot (tool_commit.py)
P1: Time-based bot list refresh every 30s in daemon (elo_daemon.py)
P3: Stage-aware timeout skip for verified/critic_checked stages (orchestrator.py)
"""

import inspect
import os
from pathlib import Path

import pytest


# ── P0: Reap Signal Ordering ─────────────────────────────────────────

class TestP0ReapSignalOrder:
    """P0: In commit_bot, reap_signal and priority_eval are written BEFORE archive_generation."""

    def _get_commit_bot_source(self):
        """Return the source text of tool_commit.py."""
        p = Path(__file__).resolve().parent.parent / "core" / "tool_commit.py"
        return p.read_text()

    def _get_commit_bot_body(self):
        """Return only the commit_bot function body from source file."""
        source = self._get_commit_bot_source()
        # Find the async def commit_bot block — @tool wraps it, so inspect won't work.
        # Read the function body by finding the def and tracking indentation.
        start = source.find("async def commit_bot(")
        assert start >= 0, "async def commit_bot not found in tool_commit.py"
        # Extract from function start to the next top-level function/class def
        lines = source[start:].splitlines()
        body_lines = []
        for i, line in enumerate(lines):
            if i > 0 and line and not line[0].isspace() and line.strip():
                break
            body_lines.append(line)
        return "\n".join(body_lines)

    def test_reap_signal_before_archive(self):
        source = self._get_commit_bot_body()
        reap_pos = source.find(".reap_signal")
        archive_pos = source.find("archive_generation")
        assert reap_pos >= 0, ".reap_signal not found in commit_bot source"
        assert archive_pos >= 0, "archive_generation not found in commit_bot source"
        assert reap_pos < archive_pos, (
            f"reap_signal (pos {reap_pos}) must appear BEFORE archive_generation (pos {archive_pos})"
        )

    def test_priority_eval_before_archive(self):
        source = self._get_commit_bot_body()
        priority_pos = source.find("priority_eval")
        archive_pos = source.find("archive_generation")
        assert priority_pos >= 0, "priority_eval not found in commit_bot source"
        assert archive_pos >= 0, "archive_generation not found in commit_bot source"
        assert priority_pos < archive_pos, (
            f"priority_eval (pos {priority_pos}) must appear BEFORE archive_generation (pos {archive_pos})"
        )

    def test_completed_before_reap_signal(self):
        source = self._get_commit_bot_body()
        completed_pos = source.find(".completed")
        reap_pos = source.find(".reap_signal")
        assert completed_pos >= 0, ".completed not found in commit_bot source"
        assert reap_pos >= 0, ".reap_signal not found in commit_bot source"
        assert completed_pos < reap_pos, (
            f".completed (pos {completed_pos}) must appear BEFORE .reap_signal (pos {reap_pos})"
        )


# ── P1: Time-Based Bot Refresh ───────────────────────────────────────

class TestP1TimeBasedRefresh:
    """P1: Daemon has time-based bot list refresh every 30s as a safety net."""

    def test_get_active_bots_finds_completed(self, tmp_path):
        """get_active_bots returns directories with .completed sentinel."""
        from elo_daemon import get_active_bots
        import elo_daemon

        # Create a fake bot dir with .completed
        bots_dir = tmp_path / "bots"
        bots_dir.mkdir()
        bot_dir = bots_dir / "claude_v99"
        bot_dir.mkdir()
        (bot_dir / ".completed").touch()

        # Patch BOTS_DIR
        original = elo_daemon.BOTS_DIR
        try:
            elo_daemon.BOTS_DIR = bots_dir
            result = get_active_bots()
            assert "claude_v99" in result
        finally:
            elo_daemon.BOTS_DIR = original

    def test_get_active_bots_skips_incomplete(self, tmp_path):
        """get_active_bots does NOT return directories without .completed."""
        from elo_daemon import get_active_bots
        import elo_daemon

        # Create a fake bot dir WITHOUT .completed
        bots_dir = tmp_path / "bots"
        bots_dir.mkdir()
        bot_dir = bots_dir / "claude_v99"
        bot_dir.mkdir()
        # No .completed file

        original = elo_daemon.BOTS_DIR
        try:
            elo_daemon.BOTS_DIR = bots_dir
            result = get_active_bots()
            assert "claude_v99" not in result
        finally:
            elo_daemon.BOTS_DIR = original

    def test_refresh_timer_variable_exists(self):
        """Daemon source contains the last_bot_refresh_time variable."""
        source = Path(__file__).resolve().parent.parent / "core" / "elo_daemon.py"
        text = source.read_text()
        assert "last_bot_refresh_time" in text

    def test_refresh_interval_is_30(self):
        """Time-based refresh check uses 30-second interval."""
        source = Path(__file__).resolve().parent.parent / "core" / "elo_daemon.py"
        text = source.read_text()
        # Find the time-based refresh block and verify it uses 30
        assert "last_bot_refresh_time >= 30" in text


# ── P3: Stage-Aware Timeout ──────────────────────────────────────────

class TestP3StageAwareTimeout:
    """P3: Timeout handler skips kill when pipeline is at verified/critic_checked stage."""

    def _read_orchestrator_source(self):
        return Path(__file__).resolve().parent.parent / "core" / "orchestrator.py"

    def test_timeout_skip_stages(self):
        """Timeout handler checks for verified and critic_checked stages."""
        source = self._read_orchestrator_source().read_text()
        # Find the stage-aware timeout block
        assert '"verified"' in source, 'Stage "verified" not found in timeout handler'
        assert '"critic_checked"' in source, 'Stage "critic_checked" not found in timeout handler'
        # Verify they appear in the same conditional block as the timeout handling
        timeout_block_start = source.find("asyncio.TimeoutError")
        assert timeout_block_start >= 0
        verified_in_timeout = source.find('"verified"', timeout_block_start)
        critic_in_timeout = source.find('"critic_checked"', timeout_block_start)
        assert verified_in_timeout > timeout_block_start, (
            '"verified" must appear after the TimeoutError handler'
        )
        assert critic_in_timeout > timeout_block_start, (
            '"critic_checked" must appear after the TimeoutError handler'
        )

    def test_watchdog_still_active(self):
        """P3 does not disable the watchdog — WATCHDOG_TIMEOUT still referenced."""
        source = self._read_orchestrator_source().read_text()
        assert "WATCHDOG_TIMEOUT" in source, "WATCHDOG_TIMEOUT reference removed — watchdog disabled"
        assert "_watchdog_coroutine" in source, "_watchdog_coroutine function removed"

    def test_no_blanket_extension(self):
        """CYCLE_TIMEOUT does not add a blanket +300 or +360 extension."""
        source = self._read_orchestrator_source().read_text()
        # CYCLE_TIMEOUT should be a fixed value, not CYCLE_TIMEOUT + 300 or similar
        assert "CYCLE_TIMEOUT + 300" not in source, (
            "Blanket +300 extension found — P3 should use stage-aware skip, not blanket extension"
        )
        assert "CYCLE_TIMEOUT + 360" not in source, (
            "Blanket +360 extension found — P3 should use stage-aware skip, not blanket extension"
        )
        # Verify CYCLE_TIMEOUT is a simple constant assignment (not dynamically inflated)
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("CYCLE_TIMEOUT ="):
                # Should be a plain integer, not a sum/expression
                assert "+ 3" not in stripped and "+ 3" not in stripped, (
                    f"CYCLE_TIMEOUT should not be inflated: {stripped}"
                )
                break
