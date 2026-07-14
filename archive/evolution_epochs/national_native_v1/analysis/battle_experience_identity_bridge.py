"""Archived background analysis bridge for national_native_v1 rating replays.

The worker consumes only complete, admitted 70-hand native TCP replay files
whose epoch and evaluation identity match the current daemon contract.  It
persists deterministic evidence before optional advisory LLM synthesis.  There
is no compatibility markdown store and no retired replay parser; rejected data
can never be returned by :func:`get_battle_experience`.  The active policy
epoch keeps only the identity-bound replay-lesson storage kernel and does not
start this LLM/background bridge.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import threading
import time
from typing import Any

import battle_memory
from bot_namespace import EVALUATION_EPOCH, FIRST_STRICT_POLICY_VERSION, parse_bot_version
from evolution_infra import (
    BaseUI,
    LLM_COSTS_FILE,
    MATCH_HISTORY_FILE,
    REPLAY_DIR,
    RESULTS_DIR,
    append_locked_jsonl,
    locked_file,
    read_locked_json,
    write_locked_json,
)
from llm_failure import is_llm_infra_error, is_success_error_result
import replay_analysis


log = logging.getLogger("pok.battle_exp")

POLL_INTERVAL = int(os.environ.get("POK_BATTLE_EXPERIENCE_POLL_INTERVAL", "20"))
TARGET_BATCH = int(os.environ.get("POK_BATTLE_EXPERIENCE_BATCH", "6"))
MAX_CONCURRENT_LLM = 3
MAX_ANALYSES_PER_HOUR = 240
MERGE_THRESHOLD = 6
LLM_TIMEOUT = int(os.environ.get("POK_BATTLE_EXPERIENCE_LLM_TIMEOUT", "90"))
BATTLE_EXPERIENCE_LLM_ENABLED = os.environ.get("POK_BATTLE_EXPERIENCE_LLM", "0") == "1"
BATTLE_PROMPT_CURRENT_BUDGET = int(os.environ.get("POK_BATTLE_EXP_CURRENT_BUDGET", "10000"))
BATTLE_PROMPT_NEW_DATA_BUDGET = int(os.environ.get("POK_BATTLE_EXP_NEW_DATA_BUDGET", "12000"))
BATTLE_PROMPT_MATCH_SECTION_BUDGET = int(os.environ.get("POK_BATTLE_EXP_SECTION_BUDGET", "1000"))
BATTLE_PROMPT_MAX_CHARS = int(os.environ.get("POK_BATTLE_EXP_MAX_PROMPT_CHARS", "30000"))

BATTLE_EVIDENCE_FILE = RESULTS_DIR / "battle_evidence.jsonl"
BATTLE_PENDING_SUMMARIES_FILE = RESULTS_DIR / "battle_pending_summaries.jsonl"
BATTLE_LESSONS_FILE = RESULTS_DIR / "battle_lessons.jsonl"
ANALYSIS_MARKER_FILE = RESULTS_DIR / ".battle_analysis_progress.json"

MARKER_SCHEMA_VERSION = 2
_DONE_STATUS = "summary_ready"
_REJECTED_STATUS = "rejected"
_NO_EXPERIENCE_UPDATE = object()
_LOG_ROTATION_LOCK = threading.Lock()


def _classify_llm_error(error: BaseException) -> str:
    if is_success_error_result(error):
        return "sdk_success_result"
    return "infra" if is_llm_infra_error(error) else "business"


def _llm_error_event_type(kind: str) -> str:
    return "battle_exp.sdk_success_result" if kind == "sdk_success_result" else f"battle_exp.{kind}_error"


def _llm_error_event_severity(kind: str) -> str:
    return "warn" if kind == "infra" else "info"


def _log_llm_failure(message: str, kind: str, exc: BaseException) -> None:
    (log.info if kind == "sdk_success_result" else log.warning)(message, kind, exc)


def _memory_paths() -> battle_memory.BattleMemoryPaths:
    return battle_memory.BattleMemoryPaths(
        evidence_file=BATTLE_EVIDENCE_FILE,
        pending_file=BATTLE_PENDING_SUMMARIES_FILE,
        lessons_file=BATTLE_LESSONS_FILE,
    )


def _current_identity_digest() -> str | None:
    try:
        from evaluation_data_identity import current_evaluation_digest

        value = current_evaluation_digest(RESULTS_DIR)
    except Exception:
        return None
    return str(value) if isinstance(value, str) and len(value) == 64 else None


def _strict_bot(value: Any) -> bool:
    version = parse_bot_version(value if isinstance(value, str) else None)
    return version is not None and version >= FIRST_STRICT_POLICY_VERSION


class SilentUI(BaseUI):
    """Cost-only UI used by the optional replay lesson synthesizer."""

    def update_cost(self, role, cost_usd, usage):
        if cost_usd is None:
            return
        try:
            append_locked_jsonl(LLM_COSTS_FILE, {
                "role": role,
                "cost_usd": cost_usd,
                "input_tokens": (usage or {}).get("input_tokens", 0),
                "output_tokens": (usage or {}).get("output_tokens", 0),
                "ts": time.time(),
                "epoch": EVALUATION_EPOCH,
            })
        except OSError:
            pass


def _empty_markers() -> dict[str, Any]:
    return {
        "schema_version": MARKER_SCHEMA_VERSION,
        "epoch": EVALUATION_EPOCH,
        "execution_mode": "native_tcp",
        "entries": {},
    }


def _read_markers() -> dict[str, Any]:
    raw = read_locked_json(ANALYSIS_MARKER_FILE, default=None)
    if not isinstance(raw, dict):
        return _empty_markers()
    if (
        raw.get("schema_version") != MARKER_SCHEMA_VERSION
        or raw.get("epoch") != EVALUATION_EPOCH
        or raw.get("execution_mode") != "native_tcp"
        or not isinstance(raw.get("entries"), dict)
    ):
        return _empty_markers()
    return raw


def _write_markers(markers: dict[str, Any]) -> None:
    write_locked_json(ANALYSIS_MARKER_FILE, markers)


def _mark(match_id: str, status: str, digest: str, reason: str = "") -> None:
    if not isinstance(match_id, str) or Path(match_id).name != match_id:
        return
    markers = _read_markers()
    markers["entries"][match_id] = {
        "status": status,
        "evaluation_identity_digest": digest,
        "reason": reason[:160],
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    _write_markers(markers)


def is_analyzed(match_id: str, *, evaluation_identity_digest: str | None = None) -> bool:
    digest = evaluation_identity_digest or _current_identity_digest()
    if digest is None:
        return False
    entry = _read_markers()["entries"].get(match_id)
    return bool(
        isinstance(entry, dict)
        and entry.get("evaluation_identity_digest") == digest
        and entry.get("status") in {_DONE_STATUS, _REJECTED_STATUS}
    )


def mark_analyzed(
    match_id: str,
    *,
    evaluation_identity_digest: str | None = None,
    fail_count: int = 0,
) -> None:
    """Close a current-identity replay after durable evidence publication."""

    digest = evaluation_identity_digest or _current_identity_digest()
    if digest is not None:
        _mark(match_id, _DONE_STATUS if fail_count == 0 else _REJECTED_STATUS, digest)


def mark_summary_ready(
    match_id: str,
    *,
    evidence_ids: list[str] | None = None,
    evaluation_identity_digest: str | None = None,
) -> None:
    digest = evaluation_identity_digest or _current_identity_digest()
    if digest is not None:
        _mark(match_id, _DONE_STATUS, digest, f"evidence={','.join(evidence_ids or [])[:100]}")


def increment_fail_count(match_id: str) -> int:
    """Reject an unreadable current replay immediately; retired data is not retried."""

    digest = _current_identity_digest()
    if digest is not None:
        _mark(match_id, _REJECTED_STATUS, digest, "native_replay_rejected")
    return 1


def _history_entry_valid(entry: Any, digest: str) -> bool:
    if not isinstance(entry, dict):
        return False
    match_id = entry.get("id")
    if (
        not isinstance(match_id, str)
        or Path(match_id).name != match_id
        or not match_id.endswith(".json")
        or entry.get("evaluation_epoch") != EVALUATION_EPOCH
        or entry.get("execution_mode") != "native_tcp"
        or entry.get("evaluation_identity_digest") != digest
        or not _strict_bot(entry.get("bot0"))
        or not _strict_bot(entry.get("bot1"))
    ):
        return False
    from rating_snapshot import _admitted_70_hand_history_sample

    return _admitted_70_hand_history_sample(entry) is not None


def get_unanalyzed_matches(n: int = TARGET_BATCH) -> list[dict[str, Any]]:
    """Return chronological current-identity native rating rows only."""

    digest = _current_identity_digest()
    if digest is None or not MATCH_HISTORY_FILE.exists() or MATCH_HISTORY_FILE.is_symlink():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with locked_file(MATCH_HISTORY_FILE, "r", encoding="utf-8") as handle:
            for raw in handle:
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not _history_entry_valid(entry, digest) or is_analyzed(
                    entry["id"], evaluation_identity_digest=digest
                ):
                    continue
                replay_path = REPLAY_DIR / entry["id"]
                if replay_path.is_file() and not replay_path.is_symlink():
                    rows.append(entry)
    except (OSError, UnicodeDecodeError):
        return []
    rows.sort(key=lambda row: (str(row.get("timestamp") or ""), row["id"]))
    return rows[: max(0, int(n))]


def _process_one_match_safe(entry: dict[str, Any]) -> dict[str, Any] | None:
    """Validate and summarize one replay without persisting prompt material."""

    digest = str(entry.get("evaluation_identity_digest") or "")
    match_id = str(entry.get("id") or "")
    if not _history_entry_valid(entry, digest):
        return None
    replay_path = REPLAY_DIR / match_id
    try:
        with locked_file(replay_path, "r", encoding="utf-8") as handle:
            replay = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        _mark(match_id, _REJECTED_STATUS, digest, "replay_unreadable")
        return None
    validation = replay_analysis.validate_native_replay(
        replay,
        expected_evaluation_identity_digest=digest,
        expected_replay_id=match_id,
    )
    if not validation.accepted:
        _mark(match_id, _REJECTED_STATUS, digest, validation.reason)
        return None
    summaries: list[str] = []
    evidence: list[dict[str, Any]] = []
    for bot in (entry["bot0"], entry["bot1"]):
        summary = replay_analysis.summarize_replay_for_analysis(
            replay,
            bot,
            expected_evaluation_identity_digest=digest,
        )
        row = replay_analysis.extract_replay_evidence_for_analysis(
            replay,
            bot,
            match_id=match_id,
            expected_evaluation_identity_digest=digest,
        )
        if not summary or row is None:
            _mark(match_id, _REJECTED_STATUS, digest, "deterministic_analysis_failed")
            return None
        summaries.append(summary)
        evidence.append(row)
    return {"summary": "\n\n".join(summaries), "evidence": evidence}


def _summary_payload_parts(payload: Any) -> tuple[str, list[dict[str, Any]]]:
    if not isinstance(payload, dict):
        return "", []
    summary = payload.get("summary")
    evidence = payload.get("evidence")
    return (
        str(summary) if isinstance(summary, str) else "",
        [row for row in evidence if isinstance(row, dict)] if isinstance(evidence, list) else [],
    )


def _record_structured_batch_memory(
    payloads: list[tuple[dict[str, Any], str, list[dict[str, Any]]]],
) -> dict[str, list[str]]:
    paths = _memory_paths()
    all_evidence = [row for _entry, _summary, rows in payloads for row in rows]
    appended = battle_memory.append_evidence(all_evidence, paths=paths)
    available = {
        str(row.get("evidence_id")): row
        for row in all_evidence + appended
        if row.get("evidence_id")
    }
    by_match: dict[str, list[str]] = {}
    for entry, summary, rows in payloads:
        match_id = str(entry["id"])
        ids = [
            str(row["evidence_id"]) for row in rows
            if row.get("evidence_id") in available
        ]
        by_match[match_id] = ids
        battle_memory.append_pending_summary(
            match_entry=entry,
            summary=summary,
            evidence_ids=ids,
            paths=paths,
            status="summary_ready",
        )
    return by_match


def _apply_batch_results(results: list[tuple[dict[str, Any], bool, Any]]) -> None:
    payloads: list[tuple[dict[str, Any], str, list[dict[str, Any]]]] = []
    for entry, success, payload in results:
        if not success:
            continue
        summary, evidence = _summary_payload_parts(payload)
        if summary and evidence:
            payloads.append((entry, summary, evidence))
    if not payloads:
        return
    evidence_by_match = _record_structured_batch_memory(payloads)
    all_ids = list(dict.fromkeys(
        evidence_id for ids in evidence_by_match.values() for evidence_id in ids
    ))
    digests = {str(entry.get("evaluation_identity_digest") or "") for entry, _s, _e in payloads}
    if BATTLE_EXPERIENCE_LLM_ENABLED and len(digests) == 1 and all_ids:
        rendered = "\n\n---\n\n".join(summary for _entry, summary, _rows in payloads)
        lesson_text = _run_llm_incremental("", rendered)
        if isinstance(lesson_text, str) and lesson_text.strip():
            records = battle_memory.markdown_lessons_to_records(
                lesson_text,
                evidence_ids=all_ids,
                evaluation_identity_digest=next(iter(digests)),
            )
            battle_memory.append_lessons(records, paths=_memory_paths())
    for entry, _summary, _rows in payloads:
        ids = evidence_by_match.get(str(entry["id"]), [])
        if ids:
            mark_summary_ready(
                entry["id"],
                evidence_ids=ids,
                evaluation_identity_digest=str(entry["evaluation_identity_digest"]),
            )


def _process_one_match(entry: dict[str, Any]) -> None:
    payload = _process_one_match_safe(entry)
    if payload is not None:
        _apply_batch_results([(entry, True, payload)])


_thread: threading.Thread | None = None
_stop_event = threading.Event()


def start_experience_thread() -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(target=_experience_loop, daemon=True, name="native-replay-experience")
    _thread.start()
    log.info("Strict native replay experience thread started")


def _experience_loop() -> None:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    while not _stop_event.wait(max(1, POLL_INTERVAL)):
        try:
            entries = get_unanalyzed_matches(TARGET_BATCH)
            if not entries:
                continue
            results: list[tuple[dict[str, Any], bool, Any]] = []
            with ThreadPoolExecutor(max_workers=max(1, MAX_CONCURRENT_LLM)) as pool:
                futures = {pool.submit(_process_one_match_safe, entry): entry for entry in entries}
                for future in as_completed(futures):
                    entry = futures[future]
                    try:
                        payload = future.result()
                    except Exception as exc:
                        log.warning("Native replay analysis failed for %s: %s", entry.get("id"), exc)
                        payload = None
                    results.append((entry, payload is not None, payload))
            _apply_batch_results(results)
        except Exception as exc:
            log.warning("Native replay experience cycle failed: %s", exc)


def _trim_middle_for_prompt(text: str, budget: int) -> tuple[str, bool]:
    if len(text) <= budget:
        return text, False
    marker = "\n...[identity-bound native evidence omitted for budget]...\n"
    remaining = max(0, budget - len(marker))
    head = remaining // 2
    return text[:head] + marker + text[-(remaining - head):], True


def _trim_tail_for_prompt(text: str, budget: int) -> tuple[str, bool]:
    if len(text) <= budget:
        return text, False
    marker = "[older identity-bound native summaries omitted]\n"
    return marker + text[-max(0, budget - len(marker)):], True


def _compact_new_match_data(text: str) -> tuple[str, dict[str, int]]:
    sections = text.split("\n\n---\n\n")
    compact = [
        _trim_middle_for_prompt(section, BATTLE_PROMPT_MATCH_SECTION_BUDGET)[0]
        for section in sections
    ]
    result, trimmed = _trim_tail_for_prompt(
        "\n\n---\n\n".join(compact), BATTLE_PROMPT_NEW_DATA_BUDGET
    )
    return result, {"sections": len(sections), "trimmed": int(trimmed)}


def _prepare_prompt_inputs(current: str, new_data: str, *, mode: str) -> tuple[str, str]:
    del mode
    current_prompt, _ = _trim_middle_for_prompt(current, BATTLE_PROMPT_CURRENT_BUDGET)
    new_prompt, _ = _compact_new_match_data(new_data)
    return current_prompt, new_prompt


def _run_llm_incremental(current_experience: str, new_match_data: str):
    """Optionally synthesize advisory bullets from already-validated summaries."""

    if not BATTLE_EXPERIENCE_LLM_ENABLED:
        return _NO_EXPERIENCE_UPDATE
    current, new_data = _prepare_prompt_inputs(
        current_experience, new_match_data, mode="strict_native_incremental"
    )
    prompt = (
        "You receive deterministic evidence from complete national_tcp_policy_v1 "
        "native TCP 70-hand matches. Return at most eight concise Markdown bullets. "
        "Treat every conclusion as advisory, preserve sample counts, distinguish "
        "fold_to_raise/fold_to_jam/river_overcall denominators, and use showdown "
        "bucket evidence when present. Do not invent hands, actions, protocols, or "
        "action encodings absent from the validated evidence.\n\n"
        f"Existing same-identity notes:\n{current}\n\nNew validated evidence:\n{new_data}"
    )
    if BATTLE_PROMPT_MAX_CHARS > 0 and len(prompt) > BATTLE_PROMPT_MAX_CHARS:
        return _NO_EXPERIENCE_UPDATE
    return _run_sync_llm_call(prompt)


def _run_llm_update(current_experience: str, new_match_data: str):
    return _run_llm_incremental(current_experience, new_match_data)


def _run_sync_llm_call(prompt: str) -> str | None:
    ui = SilentUI()
    log_path = RESULTS_DIR / "native_replay_analysis_llm.log"

    async def call():
        from llm_query import run_claude_query

        output, _cost, _usage = await run_claude_query(
            prompt=prompt,
            context_files=[],
            ui=ui,
            role_name="native_replay_analysis",
            log_file_path=str(log_path),
            model="sonnet",
            tools=None,
        )
        return output

    try:
        return asyncio.run(asyncio.wait_for(call(), timeout=LLM_TIMEOUT))
    except Exception as exc:
        kind = _classify_llm_error(exc)
        _log_llm_failure("Native replay LLM failed (%s): %s", kind, exc)
        return None


def get_battle_experience(source_bot: str = "") -> str:
    """Return current-identity native memory, or an empty prompt section."""

    digest = _current_identity_digest()
    if digest is None or (source_bot and not _strict_bot(source_bot)):
        return ""
    return battle_memory.format_battle_memory_for_master(
        expected_evaluation_identity_digest=digest,
        paths=_memory_paths(),
        source_bot=source_bot,
        max_lessons=8,
        max_pending=6,
        max_evidence=8,
    )


__all__ = ["get_battle_experience", "start_experience_thread"]
