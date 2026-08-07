"""P0-3: one-ahead draft resilience tests.

P0-3a: a transient exception inside ``_draft_prepare_task`` must PRESERVE the
        draft checkpoint (not blanket-clear it) so the completed expensive LLM
        work survives a transient LLM/network/infra failure.

P0-3b: boot-time orphan draft reconcile reaps a mid-flight draft (breaking the
        launch deadlock) while preserving (best-effort promoting) a complete
        ``workers_done`` buffer.
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.usefixtures("synthetic_checkpoint_authority")

import orchestrator  # noqa: F401  (ensures companion modules initialize)
import orchestrator_loop_phases
from evolution_infra import (
    pipeline_state_path,
    read_pipeline_checkpoint,
    write_pipeline_checkpoint,
)


# ---------------------------------------------------------------------------
# P0-3a: transient exception PRESERVES the draft checkpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_draft_prepare_task_preserves_checkpoint_on_runtime_error(monkeypatch):
    """A generic RuntimeError propagating out of the draft cycle must NOT clear
    the draft checkpoint (P0-3a: preserve expensive draft work on transient
    failures)."""
    # Seed a mid-flight draft checkpoint representing completed LLM work.
    write_pipeline_checkpoint(
        next_v=12, source_v=11, stage="master_planned", slot_id="draft"
    )
    assert read_pipeline_checkpoint(slot_id="draft") is not None

    async def raising_draft_cycle(ui, shutdown_mgr, gen_count):
        raise RuntimeError("transient LLM blip")

    monkeypatch.setattr(
        orchestrator_loop_phases, "_run_draft_cycle", raising_draft_cycle
    )

    # _draft_prepare_task is module-level so we can call it directly, exercising
    # the real production exception handler without mocking slice2b activation.
    await orchestrator_loop_phases._draft_prepare_task(None, None, 11)

    # Core assertion: the draft checkpoint SURVIVES the transient error.
    assert read_pipeline_checkpoint(slot_id="draft") is not None, (
        "draft checkpoint was wiped by a transient error (P0-3a regression)"
    )


@pytest.mark.asyncio
async def test_draft_prepare_task_preserves_on_claude_sdk_error(monkeypatch):
    """A ClaudeSDKError (infra class) must also preserve the draft."""
    from llm_query import ClaudeSDKError

    write_pipeline_checkpoint(
        next_v=13, source_v=11, stage="direction_audited", slot_id="draft"
    )

    async def raising_draft_cycle(ui, shutdown_mgr, gen_count):
        raise ClaudeSDKError("529 overload")

    monkeypatch.setattr(
        orchestrator_loop_phases, "_run_draft_cycle", raising_draft_cycle
    )

    await orchestrator_loop_phases._draft_prepare_task(None, None, 11)

    assert read_pipeline_checkpoint(slot_id="draft") is not None


@pytest.mark.asyncio
async def test_draft_prepare_task_preserves_on_timeout_error(monkeypatch):
    """asyncio.TimeoutError is an infra error and must preserve the draft."""
    import asyncio as _asyncio

    write_pipeline_checkpoint(
        next_v=14, source_v=11, stage="prepared", slot_id="draft"
    )

    async def raising_draft_cycle(ui, shutdown_mgr, gen_count):
        raise _asyncio.TimeoutError()

    monkeypatch.setattr(
        orchestrator_loop_phases, "_run_draft_cycle", raising_draft_cycle
    )

    await orchestrator_loop_phases._draft_prepare_task(None, None, 11)

    assert read_pipeline_checkpoint(slot_id="draft") is not None


# ---------------------------------------------------------------------------
# P0-3b: boot-time orphan draft reconcile
# ---------------------------------------------------------------------------


def test_reconcile_reaps_mid_flight_draft():
    """A mid-flight orphan draft (non-workers_done) is reaped at boot, breaking
    the _try_launch_draft_prepare deadlock."""
    draft_path = pipeline_state_path("draft")
    draft_path.write_text(json.dumps({
        "next_v": 12,
        "source_v": 11,
        "stage": "master_planned",
    }))

    assert read_pipeline_checkpoint(slot_id="draft") is not None

    orchestrator_loop_phases._reconcile_orphan_draft_at_boot(None)

    # Mid-flight draft reaped -> no deadlock.
    assert read_pipeline_checkpoint(slot_id="draft") is None, (
        "mid-flight orphan draft was not reaped (P0-3b deadlock regression)"
    )


def test_reconcile_preserves_workers_done_draft():
    """A complete workers_done draft is NOT reaped (the one-ahead buffer
    survives restart).  Promotion is best-effort: if it succeeds the work moves
    to the primary slot (draft file cleared as part of promotion); if it
    refuses the draft stays in place.  Either way the pre-computed candidate is
    never destroyed.

    The draft's next_v must be AHEAD of the live published high-water, otherwise
    the (correct) stale-draft reaper would reap it.  We pick a version far above
    any plausible published high-water so the test is robust to a real checkout
    that already has published bots."""
    from evolution_infra import read_pipeline_checkpoint as _read_primary
    from epoch_authority import strict_epoch_projection

    projection = strict_epoch_projection()
    published_high_water = int(projection.get("published_high_water") or 0)
    ahead_next_v = max(published_high_water + 5, 100)

    draft_path = pipeline_state_path("draft")
    draft_path.write_text(json.dumps({
        "next_v": ahead_next_v,
        "source_v": ahead_next_v - 1,
        "stage": "workers_done",
    }))

    assert read_pipeline_checkpoint(slot_id="draft") is not None

    orchestrator_loop_phases._reconcile_orphan_draft_at_boot(None)

    # The workers_done work survives in two of three places:
    #   (a) still in the draft slot (promotion refused/not stale), or
    #   (b) promoted to the primary slot (draft cleared on success).
    draft = read_pipeline_checkpoint(slot_id="draft")
    primary = _read_primary()
    buffer_survives = (
        (draft is not None and draft.get("stage") == "workers_done")
        or (primary is not None and primary.get("stage") == "workers_done")
    )
    assert buffer_survives, (
        "workers_done draft was reaped instead of preserved/promoted (P0-3b)"
    )


def test_reconcile_noop_when_no_draft():
    """No orphan draft -> reconcile is a cheap no-op."""
    assert read_pipeline_checkpoint(slot_id="draft") is None
    # Must not raise.
    orchestrator_loop_phases._reconcile_orphan_draft_at_boot(None)
    assert read_pipeline_checkpoint(slot_id="draft") is None


# ---------------------------------------------------------------------------
# P0-3c: _try_launch_draft_prepare offloads read_all_pipeline_checkpoints
#        off the ASGI event loop (create_task stays on the loop)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_try_launch_draft_prepare_offloads_slot_scan_off_event_loop(monkeypatch):
    """The blocking slot-scan read in _try_launch_draft_prepare MUST run in a
    worker thread (AGENTS.md blocking boundary), while the inner
    create_task(_draft_prepare_task) still runs on the event loop.

    Regression guard for the second py-spy-confirmed blocking path: during
    eval-wait the orchestrator fires _try_launch_draft_prepare every ~30s via
    the _eval_wait_draft_launcher hook. read_all_pipeline_checkpoints globs +
    parses every pipeline_state slot JSON; running it inline starves HTTP.
    """
    import asyncio
    import threading
    from types import SimpleNamespace

    import evolution_infra
    import orchestrator as _orch
    import producer_consumer_slice2b_activation as _slice2b_act

    loop_thread = threading.current_thread()
    read_threads = []

    # Drive _try_launch_draft_prepare past every guard up to the slot scan.
    monkeypatch.setattr(_slice2b_act, "slice2b_active", lambda: True)

    _fake_activation = SimpleNamespace(
        producer_may_draft_behind=lambda: True,
        producer_may_draft_ahead_of_eval=lambda: True,
        coordinator=SimpleNamespace(max_ahead=1),
    )
    monkeypatch.setattr(_orch, "_slice2b_ensure_activation", lambda: _fake_activation)

    # Record the executing thread of the blocking read; return no occupied
    # slots so the slot-pick loop launches exactly one draft.
    def _record_read_thread():
        read_threads.append(threading.current_thread())
        return {}

    monkeypatch.setattr(evolution_infra, "read_all_pipeline_checkpoints", _record_read_thread)

    # Capture whether _draft_prepare_task was scheduled via create_task ON the
    # event loop (it must NOT move to a worker thread). Replace it with a
    # no-op coroutine that records the scheduling thread.
    scheduled_threads = []

    async def _noop_draft_task(ui, shutdown_mgr, gen_count, slot_id="draft"):
        scheduled_threads.append(threading.current_thread())

    monkeypatch.setattr(orchestrator_loop_phases, "_draft_prepare_task", _noop_draft_task)

    # _try_launch_draft_prepare is async (the hook schedules it via create_task).
    await orchestrator_loop_phases._try_launch_draft_prepare(None, None, 0)
    # Yield to the loop so the create_task'd _draft_prepare_task actually runs
    # and records its scheduling thread.
    await asyncio.sleep(0)

    # The blocking slot scan MUST have executed ...
    assert read_threads, "read_all_pipeline_checkpoints was not reached"
    # ... and EVERY invocation ran off the event-loop thread.
    for thread in read_threads:
        assert thread is not loop_thread, (
            "read_all_pipeline_checkpoints ran on the event-loop thread "
            f"(ident={thread.ident}); it must be offloaded to a worker thread"
        )
    # The inner draft task MUST have been scheduled on the event-loop thread
    # (create_task must not move to a worker thread).
    assert scheduled_threads, "_draft_prepare_task was never scheduled"
    for thread in scheduled_threads:
        assert thread is loop_thread, (
            "_draft_prepare_task ran off the event-loop thread "
            f"(ident={thread.ident}); create_task must stay on the loop"
        )

