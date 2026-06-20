"""LLM query primitive and JSON output parsing.

Provides run_claude_query() for all sub-agent LLM calls, and parse_json_output()
for extracting structured data from LLM responses.
"""

import asyncio
import json
import logging
import os
import re
import threading

from claude_agent_sdk import (
    query as claude_query,
    ClaudeAgentOptions,
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
    ThinkingBlock,
    ClaudeSDKError,
)

log = logging.getLogger("pok.infra")

# Serialize role-IO log rotation across threads/processes (mirrors
# battle_experience._LOG_ROTATION_LOCK). Without this lock, two concurrent
# appenders can both observe the file over the size cap and race the rename
# (one wins, the other's rename throws FileNotFoundError — swallowed by the
# except, benign but loses the backup). The lock makes the rotate-then-append
# atomic. A threading.Lock suffices within one process; cross-process safety
# for the append itself is provided by fcntl (locked_file below).
_ROLE_IO_ROTATION_LOCK = threading.Lock()

#: Cap a single role-IO log at 20MB before rotating to one backup (``.1``).
#: battle_exp_llm.log previously grew to 103MB with no upper bound (root-cause
#: 6); this is the structural cap. Mirrors battle_experience's 50MB cap
#: (lowered here because role-IO files are append-heavy and per-role).
_ROLE_IO_MAX_BYTES = 20 * 1024 * 1024


def _append_role_io(log_file_path, text):
    """Append text to a role-IO log file with fcntl locking + 20MB rotation.

    Replaces the bare ``with open(path, "a") as lf: lf.write(...)`` pattern that
    had no locking and no size cap (root-cause 6: battle_exp_llm.log reached
    103MB).

      - fcntl LOCK_EX via ``evolution_infra.locked_file`` → cross-process +
        cross-thread safe (orchestrator + battle_experience workers append
        concurrently to the same path).
      - Before writing: if the file exceeds ``_ROLE_IO_MAX_BYTES`` (20MB),
        rename it to ``.1`` (single overwrite backup, mirroring
        battle_experience._LOG_ROTATION_LOCK). Rotation is serialized by
        ``_ROLE_IO_ROTATION_LOCK`` so two appenders can't race the rename.
      - Each appended chunk is prefixed with ``[<run_id>] `` (or ``[-]`` when
        no run_id is resolvable) so role-IO lines join app.log + events.jsonl
        on the same correlation key (RC6).

    Never raises — logging must not crash the pipeline. Returns silently on any
    error (the underlying stream processing / return value is unaffected).
    """
    try:
        # Resolve the current run_id for the correlation prefix. event_bus reads
        # the live checkpoint as fallback, so this works even in long-lived
        # worker threads that are not pinned to one generation.
        try:
            from event_bus import capture_context
            _ctx = capture_context() or {}
            _rid = _ctx.get("run_id") or "-"
        except Exception:
            _rid = "-"
        chunk = f"[{_rid}] {text}" if not text.startswith("\n") else f"\n[{_rid}] " + text.lstrip("\n")
        # Rotation check + rename (serialized; size read without a lock, which
        # is best-effort — a concurrent writer can grow the file between the
        # stat and the rename, but that only delays rotation by one cycle).
        try:
            if os.path.exists(log_file_path) and os.path.getsize(log_file_path) > _ROLE_IO_MAX_BYTES:
                with _ROLE_IO_ROTATION_LOCK:
                    if os.path.exists(log_file_path) and os.path.getsize(log_file_path) > _ROLE_IO_MAX_BYTES:
                        _rotated = log_file_path + ".1"
                        try:
                            if os.path.exists(_rotated):
                                os.remove(_rotated)
                        except Exception:
                            pass
                        try:
                            os.rename(log_file_path, _rotated)
                        except Exception:
                            pass
        except Exception:
            pass
        from evolution_infra import locked_file
        with locked_file(log_file_path, "a", encoding="utf-8") as lf:
            lf.write(chunk)
    except Exception:
        pass


