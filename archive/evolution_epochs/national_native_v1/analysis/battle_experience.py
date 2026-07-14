"""Battle Experience — incremental match analysis via background thread.

Per-match deterministic tagging + serial background thread that consumes
unanalyzed matches, persists replay evidence first, and optionally asks an LLM
to turn accumulated summaries into reusable lessons.

The thread wakes every POLL_INTERVAL seconds, finds unanalyzed matches in
match_history.jsonl, loads their replay files, summarizes them from both
perspectives, and feeds the summaries to an LLM that incrementally updates
the legacy experience file plus structured battle_evidence / battle_lessons
sidecars.

All file I/O uses fcntl locking.  LLM failures are non-fatal — the thread
breaks out of the current batch and retries next cycle.

Cost optimizations (fix-10):
- Batch accumulation: matches are accumulated for MERGE_THRESHOLD turns
  before triggering an LLM call (~4x reduction from 15/hr to ~3-4/hr).
- Incremental append: LLM outputs are appended to the existing file rather
  than rewriting the entire document (~60% token reduction per call).
- Stale markers: observations about bot versions with no WR improvement
  across STALE_GEN_THRESHOLD generations are tagged [POSSIBLY STALE].
"""

import asyncio
import json
import logging
import os
import threading
import time
from pathlib import Path

import battle_memory
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
from llm_failure import is_llm_infra_error, is_success_error_result

log = logging.getLogger("pok.battle_exp")


def _classify_llm_error(e) -> str:
    """Return "infra" for LLM infrastructure errors (SDK/timeout/connection),
    "business" otherwise. Used for typed battle_exp telemetry."""
    if is_success_error_result(e):
        return "sdk_success_result"
    return "infra" if is_llm_infra_error(e) else "business"


def _llm_error_event_type(kind: str) -> str:
    if kind == "sdk_success_result":
        return "battle_exp.sdk_success_result"
    return f"battle_exp.{kind}_error"


def _llm_error_event_severity(kind: str) -> str:
    return "warn" if kind == "infra" else "info"


def _log_llm_failure(message: str, kind: str, exc) -> None:
    log_fn = log.info if kind == "sdk_success_result" else log.warning
    log_fn(message, kind, exc)

# ──────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────

BATTLE_EXPERIENCE_FILE = RESULTS_DIR / "battle_experience.md"
BATTLE_EVIDENCE_FILE = RESULTS_DIR / "battle_evidence.jsonl"
BATTLE_PENDING_SUMMARIES_FILE = RESULTS_DIR / "battle_pending_summaries.jsonl"
BATTLE_LESSONS_FILE = RESULTS_DIR / "battle_lessons.jsonl"
ANALYSIS_MARKER_FILE = RESULTS_DIR / ".battle_analysis_progress.json"
POLL_INTERVAL = 20  # seconds between background thread wake-ups
TARGET_BATCH = 6  # P2: was 16 — smaller batches cut per-call latency + SIGTERM data loss
MAX_CONCURRENT_LLM = 6  # concurrent replay-summary PARSERS within one batch (pure-data JSON, NO LLM). LLM merge is 1 serial call in _apply_batch_results → this常量不影响 gateway 503 (6→2 是误诊，已还原).
MAX_ANALYSES_PER_HOUR = 240  # rate-limit defense (non-zero budget ~$5/hr)
LLM_TIMEOUT = int(os.environ.get("POK_BATTLE_EXPERIENCE_LLM_TIMEOUT", "90"))
_LOG_ROTATION_LOCK = threading.Lock()  # P3: serialize battle_exp_llm.log rotation across the 6 concurrent workers
# fix-10: accumulation threshold — LLM merge fires only after this many
# match summaries have been accumulated across multiple poll cycles.
# At ~6 matches per poll cycle every 20s, this triggers after ~4 cycles
# (~1.3 min), cutting LLM calls from ~15/hr to ~3-4/hr.
MERGE_THRESHOLD = 24  # min accumulated summaries before triggering LLM
# fix-10: generations without WR improvement to flag an observation as stale
STALE_GEN_THRESHOLD = 5
BATTLE_PROMPT_CURRENT_BUDGET = int(os.environ.get("POK_BATTLE_EXP_CURRENT_BUDGET", "10000"))
BATTLE_PROMPT_NEW_DATA_BUDGET = int(os.environ.get("POK_BATTLE_EXP_NEW_DATA_BUDGET", "12000"))
BATTLE_PROMPT_MATCH_SECTION_BUDGET = int(os.environ.get("POK_BATTLE_EXP_SECTION_BUDGET", "1000"))
# Background experience LLM is advisory and noisy under live daemon load. Keep it
# opt-in; production can run it as an offline task by setting this env var.
BATTLE_PROMPT_MAX_CHARS = int(os.environ.get("POK_BATTLE_EXP_MAX_PROMPT_CHARS", "30000"))
BATTLE_EXPERIENCE_LLM_ENABLED = os.environ.get("POK_BATTLE_EXPERIENCE_LLM", "0") == "1"
_NO_EXPERIENCE_UPDATE = object()
_SUMMARY_READY_STATUS = "summary_ready"
_DONE_STATUS = "done"
_FORCE_SKIPPED_STATUS = "force_skipped"


