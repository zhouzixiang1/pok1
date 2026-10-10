"""Bounded-container releases for the two high-confidence heap residuals.

2026-10-10 heap recon (fixable_now + high confidence only):

* ``strict_authority_workflow._OBSERVED_PROVIDER_RESULTS`` — an unbounded
  ``id(result) -> (invocation_id, effect_id)`` dict whose entries were
  popped ONLY on ``complete_provider_call``'s fully-validated success path;
  every schema-reject / timeout / cancel / retry escape leaked its rows
  forever. Fixed by ``release_observed_provider_results`` called from the
  ``run_claude_query`` strict-capture ``finally`` (scope-exit cleanup; no
  cap-eviction that could break a later validation).
* ``Slice2bActivation`` four per-candidate registries
  (``_consumer_tasks`` / ``_sealed_snapshots`` / ``_dispatch_clocks`` /
  ``_scheduled_factories``) — write-only for the process lifetime. Fixed
  by ``release_terminal_candidate`` at the terminal points (consumer-task
  finally + the promotion barrier's ``await_promotion``). The persisted
  lifecycle stays the source of truth, so no reader observably changes.
"""

from __future__ import annotations

import inspect

from claude_agent_sdk import ResultMessage

import llm_query
import producer_consumer_slice2b_activation as s2a
import strict_authority_workflow


# --- _OBSERVED_PROVIDER_RESULTS scope-exit release ----------------------------


def _make_result() -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=10,
        duration_api_ms=10,
        is_error=False,
        num_turns=1,
        session_id="bounded-release-session",
        total_cost_usd=0.0,
        usage={},
    )


def test_release_drops_entries_and_is_idempotent():
    strict_authority_workflow._OBSERVED_PROVIDER_RESULTS.clear()
    result = _make_result()
    strict_authority_workflow._observe_provider_result(
        result, invocation_id="inv-1", effect_id="eff-1"
    )
    assert id(result) in strict_authority_workflow._OBSERVED_PROVIDER_RESULTS

    strict_authority_workflow.release_observed_provider_results([result])
    assert not strict_authority_workflow._OBSERVED_PROVIDER_RESULTS
    # Idempotent (the success path's consume may already have popped).
    strict_authority_workflow.release_observed_provider_results([result])
    assert not strict_authority_workflow._OBSERVED_PROVIDER_RESULTS
    # A released result can never satisfy a later validation.
    assert not strict_authority_workflow._provider_results_were_observed(
        [result], invocation_id="inv-1", effect_id="eff-1"
    )


def test_consume_path_still_works_alone():
    strict_authority_workflow._OBSERVED_PROVIDER_RESULTS.clear()
    result = _make_result()
    strict_authority_workflow._observe_provider_result(
        result, invocation_id="inv-2", effect_id="eff-2"
    )
    assert strict_authority_workflow._provider_results_were_observed(
        [result], invocation_id="inv-2", effect_id="eff-2"
    )
    strict_authority_workflow._consume_observed_provider_results([result])
    assert not strict_authority_workflow._OBSERVED_PROVIDER_RESULTS


def test_run_claude_query_finally_releases_the_capture_scope():
    """Source wiring: the strict-capture finally releases the observed
    registrations for its capture's results (the leak was exactly the
    failure paths that never reached complete_provider_call)."""
    source = inspect.getsource(llm_query.run_claude_query)
    assert "release_observed_provider_results" in source


# --- slice2b per-candidate registries -----------------------------------------


class _LedgerStub:
    def __init__(self, terminal_ids):
        self._terminal = set(terminal_ids)

    def is_terminal(self, candidate_id):
        if candidate_id == "raising":
            raise RuntimeError("ledger unavailable")
        return candidate_id in self._terminal


def _stub_activation(terminal_ids):
    activation = s2a.Slice2bActivation.__new__(s2a.Slice2bActivation)
    activation.ledger = _LedgerStub(terminal_ids)
    activation._consumer_tasks = {"term": "task-obj", "live": "task-live"}
    activation._sealed_snapshots = {"term": {"big": "snapshot"}, "live": {}}
    activation._dispatch_clocks = {"term": 1.0, "live": 2.0}
    activation._scheduled_factories = {"term": ("factory",), "live": ()}
    return activation


def test_release_terminal_candidate_drops_all_four_registers():
    activation = _stub_activation({"term"})
    assert activation.release_terminal_candidate("term") is True
    assert activation._consumer_tasks == {"live": "task-live"}
    assert activation._sealed_snapshots == {"live": {}}
    assert activation._dispatch_clocks == {"live": 2.0}
    assert activation._scheduled_factories == {"live": ()}


def test_release_refuses_non_terminal_and_failing_ledger():
    activation = _stub_activation({"term"})
    # Non-terminal: registers stay (the consumer may still be resumed).
    assert activation.release_terminal_candidate("live") is False
    assert "live" in activation._sealed_snapshots
    # Ledger read failure: fail-closed no-op.
    assert activation.release_terminal_candidate("raising") is False
    assert "term" in activation._sealed_snapshots


def test_terminal_release_wired_at_both_terminal_points():
    consumer_finally = inspect.getsource(
        s2a.Slice2bActivation.launch_consumer_task
    )
    assert "self.release_terminal_candidate(candidate_id)" in consumer_finally
    barrier = inspect.getsource(s2a.Slice2bActivation.await_promotion)
    assert "self.release_terminal_candidate(candidate_id)" in barrier
