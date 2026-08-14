"""LLM role-IO logging, observability, timeout policy, and thinking options.

Extracted from llm_query.py as a single business responsibility: emit LLM
events, append/rotate role-IO logs, derive role-log metadata, and resolve
per-role timeout + extended-thinking policies.

All public symbols are re-exported by llm_query.py for backward compatibility.
"""

import contextlib  # noqa: F401  (preserved verbatim from llm_query.py)
import json  # noqa: F401  (preserved verbatim from llm_query.py)
import math
import os
import re
import threading
import time  # noqa: F401  (preserved verbatim from llm_query.py)
from pathlib import Path  # noqa: F401  (preserved verbatim from llm_query.py)

# NOTE: ``llm_query`` imports this module at its own top level. To avoid a
# circular import, we do NOT ``import llm_query`` at module scope here. Each
# function that needs to read an env-overridable / monkeypatchable constant
# through the ``llm_query`` namespace (so test patches take effect) imports it
# lazily.


# Serialize role-IO log rotation across threads/processes. Without this lock, two concurrent
# appenders can both observe the file over the size cap and race the rename
# (one wins, the other's rename throws FileNotFoundError — swallowed by the
# except, benign but loses the backup). The lock makes the rotate-then-append
# atomic. A threading.Lock suffices within one process; cross-process safety
# for the append itself is provided by fcntl (locked_file below).
_ROLE_IO_ROTATION_LOCK = threading.Lock()

#: Cap a single role-IO log at 20MB before rotating to one backup (``.1``).
#: Historical role logs grew without an upper bound; this is the structural cap
#: (lowered here because role-IO files are append-heavy and per-role).
_ROLE_IO_MAX_BYTES = 20 * 1024 * 1024


_LLM_FIRST_ACTIVITY_WARN_SEC = float(
    os.environ.get("POK_LLM_FIRST_ACTIVITY_WARN_SEC", "60")
)

_LLM_PROGRESS_INTERVAL_SEC = float(
    os.environ.get("POK_LLM_PROGRESS_INTERVAL_SEC", "120")
)

_LLM_SILENCE_WARN_SEC = float(
    os.environ.get("POK_LLM_SILENCE_WARN_SEC", "240")
)

_ROLE_TIMEOUT_DEFAULTS = {
    # Fallback for analysis/probe roles such as MATCH ANALYST, COMBINED
    # ANALYST and literature-probe roles. These can be
    # slower than gate roles on GLM-backed Claude-compatible endpoints, but must
    # still have a hard ceiling so the pipeline cannot wait forever.
    "DEFAULT": (240.0, 360.0, 900.0),
    # The final Master is a zero-tool selector/compiler over the frozen three-
    # proposal/two-ballot packet.  Its prompt and output schema are necessarily
    # large. Live v148 emitted only system/thinking telemetry through the old
    # 240s first-substantive boundary and was killed at 277.5s including owned
    # cleanup, even though every Scout/ballot input was already durable. Give
    # this zero-tool selector/compiler 360s for first activity and later
    # productive-message silence. This is independent from both proposal
    # Scouts and ordinary Master roles and remains bounded by the same 900s
    # total ceiling.
    "MASTER_FINAL": (360.0, 360.0, 900.0),
    # Proposal Scouts are Read-capable mechanism designers.  Live strict runs
    # routinely complete between 155s and 236s after one or more bounded Read
    # round-trips. Live singleton-successor evidence also showed a valid
    # counterfactual role still computing at the former 240s boundary. Keep the
    # pre-output gate at 120s, but give an already productive Scout 360s of
    # silence. Successful roles are now journaled separately, so this larger
    # per-role bound no longer multiplies into whole-ensemble redispatch.
    # System/thinking telemetry remains nonproductive and the 900s total
    # ceiling is unchanged.
    "MASTER_PROPOSAL": (120.0, 360.0, 900.0),
    # Master is the highest leverage failure point: it plans, reads evidence,
    # and can otherwise burn the whole orchestrator cycle before any code exists.
    "MASTER": (120.0, 240.0, 900.0),
    # Combined Analyst analyzes the source bot and selects the parent for the
    # next generation.  It is a single deep-analysis role (no tools, large
    # prompt + output schema) that under GLM effort=max + 64k thinking budget
    # can spend 15+ min in thinking before emitting visible text.  The DEFAULT
    # first_activity (900s via env) was too short for some generations (v56 at
    # 2026-08-05 hit "LLM first_activity timeout after 900.0s").  Give it the
    # same 1200s first-activity window as REVIEW.
    "COMBINED_ANALYST": (300.0, 600.0, 1800.0),
    # Review/Critic can be slow on GLM-backed Claude-compatible endpoints.
    # They still have ceilings, but defaults must be long enough to avoid
    # repeated 600s retries that keep the generation stuck at quality_passed.
    "REVIEW": (180.0, 360.0, 1200.0),
    "CRITIC": (180.0, 360.0, 900.0),
    # Crossover synthesizes a whole child bot from two parents and routinely
    # exceeds the generic analysis/probe budget on GLM-backed Claude-compatible
    # endpoints. Keep the idle ceiling, but give total wall-clock enough room so
    # a live stream is not killed and restarted at ~15 minutes.
    "CROSSOVER": (240.0, 420.0, 2400.0),
    # Workers already have an outer WORKER_TIMEOUT. Live v147 showed legitimate
    # Read/tool reasoning repeatedly crossing the generic 180s mid-loop stall
    # ceiling: four provider streams were restarted from the same frozen prompt
    # before any Edit could land. Give a productive Worker the full 360s idle
    # window while retaining the 180s no-first-output gate and 1000s total cap.
    "WORKER": (180.0, 360.0, 1000.0),
    # The background LLM saturator ("SATURATOR STRATEGY RESEARCH") runs long
    # single-agent deep strategy research sessions (Read-tool, effort=max,
    # 64k thinking budget) whose purpose is to consume idle LLM capacity.
    # GLM routinely thinks 200-500s between productive messages; the generic
    # DEFAULT stall clamp (180s) killed every deep session mid-thought
    # (2026-08-14: 5,944 sessions stall-killed vs 9 completed, zero after
    # 07:46). Give it its own bucket: generous first-activity/idle (900s),
    # a 2h total ceiling, and exemption from the [60,180]s stall clamp so a
    # productive deep-thinking session is judged by the WORKER-class 360s
    # stall default (or the POK_LLM_SATURATOR_* env overrides).
    "SATURATOR": (900.0, 900.0, 7200.0),
}


