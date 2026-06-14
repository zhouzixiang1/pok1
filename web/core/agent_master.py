"""Master Architect agent: plans worker tasks for the next evolution generation.

Analysis helpers (stagnation, direction audit, replay, experience, archivist)
live in their own modules. This module keeps the core Master and match analysis.
"""

import json
import time

from evolution_infra import (
    run_claude_query, parse_json_output, substitute_template,
    locked_file, get_logs_dir, load_ratings, get_active_bots,
    _trim_to_budget, RESULTS_DIR, PROMPTS_DIR,
    MATCH_HISTORY_FILE, REPLAY_DIR,
    MAX_MASTER_RETRIES,
)

from replay_analysis import summarize_replay_for_analysis  # noqa: F401 — re-exported via evolution_core


# C-class sentinel: returned by _analyze_recent_matches /
# _run_performance_verification when their LLM call hit an infrastructure
# error (ClaudeSDKError / timeout / connection). Detected here so the Master
# prompt surfaces "analysis unavailable due to LLM failure" rather than the
# misleading "No data available" (which would imply the daemon hadn't run).
LLM_INFRA_SENTINEL = "[LLM_INFRA_ERROR: analysis unavailable]"
LLM_INFRA_SENTINEL_MSG = (
    "⚠ Analysis unavailable: the LLM analyst crashed with an infrastructure "
    "error (NOT a business judgement). Treat conclusions in this section as "
    "missing rather than negative — the daemon data still exists, only the "
    "LLM interpretation failed."
)


def _render_analysis_section(text: str, default_msg: str) -> str:
    """Map an analyst's raw return into the text injected into the Master prompt.

    - Empty/None -> default "no data" message (unchanged behaviour).
    - LLM_INFRA_SENTINEL -> explicit "LLM crashed" warning (so the Master does
      not misread a missing analysis as a negative business signal).
    - Anything else -> the actual analysis text.
    """
    if not text or not text.strip():
        return default_msg
    if text.strip() == LLM_INFRA_SENTINEL:
        return LLM_INFRA_SENTINEL_MSG
    return text


# ──────────────────────────────────────────────
# Master Analysis
# ──────────────────────────────────────────────

async def _run_master_analysis(source_v, next_v, stagnation_info, ui,
                               match_analysis="", performance_verification="",
                               replay_spotlight="", bot_action_stats="",
                               battle_experience="", exploitability_weaknesses=""):
    """Run Master analysis — can run concurrently with daemon evaluation."""
    master_prompt = (PROMPTS_DIR / "master_prompt.md").read_text()
    # Apply section budgets to avoid experience_pool crowding out match_analysis.
    # C-class: render the sentinel (returned when the analyst LLM crashed on an
    # infrastructure error) into an explicit warning BEFORE trimming, so the
    # Master sees "LLM crashed" rather than "no data" (which would be read as a
    # negative business signal). Non-sentinel text passes through unchanged.
    match_analysis_rendered = _render_analysis_section(
        match_analysis, "",
    )
    perf_rendered = _render_analysis_section(
        performance_verification, "No performance verification data available.",
    )
    match_analysis_trimmed = _trim_to_budget(match_analysis_rendered, 10_000, tail=True)
    perf_trimmed = _trim_to_budget(perf_rendered, 4_000)

    # Build eval round summary BEFORE substitute_template so it's included in one pass
    eval_round_summary = "No eval round data available yet."
    try:
        from eval_rounds import EvalRoundManager
        _erm = EvalRoundManager()
        _eval_summary = _erm.get_last_round_summary(f"claude_v{source_v}")
        if _eval_summary:
            eval_round_summary = _eval_summary
    except Exception:
        pass

    master_prompt = substitute_template(master_prompt, {
        "stagnation_info": stagnation_info,
        "match_analysis": match_analysis_trimmed,
        "performance_verification": perf_trimmed,
        "source_v": str(source_v),
        "replay_spotlight": replay_spotlight or "No replay spotlight data available.",
        "bot_action_stats": bot_action_stats or "No bot action statistics available.",
        "eval_round_summary": eval_round_summary,
        "battle_experience": battle_experience or "No battle experience data available yet.",
        "exploitability_weaknesses": exploitability_weaknesses or "No exploitability probe data available yet.",
    })
    master_ctx = (
        f"Current evolution: v{source_v} → v{next_v}\n"
        f"Bot directory: bots/claude_v{source_v}/\n"
        f"Ratings file: web/core/results/glicko_ratings.json\n"
        f"Rating history: web/core/results/rating_history.jsonl\n"
        f"Head-to-Head data: web/core/results/head_to_head.json\n"
        f"Bot stats: web/core/results/bot_stats.json\n"
        f"Experience pool: web/core/experience_pool.md  ← READ THIS, not evolution_workspace/experience_pool.md\n"
    )
    master_log_file = get_logs_dir(next_v) / "master_io.txt"

    for attempt in range(MAX_MASTER_RETRIES):
        ui.clear_io()
        output, _, _ = await run_claude_query(
            master_prompt + "\n" + master_ctx, [], ui,
            f"MASTER (Try {attempt+1})", master_log_file,
            tools=["Bash", "Read"],
        )
        data = parse_json_output(output)
        if data and "tasks" in data:
            from output_schema import validate_agent_output
            data, errors = validate_agent_output("master", data)
            if errors:
                ui.log_history(f"Master plan validation issues: {'; '.join(errors[:3])}", "warn")
            ui.log_history("Master analysis complete.", "success")
            return data
        ui.log_history("Master output malformed JSON. Retrying...", "warn")
        import asyncio
        await asyncio.sleep(2)

    ui.log_history(f"Master failed to plan after {MAX_MASTER_RETRIES} retries.", "error")
    return None


