"""Battle Experience — incremental match analysis via background thread.

Per-match deterministic tagging + serial background thread that consumes
unanalyzed matches one by one via LLM, maintaining a single
battle_experience.md file.

The thread wakes every POLL_INTERVAL seconds, finds unanalyzed matches in
match_history.jsonl, loads their replay files, summarizes them from both
perspectives, and feeds the summaries to an LLM that incrementally updates
the experience file.

All file I/O uses fcntl locking.  LLM failures are non-fatal — the thread
breaks out of the current batch and retries next cycle.
"""

import asyncio
import json
import logging
import os
import threading
import time
from pathlib import Path

from evolution_infra import (
    BaseUI,
    RESULTS_DIR,
    REPLAY_DIR,
    PROMPTS_DIR,
    MATCH_HISTORY_FILE,
    LLM_COSTS_FILE,
    read_locked_json,
    write_locked_json,
    append_locked_jsonl,
    locked_file,
    substitute_template,
)
import replay_analysis

log = logging.getLogger("pok.battle_exp")

# ──────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────

BATTLE_EXPERIENCE_FILE = RESULTS_DIR / "battle_experience.md"
ANALYSIS_MARKER_FILE = RESULTS_DIR / ".battle_analysis_progress.json"
POLL_INTERVAL = 60  # seconds between background thread wake-ups

# ──────────────────────────────────────────────
# SilentUI
# ──────────────────────────────────────────────


class SilentUI(BaseUI):
    """Minimal BaseUI subclass for background-thread LLM calls.

    All methods are no-op except update_cost(), which appends cost entries
    to llm_costs.jsonl using append_locked_jsonl.
    """

    def update_cost(self, role, cost_usd, usage):
        if cost_usd is None:
            return
        in_tok = usage.get("input_tokens", 0) if usage else 0
        out_tok = usage.get("output_tokens", 0) if usage else 0
        try:
            entry = {
                "role": role,
                "cost_usd": cost_usd,
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "ts": time.time(),
            }
            append_locked_jsonl(LLM_COSTS_FILE, entry)
        except OSError as e:
            log.warning("SilentUI cost write failed: %s", e)


# ──────────────────────────────────────────────
# Match tagging
# ──────────────────────────────────────────────


def is_analyzed(match_id: str) -> bool:
    """Check whether a match ID has already been analyzed."""
    markers = read_locked_json(ANALYSIS_MARKER_FILE, default=None)
    if not markers:
        return False
    return match_id in markers


def mark_analyzed(match_id: str):
    """Record a match ID as analyzed.  Atomic read-merge-write under lock."""
    markers = read_locked_json(ANALYSIS_MARKER_FILE, default=None)
    if markers is None:
        markers = set()
    else:
        markers = set(markers)
    markers.add(match_id)
    write_locked_json(ANALYSIS_MARKER_FILE, sorted(markers))


