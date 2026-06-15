"""Tests for tool_eval.py precommit FAILED directive + precommit_attempt counter.

Covers the global interface contract:
  - pipeline_state.json gains "precommit_attempt" (int, default 0), persisted
    at the start of run_precommit_eval against the current bot code.
  - When precommit FAILED and precommit_attempt < MAX_PRECOMMIT_RETRIES, the
    result directive tells the Orchestrator to call execute_workers (or abandon).
  - When precommit_attempt >= MAX_PRECOMMIT_RETRIES, the directive says HARD
    LIMIT and the bot must NOT be retried.
"""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Ensure imports resolve (mirrors test_precommit_scheduler_path.py layout)
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "web" / "core"))
sys.path.insert(0, str(_PROJECT_ROOT / "web" / "server"))

import tool_eval
from tool_eval import run_precommit_eval as _run_precommit_eval_tool

# The @tool decorator wraps the function in an SdkMcpTool object.
# Tests need the raw async handler.
run_precommit_eval = _run_precommit_eval_tool.handler


# ── Fixtures ──

@pytest.fixture
def mock_ui():
    ui = MagicMock()
    ui.log_history = MagicMock()
    return ui


@pytest.fixture(autouse=True)
def mock_precommit_semantic(monkeypatch):
    """Prevent real LLM API calls from _run_precommit_semantic."""
    async def _fake_semantic(v, source_v, matchups, master_plan, ui):
        return {
            "win_pattern_analysis": "",
            "top_opponent_assessment": "",
            "regression_semantics": "safe",
            "recommended_action": "proceed",
            "confidence": "low",
        }

    import audit_agents
    monkeypatch.setattr(audit_agents, "_run_precommit_semantic", _fake_semantic)


@pytest.fixture
def patch_checkpoint_file(monkeypatch, tmp_path):
    """Redirect the real PIPELINE_STATE_FILE into tmp_path.

    This lets run_precommit_eval persist precommit_attempt through the real
    write_pipeline_checkpoint / _matching_checkpoint round-trip so the
    increment-across-calls test exercises genuine persistence.
    """
    import evolution_infra
    ckpt_path = tmp_path / "pipeline_state.json"
    monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", ckpt_path)
    # write_pipeline_checkpoint references the module global at call time.
    return ckpt_path


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
    """Deterministic opponent list including the parent."""
    ops = [
        {"name": "claude_v98", "reason": "parent"},
        {"name": "claude_v50", "reason": "top_opponent"},
    ]
    monkeypatch.setattr("tool_eval._select_precommit_opponents", lambda _v, _sv: ops)
    return ops


def _seed_passing_checkpoint():
    """Write a checkpoint for v99<-v98 with all gates passed, precommit_attempt=0."""
    tool_eval.write_pipeline_checkpoint(
        99,
        98,
        "critic_checked",
        gate_results={
            "quality": {"all_passed": True, "critical_scenarios_passed": True},
            "review": {"approved": True},
            "critic": {"approved": True, "score": 7},
        },
        precommit_attempt=0,
    )


def _patch_losing_mirror(monkeypatch):
    """Patch mirror_battle so the parent matchup loses 2-6 (triggers lost_to_parent)."""
    def fake_mirror(a, b, n_games=1, verbose=False, save_log=False):
        if "claude_v98" in b:
            return ([2, 6], 0, n_games, None)  # parent: clear loss
        return ([3, 3], 0, n_games, None)

    _patch_mirror_battle(monkeypatch, fake_mirror)


def _patch_mirror_battle(monkeypatch, fn):
    """Patch mirror_battle on the engine.battle module (see test_precommit_scheduler_path)."""
    import engine.battle as _mod  # noqa: F811
    _battle_module = sys.modules["engine.battle"]
    monkeypatch.setattr(_battle_module, "mirror_battle", fn)


def _common_patches(monkeypatch, mock_ui):
    monkeypatch.setattr("tool_eval.is_daemon_scheduler_capable", lambda: False)
    monkeypatch.setattr("tool_eval._get_ui", lambda: mock_ui)
    monkeypatch.setattr("tool_eval._record_gate", lambda *a, **k: True)


def _decode(result):
    return json.loads(result["content"][0]["text"])


# ── Directive content tests ──

