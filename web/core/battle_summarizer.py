"""Background LLM summarizer for battle insights.

Runs a daemon thread inside the elo_daemon process. Every BATCH_INTERVAL
save cycles (~100 games), the thread snapshots H2H / match-history data,
calls the LLM for pattern analysis, and appends structured insights to a
JSONL file.  request_summary() is the non-blocking entry point called from
save_cycle(); it returns in microseconds after putting a dict in a queue.

Level-2 API: synthesize_battle_insights() is async, called from
generation_scheduler to fold accumulated insights into Master context.
"""

import asyncio
import json
import logging
import os
import queue
import threading
import time
from pathlib import Path

from evolution_infra import (
    RESULTS_DIR, PROMPTS_DIR, MATCH_HISTORY_FILE, REPLAY_DIR,
    locked_file, append_locked_jsonl, read_locked_json, substitute_template,
    BaseUI, pair_key,
)
from replay_analysis import summarize_replay_for_analysis
from llm_query import run_claude_query, parse_json_output

log = logging.getLogger("pok.summarizer")

# ──────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────

BATCH_INTERVAL = 5           # trigger every 5 save_cycles (~100 games)
MAX_INSIGHTS_LINES = 200     # JSONL rotation cap
INSIGHTS_FILE = RESULTS_DIR / "battle_insights.jsonl"


# ──────────────────────────────────────────────
# SilentUI — no-op BaseUI except cost tracking
# ──────────────────────────────────────────────

class SilentUI(BaseUI):
    """Minimal BaseUI for the summarizer thread.

    All UI methods are no-ops.  update_cost() writes to llm_costs.jsonl so
    that daemon-initiated LLM costs are tracked alongside orchestrator costs.
    """

    def __init__(self):
        self._log_file = RESULTS_DIR / "battle_summarizer_io.txt"

    # ── no-op UI methods ──

    def log_history(self, msg, status="info"):
        pass

    def set_status(self, msg, is_working=False):
        pass

    def log_io(self, msg, stream_type="default", role=""):
        pass

    def clear_io(self):
        pass

    def update_eval_table(self, ratings, active_bots):
        pass

    def update_daemon_status(self, stats, ratings):
        pass

    def set_header(self, msg):
        pass

    def update_metrics(self, metrics):
        pass

    def emit_tool_call(self, tool_name, args, role=""):
        pass

    def update_cost(self, role, cost_usd, usage):
        if cost_usd is None:
            return
        try:
            append_locked_jsonl(RESULTS_DIR / "llm_costs.jsonl", {
                "role": role,
                "cost_usd": cost_usd,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "source": "daemon",
            })
        except Exception as e:
            log.warning("Failed to append LLM cost: %s", e)


# ──────────────────────────────────────────────
# Background thread infrastructure
# ──────────────────────────────────────────────

_summary_queue: queue.Queue = queue.Queue()
_summary_thread: threading.Thread | None = None


def start_summary_thread():
    """Start the background summarizer daemon thread (idempotent)."""
    global _summary_thread
    if _summary_thread is not None and _summary_thread.is_alive():
        return
    _summary_thread = threading.Thread(
        target=_summarizer_loop,
        daemon=True,
        name="battle-summarizer",
    )
    _summary_thread.start()
    log.info("Battle summarizer thread started")


def stop_summary_thread():
    """Signal the summarizer thread to exit and wait briefly."""
    global _summary_thread
    if _summary_thread is None:
        return
    _summary_queue.put(None)  # poison pill
    _summary_thread.join(timeout=5)
    _summary_thread = None
    log.info("Battle summarizer thread stopped")


def _summarizer_loop():
    """Main loop of the background summarizer thread."""
    while True:
        try:
            request = _summary_queue.get(timeout=60)
            if request is None:
                break
            _do_summarize(request)
        except Exception as e:
            log.warning("Summarizer thread error: %s", e)


def request_summary(save_num, active_bots, h2h, bot_stats):
    """Non-blocking entry point called from save_cycle.

    Snapshots data and puts it in the queue.  Returns immediately.
    Only triggers every BATCH_INTERVAL save cycles.
    """
    if save_num % BATCH_INTERVAL != 0:
        return
    # Snapshot dicts to avoid races with daemon's next update
    try:
        _summary_queue.put({
            "save_num": save_num,
            "active_bots": list(active_bots),
            "h2h": {k: dict(v) for k, v in h2h.items()},
            "bot_stats": {k: dict(v) for k, v in bot_stats.items()},
        })
    except Exception as e:
        log.warning("request_summary queue put failed: %s", e)


# ──────────────────────────────────────────────
# Core summarization pipeline
# ──────────────────────────────────────────────

