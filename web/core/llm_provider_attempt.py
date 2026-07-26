"""Owned provider-attempt lifecycle + terminal-abandon result cache.

Extracted from llm_query.py as a single business responsibility: transport
ownership, cleanup, exit confirmation, bounded cancel, and the verified
terminal-abandon result cache that bridges transport-loss across attempts.
The two halves share the _LLM_PROVIDER_ATTEMPT ContextVar and the
_PROVIDER_CLEANUP_LOCK.

All public symbols are re-exported by llm_query.py for backward compatibility.
"""

import asyncio
import contextlib
import contextvars
import json
import os
import threading
import time
import uuid
from copy import deepcopy

# NOTE: ``llm_query`` is imported lazily inside the few functions that need
# cross-references to its internal helpers (_LLM_TOTAL_DEADLINE,
# _emit_llm_event, _role_log_metadata, _new_owned_sdk_transport).  A top-level
# import would create a circular dependency because llm_query.py imports this
# module at its own top level to re-export the ContextVar and lock.


# ---------------------------------------------------------------------------
# Module globals / constants
# ---------------------------------------------------------------------------

# This ContextVar is defined EXACTLY ONCE here.  llm_query.py re-exports the
# SAME object via assignment so the ``is``-identity contract is preserved for
# callers that compare or reset tokens across module boundaries.
_LLM_PROVIDER_ATTEMPT = contextvars.ContextVar(
    "llm_provider_attempt", default=None
)

_PROVIDER_CLEANUP_LOCK = threading.Lock()
_UNRESOLVED_PROVIDER_ATTEMPTS = {}

_CANONICAL_ABANDON_RESULT_FIELDS = frozenset({
    "abandoned",
    "cleared_checkpoint",
    "workflow_run_id",
    "abandon_transaction_id",
    "abandon_receipt_digest",
    "finalize_receipt_digest",
    "abandon_checkpoint_identity",
})
TERMINAL_ABANDON_RESULT_OWNER_TOOLS = frozenset({
    "abandon_generation",
    "prepare_next_gen",
    "run_crossover",
    "run_direction_audit",
    "run_literature_probe",
    "run_master",
    "execute_workers",
    "run_quality_gates",
    "run_review",
    "run_critic",
    "run_precommit_eval",
    "commit_bot",
})
_EVOLUTION_PROVIDER_TOOL_PREFIX = "mcp__evolution__"


# ---------------------------------------------------------------------------
# Exception types
# ---------------------------------------------------------------------------


class LLMStreamNextTimeout(asyncio.TimeoutError):
    """One SDK ``__anext__`` exceeded its deadline.

    ``pending_task`` is retained only when cancellation did not complete during
    the bounded grace period.  The attempt owner must then close its exact SDK
    transport and prove both task and child-process exit before another provider
    call may start.
    """

    def __init__(self, pending_task=None):
        self.pending_task = pending_task
        super().__init__("SDK stream __anext__ timed out")


class LLMProviderCleanupError(ConnectionError):
    """The SDK stream required exceptional transport-level cleanup."""

    def __init__(self, message, *, provider_exit_confirmed=False, attempt_id=None):
        self.provider_exit_confirmed = bool(provider_exit_confirmed)
        self.attempt_id = attempt_id
        super().__init__(str(message))


class LLMProviderCleanupBlocked(LLMProviderCleanupError):
    """A prior provider attempt has not yet proven task/process termination."""


# ---------------------------------------------------------------------------
# Attempt construction + canonical abandon helpers
# ---------------------------------------------------------------------------


def _new_provider_attempt(transport):
    return {
        "attempt_id": uuid.uuid4().hex,
        "transport": transport,
        "owned_process": None,
        "pending_tasks": set(),
        "cleanup_reasons": [],
        "cleanup_task": None,
        "transport_close_attempted": False,
        "transport_close_confirmed": False,
    }


def _normalized_provider_tool_name(name: object) -> str:
    return str(name or "").rsplit("__", 1)[-1]


