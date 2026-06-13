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
POLL_INTERVAL = 20  # seconds between background thread wake-ups
TARGET_BATCH = 16  # matches per wake-up
MAX_CONCURRENT_LLM = 6  # parallel LLM calls within one batch
MAX_ANALYSES_PER_HOUR = 240  # rate-limit defense (non-zero budget ~$5/hr)
LLM_TIMEOUT = 120  # seconds per LLM update call

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
    """True if the match is DONE (success, fail_count==0) or force-skipped (>=3).

    Transient failures (fail_count 1-2) return False so they get RETRIED.
    """
    markers = _read_markers()
    entry = markers.get(match_id)
    if entry is None:
        return False
    if isinstance(entry, dict):
        fc = entry.get("fail_count", 0)
        return fc == 0 or fc >= 3
    # legacy list form: plain string ID — treated as analyzed
    return True


def _read_markers() -> dict:
    """Read marker file, normalizing legacy list format to dict."""
    raw = read_locked_json(ANALYSIS_MARKER_FILE, default=None)
    if raw is None:
        return {}
    if isinstance(raw, list):
        # Legacy format: list of IDs (all done) — convert to dict.
        return {mid: {} for mid in raw}
    if isinstance(raw, dict):
        return raw
    return {}


def _write_markers(markers: dict):
    """Write marker dict atomically under lock."""
    write_locked_json(ANALYSIS_MARKER_FILE, markers)


def mark_analyzed(match_id: str, *, fail_count: int = 0):
    """Record a match ID as analyzed (done). Atomic read-merge-write under lock.

    Args:
        match_id: the match ID to mark.
        fail_count: 0 = successfully analyzed (done); >=3 = force-skipped poison.
    """
    markers = _read_markers()
    markers[match_id] = {"fail_count": fail_count}
    _write_markers(markers)


def increment_fail_count(match_id: str) -> int:
    """Bump fail_count for a match ID, return new count. Stays retryable until 3."""
    markers = _read_markers()
    entry = markers.get(match_id, {})
    if not isinstance(entry, dict):
        entry = {}
    new_count = entry.get("fail_count", 0) + 1
    markers[match_id] = {"fail_count": new_count}
    _write_markers(markers)
    return new_count


