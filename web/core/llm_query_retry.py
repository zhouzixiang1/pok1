"""LLM streaming, bounded signature-retry execution path, and JSON output parsing.

Extracted from llm_query.py as a single business responsibility: the per-attempt
stream processor (``_process_stream``) that consumes SDK messages, records
role-IO/observability, and enforces role timeout policy; the bounded
signature-retry loop (``_run_stream_with_signature_retry`` /
``_run_stream_with_signature_retry_attempts``) that owns the call-wide total
deadline, per-attempt billing, and the empty-output / signature-error backoff;
and the JSON output parsers (``parse_json_output`` /
``parse_json_output_with_mode``) that extract structured data from LLM
responses.  The billing helpers (``_merge_billing_usage``,
``_record_completed_billing_attempt``) and the deadline-aware sleep
(``_signature_retry_sleep`` / ``_raise_signature_retry_total_timeout``) live
here because they are private to the retry loop.

All public symbols are re-exported by llm_query.py (as thin delegate shells)
for backward compatibility, so ``from llm_query import <name>`` imports and
test monkeypatches on ``llm_query.<name>`` keep resolving.

Monkeypatch contract: tests routinely patch ``llm_query.claude_query``,
``llm_query._process_stream``, ``llm_query._emit_llm_event``,
``llm_query._role_timeout_policy``, ``llm_query._LLM_PROGRESS_INTERVAL_SEC``,
``llm_query._LLM_SILENCE_WARN_SEC``, ``llm_query.asyncio.sleep``, etc. and then
invoke ``llm_query._run_stream_with_signature_retry`` /
``llm_query._process_stream``.  Because a function reads its globals from the
module where it is defined, every reference below to a parent-owned symbol is
routed through ``_lq.<name>`` (a lazy ``import llm_query as _lq``) so the test
patches on the ``llm_query`` namespace take effect at call time.  Intra-cluster
helpers that tests do NOT patch (e.g. ``_merge_billing_usage``) are called
directly.  ``asyncio`` is a shared singleton module object, so
``monkeypatch.setattr(llm_query.asyncio, "sleep", fake)`` mutates the same
object visible here — direct ``asyncio.sleep`` / ``asyncio.create_task`` /
``asyncio.CancelledError`` references remain correct.
"""

import asyncio
import contextlib
import hashlib
import json
import re
import time
import uuid

from claude_agent_sdk import (
    AssistantMessage,
    UserMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
    ThinkingBlock,
    ClaudeSDKError,
)
from llm_availability import LLMAvailabilityBlocked, LLMAvailabilityTrace

# NOTE: ``llm_query`` imports this module at its own top level.  To avoid a
# circular import we do NOT ``import llm_query`` at module scope.  Each
# function that needs to read a monkeypatchable parent symbol (claude_query,
# _process_stream, _emit_llm_event, _role_timeout_policy, the
# _LLM_*_SEC / _LLM_*_DEADLINE / _LLM_*_ATTEMPT / _LLM_BILLING_RESULTS
# module globals, the typed exceptions, etc.) imports it lazily as ``_lq``
# and references the symbol as ``_lq.<name>``.


# ---------------------------------------------------------------------------
# Per-attempt stream processor
# ---------------------------------------------------------------------------

