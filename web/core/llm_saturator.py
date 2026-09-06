"""LLM saturator: a background workload that keeps LLM permits consumed.

The poker-evolution pipeline is bursty (short LLM bursts during
master/workers, then long native windows during precommit/gates) and, on this
cloud runtime, also fragile (a long tail of stall edge cases). That leaves the
GLM provider under-utilized: historical peak was ~1.6M tok/h, day-average often
near zero.

The saturator decouples raw LLM consumption from the pipeline. It is a
long-running background task (started from the app lifespan, NOT the
orchestrator) that, whenever the global LLM semaphore has a free permit,
launches a **bounded packet** (not a 60-turn essay). Three job kinds rotate
so more work finishes before the next Master burst, and so a preemption
throws away a small packet instead of a half-hour duel. Consumption is
still dominated by per-turn cache re-reads; the lever is *completed*
packets, not unfinished depth.

Launch is RAM-aware: extra ``claude`` children leftover from cancel (the
semaphore can read 4 while 5–6 processes still live) block new sessions
until they exit. Pipeline bursts batch-preempt the youngest packets rather
than waiting 30s per slot.

Gated by ``POK_LLM_SATURATOR_ENABLED`` (default off). Shares the single global
LLM semaphore via ``run_claude_query`` (so it never over-subscribes beyond
``POK_GLOBAL_LLM_CONCURRENCY``). New packets also wait on RAM (``claude``
child count and ``MemAvailable``). A queued pipeline role does not freeze
fill of idle permits; preemption yields only when the pool is full.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("pok.saturator")

SATURATOR_ENABLED = (
    os.environ.get("POK_LLM_SATURATOR_ENABLED", "0").strip().lower()
    in {"1", "true", "yes", "on"}
)


_PUBLISHED_TAG_CACHE: "tuple[float, set[int]] | None" = None


def _published_versions() -> "set[int]":
    """Versions carrying an annotated completion tag (cached 10 min).

    An in-flight draft's candidate dir (no completion tag yet) is NOT
    published evidence and must not be served to the saturator as a
    reference bot.
    """
    global _PUBLISHED_TAG_CACHE
    import subprocess

    now = time.time()
    if _PUBLISHED_TAG_CACHE and now - _PUBLISHED_TAG_CACHE[0] < 600:
        return _PUBLISHED_TAG_CACHE[1]
    versions: "set[int]" = set()
    try:
        from evolution_infra import BOTS_DIR

        root = Path(BOTS_DIR).parent
        out = subprocess.run(
            ["git", "tag", "--list", "national-cloud-bot-v*"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                versions.add(int(line.rsplit("-v", 1)[1]))
            except (ValueError, IndexError):
                continue
    except Exception:
        pass
    _PUBLISHED_TAG_CACHE = (now, versions)
    return versions


def _published_bot_dirs() -> "list[Path]":
    """Published canonical bot dirs, newest version first."""
    try:
        from evolution_infra import BOTS_DIR

        published = _published_versions()
        bots = Path(BOTS_DIR)
        found = []
        for d in bots.iterdir():
            if not d.is_dir() or not d.name.startswith("national_cloud_v"):
                continue
            try:
                v = int(d.name.rsplit("_v", 1)[1])
            except (ValueError, IndexError):
                continue
            if v in published and (d / "policy.py").exists():
                found.append((v, d))
        found.sort(key=lambda t: t[0], reverse=True)
        return [d for _, d in found]
    except Exception:
        return []


def _saturator_bots(session_id: int, limit: int = 2) -> "list[Path]":
    """Pick this session's bot set: FOCUS bot first, then the newest other.

    The dispatch guard caps canonical_candidates read dirs at exactly 2, but
    files WITHIN each dir are unlimited — so the prompt mandates a full read
    of every file in both dirs (~90-110K tokens of base context). Consumption
    is dominated by per-turn cache re-reads as the analysis compounds, so turn
    count matters more than base size anyway.

    The focus pool is biased to the newest 4 published bots plus the v1
    bootstrap (the long-standing rank-1 selection parent): planning consumes
    findings via focus_v/opponent_v matching the next generation's source_v,
    which is almost always one of those — a uniform rotation over the whole
    published pool would spend most sessions on bots planning never reads
    back."""
    dirs = _published_bot_dirs()
    if not dirs:
        return []
    focus_pool = dirs[:4]
    versions = [_bot_version(d) for d in dirs]
    v1_index = next((i for i, v in enumerate(versions) if v == 1), None)
    if v1_index is not None and dirs[v1_index] not in focus_pool:
        focus_pool.append(dirs[v1_index])
    focus = focus_pool[session_id % len(focus_pool)]
    others = [d for d in dirs if d != focus][: max(0, limit - 1)]
    return [focus] + others


_SATURATOR_HARD_STOP = """
HARD STOP: after at most 18 Read tool calls, write the synthesis and end.
Do not pad, do not start extra phases, do not promise a follow-up. A finished
bounded packet is worth more than an unfinished 60-turn essay. Re-read code
before every citation; never cite from memory.
"""

_SATURATOR_PROMPT = """\
You are a senior heads-up no-limit poker researcher running a BOUNDED
two-bot matchup packet (FOCUS vs OPPONENT). This is one packet, not a
marathon.