def _memory_paths() -> battle_memory.BattleMemoryPaths:
    return battle_memory.BattleMemoryPaths(
        evidence_file=BATTLE_EVIDENCE_FILE,
        pending_file=BATTLE_PENDING_SUMMARIES_FILE,
        lessons_file=BATTLE_LESSONS_FILE,
    )

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
    """True if the replay no longer needs deterministic summary extraction.

    A match can be terminally done, force-skipped, or summary_ready. The last
    state means deterministic evidence was captured and queued for later lesson
    extraction; it is intentionally not the same as a completed LLM lesson.
    Transient failures (fail_count 1-2) return False so they get retried.
    """
    markers = _read_markers()
    entry = markers.get(match_id)
    if entry is None:
        return False
    if isinstance(entry, dict):
        return _marker_is_closed(entry)
    # legacy list form: plain string ID — treated as analyzed
    return True


def _marker_is_closed(entry: dict) -> bool:
    status = entry.get("status")
    if status in {_DONE_STATUS, _SUMMARY_READY_STATUS, _FORCE_SKIPPED_STATUS}:
        return True
    fc = entry.get("fail_count", 0)
    return fc == 0 or fc >= 3


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
    status = _DONE_STATUS if fail_count == 0 else _FORCE_SKIPPED_STATUS
    markers[match_id] = {"fail_count": fail_count, "status": status}
    _write_markers(markers)


def mark_summary_ready(match_id: str, *, evidence_ids: list[str] | None = None):
    """Mark deterministic replay evidence as captured, with LLM lessons pending."""
    markers = _read_markers()
    markers[match_id] = {
        "fail_count": 0,
        "status": _SUMMARY_READY_STATUS,
        "llm_pending": True,
        "evidence_ids": list(evidence_ids or []),
    }
    _write_markers(markers)