async def _process_stream(query_gen, log_file_path, ui, role_name):
    """Process a streaming LLM query, returning (texts, cost_usd, usage).

    Handles TextBlock, ThinkingBlock, ToolUseBlock, UserMessage ToolResultBlock,
    and ResultMessage.
    Writes to log file and emits UI events as they arrive.
    """
    import llm_query as _lq

    texts = []
    cost_usd = None
    usage = None
    availability_trace = LLMAvailabilityTrace()
    stream_started_at = time.time()
    # Metrics: track first-token and first-text latencies for call analytics.
    first_productive_at = None  # first AssistantMessage/ToolUse/etc
    first_text_at = None        # first TextBlock with non-empty text
    # Metrics: capture ResultMessage diagnostic fields for llm_call_metrics.jsonl.
    result_diag = {}            # subtype, is_error, num_turns, stop_reason, etc.
    assistant_message_count = 0
    first_activity_logged = False
    # B2 (2026-07-09): a SystemMessage (e.g. subtype=init, thinking_tokens) is
    # emitted by the SDK/proxy purely to acknowledge the request or carry
    # billing telemetry — it is not model output. Letting it satisfy the
    # first-activity gate flips the wait budget from first_activity_timeout to
    # idle_timeout (e.g. 240s → 420s for CROSSOVER). When a backend (here the
    # GLM proxy behind cc-switch) stalls right after init, that extra slack
    # turns a hard stall into a ~420s dead wait per attempt. Track substantive
    # activity (AssistantMessage/ToolUse/UserMessage/ResultMessage) separately
    # and keep enforcing first_activity_timeout until real output arrives.
    substantive_activity_logged = False
    message_count = 0
    last_progress_at = stream_started_at
    last_message_at = stream_started_at
    last_silence_event_at = stream_started_at
    stream_done = False
    text_chars = 0
    thinking_chars = 0
    tool_use_count = 0
    tool_result_count = 0
    system_message_count = 0
    thinking_tokens_estimate = 0
    thinking_tokens_delta_total = 0
    unknown_message_count = 0
    timeout_policy = _lq._role_timeout_policy(role_name)
    total_timeout = float(timeout_policy.get("total_timeout") or 0)
    first_activity_timeout = float(timeout_policy.get("first_activity_timeout") or 0)
    idle_timeout = float(timeout_policy.get("idle_timeout") or 0)
    # B3: shorter stall ceiling once substantive output has started (tool/think
    # loop). 0 means "do not enforce a separate stall ceiling; use idle_timeout".
    stall_timeout = float(timeout_policy.get("stall_timeout") or 0)
    total_scope = _lq._LLM_TOTAL_DEADLINE.get()
    scoped_deadline = (
        float(total_scope.get("deadline"))
        if isinstance(total_scope, dict) and total_scope.get("deadline") is not None
        else None
    )
    scoped_started_at = (
        float(total_scope.get("started_at"))
        if isinstance(total_scope, dict) and total_scope.get("started_at") is not None
        else stream_started_at
    )
    attempt_total_deadline = (
        stream_started_at + total_timeout if total_timeout > 0 else None
    )
    total_deadline = attempt_total_deadline
    if scoped_deadline is not None and (
        total_deadline is None or scoped_deadline < total_deadline
    ):
        total_deadline = scoped_deadline

    def _tool_result_text(content):
        if isinstance(content, str):
            return content
        if content is None:
            return ""
        try:
            return json.dumps(content, ensure_ascii=False, default=str)
        except Exception:
            return str(content)

    def _record_tool_result(
        content,
        is_error=None,
        source="ToolResultBlock",
        tool_use_id=None,
    ):
        nonlocal tool_result_count
        tool_result_count += 1
        result_text = _tool_result_text(content)
        if not result_text:
            result_text = "[empty tool result]"
        result_preview = result_text[:3000]
        header = f"[TOOL_RESULT source={source} is_error={bool(is_error)}]"
        _lq._append_role_io(log_file_path, f"\n{header} {result_preview}\n")
        ui.log_io(result_preview, "tool_result", role_name)
        _lq._record_llm_tool_trace_event({
            "event": "tool_result",
            "tool_use_id": str(tool_use_id or ""),
            "is_error": bool(is_error),
            "source": str(source),
            "content_chars": len(result_text),
            "content_sha256": hashlib.sha256(result_text.encode("utf-8")).hexdigest(),
            "content_preview": result_text[:3000],
        })

    def _mark_first_activity(kind, substantive=True):
        nonlocal first_activity_logged, substantive_activity_logged
        # substantive output (assistant/tool/user/result) upgrades the gate so
        # the wait loop may switch to the idle_timeout budget. System-only
        # messages record the first-activity milestone for observability but do
        # NOT lift the (shorter) first_activity_timeout ceiling — see B2.
        if substantive:
            substantive_activity_logged = True
        if first_activity_logged:
            return
        first_activity_logged = True
        elapsed = time.time() - stream_started_at
        delayed = elapsed >= _lq._LLM_FIRST_ACTIVITY_WARN_SEC
        category = (
            "pipeline.llm_role_first_activity_delayed"
            if delayed else
            "pipeline.llm_role_first_activity"
        )
        severity = "warn" if delayed else "info"
        _lq._emit_llm_event(
            category, severity,
            f"{role_name}: first LLM stream activity after {elapsed:.1f}s",
            role=role_name,
            elapsed_sec=round(elapsed, 2),
            first_activity_warn_sec=_lq._LLM_FIRST_ACTIVITY_WARN_SEC,
            activity_kind=kind,
            substantive=substantive,
            **_lq._role_log_metadata(log_file_path),
        )

    def _emit_progress():
        nonlocal last_progress_at
        if _lq._LLM_PROGRESS_INTERVAL_SEC <= 0:
            return
        now = time.time()
        if now - last_progress_at < _lq._LLM_PROGRESS_INTERVAL_SEC:
            return
        elapsed = now - stream_started_at
        last_progress_at = now
        _lq._emit_llm_event(
            "pipeline.llm_role_progress", "info",
            f"{role_name}: LLM stream active for {elapsed:.1f}s",
            role=role_name,
            elapsed_sec=round(elapsed, 2),
            messages_seen=message_count,
            system_messages_seen=system_message_count,
            unknown_messages_seen=unknown_message_count,
            text_chars=text_chars,
            thinking_chars=thinking_chars,
            thinking_tokens_estimate=thinking_tokens_estimate,
            thinking_tokens_delta_total=thinking_tokens_delta_total,
            tool_use_count=tool_use_count,
            tool_result_count=tool_result_count,
            progress_interval_sec=_lq._LLM_PROGRESS_INTERVAL_SEC,
            **_lq._role_log_metadata(log_file_path),
        )

    async def _silence_watchdog():
        nonlocal last_silence_event_at
        if _lq._LLM_SILENCE_WARN_SEC <= 0:
            return
        sleep_for = max(0.01, min(_lq._LLM_SILENCE_WARN_SEC / 2.0, 30.0))
        while not stream_done:
            await asyncio.sleep(sleep_for)
            if stream_done:
                return
            now = time.time()
            silent_for = now - last_message_at
            since_last_event = now - last_silence_event_at
            if silent_for < _lq._LLM_SILENCE_WARN_SEC:
                continue
            if since_last_event < _lq._LLM_SILENCE_WARN_SEC:
                continue
            last_silence_event_at = now
            _lq._emit_llm_event(
                "pipeline.llm_role_stream_silent", "warn",
                f"{role_name}: no productive LLM stream messages for {silent_for:.1f}s",
                role=role_name,
                elapsed_sec=round(now - stream_started_at, 2),
                silent_for_sec=round(silent_for, 2),
                silence_warn_sec=_lq._LLM_SILENCE_WARN_SEC,
                messages_seen=message_count,
                system_messages_seen=system_message_count,
                unknown_messages_seen=unknown_message_count,
                text_chars=text_chars,
                thinking_chars=thinking_chars,
                thinking_tokens_estimate=thinking_tokens_estimate,
                thinking_tokens_delta_total=thinking_tokens_delta_total,
                tool_use_count=tool_use_count,
                tool_result_count=tool_result_count,
                **_lq._role_log_metadata(log_file_path),
            )

    def _should_log_sparse_count(count):
        return count == 1 or count in {5, 10, 20, 50} or count % 100 == 0

    def _timeout_limit(effective_kind, wait_timeout):
        if effective_kind == "total":
            return total_timeout
        if effective_kind == "first_activity":
            return first_activity_timeout
        if effective_kind == "idle":
            return idle_timeout
        if effective_kind == "stall":
            return stall_timeout
        return wait_timeout or 0

    def _raise_role_timeout(
        timeout_kind,
        wait_timeout,
        *,
        pending_stream_task=None,
    ):
        attempt_elapsed = time.time() - stream_started_at
        effective_kind = timeout_kind or "stream"
        elapsed = (
            time.time() - scoped_started_at
            if effective_kind == "total"
            else attempt_elapsed
        )
        effective_limit = _timeout_limit(effective_kind, wait_timeout)
        _lq._emit_llm_event(
            f"pipeline.llm_role_{effective_kind}_timeout",
            "error",
            f"{role_name}: LLM {effective_kind} timeout after {effective_limit:.1f}s",
            role=role_name,
            elapsed_sec=round(elapsed, 2),
            attempt_elapsed_sec=round(attempt_elapsed, 2),
            timeout_sec=round(effective_limit, 2),
            messages_seen=message_count,
            system_messages_seen=system_message_count,
            unknown_messages_seen=unknown_message_count,
            text_chars=text_chars,
            thinking_chars=thinking_chars,
            thinking_tokens_estimate=thinking_tokens_estimate,
            thinking_tokens_delta_total=thinking_tokens_delta_total,
            tool_use_count=tool_use_count,
            tool_result_count=tool_result_count,
            **timeout_policy,
            **_lq._role_log_metadata(log_file_path),
        )
        raise _lq.LLMRoleTimeout(
            role_name,
            effective_kind,
            effective_limit,
            pending_stream_task=pending_stream_task,
        )

    try:
        watchdog_task = asyncio.create_task(_silence_watchdog())
        stream_iter = query_gen.__aiter__()
        while True:
            wait_timeout = None
            timeout_kind = None
            now = time.time()
            # B2: keep the (shorter) first_activity_timeout budget until we see
            # substantive model output, not just SDK/proxy bookkeeping
            # (SystemMessage init/thinking_tokens). This prevents a stalled
            # backend from degrading into the longer idle_timeout dead-wait.
            if not substantive_activity_logged and first_activity_timeout > 0:
                wait_timeout = max(
                    0.0,
                    first_activity_timeout - (now - stream_started_at),
                )
                timeout_kind = "first_activity"
            elif substantive_activity_logged:
                # B3: once we are inside the tool/think loop, a mid-loop stall
                # (tool_use emitted but tool_result never returns, or the model
                # stops streaming mid-think) should be caught at the shorter
                # stall_timeout rather than burning the full idle_timeout
                # before the role retry can restart. stall_timeout<=0 disables
                # this layer and falls back to idle_timeout.
                idle_budget = (idle_timeout - (now - last_message_at)) if idle_timeout > 0 else None
                stall_budget = (stall_timeout - (now - last_message_at)) if stall_timeout > 0 else None
                if stall_budget is not None and (idle_budget is None or stall_budget <= idle_budget):
                    wait_timeout = max(0.0, stall_budget)
                    timeout_kind = "stall"
                elif idle_budget is not None:
                    wait_timeout = max(0.0, idle_budget)
                    timeout_kind = "idle"
            if total_deadline is not None:
                remaining_total = max(0.0, total_deadline - now)
                if wait_timeout is None or remaining_total < wait_timeout:
                    wait_timeout = remaining_total
                    timeout_kind = "total"
            if wait_timeout is not None and wait_timeout <= 0:
                _raise_role_timeout(timeout_kind, wait_timeout)
            try:
                if wait_timeout is None:
                    message = await stream_iter.__anext__()
                else:
                    message = await _lq._await_stream_next_bounded(
                        stream_iter,
                        max(0.001, wait_timeout),
                    )
            except StopAsyncIteration:
                break
            except _lq.LLMStreamNextTimeout as exc:
                _raise_role_timeout(
                    timeout_kind,
                    wait_timeout,
                    pending_stream_task=exc.pending_task,
                )
            message_count += 1
            productive_message = False
            if isinstance(message, AssistantMessage):
                productive_message = True
                if first_productive_at is None:
                    first_productive_at = time.time()
                assistant_message_count += 1
                _mark_first_activity("assistant")
                for block in message.content:
                    if isinstance(block, TextBlock):
                        text = block.text
                        if text and first_text_at is None:
                            first_text_at = time.time()
                        availability_trace.observe_text(text)
                        text_chars += len(text or "")
                        texts.append(text)
                        _lq._append_role_io(log_file_path, text + "\n")
                        ui.log_io(text, "claude", role_name)
                    elif isinstance(block, ThinkingBlock):
                        thinking = block.thinking or "[thinking...]"
                        thinking_chars += len(thinking or "")
                        _lq._append_role_io(log_file_path, f"\n[THINKING] {thinking[:2000]}\n")
                        ui.log_io(thinking, "thinking", role_name)
                    elif isinstance(block, ToolUseBlock):
                        tool_use_count += 1
                        args_str = json.dumps(block.input, ensure_ascii=False, indent=2)[:2000]
                        _lq._append_role_io(log_file_path, f"\n[TOOL_CALL] {block.name}\n[ARGS] {args_str}\n")
                        ui.log_io(f"\n[tool: {block.name}]", "tool", role_name)
                        ui.emit_tool_call(block.name, block.input, role_name)
                        _lq._record_llm_tool_trace_event({
                            "event": "tool_use",
                            "tool_use_id": str(getattr(block, "id", "") or ""),
                            "tool_name": str(block.name),
                            "tool_input": dict(block.input or {}),
                        })
                    elif isinstance(block, ToolResultBlock):
                        _record_tool_result(
                            block.content,
                            getattr(block, "is_error", None),
                            tool_use_id=getattr(block, "tool_use_id", None),
                        )
                _emit_progress()
            elif isinstance(message, UserMessage):
                productive_message = True
                _mark_first_activity("user")
                saw_tool_result_block = False
                if isinstance(message.content, list):
                    for block in message.content:
                        if isinstance(block, ToolResultBlock):
                            saw_tool_result_block = True
                            _record_tool_result(
                                block.content,
                                getattr(block, "is_error", None),
                                tool_use_id=getattr(block, "tool_use_id", None),
                            )
                tool_use_result = getattr(message, "tool_use_result", None)
                if tool_use_result is not None and not saw_tool_result_block:
                    _record_tool_result(
                        tool_use_result,
                        None,
                        source="UserMessage.tool_use_result",
                        tool_use_id=(
                            tool_use_result.get("tool_use_id")
                            if isinstance(tool_use_result, dict)
                            else None
                        ),
                    )
                _emit_progress()
            elif isinstance(message, SystemMessage):
                # SDK/proxy init and thinking-token telemetry can arrive tens
                # of times per second while the actual model/tool loop is
                # wedged.  It is observable bookkeeping, not progress: never
                # refresh ``last_message_at`` and never emit the progress event
                # consumed by the parent orchestrator's liveness extension.
                productive_message = False
                system_message_count += 1
                subtype = getattr(message, "subtype", None) or "unknown"
                data = getattr(message, "data", None)
                if not isinstance(data, dict):
                    data = {}
                if subtype == "thinking_tokens":
                    try:
                        estimate = int(data.get("estimated_tokens") or 0)
                    except (TypeError, ValueError):
                        estimate = 0
                    try:
                        delta = int(data.get("estimated_tokens_delta") or 0)
                    except (TypeError, ValueError):
                        delta = 0
                    thinking_tokens_estimate = max(
                        thinking_tokens_estimate,
                        estimate,
                    )
                    thinking_tokens_delta_total += max(0, delta)
                # B2: SystemMessages (init / thinking_tokens) are SDK/proxy
                # bookkeeping, not model output — do NOT let them satisfy the
                # substantive first-activity gate, otherwise a backend that
                # stalls right after init slips into the longer idle_timeout.
                _mark_first_activity(f"system:{subtype}", substantive=False)
                if _should_log_sparse_count(system_message_count):
                    _lq._append_role_io(
                        log_file_path,
                        f"\n[SYSTEM_MESSAGE subtype={subtype} "
                        f"count={system_message_count} "
                        f"thinking_tokens={thinking_tokens_estimate} "
                        f"thinking_delta_total={thinking_tokens_delta_total}]\n",
                    )
            elif isinstance(message, ResultMessage):
                productive_message = True
                _mark_first_activity("result")
                availability_trace.observe_result(message)
                cost_usd = message.total_cost_usd
                usage = message.usage
                # Capture ALL ResultMessage diagnostic fields for metrics logging.
                # Previously only extracted on error path; now always captured
                # so stop_reason/num_turns/duration are available for success too.
                result_diag = {
                    "subtype": getattr(message, "subtype", None),
                    "is_error": bool(getattr(message, "is_error", False)),
                    "num_turns": getattr(message, "num_turns", None),
                    "stop_reason": getattr(message, "stop_reason", None),
                    "terminal_reason": getattr(message, "terminal_reason", None),
                    "duration_ms": getattr(message, "duration_ms", None),
                    "duration_api_ms": getattr(message, "duration_api_ms", None),
                    "session_id": getattr(message, "session_id", None),
                    "uuid": getattr(message, "uuid", None),
                    "result_text": getattr(message, "result", None),
                    "api_error_status": getattr(message, "api_error_status", None),
                    "errors": getattr(message, "errors", None),
                    "model_usage": None,
                }
                # model_usage may be a dict of ModelUsage dataclasses; convert.
                _mu = getattr(message, "model_usage", None)
                if isinstance(_mu, dict):
                    try:
                        result_diag["model_usage"] = {
                            k: (v if isinstance(v, dict) else
                                (v.model_dump() if hasattr(v, "model_dump") else
                                 dict(v) if hasattr(v, "__iter__") and not isinstance(v, str) else str(v)))
                            for k, v in _mu.items()
                        }
                    except Exception:
                        result_diag["model_usage"] = None
                billing_results = _lq._LLM_BILLING_RESULTS.get()
                if isinstance(billing_results, list):
                    billing_results.append(message)
                strict_provider_capture = _lq._STRICT_PROVIDER_RESULTS.get()
                if isinstance(strict_provider_capture, dict):
                    # The strict authority workflow consumes the SDK object in
                    # the parent process.  Logs/cost projections are not an
                    # execution authority and cannot synthesize this entry.
                    from strict_authority_workflow import _observe_provider_result

                    _observe_provider_result(
                        message,
                        invocation_id=str(
                            strict_provider_capture.get("invocation_id") or ""
                        ),
                        effect_id=str(
                            strict_provider_capture.get("effect_id") or ""
                        ),
                    )
                    strict_provider_capture.setdefault("results", []).append(message)
                _emit_progress()
                # A1 (v125 retry-storm fix): capture ResultMessage diagnostic fields.
                # Previously this branch read ONLY cost/usage, discarding subtype /
                # is_error / num_turns / stop_reason. That made every Master-failure
                # mode (missing-return / NO_FENCE / empty-output) collapse to the SAME
                # undifferentiated "malformed JSON" symptom downstream, which caused
                # multiple rounds of mis-attribution (v125 wasted several analysis
                # cycles before the real root cause was found). Log the diagnostics so
                # future failures are classifiable. Return signature is UNCHANGED (3-tuple)
                # — this is pure observation and must not alter retry/circuit behavior.
                try:
                    _subtype = getattr(message, "subtype", None)
                    _is_err = bool(getattr(message, "is_error", False))
                    if _is_err or (_subtype and _subtype != "success"):
                        _num_turns = getattr(message, "num_turns", None)
                        _stop_reason = getattr(message, "stop_reason", None)
                        _diag = {
                            "role": role_name,
                            "subtype": _subtype,
                            "is_error": _is_err,
                            "num_turns": _num_turns,
                            "stop_reason": _stop_reason,
                        }
                        _lq._append_role_io(
                            log_file_path,
                            "\n[RESULT_DIAG] "
                            + json.dumps(_diag, ensure_ascii=False, default=str)
                            + "\n",
                        )
                        if ui:
                            ui.log_history(
                                f"{role_name}: ResultMessage non-success "
                                f"(subtype={_subtype}, is_error={_is_err}, "
                                f"num_turns={_num_turns}, stop_reason={_stop_reason})",
                                "warn",
                            )
                            try:
                                import event_bus
                                event_bus.warn(
                                    "pipeline.llm_result_non_success",
                                    f"{role_name} ResultMessage non-success (subtype={_subtype})",
                                    role=role_name,
                                    subtype=_subtype,
                                    is_error=_is_err,
                                    num_turns=_num_turns,
                                    stop_reason=_stop_reason,
                                )
                            except Exception:
                                pass
                except Exception:
                    pass
                availability_block = availability_trace.blocked(role=role_name)
                if availability_block is not None:
                    raise availability_block
            else:
                unknown_message_count += 1
                message_type = type(message).__name__
                message_module = type(message).__module__
                if _should_log_sparse_count(unknown_message_count):
                    _lq._append_role_io(
                        log_file_path,
                        f"\n[UNKNOWN_SDK_MESSAGE] {message_module}.{message_type}: "
                        f"{repr(message)[:1000]}\n",
                    )
                    _lq._emit_llm_event(
                        "pipeline.llm_role_unknown_message",
                        "warn",
                        f"{role_name}: unknown SDK stream message {message_type}",
                        role=role_name,
                        elapsed_sec=round(time.time() - stream_started_at, 2),
                        message_type=message_type,
                        message_module=message_module,
                        messages_seen=message_count,
                        system_messages_seen=system_message_count,
                        unknown_messages_seen=unknown_message_count,
                        text_chars=text_chars,
                        thinking_chars=thinking_chars,
                        thinking_tokens_estimate=thinking_tokens_estimate,
                        thinking_tokens_delta_total=thinking_tokens_delta_total,
                        tool_use_count=tool_use_count,
                        tool_result_count=tool_result_count,
                        **_lq._role_log_metadata(log_file_path),
                    )
            if productive_message:
                last_message_at = time.time()
    except LLMAvailabilityBlocked as e:
        issue = e.issue
        _lq._emit_llm_event(
            "pipeline.llm_role_availability_blocked", "error",
            f"{role_name}: LLM availability blocked ({issue.category})",
            role=role_name,
            elapsed_sec=round(time.time() - stream_started_at, 2),
            messages_seen=message_count,
            availability_category=issue.category,
            availability_issue=issue.as_dict(),
            **_lq._role_log_metadata(log_file_path),
        )
        ui.log_io(f"[LLM UNAVAILABLE] {issue.summary}", "error", role_name)
        raise
    except ClaudeSDKError as e:
        # GLM 429 配额耗尽检测：与签名重试循环相同的检测逻辑。这覆盖那些
        # 绕过签名重试循环、直接在外层抛出的 429（例如 availability block
        # 路径，或 SDK 在建立流之前就拒绝的情况）。
        try:
            if _lq._is_quota_exceeded(str(e)):
                from rate_limiter import rate_limiter
                rate_limiter.parse_429(str(e))
                _lq._emit_llm_event(
                    "pipeline.llm_quota_exceeded_detected", "error",
                    (
                        f"{role_name}: GLM 429 quota exhaustion detected "
                        f"(outer handler); rate_limiter will block pipeline "
                        f"until reset"
                    ),
                    role=role_name,
                    elapsed_sec=round(time.time() - stream_started_at, 2),
                    messages_seen=message_count,
                    exception_type=type(e).__name__,
                    reset_time=(
                        rate_limiter.reset_time_str()
                        if rate_limiter.is_blocked() else None
                    ),
                    **_lq._role_log_metadata(log_file_path),
                )
                if ui:
                    ui.log_history(
                        f"{role_name}: GLM API 配额耗尽 (429)。"
                        + (
                            f" 将暂停进化直到 {rate_limiter.reset_time_str()} 自动恢复。"
                            if rate_limiter.is_blocked()
                            else " 未检测到重置时间。"
                        ),
                        "error",
                    )
        except Exception:
            pass
        availability_block = availability_trace.blocked(
            role=role_name,
            exception=e,
        )
        if availability_block is not None:
            issue = availability_block.issue
            _lq._emit_llm_event(
                "pipeline.llm_role_availability_blocked", "error",
                f"{role_name}: LLM availability blocked ({issue.category})",
                role=role_name,
                elapsed_sec=round(time.time() - stream_started_at, 2),
                messages_seen=message_count,
                exception_type=type(e).__name__,
                availability_category=issue.category,
                availability_issue=issue.as_dict(),
                **_lq._role_log_metadata(log_file_path),
            )
            ui.log_io(f"[LLM UNAVAILABLE] {issue.summary}", "error", role_name)
            raise availability_block from e
        _lq._emit_llm_event(
            "pipeline.llm_role_stream_sdk_error", "warn",
            f"{role_name}: SDK stream error: {str(e)[:180]}",
            role=role_name,
            elapsed_sec=round(time.time() - stream_started_at, 2),
            messages_seen=message_count,
            exception_type=type(e).__name__,
            error=str(e)[:500],
            **_lq._role_log_metadata(log_file_path),
        )
        ui.log_io(f"[ERROR] {e}", "error", role_name)
        raise   # propagate so callers distinguish a hard SDK error from an empty-but-valid reply
    except _lq.LLMRoleTimeout:
        # This is our own role-policy deadline, not evidence that the provider
        # transport failed.  Preserve the existing typed timeout contract.
        raise
    except Exception as e:
        availability_block = availability_trace.blocked(
            role=role_name,
            exception=e,
        )
        if availability_block is not None:
            issue = availability_block.issue
            _lq._emit_llm_event(
                "pipeline.llm_role_availability_blocked", "error",
                f"{role_name}: LLM availability blocked ({issue.category})",
                role=role_name,
                elapsed_sec=round(time.time() - stream_started_at, 2),
                messages_seen=message_count,
                exception_type=type(e).__name__,
                availability_category=issue.category,
                availability_issue=issue.as_dict(),
                **_lq._role_log_metadata(log_file_path),
            )
            ui.log_io(f"[LLM UNAVAILABLE] {issue.summary}", "error", role_name)
            raise availability_block from e
        raise
    except asyncio.CancelledError:
        _category, _severity, _cancel_fields = _lq._cancelled_event(
            "pipeline.llm_role_stream_cancelled",
            "pipeline.llm_role_stream_parent_timeout_cancelled",
        )
        _scope = _cancel_fields.get("cancel_scope")
        _timeout = _cancel_fields.get("timeout_sec")
        if _cancel_fields.get("cancel_reason") == "parent_timeout":
            _msg = (
                f"{role_name}: LLM stream cancelled by parent timeout"
                f" ({_scope}, {_timeout:g}s)"
                if isinstance(_timeout, (int, float))
                else f"{role_name}: LLM stream cancelled by parent timeout ({_scope})"
            )
        else:
            _msg = f"{role_name}: LLM stream cancelled"
        _lq._emit_llm_event(
            _category, _severity,
            _msg,
            role=role_name,
            elapsed_sec=round(time.time() - stream_started_at, 2),
            messages_seen=message_count,
            **_cancel_fields,
            **_lq._role_log_metadata(log_file_path),
        )
        ui.log_io(f"\n[{role_name} CANCELLED]", "error", role_name)
        raise
    finally:
        stream_done = True
        if 'watchdog_task' in locals():
            watchdog_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watchdog_task
    availability_block = availability_trace.blocked(role=role_name)
    if availability_block is not None:
        raise availability_block
    # Build timing metrics for call analytics (llm_call_metrics.jsonl).
    stream_end_at = time.time()
    stream_metrics = {
        "stream_elapsed_sec": round(stream_end_at - stream_started_at, 2),
        "first_token_latency_sec": (
            round(first_productive_at - stream_started_at, 2)
            if first_productive_at is not None else None
        ),
        "first_text_latency_sec": (
            round(first_text_at - stream_started_at, 2)
            if first_text_at is not None else None
        ),
        "thinking_tokens_estimated": thinking_tokens_estimate,
        "thinking_tokens_delta_total": thinking_tokens_delta_total,
        "text_block_count": len(texts),
        "thinking_chars": thinking_chars,
        "tool_use_count": tool_use_count,
        "tool_result_count": tool_result_count,
        "message_count": message_count,
        "assistant_message_count": assistant_message_count,
        "result_diag": result_diag,
    }
    return texts, cost_usd, usage, stream_metrics