# --- Extended-thinking configuration ---------------------------------------
# GLM-5.2 via the Anthropic-compatible endpoint:
#
#   * ``thinking.type=adaptive`` — KNOWN BUG: GLM emits 16k-19k+ thinking
#     tokens without ever producing visible output, exhausting the timeout
#     ceiling. Do NOT use ``adaptive``.
#   * ``thinking.type=enabled`` + ``budget_tokens`` — reliable: reason then
#     answer. GLM treats budget as a SOFT TARGET (not a hard cap), so the model
#     may exceed it when deep reasoning is warranted. A large budget (64000)
#     gives GLM full freedom to reason deeply.
#   * ``effort=max`` — GLM's strongest reasoning depth. Confirmed NOT a
#     death-loop: thinking tokens grow linearly and the model eventually emits
#     visible text. It is simply SLOW, requiring role timeouts of 1800-3600s
#     (see _ROLE_TIMEOUT_DEFAULTS and deploy/tencent-cloud/env.runtime). The
#     earlier "infinite loop" diagnosis was a misattribution caused by killing
#     the stream at 900s while GLM was still productively reasoning.
#
# All three are environment-overridable via POK_LLM_THINKING_MODE,
# POK_LLM_THINKING_BUDGET, and POK_LLM_EFFORT.
def _llm_thinking_options() -> dict:
    mode = os.environ.get("POK_LLM_THINKING_MODE", "enabled").strip().lower()
    if mode == "disabled":
        return {"thinking": {"type": "disabled"}}
    if mode == "adaptive":
        return {"thinking": {"type": "adaptive"}}
    # default / "enabled": deep reasoning with strong effort. GLM-5.2 treats
    # budget_tokens as a soft target (not a hard cap), so a large budget (default
    # 64000) lets the model reason as deeply as it needs and still converge.
    # effort=max selects GLM's strongest reasoning depth, producing the highest
    # quality strategy output. Both are now the defaults after confirming that:
    # (1) GLM does NOT enter a death-loop at effort=max — thinking tokens grow
    # linearly and the model eventually emits visible text; it is simply slow,
    # requiring higher role timeouts (see _ROLE_TIMEOUT_DEFAULTS / env.runtime).
    # (2) The earlier "infinite loop" diagnosis was a misattribution: the
    # stream was killed by insufficient timeouts (900s) while GLM was still
    # productively reasoning at 27k-66k thinking tokens.
    budget = int(os.environ.get("POK_LLM_THINKING_BUDGET", "64000"))
    options: dict = {"thinking": {"type": "enabled", "budget_tokens": budget}}
    effort = os.environ.get("POK_LLM_EFFORT", "max").strip().lower()
    if effort:
        options["effort"] = effort
    return options