def get_unanalyzed_matches(n: int = TARGET_BATCH) -> list[dict]:
    """Return up to *n* match entries to analyze from match_history.jsonl.

    Includes: never-tried matches (no marker) AND transient failures (fail_count
    1-2, retried). Excludes: successfully analyzed (fail_count==0), force-skipped
    poison (fail_count>=3), legacy markers, and IDs whose replay file was evicted.

    Random-samples from the candidate pool to avoid recency bias, returns the
    selected entries in chronological order (oldest first).
    """
    if not MATCH_HISTORY_FILE.exists():
        return []

    markers = _read_markers()

    try:
        with locked_file(MATCH_HISTORY_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except (OSError, UnicodeDecodeError):
        return []

    candidates = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        match_id = entry.get("id", "")
        if not match_id:
            continue
        marker = markers.get(match_id)
        if isinstance(marker, dict):
            fc = marker.get("fail_count", 0)
            if fc == 0 or fc >= 3:
                continue  # done or force-skipped
            # fc in 1-2: transient failure — retry (fall through)
        elif marker is not None:
            continue  # legacy string form — already analyzed
        # Skip if replay file has been evicted
        replay_path = REPLAY_DIR / match_id
        if not replay_path.exists():
            continue
        candidates.append(entry)

    if not candidates:
        return []

    # Random sample to avoid recency bias, then sort chronologically
    import random
    if len(candidates) > n:
        selected = random.sample(candidates, n)
    else:
        selected = candidates
    selected.sort(key=lambda e: e.get("timestamp", e.get("id", "")))
    return selected


# ──────────────────────────────────────────────
# Background thread
# ──────────────────────────────────────────────

_thread: threading.Thread | None = None


def start_experience_thread():
    """Start the background experience thread.  Called once at daemon startup."""
    global _thread
    if _thread is not None and _thread.is_alive():
        log.info("Battle experience thread already running")
        return
    _thread = threading.Thread(target=_experience_loop, daemon=True, name="battle-experience")
    _thread.start()
    log.info(
        "Battle experience thread started (interval=%ds, batch=%d, concurrent=%d)",
        POLL_INTERVAL, TARGET_BATCH, MAX_CONCURRENT_LLM,
    )


def _experience_loop():
    """Background loop: wakes every POLL_INTERVAL, processes a batch.

    Uses a ThreadPoolExecutor for parallel LLM calls within the batch.
    Per-match errors are isolated — one failure does not abort the batch.
    Writes are serialized to avoid race conditions.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    _analyses_this_hour = 0
    _hour_start = time.time()

    while True:
        try:
            time.sleep(POLL_INTERVAL)

            # Budget window: reset hourly counter
            now = time.time()
            if now - _hour_start >= 3600:
                _analyses_this_hour = 0
                _hour_start = now

            # Rate-limit defense
            remaining_budget = MAX_ANALYSES_PER_HOUR - _analyses_this_hour
            if remaining_budget <= 0:
                log.debug("Hourly analysis budget exhausted (%d/%d) — skipping cycle",
                          _analyses_this_hour, MAX_ANALYSES_PER_HOUR)
                continue

            batch_size = min(TARGET_BATCH, remaining_budget)
            unanalyzed = get_unanalyzed_matches(n=batch_size)
            if not unanalyzed:
                continue

            # Extract per-match summaries in parallel (pure-data, parallel-safe).
            # The LLM merge is done ONCE over the combined batch in
            # _apply_batch_results (avoids the read-modify-write data-loss bug
            # where each worker read the same stale baseline and sequential
            # writes clobbered N-1 of the merges).
            results = []  # list of (entry, success_bool, summary_or_None)
            with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_LLM) as pool:
                future_map = {
                    pool.submit(_process_one_match_safe, entry): entry
                    for entry in unanalyzed
                }
                for fut in as_completed(future_map):
                    entry = future_map[fut]
                    match_id = entry.get("id", "?")
                    try:
                        summary = fut.result(timeout=60)
                        results.append((entry, True, summary))
                    except Exception as e:
                        log.warning("Battle experience summary failed for %s: %s", match_id, e)
                        results.append((entry, False, None))

            # Single cumulative LLM merge + write (serial, correct chaining).
            _apply_batch_results(results)

            # One LLM merge per cycle when any summaries were collected.
            if any(r[2] for r in results):
                _analyses_this_hour += 1

        except Exception as e:
            log.warning("Experience thread error: %s", e)


def _process_one_match_safe(entry: dict) -> str | None:
    """Extract the new-match summary for one match (pure-data, parallel-safe).

    Returns the concatenated bot-perspective summary string, or None if the
    replay is missing/corrupt/empty. Does NOT touch the experience file or run
    the LLM — the LLM merge is done ONCE over the combined batch in
    _apply_batch_results. This avoids the parallel read-modify-write data-loss
    bug where each worker would read the same stale baseline and the sequential
    writes would clobber N-1 of the merges.
    """
    match_id = entry.get("id", "")
    bot0 = entry.get("bot0", "")
    bot1 = entry.get("bot1", "")

    replay_path = REPLAY_DIR / match_id
    if not replay_path.exists():
        log.debug("Replay file missing for %s — will skip", match_id)
        return None

    try:
        with locked_file(replay_path, "r", encoding="utf-8") as f:
            replay_data = json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        log.warning("Failed to read replay %s: %s — skipping", match_id, e)
        return None

    summary_parts = []
    for bot_name in (bot0, bot1):
        if not bot_name:
            continue
        summary = replay_analysis.summarize_replay_for_analysis(replay_data, bot_name)
        if summary:
            summary_parts.append(summary)

    if not summary_parts:
        log.debug("Empty summaries for %s — will skip", match_id)
        return None

    return "\n\n".join(summary_parts)


def _apply_batch_results(results: list):
    """Apply a batch: ONE cumulative LLM merge over all successful summaries.

    All summaries from the parallel batch are concatenated and merged into the
    live experience file in a SINGLE _run_llm_update call. This is correct
    (chaining semantics preserved — every summary folds into the latest file)
    AND cheaper (~1 LLM call vs N). Successful matches are marked analyzed;
    failures bump fail_count (force-skip after 3).
    """
    summaries = []
    success_entries = []
    for entry, success, summary in results:
        match_id = entry.get("id", "")
        if not success or summary is None:
            fail_count = increment_fail_count(match_id)
            if fail_count >= 3:
                log.warning("Match %s force-skipped after %d failures", match_id, fail_count)
            continue
        summaries.append(summary)
        success_entries.append(entry)

    if not summaries:
        return

    combined = "\n\n---\n\n".join(summaries)
    current = _read_experience_file()
    updated = _run_llm_update(current, combined)
    if updated is not None:
        _write_experience_file(updated)
        for entry in success_entries:
            mark_analyzed(entry.get("id", ""), fail_count=0)
    else:
        # LLM merge failed: bump fail_count for every successful-summary match
        # so they stay retryable (and force-skip after 3 LLM failures).
        for entry in success_entries:
            increment_fail_count(entry.get("id", ""))


# ──────────────────────────────────────────────
# Per-match processing
# ──────────────────────────────────────────────


# ──────────────────────────────────────────────
# Per-match processing (legacy single-match path, kept for tests)
# ──────────────────────────────────────────────


def _process_one_match(entry: dict):
    """Process a single match entry through the LLM update pipeline (serial path).

    Marks analyzed ONLY on success (fail_count=0). On LLM failure, bumps
    fail_count and leaves the match retryable until 3 strikes force-skip it.
    Missing/empty replays are marked done (skip) since they cannot be analyzed.
    """
    match_id = entry.get("id", "")
    bot0 = entry.get("bot0", "")
    bot1 = entry.get("bot1", "")

    # 1. Load replay
    replay_path = REPLAY_DIR / match_id
    if not replay_path.exists():
        log.debug("Replay file missing for %s — marking as analyzed", match_id)
        mark_analyzed(match_id, fail_count=0)
        return

    try:
        with locked_file(replay_path, "r", encoding="utf-8") as f:
            replay_data = json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        log.warning("Failed to read replay %s: %s — skipping", match_id, e)
        mark_analyzed(match_id, fail_count=0)
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
        mark_analyzed(match_id, fail_count=0)
        return

    new_match_summary = "\n\n".join(summary_parts)

    # 4. Read current experience
    current_experience = _read_experience_file()

    # 5-6. Run LLM update
    updated = _run_llm_update(current_experience, new_match_summary)
    if updated is not None:
        _write_experience_file(updated)
        # 7. Mark analyzed ONLY on success
        mark_analyzed(match_id, fail_count=0)
    else:
        # LLM failure: bump fail_count, do NOT permanently drop data.
        # fail_count 1-2 stays retryable; 3 strikes force-skips the poison match.
        fail_count = increment_fail_count(match_id)
        if fail_count >= 3:
            log.warning("Match %s force-skipped after %d LLM failures", match_id, fail_count)
            mark_analyzed(match_id, fail_count=fail_count)


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
    """Run run_claude_query in this thread via a fresh event loop.

    Uses asyncio.wait_for for a cancellable timeout — when the timeout fires,
    the underlying task is cancelled (no leaked thread continuing to burn LLM
    quota, which the previous threading+join approach caused).

    Returns the text output, or None on any failure (including timeout).
    """
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

    try:
        return asyncio.run(asyncio.wait_for(_async_call(), timeout=LLM_TIMEOUT))
    except asyncio.TimeoutError:
        log.warning("LLM call timed out after %ds — skipping update", LLM_TIMEOUT)
        return None
    except Exception as e:
        log.warning("Sync LLM call failed: %s", e)
        return None


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