# ---------------------------------------------------------------------------
# Billing helpers (private to the retry loop)
# ---------------------------------------------------------------------------

# claude_agent_sdk 0.2.91 intermittently raises ClaudeSDKError "Missing required
# field in assistant message: 'signature'" mid-stream. It is transient (a fresh
# query usually succeeds) but frequent enough that 3 retries occasionally exhaust,
# stalling Master/analyst. Bumped to 5 with slightly longer backoff so a brief
# SDK-side storm still resolves without surfacing a failure to the caller.
_SIGNATURE_MAX_ATTEMPTS = 5


def _merge_billing_usage(total, usage):
    if not isinstance(usage, dict):
        return total
    merged = dict(total or {})
    for key, value in usage.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            previous = merged.get(key, 0)
            if not isinstance(previous, (int, float)) or isinstance(previous, bool):
                previous = 0
            merged[key] = previous + value
        elif key not in merged:
            # Keep non-numeric metadata from the first result. It is not summed,
            # but callers do not lose fields such as service tier/model detail.
            merged[key] = value
    return merged


def _record_completed_billing_attempt(
    *,
    role_name,
    ui,
    billing_results,
    fallback_cost,
    fallback_usage,
    attempt,
    billing_call_id,
):
    """Record each SDK Result exactly once and return newly billed totals."""

    from orchestrator_cost_policy import (
        assert_operator_cost_limit_available,
        current_generation_cost_scope,
        record_generation_cost,
        sdk_result_event_id,
    )

    results = list(billing_results or [])
    if not results and (fallback_cost is not None or fallback_usage is not None):
        results = [None]
    billed_cost = 0.0
    billed_usage = None
    for result_index, result in enumerate(results):
        if result is None:
            cost_usd = fallback_cost
            usage = fallback_usage
            event_id = (
                f"llm-result-fallback:{billing_call_id}:"
                f"{int(attempt)}:{int(result_index)}"
            )
        else:
            cost_usd = getattr(result, "total_cost_usd", None)
            usage = getattr(result, "usage", None)
            event_id = sdk_result_event_id(
                result,
                source="llm_query",
                attempt=attempt,
            )
        status = record_generation_cost(
            role_name,
            cost_usd,
            usage,
            source="llm_query_attempt",
            event_id=event_id,
        )
        accepted = bool(
            not status.get("active")
            or status.get("recorded")
            or status.get("pending_only")
        )
        if accepted:
            if cost_usd is not None:
                billed_cost += float(cost_usd)
            billed_usage = _merge_billing_usage(billed_usage, usage)
            if ui:
                ui.update_cost(role_name, float(cost_usd or 0.0), usage)
        elif ui and status.get("active") and not status.get("accounting_ok"):
            # A pending write-ahead entry is already included in durable status;
            # refresh the projection without incrementing a replay twice.
            scope = current_generation_cost_scope()
            begin_cost = getattr(ui, "begin_generation_cost", None)
            if scope is not None and callable(begin_cost):
                begin_cost(
                    scope.generation_id,
                    status.get("spent_usd", 0.0),
                    scope.receipt(
                        spent_before_usd=float(status.get("spent_usd") or 0.0),
                        ledger_errors=tuple(status.get("accounting_errors") or ()),
                    ),
                )
        assert_operator_cost_limit_available()
    return billed_cost, billed_usage


