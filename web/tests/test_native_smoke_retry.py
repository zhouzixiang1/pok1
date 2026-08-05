"""Regression guard for the single-shot native smoke flakiness deadlock.

``_run_workflow_smoke_gate`` previously called ``run_native_tcp_smoke`` exactly
once.  A single transient infrastructure stall (startup-watchdog kill,
transport stall, launch-latency spike) therefore permanently abandoned a
generation that had already spent its full Master+Worker LLM budget — even
though the candidate policy was fine (the system-owned bot runtime is
byte-identical across every candidate and baseline opponent).  v30-v62
abandoned 16 generations at ``workers_done`` this way.

The gate now runs a bounded retry loop (``POK_NATIVE_SMOKE_MAX_ATTEMPTS``,
default 3) that retries only infrastructure-class failures.  A genuine
candidate defect (illegal_actions, artifact_changed_during_execution) is
reported immediately without burning retries.
"""

import asyncio

import national_native as national_native_mod
# Import the parent module first so the tool_gates <-> tool_gates_native_smoke
# circular re-export delegate resolves before we touch the smoke submodule.
import tool_gates  # noqa: F401  (establishes import order)
import tool_gates_native_smoke


def _gate_kwargs(bot_dir):
    return dict(
        bot_dir=bot_dir,
        source_v=27,
        native_tcp_mode=True,
        compile_errors=[],
        import_errors=[],
        protected_contract_errors=[],
        native_contract_errors=[],
        embedded_selftest_errors=[],
        opponent_token=None,
        self_play=False,
    )


def _install_sleep_noop(monkeypatch):
    """Make the retry backoff instant so the test does not really sleep."""
    real_sleep = asyncio.sleep

    async def _noop(_delay):
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _noop)


def test_infra_failure_then_pass_retries_and_passes(monkeypatch, tmp_path):
    """First attempt fails as infrastructure, second attempt passes -> passed."""
    calls = []

    async def fake_smoke(bot_dir, **_kwargs):
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            return {
                "passed": False,
                "execution_mode": "native_tcp",
                "outcome": "infrastructure_failure",
                "failure_side": "harness",
                "issues": ["native_name_handshake_missing"],
                "hands_played": 0,
                "native_match_timeout_phase": "startup_watchdog",
            }
        return {
            "passed": True,
            "execution_mode": "native_tcp",
            "outcome": "passed",
            "failure_side": "",
            "issues": [],
            "hands_played": 1,
            "native_match_timeout_phase": "",
        }

    monkeypatch.setattr(national_native_mod, "run_native_tcp_smoke", fake_smoke)
    _install_sleep_noop(monkeypatch)

    errors, report = asyncio.run(
        tool_gates_native_smoke._run_workflow_smoke_gate(**_gate_kwargs(tmp_path))
    )
    assert report["passed"] is True
    assert errors == []
    assert len(calls) == 2  # retried exactly once


def test_all_infra_failures_returns_last_report(monkeypatch, tmp_path):
    """Three infrastructure failures -> returns the last report, still infra."""
    calls = []

    async def fake_smoke(bot_dir, **_kwargs):
        calls.append(len(calls) + 1)
        return {
            "passed": False,
            "execution_mode": "native_tcp",
            "outcome": "infrastructure_failure",
            "failure_side": "harness",
            "issues": ["native_name_handshake_missing"],
            "hands_played": 0,
            "native_match_timeout_phase": "startup_watchdog",
        }

    monkeypatch.setattr(national_native_mod, "run_native_tcp_smoke", fake_smoke)
    _install_sleep_noop(monkeypatch)

    errors, report = asyncio.run(
        tool_gates_native_smoke._run_workflow_smoke_gate(**_gate_kwargs(tmp_path))
    )
    assert report["passed"] is False
    assert report["outcome"] == "infrastructure_failure"
    assert len(calls) == 3  # tried max_attempts times
    assert errors  # surfaced the last failure's issues


