"""Tests for _do_abandon_generation (B2 v125 fix helper).

Validates the shared abandon logic that MASTER_EXHAUSTED (tool_planning.py) and
CYCLE_TIMEOUT (orchestrator.py B3) now call directly instead of relying on the
orchestrator LLM to obey a plain-text directive.
"""

import asyncio

import tool_bot_management as tbm


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class TestDoAbandonGeneration:
    def test_clears_checkpoint_and_removes_incomplete_dir(self, tmp_path, monkeypatch):
        # Active checkpoint -> clear it + remove the incomplete next dir.
        monkeypatch.setattr(tbm, "read_pipeline_checkpoint",
                            lambda: {"next_v": 100, "source_v": 99, "stage": "master_planned"})
        import evolution_core
        fake_state = tmp_path / "pipeline_state.json"
        fake_state.write_text("{}")
        monkeypatch.setattr(evolution_core, "PIPELINE_STATE_FILE", fake_state)

        cleared = []
        monkeypatch.setattr(tbm, "clear_pipeline_checkpoint", lambda: cleared.append(True))

        next_dir = tmp_path / "claude_v100"
        next_dir.mkdir()
        (next_dir / "main.py").write_text("x=1")
        monkeypatch.setattr(tbm, "get_bot_dir", lambda v: next_dir)
        monkeypatch.setattr(tbm, "log_system_event", lambda *a, **k: None)

        result = _run(tbm._do_abandon_generation(reason="master_exhausted (4 fails)"))

        assert result["abandoned"] is True
        assert result["cleared_checkpoint"] is True
        assert result["removed_directory"] == "claude_v100"
        assert result["reason"] == "master_exhausted (4 fails)"
        assert cleared == [True]          # clear_pipeline_checkpoint called
        assert not next_dir.exists()      # incomplete dir removed

    def test_no_checkpoint_falls_back_to_current_v_plus_1(self, tmp_path, monkeypatch):
        # No checkpoint -> cleared_checkpoint stays False, but an orphaned
        # incomplete next dir is still cleaned up.
        monkeypatch.setattr(tbm, "read_pipeline_checkpoint", lambda: None)
        import evolution_core
        monkeypatch.setattr(evolution_core, "PIPELINE_STATE_FILE",
                            tmp_path / "nonexistent.json")  # .exists() == False
        monkeypatch.setattr(tbm, "clear_pipeline_checkpoint", lambda: None)
        monkeypatch.setattr(tbm, "find_current_v", lambda: 99)

        next_dir = tmp_path / "claude_v100"
        next_dir.mkdir()
        (next_dir / "main.py").write_text("x=1")
        monkeypatch.setattr(tbm, "get_bot_dir", lambda v: next_dir)
        monkeypatch.setattr(tbm, "log_system_event", lambda *a, **k: None)

        result = _run(tbm._do_abandon_generation(reason="no_ckpt"))

        assert result["abandoned"] is True
        assert result["cleared_checkpoint"] is False
        assert result["removed_directory"] == "claude_v100"
        assert not next_dir.exists()

    def test_preserves_completed_dir(self, tmp_path, monkeypatch):
        # A completed generation dir (.completed sentinel) must NOT be removed.
        monkeypatch.setattr(tbm, "read_pipeline_checkpoint",
                            lambda: {"next_v": 100, "source_v": 99, "stage": "master_planned"})
        import evolution_core
        fake_state = tmp_path / "pipeline_state.json"
        fake_state.write_text("{}")
        monkeypatch.setattr(evolution_core, "PIPELINE_STATE_FILE", fake_state)
        monkeypatch.setattr(tbm, "clear_pipeline_checkpoint", lambda: None)

        next_dir = tmp_path / "claude_v100"
        next_dir.mkdir()
        (next_dir / "main.py").write_text("x=1")
        (next_dir / ".completed").touch()  # COMPLETED — must be preserved
        monkeypatch.setattr(tbm, "get_bot_dir", lambda v: next_dir)
        monkeypatch.setattr(tbm, "log_system_event", lambda *a, **k: None)

        result = _run(tbm._do_abandon_generation(reason="test"))

        assert result["abandoned"] is True
        assert result["removed_directory"] is None  # not removed (completed)
        assert next_dir.exists()                    # preserved