def increment_fail_count(match_id: str) -> int:
    """Bump fail_count for a match ID, return new count. Stays retryable until 3."""
    markers = _read_markers()
    entry = markers.get(match_id, {})
    if not isinstance(entry, dict):
        entry = {}
    new_count = entry.get("fail_count", 0) + 1
    markers[match_id] = {
        "fail_count": new_count,
        "status": _FORCE_SKIPPED_STATUS if new_count >= 3 else "llm_failed",
    }
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
    from rating_snapshot import _admitted_70_hand_history_sample
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if _admitted_70_hand_history_sample(entry) is None:
            continue
        match_id = entry.get("id", "")
        if not match_id:
            continue
        marker = markers.get(match_id)
        if isinstance(marker, dict):
            if _marker_is_closed(marker):
                continue  # done or force-skipped
            # transient failure — retry (fall through)
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
    """Background loop: wakes every POLL_INTERVAL, accumulates match summaries.

    Matches are accumulated across poll cycles until MERGE_THRESHOLD summaries
    are ready, then a SINGLE LLM merge is triggered.  This reduces LLM calls
    from ~15/hr to ~3-4/hr (~4x reduction).

    Uses a ThreadPoolExecutor for parallel replay parsing within the batch.
    Per-match errors are isolated — one failure does not abort the batch.
    Writes are serialized to avoid race conditions.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    _analyses_this_hour = 0
    _hour_start = time.time()
    # Accumulator: holds (entry, success_bool, summary_or_None) across cycles
    _pending_summaries: list[tuple[dict, bool, str | None]] = []

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
                # No new matches — but check if we have accumulated summaries
                # that should be flushed (e.g., after a long idle period).
                if len(_pending_summaries) >= MERGE_THRESHOLD:
                    try:
                        _apply_batch_results(_pending_summaries)
                        if any(r[2] for r in _pending_summaries):
                            _analyses_this_hour += 1
                    finally:
                        _pending_summaries = []
                continue

            # Extract per-match summaries in parallel (pure-data, parallel-safe).
            # The LLM merge is done ONCE over the combined batch in
            # _apply_batch_results (avoids the read-modify-write data-loss bug
            # where each worker read the same stale baseline and sequential
            # writes clobbered N-1 of the merges).
            batch_results = []  # list of (entry, success_bool, summary_or_None)
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
                        batch_results.append((entry, True, summary))
                    except Exception as e:
                        _kind = _classify_llm_error(e)
                        log_fn = log.info if _kind == "sdk_success_result" else log.warning
                        log_fn("Battle experience summary failed for %s (%s): %s", match_id, _kind, e)
                        try:
                            from system_log import log_system_event
                            log_system_event(
                                _llm_error_event_type(_kind),
                                _llm_error_event_severity(_kind),
                                f"Match {match_id} summary failed ({_kind}): {e}",
                                {"match_id": str(match_id), "kind": _kind, "error": str(e)[:200]},
                            )
                        except Exception:
                            pass
                        batch_results.append((entry, False, None))

            # Accumulate or persist successful summaries. When the advisory LLM
            # path is disabled, deterministic replay memory should be durable
            # immediately; otherwise a normal restart before MERGE_THRESHOLD
            # would lose summaries that only lived in this thread's list.
            for entry, success, summary in batch_results:
                if not success or summary is None:
                    # Failures bump fail_count immediately (not deferred)
                    fail_count = increment_fail_count(entry.get("id", ""))
                    if fail_count >= 3:
                        log.warning("Match %s force-skipped after %d failures",
                                    entry.get("id", ""), fail_count)
            immediate_count = _accumulate_successful_summaries(batch_results, _pending_summaries)
            if immediate_count:
                _analyses_this_hour += 1

            log.debug("Accumulated %d/%d summaries before LLM merge",
                      len(_pending_summaries), MERGE_THRESHOLD)

            # Fire LLM merge when threshold reached
            if len(_pending_summaries) >= MERGE_THRESHOLD:
                # L1: wrap merge + reset so a raised exception between
                # _apply_batch_results() and the reset cannot leave
                # _pending_summaries accumulating across iterations (a slow
                # memory leak under repeated LLM/asyncio errors). The summaries
                # have already been handed to _apply_batch_results; on failure it
                # bumps fail_count for each entry, so dropping them here is safe.
                try:
                    _apply_batch_results(_pending_summaries)
                    if any(r[2] for r in _pending_summaries):
                        _analyses_this_hour += 1
                finally:
                    _pending_summaries = []

        except Exception as e:
            log.warning("Experience thread error: %s", e)


def _process_one_match_safe(entry: dict) -> dict | None:
    """Extract the new-match summary for one match (pure-data, parallel-safe).

    Returns {"summary": str, "evidence": list[dict]}, or None if the replay is
    missing/corrupt/empty. Does NOT touch the experience file or run the LLM —
    the LLM merge is done ONCE over the combined batch in _apply_batch_results.
    This avoids the parallel read-modify-write data-loss bug where each worker
    would read the same stale baseline and the sequential writes would clobber
    N-1 of the merges.
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
    evidence_records = []
    for bot_name in (bot0, bot1):
        if not bot_name:
            continue
        summary = replay_analysis.summarize_replay_for_analysis(replay_data, bot_name)
        if summary:
            summary_parts.append(summary)
        evidence = replay_analysis.extract_replay_evidence_for_analysis(
            replay_data,
            bot_name,
            match_id=match_id,
        )
        if evidence:
            evidence_records.append(evidence)

    if not summary_parts:
        log.debug("Empty summaries for %s — will skip", match_id)
        return None

    return {
        "summary": "\n\n".join(summary_parts),
        "evidence": evidence_records,
    }


def _summary_payload_parts(payload) -> tuple[str, list[dict]]:
    """Normalize legacy string summaries and new structured summary payloads."""
    if isinstance(payload, dict):
        summary = str(payload.get("summary") or "")
        evidence = payload.get("evidence") or []
        if not isinstance(evidence, list):
            evidence = []
        return summary, [row for row in evidence if isinstance(row, dict)]
    return str(payload or ""), []


def _record_structured_batch_memory(success_payloads: list[tuple[dict, str, list[dict]]]) -> dict[str, list[str]]:
    """Persist deterministic evidence and pending summaries for a batch.

    Returns match_id -> evidence_ids for marker updates and lesson attribution.
    """
    paths = _memory_paths()
    all_evidence = []
    evidence_by_match: dict[str, list[str]] = {}
    for entry, _summary, evidence_records in success_payloads:
        match_id = str(entry.get("id", ""))
        ids = []
        for record in evidence_records:
            if record:
                all_evidence.append(record)
                if record.get("evidence_id"):
                    ids.append(str(record["evidence_id"]))
        evidence_by_match[match_id] = list(dict.fromkeys(ids))
    if all_evidence:
        try:
            battle_memory.append_evidence(all_evidence, paths=paths)
        except Exception as e:
            log.warning("Structured battle evidence write failed: %s", e)

    for entry, summary, _evidence_records in success_payloads:
        match_id = str(entry.get("id", ""))
        try:
            battle_memory.append_pending_summary(
                match_entry=entry,
                summary=summary,
                evidence_ids=evidence_by_match.get(match_id, []),
                paths=paths,
                status="llm_pending",
            )
        except Exception as e:
            log.warning("Structured pending battle summary write failed for %s: %s", match_id, e)
    return evidence_by_match