def extract_result_error(message) -> str:
    """Extract diagnostic error text from a ResultMessage.

    Uses correct SDK attributes:
    - message.errors: list[str]|None — error messages from the SDK
    - message.api_error_status: int|None — HTTP status code (429, 500, etc.)

    Falls back to 'Unknown SDK error' if no error info is available.
    """
    _err_list = getattr(message, 'errors', None) or []
    _status = getattr(message, 'api_error_status', None)
    if _err_list:
        return '; '.join(str(e) for e in _err_list)
    if _status:
        return f'API error {_status}'
    return 'Unknown SDK error'


def _is_rate_limited(output: str) -> bool:
    # Long responses are never rate-limit errors — avoid false positives
    # when LLM discusses "rate limit" or "overloaded" in normal output.
    # NOTE: 429 "Request rejected" is handled separately by _is_quota_exceeded()
    # to avoid triggering the 529 exponential-backoff retry loop.
    if len(output) > 2000:
        return False
    return (
        "overloaded" in output.lower()
        or "该模型当前访问量过大" in output
        or "rate limit" in output.lower()
        or re.search(r'(?:status["\s:=]+529|HTTP/\d\.?\d?\s+529|error.*529)', output, re.IGNORECASE) is not None
    )


def _is_quota_exceeded(output: str) -> bool:
    """Detect 429 quota exhaustion (distinct from 529 overloaded).

    Matches the GLM API error pattern:
        "Request rejected (429) · [1308][已达到 5 小时的使用上限...]"
    """
    if len(output) > 2000:
        return False
    return (
        "Request rejected (429)" in output
        or ("已达到" in output and "使用上限" in output)
    )


def _trim_to_budget(text: str, max_chars: int, tail: bool = False) -> str:
    """Trim text to max_chars. If tail=True, keep the LAST max_chars (most recent content)."""
    if len(text) <= max_chars:
        return text
    note = "\n...[TRIMMED]\n"
    if tail:
        return note + text[-(max_chars - len(note)):]
    return text[:max_chars - len(note)] + note