class TestPrecommitFailedDirective:
    @pytest.mark.asyncio
    async def test_failed_below_limit_mentions_execute_workers(
        self, monkeypatch, fake_bots, fake_opponents, mock_ui, patch_checkpoint_file
    ):
        """FAILED with precommit_attempt < MAX: directive must mention execute_workers."""
        _seed_passing_checkpoint()
        _patch_losing_mirror(monkeypatch)
        _common_patches(monkeypatch, mock_ui)

        result = await run_precommit_eval({"version": 99, "source_v": 98, "n_games": 8})
        data = _decode(result)

        assert data["passed"] is False
        assert "directive" in data
        assert "execute_workers" in data["directive"]
        # Below the hard limit, so NO "HARD LIMIT" wording.
        assert "HARD LIMIT" not in data["directive"]
        # The directive must name the worst opponent and its W-L.
        assert "claude_v98" in data["directive"]
        assert "2W-6L" in data["directive"]

    @pytest.mark.asyncio
    async def test_failed_at_limit_mentions_hard_limit(
        self, monkeypatch, fake_bots, fake_opponents, mock_ui, patch_checkpoint_file
    ):
        """FAILED with precommit_attempt >= MAX: directive must say HARD LIMIT."""
        # Seed the checkpoint with the counter already at the hard limit.
        tool_eval.write_pipeline_checkpoint(
            99,
            98,
            "critic_checked",
            gate_results={
                "quality": {"all_passed": True, "critical_scenarios_passed": True},
                "review": {"approved": True},
                "critic": {"approved": True, "score": 7},
            },
            precommit_attempt=tool_eval.MAX_PRECOMMIT_RETRIES - 1,
        )
        _patch_losing_mirror(monkeypatch)
        _common_patches(monkeypatch, mock_ui)

        result = await run_precommit_eval({"version": 99, "source_v": 98, "n_games": 8})
        data = _decode(result)

        assert data["passed"] is False
        assert "directive" in data
        assert "HARD LIMIT" in data["directive"]
        # At the hard limit the directive must NOT tell it to call execute_workers.
        assert "execute_workers" not in data["directive"]


# ── Counter persistence test ──

class TestPrecommitAttemptIncrement:
    @pytest.mark.asyncio
    async def test_precommit_attempt_increments_across_two_calls(
        self, monkeypatch, fake_bots, fake_opponents, mock_ui, patch_checkpoint_file
    ):
        """Two FAILED precommit calls must increment precommit_attempt 1 -> 2.

        The counter persists through the real write/read checkpoint round-trip
        (PIPELINE_STATE_FILE redirected to tmp_path), proving it survives between
        Orchestrator turns.
        """
        _seed_passing_checkpoint()
        _patch_losing_mirror(monkeypatch)
        _common_patches(monkeypatch, mock_ui)

        # First FAILED call.
        r1 = await run_precommit_eval({"version": 99, "source_v": 98, "n_games": 8})
        d1 = _decode(r1)
        assert d1["passed"] is False
        assert "1/" in d1["directive"]
        assert "execute_workers" in d1["directive"]

        # The persisted counter must now read 1.
        ck1 = tool_eval._matching_checkpoint(99, 98)
        assert ck1["precommit_attempt"] == 1

        # Second FAILED call (bot code unchanged) -> counter becomes 2.
        r2 = await run_precommit_eval({"version": 99, "source_v": 98, "n_games": 8})
        d2 = _decode(r2)
        assert d2["passed"] is False
        assert "2/" in d2["directive"]
        assert "execute_workers" in d2["directive"]

        ck2 = tool_eval._matching_checkpoint(99, 98)
        assert ck2["precommit_attempt"] == 2


# ── Bug-2 regression: increment must NOT fire on already-verified idempotent call ──

class TestPrecommitAttemptNotIncrementedOnIdempotent:
    @pytest.mark.asyncio
    async def test_verified_idempotent_call_does_not_increment(
        self, monkeypatch, fake_bots, fake_opponents, mock_ui, patch_checkpoint_file
    ):
        """run_precommit_eval against an already-verified version must return the
        idempotent cache and MUST NOT bump precommit_attempt.

        Otherwise redundant verified calls would push the counter to MAX and
        trigger a false HARD-LIMIT abandonment even though the gate passed.
        """
        # Seed a checkpoint that already passed precommit (stage=verified, gate
        # passed). precommit_attempt starts at 1 (the real eval that passed).
        tool_eval.write_pipeline_checkpoint(
            99,
            98,
            "verified",
            gate_results={
                "quality": {"all_passed": True, "critical_scenarios_passed": True},
                "review": {"approved": True},
                "critic": {"approved": True, "score": 7},
                "precommit_eval": {"passed": True},
            },
            precommit_attempt=1,
        )

        # Even if mirror_battle were called, the idempotent guard returns first.
        # Patch it to raise so any battle path would surface as a test failure.
        def _boom_mirror(*a, **k):
            raise AssertionError("mirror_battle must not run on idempotent path")

        _patch_mirror_battle(monkeypatch, _boom_mirror)
        _common_patches(monkeypatch, mock_ui)

        result = await run_precommit_eval({"version": 99, "source_v": 98, "n_games": 8})
        data = _decode(result)

        # Idempotent cache hit
        assert data.get("idempotent_cache") is True
        assert "ALREADY PASSED" in data.get("directive", "")

        # Counter must be unchanged
        ck = tool_eval._matching_checkpoint(99, 98)
        assert ck["precommit_attempt"] == 1
