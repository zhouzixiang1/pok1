"""LLM query primitive and JSON output parsing.

Provides run_claude_query() for all sub-agent LLM calls, and parse_json_output()
for extracting structured data from LLM responses.
"""

import asyncio
import json
import logging
import os
import re

from claude_agent_sdk import (
    query as claude_query,
    ClaudeAgentOptions,
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
    ThinkingBlock,
    CLINotFoundError,
    ProcessError,
)

log = logging.getLogger("pok.infra")


def _is_rate_limited(output: str) -> bool:
    return (
        "overloaded" in output.lower()
        or "该模型当前访问量过大" in output
        or "rate limit" in output.lower()
        or re.search(r'(?:status["\s:=]+529|HTTP/\d\.?\d?\s+529|error.*529)', output, re.IGNORECASE) is not None
    )


def _trim_to_budget(text: str, max_chars: int, tail: bool = False) -> str:
    """Trim text to max_chars. If tail=True, keep the LAST max_chars (most recent content)."""
    if len(text) <= max_chars:
        return text
    note = "\n...[TRIMMED]\n"
    if tail:
        return note + text[-(max_chars - len(note)):]
    return text[:max_chars - len(note)] + note


async def run_claude_query(prompt, context_files, ui, role_name, log_file_path, model="sonnet", tools=None):
    """Run a Claude query via the Agent SDK with cost tracking and typed streaming.

    tools: list of built-in tool names (e.g. ["Bash", "Read"]) or a ToolsPreset dict.
           When None, no built-in tools are exposed to the model.
    """
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

    with open(log_file_path, "a") as lf:
        lf.write(f"\n[{role_name} PROMPT]\n=============================\n")
        lf.write(full_prompt)
        lf.write("\n=============================\n[CLAUDE OUTPUT]\n")

    options = ClaudeAgentOptions(
        model=model,
        permission_mode="bypassPermissions",
        cwd=str(PROJECT_ROOT),  # pok/ — workers use relative paths like bots/claude_vN/
        tools=tools,
        disallowed_tools=_BLOCKED_MCP_TOOLS,
        thinking={"type": "adaptive", "display": "summarized"},
    )

    full_text = []
    cost_usd = None
    usage = None

    query_gen = None
    try:
        query_gen = claude_query(prompt=full_prompt, options=options)
        async for message in query_gen:
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        text = block.text
                        full_text.append(text)
                        with open(log_file_path, "a") as lf:
                            lf.write(text + "\n")
                        ui.log_io(text, "claude", role_name)
                    elif isinstance(block, ThinkingBlock):
                        thinking = block.thinking or "[thinking...]"
                        with open(log_file_path, "a") as lf:
                            lf.write(f"\n[THINKING] {thinking[:2000]}\n")
                        ui.log_io(thinking, "thinking", role_name)
                    elif isinstance(block, ToolUseBlock):
                        args_str = json.dumps(block.input, ensure_ascii=False, indent=2)[:2000]
                        with open(log_file_path, "a") as lf:
                            lf.write(f"\n[TOOL_CALL] {block.name}\n[ARGS] {args_str}\n")
                        ui.log_io(f"\n[tool: {block.name}]", "tool", role_name)
                        ui.emit_tool_call(block.name, block.input, role_name)
                    elif isinstance(block, ToolResultBlock):
                        content = block.content if isinstance(block.content, str) else (
                            json.dumps(block.content, ensure_ascii=False) if block.content is not None else ""
                        )
                        if content:
                            with open(log_file_path, "a") as lf:
                                lf.write(f"\n[TOOL_RESULT] {content[:3000]}\n")
                            ui.log_io(content[:3000], "tool_result", role_name)
            elif isinstance(message, ResultMessage):
                cost_usd = message.total_cost_usd
                usage = message.usage
    except (CLINotFoundError, ProcessError) as e:
        ui.log_io(f"[ERROR] {e}", "error", role_name)
        if query_gen is not None:
            try:
                await query_gen.aclose()
            except Exception:
                pass
    except asyncio.CancelledError:
        ui.log_io(f"\n[{role_name} CANCELLED]", "error", role_name)
        if query_gen is not None:
            try:
                await query_gen.aclose()
            except Exception:
                pass
        raise

    output = "\n".join(full_text)

    # Auto-retry on API rate limit (529) with exponential backoff
    if _is_rate_limited(output):
        for backoff in [30, 60, 120]:
            ui.log_history(f"API rate limited (529). Retrying in {backoff}s...", "warn")
            await asyncio.sleep(backoff)
            full_text.clear()
            retry_gen = None
            try:
                retry_gen = claude_query(prompt=full_prompt, options=options)
                async for message in retry_gen:
                    if isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                text = block.text
                                full_text.append(text)
                                with open(log_file_path, "a") as lf:
                                    lf.write(text + "\n")
                                ui.log_io(text, "claude", role_name)
                            elif isinstance(block, ThinkingBlock):
                                thinking = block.thinking or "[thinking...]"
                                with open(log_file_path, "a") as lf:
                                    lf.write(f"\n[THINKING] {thinking[:2000]}\n")
                                ui.log_io(thinking, "thinking", role_name)
                            elif isinstance(block, ToolUseBlock):
                                args_str = json.dumps(block.input, ensure_ascii=False, indent=2)[:2000]
                                with open(log_file_path, "a") as lf:
                                    lf.write(f"\n[TOOL_CALL] {block.name}\n[ARGS] {args_str}\n")
                                ui.log_io(f"\n[tool: {block.name}]", "tool", role_name)
                                ui.emit_tool_call(block.name, block.input, role_name)
                            elif isinstance(block, ToolResultBlock):
                                content = block.content if isinstance(block.content, str) else (
                                    json.dumps(block.content, ensure_ascii=False) if block.content is not None else ""
                                )
                                if content:
                                    with open(log_file_path, "a") as lf:
                                        lf.write(f"\n[TOOL_RESULT] {content[:3000]}\n")
                                    ui.log_io(content[:3000], "tool_result", role_name)
                    elif isinstance(message, ResultMessage):
                        cost_usd = (cost_usd or 0) + (message.total_cost_usd or 0)
                        if usage is None:
                            usage = message.usage
                        elif message.usage:
                            merged = {}
                            for k in ("input_tokens", "output_tokens"):
                                merged[k] = (usage.get(k, 0) or 0) + (message.usage.get(k, 0) or 0)
                            usage = merged
            except (CLINotFoundError, ProcessError) as e:
                ui.log_io(f"[ERROR] {e}", "error", role_name)
                if retry_gen is not None:
                    try:
                        await retry_gen.aclose()
                    except Exception:
                        pass
            except asyncio.CancelledError:
                if retry_gen is not None:
                    try:
                        await retry_gen.aclose()
                    except Exception:
                        pass
                raise

            output = "\n".join(full_text)
            if not _is_rate_limited(output):
                break

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