def test_candidate_failure_not_retried(monkeypatch, tmp_path):
    """A genuine candidate defect (illegal_actions) is reported immediately
    without burning retries."""
    calls = []

    async def fake_smoke(bot_dir, **_kwargs):
        calls.append(len(calls) + 1)
        return {
            "passed": False,
            "execution_mode": "native_tcp",
            "outcome": "candidate_failure",
            "failure_side": "candidate",
            "issues": ["national_cloud_v63: illegal_actions=1"],
            "hands_played": 1,
            "native_match_timeout_phase": "",
        }

    monkeypatch.setattr(national_native_mod, "run_native_tcp_smoke", fake_smoke)
    _install_sleep_noop(monkeypatch)

    errors, report = asyncio.run(
        tool_gates_native_smoke._run_workflow_smoke_gate(**_gate_kwargs(tmp_path))
    )
    assert report["passed"] is False
    assert report["outcome"] == "candidate_failure"
    assert len(calls) == 1  # no retry


def test_smoke_exception_treated_as_infra_and_retried(monkeypatch, tmp_path):
    """An exception from run_native_tcp_smoke is infra-class and is retried."""
    calls = []

    async def fake_smoke(bot_dir, **_kwargs):
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            raise OSError("transport stall")
        return {
            "passed": True,
            "execution_mode": "native_tcp",
            "outcome": "passed",
            "failure_side": "",
            "issues": [],
            "hands_played": 1,
            "native_match_timeout_phase": "",
        }

    monkeypatch.setattr(national_native_mod, "run_native_tcp_smoke", fake_smoke)
    _install_sleep_noop(monkeypatch)

    errors, report = asyncio.run(
        tool_gates_native_smoke._run_workflow_smoke_gate(**_gate_kwargs(tmp_path))
    )
    assert report["passed"] is True
    assert len(calls) == 2


def test_candidate_failure_with_zero_hands_is_retried(monkeypatch, tmp_path):
    """A run that never produced a hand (process killed at startup) is not a
    real candidate defect even if the fail-closed analyser attributed a
    handshake issue to the candidate — it is retried as infrastructure."""
    calls = []

    async def fake_smoke(bot_dir, **_kwargs):
        calls.append(len(calls) + 1)
        return {
            "passed": False,
            "execution_mode": "native_tcp",
            "outcome": "candidate_failure",
            "failure_side": "candidate",
            "issues": ["native_name_handshake_missing"],
            "hands_played": 0,
            "native_match_timeout_phase": "startup_watchdog",
        }

    monkeypatch.setattr(national_native_mod, "run_native_tcp_smoke", fake_smoke)
    _install_sleep_noop(monkeypatch)

    asyncio.run(
        tool_gates_native_smoke._run_workflow_smoke_gate(**_gate_kwargs(tmp_path))
    )
    assert len(calls) == 3  # retried because hands_played==0


def test_retry_count_respects_env_override(monkeypatch, tmp_path):
    """POK_NATIVE_SMOKE_MAX_ATTEMPTS overrides the retry ceiling."""
    calls = []

    async def fake_smoke(bot_dir, **_kwargs):
        calls.append(len(calls) + 1)
        return {
            "passed": False,
            "execution_mode": "native_tcp",
            "outcome": "infrastructure_failure",
            "failure_side": "harness",
            "issues": ["native_name_handshake_missing"],
            "hands_played": 0,
            "native_match_timeout_phase": "startup_watchdog",
        }

    monkeypatch.setattr(national_native_mod, "run_native_tcp_smoke", fake_smoke)
    _install_sleep_noop(monkeypatch)
    monkeypatch.setenv("POK_NATIVE_SMOKE_MAX_ATTEMPTS", "5")

    asyncio.run(
        tool_gates_native_smoke._run_workflow_smoke_gate(**_gate_kwargs(tmp_path))
    )
    assert len(calls) == 5
