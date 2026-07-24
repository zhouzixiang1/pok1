"""Detailed LLM call metrics logger.

Records a structured JSON line for every LLM dispatch (each attempt of each
role) to ``web/core/results/llm_call_metrics.jsonl``.  Designed for offline
analysis of timing, token usage, cost, and error patterns to tune timeouts,
concurrency, and model configuration.

Schema (inspired by LangSmith, Helicone, OpenTelemetry GenAI conventions):

    {
      "schema_version": 1,
      "ts": "2026-07-25T07:15:32.123456+08:00",   # ISO timestamp
      "epoch_ts": 1784934600.12,                    # Unix epoch float
      "call_id": "a1b2c3d4...",                     # unique per run_claude_query call
      "attempt": 0,                                 # signature retry attempt (0-based)
      "role": "MASTER PROPOSAL mechanism",          # role name
      "model": "glm-5.2",                           # resolved model id

      # --- Timing (seconds) ---
      "total_elapsed_sec": 312.5,                   # wall-clock for this attempt
      "first_token_latency_sec": 1.2,              # time to first productive message
      "first_text_latency_sec": 245.3,             # time to first assistant text block
      "semaphore_wait_sec": 0.0,                   # time blocked on global LLM semaphore
      "stream_active_sec": 310.0,                  # last "stream active" timestamp delta

      # --- Token usage (from SDK usage) ---
      "input_tokens": 40562,
      "output_tokens": 23473,
      "cache_creation_input_tokens": 0,
      "cache_read_input_tokens": 23360,
      "thinking_tokens_estimated": 49552,          # max estimate from SystemMessage telemetry
      "thinking_tokens_delta_total": 55656,        # cumulative delta (all thinking chunks)
      "total_tokens": 64035,                       # input + output (computed)

      # --- Throughput ---
      "output_tokens_per_sec": 75.2,               # output_tokens / total_elapsed
      "total_tokens_per_sec": 204.9,               # total_tokens / total_elapsed

      # --- Cost ---
      "cost_usd": 0.837018,

      # --- Status ---
      "success": true,
      "error_type": null,                          # e.g. "ClaudeSDKError", "asyncio.TimeoutError"
      "error_message": null,                       # truncated to 500 chars

      # --- Configuration ---
      "effort": "max",
      "thinking_budget": 64000,
      "thinking_mode": "enabled",
      "global_concurrency": 2,

      # --- Context ---
      "prompt_chars": 15234,
      "output_chars": 8421,
      "text_block_count": 1,
      "invocation_id": "8aa3c35cee744fc19de52aa4c9003d9b",  # strict authority (if any)
      "generation_id": "generation:1:workflow-v3",
      "log_file": "v1/logs/strict_invocations/8aa3.../master_proposal_mechanism_io.txt"
    }
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path


_SCHEMA_VERSION = 1
_METRICS_FILE_NAME = "llm_call_metrics.jsonl"


def _metrics_file() -> Path:
    """Return the path to the metrics JSONL file under results/."""
    try:
        from evolution_infra import RESULTS_DIR
        return Path(RESULTS_DIR) / _METRICS_FILE_NAME
    except Exception:
        return Path("web/core/results") / _METRICS_FILE_NAME


def _truncate(value, limit=500):
    if value is None:
        return None
    text = str(value)
    return text[:limit] if len(text) > limit else text


def record_llm_call_metrics(
    *,
    call_id,
    attempt,
    role,
    model,
    total_elapsed_sec,
    first_token_latency_sec=None,
    first_text_latency_sec=None,
    semaphore_wait_sec=None,
    stream_active_sec=None,
    input_tokens=None,
    output_tokens=None,
    cache_creation_input_tokens=None,
    cache_read_input_tokens=None,
    thinking_tokens_estimated=None,
    thinking_tokens_delta_total=None,
    cost_usd=None,
    success=True,
    error_type=None,
    error_message=None,
    api_error_status=None,
    stop_reason=None,
    num_turns=None,
    sdk_subtype=None,
    sdk_duration_ms=None,
    sdk_duration_api_ms=None,
    terminal_reason=None,
    sdk_session_id=None,
    sdk_uuid=None,
    sdk_result_text=None,
    model_usage=None,
    raw_usage=None,
    effort=None,
    thinking_budget=None,
    thinking_mode=None,
    global_concurrency=None,
    prompt_chars=None,
    output_chars=None,
    text_block_count=None,
    thinking_chars=None,
    tool_use_count=None,
    tool_result_count=None,
    message_count=None,
    assistant_message_count=None,
    invocation_id=None,
    generation_id=None,
    log_file=None,
    timeout_kind=None,
    max_attempts=None,
):
    """Append one structured metrics record to llm_call_metrics.jsonl.

    All writes are best-effort: any exception is swallowed so metrics
    recording can never affect the LLM dispatch path.
    """
    try:
        input_tok = int(input_tokens) if input_tokens is not None else 0
        output_tok = int(output_tokens) if output_tokens is not None else 0
        total_tok = input_tok + output_tok

        elapsed = float(total_elapsed_sec) if total_elapsed_sec else 0.0
        output_per_sec = round(output_tok / elapsed, 1) if elapsed > 0 else None
        total_per_sec = round(total_tok / elapsed, 1) if elapsed > 0 else None

        # Cache hit rate KPI (Langfuse pattern)
        cache_read = int(cache_read_input_tokens) if cache_read_input_tokens else 0
        cache_write = int(cache_creation_input_tokens) if cache_creation_input_tokens else 0
        cache_total = cache_read + cache_write + input_tok
        cache_hit_rate = round(cache_read / cache_total, 4) if cache_total > 0 else None

        now = time.time()
        record = {
            "schema_version": _SCHEMA_VERSION,
            "ts": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
            "epoch_ts": round(now, 3),
            "call_id": str(call_id) if call_id else None,
            "attempt": int(attempt) if attempt is not None else 0,
            "max_attempts": int(max_attempts) if max_attempts is not None else None,
            "role": str(role) if role else None,
            "model": str(model) if model else None,
            # Timing
            "total_elapsed_sec": round(elapsed, 2),
            "first_token_latency_sec": round(float(first_token_latency_sec), 2) if first_token_latency_sec is not None else None,
            "first_text_latency_sec": round(float(first_text_latency_sec), 2) if first_text_latency_sec is not None else None,
            "semaphore_wait_sec": round(float(semaphore_wait_sec), 3) if semaphore_wait_sec is not None else None,
            "stream_active_sec": round(float(stream_active_sec), 2) if stream_active_sec is not None else None,
            # Tokens
            "input_tokens": input_tok or None,
            "output_tokens": output_tok or None,
            "cache_creation_input_tokens": int(cache_creation_input_tokens) if cache_creation_input_tokens else None,
            "cache_read_input_tokens": int(cache_read_input_tokens) if cache_read_input_tokens else None,
            "cache_hit_rate": cache_hit_rate,
            "thinking_tokens_estimated": int(thinking_tokens_estimated) if thinking_tokens_estimated else None,
            "thinking_tokens_delta_total": int(thinking_tokens_delta_total) if thinking_tokens_delta_total else None,
            "total_tokens": total_tok or None,
            # Throughput
            "output_tokens_per_sec": output_per_sec,
            "total_tokens_per_sec": total_per_sec,
            # Cost
            "cost_usd": round(float(cost_usd), 6) if cost_usd is not None else None,
            # Status
            "success": bool(success),
            "error_type": str(error_type) if error_type else None,
            "error_message": _truncate(error_message),
            "api_error_status": int(api_error_status) if api_error_status is not None else None,
            "stop_reason": str(stop_reason) if stop_reason else None,
            "num_turns": int(num_turns) if num_turns is not None else None,
            "sdk_subtype": str(sdk_subtype) if sdk_subtype else None,
            "terminal_reason": str(terminal_reason) if terminal_reason else None,
            "timeout_kind": str(timeout_kind) if timeout_kind else None,
            # SDK-reported durations (from ResultMessage)
            "sdk_duration_ms": int(sdk_duration_ms) if sdk_duration_ms is not None else None,
            "sdk_duration_api_ms": int(sdk_duration_api_ms) if sdk_duration_api_ms is not None else None,
            "sdk_session_id": str(sdk_session_id) if sdk_session_id else None,
            "sdk_uuid": str(sdk_uuid) if sdk_uuid else None,
            "sdk_result_text": _truncate(sdk_result_text, 200),
            # Per-model breakdown (from ResultMessage.model_usage)
            "model_usage": model_usage if isinstance(model_usage, dict) else None,
            # Raw usage dict (preserves all vendor-extension keys)
            "raw_usage": raw_usage if isinstance(raw_usage, dict) else None,
            # Config
            "effort": str(effort) if effort else None,
            "thinking_budget": int(thinking_budget) if thinking_budget else None,
            "thinking_mode": str(thinking_mode) if thinking_mode else None,
            "global_concurrency": int(global_concurrency) if global_concurrency else None,
            # Context
            "prompt_chars": int(prompt_chars) if prompt_chars is not None else None,
            "output_chars": int(output_chars) if output_chars is not None else None,
            "text_block_count": int(text_block_count) if text_block_count is not None else None,
            "thinking_chars": int(thinking_chars) if thinking_chars is not None else None,
            "tool_use_count": int(tool_use_count) if tool_use_count is not None else None,
            "tool_result_count": int(tool_result_count) if tool_result_count is not None else None,
            "message_count": int(message_count) if message_count is not None else None,
            "assistant_message_count": int(assistant_message_count) if assistant_message_count is not None else None,
            "invocation_id": str(invocation_id) if invocation_id else None,
            "generation_id": str(generation_id) if generation_id else None,
            "log_file": str(log_file) if log_file else None,
        }

        path = _metrics_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass
