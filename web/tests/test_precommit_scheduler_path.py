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


@pytest.fixture(autouse=True)
def mock_precommit_semantic(monkeypatch):
    """Mock _run_precommit_semantic to prevent real LLM API calls.

    The audit_agents._run_precommit_semantic calls run_claude_query which
    makes real Claude API calls and hangs forever in tests.
    Safe default: recommended_action="proceed" (non-blocking).
    """
    async def _fake_semantic(v, source_v, matchups, master_plan, ui):
        return {
            "win_pattern_analysis": "",
            "top_opponent_assessment": "",
            "regression_semantics": "safe",
            "recommended_action": "proceed",
            "confidence": "low",
        }

    # Patch on the audit_agents module so the import inside tool_eval finds it
    import audit_agents
    monkeypatch.setattr(audit_agents, "_run_precommit_semantic", _fake_semantic)


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
        "gate_results": {
            "quality": {"all_passed": True, "critical_scenarios_passed": True},
            "review": {"approved": True},
            "critic": {"approved": True, "score": 7},
        },
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
        _patch_mirror_battle(monkeypatch, lambda *a, **k: (_raise("should not be called"),))

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

        _patch_mirror_battle(monkeypatch, fake_mirror)

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

        _patch_mirror_battle(monkeypatch, fake_mirror)

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

        # Mock time so the polling loop runs at least once then exits.
        # Calls 1-2: submitted_at for each BattleJob
        # Call 3: deadline calculation
        # Call 4: while loop check (should return small so loop enters)
        # Call 5+: return large to exceed deadline → loop exits
        import tool_eval
        _call_count = [0]
        class _FakeTime:
            def time(self):
                _call_count[0] += 1
                if _call_count[0] <= 5:
                    return 100.0
                return 999999.0
        monkeypatch.setattr(tool_eval, "time", _FakeTime())

        # Speed up polling sleep
        _real_sleep = asyncio.sleep
        monkeypatch.setattr(asyncio, "sleep", lambda t: _real_sleep(0))

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

        _patch_mirror_battle(monkeypatch, fake_mirror)

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

        _patch_mirror_battle(monkeypatch, fake_mirror)

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

        monkeypatch.setattr("tool_eval.log_system_event", capture_event)
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
        _patch_mirror_battle(monkeypatch, lambda *a, **k: (_raise("should not be called"),))

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

        _patch_mirror_battle(monkeypatch, fake_mirror)

        def fake_submit(jobs):
            return [j.job_id for j in jobs]

        monkeypatch.setattr(battle_scheduler, "submit_jobs", fake_submit)

        # Never return results → timeout → fallback
        monkeypatch.setattr(battle_scheduler, "collect_results", lambda _ids: {})

        # Mock time so the polling loop exits quickly after deadline.
        # The code calls time.time() in several places before the while loop:
        #   - submitted_at for each BattleJob (2 opponents = 2 calls)
        #   - deadline = time.time() + per_game_timeout * len(opponents) (1 call)
        # So the first 3 calls should return a small value so deadline is reasonable,
        # then subsequent calls return a large value to exceed deadline.
        # With 2 opponents: deadline = 100.0 + max(300,1*120)*2 = 100 + 600 = 700
        import tool_eval
        _call_count = [0]
        class _FakeTime:
            def time(self):
                _call_count[0] += 1
                if _call_count[0] <= 4:
                    return 100.0   # deadline = 100 + 600 = 700
                return 999999.0    # way past deadline → loop exits
        monkeypatch.setattr(tool_eval, "time", _FakeTime())

        # Speed up polling sleep so test doesn't actually wait.
        # Save original before patching to avoid infinite recursion.
        _real_sleep = asyncio.sleep
        monkeypatch.setattr(asyncio, "sleep", lambda t: _real_sleep(0))

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

        _patch_mirror_battle(monkeypatch, fake_mirror)

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

        # The scheduler path reports scheduler_error blockers for each errored job.
        # (lost_to_parent is serial-path-only; the new ratio-based aggregate gate
        # requires total_decided >= 8, which a 3-game/3-game scheduler result (6)
        # does not meet, so aggregate_precommit_regression is NOT expected here.)
        blocker_reasons = {b["reason"] for b in data["blockers"]}
        assert "scheduler_error" in blocker_reasons

        # All blockers must have 'reason' and 'details' keys
        for b in data["blockers"]:
            assert "reason" in b
            assert "details" in b