def _record_lessons_from_markdown(markdown: str, evidence_ids: list[str]):
    try:
        records = battle_memory.markdown_lessons_to_records(
            markdown,
            evidence_ids=evidence_ids,
        )
        if records:
            battle_memory.append_lessons(records, paths=_memory_paths())
    except Exception as e:
        log.warning("Structured battle lesson write failed: %s", e)


def _accumulate_successful_summaries(
    batch_results: list[tuple[dict, bool, object | None]],
    pending_summaries: list[tuple[dict, bool, object | None]],
) -> int:
    """Handle successful replay summaries according to the active LLM mode.

    With battle-experience LLM enabled, summaries are queued for the batched
    lesson merge. With it disabled, deterministic evidence and pending summaries
    are flushed immediately and the match marker is closed as summary_ready.

    Returns the number of summaries written immediately.
    """
    if BATTLE_EXPERIENCE_LLM_ENABLED:
        for entry, success, summary in batch_results:
            if success and summary is not None:
                pending_summaries.append((entry, success, summary))
        return 0

    success_payloads = []
    for entry, success, summary in batch_results:
        if not success or summary is None:
            continue
        summary_text, evidence_records = _summary_payload_parts(summary)
        if summary_text:
            success_payloads.append((entry, summary_text, evidence_records))

    if not success_payloads:
        return 0

    evidence_by_match = _record_structured_batch_memory(success_payloads)
    for entry, _summary, _evidence_records in success_payloads:
        match_id = str(entry.get("id", ""))
        mark_summary_ready(
            match_id,
            evidence_ids=evidence_by_match.get(match_id, []),
        )
    log.debug(
        "Recorded %d summaries as structured battle memory immediately (LLM disabled)",
        len(success_payloads),
    )
    return len(success_payloads)


def _apply_batch_results(results: list):
    """Apply a batch: ONE cumulative LLM merge over all successful summaries.

    fix-10: Uses incremental append mode — LLM generates ONLY new observations
    to append, rather than rewriting the entire document.  This cuts output
    tokens ~60% (append-only paragraph vs 80-line full rewrite).

    Successful matches are marked analyzed; failures bump fail_count
    (force-skip after 3).
    """
    summaries = []
    success_payloads = []
    for entry, success, summary in results:
        match_id = entry.get("id", "")
        if not success or summary is None:
            # Already bumped in caller; skip duplicate bump
            continue
        summary_text, evidence_records = _summary_payload_parts(summary)
        if not summary_text:
            continue
        summaries.append(summary_text)
        success_payloads.append((entry, summary_text, evidence_records))

    if not summaries:
        return

    evidence_by_match = _record_structured_batch_memory(success_payloads)
    combined = "\n\n---\n\n".join(summaries)
    current = _read_experience_file()

    # fix-10: incremental append — LLM outputs ONLY the new section,
    # which is then appended to the existing document rather than replacing it.
    new_section = _run_llm_incremental(current, combined)
    if new_section is _NO_EXPERIENCE_UPDATE:
        for entry, _summary, _evidence_records in success_payloads:
            match_id = entry.get("id", "")
            mark_summary_ready(
                match_id,
                evidence_ids=evidence_by_match.get(str(match_id), []),
            )
    elif new_section is not None:
        if current.strip():
            # Append new observations after existing content
            updated = current.rstrip() + "\n\n" + new_section + "\n"
        else:
            # First-ever analysis — write as-is
            updated = new_section + "\n"
        _write_experience_file(updated)
        all_evidence_ids = []
        for ids in evidence_by_match.values():
            all_evidence_ids.extend(ids)
        _record_lessons_from_markdown(new_section, list(dict.fromkeys(all_evidence_ids)))
        for entry, _summary, _evidence_records in success_payloads:
            mark_analyzed(entry.get("id", ""), fail_count=0)
    else:
        # LLM merge failed: bump fail_count for every successful-summary match
        # so they stay retryable (and force-skip after 3 LLM failures).
        for entry, _summary, _evidence_records in success_payloads:
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
    evidence_records = []
    for bot_name in (bot0, bot1):
        if not bot_name:
            continue
        summary = replay_analysis.summarize_replay_for_analysis(replay_data, bot_name)
        if summary:
            summary_parts.append(summary)
        evidence = replay_analysis.extract_replay_evidence_for_analysis(
            replay_data,
            bot_name,
            match_id=match_id,
        )
        if evidence:
            evidence_records.append(evidence)

    if not summary_parts:
        log.debug("Empty summaries for %s — marking as analyzed", match_id)
        mark_analyzed(match_id, fail_count=0)
        return

    new_match_summary = "\n\n".join(summary_parts)
    evidence_by_match = _record_structured_batch_memory([
        (entry, new_match_summary, evidence_records)
    ])

    # 4. Read current experience
    current_experience = _read_experience_file()

    # 5-6. Run LLM update
    updated = _run_llm_update(current_experience, new_match_summary)
    if updated is not None:
        _write_experience_file(updated)
        _record_lessons_from_markdown(
            updated,
            evidence_by_match.get(str(match_id), []),
        )
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


def _run_llm_incremental(current_experience: str, new_match_data: str):
    """Run LLM in incremental append mode (fix-10).

    Instead of rewriting the entire document, asks the LLM to produce ONLY
    the new observations section to append.  Returns the new section text,
    or None on failure.
    """
    prompt_template_path = PROMPTS_DIR / "battle_experience_incremental.md"
    if not prompt_template_path.exists():
        # Fallback to full-rewrite mode if incremental template missing
        log.debug("Incremental prompt template not found — falling back to full rewrite")
        return _run_llm_update(current_experience, new_match_data)

    try:
        template = prompt_template_path.read_text(encoding="utf-8")
    except OSError as e:
        log.warning("Failed to read incremental prompt template: %s", e)
        return _run_llm_update(current_experience, new_match_data)

    current_prompt, new_data_prompt = _prepare_prompt_inputs(
        current_experience,
        new_match_data,
        mode="incremental",
    )

    prompt = substitute_template(template, {
        "current_experience": current_prompt or "(empty — first analysis)",
        "new_match_data": new_data_prompt,
    })

    skip_reason = _llm_skip_reason(prompt)
    if skip_reason:
        _log_llm_skip(skip_reason, prompt_chars=len(prompt), mode="incremental")
        return _NO_EXPERIENCE_UPDATE

    output = _run_sync_llm_call(prompt)
    if output is None:
        return None

    stripped = output.strip()
    if len(stripped) < 20:
        log.warning("LLM returned very short incremental output (%d chars) — skipping", len(stripped))
        return None

    return stripped