def _raise_signature_retry_total_timeout(role_name, log_file_path):
    import llm_query as _lq

    scope = _lq._LLM_TOTAL_DEADLINE.get()
    timeout_sec = float((scope or {}).get("timeout_sec") or 0)
    started_at = float((scope or {}).get("started_at") or time.time())
    elapsed = max(0.0, time.time() - started_at)
    _lq._emit_llm_event(
        "pipeline.llm_role_total_timeout",
        "error",
        f"{role_name}: LLM total timeout after {timeout_sec:.1f}s",
        role=role_name,
        elapsed_sec=round(elapsed, 2),
        timeout_sec=round(timeout_sec, 2),
        retry_phase="sdk_signature_backoff",
        **_lq._role_log_metadata(log_file_path),
    )
    raise _lq.LLMRoleTimeout(role_name, "total", timeout_sec)


async def _signature_retry_sleep(delay, role_name, log_file_path):
    """Sleep without letting retry backoff cross the call-wide total deadline."""

    import llm_query as _lq

    scope = _lq._LLM_TOTAL_DEADLINE.get()
    deadline = (scope or {}).get("deadline") if isinstance(scope, dict) else None
    if deadline is None:
        await asyncio.sleep(delay)
        return
    remaining = float(deadline) - time.time()
    if remaining <= 0:
        _raise_signature_retry_total_timeout(role_name, log_file_path)
    if float(delay) >= remaining:
        await asyncio.sleep(max(0.0, remaining))
        _raise_signature_retry_total_timeout(role_name, log_file_path)
    await asyncio.sleep(delay)


