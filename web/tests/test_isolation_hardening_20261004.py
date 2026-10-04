"""Test-isolation hardening for runtime-state bindings (full-suite failures).

A full-suite run executed inside the live autonomous checkout
(``.evolution_pok``, with ``pok-evolution.service`` actively evolving) failed
17 tests that all pass in a quiet operator checkout.  Root-caused classes:

* ``tool_bot_management`` (and the reap/archivist chain reading
  ``_tbm.RESULTS_DIR``) holds a ``from evolution_infra import RESULTS_DIR``
  binding copy that the autouse ``isolate_state`` fixture never patched, so
  ``evaluation_cycle_lock`` flocked the REAL results directory and timed out
  against the live daemon (reproduced: holding the real
  ``.evaluation_cycle.lock`` makes
  ``test_post_publication_effect_executor::test_executor_crash_after_effect_
  resumes_from_persisted_plan`` time out at 30s).
* the ``rate_limiter`` singleton binds its state file to the REAL
  ``RESULTS_DIR`` at import time and loads it once; a live GLM 1308 quota
  block (or any earlier test's ``parse_429``) then makes every
  ``run_claude_query`` entry point ``await wait_until_reset()`` for up to
  hours, timing out 10 LLM-role tests at 30s (reproduced by writing a future
  ``reset_time`` into the real state file).  The in-memory ``_reset_time``
  also leaks ACROSS tests because nothing resets the singleton.
* the active-tree provider scans recurse into the gitignored runtime
  artifact tree ``web/core/results/`` (1.2GB, 200+ directories in the live
  checkout), timing out two registry scans at 30s (reproduced in the live
  checkout; the fix excludes runtime artifacts from the ACTIVE-tree scan,
  which by contract covers active code only).

These tests pin the isolation so the suite stays green regardless of the
runtime state of the checkout it runs in.
"""

import sys
from pathlib import Path

import pytest

WEB_DIR = Path(__file__).resolve().parents[1]
CORE_DIR = WEB_DIR / "core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))


def test_tool_bot_management_results_dir_is_isolated():
    """The publication/reap chain must lock the ISOLATED results dir.

    ``tool_bot_management`` imported ``RESULTS_DIR`` by value, so the autouse
    ``evolution_infra.RESULTS_DIR`` patch never reached it and
    ``evaluation_cycle_lock(_tbm.RESULTS_DIR, ...)`` flocked the real
    directory — timing out whenever an external process (the live daemon or
    service) holds the publication lock.
    """
    import evolution_infra
    import tool_bot_management as tbm

    assert tbm.RESULTS_DIR == evolution_infra.RESULTS_DIR, (
        "tool_bot_management.RESULTS_DIR must follow the isolated "
        "evolution_infra.RESULTS_DIR; a stale real-checkout binding makes "
        "evaluation_cycle_lock contend with the live daemon "
        "(test_post_publication_effect_executor 30s flock timeout)"
    )


def test_rate_limiter_singleton_is_isolated_and_unblocked():
    """The rate-limiter singleton must read/write the ISOLATED state file.

    The singleton binds its state file at import time from the REAL results
    directory and keeps an in-memory ``_reset_time`` across tests.  A live
    quota block loaded at import (or set by an earlier test's parse_429)
    then makes every run_claude_query entry point wait for the real reset
    time — an hours-long wait inside a 30s test budget.
    """
    import evolution_infra
    from rate_limiter import rate_limiter

    assert Path(rate_limiter._state_file).parent == Path(
        evolution_infra.RESULTS_DIR
    ), (
        "rate_limiter singleton must point at the isolated results dir, not "
        "the real checkout's rate_limit_state.json"
    )
    assert rate_limiter._reset_time is None, (
        "an earlier test or an import-time load left a live reset_time in "
        "the singleton; it must be cleared per-test or every "
        "run_claude_query test waits on the real quota window"
    )
    assert rate_limiter.is_blocked() is False
    assert rate_limiter.wait_seconds() == 0.0


def test_rate_limiter_save_state_writes_isolated_file():
    """A test that trips parse_429 must not poison the real checkout.

    The singleton's _save_state writes whatever _state_file points at; under
    isolation that is the per-test tmp dir, so quota experiments inside the
    suite can never block the live service's own rate_limit_state.json.
    """
    import json
    from datetime import datetime, timedelta

    import evolution_infra
    from rate_limiter import rate_limiter

    real = (
        Path(__file__).resolve().parents[2] / "web" / "core" / "results"
        / "rate_limit_state.json"
    )
    # Snapshot BEFORE tripping parse_429: whatever the real checkout's state
    # file already contained (an externally pre-existing block, e.g. a live
    # quota window) is the environment's business, not this suite's.
    before = real.read_bytes() if real.exists() else None

    # Near-future reset (a 9999 date overflows the local-timezone→UTC
    # conversion inside parse_429 and is silently ignored by design).
    reset_at = (datetime.now() + timedelta(hours=2)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    assert rate_limiter.parse_429(
        "Request rejected (429) · [1308][已达到 5 小时的使用上限。"
        f"您的限额将在 {reset_at} 重置。]"
    ) is True

    isolated = Path(evolution_infra.RESULTS_DIR) / "rate_limit_state.json"
    assert isolated.exists(), (
        "parse_429 must persist the block into the ISOLATED state file"
    )
    data = json.loads(isolated.read_text())
    assert data.get("reset_time") is not None

    after = real.read_bytes() if real.exists() else None
    assert after == before, (
        "the real checkout's rate_limit_state.json must never be written "
        "by an isolated test"
    )
    # Restore the in-memory block cleared state for the following tests.
    rate_limiter._reset_time = None