def _run_llm_update(current_experience: str, new_match_data: str) -> str | None:
    """Send current experience + new match data to LLM, get updated experience.

    Returns the updated markdown content, or None on failure (caller keeps
    the existing file unchanged).  Full-rewrite mode — kept as fallback and
    for the serial _process_one_match path.
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

    current_prompt, new_data_prompt = _prepare_prompt_inputs(
        current_experience,
        new_match_data,
        mode="full_update",
    )

    prompt = substitute_template(template, {
        "current_experience": current_prompt or "(empty — first analysis)",
        "new_match_data": new_data_prompt,
    })

    skip_reason = _llm_skip_reason(prompt)
    if skip_reason:
        _log_llm_skip(skip_reason, prompt_chars=len(prompt), mode="full_update")
        return None

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


def _llm_skip_reason(prompt: str) -> str:
    if not BATTLE_EXPERIENCE_LLM_ENABLED:
        return "disabled"
    if BATTLE_PROMPT_MAX_CHARS > 0 and len(prompt) > BATTLE_PROMPT_MAX_CHARS:
        return "prompt_too_large"
    return ""


def _log_llm_skip(reason: str, *, prompt_chars: int, mode: str) -> None:
    log.info(
        "Battle experience LLM skipped (%s): prompt_chars=%d max=%d mode=%s",
        reason,
        prompt_chars,
        BATTLE_PROMPT_MAX_CHARS,
        mode,
    )
    try:
        from system_log import log_system_event
        log_system_event(
            "battle_exp.llm_skipped",
            "info",
            f"Battle experience LLM skipped ({reason})",
            {
                "reason": reason,
                "prompt_chars": prompt_chars,
                "max_prompt_chars": BATTLE_PROMPT_MAX_CHARS,
                "mode": mode,
                "timeout_sec": LLM_TIMEOUT,
            },
        )
    except Exception:
        pass


def _run_sync_llm_call(prompt: str) -> str | None:
    """Run run_claude_query in this thread via a fresh event loop.

    Uses asyncio.wait_for for a cancellable timeout — when the timeout fires,
    the underlying task is cancelled (no leaked thread continuing to burn LLM
    quota, which the previous threading+join approach caused).

    Returns the text output, or None on any failure (including timeout).
    """
    ui = SilentUI()
    log_path = RESULTS_DIR / "battle_exp_llm.log"
    # Rotate the LLM prompt/response dump before appending (root-cause fix for
    # the 102MB unbounded growth, 2026-06-18). llm_query appends every prompt+
    # response with no upper bound; cap at one rotated backup (.log.1) so the
    # file cannot grow without limit. Mirrors orchestrator _rotate_orchestrator_logs.
    # Serialize rotation across the MAX_CONCURRENT_LLM workers: without a lock,
    # two workers can both see >50MB and race the rename (one wins, the other's
    # rename throws FileNotFoundError — swallowed by the except, benign but loses
    # the backup). The lock makes rotation atomic.
    with _LOG_ROTATION_LOCK:
        try:
            if log_path.exists() and log_path.stat().st_size > 50 * 1024 * 1024:
                rotated = log_path.with_suffix(log_path.suffix + ".1")
                if rotated.exists():
                    try:
                        rotated.unlink()
                    except Exception:
                        pass
                log_path.rename(rotated)
        except Exception:
            pass

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
        _kind = _classify_llm_error(e)
        _log_llm_failure("Sync LLM call failed (%s): %s", _kind, e)
        try:
            from system_log import log_system_event
            log_system_event(
                _llm_error_event_type(_kind),
                _llm_error_event_severity(_kind),
                f"Sync LLM call failed ({_kind}): {e}",
                {"kind": _kind, "error": str(e)[:200]},
            )
        except Exception:
            pass
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


def _trim_middle_for_prompt(text: str, budget: int) -> tuple[str, bool]:
    """Keep both headline context and the most recent tail under a char budget."""
    text = text or ""
    if budget <= 0 or len(text) <= budget:
        return text, False
    marker = f"\n\n[... omitted {len(text) - budget} chars for battle_experience prompt budget ...]\n\n"
    if budget <= len(marker) + 200:
        return text[-budget:], True
    keep = budget - len(marker)
    head = max(200, int(keep * 0.35))
    tail = max(200, keep - head)
    return text[:head].rstrip() + marker + text[-tail:].lstrip(), True


def _trim_tail_for_prompt(text: str, budget: int) -> tuple[str, bool]:
    text = text or ""
    if budget <= 0 or len(text) <= budget:
        return text, False
    marker = f"[... omitted older {len(text) - budget} chars for battle_experience prompt budget ...]\n\n"
    if budget <= len(marker):
        return text[-budget:], True
    return marker + text[-(budget - len(marker)):], True


def _compact_new_match_data(new_match_data: str) -> tuple[str, dict]:
    """Bound each replay-summary section before applying a global new-data cap."""
    raw = new_match_data or ""
    sections = raw.split("\n\n---\n\n")
    trimmed_sections = []
    section_trims = 0
    for section in sections:
        compact, trimmed = _trim_tail_for_prompt(section, BATTLE_PROMPT_MATCH_SECTION_BUDGET)
        trimmed_sections.append(compact)
        section_trims += int(trimmed)
    joined = "\n\n---\n\n".join(trimmed_sections)
    compact_joined, global_trimmed = _trim_tail_for_prompt(joined, BATTLE_PROMPT_NEW_DATA_BUDGET)
    return compact_joined, {
        "new_data_chars_before": len(raw),
        "new_data_chars_after": len(compact_joined),
        "new_data_sections": len(sections),
        "section_trims": section_trims,
        "global_trimmed": bool(global_trimmed),
    }


def _prepare_prompt_inputs(current_experience: str, new_match_data: str, *, mode: str) -> tuple[str, str]:
    """Prepare bounded LLM inputs for battle-experience updates.

    The background thread is advisory. It should never feed a 700k+ prompt into
    the LLM or spend the full cycle budget just to append lessons.
    """
    current_raw = current_experience or ""
    try:
        current_raw = _compress_dup_sections(current_raw)
    except Exception:
        pass
    current_prompt, current_trimmed = _trim_middle_for_prompt(
        current_raw,
        BATTLE_PROMPT_CURRENT_BUDGET,
    )
    new_prompt, meta = _compact_new_match_data(new_match_data)
    meta.update({
        "mode": mode,
        "current_chars_before": len(current_experience or ""),
        "current_chars_after": len(current_prompt),
        "current_trimmed": bool(current_trimmed),
        "current_budget": BATTLE_PROMPT_CURRENT_BUDGET,
        "new_data_budget": BATTLE_PROMPT_NEW_DATA_BUDGET,
        "section_budget": BATTLE_PROMPT_MATCH_SECTION_BUDGET,
    })
    if meta["current_trimmed"] or meta["section_trims"] or meta["global_trimmed"]:
        log.info(
            "Battle experience prompt compacted: current %d->%d, new %d->%d",
            meta["current_chars_before"],
            meta["current_chars_after"],
            meta["new_data_chars_before"],
            meta["new_data_chars_after"],
        )
        try:
            from system_log import log_system_event
            log_system_event(
                "battle_exp.prompt_compacted",
                "info",
                "Battle experience prompt compacted before LLM call",
                meta,
            )
        except Exception:
            pass
    return current_prompt, new_prompt


# ──────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────


def get_battle_experience(source_bot: str = "") -> str:
    """Return the current battle experience content.

    Called from generation_scheduler at generation start.
    No LLM call — just reads the file.

    fix-10: Tags observations referencing bot versions with no WR improvement
    across >= STALE_GEN_THRESHOLD generations with [POSSIBLY STALE] markers.

    RC5: Compresses duplicated section types (CROSS-PAIR PATTERNS / VERSION
    TRENDS accumulate via incremental-append) by keeping only the most recent
    N per type, while preserving high-value sections (ACTIONABLE INSIGHTS,
    STRENGTH PATTERNS) in full. Keeps the Master prompt bounded without
    discarding actionable strategic lessons.
    """
    raw = _read_experience_file()
    structured = ""
    try:
        structured = battle_memory.format_battle_memory_for_master(
            paths=_memory_paths(),
            source_bot=source_bot,
            max_lessons=8,
            max_pending=6,
            max_evidence=8,
        )
    except Exception as e:
        log.warning("Structured battle memory read failed: %s", e)
        structured = ""
    if not raw:
        return structured
    tagged = _tag_stale_observations(raw)
    compact = _compress_dup_sections(tagged)
    if structured:
        return compact.rstrip() + "\n\n" + structured + "\n"
    return compact


# Section types that accumulate via incremental-append (many near-duplicate
# snapshots). Keep only the most recent _MAX_PER_DUP_TYPE per type.
_DUP_SECTION_TYPES = {"CROSS-PAIR PATTERNS", "VERSION TRENDS"}
_MAX_PER_DUP_TYPE = 4
# High-value section types kept in full (no truncation).
_FULL_KEEP_TYPES = {"ACTIONABLE INSIGHTS", "STRENGTH PATTERNS"}
_COMPRESS_BUDGET = 60_000


def _compress_dup_sections(content: str) -> str:
    """Compress duplicated section types while preserving high-value sections.

    Splits content by '## ' headers. For each section type in
    _DUP_SECTION_TYPES, keeps only the last _MAX_PER_DUP_TYPE occurrences
    (most recent). All other sections (ACTIONABLE INSIGHTS, STRENGTH
    PATTERNS, preamble, etc.) are kept in full. If the result still exceeds
    _COMPRESS_BUDGET, falls back to a tail-trim (most recent content wins).
    """
    import re as _re
    parts = _re.split(r'(\n##\s+[^\n]+)', content)
    preamble = parts[0] if parts else content
    sections = []
    for i in range(1, len(parts) - 1, 2):
        header = parts[i].strip()
        body = parts[i + 1] if i + 1 < len(parts) else ""
        htype = header.replace('## ', '')
        bracket = htype.find(' [')
        if bracket > 0:
            htype = htype[:bracket]
        htype = htype.strip()
        sections.append((header, body, htype))

    by_type = {}
    for idx, (header, body, htype) in enumerate(sections):
        by_type.setdefault(htype, []).append((idx, header, body))

    kept = [(0, preamble)]
    for htype, items in by_type.items():
        if htype in _DUP_SECTION_TYPES and len(items) > _MAX_PER_DUP_TYPE:
            kept_items = items[-_MAX_PER_DUP_TYPE:]
            dropped = len(items) - len(kept_items)
            kept.append((items[-_MAX_PER_DUP_TYPE][0],
                         "\n[... " + str(dropped) + " earlier '" + htype +
                         "' sections compressed \u2014 kept most recent " +
                         str(_MAX_PER_DUP_TYPE) + " ...]\n"))
        else:
            kept_items = items
        for idx, header, body in kept_items:
            kept.append((idx, header + body))

    kept.sort(key=lambda x: x[0])
    result = "".join(t for _, t in kept)

    if len(result) > _COMPRESS_BUDGET:
        from llm_query import _trim_to_budget
        result = _trim_to_budget(result, _COMPRESS_BUDGET, tail=True)
    return result


def _tag_stale_observations(content: str) -> str:
    """Tag paragraphs mentioning bot versions whose WR shows no improvement.

    Reads glicko_ratings.json to find which bot versions have been in the pool
    longest (proxy: rating has not improved over recent generations).

    For each ## section, if it mentions bot versions that are >= STALE_GEN_THRESHOLD
    generations old AND the version's rating is below the pool median, prepend
    [POSSIBLY STALE — no WR improvement in N gens] to that section.
    """
    from evolution_infra import read_locked_json
    from pathlib import Path

    # Only tag sections that look like candidate "lessons" (mention specific versions)
    # Skip generic sections like CROSS-PAIR PATTERNS if they don't name versions.
    import re

    # Parse the current pool to find generation gap per bot version
    try:
        ratings = read_locked_json(RESULTS_DIR / "glicko_ratings.json", default={})
    except Exception:
        return content  # can't determine staleness — return as-is

    if not isinstance(ratings, dict) or not ratings:
        return content

    # Build a map: version_name -> generation_number (from tag/v-number)
    def _bot_gen(name: str) -> int | None:
        """Extract generation number from bot name (e.g. 'national_v143' -> 143)."""
        m = re.search(r'v(\d+)$', name)
        return int(m.group(1)) if m else None

    # Current generation = max generation in the pool
    gens = [_bot_gen(name) for name in ratings if _bot_gen(name) is not None]
    if not gens:
        return content
    current_gen = max(gens)

    # Minimum generation that has NOT gone stale
    min_fresh_gen = current_gen - STALE_GEN_THRESHOLD

    # Split content into sections (## headers)
    sections = re.split(r'(?=^## )', content, flags=re.MULTILINE)
    tagged_sections = []

    for section in sections:
        if not section.strip():
            continue
        # Find bot version references in this section
        versions_mentioned = re.findall(r'v(\d+)', section)
        if not versions_mentioned:
            tagged_sections.append(section)
            continue

        stale_versions = [int(v) for v in versions_mentioned if int(v) < min_fresh_gen]
        if stale_versions:
            oldest = min(stale_versions)
            gap = current_gen - oldest
            stale_tag = f"[POSSIBLY STALE — no WR improvement in {gap} gens]\n"
            # Only tag once per section (prepend to first line)
            first_line_end = section.find('\n')
            if first_line_end >= 0:
                header = section[:first_line_end + 1]
                rest = section[first_line_end + 1:]
                # Don't double-tag
                if '[POSSIBLY STALE' not in section:
                    section = header + stale_tag + rest
        tagged_sections.append(section)

    return "".join(tagged_sections)


def _tag_stale_observations(content: str) -> str:
    """Tag paragraphs mentioning bot versions whose WR shows no improvement.

    Reads glicko_ratings.json to find which bot versions have been in the pool
    longest (proxy: rating has not improved over recent generations).

    For each ## section, if it mentions bot versions that are >= STALE_GEN_THRESHOLD
    generations old AND the version's rating is below the pool median, prepend
    [POSSIBLY STALE — no WR improvement in N gens] to that section.
    """
    from evolution_infra import read_locked_json
    from pathlib import Path

    # Only tag sections that look like candidate "lessons" (mention specific versions)
    # Skip generic sections like CROSS-PAIR PATTERNS if they don't name versions.
    import re

    # Parse the current pool to find generation gap per bot version
    try:
        ratings = read_locked_json(RESULTS_DIR / "glicko_ratings.json", default={})
    except Exception:
        return content  # can't determine staleness — return as-is

    if not isinstance(ratings, dict) or not ratings:
        return content

    # Build a map: version_name -> generation_number (from tag/v-number)
    def _bot_gen(name: str) -> int | None:
        """Extract generation number from bot name (e.g. 'national_v143' -> 143)."""
        m = re.search(r'v(\d+)$', name)
        return int(m.group(1)) if m else None

    # Current generation = max generation in the pool
    gens = [_bot_gen(name) for name in ratings if _bot_gen(name) is not None]
    if not gens:
        return content
    current_gen = max(gens)

    # Minimum generation that has NOT gone stale
    min_fresh_gen = current_gen - STALE_GEN_THRESHOLD

    # Split content into sections (## headers)
    sections = re.split(r'(?=^## )', content, flags=re.MULTILINE)
    tagged_sections = []

    for section in sections:
        if not section.strip():
            continue
        # Find bot version references in this section
        versions_mentioned = re.findall(r'v(\d+)', section)
        if not versions_mentioned:
            tagged_sections.append(section)
            continue

        stale_versions = [int(v) for v in versions_mentioned if int(v) < min_fresh_gen]
        if stale_versions:
            oldest = min(stale_versions)
            gap = current_gen - oldest
            stale_tag = f"[POSSIBLY STALE — no WR improvement in {gap} gens]\n"
            # Only tag once per section (prepend to first line)
            first_line_end = section.find('\n')
            if first_line_end >= 0:
                header = section[:first_line_end + 1]
                rest = section[first_line_end + 1:]
                # Don't double-tag
                if '[POSSIBLY STALE' not in section:
                    section = header + stale_tag + rest
        tagged_sections.append(section)

    return "".join(tagged_sections)