def _role_timeout_policy(role_name: str) -> dict:
    """Return hard stream timeout policy for a role.

    Values <=0 disable that timeout. Environment overrides are intentionally
    role-scoped so slow backends can be tuned without changing code.
    """
    role = str(role_name or "").upper()
    key = ""
    if re.fullmatch(r"MASTER(?:\s+\(TRY\s+\d+\))?", role):
        key = "MASTER_FINAL"
    elif re.fullmatch(
        r"MASTER PROPOSAL (?:MECHANISM|COUNTERFACTUAL|COMPUTE_MEMORY)"
        r"(?: (?:SCHEMA|DISTINCTNESS) RETRY)?",
        role,
    ):
        key = "MASTER_PROPOSAL"
    elif "MASTER" in role:
        key = "MASTER"
    elif "REVIEW" in role:
        key = "REVIEW"
    elif "CRITIC" in role:
        key = "CRITIC"
    elif "CROSSOVER" in role:
        key = "CROSSOVER"
    elif "COMBINED" in role:
        key = "COMBINED_ANALYST"
    elif "WORKER" in role:
        key = "WORKER"
    elif "SATURATOR" in role:
        key = "SATURATOR"
    # Read _ROLE_TIMEOUT_DEFAULTS through llm_query so test monkeypatches on
    # llm_query._ROLE_TIMEOUT_DEFAULTS take effect.
    import llm_query as _lq
    defaults = _lq._ROLE_TIMEOUT_DEFAULTS.get(key or "DEFAULT", (0.0, 0.0, 0.0))

    def _env(name, default):
        names = [name]
        # Preserve existing operator overrides while giving the zero-tool final
        # compiler its own more-specific namespace.  MASTER_FINAL wins when
        # both are present; legacy MASTER remains a safe fallback.
        if key in {"MASTER_FINAL", "MASTER_PROPOSAL"} and (
            f"POK_LLM_{key}_" in name
        ):
            names.append(name.replace(
                f"POK_LLM_{key}_", "POK_LLM_MASTER_", 1
            ))
        for candidate in names:
            if candidate not in os.environ:
                continue
            try:
                parsed = float(os.environ[candidate])
                if math.isfinite(parsed):
                    return parsed
            except Exception:
                pass
            # A malformed/non-finite role-specific override must not mask a
            # valid legacy operator override. Continue through the ordered
            # fallback chain and use the compiled default only if none parses.
            continue
        return float(default)

    prefix = f"POK_LLM_{key}_" if key else "POK_LLM_DEFAULT_"
    first_activity = _env(prefix + "FIRST_ACTIVITY_TIMEOUT", defaults[0])
    idle = _env(prefix + "IDLE_TIMEOUT", defaults[1])
    total = _env(prefix + "TOTAL_TIMEOUT", defaults[2])
    # B3 (2026-07-09): a shorter stall ceiling enforced AFTER the first
    # substantive model output, i.e. once the stream has entered the
    # tool/thinking loop. Backends like the deepseek-v4-pro endpoint behind
    # cc-switch intermittently stall mid-tool-loop (a tool_use is emitted but
    # its tool_result never returns, or the model stops streaming mid-think).
    # The full idle_timeout (240-420s) is appropriate for the FIRST real
    # output but is too long to wait once we are already in the loop: every
    # mid-loop stall costs the full idle budget before the role retry can
    # restart. Default to ~55% of idle (clamped to [60, 180]s) so a stall is
    # caught well before the full idle ceiling while still tolerating legit
    # slow tool/think deltas. 0 disables (falls back to idle_timeout).
    stall_default = (
        360.0
        if key in {"MASTER_PROPOSAL", "MASTER_FINAL", "WORKER", "SATURATOR"}
        else 0.0
    )
    if idle > 0 and key not in {
        "MASTER_FINAL",
        "MASTER_PROPOSAL",
        "WORKER",
        "SATURATOR",
    }:
        stall_default = max(60.0, min(180.0, idle * 0.55))
    stall = _env(prefix + "STALL_TIMEOUT", stall_default)
    return {
        "policy_key": key or "DEFAULT",
        "first_activity_timeout": first_activity,
        "idle_timeout": idle,
        "stall_timeout": stall,
        "total_timeout": total,
    }


