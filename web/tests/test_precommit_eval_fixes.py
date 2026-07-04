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

    def test_get_active_bots_finds_tagged_completed(self, tmp_path, monkeypatch):
        """get_active_bots returns directories with a tag-backed .completed sentinel."""
        from elo_daemon import get_active_bots
        import evolution_infra

        # Create a fake bot dir with .completed
        bots_dir = tmp_path / "bots"
        bots_dir.mkdir()
        bot_dir = bots_dir / "claude_v99"
        bot_dir.mkdir()
        (bot_dir / ".completed").touch()

        monkeypatch.setattr(evolution_infra, "BOTS_DIR", bots_dir)
        monkeypatch.setattr(evolution_infra, "_git", lambda *args, **kwargs: "bot-v99\n")

        result = get_active_bots()
        assert "claude_v99" in result

    def test_get_active_bots_skips_untagged_completed(self, tmp_path, monkeypatch):
        """get_active_bots does NOT trust .completed without a bot-vN tag."""
        from elo_daemon import get_active_bots
        import evolution_infra

        bots_dir = tmp_path / "bots"
        bots_dir.mkdir()
        bot_dir = bots_dir / "claude_v99"
        bot_dir.mkdir()
        (bot_dir / ".completed").touch()

        monkeypatch.setattr(evolution_infra, "BOTS_DIR", bots_dir)
        monkeypatch.setattr(evolution_infra, "_git", lambda *args, **kwargs: "")

        result = get_active_bots()
        assert "claude_v99" not in result

    def test_get_active_bots_restores_missing_completed_for_tagged_bot(self, tmp_path, monkeypatch):
        """A tagged bot dir missing gitignored .completed is restored and treated active."""
        from elo_daemon import get_active_bots
        import evolution_infra

        bots_dir = tmp_path / "bots"
        bots_dir.mkdir()
        bot_dir = bots_dir / "claude_v99"
        bot_dir.mkdir()

        monkeypatch.setattr(evolution_infra, "BOTS_DIR", bots_dir)
        monkeypatch.setattr(evolution_infra, "_git", lambda *args, **kwargs: "bot-v99\n")

        result = get_active_bots()
        assert result == ["claude_v99"]
        assert (bot_dir / ".completed").exists()

    def test_get_active_bots_does_not_restore_reaped_tagged_bot(self, tmp_path, monkeypatch):
        """A deliberately reaped tagged bot remains inactive across discovery calls."""
        from elo_daemon import get_active_bots
        import evolution_infra

        bots_dir = tmp_path / "bots"
        bots_dir.mkdir()
        bot_dir = bots_dir / "claude_v99"
        bot_dir.mkdir()
        results_dir = tmp_path / "results"
        results_dir.mkdir()
        (results_dir / "reaped_bots.jsonl").write_text(
            '{"bot":"claude_v99","version":99,"reason":"test"}\n',
            encoding="utf-8",
        )

        monkeypatch.setattr(evolution_infra, "BOTS_DIR", bots_dir)
        monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results_dir)
        monkeypatch.setattr(evolution_infra, "REAPED_BOTS_FILE", results_dir / "reaped_bots.jsonl")
        monkeypatch.setattr(evolution_infra, "_git", lambda *args, **kwargs: "bot-v99\n")

        result = get_active_bots()
        assert result == []
        assert not (bot_dir / ".completed").exists()

    @pytest.mark.asyncio
    async def test_reap_weakest_deactivates_without_moving_source(self, tmp_path, monkeypatch):
        """Reaping must not move tracked bot dirs and dirty the evolution worktree."""
        import evolution_infra
        import tool_bot_management as tbm

        bots_dir = tmp_path / "bots"
        results_dir = tmp_path / "web" / "core" / "results"
        replay_dir = results_dir / "match_replay"
        results_dir.mkdir(parents=True)
        replay_dir.mkdir()
        (bots_dir / "graveyard").mkdir(parents=True)
        for version in (1, 2):
            bot_dir = bots_dir / f"claude_v{version}"
            bot_dir.mkdir()
            (bot_dir / "main.py").write_text("print('bot')\n", encoding="utf-8")
            (bot_dir / ".completed").touch()
        (results_dir / "bot_stats.json").write_text(
            '{"claude_v1":{"games":1000},"claude_v2":{"games":1000}}\n',
            encoding="utf-8",
        )

        monkeypatch.setattr(evolution_infra, "BOTS_DIR", bots_dir)
        monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results_dir)
        monkeypatch.setattr(evolution_infra, "REAPED_BOTS_FILE", results_dir / "reaped_bots.jsonl")
        monkeypatch.setattr(evolution_infra, "_git", lambda *args, **kwargs: "bot-v1\nbot-v2\n")
        monkeypatch.setattr(tbm, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(tbm, "RESULTS_DIR", results_dir)
        monkeypatch.setattr(tbm, "REPLAY_DIR", replay_dir)
        monkeypatch.setattr(tbm, "MAX_ACTIVE_BOTS", 1)
        monkeypatch.setattr(
            tbm,
            "load_ratings",
            lambda: {
                "claude_v1": tbm.Glicko2Player(r=1200, rd=50),
                "claude_v2": tbm.Glicko2Player(r=1600, rd=50),
            },
        )
        monkeypatch.setattr(tbm, "load_h2h_avg_winrates", lambda: {"claude_v1": 0.4})
        monkeypatch.setattr(tbm, "load_strength_scores", lambda: {"claude_v1": 0.4})

        result = await tbm._do_reap_weakest(quiet=True)

        assert result["reaped"] is True
        assert result["culled"] == "claude_v1"
        assert result["reap_mode"] == "deactivate_completed_sentinel"
        assert (bots_dir / "claude_v1" / "main.py").exists()
        assert not (bots_dir / "claude_v1" / ".completed").exists()
        assert not (bots_dir / "graveyard" / "claude_v1").exists()
        assert evolution_infra.get_active_bots() == ["claude_v2"]

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