def get_unanalyzed_matches(n: int = 5) -> list[dict]:
    """Return up to *n* unanalyzed match entries from match_history.jsonl.

    Reads the tail of the JSONL file, filters out already-analyzed IDs,
    and returns the most recent unanalyzed entries (up to *n*).
    """
    if not MATCH_HISTORY_FILE.exists():
        return []

    markers = read_locked_json(ANALYSIS_MARKER_FILE, default=None)
    analyzed_ids = set(markers) if markers else set()

    try:
        with locked_file(MATCH_HISTORY_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except (OSError, UnicodeDecodeError):
        return []

    # Scan from the end for most-recent unanalyzed matches
    unanalyzed = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("id") not in analyzed_ids:
            unanalyzed.append(entry)
            if len(unanalyzed) >= n:
                break

    # Return in chronological order (oldest first)
    unanalyzed.reverse()
    return unanalyzed


# ──────────────────────────────────────────────
# Background thread
# ──────────────────────────────────────────────

_thread: threading.Thread | None = None


def start_experience_thread():
    """Start the serial background thread.  Called once at daemon startup."""
    global _thread
    if _thread is not None and _thread.is_alive():
        log.info("Battle experience thread already running")
        return
    _thread = threading.Thread(target=_experience_loop, daemon=True, name="battle-experience")
    _thread.start()
    log.info("Battle experience thread started (interval=%ds)", POLL_INTERVAL)


def _experience_loop():
    """Serial background loop.  Wakes every POLL_INTERVAL seconds.

    Finds unanalyzed matches and processes them ONE AT A TIME serially.
    On any processing error the loop breaks out of the current batch and
    retries next cycle.
    """
    while True:
        try:
            time.sleep(POLL_INTERVAL)
            unanalyzed = get_unanalyzed_matches(n=5)
            if not unanalyzed:
                continue
            for entry in unanalyzed:
                try:
                    _process_one_match(entry)
                except Exception as e:
                    log.warning(
                        "Battle experience processing failed for %s: %s",
                        entry.get("id", "?"), e,
                    )
                    break  # Stop processing on error, retry next cycle
        except Exception as e:
            log.warning("Experience thread error: %s", e)


# ──────────────────────────────────────────────
# Per-match processing
# ──────────────────────────────────────────────


def _process_one_match(entry: dict):
    """Process a single match entry through the LLM update pipeline.

    Steps:
      1. Load the replay file from REPLAY_DIR using the entry ID.
      2. If missing, mark as analyzed (skip) and return.
      3. Summarize from BOTH bot perspectives.
      4. Read current experience file.
      5. Run LLM to produce updated experience.
      6. Write updated content atomically.
      7. Mark the match as analyzed.
    """
    match_id = entry.get("id", "")
    bot0 = entry.get("bot0", "")
    bot1 = entry.get("bot1", "")

    # 1. Load replay
    replay_path = REPLAY_DIR / match_id
    if not replay_path.exists():
        log.debug("Replay file missing for %s — marking as analyzed", match_id)
        mark_analyzed(match_id)
        return

    try:
        with locked_file(replay_path, "r", encoding="utf-8") as f:
            replay_data = json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        log.warning("Failed to read replay %s: %s — skipping", match_id, e)
        mark_analyzed(match_id)
        return

    # 3. Summarize from both perspectives
    summary_parts = []
    for bot_name in (bot0, bot1):
        if not bot_name:
            continue
        summary = replay_analysis.summarize_replay_for_analysis(replay_data, bot_name)
        if summary:
            summary_parts.append(summary)

    if not summary_parts:
        log.debug("Empty summaries for %s — marking as analyzed", match_id)
        mark_analyzed(match_id)
        return

    new_match_summary = "\n\n".join(summary_parts)

    # 4. Read current experience
    current_experience = _read_experience_file()

    # 5-6. Run LLM update
    updated = _run_llm_update(current_experience, new_match_summary)
    if updated is not None:
        _write_experience_file(updated)

    # 7. Mark analyzed
    mark_analyzed(match_id)


# ──────────────────────────────────────────────
# LLM call
# ──────────────────────────────────────────────


def _run_llm_update(current_experience: str, new_match_data: str) -> str | None:
    """Send current experience + new match data to LLM, get updated experience.

    Returns the updated markdown content, or None on failure (caller keeps
    the existing file unchanged).
    """
    prompt_template_path = PROMPTS_DIR / "battle_experience_update.md"
    if not prompt_template_path.exists():
        log.warning("Prompt template %s not found — skipping LLM update", prompt_template_path)
        return None

    try:
        template = prompt_template_path.read_text(encoding="utf-8")
    except OSError as e:
        log.warning("Failed to read prompt template: %s", e)
        return None

    prompt = substitute_template(template, {
        "current_experience": current_experience or "(empty — first analysis)",
        "new_match_data": new_match_data,
    })

    output = _run_sync_llm_call(prompt)
    if output is None:
        return None

    # Return the LLM output as-is (should be markdown)
    # If the output is empty or suspiciously short, keep existing
    stripped = output.strip()
    if len(stripped) < 20:
        log.warning("LLM returned very short output (%d chars) — keeping existing", len(stripped))
        return None

    return stripped


def _run_sync_llm_call(prompt: str) -> str | None:
    """Run run_claude_query in a new thread with its own event loop.

    Returns the text output, or None on any failure.
    30-second timeout on the synchronous wrapper.
    """
    result = [None]  # mutable container for thread communication

    def _worker():
        try:
            ui = SilentUI()
            log_path = RESULTS_DIR / "battle_exp_llm.log"

            async def _async_call():
                from llm_query import run_claude_query
                output, cost_usd, usage = await run_claude_query(
                    prompt=prompt,
                    context_files=[],
                    ui=ui,
                    role_name="battle_experience",
                    log_file_path=str(log_path),
                    model="sonnet",
                    tools=None,
                )
                return output

            output = asyncio.run(_async_call())
            result[0] = output
        except Exception as e:
            log.warning("Sync LLM call failed: %s", e)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=30)

    if t.is_alive():
        log.warning("LLM call timed out after 30s — skipping update")
        return None

    return result[0]


# ──────────────────────────────────────────────
# File I/O helpers
# ──────────────────────────────────────────────


def _read_experience_file() -> str:
    """Read the current battle_experience.md content.  Returns '' if absent."""
    if not BATTLE_EXPERIENCE_FILE.exists():
        return ""
    try:
        with locked_file(BATTLE_EXPERIENCE_FILE, "r", encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return ""


def _write_experience_file(content: str):
    """Write the battle_experience.md file atomically (tmp + rename)."""
    import fcntl
    os.makedirs(RESULTS_DIR, exist_ok=True)
    tmp = BATTLE_EXPERIENCE_FILE.with_suffix(".md.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        # Atomic rename under exclusive lock on the target file
        with locked_file(BATTLE_EXPERIENCE_FILE, "w", encoding="utf-8",
                         lock_type=fcntl.LOCK_EX) as _guard:
            os.replace(str(tmp), str(BATTLE_EXPERIENCE_FILE))
    except OSError as e:
        log.warning("Failed to write battle experience file: %s", e)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


# ──────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────


def get_battle_experience() -> str:
    """Return the current battle experience content.

    Called from generation_scheduler at generation start.
    No LLM call — just reads the file.
    """
    return _read_experience_file()