# ── Regression gate ratio logic (serial fallback path) ──

class TestPrecommitRegressionGates:
    """Tests for the n_games cap removal + ratio-based regression gates.

    These run on the serial fallback path (scheduler NOT capable) so the
    lost_to_parent and aggregate gates are exercised directly.
    """

    @pytest.mark.asyncio
    async def test_n_games_cap_removed(
        self, monkeypatch, fake_bots, fake_opponents, mock_checkpoint, mock_ui
    ):
        """n_games is no longer capped to 3; values up to PRECOMMIT_MAX_N_GAMES pass through."""
        monkeypatch.setattr("tool_eval.is_daemon_scheduler_capable", lambda: False)
        monkeypatch.setattr("tool_eval._matching_checkpoint", lambda _v, _sv: mock_checkpoint)
        monkeypatch.setattr("tool_eval._get_ui", lambda: mock_ui)
        monkeypatch.setattr("tool_eval._record_gate", lambda *a, **k: True)

        captured = []

        def fake_mirror(a, b, n_games=1, verbose=False, save_log=False):
            captured.append(n_games)
            return ([2, 2], 0, n_games, None)  # balanced → no block

        _patch_mirror_battle(monkeypatch, fake_mirror)

        result = await run_precommit_eval({"version": 99, "source_v": 98, "n_games": 10})
        data = json.loads(result["content"][0]["text"])

        # 10 must NOT be clamped down to 3 anymore
        assert data["n_games"] == 10
        assert all(ng == 10 for ng in captured)
        assert data["passed"] is True

    @pytest.mark.asyncio
    async def test_n_games_clamped_to_max(
        self, monkeypatch, fake_bots, fake_opponents, mock_checkpoint, mock_ui
    ):
        """n_games above PRECOMMIT_MAX_N_GAMES is clamped to the max (12), not rejected."""
        monkeypatch.setattr("tool_eval.is_daemon_scheduler_capable", lambda: False)
        monkeypatch.setattr("tool_eval._matching_checkpoint", lambda _v, _sv: mock_checkpoint)
        monkeypatch.setattr("tool_eval._get_ui", lambda: mock_ui)
        monkeypatch.setattr("tool_eval._record_gate", lambda *a, **k: True)

        captured = []

        def fake_mirror(a, b, n_games=1, verbose=False, save_log=False):
            captured.append(n_games)
            return ([2, 2], 0, n_games, None)

        _patch_mirror_battle(monkeypatch, fake_mirror)

        result = await run_precommit_eval({"version": 99, "source_v": 98, "n_games": 50})
        data = json.loads(result["content"][0]["text"])

        import tool_eval
        assert data["n_games"] == tool_eval.PRECOMMIT_MAX_N_GAMES
        assert all(ng == tool_eval.PRECOMMIT_MAX_N_GAMES for ng in captured)

    @pytest.mark.asyncio
    async def test_default_n_games_applied(
        self, monkeypatch, fake_bots, fake_opponents, mock_checkpoint, mock_ui
    ):
        """When n_games is omitted entirely, PRECOMMIT_DEFAULT_N_GAMES is used (not 3)."""
        monkeypatch.setattr("tool_eval.is_daemon_scheduler_capable", lambda: False)
        monkeypatch.setattr("tool_eval._matching_checkpoint", lambda _v, _sv: mock_checkpoint)
        monkeypatch.setattr("tool_eval._get_ui", lambda: mock_ui)
        monkeypatch.setattr("tool_eval._record_gate", lambda *a, **k: True)

        captured = []

        def fake_mirror(a, b, n_games=1, verbose=False, save_log=False):
            captured.append(n_games)
            return ([2, 2], 0, n_games, None)

        _patch_mirror_battle(monkeypatch, fake_mirror)

        result = await run_precommit_eval({"version": 99, "source_v": 98})
        data = json.loads(result["content"][0]["text"])

        import tool_eval
        assert data["n_games"] == tool_eval.PRECOMMIT_DEFAULT_N_GAMES
        assert all(ng == tool_eval.PRECOMMIT_DEFAULT_N_GAMES for ng in captured)

    @pytest.mark.asyncio
    async def test_parent_ratio_blocks_on_clear_loss(
        self, monkeypatch, fake_bots, fake_opponents, mock_checkpoint, mock_ui
    ):
        """lost_to_parent ratio gate: parent [2,6,0,8] blocks (decided=8, loss ratio 6/8=0.75).

        This is the dead-code regression: under the old n_games=3 cap the unreachable
        `n_played >= 4` check let this degraded bot pass. The ratio gate now blocks it.
        """
        monkeypatch.setattr("tool_eval.is_daemon_scheduler_capable", lambda: False)
        monkeypatch.setattr("tool_eval._matching_checkpoint", lambda _v, _sv: mock_checkpoint)
        monkeypatch.setattr("tool_eval._get_ui", lambda: mock_ui)
        monkeypatch.setattr("tool_eval._record_gate", lambda *a, **k: True)

        def fake_mirror(a, b, n_games=1, verbose=False, save_log=False):
            # Parent (claude_v98) loses 2-6; top opponent (claude_v50) neutral so it
            # does not independently trip the aggregate gate.
            if "claude_v98" in b:
                return ([2, 6], 0, n_games, None)
            return ([3, 3], 0, n_games, None)

        _patch_mirror_battle(monkeypatch, fake_mirror)

        result = await run_precommit_eval({"version": 99, "source_v": 98, "n_games": 8})
        data = json.loads(result["content"][0]["text"])

        blocker_reasons = {b["reason"] for b in data["blockers"]}
        assert "lost_to_parent" in blocker_reasons
        assert data["passed"] is False

    @pytest.mark.asyncio
    async def test_parent_ratio_no_block_on_small_sample(
        self, monkeypatch, fake_bots, fake_opponents, mock_checkpoint, mock_ui
    ):
        """lost_to_parent ratio gate: parent [1,2,0,3] does NOT block (decided=3 < 4)."""
        monkeypatch.setattr("tool_eval.is_daemon_scheduler_capable", lambda: False)
        monkeypatch.setattr("tool_eval._matching_checkpoint", lambda _v, _sv: mock_checkpoint)
        monkeypatch.setattr("tool_eval._get_ui", lambda: mock_ui)
        monkeypatch.setattr("tool_eval._record_gate", lambda *a, **k: True)

        def fake_mirror(a, b, n_games=1, verbose=False, save_log=False):
            # Both opponents 1-2-0 → decided=3 each (<4 ratio gate), total decided=6 (<8 aggregate)
            return ([1, 2], 0, n_games, None)

        _patch_mirror_battle(monkeypatch, fake_mirror)

        result = await run_precommit_eval({"version": 99, "source_v": 98, "n_games": 3})
        data = json.loads(result["content"][0]["text"])

        blocker_reasons = {b["reason"] for b in data["blockers"]}
        assert "lost_to_parent" not in blocker_reasons
        assert "aggregate_precommit_regression" not in blocker_reasons
        assert data["passed"] is True

    @pytest.mark.asyncio
    async def test_parent_ratio_no_block_on_coin_flip(
        self, monkeypatch, fake_bots, fake_opponents, mock_checkpoint, mock_ui
    ):
        """lost_to_parent ratio gate: parent [4,3,0,7] does NOT block (loss ratio 3/7=0.43 < 0.60)."""
        monkeypatch.setattr("tool_eval.is_daemon_scheduler_capable", lambda: False)
        monkeypatch.setattr("tool_eval._matching_checkpoint", lambda _v, _sv: mock_checkpoint)
        monkeypatch.setattr("tool_eval._get_ui", lambda: mock_ui)
        monkeypatch.setattr("tool_eval._record_gate", lambda *a, **k: True)

        def fake_mirror(a, b, n_games=1, verbose=False, save_log=False):
            # Parent coin-flip 4-3; top opponent balanced so aggregate does not trip
            if "claude_v98" in b:
                return ([4, 3], 0, n_games, None)
            return ([3, 3], 0, n_games, None)

        _patch_mirror_battle(monkeypatch, fake_mirror)

        result = await run_precommit_eval({"version": 99, "source_v": 98, "n_games": 7})
        data = json.loads(result["content"][0]["text"])

        blocker_reasons = {b["reason"] for b in data["blockers"]}
        assert "lost_to_parent" not in blocker_reasons
        assert "aggregate_precommit_regression" not in blocker_reasons
        assert data["passed"] is True

    @pytest.mark.asyncio
    async def test_aggregate_collapse_blocks_without_parent_loss(
        self, monkeypatch, fake_bots, fake_opponents, mock_checkpoint, mock_ui
    ):
        """Aggregate gate blocks a field collapse even when the parent matchup is fine.

        Parent wins 3-2 (no lost_to_parent), top opponent collapses 1-5.
        total W=4 L=7 decided=11, losses >= wins+2 → aggregate block.
        """
        monkeypatch.setattr("tool_eval.is_daemon_scheduler_capable", lambda: False)
        monkeypatch.setattr("tool_eval._matching_checkpoint", lambda _v, _sv: mock_checkpoint)
        monkeypatch.setattr("tool_eval._get_ui", lambda: mock_ui)
        monkeypatch.setattr("tool_eval._record_gate", lambda *a, **k: True)

        def fake_mirror(a, b, n_games=1, verbose=False, save_log=False):
            if "claude_v98" in b:
                return ([3, 2], 0, n_games, None)  # parent fine
            return ([1, 5], 0, n_games, None)  # top collapse

        _patch_mirror_battle(monkeypatch, fake_mirror)

        result = await run_precommit_eval({"version": 99, "source_v": 98, "n_games": 6})
        data = json.loads(result["content"][0]["text"])

        blocker_reasons = {b["reason"] for b in data["blockers"]}
        assert "aggregate_precommit_regression" in blocker_reasons
        assert "lost_to_parent" not in blocker_reasons
        assert data["passed"] is False

    @pytest.mark.asyncio
    async def test_no_false_positive_on_balanced_field(
        self, monkeypatch, fake_bots, fake_opponents, mock_checkpoint, mock_ui
    ):
        """A balanced coin-flip field must NOT trip either gate.

        Both opponents 3-3-0: parent ratio 0.5 (<0.60), total decided=12 but
        losses == wins so the aggregate margin (>= wins+2) is not met.
        """
        monkeypatch.setattr("tool_eval.is_daemon_scheduler_capable", lambda: False)
        monkeypatch.setattr("tool_eval._matching_checkpoint", lambda _v, _sv: mock_checkpoint)
        monkeypatch.setattr("tool_eval._get_ui", lambda: mock_ui)
        monkeypatch.setattr("tool_eval._record_gate", lambda *a, **k: True)

        def fake_mirror(a, b, n_games=1, verbose=False, save_log=False):
            return ([3, 3], 0, n_games, None)

        _patch_mirror_battle(monkeypatch, fake_mirror)

        result = await run_precommit_eval({"version": 99, "source_v": 98, "n_games": 6})
        data = json.loads(result["content"][0]["text"])

        blocker_reasons = {b["reason"] for b in data["blockers"]}
        assert "lost_to_parent" not in blocker_reasons
        assert "aggregate_precommit_regression" not in blocker_reasons
        assert data["passed"] is True


# ── Helper ──

def _raise(msg):
    raise AssertionError(msg)


def _patch_mirror_battle(monkeypatch, fn):
    """Patch mirror_battle on the engine.battle module.

    String-based monkeypatch like "engine.battle.mirror_battle" fails because
    engine.__init__ re-exports the 'battle' function, so engine.battle resolves
    to the function, not the module.  We must patch via sys.modules instead.
    """
    import engine.battle as _mod  # noqa: F811 – forces module into sys.modules
    _battle_module = sys.modules["engine.battle"]
    monkeypatch.setattr(_battle_module, "mirror_battle", fn)
