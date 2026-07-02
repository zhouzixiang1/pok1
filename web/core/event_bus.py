"""Unified event bus — single ``emit()`` entry enforcing a correlation schema.

This is the backbone of the log-system redesign (Phase 0). It coexists with
``system_log.log_system_event`` (now a thin forwarding shim) via dual-write:

    caller → system_log.log_system_event(type, sev, msg, data)   # 161 existing sites
                 ↓ forwards
           event_bus.emit(type, sev, msg, **data)
                 ↓ resolves correlation (run_id/stage/attempt) + normalises severity
           _dispatch(event)
                 ↓ dual-write
           ├─ events.jsonl          (new authoritative schema, with correlation)
           └─ system_log._write_system_event_raw  (legacy system_events.jsonl + SSE)

The six audited root causes are eliminated *structurally* at this entry point:

  - RC1 (success-path asymmetry): ``success()``/``failure()`` semantic family +
    dev-mode ``POK_LOG_STRICT`` hard-asserts + Phase-1 return-site assertions.
  - RC2 (zero attempt/stage): ``_resolve_context()`` reads them from
    ``pipeline_state.json`` automatically — callers never have to pass them.
  - RC3 (noise): scheduler/webui level-gating + SSE level-priority (Phase 2).
  - RC4 (parse collapse): ``failure_mode`` is a first-class kwarg.
  - RC5 (worker_failures pollution): ``category`` is required.
  - RC6 (no correlation): ``run_id`` (``v{next_v}#{generation_attempt}``) +
    ``pid``/``proc`` injected into every event; same key joins app.log +
    events.jsonl + role-IO across all three processes.

Correlation across process/thread boundaries (the blind spot all three design
candidates shared):

  - In-process (orchestrator asyncio.gather): contextvars (tasks copy context).
  - Long-lived worker threads (battle_experience._experience_loop,
    qd_async_eval._qd_eval_worker): deliberately NOT pinned — they span many
    generations, so per-event ``_resolve_context()`` reads the *live* checkpoint
    to get the current generation's run_id. Pinning would freeze a stale id.
  - daemon subprocess (subprocess.Popen): ``daemon_management`` injects
    ``POK_PROC=daemon``; daemon events fall back to checkpoint too.
  - checkpoint-clear race: ``_last_known`` survives ``clear_pipeline_checkpoint``
    (commit/orchestrator_session), so events emitted in the window still resolve.
  - High-frequency daemon events: 500ms checkpoint TTL cache avoids per-event fcntl.

See ~/.claude/plans/luminous-crafting-cocoa.md for the full design.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import threading
import time

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

#: Severity values the frontend hard-requires (SystemLogTab SEVERITY_CONFIG /
#: api/types.ts SystemEvent.severity). Anything else would fall back to "info"
#: and render the wrong colour, so we normalise at the write site.
VALID_SEVERITY = frozenset({"info", "warn", "error", "success"})

#: Map legacy/non-standard severities to the canonical 4. Anything unmapped
#: defaults to "error" (safe — never silently becomes invisible "info").
_SEVERITY_MAP = {
    "warning": "warn",
    "critical": "error",
    "fatal": "error",
    "ok": "success",
    "done": "success",
    "pass": "success",
    "passed": "success",
    "fail": "error",
    "failed": "error",
    "err": "error",
}

#: Dev/CI hard-assert. When set, emit() refuses bad category/severity — promotes
#: "emit success on every return" / "category required" from convention to a
#: fail-fast check caught at startup. (All three design candidates shared the
#: weakness that their enforcement was too soft; this is the structural fix.)
_STRICT = os.environ.get("POK_LOG_STRICT") == "1"

_PID = os.getpid()
_PROC: str | None = None  # lazily detected, cached

# ─────────────────────────────────────────────────────────────────────────────
# Correlation state
# ─────────────────────────────────────────────────────────────────────────────

# Valid within one asyncio task or a thread that explicitly applied context.
# asyncio.create_task copies the parent context, so orchestrator's gather-based
# worker tasks inherit the bound run_id/stage automatically.
_run_id_cv: contextvars.ContextVar = contextvars.ContextVar("event_bus.run_id", default=None)
_stage_cv: contextvars.ContextVar = contextvars.ContextVar("event_bus.stage", default=None)
_attempt_cv: contextvars.ContextVar = contextvars.ContextVar("event_bus.attempt", default=None)

#: Survives clear_pipeline_checkpoint(). Updated on every bind() / apply_context()
#: / update_last_known(), so the window after a commit (checkpoint == None) still
#: resolves the just-finished generation's run_id rather than dropping to "-".
_last_known: dict = {"run_id": None, "stage": None, "attempt": None}
_last_known_lock = threading.Lock()

#: 500ms TTL cache of pipeline_state.json to keep high-frequency daemon events
#: off the fcntl path. Invalidated naturally by TTL — stage/attempt change
#: infrequently and a generation switch updates next_v in the checkpoint.
_ckpt_cache: dict = {"ts": 0.0, "data": None}
_ckpt_cache_lock = threading.Lock()
_CKPT_TTL_SEC = 0.5

#: Module-level path so conftest can redirect it to tmp (mirrors the existing
#: SYSTEM_EVENTS_FILE / WORKER_FAILURES_FILE pattern). Resolved lazily to avoid
#: importing evolution_infra at module-load time (lets tests import event_bus
#: without the full results dir wired up).
EVENTS_FILE = None


def _events_file():
    global EVENTS_FILE
    if EVENTS_FILE is None:
        try:
            from evolution_infra import RESULTS_DIR
            EVENTS_FILE = RESULTS_DIR / "events.jsonl"
        except Exception:
            return None
    return EVENTS_FILE


# ─────────────────────────────────────────────────────────────────────────────
# Process identity
# ─────────────────────────────────────────────────────────────────────────────

def _detect_proc() -> str:
    """Identify the running process for the ``proc`` field (RC6).

    Priority: explicit ``POK_PROC`` env (daemon_management injects this for the
    daemon subprocess) → entry-script name → default ``orchestrator``.
    """
    env_proc = os.environ.get("POK_PROC")
    if env_proc:
        return env_proc
    import sys
    argv0 = (sys.argv[0] or "").lower()
    # entry scripts: elo_daemon.py / orchestrator.py / main.py (uvicorn web)
    if "elo_daemon" in argv0:
        return "daemon"
    if "orchestrator" in argv0:
        return "orchestrator"
    if "main.py" in argv0 or "uvicorn" in argv0:
        return "web"
    return "orchestrator"


def current_proc() -> str:
    global _PROC
    if _PROC is None:
        _PROC = _detect_proc()
    return _PROC


# ─────────────────────────────────────────────────────────────────────────────
# Context resolution
# ─────────────────────────────────────────────────────────────────────────────

def _read_ckpt_nolock() -> dict:
    """Read pipeline_state.json WITHOUT fcntl (best-effort correlation fallback).

    The checkpoint is written atomically (tmp + fsync + os.replace in
    write_pipeline_checkpoint), so a plain read never observes a torn file. We
    deliberately do NOT take LOCK_SH here: emit() can be reached from inside
    write_pipeline_checkpoint's LOCK_EX scope (log_system_event is invoked
    mid-write by the pipeline), and fcntl.flock is per-process — a nested
    LOCK_SH request on the same file self-deadlocks (EX blocks SH even within
    a single process). That deadlock surfaced as 30s pytest timeouts in
    test_mcp_pipeline / test_precommit_attempt_checkpoint during Phase 0.
    """
    try:
        from evolution_infra import PIPELINE_STATE_FILE
        with open(PIPELINE_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _read_ckpt_cached() -> dict:
    """Read pipeline_state.json with a 500ms TTL cache (thread-safe)."""
    now = time.monotonic()
    with _ckpt_cache_lock:
        if now - _ckpt_cache["ts"] > _CKPT_TTL_SEC:
            _ckpt_cache["data"] = _read_ckpt_nolock()
            _ckpt_cache["ts"] = now
        return _ckpt_cache["data"] or {}


def invalidate_ckpt_cache():
    """Force the next _read_ckpt_cached() to re-read (call after a stage/commit)."""
    with _ckpt_cache_lock:
        _ckpt_cache["ts"] = 0.0


def _resolve_context():
    """Resolve (run_id, stage, attempt), never raising.

    Order: explicit contextvar → _last_known (survives checkpoint clear) →
    pipeline_state.json (authoritative source, 500ms cached). Callers that pass
    nothing still get correct correlation — this is what makes RC2/RC6 vanish
    structurally rather than depending on every call site remembering to pass keys.
    """
    run_id = _run_id_cv.get()
    stage = _stage_cv.get()
    attempt = _attempt_cv.get()
    if run_id is None:
        run_id = _last_known["run_id"]
    if stage is None:
        stage = _last_known["stage"]
    if attempt is None:
        attempt = _last_known["attempt"]

    if run_id is None or stage is None:
        ckpt = _read_ckpt_cached()
        nxt = ckpt.get("next_v")
        if nxt is not None:
            gen_a = ckpt.get("generation_attempt", 0)
            # Composite key: same next_v across N retries shows up as an
            # increasing-attempt wall under `grep run_id` — deadloops/abandon
            # storms become visible at a glance (RC2).
            if run_id is None:
                run_id = f"{nxt}#{gen_a}"
            if stage is None:
                stage = ckpt.get("stage")
            if attempt is None:
                attempt = {
                    "generation": gen_a,
                    "audit": ckpt.get("audit_attempt", 0),
                    "precommit": ckpt.get("precommit_attempt", 0),
                }
            if run_id is not None or stage is not None or attempt is not None:
                update_last_known(run_id=run_id, stage=stage, attempt=attempt)
    return run_id, stage, attempt


def update_last_known(*, run_id=None, stage=None, attempt=None):
    """Refresh the last-known correlation cache.

    Called by bind() and apply_context(); also safe to call directly at pipeline
    stage entry to ensure the value survives a later checkpoint clear.
    """
    with _last_known_lock:
        if run_id is not None:
            _last_known["run_id"] = run_id
        if stage is not None:
            _last_known["stage"] = stage
        if attempt is not None:
            _last_known["attempt"] = attempt


# ─────────────────────────────────────────────────────────────────────────────
# Severity normalisation
# ─────────────────────────────────────────────────────────────────────────────

def _normalise_severity(severity: str) -> tuple[str, bool]:
    """Return (canonical_severity, was_remapped). Never raises."""
    if severity in VALID_SEVERITY:
        return severity, False
    mapped = _SEVERITY_MAP.get(str(severity).lower())
    if mapped is not None:
        return mapped, True
    return "error", True  # unknown severity → error (visible), never invisible info


# ─────────────────────────────────────────────────────────────────────────────
# emit() — the single entry point
# ─────────────────────────────────────────────────────────────────────────────

def emit(category, severity, message, *, stage=None, attempt=None, run_id=None,
         failure_mode=None, **fields):
    """Emit one structured event.

    Args:
        category: dotted event type, e.g. ``"pipeline.master_plan_accepted"``.
            The prefix (``pipeline.``/``orchestrator.``/``daemon.``/``bot.``) is
            what the frontend groups by — must contain a ``.`` (RC5).
        severity: one of info/warn/error/success. Legacy values (warning/critical/…)
            are normalised; the original is preserved in ``data.severity_raw``.
        message: human-readable one-liner.
        stage/attempt/run_id: optional explicit override; otherwise auto-resolved.
        failure_mode: NO_JSON/NO_FENCE/PARSE_ERROR/OK for parse-failure events (RC4).
        **fields: arbitrary business payload (next_v, worker_id, cost, …).

    Dual-writes to events.jsonl (new schema) and system_events.jsonl (legacy,
    via system_log._write_system_event_raw, which also drives SSE).
    """
    if _STRICT:
        assert category and "." in category, f"event_bus.emit: bad category {category!r}"
        assert severity in VALID_SEVERITY, (
            f"event_bus.emit: severity {severity!r} not in {sorted(VALID_SEVERITY)}")

    sev_canon, remapped = _normalise_severity(severity)
    rid, ctx_stage, ctx_attempt = _resolve_context()
    payload_v = fields.get("next_v")
    if payload_v is None:
        payload_v = fields.get("version")
    if payload_v is None:
        payload_v = fields.get("target_v")
    payload_run_id = None
    if run_id is None and rid is None and payload_v is not None:
        gen_attempt = fields.get("generation_attempt")
        if gen_attempt is None and isinstance(ctx_attempt, dict):
            gen_attempt = ctx_attempt.get("generation", 0)
        try:
            gen_attempt = int(gen_attempt or 0)
        except Exception:
            gen_attempt = 0
        payload_run_id = f"{payload_v}#{gen_attempt}"

    emitter_proc = current_proc()
    data = {
        **fields,
        "category": category,
        "stage": stage if stage is not None else ctx_stage,
        "attempt": attempt if attempt is not None else ctx_attempt,
        "run_id": run_id if run_id is not None else (payload_run_id or rid),
        "emitter_pid": _PID,
        "emitter_ppid": os.getppid(),
        "emitter_proc": emitter_proc,
    }
    # Preserve caller-supplied business identity. Daemon lifecycle events often
    # use pid/proc for the target daemon; overwriting those with the web process
    # made stop/crash logs actively misleading. For old callers that do not pass
    # pid/proc, keep the legacy data.pid/data.proc fields as emitter identity.
    data.setdefault("pid", _PID)
    data.setdefault("proc", emitter_proc)
    if failure_mode is not None:
        data["failure_mode"] = failure_mode
    if remapped:
        data["severity_raw"] = severity

    event = {
        "ts": time.time(),
        "type": category,
        "severity": sev_canon,
        "message": message,
        "data": data,
    }
    _dispatch(event)


def _dispatch(event):
    """Dual-write one event: events.jsonl (new) + legacy system_events.jsonl + SSE."""
    # Write 1 — new authoritative schema
    try:
        from evolution_infra import append_locked_jsonl
        path = _events_file()
        if path is not None:
            append_locked_jsonl(path, event)
    except Exception:
        # Never let logging crash the pipeline — mirror system_log's tolerance.
        pass
    # Write 2 — legacy system_events.jsonl + SSE broadcast. Goes through the raw
    # writer (NOT log_system_event) to avoid a re-entrant loop back into emit().
    try:
        from system_log import _write_system_event_raw
        _write_system_event_raw(
            event["type"], event["severity"], event["message"], event["data"])
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Semantic family (success/failure symmetry — RC1)
# ─────────────────────────────────────────────────────────────────────────────
#
# These are ordinary functions — the *structural* enforcement of "every master
# return emits exactly one success/failure" comes from (a) POK_LOG_STRICT asserts
# and (b) Phase-1 return-site assertions + tests, not from these names existing.
# (The span-based candidate's __exit__-auto-success was rejected as "fake
# success": __exit__ cannot distinguish clean return / degraded return / raise.)

def success(category, message, **fields):
    emit(category, "success", message, **fields)


def failure(category, message, *, failure_mode=None, **fields):
    emit(category, "error", message, failure_mode=failure_mode, **fields)


def progress(category, message, **fields):
    emit(category, "info", message, **fields)


def warn(category, message, **fields):
    emit(category, "warn", message, **fields)


# ─────────────────────────────────────────────────────────────────────────────
# Scope binding (in-process) and cross-thread/cross-process context handoff
# ─────────────────────────────────────────────────────────────────────────────

@contextlib.contextmanager
def bind(*, run_id=None, stage=None, attempt=None):
    """Bind correlation keys for the current task/thread scope.

    Use at pipeline stage entry::

        with event_bus.bind(run_id=f"{next_v}#{gen_attempt}", stage="master_planned"):
            plan = await run_master(...)

    Also updates ``_last_known`` so the value survives a checkpoint clear.
    Resets the contextvars on exit; ``_last_known`` is intentionally kept
    (it tracks the most-recent correlation, which is the right fallback).
    """
    tokens = []
    if run_id is not None:
        tokens.append((_run_id_cv, _run_id_cv.set(run_id)))
    if stage is not None:
        tokens.append((_stage_cv, _stage_cv.set(stage)))
    if attempt is not None:
        tokens.append((_attempt_cv, _attempt_cv.set(attempt)))
    update_last_known(run_id=run_id, stage=stage, attempt=attempt)
    invalidate_ckpt_cache()
    try:
        yield
    finally:
        for cv, tok in reversed(tokens):
            try:
                cv.reset(tok)
            except Exception:
                pass


def capture_context() -> dict:
    """Snapshot the current correlation context for handoff to a thread/process.

    contextvars do NOT cross ``subprocess.Popen`` / ``ProcessPoolExecutor(spawn)``
    / ``threading.Thread`` boundaries. Pass the captured dict explicitly and call
    ``apply_context()`` at the worker entry. Use the same fallback chain as
    ``emit()`` so role-IO appenders and other non-event logs keep the same
    checkpoint-derived run_id/stage correlation.
    """
    run_id, stage, attempt = _resolve_context()
    return {
        "run_id": run_id,
        "stage": stage,
        "attempt": attempt,
    }


def apply_context(ctx):
    """Apply a captured context at a worker thread/process entry."""
    if not ctx:
        return
    rid = ctx.get("run_id")
    stg = ctx.get("stage")
    att = ctx.get("attempt")
    if rid is not None:
        _run_id_cv.set(rid)
    if stg is not None:
        _stage_cv.set(stg)
    if att is not None:
        _attempt_cv.set(att)
    update_last_known(run_id=rid, stage=stg, attempt=att)


# ─────────────────────────────────────────────────────────────────────────────
# Test support
# ─────────────────────────────────────────────────────────────────────────────

def reset_for_test():
    """Clear all contextvars + caches for test isolation."""
    for cv in (_run_id_cv, _stage_cv, _attempt_cv):
        try:
            cv.set(None)
        except Exception:
            pass
    with _last_known_lock:
        _last_known["run_id"] = None
        _last_known["stage"] = None
        _last_known["attempt"] = None
    with _ckpt_cache_lock:
        _ckpt_cache["ts"] = 0.0
        _ckpt_cache["data"] = None
