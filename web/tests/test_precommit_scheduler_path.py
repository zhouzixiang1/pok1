"""Tests for tool_eval.py dual-path precommit eval (scheduler vs serial fallback)."""

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure imports resolve
import sys
from pathlib import Path as _Path

_PROJECT_ROOT = _Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "web" / "core"))
sys.path.insert(0, str(_PROJECT_ROOT / "web" / "server"))

from tool_eval import BattleSchedulerClient, run_precommit_eval as _run_precommit_eval_tool

# The @tool decorator wraps the function in an SdkMcpTool object.
# Tests need the raw async handler.
run_precommit_eval = _run_precommit_eval_tool.handler


# ── Fixtures ──

@pytest.fixture(autouse=True)
def patch_scheduler_files(monkeypatch, tmp_path):
    """Redirect scheduler file paths into tmp_path so tests are isolated."""
    import battle_scheduler

    monkeypatch.setattr(battle_scheduler, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(battle_scheduler, "BATTLE_JOBS_FILE", tmp_path / "battle_jobs.jsonl")
    monkeypatch.setattr(battle_scheduler, "BATTLE_CLAIMED_FILE", tmp_path / "battle_jobs.claimed")
    monkeypatch.setattr(battle_scheduler, "BATTLE_RESULTS_FILE", tmp_path / "battle_results.jsonl")


@pytest.fixture
def mock_ui():
    """Return a mock UI object with log_history."""
    ui = MagicMock()
    ui.log_history = MagicMock()
    return ui


@pytest.fixture
def mock_checkpoint():
    """Return a mock checkpoint dict that passes all gates."""
    return {
        "version": 99,
        "source_v": 98,
        "quality_gate": {"passed": True},
        "review_gate": {"passed": True},
        "critic_gate": {"passed": True},
    }


@pytest.fixture
def fake_bots(tmp_path, monkeypatch):
    """Create fake bot directories and patch _bot_main to return them."""
    bots_dir = tmp_path / "bots"
    bots_dir.mkdir()
    for name in ("claude_v99", "claude_v98", "claude_v50"):
        d = bots_dir / name
        d.mkdir()
        (d / "main.py").write_text("# fake bot")

    def _fake_bot_main(name):
        return bots_dir / name / "main.py"

    monkeypatch.setattr("tool_eval._bot_main", _fake_bot_main)
    return bots_dir


@pytest.fixture
def fake_opponents(monkeypatch):
    """Patch _select_precommit_opponents to return a deterministic list."""
    ops = [
        {"name": "claude_v98", "reason": "parent"},
        {"name": "claude_v50", "reason": "top_opponent"},
    ]
    monkeypatch.setattr("tool_eval._select_precommit_opponents", lambda _v, _sv: ops)
    return ops


# ── BattleSchedulerClient unit tests ──

class TestBattleSchedulerClient:
    @pytest.mark.asyncio
    async def test_is_available_delegates_to_sync_function(self, monkeypatch):
        called = []

        def fake_is_capable():
            called.append(True)
            return True

        monkeypatch.setattr("tool_eval.is_daemon_scheduler_capable", fake_is_capable)
        client = BattleSchedulerClient()
        result = await client.is_available()
        assert result is True
        assert called == [True]

    @pytest.mark.asyncio
    async def test_submit_delegates_to_battle_scheduler(self, monkeypatch, tmp_path):
        import battle_scheduler

        submitted = []

        def fake_submit(jobs):
            submitted.extend(jobs)
            return [j.job_id for j in jobs]

        monkeypatch.setattr(battle_scheduler, "submit_jobs", fake_submit)
        client = BattleSchedulerClient()
        job = battle_scheduler.BattleJob(
            job_id="j1",
            bot_a_name="a",
            bot_b_name="b",
            bot_a_path=str(tmp_path / "a.py"),
            bot_b_path=str(tmp_path / "b.py"),
            n_pairs=3,
            submitted_at=time.time(),
        )
        ids = await client.submit([job])
        assert ids == ["j1"]
        assert len(submitted) == 1

    @pytest.mark.asyncio
    async def test_collect_delegates_to_battle_scheduler(self, monkeypatch):
        import battle_scheduler

        def fake_collect(job_ids):
            return {jid: {"wins_a": 2, "wins_b": 1, "draws": 0, "total": 3} for jid in job_ids}

        monkeypatch.setattr(battle_scheduler, "collect_results", fake_collect)
        client = BattleSchedulerClient()
        results = await client.collect(["j1", "j2"])
        assert len(results) == 2
        assert results["j1"]["wins_a"] == 2


# ── Dual-path integration tests ──

class TestSchedulerPathUsedWhenCapable:
    @pytest.mark.asyncio
    async def test_scheduler_path_used_when_capable(
        self, monkeypatch, fake_bots, fake_opponents, mock_checkpoint, mock_ui, tmp_path
    ):
        """When daemon is scheduler-capable, all opponents are submitted to scheduler."""
        import battle_scheduler

        monkeypatch.setattr("tool_eval.is_daemon_scheduler_capable", lambda: True)
        monkeypatch.setattr("tool_eval._matching_checkpoint", lambda _v, _sv: mock_checkpoint)
        monkeypatch.setattr("tool_eval._get_ui", lambda: mock_ui)
        monkeypatch.setattr("tool_eval._record_gate", lambda *a, **k: True)

        submitted_jobs = []

        def fake_submit(jobs):
            submitted_jobs.extend(jobs)
            return [j.job_id for j in jobs]

        monkeypatch.setattr(battle_scheduler, "submit_jobs", fake_submit)

        # Pre-seed results so collect finds them immediately
        def fake_collect(job_ids):
            return {
                jid: {
                    "wins_a": 2,
                    "wins_b": 1,
                    "draws": 0,
                    "total": 3,
                    "error": None,
                    "completed_at": time.time(),
                }
                for jid in job_ids
            }

        monkeypatch.setattr(battle_scheduler, "collect_results", fake_collect)

        # Patch mirror_battle so serial path is never hit
        monkeypatch.setattr(
            "engine.mirror_battle",
            lambda *a, **k: (_raise("should not be called"),),
        )

        result = await run_precommit_eval({"version": 99, "source_v": 98, "n_games": 3})
        text = result["content"][0]["text"]
        data = json.loads(text)

        assert data["passed"] is True
        assert len(submitted_jobs) == 2
        assert len(data["matchups"]) == 2
        for m in data["matchups"]:
            assert m["wins"] == 2
            assert m["losses"] == 1

    @pytest.mark.asyncio
    async def test_fallback_path_used_when_not_capable(
        self, monkeypatch, fake_bots, fake_opponents, mock_checkpoint, mock_ui
    ):
        """When daemon is NOT scheduler-capable, serial mirror_battle is used."""
        monkeypatch.setattr("tool_eval.is_daemon_scheduler_capable", lambda: False)
        monkeypatch.setattr("tool_eval._matching_checkpoint", lambda _v, _sv: mock_checkpoint)
        monkeypatch.setattr("tool_eval._get_ui", lambda: mock_ui)
        monkeypatch.setattr("tool_eval._record_gate", lambda *a, **k: True)

        serial_calls = []

        def fake_mirror(a, b, n_games=1, verbose=False, save_log=False):
            serial_calls.append((a, b, n_games))
            return ([2, 1], 0, n_games, None)

        monkeypatch.setattr("engine.mirror_battle", fake_mirror)

        result = await run_precommit_eval({"version": 99, "source_v": 98, "n_games": 3})
        text = result["content"][0]["text"]
        data = json.loads(text)

        assert data["passed"] is True
        assert len(serial_calls) == 2
        assert len(data["matchups"]) == 2

    @pytest.mark.asyncio
    async def test_partial_results_fallback(
        self, monkeypatch, fake_bots, fake_opponents, mock_checkpoint, mock_ui
    ):
        """If scheduler returns partial results, missing opponents fall back to serial."""
        import battle_scheduler

        monkeypatch.setattr("tool_eval.is_daemon_scheduler_capable", lambda: True)
        monkeypatch.setattr("tool_eval._matching_checkpoint", lambda _v, _sv: mock_checkpoint)
        monkeypatch.setattr("tool_eval._get_ui", lambda: mock_ui)
        monkeypatch.setattr("tool_eval._record_gate", lambda *a, **k: True)

        serial_calls = []

        def fake_mirror(a, b, n_games=1, verbose=False, save_log=False):
            serial_calls.append((a, b, n_games))
            return ([1, 2], 0, n_games, None)

        monkeypatch.setattr("engine.mirror_battle", fake_mirror)

        def fake_submit(jobs):
            return [j.job_id for j in jobs]

        monkeypatch.setattr(battle_scheduler, "submit_jobs", fake_submit)

        # Only return result for first job
        first_job_id = None

        def fake_collect(job_ids):
            nonlocal first_job_id
            if first_job_id is None:
                first_job_id = job_ids[0]
            return {
                first_job_id: {
                    "wins_a": 2,
                    "wins_b": 1,
                    "draws": 0,
                    "total": 3,
                    "error": None,
                    "completed_at": time.time(),
                }
            }

        monkeypatch.setattr(battle_scheduler, "collect_results", fake_collect)

        result = await run_precommit_eval({"version": 99, "source_v": 98, "n_games": 3})
        text = result["content"][0]["text"]
        data = json.loads(text)

        # One from scheduler, one from serial fallback
        assert len(data["matchups"]) == 2
        assert len(serial_calls) == 1
        # The scheduler result should be first (claude_v98)
        scheduler_matchup = next(m for m in data["matchups"] if m["opponent"] == "claude_v98")
        serial_matchup = next(m for m in data["matchups"] if m["opponent"] == "claude_v50")
        assert scheduler_matchup["wins"] == 2
        assert serial_matchup["wins"] == 1

    @pytest.mark.asyncio
    async def test_scheduler_result_format_matches_serial(
        self, monkeypatch, fake_bots, fake_opponents, mock_checkpoint, mock_ui
    ):
        """Scheduler-produced matchup dicts have the same keys as serial-produced ones."""
        import battle_scheduler

        monkeypatch.setattr("tool_eval.is_daemon_scheduler_capable", lambda: True)
        monkeypatch.setattr("tool_eval._matching_checkpoint", lambda _v, _sv: mock_checkpoint)
        monkeypatch.setattr("tool_eval._get_ui", lambda: mock_ui)
        monkeypatch.setattr("tool_eval._record_gate", lambda *a, **k: True)

        def fake_mirror(a, b, n_games=1, verbose=False, save_log=False):
            return ([2, 1], 0, n_games, None)

        monkeypatch.setattr("engine.mirror_battle", fake_mirror)

        def fake_submit(jobs):
            return [j.job_id for j in jobs]

        monkeypatch.setattr(battle_scheduler, "submit_jobs", fake_submit)

        def fake_collect(job_ids):
            return {
                jid: {
                    "wins_a": 2,
                    "wins_b": 1,
                    "draws": 0,
                    "total": 3,
                    "error": None,
                    "completed_at": time.time(),
                }
                for jid in job_ids
            }

        monkeypatch.setattr(battle_scheduler, "collect_results", fake_collect)

        result_scheduler = await run_precommit_eval({"version": 99, "source_v": 98, "n_games": 3})

        # Now run serial path
        monkeypatch.setattr("tool_eval.is_daemon_scheduler_capable", lambda: False)
        result_serial = await run_precommit_eval({"version": 99, "source_v": 98, "n_games": 3})

        sched_data = json.loads(result_scheduler["content"][0]["text"])
        serial_data = json.loads(result_serial["content"][0]["text"])

        for s_m, ser_m in zip(sched_data["matchups"], serial_data["matchups"]):
            assert set(s_m.keys()) == set(ser_m.keys())
            assert s_m["wins"] == ser_m["wins"]
            assert s_m["losses"] == ser_m["losses"]
            assert s_m["draws"] == ser_m["draws"]
            assert s_m["n_played"] == ser_m["n_played"]

    @pytest.mark.asyncio
    async def test_scheduler_rejection_falls_back(
        self, monkeypatch, fake_bots, fake_opponents, mock_checkpoint, mock_ui
    ):
        """If scheduler submit raises RuntimeError, fallback to serial path."""
        import battle_scheduler

        monkeypatch.setattr("tool_eval.is_daemon_scheduler_capable", lambda: True)
        monkeypatch.setattr("tool_eval._matching_checkpoint", lambda _v, _sv: mock_checkpoint)
        monkeypatch.setattr("tool_eval._get_ui", lambda: mock_ui)
        monkeypatch.setattr("tool_eval._record_gate", lambda *a, **k: True)

        serial_calls = []

        def fake_mirror(a, b, n_games=1, verbose=False, save_log=False):
            serial_calls.append((a, b, n_games))
            return ([2, 1], 0, n_games, None)

        monkeypatch.setattr("engine.mirror_battle", fake_mirror)

        def fake_submit(_jobs):
            raise RuntimeError("Pending job limit exceeded")

        monkeypatch.setattr(battle_scheduler, "submit_jobs", fake_submit)

        result = await run_precommit_eval({"version": 99, "source_v": 98, "n_games": 3})
        text = result["content"][0]["text"]
        data = json.loads(text)

        assert data["passed"] is True
        assert len(serial_calls) == 2
        assert len(data["matchups"]) == 2

    @pytest.mark.asyncio
    async def test_system_events_logged(
        self, monkeypatch, fake_bots, fake_opponents, mock_checkpoint, mock_ui, tmp_path
    ):
        """Appropriate system events are logged for scheduler start/complete/fallback."""
        import battle_scheduler
        import system_log

        events = []
        original_log = system_log.log_system_event

        def capture_event(event_type, level, message, extra=None):
            events.append({"type": event_type, "level": level, "message": message})
            return original_log(event_type, level, message, extra)

        monkeypatch.setattr(system_log, "log_system_event", capture_event)
        monkeypatch.setattr("tool_eval.is_daemon_scheduler_capable", lambda: True)
        monkeypatch.setattr("tool_eval._matching_checkpoint", lambda _v, _sv: mock_checkpoint)
        monkeypatch.setattr("tool_eval._get_ui", lambda: mock_ui)
        monkeypatch.setattr("tool_eval._record_gate", lambda *a, **k: True)

        def fake_submit(jobs):
            return [j.job_id for j in jobs]

        monkeypatch.setattr(battle_scheduler, "submit_jobs", fake_submit)

        def fake_collect(job_ids):
            return {
                jid: {
                    "wins_a": 2,
                    "wins_b": 1,
                    "draws": 0,
                    "total": 3,
                    "error": None,
                    "completed_at": time.time(),
                }
                for jid in job_ids
            }

        monkeypatch.setattr(battle_scheduler, "collect_results", fake_collect)
        monkeypatch.setattr(
            "engine.mirror_battle",
            lambda *a, **k: (_raise("should not be called"),),
        )

        await run_precommit_eval({"version": 99, "source_v": 98, "n_games": 3})

        event_types = [e["type"] for e in events]
        assert "pipeline.precommit_eval.scheduler_start" in event_types
        assert "pipeline.precommit_eval.scheduler_complete" in event_types

    @pytest.mark.asyncio
    async def test_timeout_polls_and_falls_back(
        self, monkeypatch, fake_bots, fake_opponents, mock_checkpoint, mock_ui
    ):
        """If scheduler results never arrive before deadline, fallback to serial."""
        import battle_scheduler

        monkeypatch.setattr("tool_eval.is_daemon_scheduler_capable", lambda: True)
        monkeypatch.setattr("tool_eval._matching_checkpoint", lambda _v, _sv: mock_checkpoint)
        monkeypatch.setattr("tool_eval._get_ui", lambda: mock_ui)
        monkeypatch.setattr("tool_eval._record_gate", lambda *a, **k: True)

        serial_calls = []

        def fake_mirror(a, b, n_games=1, verbose=False, save_log=False):
            serial_calls.append((a, b, n_games))
            return ([2, 1], 0, n_games, None)

        monkeypatch.setattr("engine.mirror_battle", fake_mirror)

        def fake_submit(jobs):
            return [j.job_id for j in jobs]

        monkeypatch.setattr(battle_scheduler, "submit_jobs", fake_submit)

        # Never return results → timeout → fallback
        monkeypatch.setattr(battle_scheduler, "collect_results", lambda _ids: {})

        # Speed up polling so test doesn't take forever
        import tool_eval
        monkeypatch.setattr(tool_eval, "time", lambda: 0)

        result = await run_precommit_eval({"version": 99, "source_v": 98, "n_games": 1})
        text = result["content"][0]["text"]
        data = json.loads(text)

        assert len(serial_calls) == 2
        assert len(data["matchups"]) == 2

    @pytest.mark.asyncio
    async def test_blocker_format_consistent(
        self, monkeypatch, fake_bots, fake_opponents, mock_checkpoint, mock_ui
    ):
        """Blockers from scheduler errors have the same shape as serial blockers."""
        import battle_scheduler

        monkeypatch.setattr("tool_eval.is_daemon_scheduler_capable", lambda: True)
        monkeypatch.setattr("tool_eval._matching_checkpoint", lambda _v, _sv: mock_checkpoint)
        monkeypatch.setattr("tool_eval._get_ui", lambda: mock_ui)
        monkeypatch.setattr("tool_eval._record_gate", lambda *a, **k: True)

        def fake_mirror(a, b, n_games=1, verbose=False, save_log=False):
            return ([0, 3], 0, n_games, None)

        monkeypatch.setattr("engine.mirror_battle", fake_mirror)

        def fake_submit(jobs):
            return [j.job_id for j in jobs]

        monkeypatch.setattr(battle_scheduler, "submit_jobs", fake_submit)

        def fake_collect(job_ids):
            return {
                jid: {
                    "wins_a": 0,
                    "wins_b": 3,
                    "draws": 0,
                    "total": 3,
                    "error": "scheduler_worker_crash",
                    "completed_at": time.time(),
                }
                for jid in job_ids
            }

        monkeypatch.setattr(battle_scheduler, "collect_results", fake_collect)

        result = await run_precommit_eval({"version": 99, "source_v": 98, "n_games": 3})
        text = result["content"][0]["text"]
        data = json.loads(text)

        # Should have scheduler_error blockers + lost_to_parent blockers
        blocker_reasons = {b["reason"] for b in data["blockers"]}
        assert "scheduler_error" in blocker_reasons
        assert "lost_to_parent" in blocker_reasons or "aggregate_precommit_regression" in blocker_reasons

        # All blockers must have 'reason' and 'details' keys
        for b in data["blockers"]:
            assert "reason" in b
            assert "details" in b


# ── Helper ──

def _raise(msg):
    raise AssertionError(msg)