Phase 1: Read policy.py in BOTH directories (whole file). Skim precompute.py
only if policy.py imports it. Do not reread the same file without a new
question.

Phase 2: Name the three largest strategic asymmetries (preflop, flop c-bet,
river). Quote exact functions. Which FOCUS lines does THIS opponent punish?

Phase 3: Walk 6 concrete hands (specify holes + board; mix button/blind and
deep/short). Trace both bots through code.

Phase 4 — synthesis: verdict + 5 localized FOCUS policy.py refinements
(function, weakness, change, EV, risk).
""" + _SATURATOR_HARD_STOP

_SATURATOR_LINE_AUDIT_PROMPT = """\
You are auditing ONE published FOCUS bot (no opponent). Bounded packet.

Phase 1: Read its policy.py fully. Map one street only — pick preflop if
session depth is even, flop otherwise.

Phase 2: List every function that can emit fold/pass/allin/raise for that
street. Quote the legality / sizing / fallback path.

Phase 3: Find 4 concrete bugs or EV leaks (too-tight fold, illegal-intent
risk, unused opponent field, sizing that never hits a legal raise_to).

Phase 4 — synthesis: 5 localized patches (function, leak, change, EV, risk).
""" + _SATURATOR_HARD_STOP

_SATURATOR_FUNCTION_TRACE_PROMPT = """\
You are tracing the FOCUS bot's decision spine. Bounded packet.

Phase 1: Read policy.py. Locate the ABI entry (the function that receives
decision_context and returns fold/pass/allin/raise).

Phase 2: Trace three named helpers that actually change the returned intent
(typical names: a dispatcher, a raise builder, an opponent weight). For each:
inputs, branch table, what happens on missing tracker fields.

Phase 3: Show one hand where helper A and helper B disagree, and which one
wins.