def _canonical_provider_tool_args(args: object) -> str | None:
    if not isinstance(args, dict):
        return None
    try:
        return json.dumps(
            args,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Verified terminal-abandon result cache (cross-attempt abandon verification)
# ---------------------------------------------------------------------------


def register_current_provider_evolution_tool_use(
    tool_use_id: str,
    raw_name: object,
    args: object,
) -> bool:
    """Bind one observed Evolution MCP ToolUse to the active SDK attempt.

    An in-process MCP handler receives only name+arguments, not the provider's
    ToolUse id.  The SDK is allowed to invoke that handler before the outer
    stream yields its corresponding message, so a handler may have retained a
    *provisional* same-attempt proof.  It becomes consumable only here, when
    exactly one un-settled ToolUse has the exact normalized owner and canonical
    arguments.  A duplicate exact registration makes attribution ambiguous and
    invalidates the cache.  Other UserMessage content is deliberately not a
    registration capability.
    """

    attempt = _LLM_PROVIDER_ATTEMPT.get()
    name = str(raw_name or "")
    canonical_args = _canonical_provider_tool_args(args)
    identifier = str(tool_use_id or "")
    if (
        not isinstance(attempt, dict)
        or not name.startswith(_EVOLUTION_PROVIDER_TOOL_PREFIX)
        or not identifier
        or canonical_args is None
    ):
        return False
    entry = {
        "tool_use_id": identifier,
        "owner_tool": _normalized_provider_tool_name(name),
        "arguments": canonical_args,
        "settled": False,
    }
    with _PROVIDER_CLEANUP_LOCK:
        registrations = attempt.setdefault("registered_evolution_tool_uses", {})
        if not isinstance(registrations, dict) or identifier in registrations:
            return False
        registrations[identifier] = entry
        provisional = attempt.get("provisional_verified_terminal_abandon")
        bound = attempt.get("verified_terminal_abandon")
        if isinstance(provisional, dict):
            matches = [
                value
                for value in registrations.values()
                if isinstance(value, dict)
                and value.get("settled") is not True
                and value.get("owner_tool") == provisional.get("owner_tool")
                and value.get("arguments") == provisional.get("arguments")
            ]
            if len(matches) == 1:
                candidate_id = str(matches[0].get("tool_use_id") or "")
                if candidate_id:
                    record = deepcopy(provisional)
                    record["tool_use_id"] = candidate_id
                    attempt.pop("provisional_verified_terminal_abandon", None)
                    attempt["verified_terminal_abandon"] = record
                else:
                    attempt.pop("provisional_verified_terminal_abandon", None)
                    attempt["verified_terminal_abandon_conflict"] = True
            elif len(matches) > 1:
                attempt.pop("provisional_verified_terminal_abandon", None)
                attempt["verified_terminal_abandon_conflict"] = True
        elif isinstance(bound, dict):
            # The handler did not receive a ToolUse id.  A later duplicate
            # exact owner+arguments registration would make the existing bind
            # speculative, so retain neither candidate.
            if (
                bound.get("owner_tool") == entry["owner_tool"]
                and bound.get("arguments") == entry["arguments"]
                and str(bound.get("tool_use_id") or "") != identifier
            ):
                attempt.pop("verified_terminal_abandon", None)
                attempt["verified_terminal_abandon_conflict"] = True
    return True


def settle_current_provider_evolution_tool_use(tool_use_id: str) -> None:
    """Mark one stream-observed Evolution ToolUse settled after its SDK result."""

    attempt = _LLM_PROVIDER_ATTEMPT.get()
    identifier = str(tool_use_id or "")
    if not isinstance(attempt, dict) or not identifier:
        return
    with _PROVIDER_CLEANUP_LOCK:
        registrations = attempt.get("registered_evolution_tool_uses")
        entry = registrations.get(identifier) if isinstance(registrations, dict) else None
        if isinstance(entry, dict):
            entry["settled"] = True


def _single_canonical_abandon_result(value):
    """Extract exactly one terminal-abandon payload from a tool return shape.

    The SDK can carry a local MCP return as a JSON string, a text content
    block, or an already-decoded nested mapping.  This helper deliberately
    accepts only one complete payload: duplicated flattened/nested terminal
    objects remain ambiguous and are not cacheable.
    """

    matches = []

    def collect(candidate):
        if isinstance(candidate, dict):
            if _CANONICAL_ABANDON_RESULT_FIELDS.issubset(candidate):
                matches.append(candidate)
            for key in ("abandon_result", "result", "content", "text"):
                if key in candidate:
                    collect(candidate.get(key))
            return
        if isinstance(candidate, list):
            for item in candidate:
                collect(item)
            return
        if isinstance(candidate, str):
            try:
                collect(json.loads(candidate))
            except (TypeError, json.JSONDecodeError):
                pass

    collect(value)
    if len(matches) != 1:
        return None
    try:
        return deepcopy(matches[0])
    except Exception:
        return None


def cache_verified_provider_terminal_abandon(
    owner_tool: str,
    baseline_checkpoint: dict,
    raw_result,
    args: object,
):
    """Cache one already-reproved terminal result for the active SDK attempt.

    This is a narrow transport-loss bridge, not durable recovery authority.
    A guarded mutating MCP handler calls it only after returning its actual
    result.  The cache exists solely in the active provider-attempt mapping.
    If the SDK handler runs before the stream exposes its ToolUse, this
    function retains an unconsumable provisional record; only a later unique
    exact registration can attach the provider ToolUse id.  A process restart,
    a different attempt, a missing registration, or a second candidate all
    remain fail-closed in the Orchestrator.
    """

    attempt = _LLM_PROVIDER_ATTEMPT.get()
    owner = str(owner_tool or "")
    canonical_args = _canonical_provider_tool_args(args)
    if (
        not isinstance(attempt, dict)
        or not isinstance(baseline_checkpoint, dict)
        or owner not in TERMINAL_ABANDON_RESULT_OWNER_TOOLS
        or canonical_args is None
    ):
        return None
    terminal_result = _single_canonical_abandon_result(raw_result)
    if terminal_result is None:
        return None
    try:
        from tool_bot_management import validate_completed_abandon_handoff

        terminal_proof = validate_completed_abandon_handoff(
            deepcopy(baseline_checkpoint),
            terminal_result,
        )
        record = {
            "owner_tool": owner,
            "arguments": canonical_args,
            "terminal_result": terminal_result,
            "terminal_proof": deepcopy(terminal_proof),
        }
        # Canonical JSON makes later SDK/cache equality checks independent of
        # dictionary insertion order and prevents a caller from mutating our
        # retained object after this function returns.
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except Exception:
        return None
    with _PROVIDER_CLEANUP_LOCK:
        if (
            attempt.get("verified_terminal_abandon") is not None
            or attempt.get("provisional_verified_terminal_abandon") is not None
            or attempt.get("verified_terminal_abandon_conflict") is True
        ):
            # Two terminal results in one provider attempt are ambiguous even
            # when their fields happen to look similar.  Do not retain either.
            attempt.pop("verified_terminal_abandon", None)
            attempt.pop("provisional_verified_terminal_abandon", None)
            attempt["verified_terminal_abandon_conflict"] = True
            return None
        registrations = attempt.get("registered_evolution_tool_uses")
        all_matches = (
            [
                value
                for value in registrations.values()
                if isinstance(value, dict)
                and value.get("owner_tool") == owner
                and value.get("arguments") == canonical_args
            ]
            if isinstance(registrations, dict)
            else []
        )
        candidates = [
            value for value in all_matches if value.get("settled") is not True
        ]
        # Same-name or same-argument concurrent calls cannot be inferred from
        # the handler alone.  Preserve normal SDK delivery, but never cache
        # ambiguity.  Zero matches is the documented handler-before-stream
        # race: keep an unconsumable record until one exact registration binds.
        # A settled historical exact registration is also ambiguous: the
        # handler lacks a provider id, so it must not speculate that a later
        # same-name/same-argument ToolUse is its owner.
        if len(candidates) > 1 or len(all_matches) != len(candidates):
            attempt["verified_terminal_abandon_conflict"] = True
            return None
        if len(candidates) == 1:
            tool_use_id = str(candidates[0].get("tool_use_id") or "")
            if not tool_use_id:
                attempt["verified_terminal_abandon_conflict"] = True
                return None
            record["tool_use_id"] = tool_use_id
            attempt["verified_terminal_abandon"] = record
            return deepcopy(record)
        attempt["provisional_verified_terminal_abandon"] = record
    # A provisional record intentionally has no ToolUse id and is neither a
    # successful cache return nor visible through
    # ``current_provider_verified_terminal_abandon``.  It can become authority
    # only through a later unique exact registration above.
    return None


def current_provider_verified_terminal_abandon():
    """Return the active attempt's one in-memory verified terminal record.

    Callers must still bind it to a pending SDK ToolUse and revalidate it
    against their own immutable pre-call checkpoint snapshot.
    """

    attempt = _LLM_PROVIDER_ATTEMPT.get()
    if not isinstance(attempt, dict):
        return None
    with _PROVIDER_CLEANUP_LOCK:
        if attempt.get("verified_terminal_abandon_conflict") is True:
            return None
        record = attempt.get("verified_terminal_abandon")
        if not isinstance(record, dict):
            return None
        try:
            return deepcopy(record)
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Unresolved-attempt registry + exit-confirmation predicates
# ---------------------------------------------------------------------------


def _capture_owned_provider_process(attempt):
    if not isinstance(attempt, dict):
        return None
    transport = attempt.get("transport")
    process = getattr(transport, "_process", None)
    if process is not None and attempt.get("owned_process") is None:
        attempt["owned_process"] = process
    return attempt.get("owned_process")


def _register_unresolved_provider_attempt(attempt, reason, *tasks):
    if not isinstance(attempt, dict):
        return
    _capture_owned_provider_process(attempt)
    for task in tasks:
        if isinstance(task, asyncio.Task):
            attempt.setdefault("pending_tasks", set()).add(task)
            task.add_done_callback(_consume_task_result)
    reasons = attempt.setdefault("cleanup_reasons", [])
    reason = str(reason or "provider_cleanup_unresolved")
    if reason not in reasons:
        reasons.append(reason)
    with _PROVIDER_CLEANUP_LOCK:
        _UNRESOLVED_PROVIDER_ATTEMPTS[attempt["attempt_id"]] = attempt


def _provider_attempt_exit_confirmed(attempt):
    if (
        not isinstance(attempt, dict)
        or not attempt.get("transport_close_attempted")
        or not attempt.get("transport_close_confirmed")
    ):
        return False
    if any(
        isinstance(task, asyncio.Task) and not task.done()
        for task in attempt.get("pending_tasks") or ()
    ):
        return False
    cleanup_task = attempt.get("cleanup_task")
    if isinstance(cleanup_task, asyncio.Task) and not cleanup_task.done():
        try:
            current_task = asyncio.current_task()
        except RuntimeError:
            current_task = None
        if cleanup_task is not current_task:
            return False
    owned_process = _capture_owned_provider_process(attempt)
    if owned_process is not None and getattr(owned_process, "returncode", None) is None:
        return False
    transport_process = getattr(attempt.get("transport"), "_process", None)
    if (
        transport_process is not None
        and getattr(transport_process, "returncode", None) is None
    ):
        return False
    return True


def _resolve_provider_attempt_if_stopped(attempt):
    if isinstance(attempt, dict) and attempt.get("transport_close_attempted"):
        owned_process = _capture_owned_provider_process(attempt)
        transport_process = getattr(attempt.get("transport"), "_process", None)
        if (
            (owned_process is None or getattr(owned_process, "returncode", None) is not None)
            and (
                transport_process is None
                or getattr(transport_process, "returncode", None) is not None
            )
        ):
            attempt["transport_close_confirmed"] = True
    if not _provider_attempt_exit_confirmed(attempt):
        return False
    with _PROVIDER_CLEANUP_LOCK:
        _UNRESOLVED_PROVIDER_ATTEMPTS.pop(attempt.get("attempt_id"), None)
    return True


def _assert_no_unresolved_provider_attempts():
    blocked = []
    with _PROVIDER_CLEANUP_LOCK:
        attempts = list(_UNRESOLVED_PROVIDER_ATTEMPTS.values())
    for attempt in attempts:
        if _resolve_provider_attempt_if_stopped(attempt):
            continue
        blocked.append(attempt)
    if blocked:
        details = ", ".join(
            f"{item.get('attempt_id')}:{'|'.join(item.get('cleanup_reasons') or [])}"
            for item in blocked[:3]
        )
        raise LLMProviderCleanupBlocked(
            "prior SDK provider cleanup is unresolved; refusing a new provider "
            f"dispatch ({details})",
            provider_exit_confirmed=False,
            attempt_id=blocked[0].get("attempt_id"),
        )


def _track_pending_stream_task(task, reason):
    attempt = _LLM_PROVIDER_ATTEMPT.get()
    if isinstance(attempt, dict):
        _register_unresolved_provider_attempt(attempt, reason, task)
    else:
        task.add_done_callback(_consume_task_result)


def _provider_stream_cancel_grace():
    import llm_query as _lq  # local: avoids circular import at module load

    try:
        grace = max(
            0.0,
            min(
                5.0,
                float(os.environ.get("POK_LLM_NEXT_CANCEL_GRACE", "1")),
            ),
        )
    except (TypeError, ValueError):
        grace = 1.0
    total_scope = _lq._LLM_TOTAL_DEADLINE.get()
    total_deadline = (
        (total_scope or {}).get("deadline")
        if isinstance(total_scope, dict)
        else None
    )
    if total_deadline is not None:
        grace = min(grace, max(0.0, float(total_deadline) - time.time()))
    return grace


async def cancel_provider_stream_task_bounded(
    task,
    reason,
    *,
    attempt=None,
    grace=None,
):
    """Cancel one owned stream task without waiting beyond a fixed grace.

    A task which ignores cancellation is retained by its exact provider
    attempt.  The transport-level cleanup boundary then owns termination and
    future provider dispatch remains blocked until task and process exit are
    both proven.
    """

    if not isinstance(task, asyncio.Task):
        raise TypeError("provider stream cancellation requires an asyncio.Task")
    if task.done():
        _consume_task_result(task)
        return True
    task.cancel()
    if grace is None:
        grace = _provider_stream_cancel_grace()
    else:
        try:
            grace = max(0.0, min(30.0, float(grace)))
        except (TypeError, ValueError):
            grace = _provider_stream_cancel_grace()
    try:
        if grace > 0:
            await asyncio.wait({task}, timeout=grace)
    except BaseException:
        if not task.done():
            if isinstance(attempt, dict):
                _register_unresolved_provider_attempt(attempt, reason, task)
            else:
                _track_pending_stream_task(task, reason)
        raise
    if task.done():
        _consume_task_result(task)
        return True
    if isinstance(attempt, dict):
        _register_unresolved_provider_attempt(attempt, reason, task)
    else:
        _track_pending_stream_task(task, reason)
    return False


async def _await_stream_next_bounded(stream_iter, timeout):
    """Await one SDK message without unbounded ``wait_for`` cancellation.

    ``asyncio.wait_for`` waits for a cancellation-resistant awaitable to finish
    cancelling, so its wall-clock can exceed the timeout indefinitely.  Race a
    task against the timeout, give SDK cleanup a small bounded grace, then let
    the caller raise its typed role timeout.
    """

    task = asyncio.create_task(stream_iter.__anext__())
    try:
        done, _pending = await asyncio.wait({task}, timeout=timeout)
        if task in done:
            return task.result()
        cancelled = await cancel_provider_stream_task_bounded(
            task,
            "stream_next_cancellation_unconfirmed",
        )
        if not cancelled:
            raise LLMStreamNextTimeout(task)
        raise LLMStreamNextTimeout()
    except LLMStreamNextTimeout:
        raise
    except BaseException:
        if not task.done():
            await cancel_provider_stream_task_bounded(
                task,
                "stream_next_parent_cancellation_unconfirmed",
            )
        raise


async def await_provider_stream_next_bounded(stream_iter, timeout):
    """Public owned-provider boundary used by both role stream runtimes."""

    return await _await_stream_next_bounded(stream_iter, timeout)


# ---------------------------------------------------------------------------
# Owned SDK transport + attempt lifecycle
# ---------------------------------------------------------------------------


def _consume_task_result(task):
    with contextlib.suppress(BaseException):
        task.result()


async def _bounded_aclose(query_gen, role_name, log_file_path):
    """Close one SDK generator or raise a typed infrastructure failure.

    A completed close task is not automatically success: async generators raise
    ``RuntimeError`` when ``aclose()`` races an active ``__anext__``.  Suppressing
    that exception previously reported cleanup success while the CLI subprocess
    and billed provider request could continue in the background.
    """

    import llm_query as _lq  # local: avoids circular import at module load

    try:
        close_timeout = max(
            0.1,
            min(30.0, float(os.environ.get("POK_LLM_ACLOSE_TIMEOUT", "15"))),
        )
    except (TypeError, ValueError):
        close_timeout = 15.0
    close_task = asyncio.create_task(query_gen.aclose())
    done, _pending = await asyncio.wait({close_task}, timeout=close_timeout)
    if close_task in done:
        try:
            close_task.result()
        except BaseException as exc:
            raise LLMProviderCleanupError(
                "SDK stream aclose failed: "
                f"{type(exc).__name__}: {str(exc)[:300]}"
            ) from exc
        return True
    close_task.cancel()
    done, _pending = await asyncio.wait({close_task}, timeout=1.0)
    pending_task = close_task if close_task not in done else None
    if pending_task is not None:
        _track_pending_stream_task(
            pending_task,
            "stream_aclose_cancellation_unconfirmed",
        )
    else:
        _consume_task_result(close_task)
    _lq._emit_llm_event(
        "pipeline.llm_role_stream_close_timeout",
        "error",
        f"{role_name}: SDK stream cleanup exceeded {close_timeout:.1f}s",
        role=role_name,
        timeout_sec=round(close_timeout, 2),
        **_lq._role_log_metadata(log_file_path),
    )
    raise LLMProviderCleanupError(
        f"SDK stream aclose exceeded {close_timeout:.1f}s",
        provider_exit_confirmed=False,
    )


def _new_owned_sdk_transport(full_prompt, options):
    """Create the exact SDK subprocess transport owned by one query attempt."""

    try:
        from claude_agent_sdk._internal.transport.subprocess_cli import (
            SubprocessCLITransport,
        )
    except Exception as exc:
        raise LLMProviderCleanupError(
            "SDK owned subprocess transport is unavailable: "
            f"{type(exc).__name__}: {str(exc)[:300]}"
        ) from exc
    return SubprocessCLITransport(prompt=full_prompt, options=options)


def create_owned_provider_attempt(full_prompt, options):
    """Create one dispatch-ready provider attempt with exact transport ownership."""

    import llm_query as _lq  # local: avoids circular import at module load

    _assert_no_unresolved_provider_attempts()
    return _new_provider_attempt(_lq._new_owned_sdk_transport(full_prompt, options))


def owned_provider_attempt_transport(attempt):
    """Return only the transport bound to ``attempt``."""

    if not isinstance(attempt, dict) or attempt.get("transport") is None:
        raise LLMProviderCleanupError("owned provider attempt has no transport")
    return attempt["transport"]


@contextlib.contextmanager
def owned_provider_attempt_scope(attempt):
    """Bind pending SDK tasks to one provider attempt for this async context."""

    token = activate_owned_provider_attempt(attempt)
    try:
        yield attempt
    finally:
        reset_owned_provider_attempt(token)


def activate_owned_provider_attempt(attempt):
    """Activate one attempt and return the exact ContextVar reset token."""

    if not isinstance(attempt, dict):
        raise TypeError("owned provider attempt must be a mapping")
    return _LLM_PROVIDER_ATTEMPT.set(attempt)


def reset_owned_provider_attempt(token):
    """Reset a token produced by :func:`activate_owned_provider_attempt`."""

    _LLM_PROVIDER_ATTEMPT.reset(token)


def mark_owned_provider_attempt_unresolved(attempt, reason, task=None):
    """Mark an anomalous attempt and optionally retain its unfinished task."""

    tasks = (task,) if isinstance(task, asyncio.Task) else ()
    _register_unresolved_provider_attempt(attempt, reason, *tasks)


def owned_provider_attempt_exit_confirmed(attempt):
    """Return true only after this attempt's exact process and tasks exited."""

    return _resolve_provider_attempt_if_stopped(attempt)


def _refresh_transport_exit_confirmation(attempt):
    if not isinstance(attempt, dict) or not attempt.get("transport_close_attempted"):
        return False
    owned_process = _capture_owned_provider_process(attempt)
    transport_process = getattr(attempt.get("transport"), "_process", None)
    owned_stopped = (
        owned_process is None
        or getattr(owned_process, "returncode", None) is not None
    )
    transport_stopped = (
        transport_process is None
        or getattr(transport_process, "returncode", None) is not None
    )
    if owned_stopped and transport_stopped:
        attempt["transport_close_confirmed"] = True
    return bool(attempt.get("transport_close_confirmed"))


async def _bounded_owned_transport_close(attempt, role_name, log_file_path):
    """Close only this attempt's SDK-owned transport and prove process exit."""

    transport = attempt.get("transport") if isinstance(attempt, dict) else None
    if transport is None or not callable(getattr(transport, "close", None)):
        raise LLMProviderCleanupError(
            "SDK provider transport has no owned close API",
            attempt_id=(attempt or {}).get("attempt_id"),
        )
    _capture_owned_provider_process(attempt)
    attempt["transport_close_attempted"] = True
    try:
        close_timeout = max(
            0.1,
            min(
                30.0,
                float(os.environ.get("POK_LLM_TRANSPORT_CLOSE_TIMEOUT", "15")),
            ),
        )
    except (TypeError, ValueError):
        close_timeout = 15.0
    close_task = asyncio.create_task(transport.close())
    done, _pending = await asyncio.wait({close_task}, timeout=close_timeout)
    if close_task not in done:
        close_task.cancel()
        done, _pending = await asyncio.wait({close_task}, timeout=1.0)
    if close_task not in done:
        _register_unresolved_provider_attempt(
            attempt,
            "owned_transport_close_cancellation_unconfirmed",
            close_task,
        )
        raise LLMProviderCleanupError(
            f"owned SDK transport close exceeded {close_timeout:.1f}s",
            provider_exit_confirmed=False,
            attempt_id=attempt.get("attempt_id"),
        )
    try:
        close_task.result()
    except BaseException as exc:
        _register_unresolved_provider_attempt(
            attempt,
            f"owned_transport_close_failed:{type(exc).__name__}",
        )
        _refresh_transport_exit_confirmation(attempt)
        raise LLMProviderCleanupError(
            "owned SDK transport close failed: "
            f"{type(exc).__name__}: {str(exc)[:300]}",
            provider_exit_confirmed=_provider_attempt_exit_confirmed(attempt),
            attempt_id=attempt.get("attempt_id"),
        ) from exc
    _refresh_transport_exit_confirmation(attempt)
    if not attempt.get("transport_close_confirmed"):
        _register_unresolved_provider_attempt(
            attempt,
            "owned_transport_process_exit_unconfirmed",
        )
        raise LLMProviderCleanupError(
            "owned SDK transport returned from close without process-exit proof",
            provider_exit_confirmed=False,
            attempt_id=attempt.get("attempt_id"),
        )
    return True


async def _await_provider_attempt_tasks(attempt):
    tasks = {
        task
        for task in (attempt.get("pending_tasks") or ())
        if isinstance(task, asyncio.Task) and not task.done()
    }
    if not tasks:
        return True
    try:
        timeout = max(
            0.1,
            min(
                10.0,
                float(os.environ.get("POK_LLM_STREAM_TASK_EXIT_TIMEOUT", "5")),
            ),
        )
    except (TypeError, ValueError):
        timeout = 5.0
    done, pending = await asyncio.wait(tasks, timeout=timeout)
    for task in done:
        _consume_task_result(task)
    if pending:
        _register_unresolved_provider_attempt(
            attempt,
            "stream_tasks_remain_after_transport_close",
            *pending,
        )
        return False
    return True


async def _perform_owned_provider_attempt_cleanup(
    query_gen,
    attempt,
    role_name,
    log_file_path,
):
    """Close a query and fail explicitly if exceptional cleanup was required."""

    import llm_query as _lq  # local: avoids circular import at module load

    # Preserve the exact process object before ``aclose`` can clear the
    # transport's pointer.  Exceptional cleanup must prove that this same
    # child exited; a later ``None`` transport pointer is not, by itself,
    # process-exit evidence.
    _capture_owned_provider_process(attempt)
    exceptional = bool(attempt.get("cleanup_reasons"))
    cleanup_errors = []
    if exceptional:
        try:
            await _bounded_owned_transport_close(
                attempt,
                role_name,
                log_file_path,
            )
        except LLMProviderCleanupError as exc:
            cleanup_errors.append(str(exc))
        if not await _await_provider_attempt_tasks(attempt):
            cleanup_errors.append("SDK stream task exit remains unconfirmed")
        if not any(
            isinstance(task, asyncio.Task) and not task.done()
            for task in attempt.get("pending_tasks") or ()
        ):
            try:
                await _bounded_aclose(query_gen, role_name, log_file_path)
            except LLMProviderCleanupError as exc:
                cleanup_errors.append(str(exc))
    else:
        try:
            await _bounded_aclose(query_gen, role_name, log_file_path)
        except LLMProviderCleanupError as exc:
            exceptional = True
            cleanup_errors.append(str(exc))
            _register_unresolved_provider_attempt(
                attempt,
                "stream_aclose_failed",
            )
            try:
                await _bounded_owned_transport_close(
                    attempt,
                    role_name,
                    log_file_path,
                )
            except LLMProviderCleanupError as transport_exc:
                cleanup_errors.append(str(transport_exc))
            await _await_provider_attempt_tasks(attempt)
        else:
            # Another owner (for example the cycle-level timeout boundary) may
            # mark the attempt while this normal ``aclose`` is in flight.  A
            # successful generator close then counts as the transport close,
            # but only the captured original process can prove termination.
            if attempt.get("cleanup_reasons"):
                exceptional = True
                attempt["transport_close_attempted"] = True
                _refresh_transport_exit_confirmation(attempt)

    if not exceptional:
        return True
    _refresh_transport_exit_confirmation(attempt)
    confirmed = _resolve_provider_attempt_if_stopped(attempt)
    cleanup_reasons = set(attempt.get("cleanup_reasons") or ())
    pending_tasks = [
        task
        for task in attempt.get("pending_tasks") or ()
        if isinstance(task, asyncio.Task) and not task.done()
    ]
    # A parent cancellation is not a provider failure once cleanup has proven
    # that the exact child and every owned SDK task exited.  Preserve the
    # original CancelledError so the existing shutdown/control path can classify
    # it as a clean stop.  Every timeout reason, mixed reason, cleanup error, or
    # unconfirmed exit remains fail-closed below.
    if (
        cleanup_reasons
        == {"stream_next_parent_cancellation_unconfirmed"}
        and confirmed
        and not pending_tasks
        and not cleanup_errors
    ):
        _lq._emit_llm_event(
            "pipeline.llm_role_provider_cleanup_completed_after_parent_cancel",
            "info",
            f"{role_name}: provider cleanup completed after parent cancellation",
            role=role_name,
            attempt_id=attempt.get("attempt_id"),
            provider_exit_confirmed=True,
            cleanup_reasons=sorted(cleanup_reasons),
            **_lq._role_log_metadata(log_file_path),
        )
        return True
    message = (
        "SDK provider stream required exceptional cleanup; "
        f"process_exit_confirmed={confirmed}; reasons="
        + "|".join(attempt.get("cleanup_reasons") or [])
    )
    if cleanup_errors:
        message += "; errors=" + "; ".join(cleanup_errors[:4])
    _lq._emit_llm_event(
        "pipeline.llm_role_provider_cleanup_failure",
        "error",
        f"{role_name}: {message}",
        role=role_name,
        attempt_id=attempt.get("attempt_id"),
        provider_exit_confirmed=confirmed,
        cleanup_reasons=list(attempt.get("cleanup_reasons") or []),
        cleanup_errors=cleanup_errors[:4],
        **_lq._role_log_metadata(log_file_path),
    )
    raise LLMProviderCleanupError(
        message,
        provider_exit_confirmed=confirmed,
        attempt_id=attempt.get("attempt_id"),
    )


async def _cleanup_owned_provider_attempt(
    query_gen,
    attempt,
    role_name,
    log_file_path,
):
    """Run the exact attempt cleanup once, even with multiple timeout owners."""

    if not isinstance(attempt, dict):
        raise LLMProviderCleanupError("invalid owned provider cleanup attempt")
    cleanup_task = attempt.get("cleanup_task")
    if not isinstance(cleanup_task, asyncio.Task):
        cleanup_task = asyncio.create_task(
            _perform_owned_provider_attempt_cleanup(
                query_gen,
                attempt,
                role_name,
                log_file_path,
            )
        )
        attempt["cleanup_task"] = cleanup_task
        cleanup_task.add_done_callback(_consume_task_result)
    return await asyncio.shield(cleanup_task)


async def cleanup_owned_provider_attempt(
    query_gen,
    attempt,
    role_name,
    log_file_path,
):
    """Public idempotent cleanup boundary for an owned provider attempt."""

    return await _cleanup_owned_provider_attempt(
        query_gen,
        attempt,
        role_name,
        log_file_path,
    )