# ---------------------------------------------------------------------------
# Bounded signature-retry loop
# ---------------------------------------------------------------------------

async def _run_stream_with_signature_retry(
    full_prompt, options, log_file_path, ui, role_name, *, semaphore=None
):
    """Run bounded SDK retries under one role-wide total wall-clock budget.

    ``semaphore`` (the global LLM semaphore) is acquired PER-ATTEMPT inside the
    retry loop, so signature-retry backoff sleeps release the permit and allow
    other LLM work to fill the gap.  This keeps the 2-permit pool utilized even
    during multi-attempt signature retries.
    """

    import llm_query as _lq

    policy = _lq._role_timeout_policy(role_name)
    total_timeout = float(policy.get("total_timeout") or 0)
    started_at = time.time()
    token = _lq._LLM_TOTAL_DEADLINE.set({
        "started_at": started_at,
        "deadline": (
            started_at + total_timeout if total_timeout > 0 else None
        ),
        "timeout_sec": total_timeout,
    })
    try:
        return await _run_stream_with_signature_retry_attempts(
            full_prompt, options, log_file_path, ui, role_name,
            semaphore=semaphore,
        )
    finally:
        _lq._LLM_TOTAL_DEADLINE.reset(token)


async def _run_stream_with_signature_retry_attempts(
    full_prompt, options, log_file_path, ui, role_name, *, semaphore=None
):
    """Run one streaming query with retries on transient SDK signature errors.

    Extracted so the 529/429 retry paths reuse the same handling as the initial query.
    Returns (texts_list, cost_usd, usage).

    ``semaphore`` (the global LLM semaphore) is acquired per-attempt around the
    actual stream processing.  Backoff sleeps between attempts run WITHOUT the
    permit, so other LLM roles can fill the gap during signature retries.
    """
    import llm_query as _lq

    last_sdk_err = None
    total_cost = 0.0
    total_usage = None
    billing_call_id = uuid.uuid4().hex
    _lq._assert_no_unresolved_provider_attempts()
    for sdk_attempt in range(_SIGNATURE_MAX_ATTEMPTS):
        _lq._assert_no_unresolved_provider_attempts()
        owned_transport = _lq._new_owned_sdk_transport(full_prompt, options)
        provider_attempt = _lq._new_provider_attempt(owned_transport)
        provider_token = _lq._LLM_PROVIDER_ATTEMPT.set(provider_attempt)
        try:
            query_gen = _lq.claude_query(
                prompt=full_prompt,
                options=options,
                transport=owned_transport,
            )
        except BaseException:
            _lq._LLM_PROVIDER_ATTEMPT.reset(provider_token)
            raise
        billing_results = []
        billing_token = _lq._LLM_BILLING_RESULTS.set(billing_results)
        _attempt_start = time.time()
        try:
            if semaphore is not None:
                async with semaphore:
                    texts, cost_usd, usage, stream_metrics = await _lq._process_stream(
                        query_gen, log_file_path, ui, role_name
                    )
            else:
                texts, cost_usd, usage, stream_metrics = await _lq._process_stream(
                    query_gen, log_file_path, ui, role_name
                )
            attempt_cost, attempt_usage = _record_completed_billing_attempt(
                role_name=role_name,
                ui=ui,
                billing_results=billing_results,
                fallback_cost=cost_usd,
                fallback_usage=usage,
                attempt=sdk_attempt,
                billing_call_id=billing_call_id,
            )
            # Record per-attempt call metrics for offline timing/token analysis.
            try:
                from llm_call_metrics import record_llm_call_metrics
                _um = _lq._usage_metadata(usage) if usage else {}
                _rd = stream_metrics.get("result_diag") or {}
                record_llm_call_metrics(
                    call_id=billing_call_id,
                    attempt=sdk_attempt,
                    max_attempts=_SIGNATURE_MAX_ATTEMPTS,
                    role=role_name,
                    model=getattr(options, "model", None),
                    total_elapsed_sec=time.time() - _attempt_start,
                    first_token_latency_sec=stream_metrics.get("first_token_latency_sec"),
                    first_text_latency_sec=stream_metrics.get("first_text_latency_sec"),
                    stream_active_sec=stream_metrics.get("stream_elapsed_sec"),
                    input_tokens=_um.get("input_tokens"),
                    output_tokens=_um.get("output_tokens"),
                    cache_creation_input_tokens=_um.get("cache_creation_input_tokens"),
                    cache_read_input_tokens=_um.get("cache_read_input_tokens"),
                    thinking_tokens_estimated=stream_metrics.get("thinking_tokens_estimated"),
                    thinking_tokens_delta_total=stream_metrics.get("thinking_tokens_delta_total"),
                    cost_usd=attempt_cost,
                    success=True,
                    sdk_subtype=_rd.get("subtype"),
                    stop_reason=_rd.get("stop_reason"),
                    num_turns=_rd.get("num_turns"),
                    terminal_reason=_rd.get("terminal_reason"),
                    sdk_duration_ms=_rd.get("duration_ms"),
                    sdk_duration_api_ms=_rd.get("duration_api_ms"),
                    sdk_session_id=_rd.get("session_id"),
                    sdk_uuid=_rd.get("uuid"),
                    sdk_result_text=_rd.get("result_text"),
                    model_usage=_rd.get("model_usage"),
                    raw_usage=(usage if isinstance(usage, dict) else
                               (usage.model_dump() if usage and hasattr(usage, "model_dump") else None)),
                    api_error_status=_rd.get("api_error_status"),
                    text_block_count=stream_metrics.get("text_block_count"),
                    thinking_chars=stream_metrics.get("thinking_chars"),
                    tool_use_count=stream_metrics.get("tool_use_count"),
                    tool_result_count=stream_metrics.get("tool_result_count"),
                    message_count=stream_metrics.get("message_count"),
                    assistant_message_count=stream_metrics.get("assistant_message_count"),
                    log_file=_lq._role_log_basename(log_file_path),
                )
            except Exception:
                pass
            total_cost += attempt_cost
            total_usage = _merge_billing_usage(total_usage, attempt_usage)
            if sdk_attempt > 0 and ui:
                ui.log_history(
                    f"{role_name}: SDK stream recovered after {sdk_attempt} signature retry/retries",
                    "info",
                )
            # Empty-output retry (root-cause fix for Master JSON collapse, 2026-06-19).
            # claude_agent_sdk 0.2.91's signature bug has TWO failure modes:
            #   (a) raises ClaudeSDKError mid-stream — caught above, retried.
            #   (b) stream "succeeds" with a ResultMessage (cost/usage present) but ZERO
            #       TextBlocks → _process_stream returns ([], cost, usage) WITHOUT raising.
            # Mode (b) escaped ALL retry layers (only ClaudeSDKError was caught), so the
            # empty output reached the caller, parse_json_output('') returned None, and
            # the agent logged "malformed JSON" → 3x retry exhaust → abandon_generation.
            # Measured impact: 140/540 (26%) of MASTER [COST] lines were in=0 out=0, and
            # 713 "Missing required field ... signature" errors appeared app-wide — this
            # is the true root cause of the v107-110/v116/v121/v125 "Master JSON collapse"
            # (previously mis-attributed to direction-audit constraints; that is only a
            # minor secondary factor for the real-output-but-rejected subset).
            # Fix: treat 0-TextBlock output as a signature-truncation variant and retry it
            # on the same backoff schedule. `continue` here runs the finally (aclose) then
            # the for-loop's next attempt. Retries exhausted → fall through to return
            # (caller sees empty output and handles it, same as today, but now rare).
            # Condition covers BOTH empty-output variants: 0 TextBlocks (texts=[]) AND
            # empty-string TextBlocks (texts=[""] — also out=0, another face of the SDK
            # signature-truncation bug where a TextBlock carries empty text). The plain
            # `not texts` check missed the texts=[""] case ([""] is truthy). `not any
            # (... .strip())` is True iff every text is empty/whitespace, catching both.
            if not any((t or "").strip() for t in texts) and sdk_attempt < _SIGNATURE_MAX_ATTEMPTS - 1:
                _backoff = min(5 * (2 ** sdk_attempt), 30)
                if ui:
                    ui.log_history(
                        f"{role_name}: SDK stream returned 0 TextBlocks (cost={cost_usd}) — "
                        f"signature-truncation variant, retrying in {_backoff}s "
                        f"(attempt {sdk_attempt+1}/{_SIGNATURE_MAX_ATTEMPTS})",
                        "warn",
                    )
                    try:
                        import event_bus
                        event_bus.warn(
                            "pipeline.llm_empty_output_retry",
                            f"{role_name} SDK stream returned 0 TextBlocks (signature-truncation variant)",
                            role=role_name, cost=cost_usd,
                            attempt=sdk_attempt + 1, max_attempts=_SIGNATURE_MAX_ATTEMPTS,
                        )
                    except Exception:
                        pass
                await _signature_retry_sleep(
                    _backoff, role_name, log_file_path
                )
                continue
            # A completed, non-error ResultMessage is success regardless of its
            # prose. Provider failures are raised by _process_stream before this
            # point; re-scanning model text would misread ordinary discussion of
            # quotas/overload as transport evidence.
            try:
                from api_concurrency import record_llm_outcome
                record_llm_outcome(success=True)
            except Exception:
                pass
            return texts, total_cost, total_usage
        except ClaudeSDKError as e:
            last_sdk_err = e
            err_str = str(e).lower()
            if ("signature" in err_str or "missing required field" in err_str) and \
                    sdk_attempt < _SIGNATURE_MAX_ATTEMPTS - 1:
                # Exponential-ish backoff: 5, 10, 20, 30s — short enough to not stall
                # the pipeline, long enough for a transient SDK state to clear.
                _backoff = min(5 * (2 ** sdk_attempt), 30)
                if ui:
                    ui.log_history(
                        f"{role_name}: SDK stream error (attempt {sdk_attempt+1}/{_SIGNATURE_MAX_ATTEMPTS}), "
                        f"retrying in {_backoff}s: {e}",
                        "warn",
                    )
                _lq._emit_llm_event(
                    "pipeline.llm_role_signature_retry", "warn",
                    f"{role_name}: SDK signature stream error, retrying in {_backoff}s",
                    role=role_name,
                    sdk_attempt=sdk_attempt + 1,
                    max_attempts=_SIGNATURE_MAX_ATTEMPTS,
                    backoff_sec=_backoff,
                    exception_type=type(e).__name__,
                    error=str(e)[:500],
                    **_lq._role_log_metadata(log_file_path),
                )
                await _signature_retry_sleep(
                    _backoff, role_name, log_file_path
                )
                continue
            # 自适应并发:非 signature 的 SDK error(可能含 503 熔断/overloaded/429)上报降并发
            try:
                _es = str(e).lower()
                if ("503" in _es or "overloaded" in _es or "熔断" in _es
                        or "所有供应商" in _es or "rate limit" in _es or "429" in _es):
                    from api_concurrency import record_llm_outcome
                    record_llm_outcome(success=False, rate_limited=True)
            except Exception:
                pass
            # GLM 429 配额耗尽检测：解析重置时间戳到全局 rate_limiter。
            # rate_limiter.parse_429 只在 GLM 返回明确的 "限额将在 ... 重置"
            # 时间戳时设置阻塞；无重置证据的裸 429 返回 False，不阻塞（保持
            # 现有有限重试行为）。一旦 rate_limiter 被设置，orchestrator_loop
            # 的 is_blocked() 检查会暂停整个 pipeline 直到恢复窗口，所有后续
            # run_claude_query 入口也会等待。这是 "等待恢复窗口" 语义的核心。
            try:
                if _lq._is_quota_exceeded(str(e)):
                    from rate_limiter import rate_limiter
                    rate_limiter.parse_429(str(e))
                    _lq._emit_llm_event(
                        "pipeline.llm_quota_exceeded_detected", "error",
                        (
                            f"{role_name}: GLM 429 quota exhaustion detected; "
                            f"rate_limiter will block pipeline until reset"
                        ),
                        role=role_name,
                        sdk_attempt=sdk_attempt + 1,
                        max_attempts=_SIGNATURE_MAX_ATTEMPTS,
                        reset_time=(
                            rate_limiter.reset_time_str()
                            if rate_limiter.is_blocked() else None
                        ),
                        **_lq._role_log_metadata(log_file_path),
                    )
                    if ui:
                        ui.log_history(
                            f"{role_name}: GLM API 配额耗尽 (429)。"
                            + (
                                f" 将暂停进化直到 {rate_limiter.reset_time_str()} 自动恢复。"
                                if rate_limiter.is_blocked()
                                else " 未检测到重置时间，将继续有限重试。"
                            ),
                            "error",
                        )
            except Exception:
                pass
            raise  # non-signature SDK error, or signature retries exhausted
        finally:
            _lq._LLM_BILLING_RESULTS.reset(billing_token)
            try:
                # The transport is unique to this attempt. If generator cleanup
                # races a cancellation-resistant ``__anext__``, terminate only
                # that owned transport and prove process/task exit before retry.
                await _lq._cleanup_owned_provider_attempt(
                    query_gen,
                    provider_attempt,
                    role_name,
                    log_file_path,
                )
            finally:
                _lq._LLM_PROVIDER_ATTEMPT.reset(provider_token)
    if last_sdk_err is not None:
        raise last_sdk_err