Phase 4 — synthesis: 4 localized patches that make the spine consistent.
""" + _SATURATOR_HARD_STOP


def saturator_job_for(session_id: int) -> dict[str, object]:
    """Rotate bounded packets so more work finishes per occupancy-hour."""
    jobs = (
        ("matchup_packet", _SATURATOR_PROMPT, 2),
        ("line_audit", _SATURATOR_LINE_AUDIT_PROMPT, 1),
        ("function_trace", _SATURATOR_FUNCTION_TRACE_PROMPT, 1),
    )
    name, prompt, bot_limit = jobs[int(session_id) % len(jobs)]
    return {"name": name, "prompt": prompt, "bot_limit": int(bot_limit)}


def _saturator_producer(renderer_inputs):
    """Render the saturator prompt into a typed LLMRenderedMaterial.

    The provider boundary requires a sealed RenderedLLMPrompt (not a raw
    string); ``render_llm_prompt`` calls this producer and signs the output.
    """
    from llm_role_dispatch import LLMRenderedMaterial

    return LLMRenderedMaterial(
        text=str(renderer_inputs.get("prompt", "")),
        evidence_kind="none",
        evidence_provenance={},
    )


def _register_saturator_role() -> None:
    """Register the saturator role contract at runtime (no semantic-file edit).

    The role registry lives in llm_query.py (a semantic path we must not edit
    without an identity re-init). Instead we append the saturator contract to
    the runtime tuple at import time. The contract's evidence_provenance_kind
    is ``none`` to match the trivial producer above. Read tools + requires_read_scope
    enable deep MULTI-TURN analysis (the agent reads across many turns, context
    compounds, driving high token consumption).
    """
    try:
        import llm_query as _lq
        if getattr(_lq, "_saturator_role_registered", False):
            return
        contract = _lq._llm_role_contract(
            "llm_saturator",
            r"^SATURATOR(?:\s|$)",
            renderer="llm_saturator.py::_SATURATOR_PROMPT",
            producer_file="web/core/llm_saturator.py",
            producer_name="_saturator_producer",
            template_paths=(),
            evidence_kind="none",
            scope_policy="canonical_candidates",
            tools=(("Read",),),
            read_scope="explicit_candidate_and_generation_snapshot_dirs",
            write_scope="none",
            evidence_policy="system_bound_prompt_only",
            history_policy="forbidden",
            requires_read_scope=True,
            allows_context_files=False,
        )
        _lq.ACTIVE_LLM_ROLE_CONTRACTS = _lq.ACTIVE_LLM_ROLE_CONTRACTS + (contract,)
        _lq._saturator_role_registered = True
    except Exception as e:
        log.warning("saturator role registration failed: %s", e)


_register_saturator_role()


SATURATOR_ROLE = "SATURATOR STRATEGY RESEARCH"


def _saturator_read_dirs(bots: "list[Path]") -> list:
    """Read scope for the multi-turn analysis: the selected published bot dirs."""
    dirs = []
    try:
        for b in bots:
            dirs.append(str(b.resolve()))
    except Exception:
        pass
    return dirs


def _usage_tokens(usage) -> int:
    """Full consumption tokens (in + cache read/write + out) from a usage blob."""
    try:
        data = usage if isinstance(usage, dict) else usage.model_dump()
    except Exception:
        return 0
    if not isinstance(data, dict):
        return 0
    return sum(
        int(data.get(k) or 0)
        for k in (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        )
    )


#: Bounded excerpt of a duel report consumed by generation planning. The
#: Phase-4 synthesis (verdict + localized refinements with function/line,
#: EV reasoning, risk) is the highest-value part; without a marker the tail
#: (final synthesis) is used.
_FINDINGS_MAX_CHARS = 6000


def _extract_findings_text(output: str) -> str:
    text = str(output or "")
    idx = text.find("Phase 4")
    if idx >= 0:
        text = text[idx:]
    return text[:_FINDINGS_MAX_CHARS].strip()


def _bot_version(bot_dir: Path | None) -> int | None:
    if bot_dir is None:
        return None
    try:
        return int(bot_dir.name.rsplit("_v", 1)[1])
    except (ValueError, IndexError):
        return None


def _append_findings_record(record: dict) -> None:
    """Persist one duel-findings record (the consumption half of the loop).

    ``generation_scheduler`` reads these records at prepare time and renders
    a bounded advisory block into the (otherwise empty) master-context
    ``match_analysis`` slot, so every deep duel session's Phase-4 output is
    consumed by the next generation's Master planning instead of decaying in
    ``session_*.txt``. Each record carries its own digests for traceability.
    """
    import hashlib
    import json as _json

    from evolution_infra import RESULTS_DIR, locked_file

    path = Path(RESULTS_DIR) / "saturator" / "findings.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with locked_file(path, "a", encoding="utf-8") as f:
        f.write(_json.dumps(record, ensure_ascii=False) + "\n")


async def _one_saturator_session(session_id: int) -> dict:
    """Run one bounded packet. Returns a small result summary."""
    from llm_query import run_claude_query, render_llm_prompt
    from tool_helpers import ToolUI
    from evolution_infra import RESULTS_DIR

    job = saturator_job_for(session_id)
    bots = _saturator_bots(session_id, limit=int(job["bot_limit"]))
    focus = bots[0] if bots else None
    prompt = str(job["prompt"])
    if bots:
        listing = "\n".join(f"  - {b.resolve()}" for b in bots)
        prompt = (
            prompt
            + f"\n\nJob: {job['name']}\n"
            + f"FOCUS bot: {focus.resolve()}\n"
            + f"Bot directories ({len(bots)}):\n{listing}\n"
        )
    read_dirs = _saturator_read_dirs(bots)
    # No context_files (contract forbids them): the agent Reads policy.py etc.
    # itself via the Read tool across multiple turns (context compounds).
    context_files = []
    ui = ToolUI()
    log_dir = Path(RESULTS_DIR) / "saturator"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"session_{session_id:05d}.txt"
    t0 = time.time()
    try:
        rendered = render_llm_prompt(
            SATURATOR_ROLE,
            producer=_saturator_producer,
            renderer_inputs={"prompt": prompt},
        )
        output, _cost, usage = await run_claude_query(
            rendered,
            context_files,
            ui,
            SATURATOR_ROLE,
            str(log_file),
            tools=["Read"],  # match the contract's allowed Read tool set
            allowed_read_dirs=read_dirs,
        )
        out_len = len(output or "")
        tokens = _usage_tokens(usage)
        log.info(
            "saturator session %d job=%s done: %d chars, %d tokens in %.0fs (focus=%s, bots=%d)",
            session_id, job["name"], out_len, tokens, time.time() - t0,
            focus.name if focus else "none", len(bots),
        )
        if out_len and focus is not None:
            # Consumption loop: persist the bounded Phase-4 findings so the
            # next generation's Master planning can target proven weaknesses.
            try:
                import hashlib

                _append_findings_record({
                    "schema_version": 1,
                    "ts": time.time(),
                    "focus_v": _bot_version(focus),
                    "opponent_v": _bot_version(bots[1]) if len(bots) > 1 else None,
                    "focus_bot": focus.name,
                    "opponent_bot": bots[1].name if len(bots) > 1 else None,
                    "session_file": str(log_file),
                    "report_sha256": hashlib.sha256(
                        (output or "").encode("utf-8")
                    ).hexdigest(),
                    "tokens": tokens,
                    "findings_text": _extract_findings_text(output),
                })
            except Exception as e:
                log.warning("saturator findings append failed: %s", e)
        return {
            "session": session_id, "chars": out_len,
            "tokens": tokens, "ok": True,
        }
    except Exception as e:
        _log_session_failure(session_id, e)
        _note_saturator_provider_failure(e)
        return {"session": session_id, "ok": False, "error": str(e)[:200]}


def _housekeep_session_files(
    log_dir: Path,
    *,
    keep_sessions: int = 400,
    lock_max_age_sec: float = 4 * 3600.0,
) -> None:
    """Bound the saturator's on-disk footprint (unattended-operation hygiene).

    Two growth sources, both observed in production: every session leaves a
    ``session_NNN.txt.lock`` sidecar that the shared ``locked_file`` util
    (semantic, not editable here) never removes (2,143 leaked locks in two
    days), and session transcripts themselves accumulate write-only forever
    (findings.jsonl is the consumed artifact; transcripts are raw logs).
    Sessions cap at POK_LLM_SATURATOR_TOTAL_TIMEOUT (7200s), so a lock older
    than 4h can never be held. Failures here must never kill the loop."""
    import time as _time

    try:
        now = _time.time()
        sessions = sorted(
            (p for p in log_dir.glob("session_*.txt") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for stale in sessions[keep_sessions:]:
            try:
                stale.unlink()
            except OSError:
                pass
        for lock in log_dir.glob("session_*.txt.lock"):
            try:
                if now - lock.stat().st_mtime > lock_max_age_sec:
                    lock.unlink()
            except OSError:
                pass
    except Exception as e:
        log.warning("saturator housekeeping failed: %s", e)


# Quota/availability backoff: during a GLM 5h quota window every launched
# session fails within ~1s, and the fill loop relaunched thousands of
# dead sessions per window (2026-08-16 23:06: sessions 3604-3615 in seconds).
# The pipeline has its own durable availability pause; the saturator only
# needs to stop burning attempts while the provider is closed.
_QUOTA_PAUSE_SECONDS = 600.0
_FAIL_PAUSE_CAP_SECONDS = 30.0
_quota_pause_until: float = 0.0
_fail_pause_until: float = 0.0
_fail_streak: int = 0
_fail_log_at: float = 0.0
_fail_unlogged: int = 0


def _log_session_failure(session_id: int, error: object) -> None:
    """Rate-limit the per-session warning so a tight fail loop cannot flood journald."""
    global _fail_log_at, _fail_unlogged
    now = time.time()
    _fail_unlogged += 1
    if now - _fail_log_at >= 15.0:
        extra = f" (+{_fail_unlogged - 1} similar)" if _fail_unlogged > 1 else ""
        log.warning("saturator session %d failed: %s%s", session_id, error, extra)
        _fail_log_at = now
        _fail_unlogged = 0


def _note_saturator_provider_failure(error: object) -> None:
    """Pause saturator launches after a quota/availability-class failure."""
    global _quota_pause_until
    text = str(error or "").lower()
    if "quota" in text or "429" in text or "unavailable" in text:
        _quota_pause_until = max(_quota_pause_until, time.time() + _QUOTA_PAUSE_SECONDS)
        log.warning(
            "saturator pausing launches for %.0fs after provider "
            "quota/availability failure", _QUOTA_PAUSE_SECONDS,
        )


def _note_saturator_launch_success() -> None:
    global _fail_streak
    _fail_streak = 0


def _note_saturator_launch_failure(error: object | None = None) -> None:
    """Backoff after any failed packet so a 0.5s refill cannot spin thousands of sessions.

    v298 logged 2000+ ``different event loop`` failures after each cancelled
    packet completed instantly and the fill loop treated that as a free slot.
    """
    global _fail_pause_until, _fail_streak
    _fail_streak += 1
    if error is not None:
        _note_saturator_provider_failure(error)
    delay = min(_FAIL_PAUSE_CAP_SECONDS, 2.0 * (2 ** min(_fail_streak - 1, 4)))
    _fail_pause_until = max(_fail_pause_until, time.time() + delay)


def _saturator_pause_remaining_sec() -> float:
    until = max(_quota_pause_until, _fail_pause_until)
    return max(0.0, until - time.time())


def _saturator_provider_paused() -> bool:
    return _saturator_pause_remaining_sec() > 0.0


def _claude_child_count() -> int:
    """Live ``claude`` children of this process (RAM occupancy, not the semaphore)."""
    try:
        pid = os.getpid()
        children_path = Path(f"/proc/{pid}/task/{pid}/children")
        raw = children_path.read_text(encoding="utf-8") if children_path.is_file() else ""
        count = 0
        for token in raw.split():
            try:
                comm = Path(f"/proc/{token}/comm").read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if comm == "claude":
                count += 1
        return count
    except OSError:
        return 0


def _mem_available_mb() -> int | None:
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _preempt_after_sec() -> float:
    try:
        return max(15.0, float(os.environ.get("POK_LLM_SATURATOR_PREEMPT_AFTER_SEC", "45")))
    except (TypeError, ValueError):
        return 45.0


def _min_free_mb() -> int:
    try:
        return max(0, int(os.environ.get("POK_LLM_SATURATOR_MIN_FREE_MB", "512")))
    except (TypeError, ValueError):
        return 512


def saturator_may_launch(*, in_flight: int, soft_cap: int) -> tuple[bool, str]:
    """Admit a new packet when a permit and RAM exist.

    A queued pipeline role must NOT freeze launches: v298 sat at
    ``waiting=1`` with three idle permits for 11h because this gate used to
    return ``pipeline_pending``. Fill every free permit; preemption yields
    only when the pool is actually full.
    """
    if in_flight >= max(1, int(soft_cap)):
        return False, "soft_cap"
    if _saturator_provider_paused():
        return False, "provider_paused"
    try:
        from llm_concurrency import GLOBAL_LLM_CONCURRENCY, llm_semaphore_has_capacity
    except Exception:
        return True, "ok"
    if not llm_semaphore_has_capacity(1):
        return False, "no_permit"
    if _claude_child_count() >= int(GLOBAL_LLM_CONCURRENCY):
        return False, "claude_children"
    avail = _mem_available_mb()
    min_free = _min_free_mb()
    if avail is not None and min_free and avail < min_free:
        return False, "low_memory"
    return True, "ok"


def _preempt_cooldown_sec() -> float:
    try:
        return max(15.0, float(os.environ.get("POK_LLM_SATURATOR_PREEMPT_COOLDOWN_SEC", "90")))
    except (TypeError, ValueError):
        return 90.0


def saturator_preempt_n(
    *,
    waiting: int,
    pending_age_sec: float,
    has_capacity: bool,
    in_flight: int,
    last_preempt_at: float | None,
    now: float,
    min_pending_age_sec: float = 45.0,
    cooldown_sec: float = 90.0,
) -> int:
    """How many packets to cancel this tick so LLM occupancy stays high.

    Yield only when the pipeline is blocked on a full pool. One waiter must
    not drain every session across successive loops.
    """
    if waiting <= 0 or in_flight <= 0 or has_capacity:
        return 0
    if pending_age_sec < min_pending_age_sec:
        return 0
    if last_preempt_at is not None and (now - last_preempt_at) < cooldown_sec:
        return 0
    return min(int(waiting), int(in_flight))


def _task_is_live(task) -> bool:
    if task.done() or task.cancelled():
        return False
    cancelling = getattr(task, "cancelling", None)
    if callable(cancelling) and cancelling():
        return False
    return True


def _pick_preemptable_many(
    sessions: "dict[object, float]",
    pending_age_sec: float,
    n: int,
    *,
    min_pending_age_sec: float = 30.0,
) -> list:
    """Youngest-first victims. Empty until pipeline demand has persisted."""
    if pending_age_sec < min_pending_age_sec or not sessions or n <= 0:
        return []
    ordered = sorted(sessions, key=lambda task: sessions[task], reverse=True)
    return ordered[: int(n)]


def _pick_preemptable(
    sessions: "dict[object, float]",
    pending_age_sec: float,
    *,
    min_pending_age_sec: float = 30.0,
    now: float | None = None,
):
    """Pick the youngest in-flight session to cancel for pipeline preemption.

    Youngest = least invested (fewest tokens spent). Returns None when
    pipeline demand has not persisted long enough (transient queue churn
    must not cancel sessions) or when nothing is in flight."""
    del now
    picked = _pick_preemptable_many(
        sessions, pending_age_sec, 1, min_pending_age_sec=min_pending_age_sec
    )
    return picked[0] if picked else None


async def run_llm_saturator(shutdown_mgr=None) -> None:
    """Background loop: keep free LLM permits filled with bounded packets.

    Launch whenever a permit and RAM exist — a queued pipeline role does not
    freeze fill (that hole idled v298 for 11h). Preempt only when the pool
    is full, at most ``waiting`` packets, then cooldown so one waiter cannot
    drain every session.
    """
    if not SATURATOR_ENABLED:
        log.info("LLM saturator disabled (POK_LLM_SATURATOR_ENABLED not set)")
        return
    log.info("LLM saturator started — filling free LLM permits with bounded packets")
    session_id = 0
    in_flight: set = set()
    started_at: "dict[object, float]" = {}
    last_housekeep = 0.0
    last_preempt_at: float | None = None

    async def _launch(sid: int):
        try:
            result = await _one_saturator_session(sid)
        except Exception as e:
            log.warning("saturator task %d error: %s", sid, e)
            _note_saturator_launch_failure(e)
            return
        if isinstance(result, dict) and result.get("ok"):
            _note_saturator_launch_success()
            return
        err = result.get("error") if isinstance(result, dict) else None
        _note_saturator_launch_failure(err)

    try:
        while True:
            if shutdown_mgr is not None and getattr(shutdown_mgr, "is_shutting_down", False):
                break
            try:
                from llm_concurrency import (
                    llm_semaphore_has_capacity,
                    pipeline_pending_age_sec,
                    pipeline_pending_count,
                )

                _pending_age = pipeline_pending_age_sec()
                _pipeline_waiting = pipeline_pending_count()
                _has_capacity = bool(llm_semaphore_has_capacity(1))
            except Exception:
                _pending_age = 0.0
                _pipeline_waiting = 0
                _has_capacity = True
            in_flight = {t for t in in_flight if not t.done()}
            started_at = {t: ts for t, ts in started_at.items() if t in in_flight}
            live_tasks = {t for t in in_flight if _task_is_live(t)}
            n_preempt = saturator_preempt_n(
                waiting=_pipeline_waiting,
                pending_age_sec=_pending_age,
                has_capacity=_has_capacity,
                in_flight=len(live_tasks),
                last_preempt_at=last_preempt_at,
                now=time.time(),
                min_pending_age_sec=_preempt_after_sec(),
                cooldown_sec=_preempt_cooldown_sec(),
            )
            if n_preempt:
                victims = _pick_preemptable_many(
                    {t: started_at[t] for t in live_tasks if t in started_at},
                    _pending_age,
                    n_preempt,
                    min_pending_age_sec=0.0,
                )
                for victim in victims:
                    log.warning(
                        "saturator preempting session %s after %.0fs of "
                        "pipeline queue demand (in-flight=%d waiting=%d n=%d)",
                        getattr(victim, "get_name", lambda: "?")(),
                        _pending_age,
                        len(live_tasks),
                        _pipeline_waiting,
                        n_preempt,
                    )
                    victim.cancel()
                last_preempt_at = time.time()
                if victims:
                    await asyncio.wait(set(victims), timeout=2.0)
            in_flight = {t for t in in_flight if not t.done()}
            started_at = {t: ts for t, ts in started_at.items() if t in in_flight}
            live_n = sum(1 for t in in_flight if _task_is_live(t))
            if time.time() - last_housekeep > 600:
                last_housekeep = time.time()
                from evolution_infra import RESULTS_DIR

                _housekeep_session_files(
                    Path(RESULTS_DIR) / "saturator"
                )
            try:
                from llm_concurrency import GLOBAL_LLM_CONCURRENCY

                soft_cap = min(
                    max(1, int(os.environ.get("POK_LLM_SATURATOR_MAX_INFLIGHT", "4"))),
                    int(GLOBAL_LLM_CONCURRENCY),
                )
            except Exception:
                soft_cap = max(1, int(os.environ.get("POK_LLM_SATURATOR_MAX_INFLIGHT", "4")))
            while True:
                in_flight = {t for t in in_flight if not t.done()}
                started_at = {t: ts for t, ts in started_at.items() if t in in_flight}
                live_n = sum(1 for t in in_flight if _task_is_live(t))
                if _saturator_provider_paused():
                    break
                ok, reason = saturator_may_launch(
                    in_flight=live_n, soft_cap=soft_cap
                )
                if not ok:
                    break
                if shutdown_mgr is not None and getattr(shutdown_mgr, "is_shutting_down", False):
                    break
                session_id += 1
                t = asyncio.create_task(_launch(session_id), name=f"saturator-{session_id}")
                in_flight.add(t)
                started_at[t] = time.time()
                live_n += 1
                await asyncio.sleep(0.5)
            if _saturator_provider_paused():
                await asyncio.sleep(min(max(_saturator_pause_remaining_sec(), 0.5), 60.0))
            elif not in_flight:
                await asyncio.sleep(5.0)
            else:
                try:
                    await asyncio.wait(
                        in_flight, return_when=asyncio.FIRST_COMPLETED, timeout=15.0
                    )
                except asyncio.TimeoutError:
                    pass
    except asyncio.CancelledError:
        log.info("LLM saturator cancelled")
        raise
    finally:
        for t in in_flight:
            t.cancel()
        log.info("LLM saturator stopped")


__all__ = [
    "run_llm_saturator",
    "SATURATOR_ENABLED",
    "saturator_job_for",
    "saturator_may_launch",
    "saturator_preempt_n",
]
