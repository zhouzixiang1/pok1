"""Bounded native-match stream extension + provider-cycle task cancellation.

Extracted from orchestrator.py as a single business responsibility: bound one
orchestrator provider stream with at most one frozen native-match grace, and
cancel a cycle task while closing only its proven provider transport.

Members moved here (all re-exported by orchestrator.py):

* ``_orchestrator_cycle_cancel_grace``  -- read the cancel grace env budget.
* ``_orchestrator_task_error``  -- pull the exception object out of a task.
* ``_cancel_orchestrator_stream_task_bounded``  -- cancel one cycle task and
  close only its proven provider transport.
* ``_bounded_native_match_extension``,
  ``_native_match_extension_reproof``,
  ``_native_match_terminal_handoff_checkpoint_valid``,
  ``_consume_native_match_terminal_handoff``,
  ``_native_match_terminal_handoff_reproof``  -- thin delegates over the
  ``orchestrator_native_match_extension`` companion (kept here so the
  monkeypatch surface ``orchestrator._bounded_native_match_extension`` etc.
  remains unchanged).
* ``_await_orchestrator_stream_response_bounded``  -- bound one provider
  stream, with at most one frozen native-match grace.

IMPORTANT -- shared-symbol access model
---------------------------------------
Symbols referenced by these bodies that live in ``orchestrator`` are written
as ``_o.<name>`` so they resolve against the live ``orchestrator`` module
attribute, matching the pattern proven by ``orchestrator_branch_guard`` /
``orchestrator_post_generation``.  This covers:

* constant: ``ORCH_NATIVE_MATCH_REPROOF_INTERVAL_SEC``.
* the native-match companion handle: ``_nme`` (i.e. the live
  ``orchestrator._nme`` attribute).

``LLMProviderCleanupError`` and the owned-provider-attempt helpers are
imported directly from ``llm_query`` (stable imports, not monkeypatched on
``orchestrator``); ``log_system_event`` is imported directly from
``system_log``.
"""

from __future__ import annotations

import asyncio
import os
import time

import orchestrator as _o
from llm_query import (
    LLMProviderCleanupError,
    cleanup_owned_provider_attempt,
    mark_owned_provider_attempt_unresolved,
    owned_provider_attempt_exit_confirmed,
    owned_provider_attempt_scope,
)


def _orchestrator_cycle_cancel_grace():
    try:
        return max(
            0.0,
            min(
                30.0,
                float(os.environ.get("POK_ORCH_CYCLE_CANCEL_GRACE", "1")),
            ),
        )
    except (TypeError, ValueError):
        return 1.0


def _orchestrator_task_error(task):
    try:
        task.result()
    except BaseException as exc:
        return exc
    return None


