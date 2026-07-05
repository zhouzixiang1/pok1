"""Tests for _do_abandon_generation (B2 v125 fix helper).

Validates the shared abandon logic that MASTER_EXHAUSTED (tool_planning.py) and
CYCLE_TIMEOUT (orchestrator.py B3) now call directly instead of relying on the
orchestrator LLM to obey a plain-text directive.
"""

import asyncio

import tool_bot_management as tbm


def _run(coro):
    # A4 (2026-06-30): reset the abandon rate-limit before each test call so the
    # 60s cooldown doesn't block consecutive test abandons.
    tbm._LAST_ABANDON_TS[0] = 0.0
    tbm._LAST_ABANDON_TS[1] = ""
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

        next_dir = tmp_path / "national_v100"
        next_dir.mkdir()
        (next_dir / "main.py").write_text("x=1")
        monkeypatch.setattr(tbm, "get_bot_dir", lambda v: next_dir)
        monkeypatch.setattr(tbm, "git_dir_is_committed", lambda v: False)
        monkeypatch.setattr(tbm, "log_system_event", lambda *a, **k: None)

        result = _run(tbm._do_abandon_generation(reason="master_exhausted (4 fails)"))

        assert result["abandoned"] is True
        assert result["cleared_checkpoint"] is True
        assert result["removed_directory"] == "national_v100"
        assert result["reason"] == "master_exhausted (4 fails)"
        assert cleared == [True]          # clear_pipeline_checkpoint called
        assert not next_dir.exists()      # incomplete dir removed

    def test_no_checkpoint_uses_authoritative_next_version_floor(self, tmp_path, monkeypatch):
        # No checkpoint -> cleared_checkpoint stays False, but an orphaned
        # incomplete authoritative next dir is still cleaned up. Abandoned
        # version floors mean this is not always current_v + 1.
        monkeypatch.setattr(tbm, "read_pipeline_checkpoint", lambda: None)
        import evolution_core
        monkeypatch.setattr(evolution_core, "PIPELINE_STATE_FILE",
                            tmp_path / "nonexistent.json")  # .exists() == False
        monkeypatch.setattr(tbm, "clear_pipeline_checkpoint", lambda: None)
        monkeypatch.setattr(tbm, "find_current_v", lambda: 99)
        monkeypatch.setattr(tbm, "find_max_committed_v", lambda: 99)
        monkeypatch.setattr(tbm, "find_abandoned_version_floor", lambda: 100)
        monkeypatch.setattr(
            tbm,
            "compute_next_generation_v",
            lambda current_v, max_committed_v, abandoned_floor: max(
                current_v, max_committed_v, abandoned_floor
            ) + 1,
        )

        next_dir = tmp_path / "national_v101"
        next_dir.mkdir()
        (next_dir / "main.py").write_text("x=1")
        monkeypatch.setattr(tbm, "get_bot_dir", lambda v: next_dir)
        monkeypatch.setattr(tbm, "git_dir_is_committed", lambda v: False)
        monkeypatch.setattr(tbm, "log_system_event", lambda *a, **k: None)

        result = _run(tbm._do_abandon_generation(reason="no_ckpt"))

        assert result["abandoned"] is True
        assert result["cleared_checkpoint"] is False
        assert result["removed_directory"] == "national_v101"
        assert result["abandoned_v"] == 101
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

        next_dir = tmp_path / "national_v100"
        next_dir.mkdir()
        (next_dir / "main.py").write_text("x=1")
        (next_dir / ".completed").touch()  # COMPLETED — must be preserved
        monkeypatch.setattr(tbm, "get_bot_dir", lambda v: next_dir)
        monkeypatch.setattr(tbm, "git_dir_is_committed", lambda v: False)
        monkeypatch.setattr(tbm, "log_system_event", lambda *a, **k: None)

        result = _run(tbm._do_abandon_generation(reason="test"))

        assert result["abandoned"] is True
        assert result["removed_directory"] is None  # not removed (completed)
        assert next_dir.exists()                    # preserved

    def test_preserves_git_tracked_incomplete_dir(self, tmp_path, monkeypatch):
        # A git-tracked dir without a tag is a bare-commit recovery case, not
        # disposable scratch. abandon_generation may clear the checkpoint, but
        # must not rmtree committed code.
        monkeypatch.setattr(tbm, "read_pipeline_checkpoint",
                            lambda: {"next_v": 100, "source_v": 99, "stage": "master_planned"})
        import evolution_core
        fake_state = tmp_path / "pipeline_state.json"
        fake_state.write_text("{}")
        monkeypatch.setattr(evolution_core, "PIPELINE_STATE_FILE", fake_state)
        monkeypatch.setattr(tbm, "clear_pipeline_checkpoint", lambda: None)

        next_dir = tmp_path / "national_v100"
        next_dir.mkdir()
        (next_dir / "main.py").write_text("x=1")
        monkeypatch.setattr(tbm, "get_bot_dir", lambda v: next_dir)
        monkeypatch.setattr(tbm, "git_dir_is_committed", lambda v: True)
        monkeypatch.setattr(tbm, "log_system_event", lambda *a, **k: None)

        result = _run(tbm._do_abandon_generation(reason="test"))

        assert result["abandoned"] is True
        assert result["removed_directory"] is None
        assert result["abandoned_v"] == 100
        assert next_dir.exists()

    def test_generic_abandon_refuses_forward_only_reviewed_stage(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tbm, "read_pipeline_checkpoint",
                            lambda: {"next_v": 100, "source_v": 99, "stage": "reviewed"})
        import evolution_core
        fake_state = tmp_path / "pipeline_state.json"
        fake_state.write_text("{}")
        monkeypatch.setattr(evolution_core, "PIPELINE_STATE_FILE", fake_state)

        cleared = []
        monkeypatch.setattr(tbm, "clear_pipeline_checkpoint", lambda: cleared.append(True))

        next_dir = tmp_path / "national_v100"
        next_dir.mkdir()
        (next_dir / "main.py").write_text("x=1")
        monkeypatch.setattr(tbm, "get_bot_dir", lambda v: next_dir)
        monkeypatch.setattr(tbm, "git_dir_is_committed", lambda v: False)

        events = []
        monkeypatch.setattr(
            tbm,
            "log_system_event",
            lambda event_type, severity, message, data=None: events.append(
                (event_type, severity, message, data)
            ),
        )

        result = _run(tbm._do_abandon_generation(reason="abandon_generation"))

        assert result["abandoned"] is False
        assert result["blocked"] is True
        assert result["stage"] == "reviewed"
        assert result["next_tool"] == "run_critic"
        assert "run_critic" in result["directive"]
        assert cleared == []
        assert next_dir.exists()
        assert events[0][0] == "pipeline.abandon_refused_state_guard"

    def test_forced_abandon_still_allowed_after_reviewed_stage(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tbm, "read_pipeline_checkpoint",
                            lambda: {"next_v": 100, "source_v": 99, "stage": "reviewed"})
        import evolution_core
        fake_state = tmp_path / "pipeline_state.json"
        fake_state.write_text("{}")
        monkeypatch.setattr(evolution_core, "PIPELINE_STATE_FILE", fake_state)

        cleared = []
        monkeypatch.setattr(tbm, "clear_pipeline_checkpoint", lambda: cleared.append(True))

        next_dir = tmp_path / "national_v100"
        next_dir.mkdir()
        (next_dir / "main.py").write_text("x=1")
        monkeypatch.setattr(tbm, "get_bot_dir", lambda v: next_dir)
        monkeypatch.setattr(tbm, "git_dir_is_committed", lambda v: False)
        monkeypatch.setattr(tbm, "log_system_event", lambda *a, **k: None)

        result = _run(tbm._do_abandon_generation(reason="cycle_timeout"))

        assert result["abandoned"] is True
        assert result["cleared_checkpoint"] is True
        assert result["removed_directory"] == "national_v100"
        assert cleared == [True]
        assert not next_dir.exists()