def _do_summarize(request):
    """Process one summarization request (runs in background thread)."""
    try:
        save_num = request["save_num"]
        active_bots = request["active_bots"]
        h2h = request["h2h"]
        bot_stats = request["bot_stats"]

        batch_inputs, h2h_context = _prepare_batch_input(active_bots, h2h, bot_stats)
        if not batch_inputs:
            log.debug("No batch inputs for summarization (save #%d)", save_num)
            return

        prompt = _build_prompt(batch_inputs, h2h_context)
        raw_output = _run_sync_llm_call(prompt)
        if not raw_output:
            log.warning("LLM call returned empty for battle summary (save #%d)", save_num)
            return

        _parse_and_write_insights(raw_output, save_num)
        log.info("Battle insights written (save #%d)", save_num)
    except Exception as e:
        log.warning("_do_summarize failed: %s", e)


def _prepare_batch_input(active_bots, h2h, bot_stats):
    """Read recent match history and replay files; return (batch_inputs, h2h_context).

    Returns:
        batch_inputs: list of dicts with 'pair' and 'summaries' keys
        h2h_context: formatted string of H2H win rates for active pairs
    """
    batch_inputs = []

    # Read last 50 entries from match history
    if not MATCH_HISTORY_FILE.exists():
        return batch_inputs, ""

    recent_entries = []
    try:
        with locked_file(MATCH_HISTORY_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    recent_entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        log.warning("Failed to read match history: %s", e)
        return batch_inputs, ""

    recent_entries = recent_entries[-50:]

    # Group by sorted pair
    from collections import defaultdict
    groups = defaultdict(list)
    for entry in recent_entries:
        b0 = entry.get("bot0", "")
        b1 = entry.get("bot1", "")
        if not b0 or not b1:
            continue
        key = tuple(sorted([b0, b1]))
        groups[key].append(entry)

    # For each group: compute aggregated win rate, load up to 3 replay summaries
    for (bot_a, bot_b), entries in groups.items():
        summaries = []
        # Aggregate win rate
        w_a = sum(e.get("bot0_wins", 0) if e.get("bot0") == bot_a else e.get("bot1_wins", 0) for e in entries)
        w_b = sum(e.get("bot1_wins", 0) if e.get("bot1") == bot_b else e.get("bot0_wins", 0) for e in entries)
        total = w_a + w_b
        wr_a = round(w_a / total, 3) if total > 0 else 0.5
        summaries.append(
            f"Pair {bot_a} vs {bot_b}: {len(entries)} recent matches, "
            f"{bot_a} win_rate={wr_a:.1%} ({w_a}W/{w_b}L/{total} total games)"
        )

        # Load up to 3 replay files from this group
        replays_loaded = 0
        for entry in reversed(entries):
            if replays_loaded >= 3:
                break
            replay_id = entry.get("id")
            if not replay_id:
                continue
            replay_path = REPLAY_DIR / replay_id
            if not replay_path.exists():
                continue
            try:
                with locked_file(replay_path, "r") as rf:
                    replay_data = json.load(rf)
                # Summarize from both perspectives
                for bot_name in (bot_a, bot_b):
                    s = summarize_replay_for_analysis(replay_data, bot_name)
                    if s:
                        summaries.append(s)
                replays_loaded += 1
            except Exception:
                continue

        batch_inputs.append({
            "pair": (bot_a, bot_b),
            "summaries": summaries,
        })

    # Build H2H context string
    h2h_lines = []
    for a in active_bots:
        for b in active_bots:
            if a >= b:
                continue
            k = pair_key(a, b)
            entry = h2h.get(k)
            if not entry:
                continue
            g = entry.get("games", 0)
            wr = entry.get("a_wins", 0) / g if g > 0 else 0.5
            h2h_lines.append(f"  {a} vs {b}: {wr:.1%} ({g} games)")
    h2h_context = "\n".join(h2h_lines) if h2h_lines else "No H2H data available"

    return batch_inputs, h2h_context


def _build_prompt(batch_inputs, h2h_context):
    """Load prompt template and substitute batch data."""
    template_path = PROMPTS_DIR / "battle_summarizer.md"
    if not template_path.exists():
        log.warning("battle_summarizer.md prompt template not found")
        return ""

    template = template_path.read_text()

    pair_summaries = "\n\n".join(
        "\n".join(bi["summaries"]) for bi in batch_inputs
    )

    prompt = substitute_template(template, {
        "pair_summaries": pair_summaries,
        "h2h_context": h2h_context,
    })
    return prompt


def _run_sync_llm_call(prompt):
    """Run an LLM call synchronously (blocking) from the summarizer thread.

    Creates a new event loop, runs with 30s timeout.
    Returns output text or None on failure.
    """
    if not prompt:
        return None
    try:
        return asyncio.run(asyncio.wait_for(_async_llm_call(prompt), timeout=30))
    except asyncio.TimeoutError:
        log.warning("LLM call timed out (30s)")
        return None
    except Exception as e:
        log.warning("LLM call failed: %s", e)
        return None


async def _async_llm_call(prompt):
    """Async LLM call using SilentUI for cost tracking."""
    ui = SilentUI()
    output, _, _ = await run_claude_query(
        prompt, [], ui, "BATTLE_SUMMARIZER", ui._log_file,
    )
    return output


# ──────────────────────────────────────────────
# Insight persistence & rotation
# ──────────────────────────────────────────────

def _parse_and_write_insights(raw_output, save_num):
    """Parse LLM JSON output and append insights to JSONL file."""
    data = parse_json_output(raw_output)
    if not data or not isinstance(data, dict):
        log.warning("Could not parse battle insights JSON")
        return

    insights = data.get("insights")
    if not insights or not isinstance(insights, list):
        log.warning("No 'insights' array in battle summarizer output")
        return

    timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    for insight in insights:
        if not isinstance(insight, dict):
            continue
        enriched = {
            "theme": insight.get("theme", ""),
            "pairs_affected": insight.get("pairs_affected", []),
            "pattern": insight.get("pattern", ""),
            "evidence": insight.get("evidence", ""),
            "recommendation": insight.get("recommendation", ""),
            "timestamp": timestamp,
            "save_num": save_num,
            "source": "daemon",
        }
        try:
            append_locked_jsonl(INSIGHTS_FILE, enriched)
        except Exception as e:
            log.warning("Failed to write insight: %s", e)

    _rotate_insights_file()


def _rotate_insights_file():
    """Keep INSIGHTS_FILE under MAX_INSIGHTS_LINES by dropping oldest entries."""
    if not INSIGHTS_FILE.exists():
        return
    try:
        with locked_file(INSIGHTS_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return

    if len(lines) <= MAX_INSIGHTS_LINES:
        return

    trimmed = lines[-MAX_INSIGHTS_LINES:]
    try:
        with locked_file(INSIGHTS_FILE, "w", encoding="utf-8") as f:
            f.writelines(trimmed)
    except Exception as e:
        log.warning("Failed to rotate insights file: %s", e)


# ──────────────────────────────────────────────
# Level-2: synthesize accumulated insights
# ──────────────────────────────────────────────

async def synthesize_battle_insights(ui=None):
    """Level-2 async function called from generation_scheduler.

    Reads the last 30 insight entries, deduplicates by theme, and calls
    the LLM to synthesize a cohesive strategic summary for Master context.

    Returns the synthesis text, or "" on failure.
    """
    if not INSIGHTS_FILE.exists():
        return ""

    # Read last 30 entries
    entries = []
    try:
        with locked_file(INSIGHTS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        log.warning("Failed to read insights file: %s", e)
        return ""

    entries = entries[-30:]
    if not entries:
        return ""

    # Deduplicate by theme (keep most recent per theme)
    seen_themes = {}
    for entry in reversed(entries):
        theme = entry.get("theme", "")
        if theme and theme not in seen_themes:
            seen_themes[theme] = entry

    # Build accumulated insights string
    deduped = list(reversed(seen_themes.values()))
    accumulated_lines = []
    for i, entry in enumerate(deduped, 1):
        accumulated_lines.append(
            f"{i}. [{entry.get('timestamp', '?')}] Theme: {entry.get('theme', '?')}\n"
            f"   Pattern: {entry.get('pattern', '')}\n"
            f"   Evidence: {entry.get('evidence', '')}\n"
            f"   Pairs: {', '.join(entry.get('pairs_affected', []))}\n"
            f"   Recommendation: {entry.get('recommendation', '')}"
        )
    accumulated_insights = "\n\n".join(accumulated_lines)

    # Load synthesis template
    template_path = PROMPTS_DIR / "battle_insights_synthesis.md"
    if not template_path.exists():
        log.warning("battle_insights_synthesis.md template not found")
        return accumulated_insights  # fallback: raw insights

    template = template_path.read_text()
    prompt = substitute_template(template, {
        "accumulated_insights": accumulated_insights,
    })

    if ui is None:
        ui = SilentUI()

    log_file = RESULTS_DIR / "battle_summarizer_io.txt"
    try:
        output, _, _ = await run_claude_query(
            prompt, [], ui, "INSIGHTS_SYNTHESIS", log_file,
        )
        return output or ""
    except Exception as e:
        log.warning("synthesize_battle_insights LLM call failed: %s", e)
        return accumulated_insights  # fallback: raw insights