async def _cancel_orchestrator_stream_task_bounded(
    stream_task,
    *,
    attempt_ref,
    gen_ref,
    reason,
    log_file_path,
):
    """Cancel one cycle task and close only its proven provider transport."""

    attempt = attempt_ref[0] if attempt_ref else None
    # Revoke native-match liveness before task cancellation/transport cleanup.
    # A detached tool coroutine can retain its ContextVar, so merely cancelling
    # the provider task is not enough to stop an old match from extending a
    # later SDK dispatch.
    if isinstance(attempt, dict):
        try:
            from pipeline_state import revoke_native_match_dispatch_nonce

            revoke_native_match_dispatch_nonce(str(attempt.get("attempt_id") or ""))
        except Exception:
            pass
    if stream_task.done():
        error = _o._orchestrator_task_error(stream_task)
        return error if isinstance(error, LLMProviderCleanupError) else None

    stream_task.cancel()
    grace = _o._orchestrator_cycle_cancel_grace()
    if grace > 0:
        await asyncio.wait({stream_task}, timeout=grace)
    if stream_task.done():
        error = _o._orchestrator_task_error(stream_task)
        return error if isinstance(error, LLMProviderCleanupError) else None

    query_gen = gen_ref[0] if gen_ref else None
    cleanup_error = None
    if isinstance(attempt, dict):
        # Do not add ``stream_task`` yet: it may currently be awaiting the
        # shared cleanup task in its own finally block. Tracking it before that
        # cleanup completes would create a self-dependency. Mark the attempt,
        # run/await the single shared cleanup, then retain the owner task only
        # if it still refuses to exit.
        mark_owned_provider_attempt_unresolved(attempt, reason)
        if query_gen is not None:
            try:
                with owned_provider_attempt_scope(attempt):
                    await cleanup_owned_provider_attempt(
                        query_gen,
                        attempt,
                        "ORCHESTRATOR",
                        log_file_path,
                    )
            except LLMProviderCleanupError as exc:
                cleanup_error = exc
            except BaseException as exc:
                cleanup_error = LLMProviderCleanupError(
                    "orchestrator owned provider cleanup failed: "
                    f"{type(exc).__name__}: {str(exc)[:300]}",
                    provider_exit_confirmed=False,
                    attempt_id=attempt.get("attempt_id"),
                )
                mark_owned_provider_attempt_unresolved(
                    attempt,
                    f"orchestrator_cleanup_failed:{type(exc).__name__}",
                )
        else:
            cleanup_error = LLMProviderCleanupError(
                "orchestrator provider attempt has no query generator for cleanup",
                provider_exit_confirmed=False,
                attempt_id=attempt.get("attempt_id"),
            )
    else:
        cleanup_error = LLMProviderCleanupError(
            "orchestrator cycle task resisted cancellation before provider ownership was published",
            provider_exit_confirmed=False,
        )

    post_cleanup_grace = max(0.1, grace)
    await asyncio.wait({stream_task}, timeout=post_cleanup_grace)
    if not stream_task.done():
        if isinstance(attempt, dict):
            mark_owned_provider_attempt_unresolved(
                attempt,
                f"{reason}:owner_task_pending",
                stream_task,
            )
            confirmed = owned_provider_attempt_exit_confirmed(attempt)
            cleanup_error = LLMProviderCleanupError(
                "orchestrator stream owner task remained pending after owned transport cleanup",
                provider_exit_confirmed=confirmed,
                attempt_id=attempt.get("attempt_id"),
            )
        else:
            stream_task.add_done_callback(_o._orchestrator_task_error)
        return cleanup_error

    task_error = _o._orchestrator_task_error(stream_task)
    if cleanup_error is None and isinstance(task_error, LLMProviderCleanupError):
        cleanup_error = task_error
    if isinstance(attempt, dict):
        owned_provider_attempt_exit_confirmed(attempt)
    return cleanup_error


def _bounded_native_match_extension(
    *,
    stream_started_epoch: float,
    original_deadline_epoch: float,
    provider_dispatch_nonce: str | None,
) -> dict | None:
    """Delegate to orchestrator_native_match_extension."""
    return _o._nme._bounded_native_match_extension(
        stream_started_epoch=stream_started_epoch,
        original_deadline_epoch=original_deadline_epoch,
        provider_dispatch_nonce=provider_dispatch_nonce,
    )


def _native_match_extension_reproof(previous: dict, fresh: dict | None) -> bool:
    """Delegate to orchestrator_native_match_extension."""
    return _o._nme._native_match_extension_reproof(previous, fresh)


def _native_match_terminal_handoff_checkpoint_valid(
    extension: dict,
    receipt: dict,
) -> bool:
    """Delegate to orchestrator_native_match_extension."""
    return _o._nme._native_match_terminal_handoff_checkpoint_valid(extension, receipt)


def _consume_native_match_terminal_handoff(
    extension: dict,
    *,
    observed_at_epoch: float,
) -> dict | None:
    """Delegate to orchestrator_native_match_extension."""
    return _o._nme._consume_native_match_terminal_handoff(
        extension,
        observed_at_epoch=observed_at_epoch,
    )


def _native_match_terminal_handoff_reproof(
    state: dict | None,
    *,
    observed_at_epoch: float,
) -> bool:
    """Delegate to orchestrator_native_match_extension."""
    return _o._nme._native_match_terminal_handoff_reproof(
        state,
        observed_at_epoch=observed_at_epoch,
    )