async def _process_stream(query_gen, log_file_path, ui, role_name):
    """Process a streaming LLM query, returning (texts, cost_usd, usage).

    Handles TextBlock, ThinkingBlock, ToolUseBlock, ToolResultBlock, and ResultMessage.
    Writes to log file and emits UI events as they arrive.
    """
    texts = []
    cost_usd = None
    usage = None
    try:
        async for message in query_gen:
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        text = block.text
                        texts.append(text)
                        _append_role_io(log_file_path, text + "\n")
                        ui.log_io(text, "claude", role_name)
                    elif isinstance(block, ThinkingBlock):
                        thinking = block.thinking or "[thinking...]"
                        _append_role_io(log_file_path, f"\n[THINKING] {thinking[:2000]}\n")
                        ui.log_io(thinking, "thinking", role_name)
                    elif isinstance(block, ToolUseBlock):
                        args_str = json.dumps(block.input, ensure_ascii=False, indent=2)[:2000]
                        _append_role_io(log_file_path, f"\n[TOOL_CALL] {block.name}\n[ARGS] {args_str}\n")
                        ui.log_io(f"\n[tool: {block.name}]", "tool", role_name)
                        ui.emit_tool_call(block.name, block.input, role_name)
                    elif isinstance(block, ToolResultBlock):
                        content = block.content if isinstance(block.content, str) else (
                            json.dumps(block.content, ensure_ascii=False) if block.content is not None else ""
                        )
                        if content:
                            _append_role_io(log_file_path, f"\n[TOOL_RESULT] {content[:3000]}\n")
                            ui.log_io(content[:3000], "tool_result", role_name)
            elif isinstance(message, ResultMessage):
                cost_usd = message.total_cost_usd
                usage = message.usage
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
                        _append_role_io(
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
    except ClaudeSDKError as e:
        ui.log_io(f"[ERROR] {e}", "error", role_name)
        raise   # propagate so callers distinguish a hard SDK error from an empty-but-valid reply
    except asyncio.CancelledError:
        ui.log_io(f"\n[{role_name} CANCELLED]", "error", role_name)
        raise
    return texts, cost_usd, usage


# claude_agent_sdk 0.2.91 intermittently raises ClaudeSDKError "Missing required
# field in assistant message: 'signature'" mid-stream. It is transient (a fresh
# query usually succeeds) but frequent enough that 3 retries occasionally exhaust,
# stalling Master/analyst. Bumped to 5 with slightly longer backoff so a brief
# SDK-side storm still resolves without surfacing a failure to the caller.
_SIGNATURE_MAX_ATTEMPTS = 5


async def _run_stream_with_signature_retry(full_prompt, options, log_file_path, ui, role_name):
    """Run one streaming query with retries on transient SDK signature errors.

    Extracted so the 529/429 retry paths reuse the same handling as the initial query.
    Returns (texts_list, cost_usd, usage).
    """
    last_sdk_err = None
    for sdk_attempt in range(_SIGNATURE_MAX_ATTEMPTS):
        query_gen = claude_query(prompt=full_prompt, options=options)
        try:
            texts, cost_usd, usage = await _process_stream(query_gen, log_file_path, ui, role_name)
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
                await asyncio.sleep(_backoff)
                continue
            return texts, cost_usd, usage
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
                await asyncio.sleep(_backoff)
                continue
            raise  # non-signature SDK error, or signature retries exhausted
        finally:
            # Defensive: ensure SDK generator is closed so subprocess is terminated.
            try:
                await query_gen.aclose()
            except Exception:
                pass  # suppress any aclose() errors
    if last_sdk_err is not None:
        raise last_sdk_err


async def run_claude_query(prompt, context_files, ui, role_name, log_file_path, model="sonnet", tools=None):
    """Run a Claude query via the Agent SDK with cost tracking and typed streaming.

    tools: list of built-in tool names (e.g. ["Bash", "Read"]) or a ToolsPreset dict.
           When None, no built-in tools are exposed to the model.
    """
    # Pre-check: if already rate-limited, wait before making any API call
    from rate_limiter import rate_limiter
    if rate_limiter.is_blocked():
        if ui:
            ui.log_history(
                f"API 配额受限，等待至 {rate_limiter.reset_time_str()}...",
                "warn",
            )
        await rate_limiter.wait_until_reset()

    from evolution_infra import PROJECT_ROOT, MAX_PROMPT_CHARS, _BLOCKED_MCP_TOOLS

    # Build (path, content) pairs for context files
    context_parts = []
    if context_files:
        for cf in context_files:
            if os.path.exists(cf):
                with open(cf, 'r') as f:
                    context_parts.append((cf, f.read()))

    # Assemble prompt with context files, smart-budgeting if needed
    if context_parts:
        ctx_section = "\n\n# Context Files:\n" + "".join(
            f"\n--- {p} ---\n{c}\n" for p, c in context_parts
        )
        full_prompt = prompt + ctx_section
        if len(full_prompt) > MAX_PROMPT_CHARS:
            # Compress context_files proportionally while keeping base prompt intact
            budget_for_files = MAX_PROMPT_CHARS - len(prompt) - 500
            if budget_for_files > 0:
                per_file = max(budget_for_files // len(context_parts), 500)
                ctx_section = "\n\n# Context Files:\n" + "".join(
                    f"\n--- {p} ---\n{_trim_to_budget(c, per_file)}\n"
                    for p, c in context_parts
                )
                full_prompt = prompt + ctx_section
            else:
                full_prompt = prompt + "\n\n[Context files omitted — prompt too long]"
            ui.log_history(f"Prompt budgeted to {len(full_prompt):,} chars (context compressed)", "warn")
    else:
        full_prompt = prompt
        if len(full_prompt) > MAX_PROMPT_CHARS:
            ui.log_history(f"Prompt too long ({len(full_prompt):,} chars), trimming...", "warn")
            full_prompt = _trim_to_budget(full_prompt, MAX_PROMPT_CHARS)

    ui.log_io(f"\n[{role_name} PROMPT]", "prompt", role_name)
    ui.log_io(prompt[:200] + "...\n[Context Attached]", "prompt", role_name)
    ui.log_io("\n[WAITING FOR CLAUDE...]\n", "prompt", role_name)

    _append_role_io(
        log_file_path,
        f"\n[{role_name} PROMPT]\n=============================\n"
        + full_prompt
        + "\n=============================\n[CLAUDE OUTPUT]\n",
    )

    options = ClaudeAgentOptions(
        model=model,
        permission_mode="bypassPermissions",
        cwd=str(PROJECT_ROOT),  # pok/ — workers use relative paths like bots/claude_vN/
        tools=tools,
        disallowed_tools=_BLOCKED_MCP_TOOLS,
        # Adaptive thinking: let Claude decide when/how much to think (Opus 4.6+).
        # Previously disabled because adaptive was combined with `display:summarized`
        # (an Opus-4.7-only flag) which interacted badly with older models. Plain
        # `{"type":"adaptive"}` is the documented default and no longer co-triggers
        # the SDK signature-field bug. See llm_query._run_stream_with_signature_retry
        # for the remaining transient-signature safety net.
        thinking={"type": "adaptive"},
    )

    # Initial query — retry transient SDK stream errors (signature field missing).
    # claude_agent_sdk 0.2.91 intermittently raises ClaudeSDKError "Missing required
    # field in assistant message: 'signature'"; a fresh query usually succeeds.
    # Without this retry, the error propagates and the calling tool either rejects
    # (critic) or skips (battle_exp), stalling the pipeline.
    full_text, cost_usd, usage = await _run_stream_with_signature_retry(
        full_prompt, options, log_file_path, ui, role_name)

    output = "\n".join(full_text)

    # Auto-retry on API rate limit (529) with exponential backoff
    if _is_rate_limited(output):
        for backoff in [30, 60, 120]:
            ui.log_history(f"API rate limited (529). Retrying in {backoff}s...", "warn")
            await asyncio.sleep(backoff)
            full_text.clear()
            retry_texts, retry_cost, retry_usage = await _run_stream_with_signature_retry(
                full_prompt, options, log_file_path, ui, role_name)
            if retry_texts:
                full_text.extend(retry_texts)
            if retry_cost:
                cost_usd = (cost_usd or 0) + retry_cost
            if retry_usage:
                if usage is None:
                    usage = retry_usage
                else:
                    merged = {}
                    for k in ("input_tokens", "output_tokens"):
                        merged[k] = (usage.get(k, 0) or 0) + (retry_usage.get(k, 0) or 0)
                    usage = merged

            output = "\n".join(full_text)
            if not _is_rate_limited(output):
                break

    # 429 quota exhaustion — parse reset time, block until reset, then retry once
    if _is_quota_exceeded(output):
        if rate_limiter.parse_429(output):
            wait = rate_limiter.wait_seconds()
            ui.log_history(
                f"API 配额耗尽 (429)。等待 {wait:.0f}s 至 {rate_limiter.reset_time_str()}",
                "error",
            )
            await rate_limiter.wait_until_reset()
            # Retry after reset
            full_text.clear()
            retry_texts, retry_cost, retry_usage = await _run_stream_with_signature_retry(
                full_prompt, options, log_file_path, ui, role_name)
            if retry_texts:
                full_text.extend(retry_texts)
            if retry_cost:
                cost_usd = (cost_usd or 0) + retry_cost
            if retry_usage:
                if usage is None:
                    usage = retry_usage
                else:
                    merged = {}
                    for k in ("input_tokens", "output_tokens"):
                        merged[k] = (usage.get(k, 0) or 0) + (retry_usage.get(k, 0) or 0)
                    usage = merged
            output = "\n".join(full_text)

    ui.update_cost(role_name, cost_usd, usage)

    return output, cost_usd, usage


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