# ──────────────────────────────────────────────
# Match Analysis
# ──────────────────────────────────────────────

async def _analyze_recent_matches(source_v, ui, max_matches=8):
    """Use LLM to analyze recent replay data for the current bot.

    Collects both recent losses and close wins (margin < 3 games) to give
    the Master a balanced view of weaknesses and what's working.

    Returns a match analysis string to inject into Master's context, or ""
    if no replay data is available.
    """
    bot_name = f"claude_v{source_v}"

    if not MATCH_HISTORY_FILE.exists():
        return ""

    recent_losses = []
    close_wins = []

    with locked_file(MATCH_HISTORY_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            b0, b1 = entry.get("bot0"), entry.get("bot1")
            w0, w1 = entry.get("bot0_wins", 0), entry.get("bot1_wins", 0)

            if b0 == bot_name:
                bot_wins, opp_wins = w0, w1
            elif b1 == bot_name:
                bot_wins, opp_wins = w1, w0
            else:
                continue

            if opp_wins > bot_wins:
                recent_losses.append(entry)
            elif bot_wins > opp_wins and (bot_wins - opp_wins) <= 2:
                # Close win (margin ≤ 2 games) — reveals near-miss vulnerabilities
                close_wins.append(entry)

    if not recent_losses and not close_wins:
        return ""

    recent_losses = recent_losses[-max_matches:]
    close_wins = close_wins[-(max_matches // 2):]

    def _load_summaries(entries, label):
        result = []
        for entry in entries:
            replay_path = REPLAY_DIR / entry["id"]
            if not replay_path.exists():
                continue
            try:
                with locked_file(replay_path, "r") as rf:
                    replay_data = json.load(rf)
                summary = summarize_replay_for_analysis(replay_data, bot_name)
                if summary:
                    result.append(f"[{label}] {summary}")
            except (json.JSONDecodeError, OSError):
                continue
        return result

    summaries = _load_summaries(recent_losses, "LOSS") + _load_summaries(close_wins, "CLOSE WIN")

    if not summaries:
        return ""

    # Load template and substitute
    template_file = PROMPTS_DIR / "match_analyst.md"
    if not template_file.exists():
        return ""
    match_analyst_prompt = template_file.read_text()
    match_analyst_prompt = substitute_template(match_analyst_prompt, {
        "match_summaries": "\n\n".join(summaries),
    })

    log_file = get_logs_dir(source_v) / "match_analyst_io.txt"
    try:
        output, _, _ = await run_claude_query(
            match_analyst_prompt, [], ui,
            "MATCH ANALYST", log_file,
        )
        if not output or not output.strip():
            # Retry once if match analyst returned empty (529/timeout)
            output, _, _ = await run_claude_query(
                match_analyst_prompt, [], ui,
                "MATCH ANALYST (retry)", log_file,
            )
        return output or ""
    except Exception as e:
        # C-class: distinguish LLM infrastructure crash from "no data".
        # Return a sentinel string so the Master prompt builder can surface
        # "analysis unavailable due to LLM failure" instead of the misleading
        # "No match analysis data available". Return type stays str for compat.
        from llm_failure import is_llm_infra_error
        if is_llm_infra_error(e):
            ui.log_history(f"Match analysis LLM infrastructure error: {e}", "warn")
            from system_log import log_system_event
            log_system_event("pipeline.match_analyst_infra", "warn",
                             f"Match analyst v{source_v} LLM crashed (infra): {e}",
                             {"source_v": source_v, "error": str(e)})
            return "[LLM_INFRA_ERROR: analysis unavailable]"
        ui.log_history(f"Match analysis failed: {e}", "warn")
        return ""