async def _await_orchestrator_stream_response_bounded(
    stream_coro,
    *,
    timeout,
    attempt_ref,
    gen_ref,
    log_file_path,
):
    """Bound one provider stream, with at most one frozen native-match grace."""

    stream_task = asyncio.create_task(stream_coro)
    stream_started_epoch = time.time()
    original_deadline_epoch = stream_started_epoch + float(timeout)
    wait_deadline_monotonic = time.monotonic() + float(timeout)
    native_extension_granted = False
    native_extension_state = None
    terminal_handoff_state = None
    provider_dispatch_nonce = None
    dispatch_revoked = False

    def revoke_dispatch():
        nonlocal dispatch_revoked
        if dispatch_revoked:
            return
        attempt = attempt_ref[0] if attempt_ref else None
        nonce = provider_dispatch_nonce
        if not nonce and isinstance(attempt, dict):
            nonce = str(attempt.get("attempt_id") or "")
        if not nonce:
            return
        try:
            from pipeline_state import revoke_native_match_dispatch_nonce

            revoke_native_match_dispatch_nonce(nonce)
            dispatch_revoked = True
        except Exception:
            pass

    try:
        try:
            while True:
                remaining = max(0.0, wait_deadline_monotonic - time.monotonic())
                poll_timeout = remaining
                if native_extension_granted and terminal_handoff_state is None:
                    poll_timeout = min(
                        remaining,
                        max(0.01, _o.ORCH_NATIVE_MATCH_REPROOF_INTERVAL_SEC),
                    )
                done, _pending = await asyncio.wait(
                    {stream_task},
                    timeout=poll_timeout,
                )
                observed_at = time.time()
                if stream_task in done:
                    if not native_extension_granted:
                        return stream_task.result()
                    if terminal_handoff_state is not None:
                        if _o._native_match_terminal_handoff_reproof(
                            terminal_handoff_state,
                            observed_at_epoch=observed_at,
                        ):
                            return stream_task.result()
                    else:
                        # Completion is accepted only with the exact last live
                        # proof or the runner's one-shot terminal replacement.
                        fresh = _o._bounded_native_match_extension(
                            stream_started_epoch=stream_started_epoch,
                            original_deadline_epoch=original_deadline_epoch,
                            provider_dispatch_nonce=provider_dispatch_nonce,
                        )
                        if _o._native_match_extension_reproof(
                            native_extension_state,
                            fresh,
                        ):
                            return stream_task.result()
                        terminal_handoff_state = (
                            _o._consume_native_match_terminal_handoff(
                                native_extension_state,
                                observed_at_epoch=observed_at,
                            )
                        )
                        if terminal_handoff_state is not None:
                            # No later match under this dispatch may borrow the
                            # consumed receipt.  The outer in-memory state now
                            # owns only its fixed, non-renewable handoff window.
                            revoke_dispatch()
                            if _o._native_match_terminal_handoff_reproof(
                                terminal_handoff_state,
                                observed_at_epoch=observed_at,
                            ):
                                return stream_task.result()
                    revoke_dispatch()
                    _o.log_system_event(
                        "pipeline.orchestrator_native_match_extension_revoked",
                        "error",
                        "A completed provider stream lost its final exact native-match proof.",
                        {
                            "provider_dispatch_nonce": provider_dispatch_nonce,
                            "match_identity_digest": (
                                (native_extension_state or {}).get("progress") or {}
                            ).get("match_identity_digest"),
                        },
                    )
                    break
                if not native_extension_granted:
                    attempt = attempt_ref[0] if attempt_ref else None
                    provider_dispatch_nonce = (
                        str(attempt.get("attempt_id") or "")
                        if isinstance(attempt, dict)
                        else None
                    )
                    extension = _o._bounded_native_match_extension(
                        stream_started_epoch=stream_started_epoch,
                        original_deadline_epoch=original_deadline_epoch,
                        provider_dispatch_nonce=provider_dispatch_nonce,
                    )
                    if extension is not None:
                        native_extension_granted = True
                        native_extension_state = extension
                        extended_deadline = float(extension["deadline_epoch"])
                        wait_deadline_monotonic = (
                            time.monotonic()
                            + max(0.0, extended_deadline - time.time())
                        )
                        progress = extension["progress"]
                        _o.log_system_event(
                            "pipeline.orchestrator_native_match_extension_granted",
                            "warn",
                            "Granted one bounded provider-cycle extension for a live "
                            "checkpoint-bound native TCP match.",
                            {
                                "owner_tool": progress.get("owner_tool"),
                                "stage": (extension["checkpoint"] or {}).get("stage"),
                                "match_identity_digest": progress.get(
                                    "match_identity_digest"
                                ),
                                "timing_plan_digest": progress.get(
                                    "timing_plan_digest"
                                ),
                                "event_seq": progress.get("event_seq"),
                                "phase_deadline_epoch": progress.get(
                                    "phase_deadline_epoch"
                                ),
                                "operation_deadline_epoch": progress.get(
                                    "operation_deadline_epoch"
                                ),
                                "extension_deadline_epoch": extended_deadline,
                                "absolute_cap_epoch": extension.get("cap_epoch"),
                            },
                        )
                        continue
                elif terminal_handoff_state is None:
                    fresh = _o._bounded_native_match_extension(
                        stream_started_epoch=stream_started_epoch,
                        original_deadline_epoch=original_deadline_epoch,
                        provider_dispatch_nonce=provider_dispatch_nonce,
                    )
                    if _o._native_match_extension_reproof(
                        native_extension_state,
                        fresh,
                    ):
                        native_extension_state = fresh
                        extended_deadline = float(fresh["deadline_epoch"])
                        wait_deadline_monotonic = (
                            time.monotonic()
                            + max(0.0, extended_deadline - time.time())
                        )
                        continue
                    terminal_handoff_state = _o._consume_native_match_terminal_handoff(
                        native_extension_state,
                        observed_at_epoch=observed_at,
                    )
                    if terminal_handoff_state is not None:
                        revoke_dispatch()
                        handoff_deadline = float(
                            terminal_handoff_state["deadline_epoch"]
                        )
                        wait_deadline_monotonic = (
                            time.monotonic()
                            + max(0.0, handoff_deadline - time.time())
                        )
                        _o.log_system_event(
                            "pipeline.orchestrator_native_match_terminal_handoff",
                            "info",
                            "Consumed one exact runner terminal receipt; awaiting only "
                            "the fixed provider-result handoff window.",
                            {
                                "provider_dispatch_nonce": provider_dispatch_nonce,
                                "match_identity_digest": (
                                    terminal_handoff_state["receipt"].get(
                                        "match_identity_digest"
                                    )
                                ),
                                "terminal_event_seq": (
                                    terminal_handoff_state["receipt"].get(
                                        "terminal_event_seq"
                                    )
                                ),
                                "handoff_deadline_epoch": handoff_deadline,
                            },
                        )
                        continue
                    revoke_dispatch()
                    _o.log_system_event(
                        "pipeline.orchestrator_native_match_extension_revoked",
                        "error",
                        "A granted native-match extension lost its exact live proof.",
                        {
                            "provider_dispatch_nonce": provider_dispatch_nonce,
                            "match_identity_digest": (
                                (native_extension_state or {}).get("progress") or {}
                            ).get("match_identity_digest"),
                        },
                    )
                    break
                if native_extension_granted:
                    _o.log_system_event(
                        "pipeline.orchestrator_native_match_extension_exhausted",
                        "error",
                        "The one bounded native-match provider extension expired.",
                        {"timeout_sec": float(timeout)},
                    )
                break
        except BaseException:
            await _o._cancel_orchestrator_stream_task_bounded(
                stream_task,
                attempt_ref=attempt_ref,
                gen_ref=gen_ref,
                reason="orchestrator_cycle_parent_cancellation_unconfirmed",
                log_file_path=log_file_path,
            )
            raise
        cleanup_error = await _o._cancel_orchestrator_stream_task_bounded(
            stream_task,
            attempt_ref=attempt_ref,
            gen_ref=gen_ref,
            reason="orchestrator_cycle_timeout_cancellation_unconfirmed",
            log_file_path=log_file_path,
        )
        timeout_error = asyncio.TimeoutError(
            f"orchestrator SDK stream exceeded cycle timeout {float(timeout):.1f}s"
        )
        if cleanup_error is not None:
            raise timeout_error from cleanup_error
        raise timeout_error
    finally:
        revoke_dispatch()