def _emit_llm_event(category, severity, message, **fields):
    """Emit an LLM lifecycle event without letting logging affect execution."""
    try:
        import event_bus
        event_bus.emit(category, severity, message, **fields)
    except Exception:
        pass


def _role_log_metadata(log_file_path):
    path = str(log_file_path or "")
    meta = {"log_file": path}
    match = re.search(
        r"/v(\d+)/logs/(?:[^/]+/)*([^/]+)_io\.txt$",
        path,
    )
    if match:
        meta["version"] = int(match.group(1))
        meta["role_log"] = match.group(2)
    return meta


def _role_log_basename(log_file_path):
    """Return a short relative path for metrics logging (e.g. v1/.../master_io.txt)."""
    path = str(log_file_path or "")
    match = re.search(r"/(v\d+/logs/.+)$", path)
    if match:
        return match.group(1)
    return path.rsplit("/", 1)[-1] if path else None


def _tools_metadata(tools):
    if tools is None:
        return {"tools": []}
    if isinstance(tools, (list, tuple)):
        return {"tools": [str(t) for t in tools]}
    return {"tools": [type(tools).__name__]}


def _usage_metadata(usage):
    if not usage:
        return {}
    try:
        data = usage if isinstance(usage, dict) else usage.model_dump()
    except Exception:
        try:
            data = dict(usage)
        except Exception:
            data = {}
    summary = {}
    for key in (
        "input_tokens", "output_tokens", "cache_creation_input_tokens",
        "cache_read_input_tokens",
    ):
        if key in data:
            summary[key] = data.get(key)
    return summary


def _llm_failure_severity(exc: Exception) -> str:
    """Classify known noisy SDK/business failures without hiding hard failures."""
    import llm_query as _lq
    if _lq.is_success_error_result(exc):
        return "info"
    return "error"


def _append_role_io(log_file_path, text):
    """Append text to a role-IO log file with fcntl locking + 20MB rotation.

    Replaces the bare ``with open(path, "a") as lf: lf.write(...)`` pattern that
    had no locking and no size cap.

      - fcntl LOCK_EX via ``evolution_infra.locked_file`` → cross-process +
        cross-thread safe when orchestrator roles append concurrently.
      - Before writing: if a non-strict file exceeds ``_ROLE_IO_MAX_BYTES``
        (20MB), copy its locked bytes to ``.1`` (single overwrite backup), then
        truncate the still-locked live inode. Strict invocation logs never
        rotate because their complete bytes are immutable evidence.
      - Each appended chunk is prefixed with ``[<run_id>] `` (or ``[-]`` when
        no run_id is resolvable) so role-IO lines join app.log + events.jsonl
        on the same correlation key (RC6).

    Never raises — logging must not crash the pipeline. Returns silently on any
    error (the underlying stream processing / return value is unaffected).
    """
    try:
        log_file_path = os.fspath(log_file_path)
        # Resolve the current run_id for the correlation prefix. event_bus reads
        # the live checkpoint as fallback, so this works even in long-lived
        # worker threads that are not pinned to one generation.
        try:
            from event_bus import capture_context
            _ctx = capture_context() or {}
            _rid = _ctx.get("run_id") or "-"
        except Exception:
            _rid = "-"
        chunk = f"[{_rid}] {text}" if not text.startswith("\n") else f"\n[{_rid}] " + text.lstrip("\n")
        from evolution_infra import locked_file
        # Read the rotation lock + size cap through llm_query so test
        # monkeypatches on llm_query._ROLE_IO_ROTATION_LOCK /
        # llm_query._ROLE_IO_MAX_BYTES take effect (see
        # test_llm_role_observability.py).
        import llm_query as _lq
        with _lq._ROLE_IO_ROTATION_LOCK:
            with locked_file(log_file_path, "a+", encoding="utf-8") as lf:
                lf.seek(0, os.SEEK_END)
                strict_log = f"{os.sep}strict_invocations{os.sep}" in (
                    os.path.abspath(log_file_path)
                )
                if lf.tell() > _lq._ROLE_IO_MAX_BYTES and not strict_log:
                    lf.seek(0)
                    previous = lf.read()
                    with locked_file(
                        log_file_path + ".1",
                        "w",
                        encoding="utf-8",
                    ) as rotated:
                        rotated.write(previous)
                        rotated.flush()
                        os.fsync(rotated.fileno())
                    lf.seek(0)
                    lf.truncate()
                lf.seek(0, os.SEEK_END)
                lf.write(chunk)
                lf.flush()
    except Exception:
        pass