# ---------------------------------------------------------------------------
# JSON output parsing
# ---------------------------------------------------------------------------

def parse_json_output(output):
    # Strategy 1: Find ALL ```json blocks, try from LAST to first.
    # Handles the case where the LLM references the prompt template before the actual plan.
    json_starts = list(re.finditer(r'```json\s*', output))
    for json_start in reversed(json_starts):
        after_start = output[json_start.end():]
        # Find all ``` positions after ```json
        close_positions = [m.start() for m in re.finditer(r'```', after_start)]
        # Try from the LAST ``` backward (most likely the actual closing)
        for pos in reversed(close_positions):
            candidate = after_start[:pos].strip()
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
        # Also try the full text after ```json (in case no closing ```)
        try:
            return json.loads(after_start.strip().rstrip('`').strip())
        except json.JSONDecodeError:
            pass

    # Strategy 1.5: Brace-matching from each ```json start.
    # Handles embedded ``` inside JSON string values (e.g., worker_prompt with code blocks).
    # Tracks string boundaries so ``` inside strings are ignored.
    for json_start in reversed(json_starts):
        after_start = output[json_start.end():]
        brace_pos = after_start.find('{')
        if brace_pos == -1:
            continue
        depth = 0
        in_string = False
        escape_next = False
        for i in range(brace_pos, len(after_start)):
            c = after_start[i]
            if escape_next:
                escape_next = False
                continue
            if c == '\\' and in_string:
                escape_next = True
                continue
            if c == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    candidate = after_start[brace_pos:i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        break  # brace match failed, try next ```json block

    # Strategy 2: Try the whole output as raw JSON
    try:
        return json.loads(output)
    except Exception:
        pass
    return None


def parse_json_output_with_mode(output):
    """Same parsing as parse_json_output, but returns a classifiable failure mode.

    Returns ``(data, failure_mode)`` where ``failure_mode`` is one of:
      - ``"OK"``          — parsed successfully (data is the dict)
      - ``"NO_JSON"``     — output empty/whitespace (no text to parse at all)
      - ``"NO_FENCE"``    — output has text but no JSON structure (no ```json
                            block and no ``{``); the model never emitted JSON
      - ``"PARSE_ERROR"`` — output looked like JSON (had a fence or brace) but
                            every parse strategy failed

    The mode lets callers (notably _run_master_analysis) log a CLASSIFIABLE
    reason instead of the undifferentiated "malformed JSON" that previously
    hid three distinct root causes (missing-return / NO_FENCE / empty-output).
    """
    if not output or not output.strip():
        return None, "NO_JSON"
    data = parse_json_output(output)
    if data is not None:
        return data, "OK"
    # parse_json_output exhausted every strategy — distinguish why.
    has_fence = "```json" in output
    has_brace = "{" in output
    if has_fence or has_brace:
        return None, "PARSE_ERROR"
    return None, "NO_FENCE"
