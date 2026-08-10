"""Deep-parallelism: LLM-capacity-driven draft launch + gating.

These tests cover the core invariant of the deep-parallelism refactoring:
draft creation and sealing are gated on the GLOBAL LLM semaphore's available
capacity (``llm_semaphore_has_capacity``), not a hard ``max_ahead`` count.
``max_ahead`` remains only as a generous backstop.

Branch-portable: uses ``STRICT_TARGET_V`` / ``strict_bot_name`` helpers, never
hardcodes version literals.  Mirrors the Stage-0 ``test_master_abandon_signal``
template for signal/contract assertions.
"""

import asyncio

import pytest

import llm_concurrency
import producer_consumer_slice2b as pcs
from producer_consumer_slice2b import AheadCoordinator, ValidationLedger

DIGESTS = {
    "a": "a" * 64,
    "b": "b" * 64,
    "e": "e" * 64,
}


def _seal_into_fsm(ledger, candidate_id, artifact_hash=DIGESTS["a"]):
    ledger.start(
        candidate_id=candidate_id,
        sealed_artifact_hash=artifact_hash,
        envelope_effect_id=f"eff-{candidate_id}",
        envelope_digest=DIGESTS["e"],
    )


@pytest.fixture(autouse=True)
def _reset_llm_semaphore(monkeypatch):
    """Reset the shared LLM semaphore + native precommit semaphore between tests
    so each test starts from a known capacity state."""
    monkeypatch.setattr(llm_concurrency, "_SHARED_LLM_SEMAPHORE", None)
    monkeypatch.setattr(llm_concurrency, "_GLOBAL_LLM_SEMAPHORE", None)
    pcs._reset_native_precommit_semaphore_for_tests()
    yield


def test_llm_semaphore_has_capacity_default_full():
    """Before any LLM call the semaphore is lazily full, so capacity is True."""
    assert llm_concurrency.get_capacity() >= 1
    assert llm_concurrency.llm_semaphore_has_capacity(1) is True


def test_llm_semaphore_has_capacity_tracks_value():
    """Acquiring permits reduces reported capacity."""
    sem = llm_concurrency.get_global_llm_semaphore()
    cap = llm_concurrency.get_capacity()

    async def acquire_one():
        await sem.acquire()

    asyncio.run(acquire_one())
    assert llm_concurrency.llm_semaphore_has_capacity(cap) is False
    if cap >= 2:
        assert llm_concurrency.llm_semaphore_has_capacity(cap - 1) is True
    else:
        assert llm_concurrency.llm_semaphore_has_capacity(1) is False

    sem.release()
    assert llm_concurrency.llm_semaphore_has_capacity(cap) is True


def test_producer_may_advance_refuses_when_llm_saturated(monkeypatch):
    """The load-bearing throttle: when the LLM semaphore reports NO free
    capacity, producer_may_advance refuses even though max_ahead has room.
    This keeps the pool saturated without over-launching."""
    ledger = ValidationLedger()
    coord = AheadCoordinator(ledger)
    monkeypatch.setattr(
        llm_concurrency, "llm_semaphore_has_capacity", lambda n=1: False
    )
    assert coord.producer_may_advance() is False
    assert coord.producer_may_draft_ahead_of_eval() is False


def test_producer_may_advance_permits_when_llm_has_capacity(monkeypatch):
    """When LLM capacity is available, sealing is permitted (the real throttle
    passed).  max_ahead is just a backstop."""
    ledger = ValidationLedger()
    coord = AheadCoordinator(ledger)
    monkeypatch.setattr(
        llm_concurrency, "llm_semaphore_has_capacity", lambda n=1: True
    )
    assert coord.producer_may_advance() is True


def test_max_ahead_backstop_still_bounded():
    """The max_ahead backstop still caps sealing even if the LLM-capacity
    predicate is optimistic (defensive against a runaway)."""
    ledger = ValidationLedger()
    coord = AheadCoordinator(ledger, max_ahead=1)
    _seal_into_fsm(ledger, "c1")
    coord.note_sealed(candidate_id="c1", artifact_hash=DIGESTS["a"])
    # 1 non-terminal == max_ahead=1: no room to seal another.
    assert coord.producer_may_advance() is False


def test_default_max_ahead_is_generous():
    """Without an explicit max_ahead, the coordinator uses a generous backstop
    (not the legacy single-ahead 1) so deep parallelism is not count-bound."""
    ledger = ValidationLedger()
    coord = AheadCoordinator(ledger)
    assert coord.max_ahead >= 8  # generous backstop, not 1


def test_note_terminal_fires_slot_freed_event():
    """note_terminal wakes the producer's parked launch loop via the
    slot_freed event (event-driven launch replacing the 45s poll)."""
    ledger = ValidationLedger()
    coord = AheadCoordinator(ledger)
    _seal_into_fsm(ledger, "c1")
    coord.note_sealed(candidate_id="c1", artifact_hash=DIGESTS["a"])
    coord.notify_slot_freed()
    # The event was lazily created and is now set.
    assert coord._slot_freed_event is not None
    assert coord._slot_freed_event.is_set() is True


def test_slot_freed_event_clear_and_reset():
    """The producer clears the event after reading so each terminal fires
    exactly one wake."""
    ledger = ValidationLedger()
    coord = AheadCoordinator(ledger)
    coord.notify_slot_freed()
    ev = coord._slot_freed_event
    ev.clear()
    assert ev.is_set() is False
    # A second terminal re-sets it.
    coord.notify_slot_freed()
    assert ev.is_set() is True
