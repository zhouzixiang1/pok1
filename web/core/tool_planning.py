"""Pipeline tools: direction audit, master planning, and worker execution."""

import ast
import io
import json
import os
import py_compile
import re
import hashlib
import shutil
import time
import tokenize
from pathlib import Path

from bot_namespace import bot_name, bot_relpath
from tool_runtime_guard import tool

from logging_config import get_logger
_log = get_logger("planning")

from evolution_core import (
    get_bot_dir,
    _run_master_analysis,
    _run_direction_audit,
    _execute_workers,
    EXPERIENCE_FILE,
    write_pipeline_checkpoint,
    check_code_size,
    MAX_PRECOMMIT_REWORK_ROUNDS,
)
from tool_helpers import (
    _get_ui, _json_tool_result,
    _matching_checkpoint, _state_blocked,
    _validate_worker_boundaries,
    _target_rel, _py_files_changed_between, _resolve_version_args,
    PROJECT_ROOT,
    _set_pipeline_status,
    normalize_worker_role,
)
from system_log import log_system_event
from pipeline_state import route_policy


def _literature_probe_cache_path(next_v: int | str) -> Path:
    from evolution_infra import RESULTS_DIR
    return RESULTS_DIR / "research_proposals" / f"v{int(next_v)}.json"


def _literature_probe_context_fingerprint(
    source_v: int | str | None,
    h2h_weakness: str = "",
    stagnation_info: str = "",
) -> str:
    payload = {
        "source_v": int(source_v) if source_v is not None else None,
        "h2h_weakness": " ".join((h2h_weakness or "").split()),
        "stagnation_info": " ".join((stagnation_info or "").split()),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _literature_probe_cache_matches(
    data: dict,
    *,
    source_v: int | str | None = None,
    h2h_weakness: str = "",
    stagnation_info: str = "",
) -> bool:
    if source_v is not None and data.get("source_v") is not None:
        try:
            if int(data.get("source_v")) != int(source_v):
                return False
        except (TypeError, ValueError):
            return False
    expected = _literature_probe_context_fingerprint(
        source_v,
        h2h_weakness,
        stagnation_info,
    )
    stored = data.get("context_fingerprint")
    if stored:
        return stored == expected

    # Backward-compatible safety for older cache files: only trust them when
    # their recorded weakness exactly matches the current brief. If the current
    # caller did not provide a weakness, source_v equality above is the best
    # available signal.
    current_weakness = " ".join((h2h_weakness or "").split())
    stored_weakness = " ".join(str(data.get("weakness", "") or "").split())
    if current_weakness and current_weakness != stored_weakness:
        return False
    return True


def _literature_probe_inject_text(payload: dict) -> str:
    proposal = payload.get("proposal") if isinstance(payload, dict) else None
    candidate_id = payload.get("candidate_id") if isinstance(payload, dict) else None
    gated_out = bool(payload.get("gated_out")) if isinstance(payload, dict) else False
    reason = payload.get("reason", "") if isinstance(payload, dict) else ""

    if proposal and candidate_id:
        return (
            "## Research Proposal (web-derived hypothesis, verify before using)\n"
            f"- claim: {proposal.get('claim','')}\n"
            f"- target_fn: {proposal.get('target_fn','')}\n"
            f"- numeric_claim: {proposal.get('numeric_claim','')}\n"
            f"- firing_tuple: {proposal.get('firing_tuple','')}\n"
            f"- source: {proposal.get('source_url','')}\n"
            f"- pseudocode: {proposal.get('pseudocode','')}\n"
            "NOTE: this is a hypothesis from web research. It must pass all quality gates "
            "(decision tests >=70%, precommit eval). If precommit fails, this pattern is "
            "auto-blacklisted by research_governance."
        )
    if reason == "literature_probe_timeout":
        return (
            "## Research Proposal\n"
            "No codable proposal was produced because the web research stage timed out. "
            "Proceed with run_master using direction audit, H2H, replay, and experience-pool evidence."
        )
    if reason and reason != "completed":
        return (
            "## Research Proposal\n"
            f"No codable proposal is available for this generation ({reason}). "
            "Proceed with run_master without a web hypothesis."
        )
    return (
        "## Research Proposal\nNo codable proposal survived the reflect/translation gate "
        f"this generation (gated_out={gated_out}). Proceed with run_master without a web hypothesis."
    )


def _read_literature_probe_cache(
    next_v: int | str,
    *,
    source_v: int | str | None = None,
    h2h_weakness: str = "",
    stagnation_info: str = "",
) -> dict | None:
    path = _literature_probe_cache_path(next_v)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if not _literature_probe_cache_matches(
        data,
        source_v=source_v,
        h2h_weakness=h2h_weakness,
        stagnation_info=stagnation_info,
    ):
        return None
    result = {
        "next_v": data.get("next_v", int(next_v)),
        "source_v": data.get("source_v"),
        "candidate_id": data.get("candidate_id"),
        "gated_out": bool(data.get("gated_out", False)),
        "proposal": data.get("proposal"),
        "elapsed_sec": data.get("elapsed_sec", 0),
        "weakness": data.get("weakness", ""),
        "context_fingerprint": data.get("context_fingerprint", ""),
        "cached": True,
        "reason": data.get("reason", "cached"),
        "skipped": data.get("skipped", False),
    }
    result["inject_text"] = data.get("inject_text") or _literature_probe_inject_text(result)
    return result


def _normalize_literature_probe_result(data: dict, next_v: int | str, *, cached: str = "") -> dict | None:
    if not isinstance(data, dict):
        return None
    result = {
        "next_v": data.get("next_v", int(next_v)),
        "source_v": data.get("source_v"),
        "candidate_id": data.get("candidate_id"),
        "gated_out": bool(data.get("gated_out", False)),
        "proposal": data.get("proposal"),
        "elapsed_sec": data.get("elapsed_sec", 0),
        "weakness": data.get("weakness", ""),
        "stagnation_info": data.get("stagnation_info", ""),
        "context_fingerprint": data.get("context_fingerprint", ""),
        "reason": data.get("reason", "cached"),
        "skipped": data.get("skipped", False),
    }
    if cached:
        result["cached"] = True
        result["cache_source"] = cached
    result["inject_text"] = data.get("inject_text") or _literature_probe_inject_text(result)
    return result


def _read_literature_probe_checkpoint(
    next_v: int | str,
    *,
    source_v: int | str | None = None,
    h2h_weakness: str = "",
    stagnation_info: str = "",
) -> dict | None:
    """Return this generation's already-completed literature probe from checkpoint.

    The checkpoint is generation-authoritative. If a resumed orchestrator rebuilds
    slightly different weakness/stagnation text, reusing the existing probe is
    still safer than launching a second web query and creating a second candidate
    for the same next_v/source_v.
    """
    try:
        from evolution_infra import read_pipeline_checkpoint
        ckpt = read_pipeline_checkpoint() or {}
    except Exception:
        return None
    try:
        if int(ckpt.get("next_v")) != int(next_v):
            return None
        if source_v is not None and int(ckpt.get("source_v")) != int(source_v):
            return None
    except (TypeError, ValueError):
        return None
    payload = ckpt.get("literature_probe")
    result = _normalize_literature_probe_result(payload, next_v, cached="checkpoint")
    if not result:
        return None
    current_fp = _literature_probe_context_fingerprint(source_v, h2h_weakness, stagnation_info)
    stored_fp = result.get("context_fingerprint")
    if stored_fp and stored_fp != current_fp:
        result["context_mismatch_reused"] = True
        result["current_context_fingerprint"] = current_fp
    return result


def _persist_literature_probe_result(next_v: int | str, source_v: int | str | None, payload: dict) -> None:
    try:
        from evolution_infra import read_pipeline_checkpoint
        ckpt = read_pipeline_checkpoint() or {}
        if int(ckpt.get("next_v")) != int(next_v):
            return
        if source_v is not None and int(ckpt.get("source_v")) != int(source_v):
            return
        stage = ckpt.get("stage") or "direction_audited"
        write_pipeline_checkpoint(
            int(next_v),
            int(ckpt.get("source_v") if source_v is None else source_v),
            stage,
            literature_probe=payload,
        )
    except Exception:
        pass


def _write_literature_probe_cache(next_v: int | str, payload: dict) -> dict:
    path = _literature_probe_cache_path(next_v)
    path.parent.mkdir(parents=True, exist_ok=True)
    out = dict(payload)
    out.setdefault("next_v", int(next_v))
    out.setdefault(
        "context_fingerprint",
        _literature_probe_context_fingerprint(
            out.get("source_v"),
            out.get("weakness", ""),
            out.get("stagnation_info", ""),
        ),
    )
    out.setdefault("inject_text", _literature_probe_inject_text(out))
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


# ──────────────────────────────────────────────
# Direction Audit Stage (pre-Master)
# ──────────────────────────────────────────────

@tool("run_direction_audit", "Audit recent generation directions for repetition. Returns exhausted directions and mandatory constraints for the Master.", {"source_v": int, "next_v": int})
async def run_direction_audit(args):
    source_v = args.get("source_v")
    next_v = args.get("next_v")
    if source_v is None or next_v is None:
        _v, source_v = _resolve_version_args(args)
        next_v = next_v or _v
    if source_v is None or next_v is None:
        return _json_tool_result({"error": "Missing source_v/next_v and no active checkpoint"})

    _set_pipeline_status(f"Auditing directions for v{next_v}")

    # Cache guard: skip LLM call if already completed for this (next_v, source_v)
    _existing = _matching_checkpoint(next_v, source_v)
    if _existing and _existing.get("stage") == "direction_audited" and _existing.get("direction_audit"):
        ui = _get_ui()
        ui.log_history("Direction audit: using cached result (already completed)", "info")
        return _json_tool_result({
            "direction_audit": _existing["direction_audit"],
            "logs": ui.get_output(),
        })

    ui = _get_ui()
    result = await _run_direction_audit(source_v, ui)

    repetition = result.get("repetition_detected", False)
    exhausted = result.get("exhausted_directions", [])
    constraints = result.get("mandatory_constraints")
    suggested = result.get("suggested_direction")
    confidence = result.get("confidence", "low")
    llm_failed = result.get("llm_failed", False)

    direction_audit_payload = {
        "repetition_detected": repetition,
        "exhausted_directions": exhausted,
        "mandatory_constraints": constraints,
        "suggested_direction": suggested,
        "confidence": confidence,
        "resolved": False,
        # Propagate the infra marker so run_master can skip injecting the
        # (untrustworthy, empty) audit mandatory_constraints block.
        "llm_failed": llm_failed,
    }

    # Persist to checkpoint
    _ckpt = _matching_checkpoint(next_v, source_v)
    existing_plan = _ckpt.get("master_plan") if _ckpt else None
    # Backward-guard (root-cause-audit 2026-06-17): do NOT regress the pipeline
    # stage. If this (next_v, source_v) checkpoint has already advanced past
    # direction_audited (e.g. a successful crossover wrote workers_done, or
    # master already planned), keep the more-advanced stage and only refresh
    # the direction_audit payload. Previously this unconditionally wrote
    # "direction_audited", causing workers_done -> direction_audited regressions
    # (logged "Illegal stage transition ... Allowing but logging") that discarded
    # crossover worker-output metadata (parent2_v lost).
    _current_stage = _ckpt.get("stage") if _ckpt else None
    try:
        from evolution_infra import STAGE_ORDER
        _da_idx = STAGE_ORDER.index("direction_audited")
        _cur_idx = STAGE_ORDER.index(_current_stage) if _current_stage in STAGE_ORDER else -1
    except Exception:
        _da_idx, _cur_idx = 1, -1
    _target_stage = _current_stage if (_cur_idx > _da_idx) else "direction_audited"
    if _target_stage != "direction_audited":
        try:
            log_system_event("pipeline.direction_audit_skip_regression", "info",
                             f"Direction audit for v{next_v}: keeping advanced stage '{_current_stage}' "
                             f"(would have regressed to direction_audited); refreshing audit payload only.",
                             {"next_v": next_v, "source_v": source_v, "kept_stage": _current_stage})
        except Exception:
            pass
    write_pipeline_checkpoint(
        next_v, source_v, _target_stage,
        direction_audit=direction_audit_payload,
        master_plan=existing_plan,
        worker_failure_count=_ckpt.get("worker_failure_count", 0) if _ckpt else 0,
    )

    if llm_failed:
        # Infra failure is neither "warning" (repetition) nor "passed" (clean).
        # Log it as a distinct event so the orchestrator can see the audit was
        # untrustworthy; run_master also emits its own pipeline.direction_audit_infra.
        event_type = "pipeline.direction_audit_infra"
        severity = "warn"
        msg = (f"Direction audit: LLM infrastructure failure for v{next_v} — "
               "verdict untrustworthy, proceeding with mechanical backstop only")
    else:
        event_type = "pipeline.direction_audit_warning" if repetition else "pipeline.direction_audit_passed"
        severity = "warn" if repetition else "success"
        msg = (f"Direction audit: repetition detected ({', '.join(exhausted)})" if repetition
               else "Direction audit: no repetition detected")
    log_system_event(event_type, severity, msg, {
        "next_v": next_v, "source_v": source_v,
        "repetition_detected": repetition,
        "exhausted_directions": exhausted,
        "llm_failed": llm_failed,
    })

    return _json_tool_result({
        "direction_audit": direction_audit_payload,
        "logs": ui.get_output(),
    })


# ──────────────────────────────────────────────
# Master Stage
# ──────────────────────────────────────────────

# Hard cap on total Master-stage failures (plan-JSON-collapse + audit-rejection)
# per generation. Beyond this, run_master refuses to burn more LLM budget and
# directs the orchestrator to abandon. Root-cause-audit 2026-06-17: a single
# cycle hit 6x run_master (≈$8 wasted) because Master JSON systematically
# collapses under a tight direction-audit (malformed-JSON observed 221× vs
# signature 50×). The orchestrator prompt's soft "at most 2 times" rule was
# ignored; this is the mechanical backstop. audit_attempt in the checkpoint
# doubles as the counter (reset to 0 on successful master_planned write via
# reset_audit_attempt=True at line ~657).
MAX_MASTER_TOTAL_FAILURES = 4
LITERATURE_PROBE_TIMEOUT = int(os.environ.get("POK_LITERATURE_PROBE_TIMEOUT", "600"))


def _bump_master_fail_count(next_v, source_v, value=None, audit_context=None):
    """Increment (or set) the Master-stage failure counter in the checkpoint.

    Reuses the audit_attempt field: both plan-JSON-collapse (data is None) and
    audit-rejection are "Master-stage failures", and run_master's hard cap at
    the top of the function counts them together to stop token-burning loops.
    Returns the new count (0 on any error / mismatched generation).
    """
    try:
        from evolution_infra import read_pipeline_checkpoint
        ckpt = read_pipeline_checkpoint() or {}
        if ckpt.get("next_v") != next_v:
            return 0
        cur = int(ckpt.get("audit_attempt") or 0)
        new = cur + 1 if value is None else int(value)
        write_pipeline_checkpoint(
            next_v, source_v, ckpt.get("stage") or "direction_audited",
            audit_attempt=new, touch_stage_timestamp=True,
            audit_context=audit_context,
        )
        return new
    except Exception:
        return 0


def _touch_master_checkpoint(next_v, source_v, *, phase, audit_attempt=None, audit_context=None):
    """Refresh the active checkpoint while Master/audit LLM work is progressing.

    `run_master` can legitimately spend many minutes inside Master retries and
    plan-audit loops before it reaches the real `master_planned` stage. Without
    this heartbeat, watchdogs see the checkpoint parked at `direction_audited`
    and misclassify an active LLM stream as a stale pipeline.
    """
    try:
        from evolution_infra import read_pipeline_checkpoint

        ckpt = read_pipeline_checkpoint() or {}
        if ckpt.get("next_v") != next_v:
            return False
        stage = ckpt.get("stage") or "direction_audited"
        if stage in {"timed_out", "infra_timed_out", "archived", "abandoned"}:
            return False
        checkpoint_kwargs = {"touch_stage_timestamp": True}
        if audit_attempt is not None:
            checkpoint_kwargs["audit_attempt"] = audit_attempt
        if audit_context is not None:
            checkpoint_kwargs["audit_context"] = audit_context
        ok = write_pipeline_checkpoint(
            next_v,
            ckpt.get("source_v", source_v),
            stage,
            **checkpoint_kwargs,
        )
        if ok:
            log_system_event(
                "pipeline.master_checkpoint_heartbeat",
                "info",
                f"Master checkpoint heartbeat for v{next_v} ({phase})",
                {
                    "next_v": next_v,
                    "source_v": ckpt.get("source_v", source_v),
                    "stage": stage,
                    "phase": phase,
                    "audit_attempt": audit_attempt,
                },
            )
        return bool(ok)
    except Exception as exc:
        _log.debug("Master checkpoint heartbeat failed (%s): %s", phase, exc)
        return False


def _normalize_master_plan_paths(plan, source_v, next_v):
    """Rewrite parent bot paths in a Master plan to the target bot path.

    Master can inspect the source bot, but worker edit and verification paths
    must point at the prepared target directory. Keep the rewrite path-scoped so
    prose such as "national_v206 is weak vs underbets" remains intact.
    """
    meta = {
        "source_v": source_v,
        "next_v": next_v,
        "replacements": 0,
        "fields": [],
    }
    if not isinstance(plan, (dict, list)) or source_v is None or next_v is None:
        return plan, meta
    try:
        source_i = int(source_v)
        next_i = int(next_v)
    except (TypeError, ValueError):
        return plan, meta
    if source_i == next_i:
        return plan, meta

    source_bot = bot_name(source_i)
    target_bot = bot_name(next_i)
    rel_source = f"bots/{source_bot}"
    rel_target = f"bots/{target_bot}"
    win_source = f"bots\\{source_bot}"
    win_target = f"bots\\{target_bot}"
    abs_source = str(PROJECT_ROOT / "bots" / source_bot)
    abs_target = str(PROJECT_ROOT / "bots" / target_bot)
    abs_win_source = abs_source.replace("/", "\\")
    abs_win_target = abs_target.replace("/", "\\")

    literal_replacements = [
        (rel_source + "/", rel_target + "/"),
        (win_source + "\\", win_target + "\\"),
        (abs_source + "/", abs_target + "/"),
        (abs_win_source + "\\", abs_win_target + "\\"),
    ]
    quoted_dirs = [
        (rel_source, rel_target),
        (win_source, win_target),
        (abs_source, abs_target),
        (abs_win_source, abs_win_target),
    ]

    def replace_text(text):
        changed = 0
        out = text
        for src, dst in literal_replacements:
            n = out.count(src)
            if n:
                out = out.replace(src, dst)
                changed += n
        for src, dst in quoted_dirs:
            pattern = re.compile(rf"(?P<q>['\"]){re.escape(src)}(?P=q)")

            def _quoted(match, replacement=dst):
                return f"{match.group('q')}{replacement}{match.group('q')}"

            out, n = pattern.subn(_quoted, out)
            changed += n

            cd_pattern = re.compile(
                rf"(?P<prefix>\bcd\s+){re.escape(src)}"
                rf"(?P<suffix>\s*(?:&&|;|\||\n|$))"
            )
            out, n = cd_pattern.subn(
                lambda m, replacement=dst: (
                    f"{m.group('prefix')}{replacement}{m.group('suffix')}"
                ),
                out,
            )
            changed += n
        return out, changed

    def walk(value, path):
        if isinstance(value, str):
            new_value, count = replace_text(value)
            if count:
                meta["replacements"] += count
                meta["fields"].append(path)
            return new_value
        if isinstance(value, list):
            return [walk(item, f"{path}[{idx}]") for idx, item in enumerate(value)]
        if isinstance(value, dict):
            return {key: walk(item, f"{path}.{key}") for key, item in value.items()}
        return value

    if isinstance(plan, dict):
        normalized = dict(plan)
        if "tasks" in normalized:
            normalized["tasks"] = walk(normalized["tasks"], "plan.tasks")
    else:
        normalized = walk(plan, "plan")
    if meta["fields"]:
        meta["fields"] = sorted(set(meta["fields"]))
    return normalized, meta


def _normalize_and_log_master_plan_paths(plan, source_v, next_v):
    normalized, meta = _normalize_master_plan_paths(plan, source_v, next_v)
    if meta.get("replacements", 0) > 0:
        try:
            log_system_event(
                "pipeline.master_plan_paths_normalized", "warn",
                f"Normalized {meta['replacements']} parent-path reference(s) "
                f"in Master plan v{next_v}: {bot_relpath(source_v)} -> "
                f"{bot_relpath(next_v)}",
                meta,
            )
        except Exception:
            pass
    return normalized


_TUNER_STRUCTURAL_PATTERNS = [
    "add parameter", "add a parameter", "function signature",
    "add function", "new function", "add method",
    "add class", "new class",
    "add import", "new import",
    "before the clamp", "after the existing",
]


# A4 (evidence_gate, evolution-plan-refresh-jun21): citation patterns the agents use
# to reference spotlight hands. Anchored form (G3H25#9a3f1c02) is preferred but the
# bare form (G3H25) is what fabricated citations usually look like.
_CITATION_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:G\d+H\d+|H\d+)(?:#[0-9a-fA-F]{8})?(?![A-Za-z0-9_])"
)


def _load_replay_anchor_map():
    """Load the spotlight manifest and return {base_id: anchor} map.

    Returns:
        dict mapping citation base ID (e.g. "G3H25") to anchor string, or
        None if the manifest is missing/corrupt (caller should skip checks).
    """
    try:
        _manifest_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "results", "spotlight_manifest.json")
        if not os.path.exists(_manifest_path):
            return None  # spotlight didn't run this gen — can't verify
        with open(_manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
    except Exception:
        return None  # corrupt/missing manifest — can't verify

    anchor_map = {}
    for c in manifest.get("citations", []):
        anchor_map[c.get("id", "")] = c.get("anchor", "")
    return anchor_map


def _check_citations(text_list, anchor_map):
    """Check text list for fabricated GxHx#anchor citations.

    Args:
        text_list: List of strings to check for citation patterns.
        anchor_map: Manifest of valid anchors.
                   None  = no manifest loaded (skip check, return []).
                   {}    = manifest loaded but empty = ALL citations fabricated.
                   {id: anchor, ...} = normal validation.

    Returns:
        List of error messages for fabricated citations.
    """
    if anchor_map is None:
        return []  # No manifest loaded, skip
    errors = []
    for text in text_list:
        for match in _CITATION_RE.finditer(text):
            ref = match.group(0)
            base = ref.split("#", 1)[0] if "#" in ref else ref
            if base not in anchor_map:
                errors.append(
                    f"FABRICATED_EVIDENCE: '{ref}' is NOT in the spotlight manifest "
                    f"(no such hand exists in recent replays). Only cite hands "
                    f"verbatim from the injected Replay Spotlight section "
                    f"(format: G<game>H<hand>#<anchor>)."
                )
            elif "#" in ref:
                cited_anchor = ref.split("#", 1)[1]
                expected = anchor_map.get(base, "")
                if expected and cited_anchor.lower() != expected.lower():
                    errors.append(
                        f"FABRICATED_EVIDENCE: '{ref}' anchor mismatch "
                        f"(expected #{expected}). Possible hallucination or "
                        f"tampering with a real hand id."
                    )
    return errors


def _sanitize_unverified_replay_citations(text, anchor_map):
    """Remove stale replay hand IDs from Master side context.

    The current replay spotlight is the only authoritative citation source for
    a generation. Direction-audit, match-analysis, research, or experience text
    can mention historical GxHy IDs from prior generations; if injected as-is,
    Master tends to repeat them and the evidence gate correctly rejects the
    plan. Keep valid current IDs, fix stale anchors, and redact invalid IDs
    before the text reaches Master.
    """
    if anchor_map is None or not isinstance(text, str) or not text:
        return text, 0

    count = 0

    def repl(match):
        nonlocal count
        ref = match.group(0)
        base = ref.split("#", 1)[0] if "#" in ref else ref
        if base not in anchor_map:
            count += 1
            return "unverified-replay-ref"
        if "#" in ref:
            cited_anchor = ref.split("#", 1)[1]
            expected = anchor_map.get(base, "")
            if expected and cited_anchor.lower() != expected.lower():
                count += 1
                return f"{base}#{expected}"
        return ref

    return _CITATION_RE.sub(repl, text), count


def _verify_cited_replays(plan):
    """A4 (evidence_gate): reject Master/Worker replay citations that don't
    correspond to any real replay hand in the spotlight manifest.

    The v127-v143 "G3H25/G2H44" fabrication recurred 9x because Master/Worker
    prompts cited hand IDs that don't exist in any recent replay (real files are
    timestamp-named; agents invented GxHx IDs). find_critical_hands now writes
    results/spotlight_manifest.json listing every citation it actually emitted;
    this function cross-checks the plan's citations against it.

    Returns a list of BLOCKING error strings. Like EXHAUSTED-direction positive
    intent, fabricated evidence must not reach workers.
    """
    anchor_map = _load_replay_anchor_map()
    tasks = plan if isinstance(plan, list) else (
        plan.get("tasks", []) if isinstance(plan, dict) else []
    )
    texts = []
    for i, task in enumerate(tasks or []):
        if not isinstance(task, dict):
            continue
        texts.append(" ".join([
            str(task.get("worker_prompt", "")),
            str(task.get("instruction", "")),
            str(task.get("targeted_failure", "")),
        ]))
    return _check_citations(texts, anchor_map)


def _exhausted_plan_violations(plan, next_v=None, precomputed_exhausted_keywords=None):
    """Return blocking positive-intent matches against EXHAUSTED directions.

    This deliberately inspects only positive execution intent. Guardrail prose
    such as "do not repeat fold-gate tuning" is stripped by
    _positive_execution_text_from_task, so the gate blocks plans that actually
    ask workers to implement a stale axis, not plans that merely quote the ban.
    """
    exhausted_keywords = (
        precomputed_exhausted_keywords
        if precomputed_exhausted_keywords is not None
        else _extract_exhausted_keywords()
    )
    if not exhausted_keywords or not isinstance(plan, dict):
        return []

    violations = []
    for i, task in enumerate(plan.get("tasks", []) or []):
        if not isinstance(task, dict):
            continue
        prompt_text = _positive_execution_text_from_task(task)
        if _fuzzy_match_exhausted(prompt_text, exhausted_keywords, require_direction_token=True):
            worker_id = task.get("worker_id", i)
            violations.append(
                "EXHAUSTED_DIRECTION_REPEATED: "
                f"Task {i} (worker_id={worker_id}) positive implementation intent "
                "matches an EXHAUSTED direction from experience_pool.md. "
                "Produce a fundamentally different axis before executing workers."
            )
    return violations


def _validate_master_plan(
    plan,
    next_v=None,
    precomputed_exhausted_keywords=None,
    exhausted_policy="error",
):
    """Validate master plan constraints before dispatching workers.

    Returns (errors, warnings) — only errors block plan storage.
    Boundary warnings are logged but non-blocking; the reviewer/critic
    enforce actual role boundaries during code review.

    exhausted_policy:
    - "error": normal Master planning; stale directions must not reach workers.
    - "warn": repair/backward-compatible callers; record risk without blocking.
    """
    errors = []
    warnings = []
    tasks = plan.get("tasks", [])
    if len(tasks) > 3:
        errors.append(f"Too many tasks: {len(tasks)} > 3")
    for i, task in enumerate(tasks):
        targets = task.get("target_files", [])
        files_allowed = task.get("files_allowed", []) or []
        if len(targets) > 3:
            errors.append(f"Task {i}: too many target_files ({len(targets)} > 3)")
        prompt = task.get("worker_prompt", "")
        if len(prompt) > 12000:
            errors.append(f"Task {i}: worker_prompt too long ({len(prompt)} > 12000 chars)")
        role = str(task.get("role", ""))
        if normalize_worker_role(role) == "tuner":
            # Tuners MUST only modify constants.py — error if target_files or
            # files_allowed includes other files. files_allowed is an expansion
            # of the writable boundary, so accepting non-constants there would
            # bypass the Tuner contract even when target_files is clean.
            # This prevents the shared-file boundary validation false positive (Bug 1)
            # where two workers target the same file, causing all changes to be incorrectly
            # reverted as a Tuner boundary violation.
            tuner_only_files = {"constants.py"}
            declared_files = list(targets) + list(files_allowed)
            non_tuner_files = [t for t in declared_files if Path(str(t)).name not in tuner_only_files]
            if non_tuner_files:
                errors.append(
                    f"Task {i}: Hyperparameter Tuner declares non-constants file(s) {non_tuner_files} "
                    f"in target_files/files_allowed. Tuners MUST only modify constants.py. "
                    f"Assign {non_tuner_files} to a Logic Architect task."
                )
            prompt_lower = prompt.lower()
            # Skip structural keywords that appear in constraint/negative contexts
            _skip_contexts = ("do not", "don't", "must not", "never", "preserve",
                              "keep", "unchanged", "maintain", "no new", "forbidden",
                              "avoid", "except", "aside from", "other than",
                              "should not", "cannot", "do not change", "do not add")
            for kw in _TUNER_STRUCTURAL_PATTERNS:
                # Find the keyword in context — skip if it's in a constraint sentence
                idx = prompt_lower.find(kw)
                if idx >= 0:
                    # Check surrounding context (200 chars before) for negative cues
                    context_before = prompt_lower[max(0, idx - 200):idx]
                    if any(cue in context_before for cue in _skip_contexts):
                        continue
                    # Keyword found in an affirmative (structural) context — warn only
                    warnings.append(
                        f"Task {i} boundary warning: Hyperparameter Tuner prompt contains structural instruction "
                        f"'{kw}' — Tuner should only change numeric constants. "
                        f"The reviewer/critic will enforce this boundary."
                    )
                    break

    # tasks 校验之后：禁止 Master 自行指定 source override 字段。
    # Source ancestor 由系统在 prepare_generation (generation_scheduler._decide_strategy)
    # 决定，Master 不得设置；否则为永不生效的死字段（写 checkpoint 后从不读取）。
    # 注意：本检查必须在 Pydantic (MasterPlan.model_validate, extra='ignore')
    # 剥离 branch_from 之前对原始 dict 调用，否则该键已被丢弃、检查永不命中。
    # 见 _run_master_analysis (agent_master.py) 中 validate_agent_output 之前的
    # 原始 dict 预检。
    source_override_fields = ("branch_from", "source_override", "source_v_override")
    offending = [f for f in source_override_fields if plan.get(f)]
    if offending:
        errors.append(
            f"Master plan must not set source-override field(s) {offending}. "
            f"Source ancestor selection is decided automatically in "
            f"prepare_generation (generation_scheduler._decide_strategy); "
            f"Master must not set branch_from."
        )

    # Check target_files overlap between workers.
    # Architect-Tuner overlap on any file is a hard error (causes boundary false positives).
    # Other overlaps are informational — workers execute sequentially so overlap is safe,
    # but different files make each worker's scope clearer.
    architect_targets = {}
    tuner_targets = {}
    all_targets = {}
    for i, task in enumerate(tasks):
        role = str(task.get("role", ""))
        _role_kind = normalize_worker_role(role)
        for target in task.get("target_files", []):
            rel = _target_rel(target, next_v) if next_v else target.strip()
            if _role_kind == "architect":
                architect_targets.setdefault(rel, []).append(i)
            elif _role_kind == "tuner":
                tuner_targets.setdefault(rel, []).append(i)
            if rel in all_targets:
                warnings.append(
                    f"Tasks {all_targets[rel]} and {i} share target_file '{target}'. "
                    f"This is safe (sequential execution) but consider splitting for clarity."
                )
            else:
                all_targets[rel] = i

    # Hard error: Architect and Tuner sharing any file causes boundary validation
    # false positives because the Tuner check sees the Architect's structural changes.
    overlap = set(architect_targets.keys()) & set(tuner_targets.keys())
    if overlap:
        errors.append(
            f"Architect and Tuner share target file(s): {sorted(overlap)}. "
            f"This causes boundary validation false positives (Tuner check sees Architect's changes). "
            f"Assign constants.py to Tuner only; other files go to Architect."
        )

    # Check positive worker intent against exhausted directions from experience
    # pool. This used to be advisory, which let v33 spend a full worker timeout
    # on a direction the system had already identified as stale. Normal Master
    # planning now blocks here; repair flows can request warning mode.
    exhausted_violations = _exhausted_plan_violations(
        plan,
        next_v=next_v,
        precomputed_exhausted_keywords=precomputed_exhausted_keywords,
    )
    if exhausted_violations:
        if exhausted_policy == "warn":
            warnings.extend(v.replace("EXHAUSTED_DIRECTION_REPEATED: ", "") for v in exhausted_violations)
        else:
            errors.extend(exhausted_violations)

    # A4 (evidence_gate, evolution-plan-refresh-jun21): BLOCKING — reject cited
    # replay hands that don't exist in the spotlight manifest (FABRICATED evidence,
    # recurred 9x v127-v143). Unlike the exhausted-direction check above, this is a
    # hard error: a plan built on hallucinated evidence must not reach workers.
    try:
        errors.extend(_verify_cited_replays(plan))
    except Exception:
        pass  # never let the gate itself crash the pipeline

    return errors, warnings


@tool("run_master", "Run Master Architect analysis to plan the next generation. Returns a task plan with worker assignments.", {"source_v": int, "next_v": int, "stagnation_info": str, "match_analysis": str, "performance_verification": str, "direction_audit": str, "research_proposals": str})
async def run_master(args):
    _t0 = time.time()
    source_v = args.get("source_v")
    next_v = args.get("next_v")
    if source_v is None or next_v is None:
        _v, source_v = _resolve_version_args(args)
        next_v = next_v or _v
    if source_v is None or next_v is None:
        return {"content": [{"type": "text", "text": json.dumps({"error": "Missing source_v/next_v and no active checkpoint"})}]}
    # B1 (v125 retry-storm fix): unify next_v with the checkpoint's authoritative
    # value. The Master-failure counter (audit_attempt) is keyed on checkpoint
    # next_v; both the top-of-function circuit-breaker guard (below, ~line 336)
    # and _bump_master_fail_count gate on `checkpoint.next_v == next_v` and
    # SILENTLY zero the count on mismatch. When the orchestrator LLM passes a
    # stale next_v (e.g. from a PreCompact-injected context snapshot), every
    # failure is silently dropped and the breaker never trips — which is exactly
    # how v125 retried 47× without ever hitting MAX_MASTER_TOTAL_FAILURES.
    # Fix: if an ACTIVE checkpoint exists with a different next_v, trust the
    # checkpoint (system-authoritative) and surface the mismatch. Dead-stage
    # checkpoints (timed_out/archived/abandoned) are NOT authoritative — the LLM
    # is likely starting a fresh generation in that case.
    try:
        from evolution_infra import read_pipeline_checkpoint
        _entry_ckpt = read_pipeline_checkpoint() or {}
        _entry_next_v = _entry_ckpt.get("next_v")
        _entry_stage = _entry_ckpt.get("stage")
        _dead_stages = (None, "timed_out", "archived", "abandoned")
        if (_entry_next_v is not None and _entry_next_v != next_v
                and _entry_stage not in _dead_stages):
            _log.warning(
                "run_master: LLM passed next_v=%s but active checkpoint is "
                "next_v=%s (stage=%s) — aligning to checkpoint to keep the "
                "Master-failure counter consistent (v125 bypass fix).",
                next_v, _entry_next_v, _entry_stage,
            )
            try:
                log_system_event(
                    "pipeline.master_next_v_mismatch", "warn",
                    f"run_master next_v={next_v} aligned to checkpoint next_v={_entry_next_v} "
                    f"(stage={_entry_stage}) — LLM passed a stale version",
                    {"args_next_v": next_v, "ckpt_next_v": _entry_next_v,
                     "source_v": source_v, "stage": _entry_stage},
                )
            except Exception:
                pass
            next_v = _entry_next_v
            if _entry_ckpt.get("source_v") is not None:
                source_v = _entry_ckpt["source_v"]
    except Exception:
        pass
    # fix-4: idempotency guard — if master already planned for this (next_v, source_v),
    # return cached result instead of re-running (LLM intermittently violates
    # orchestrator.md:43, causing duplicate run_master calls in the same cycle).
    _ckpt_idempotent = _matching_checkpoint(next_v, source_v)
    if _ckpt_idempotent and _ckpt_idempotent.get("stage") in (
        "master_planned", "workers_done", "quality_failed", "quality_passed",
        "reviewed", "critic_checked", "verified", "archived",
    ):
        _existing_plan = _ckpt_idempotent.get("master_plan")
        if _existing_plan:
            if (_ckpt_idempotent.get("parent2_v")
                    and isinstance(_existing_plan, dict)
                    and _existing_plan.get("strategy") == "crossover"):
                log_system_event(
                    "pipeline.crossover_master_call_blocked", "warn",
                    f"run_master called after crossover already produced v{next_v}; proceed to quality/retry workers, not Master",
                    {"next_v": next_v, "source_v": source_v,
                     "parent2_v": _ckpt_idempotent.get("parent2_v"),
                     "stage": _ckpt_idempotent.get("stage"),
                     "has_synthetic_plan": True},
                )
                return _json_tool_result({
                    "error": "CROSSOVER_ALREADY_DONE",
                    "next_v": next_v,
                    "source_v": source_v,
                    "parent2_v": _ckpt_idempotent.get("parent2_v"),
                    "stage": _ckpt_idempotent.get("stage"),
                    "directive": (
                        "Crossover already produced the target bot. Do NOT call run_master. "
                        "If stage=workers_done call run_quality_gates; if stage=quality_failed "
                        "call execute_workers with exact gate feedback or abandon_generation."
                    ),
                })
            log_system_event("pipeline.master_idempotent", "info",
                             f"run_master for v{next_v}: plan already exists "
                             f"(stage={_ckpt_idempotent.get('stage')}), returning cached",
                             {"next_v": next_v, "source_v": source_v})
            ui = _get_ui()
            ui.log_history("Master plan already exists — returning cached (idempotent).", "info")
            return _json_tool_result({"plan": _existing_plan, "logs": ui.get_output(),
                                      "idempotent_cache": True})
        if _ckpt_idempotent.get("parent2_v") and _ckpt_idempotent.get("stage") in (
            "workers_done", "quality_failed", "quality_passed",
            "reviewed", "critic_checked", "verified", "archived",
        ):
            log_system_event(
                "pipeline.crossover_master_call_blocked", "warn",
                f"run_master called after crossover already produced v{next_v}; proceed to quality/retry workers, not Master",
                {"next_v": next_v, "source_v": source_v,
                 "parent2_v": _ckpt_idempotent.get("parent2_v"),
                 "stage": _ckpt_idempotent.get("stage")},
            )
            return _json_tool_result({
                "error": "CROSSOVER_ALREADY_DONE",
                "next_v": next_v,
                "source_v": source_v,
                "parent2_v": _ckpt_idempotent.get("parent2_v"),
                "stage": _ckpt_idempotent.get("stage"),
                "directive": (
                    "Crossover already produced the target bot. Do NOT call run_master. "
                    "If stage=workers_done call run_quality_gates; if stage=quality_failed "
                    "call execute_workers with the exact quality failure feedback or abandon_generation."
                ),
            })

    stagnation_info = args.get("stagnation_info", "No stagnation detected. Continue from latest version.")
    match_analysis = args.get("match_analysis", "")
    performance_verification = args.get("performance_verification", "")
    direction_audit_str = args.get("direction_audit", "")
    research_proposals = args.get("research_proposals", "")

    _set_pipeline_status(f"Master planning for v{next_v}")
    _touch_master_checkpoint(next_v, source_v, phase="run_master_start")

    # Hard cap: refuse to re-burn Master LLM budget if it has already failed
    # (plan-JSON collapse or audit rejection) MAX_MASTER_TOTAL_FAILURES times
    # this generation. See MAX_MASTER_TOTAL_FAILURES docstring.
    try:
        from evolution_infra import read_pipeline_checkpoint
        _ckpt_m = read_pipeline_checkpoint() or {}
        _master_fails = int(_ckpt_m.get("audit_attempt") or 0) if _ckpt_m.get("next_v") == next_v else 0
    except Exception:
        _master_fails = 0
    if _master_fails >= MAX_MASTER_TOTAL_FAILURES:
        _ui = _get_ui()
        try:
            log_system_event("pipeline.master_exhausted", "error",
                             f"Master exhausted {_master_fails} attempts for v{next_v} — refusing retry, FORCING abandon",
                             {"next_v": next_v, "source_v": source_v, "fail_count": _master_fails})
        except Exception:
            pass
        if _ui:
            _ui.log_history(
                f"Master exhausted {_master_fails} attempts for v{next_v} — FORCING abandon "
                f"(no longer relying on a plain-text directive).",
                "error",
            )
        # B2 (v125 retry-storm fix): force-abandon instead of returning a plain-text
        # directive. The old directive relied on the orchestrator LLM voluntarily
        # calling abandon_generation, which it often ignored — so the stuck
        # generation kept burning orchestrator turns until CYCLE_TIMEOUT (v125).
        # Clear the session FIRST (so the next cycle does not resume the stuck
        # session), then abandon (clears checkpoint + removes the incomplete dir).
        try:
            from orchestrator_session import _clear_orchestrator_session
            _clear_orchestrator_session()
        except Exception:
            pass
        try:
            from tool_bot_management import _do_abandon_generation
            _abandon_result = await _do_abandon_generation(
                reason=f"master_exhausted ({_master_fails} fails)"
            )
        except Exception as _ae:
            _abandon_result = {"abandoned": False, "error": str(_ae)}
        return {"content": [{"type": "text", "text": json.dumps({
            "error": "MASTER_EXHAUSTED",
            "fail_count": _master_fails,
            **_abandon_result,
            "directive": (f"Master planning failed {_master_fails} times for v{next_v}. "
                          "This generation has been ABANDONED (checkpoint cleared, incomplete "
                          "dir removed, session cleared). The next cycle will start FRESH (it "
                          "may pick crossover or a different source ancestor). Do NOT call "
                          "run_master again — call prepare_next_gen to begin a new generation."),
            "logs": _ui.get_output() if _ui else "",
        })}]}

    # Parse direction audit from arg or checkpoint
    direction_audit = None
    if direction_audit_str:
        try:
            direction_audit = json.loads(direction_audit_str) if isinstance(direction_audit_str, str) else direction_audit_str
        except (json.JSONDecodeError, TypeError):
            pass
    if not direction_audit:
        _ckpt = _matching_checkpoint(next_v, source_v)
        direction_audit = _ckpt.get("direction_audit") if _ckpt else None

    # Inject mandatory constraints into performance_verification if audit found repetition.
    # B-class guard: if the Direction Auditor's LLM call crashed (infrastructure
    # failure), its "no repetition" verdict is untrustworthy — do NOT inject its
    # (empty) mandatory_constraints block as if the audit were authoritative.
    # The mechanical cross-gen backstop (_build_cross_gen_constraint_block below)
    # still runs and provides exhausted-direction protection independent of this
    # LLM gate, so a crashed auditor does not leave the Master unconstrained.
    if direction_audit and direction_audit.get("llm_failed"):
        _log.warning(
            "Direction audit for v%s reported LLM infrastructure failure — "
            "skipping audit mandatory_constraints injection (untrustworthy). "
            "Cross-gen mechanical backstop still applies.",
            next_v,
        )
        try:
            log_system_event(
                "pipeline.direction_audit_infra", "warn",
                f"Direction audit for v{next_v} unavailable (LLM infra error). "
                "Skipping audit constraints; cross-gen mechanical backstop still applies.",
                {"next_v": next_v, "source_v": source_v},
            )
        except Exception:
            pass
    elif direction_audit and direction_audit.get("repetition_detected") and direction_audit.get("mandatory_constraints"):
        constraint_block = (
            f"\n\n# Direction Audit Constraints (MANDATORY)\n"
            f"The Direction Auditor detected that recent generations are stuck repeating the same approach.\n"
            f"**DO NOT repeat these exhausted directions:** {', '.join(direction_audit.get('exhausted_directions', []))}\n"
            f"**Mandatory constraint:** {direction_audit['mandatory_constraints']}\n"
        )
        if direction_audit.get("suggested_direction"):
            constraint_block += f"**Suggested alternative:** {direction_audit['suggested_direction']}\n"
        constraint_block += "\nYou MUST comply with these constraints. A plan that repeats an exhausted direction will be rejected.\n"
        performance_verification = (performance_verification or "") + constraint_block

    # Cross-gen mechanical backstop: inject prior critic local-optima rejections
    # + experience-pool EXHAUSTED directions directly into performance_verification,
    # independent of the direction_audit LLM gate (which historically under-detects
    # — v82 repetition_detected=false despite the pool flagging constant-tuning
    # EXHAUSTED). No-op when there is no prior critic local-optima rejection and
    # no EXHAUSTED direction (first-ever gen / clean crossover unaffected).
    # Idempotent: guarded by CROSS_GEN_MARKER so run_master retries don't stack it.
    _cross_gen_block = _build_cross_gen_constraint_block(next_v)
    if _cross_gen_block and CROSS_GEN_MARKER not in (performance_verification or ""):
        performance_verification = (performance_verification or "") + _cross_gen_block

    ui = _get_ui()

    # --- Extract replay_spotlight for Master prompt ---
    replay_spotlight = ""
    try:
        from generation_scheduler import GenerationContext
        # replay_spotlight is computed in prepare_generation() and stored in
        # GenerationContext, but the MCP tool layer doesn't have direct access
        # to the gen_ctx object. Re-compute from the replay files instead.
        from replay_spotlight import find_critical_hands
        from evolution_infra import RESULTS_DIR
        replays_dir = str(RESULTS_DIR / "match_replay")
        replay_spotlight = find_critical_hands(
            bot_name=bot_name(source_v),
            replays_dir=replays_dir,
            max_hands=10,
            recent_n_files=20,
        )
    except Exception:
        pass

    # The Replay Spotlight below is authoritative for current-generation hand
    # IDs. Side contexts can carry historical GxHy references from old audits,
    # research proposals, or match summaries; redact those before Master sees
    # them so the hard fabricated-evidence gate can remain strict.
    _anchor_map = _load_replay_anchor_map()
    _citation_sanitized = {}
    for _name, _value in (
        ("stagnation_info", stagnation_info),
        ("match_analysis", match_analysis),
        ("performance_verification", performance_verification),
        ("research_proposals", research_proposals),
    ):
        _clean, _count = _sanitize_unverified_replay_citations(_value, _anchor_map)
        if _count:
            _citation_sanitized[_name] = _count
        if _name == "stagnation_info":
            stagnation_info = _clean
        elif _name == "match_analysis":
            match_analysis = _clean
        elif _name == "performance_verification":
            performance_verification = _clean
        elif _name == "research_proposals":
            research_proposals = _clean
    if _citation_sanitized:
        try:
            log_system_event(
                "pipeline.master_context_citations_sanitized",
                "warn",
                f"Master v{next_v} context had stale replay IDs redacted",
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "counts": _citation_sanitized,
                },
            )
        except Exception:
            pass

    # --- Read bot_action_stats for Master prompt ---
    bot_action_stats = ""
    try:
        from evolution_infra import RESULTS_DIR
        _stats_file = RESULTS_DIR / "bot_action_stats.json"
        if _stats_file.exists():
            with open(_stats_file, "r") as _f:
                _all_stats = json.load(_f)
            _source_bot_name = bot_name(source_v)
            _bot_stats = _all_stats.get(_source_bot_name)
            if _bot_stats:
                # Format as compact text for prompt injection
                _parts = []
                for _street in ("preflop", "flop", "turn", "river"):
                    _st = _bot_stats.get(_street)
                    if _st and _st.get("total", 0) > 0:
                        _total = _st["total"]
                        _parts.append(
                            f"{_street}: fold={_st.get('fold', 0)/_total:.1%} "
                            f"call={_st.get('call', 0)/_total:.1%} "
                            f"raise={_st.get('raise', 0)/_total:.1%} "
                            f"(n={_total})"
                        )
                if _parts:
                    bot_action_stats = (
                        f"Action frequencies for {_source_bot_name}:\n"
                        + "\n".join(_parts)
                    )
    except Exception:
        pass

    # --- Phase 3: per-opponent behavior profiles for Master prompt ---
    # Reads the nested per-opponent breakdown (bot_action_stats_per_opp.json,
    # written by elo_daemon alongside the flat file). For the source bot we
    # surface its most lopsided matchups (by h2h win_rate) with a compact
    # aggression / fold-to-bet / cbet / barrel line each, so the Master can
    # plan opponent-specific adaptations. Advisory: read failure -> "".
    opponent_profiles = ""
    try:
        from evolution_infra import RESULTS_DIR
        _per_opp_file = RESULTS_DIR / "bot_action_stats_per_opp.json"
        if _per_opp_file.exists():
            with open(_per_opp_file, "r") as _f:
                _per_opp_all = json.load(_f)
            _source_bot = bot_name(source_v)
            _opp_map = _per_opp_all.get(_source_bot, {}) or {}
            # Rank opponents by h2h win_rate (most-beaten and most-beating) to
            # avoid prompt bloat: keep the K most extreme matchups.
            _h2h_for_rank = {}
            try:
                from tool_helpers import _load_h2h_data, _h2h_stats
                _h2h = _load_h2h_data()
                for _opp in _opp_map:
                    _st = _h2h_stats(_source_bot, _opp, _h2h)
                    if _st:
                        _h2h_for_rank[_opp] = _st["win_rate"]
            except Exception:
                pass
            _PROFILES_K = 6
            if _h2h_for_rank:
                _ranked = sorted(_h2h_for_rank.items(), key=lambda kv: kv[1])
                _selected = [o for o, _ in _ranked[:_PROFILES_K // 2]]
                _selected += [o for o, _ in _ranked[-(_PROFILES_K // 2):]]
                # Dedup while preserving order.
                _seen = set()
                _selected = [o for o in _selected if not (o in _seen or _seen.add(o))]
            else:
                # No h2h signal: fall back to top-K by total actions observed.
                _selected = sorted(
                    _opp_map,
                    key=lambda o: sum(
                        _opp_map[o].get(s, {}).get("total", 0)
                        for s in ("preflop", "flop", "turn", "river")
                    ),
                    reverse=True,
                )[:_PROFILES_K]
            _lines = []
            for _opp in _selected:
                _ostats = _opp_map.get(_opp, {})
                _wr = _h2h_for_rank.get(_opp)
                _wr_str = f" h2h_wr={_wr:.2f}" if _wr is not None else ""
                _n = (
                    sum(_ostats.get(s, {}).get("total", 0)
                        for s in ("preflop", "flop", "turn", "river"))
                )
                if _n == 0:
                    continue
                _street_bits = []
                for _street in ("preflop", "flop", "turn", "river"):
                    _st = _ostats.get(_street, {})
                    _tot = _st.get("total", 0)
                    if _tot <= 0:
                        continue
                    _calls = _st.get("call", 0)
                    _raises = _st.get("raise", 0)
                    _folds = _st.get("fold", 0)
                    _ftb = _st.get("fold_to_bet", 0)
                    _cbet = _st.get("cbet", 0)
                    _barrel = _st.get("barrel", 0)
                    _af = (_raises / _calls) if _calls > 0 else None
                    _af_str = f"{_af:.1f}" if _af is not None else "n/a"
                    _street_bits.append(
                        f"{_street}: AF={_af_str} "
                        f"ftb={_ftb}/{_folds}f cbet={_cbet} barrel={_barrel} (n={_tot})"
                    )
                if _street_bits:
                    _lines.append(
                        f"vs {_opp}{_wr_str} (n={_n}): " + "; ".join(_street_bits)
                    )
            if _lines:
                opponent_profiles = (
                    f"Per-opponent behavior profiles for {_source_bot} "
                    f"(top extreme matchups by h2h win_rate):\n"
                    + "\n".join(_lines)
                )
    except Exception:
        pass

    # --- Read battle experience for Master prompt ---
    battle_experience = ""
    try:
        from battle_experience import get_battle_experience
        battle_experience = get_battle_experience(source_bot=bot_name(source_v))
    except Exception:
        pass

    # --- Read exploitability probe results for Master prompt ---
    # exploitability.json is written by exploitability_prober.run_exploitability_probes()
    # (called from generation_scheduler.post_generation_cleanup against the
    # PREVIOUS generation's bot). It is write-only until consumed here.
    exploitability_weaknesses = ""
    try:
        from evolution_infra import RESULTS_DIR as _RES
        _exploit_file = _RES / "exploitability.json"
        if _exploit_file.exists():
            with open(_exploit_file, "r") as _f:
                _exploit = json.load(_f)
            _overall = _exploit.get("overall_score")
            _weak_list = _exploit.get("weaknesses", []) or []
            _games = _exploit.get("num_hands")
            _bot_path = _exploit.get("bot_path", "")
            _source_bot = bot_name(source_v)
            # Stale-safe: only inject the cached probe data if it was actually
            # run for the CURRENT source bot. The post-gen probe refreshes this
            # file per generation; if it hasn't run the file holds stale data for
            # a DIFFERENT bot — injecting that would mislabel another bot's
            # weaknesses as this bot's (active misinformation into Master).
            # Stale-safe + reliability gate (defense in depth):
            # (a) Inject cached data ONLY when the cached bot_path's parent dir
            #     is EXACTLY the current source bot. A substring match
            #     (_source_bot in _bot_path) would mis-fire on similarly named
            #     bot directories, and a cached result for a DIFFERENT bot would
            #     mislabel another bot's weaknesses as this bot's (active
            #     misinformation into Master).
            # (b) Require enough hands per probe to be statistically meaningful.
            #     A tiny sample (e.g. a 2-hand diagnostic run) yields near-random
            #     win_rates that would inject noise into the Master's direction.
            _MIN_RELIABLE_PROBE_GAMES = 30
            _cached_bot = Path(_bot_path).parent.name if _bot_path else ""
            _reliable = _games is None or int(_games) >= _MIN_RELIABLE_PROBE_GAMES
            if _bot_path and _cached_bot != _source_bot:
                exploitability_weaknesses = (
                    f"No fresh exploitability probe data for {_source_bot} "
                    f"(cached result is for a different bot: {_bot_path})."
                )
            elif not _reliable:
                exploitability_weaknesses = (
                    f"Exploitability probe data for {_source_bot} is unreliable "
                    f"(only {_games} games/probe, need >= {_MIN_RELIABLE_PROBE_GAMES}). "
                    f"Treating as no data."
                )
                _log.warning("exploitability probe unreliable: %s hands for %s",
                             _games, _source_bot)
            else:
                _parts = []
                if _overall is not None:
                    _parts.append(f"overall_score={_overall:.2f}/1.0")
                if _games is not None:
                    _parts.append(f"{int(_games)} games per probe")
                if _bot_path:
                    _parts.append(f"vs {_bot_path}")
                header = ("Exploitability probe results (4 probe bots: min_bettor, "
                          "overbettor, check_raiser, always_caller): "
                          + ", ".join(_parts)) if _parts else (
                          "Exploitability probe results (4 probe bots):")
                if _weak_list:
                    exploitability_weaknesses = header + "\nWEAKNESSES:\n- " + "\n- ".join(_weak_list)
                else:
                    exploitability_weaknesses = header + "\nNo exploitable weaknesses detected."
    except Exception as e:
        # Never silent: a parse/read failure here used to swallow the whole
        # block. Log it so a corrupt/missing exploitability.json stays observable.
        _log.warning("Exploitability probe read failed for source_v=%s: %s", source_v, e)

    data = await _run_master_analysis(
        source_v, next_v, stagnation_info, ui,
        match_analysis=match_analysis,
        performance_verification=performance_verification,
        replay_spotlight=replay_spotlight,
        bot_action_stats=bot_action_stats,
        battle_experience=battle_experience,
        exploitability_weaknesses=exploitability_weaknesses,
        opponent_profiles=opponent_profiles,
        research_proposals=research_proposals,
    )

    if data is None:
        _nf = _bump_master_fail_count(next_v, source_v)
        return {"content": [{"type": "text", "text": json.dumps({"error": "Master failed to produce a valid plan after 3 retries", "fail_count": _nf, "directive": "If run_master keeps failing, do NOT retry indefinitely — call abandon_generation to restart the generation fresh.", "logs": ui.get_output()})}]}
    data = _normalize_and_log_master_plan_paths(data, source_v, next_v)
    _touch_master_checkpoint(next_v, source_v, phase="master_plan_ready")

    # --- P0-1: Post-Master Plan Verification Audit ---
    # Capped retry loop: on audit rejection with retry_recommended, re-plan AND
    # re-audit the new plan, up to MAX_MASTER_AUDIT_RETRIES. The audit_attempt
    # counter is persisted in the checkpoint so a crash-resume does not re-burn
    # the master LLM budget (bug #6b). Without this cap, a persistently-rejecting
    # auditor + an orchestrator that re-calls run_master forms a token-burning
    # loop that consumes the whole CYCLE_TIMEOUT (observed: v81 stuck 3x run_master).
    master_audit_ctx = None
    MAX_MASTER_AUDIT_RETRIES = 2
    try:
        from audit_agents import _run_master_plan_audit
        from evolution_infra import read_pipeline_checkpoint
        _ckpt0 = read_pipeline_checkpoint() or {}
        # `or 0` defends against a stored null: prepare_next_gen writes the
        # checkpoint with audit_attempt=None (default), and across the next_v
        # change the merge guard fails so it serializes as JSON null. A bare
        # .get("audit_attempt", 0) returns the stored None (not the default),
        # and int(None) raises TypeError that the surrounding try/except would
        # swallow — silently disabling the audit on every normal generation.
        _audit_attempt = int(_ckpt0.get("audit_attempt") or 0)

        for _audit_iter in range(MAX_MASTER_AUDIT_RETRIES + 1):
            _touch_master_checkpoint(
                next_v,
                source_v,
                phase="master_plan_audit_start",
                audit_attempt=_audit_attempt,
            )
            try:
                from evidence_snapshot import validate_h2h_citations_against_snapshot
                _h2h_citation_errors = validate_h2h_citations_against_snapshot(data, next_v)
            except Exception:
                _h2h_citation_errors = []
            if _h2h_citation_errors:
                audit_result = {
                    "plan_coherent": False,
                    "contradiction_found": True,
                    "contradictions": _h2h_citation_errors[:10],
                    "experience_alignment": "misaligned",
                    "direction_novelty": "incremental",
                    "overall_pass": False,
                    "feedback": (
                        "Master plan H2H citations disagree with the stable generation "
                        "H2H snapshot. Correct the cited raw games/a_wins/b_wins counts "
                        "against web/core/results/v{}/evidence_snapshot/head_to_head.json: "
                        "{}".format(next_v, "; ".join(_h2h_citation_errors[:6]))
                    ),
                    "retry_recommended": True,
                    "deterministic_h2h_snapshot_check": True,
                }
            else:
                try:
                    audit_result = await _run_master_plan_audit(data, source_v, ui, next_v=next_v)
                except TypeError as _audit_te:
                    if "next_v" not in str(_audit_te) and "keyword" not in str(_audit_te):
                        raise
                    audit_result = await _run_master_plan_audit(data, source_v, ui)
            master_audit_ctx = audit_result  # Save for audit_context chain
            if audit_result.get("overall_pass", True):
                break  # plan passed audit
            # Rejected
            log_system_event("pipeline.master_audit_rejected", "warn",
                             f"Master plan audit rejected for v{next_v} (attempt {_audit_attempt + 1}): {audit_result.get('feedback', '')[:200]}",
                             {"next_v": next_v, "audit": audit_result, "audit_attempt": _audit_attempt + 1})
            if _audit_attempt + 1 > MAX_MASTER_AUDIT_RETRIES:
                _nf = _bump_master_fail_count(next_v, source_v, value=_audit_attempt + 1)
                log_system_event("pipeline.master_audit_exhausted_abandon", "error",
                                 f"Master audit exhausted {MAX_MASTER_AUDIT_RETRIES} retries for v{next_v} — blocking plan",
                                 {"next_v": next_v, "source_v": source_v,
                                  "fail_count": _nf, "audit": audit_result})
                try:
                    from orchestrator_session import _clear_orchestrator_session
                    _clear_orchestrator_session()
                except Exception:
                    pass
                try:
                    from tool_bot_management import _do_abandon_generation
                    _abandon_result = await _do_abandon_generation(
                        reason=f"master_audit_rejected v{next_v}: {audit_result.get('feedback', '')[:300]}"
                    )
                except Exception as _ae:
                    _abandon_result = {"abandoned": False, "error": str(_ae)}
                return _json_tool_result({
                    "error": "MASTER_AUDIT_REJECTED",
                    "fail_count": _nf,
                    "audit": audit_result,
                    **_abandon_result,
                    "directive": (
                        "Master plan audit is blocking. This generation was abandoned "
                        "after audit retries were exhausted. Start a fresh generation; "
                        "do not execute workers from the rejected plan."
                    ),
                    "logs": ui.get_output(),
                })
            # Re-plan with rejection feedback, then re-audit the new plan
            _audit_attempt += 1
            _touch_master_checkpoint(
                next_v,
                source_v,
                phase="master_audit_rejected",
                audit_attempt=_audit_attempt,
                audit_context={"master_audit_rejection": master_audit_ctx},
            )
            log_system_event("pipeline.master_audit_blocked", "error",
                             f"Master plan audit blocked v{next_v}; retrying Master attempt {_audit_attempt}",
                             {"next_v": next_v, "source_v": source_v,
                              "audit_attempt": _audit_attempt, "audit": audit_result})
            performance_verification += (
                f"\n\n# PLAN AUDIT REJECTION (attempt {_audit_attempt})\n"
                f"The previous plan was rejected by the Plan Verification Auditor.\n"
                f"Issues: {audit_result.get('feedback', '')}\n"
                f"Contradictions: {', '.join(audit_result.get('contradictions', []))}\n"
                f"Direction assessment: {audit_result.get('direction_novelty', 'unknown')}\n"
                f"You MUST address these issues in your new plan.\n"
            )
            performance_verification, _retry_sanitized = _sanitize_unverified_replay_citations(
                performance_verification, _anchor_map
            )
            if _retry_sanitized:
                try:
                    log_system_event(
                        "pipeline.master_retry_context_citations_sanitized",
                        "warn",
                        f"Master retry v{next_v} context had stale replay IDs redacted",
                        {
                            "next_v": next_v,
                            "source_v": source_v,
                            "count": _retry_sanitized,
                        },
                    )
                except Exception:
                    pass
            data = await _run_master_analysis(
                source_v, next_v, stagnation_info, ui,
                match_analysis=match_analysis,
                performance_verification=performance_verification,
                replay_spotlight=replay_spotlight,
                bot_action_stats=bot_action_stats,
                battle_experience=battle_experience,
                exploitability_weaknesses=exploitability_weaknesses,
                opponent_profiles=opponent_profiles,
                research_proposals=research_proposals,
            )
            if data is None:
                _nf = _bump_master_fail_count(next_v, source_v, value=_audit_attempt)
                return {"content": [{"type": "text", "text": json.dumps({"error": "Master failed after audit retry", "fail_count": _nf, "directive": "If run_master keeps failing, call abandon_generation to restart fresh.", "logs": ui.get_output()})}]}
            data = _normalize_and_log_master_plan_paths(data, source_v, next_v)
            _touch_master_checkpoint(
                next_v,
                source_v,
                phase="master_retry_plan_ready",
                audit_attempt=_audit_attempt,
            )
            # Persist audit_attempt so crash-resume resumes at this count (not 0)
            try:
                _ckpt_retry = read_pipeline_checkpoint() or {}
                write_pipeline_checkpoint(
                    next_v, source_v,
                    _ckpt_retry.get("stage", "direction_audited"),
                    audit_attempt=_audit_attempt,
                    direction_audit=_ckpt_retry.get("direction_audit") or direction_audit,
                    audit_context={"master_audit_rejection": master_audit_ctx},
                    touch_stage_timestamp=True,
                )
            except Exception:
                pass
            log_system_event("pipeline.master_audit_retry", "info",
                             f"Master re-planned after audit rejection for v{next_v} (attempt {_audit_attempt})",
                             {"next_v": next_v})
    except Exception as e:
        _log.warning("Master plan audit error (skipping): %s", e)
        try:
            log_system_event('pipeline.master_audit_error', 'warn',
                f'Master plan audit error for v{next_v}: {e}',
                {"next_v": next_v, "source_v": source_v, "error": str(e)})
        except Exception:
            pass

    # ── fix-5: Cross-gen direction pivot check ──
    # Three conditions must ALL be true to force re-planning:
    #   1. direction_audit confidence == "high"
    #   2. exhausted_directions is non-empty
    #   3. Same exhausted direction appeared in >=2 consecutive prior generations
    #      (checked via cross_gen_exhausted_history.jsonl)
    # This breaks the "same axis dies 6 gens in a row" loop (v138-v143 stackoff guard).
    if direction_audit and not direction_audit.get("llm_failed"):
        _confidence = direction_audit.get("confidence", "low")
        _exhausted_dirs = direction_audit.get("exhausted_directions", [])
        if _confidence == "high" and _exhausted_dirs:
            # Record this generation's exhausted directions
            _record_cross_gen_exhausted(next_v, source_v, _exhausted_dirs, _confidence)
            # Check for consecutive same-axis exhaustion
            _pivot_axis = _check_consecutive_exhaustion(next_v, _exhausted_dirs)
            if _pivot_axis:
                _plan_repeats, _matched_direction = _plan_repeats_exhausted_direction(
                    data, _exhausted_dirs
                )
                if not _plan_repeats:
                    log_system_event(
                        "pipeline.cross_gen_pivot_satisfied", "info",
                        f"Cross-gen pivot axis '{_pivot_axis}' is present in history, "
                        f"but Master v{next_v} plan appears to use a different execution axis; continuing.",
                        {"next_v": next_v, "source_v": source_v,
                         "pivot_axis": _pivot_axis,
                         "exhausted_directions": _exhausted_dirs,
                         "plan_repeats_exhausted": False},
                    )
                else:
                    _bump_master_fail_count(next_v, source_v)
                    try:
                        log_system_event("pipeline.cross_gen_pivot_runtime_only", "warn",
                                         f"Cross-gen direction pivot triggered for v{next_v}: "
                                         f"exhausted axis '{_pivot_axis}' persisted >=2 consecutive gens "
                                         f"and the accepted Master plan still matches '{_matched_direction}'. "
                                         f"Forcing structural alternative without writing experience_pool.md before commit.",
                                         {"next_v": next_v, "source_v": source_v,
                                          "pivot_axis": _pivot_axis,
                                          "matched_direction": _matched_direction,
                                          "exhausted_directions": _exhausted_dirs,
                                          "experience_pool_marked": False})
                    except Exception:
                        pass
                    return {"content": [{"type": "text", "text": json.dumps({
                        "error": "CROSS_GEN_PIVOT",
                        "pivot_axis": _pivot_axis,
                        "matched_direction": _matched_direction,
                        "exhausted_directions": _exhausted_dirs,
                        "confidence": _confidence,
                        "directive": (
                            f"Direction pivot FORCED: the exhausted axis '{_pivot_axis}' has been "
                            f"flagged for >=2 consecutive generations with HIGH confidence, and "
                            f"the current plan still matches '{_matched_direction}'. You MUST produce a "
                            f"FUNDAMENTALLY different plan — new structural mechanism, new opp-line "
                            f"signal, or a completely different strategic axis. "
                            f"Do NOT re-tune constants in the same exhausted area."
                        ),
                        "logs": ui.get_output(),
                    })}]}

    # Pre-compute exhausted keywords once (used by _validate_master_plan and potentially others)
    try:
        from plan_compiler import compile_master_plan
        data, _compile_meta = compile_master_plan(
            data,
            next_v=next_v,
            target_dir=get_bot_dir(next_v),
            project_root=PROJECT_ROOT,
        )
        if _compile_meta.get("compiled"):
            log_system_event(
                "pipeline.master_plan_compiled",
                "info",
                f"Master plan v{next_v}: compiled {len(_compile_meta.get('compiled_tasks', []))} oversized worker prompt(s)",
                {"next_v": next_v, "source_v": source_v, "compiler": _compile_meta},
            )
    except Exception as _compile_exc:
        log_system_event(
            "pipeline.master_plan_compile_failed",
            "error",
            f"Master plan compiler failed for v{next_v}: {_compile_exc}",
            {"next_v": next_v, "source_v": source_v, "error": str(_compile_exc)[:500]},
        )

    _exhausted_kw = _extract_exhausted_keywords()
    plan_errors, plan_warnings = _validate_master_plan(data, next_v=next_v, precomputed_exhausted_keywords=_exhausted_kw)
    if plan_warnings:
        try:
            log_system_event("pipeline.master_boundary", "warning",
                             f"Master plan boundary warnings for v{next_v}: {plan_warnings}",
                             {"next_v": next_v, "warnings": plan_warnings})
        except Exception:
            pass
    if plan_errors:
        _validation_ctx = {
            "master_validation": {
                "errors": plan_errors,
                "warnings": plan_warnings,
                "plan_analysis": data.get("analysis", "")[:1000]
                if isinstance(data, dict) else "",
            }
        }
        _nf = _bump_master_fail_count(
            next_v,
            source_v,
            audit_context=_validation_ctx,
        )
        _severity = "error" if _nf >= MAX_MASTER_TOTAL_FAILURES else "warn"
        try:
            log_system_event(
                "pipeline.master_validation_failed",
                _severity,
                f"Master plan validation failed for v{next_v} "
                f"(fail_count={_nf}): {'; '.join(plan_errors[:3])}",
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "fail_count": _nf,
                    "validation_errors": plan_errors,
                    "validation_warnings": plan_warnings,
                },
            )
        except Exception:
            pass
        try:
            ui.log_history(
                "Master plan validation failed: " + "; ".join(plan_errors[:5]),
                "error",
            )
        except Exception:
            pass

        if _nf >= MAX_MASTER_TOTAL_FAILURES:
            try:
                from orchestrator_session import _clear_orchestrator_session
                _clear_orchestrator_session()
            except Exception:
                pass
            try:
                from tool_bot_management import _do_abandon_generation
                _abandon_result = await _do_abandon_generation(
                    reason=(
                        f"master_validation_failed v{next_v}: "
                        f"{'; '.join(plan_errors[:3])[:300]}"
                    )
                )
            except Exception as _ae:
                _abandon_result = {"abandoned": False, "error": str(_ae)}
            return _json_tool_result({
                "error": "MASTER_VALIDATION_EXHAUSTED",
                "fail_count": _nf,
                "validation_errors": plan_errors,
                "validation_warnings": plan_warnings,
                **_abandon_result,
                "directive": (
                    "Master plan validation failed too many times and this "
                    "generation was abandoned. Start a fresh generation; do "
                    "not execute workers from the invalid plan."
                ),
                "logs": ui.get_output(),
            })

        return _json_tool_result({
            "error": "MASTER_VALIDATION_FAILED",
            "fail_count": _nf,
            "validation_errors": plan_errors,
            "validation_warnings": plan_warnings,
            "invalid_plan_preview": {
                "analysis": str(data.get("analysis", ""))[:1000]
                if isinstance(data, dict) else "",
                "tasks": [
                    {
                        "worker_id": task.get("worker_id"),
                        "role": task.get("role"),
                        "target_files": task.get("target_files", []),
                        "worker_prompt_chars": len(str(task.get("worker_prompt", ""))),
                    }
                    for task in (data.get("tasks", []) if isinstance(data, dict) else [])[:3]
                    if isinstance(task, dict)
                ],
            },
            "directive": (
                "The Master plan passed schema/audit but failed hard validation. "
                "Do NOT execute workers from this plan. If retrying Master, "
                "the next plan must explicitly fix these validation_errors; "
                "after repeated failures the generation will be abandoned."
            ),
            "logs": ui.get_output(),
        })

    # Persist master plan to checkpoint so it survives crashes between master and workers
    _ckpt = _matching_checkpoint(next_v, source_v)
    existing_audit = _ckpt.get("direction_audit") if _ckpt else direction_audit
    # Mark direction_audit as resolved now that Master has produced a plan
    if existing_audit and existing_audit.get("repetition_detected"):
        existing_audit["resolved"] = True
    write_pipeline_checkpoint(next_v, source_v, "master_planned",
                              master_plan=data,
                              direction_audit=existing_audit,
                              worker_failure_count=_ckpt.get("worker_failure_count", 0) if _ckpt else 0,
                              audit_context={"master_audit": master_audit_ctx} if master_audit_ctx else None,
                              reset_generation_attempt=True,
                              reset_audit_attempt=True)

    try:
        log_system_event("pipeline.master_done", "info", f"Master planned v{next_v}: {len(data.get('tasks', []))} tasks",
                         {"next_v": next_v, "source_v": source_v, "num_tasks": len(data.get("tasks", [])),
                          "elapsed_sec": round(time.time() - _t0, 2)})
    except Exception:
        pass

    result = {"plan": data, "logs": ui.get_output()}
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2, ensure_ascii=False)}]}


# ──────────────────────────────────────────────
# Literature Probe Stage (A5, evolution-plan-refresh-jun21)
# ──────────────────────────────────────────────

@tool("run_literature_probe", "Deep-research a specific H2H weakness via web search (Exa) and synthesize ONE codable strategy proposal. Governed by research_governance (cooldown/blacklist/translation gate). Stagnation-triggered. Output is a HYPOTHESIS for run_master — it does NOT modify bot code directly.", {"source_v": int, "next_v": int, "h2h_weakness": str, "stagnation_info": str})
async def run_literature_probe(args):
    """DeepEvolve plan→search→reflect→write loop with Ratchet governance.

    Triggered by the orchestrator when stagnation ≥ 2 gens or direction-audit
    flags repetition. Uses web search (Exa, connected MCP) to find concrete,
    codable strategy improvements for the current bot's biggest H2H weakness.
    The output is a hypothesis pool entry (research_governance.add_candidate),
    NOT a direct code edit — run_master may surface it to workers as a hypothesis.
    """
    import asyncio as _asyncio
    _t0 = time.time()
    source_v = args.get("source_v")
    next_v = args.get("next_v")
    if next_v is None:
        return {"content": [{"type": "text", "text": json.dumps({"error": "Missing next_v"})}]}
    h2h_weakness = args.get("h2h_weakness", "") or ""
    stagnation_info = args.get("stagnation_info", "") or ""

    checkpoint_probe = _read_literature_probe_checkpoint(
        next_v,
        source_v=source_v,
        h2h_weakness=h2h_weakness,
        stagnation_info=stagnation_info,
    )
    if checkpoint_probe:
        try:
            event_type = "pipeline.literature_probe_checkpoint_cached"
            log_system_event(
                event_type,
                "info",
                f"literature_probe v{next_v}: using checkpoint result",
                {"next_v": next_v, "source_v": checkpoint_probe.get("source_v"),
                 "reason": checkpoint_probe.get("reason"),
                 "candidate_id": checkpoint_probe.get("candidate_id"),
                 "context_mismatch_reused": checkpoint_probe.get("context_mismatch_reused", False)},
            )
        except Exception:
            pass
        return _json_tool_result(checkpoint_probe)

    cached_probe = _read_literature_probe_cache(
        next_v,
        source_v=source_v,
        h2h_weakness=h2h_weakness,
        stagnation_info=stagnation_info,
    )
    if cached_probe:
        try:
            log_system_event(
                "pipeline.literature_probe_cached",
                "info",
                f"literature_probe v{next_v}: using cached result",
                {"next_v": next_v, "source_v": cached_probe.get("source_v"),
                 "reason": cached_probe.get("reason"),
                 "candidate_id": cached_probe.get("candidate_id")},
            )
        except Exception:
            pass
        _persist_literature_probe_result(next_v, source_v, cached_probe)
        return _json_tool_result(cached_probe)

    # ── A6 governance gate: cooldown / blacklist / kill-switch ──
    try:
        from research_governance import should_trigger_web_retrieval
        if not should_trigger_web_retrieval(next_v):
            try:
                log_system_event("research_governance.skipped", "info",
                                 f"run_literature_probe skipped for v{next_v} (cooldown/disabled)",
                                 {"next_v": next_v})
            except Exception:
                pass
            payload = {
                "skipped": True,
                "reason": "web retrieval in cooldown or disabled by governance",
                "next_v": next_v,
                "source_v": source_v,
                "weakness": h2h_weakness,
                "stagnation_info": stagnation_info,
            }
            try:
                payload = _write_literature_probe_cache(next_v, payload)
                _persist_literature_probe_result(next_v, source_v, payload)
            except Exception:
                pass
            return _json_tool_result(payload)
    except Exception as e:
        return {"content": [{"type": "text", "text": json.dumps({"error": f"governance gate failed: {e}"})}]}

    ui = _get_ui()
    try:
        from llm_query import run_claude_query, parse_json_output
        from evolution_infra import get_logs_dir, RESULTS_DIR as _RESULTS_DIR
        from research_governance import add_candidate, translation_gate
    except Exception as e:
        return {"content": [{"type": "text", "text": json.dumps({"error": f"import failed: {e}"})}]}

    probe_prompt_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "prompts", "literature_probe_prompt.md")
    try:
        with open(probe_prompt_path, encoding="utf-8") as f:
            probe_template = f.read()
    except Exception as e:
        return {"content": [{"type": "text", "text": json.dumps({"error": f"prompt load failed: {e}"})}]}

    log_dir = get_logs_dir(next_v)
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    probe_log = log_dir / "literature_probe_io.txt"

    # ── Compose the research brief ──
    weakness = h2h_weakness.strip() or (
        "General postflop stack-off leak: 0%-fold facing river all-ins (made_strength "
        "0.40-0.50 always calls). Need optimal fold frequency vs polarized jam."
    )
    brief = (
        f"{probe_template}\n\n"
        f"## Current H2H weakness to research\n{weakness}\n\n"
        f"## Stagnation context\n{stagnation_info or 'Stagnation detected — current axis exhausted.'}\n\n"
        f"## Source bot version\nv{source_v}\n\n"
        f"Now execute the 4 steps (PLAN → SEARCH → REFLECT → WRITE) and return the final "
        f"WRITE-step JSON. You have web search tools available — use them for the SEARCH step."
    )

    # ── Single research agent run (plan/search/reflect/write in one query, with web tools) ──
    # The agent has web search (Exa MCP, connected) + WebSearch. Domain whitelist is in the prompt.
    try:
        log_system_event("pipeline.literature_probe_start", "info",
                         f"literature_probe v{next_v}: research query starting",
                         {"next_v": next_v, "source_v": source_v,
                          "timeout_s": LITERATURE_PROBE_TIMEOUT,
                          "log_file": str(probe_log)})
    except Exception:
        pass
    try:
        ui.clear_io()
        output, _, _ = await _asyncio.wait_for(
            run_claude_query(
                brief, [], ui,
                f"LITERATURE_PROBE (v{next_v})", probe_log,
                tools=["WebSearch"],  # built-in; Exa MCP auto-available (not in _BLOCKED_MCP_TOOLS)
            ),
            timeout=LITERATURE_PROBE_TIMEOUT,
        )
    except _asyncio.TimeoutError:
        elapsed = round(time.time() - _t0, 1)
        try:
            log_system_event("pipeline.literature_probe_timeout", "warn",
                             f"literature_probe v{next_v}: timed out after {LITERATURE_PROBE_TIMEOUT}s; continuing without web hypothesis",
                             {"next_v": next_v, "source_v": source_v,
                              "timeout_s": LITERATURE_PROBE_TIMEOUT,
                              "elapsed_sec": elapsed,
                              "log_file": str(probe_log)})
        except Exception:
            pass
        inject_text = (
            "## Research Proposal\n"
            "No codable proposal was produced because the web research stage timed out. "
            "Proceed with run_master using direction audit, H2H, replay, and experience-pool evidence."
        )
        payload = {
            "skipped": True,
            "reason": "literature_probe_timeout",
            "next_v": next_v,
            "source_v": source_v,
            "weakness": weakness,
            "stagnation_info": stagnation_info,
            "elapsed_sec": elapsed,
            "timeout_s": LITERATURE_PROBE_TIMEOUT,
            "inject_text": inject_text,
        }
        try:
            payload = _write_literature_probe_cache(next_v, payload)
            _persist_literature_probe_result(next_v, source_v, payload)
        except Exception:
            pass
        return _json_tool_result(payload)
    except Exception as e:
        try:
            log_system_event("pipeline.literature_probe_failed", "warn",
                             f"literature_probe v{next_v}: research query failed: {str(e)[:180]}",
                             {"next_v": next_v, "source_v": source_v,
                              "elapsed_sec": round(time.time() - _t0, 1),
                              "exception_type": type(e).__name__,
                              "error": str(e)[:1000],
                              "log_file": str(probe_log)})
        except Exception:
            pass
        payload = {
            "skipped": True,
            "reason": "literature_probe_failed",
            "next_v": next_v,
            "source_v": source_v,
            "weakness": weakness,
            "stagnation_info": stagnation_info,
            "elapsed_sec": round(time.time() - _t0, 1),
            "error": str(e)[:1000],
        }
        try:
            payload = _write_literature_probe_cache(next_v, payload)
            _persist_literature_probe_result(next_v, source_v, payload)
        except Exception:
            pass
        return _json_tool_result(payload)

    # ── Parse the WRITE-step proposal ──
    data, _mode = parse_json_output(output) if False else (None, None)
    try:
        from llm_query import parse_json_output_with_mode
        data, _fm = parse_json_output_with_mode(output)
    except Exception:
        data = None

    proposal = data if isinstance(data, dict) else None
    candidate_id = None
    gated_out = False
    if proposal and proposal.get("target_fn") and proposal.get("numeric_claim"):
        # A6 translation_gate + cap + blacklist enforced inside add_candidate
        candidate_id = add_candidate({
            "claim": proposal.get("claim", ""),
            "source_url": proposal.get("source_url", ""),
            "numeric_claim": proposal.get("numeric_claim", ""),
            "target_fn": proposal.get("target_fn", ""),
            "proposed_change": proposal.get("proposed_change", ""),
            "pseudocode": proposal.get("pseudocode", ""),
            "firing_tuple": proposal.get("firing_tuple", ""),
            "born_gen": next_v,
        })
        gated_out = candidate_id is None
    elif proposal and proposal.get("claim") is None:
        # Honest null — no codable evidence. Not an error.
        pass

    # ── Persist the proposal + return text for master_prompt injection ──
    try:
        proposals_dir = _RESULTS_DIR / "research_proposals"
        proposals_dir.mkdir(parents=True, exist_ok=True)
        _payload = {
            "next_v": next_v, "source_v": source_v,
            "weakness": weakness,
            "stagnation_info": stagnation_info,
            "proposal": proposal,
            "candidate_id": candidate_id,
            "gated_out": gated_out,
            "elapsed_sec": round(time.time() - _t0, 1),
            "reason": "completed",
        }
        _payload = _write_literature_probe_cache(next_v, _payload)
        _persist_literature_probe_result(next_v, source_v, _payload)
    except Exception:
        pass

    try:
        log_system_event("pipeline.literature_probe", "info",
                         f"literature_probe v{next_v}: candidate_id={candidate_id} gated_out={gated_out}",
                         {"next_v": next_v, "candidate_id": candidate_id,
                          "target_fn": (proposal or {}).get("target_fn", "")})
    except Exception:
        pass

    # Text returned to the orchestrator: the proposal (for run_master hypothesis injection)
    result = {
        "next_v": next_v,
        "source_v": source_v,
        "candidate_id": candidate_id,
        "gated_out": gated_out,
        "proposal": proposal,
        "weakness": weakness,
        "stagnation_info": stagnation_info,
        "elapsed_sec": round(time.time() - _t0, 1),
        "reason": "completed",
    }
    result["inject_text"] = _literature_probe_inject_text(result)
    return _json_tool_result(result)


# ──────────────────────────────────────────────
# Worker Stage
# ──────────────────────────────────────────────

def _extract_exhausted_keywords():
    """Extract focused topic keywords from EXHAUSTED experience pool entries.

    For each [POSSIBLY EXHAUSTED] line, extracts:
    1. The section header (e.g., OPPONENT_MODELING, PARAMETER_TUNING)
    2. A cleaned short phrase (first clause before the explanation)
    Returns a list of (section, phrase) tuples for fuzzy matching.
    Returns an empty list if the file doesn't exist or has no EXHAUSTED entries.
    """
    if not EXPERIENCE_FILE.exists():
        return []
    try:
        text = EXPERIENCE_FILE.read_text(encoding="utf-8")
    except Exception:
        return []

    keywords = []
    current_section = ""
    # Tolerant marker: matches [POSSIBLY EXHAUSTED] AND [EXHAUSTED — hard gate]
    # (any bracketed tag containing the word EXHAUSTED). Using a regex avoids the
    # round-trip closure bug where an LLM-appended "— hard gate" suffix made the
    # old literal "[POSSIBLY EXHAUSTED]" check silently miss every marker,
    # disabling the exhausted-direction hard gate (returned []  -> gate no-op).
    marker_re = re.compile(r"\[[A-Z ]*EXHAUSTED[^\]]*\]")
    for line in text.splitlines():
        if line.startswith("## "):
            current_section = line.replace("## ", "").strip()
            continue
        if not marker_re.search(line):
            continue
        # Skip non-direction sections: RECENT_LESSONS holds free-form critic
        # commentary (e.g. a 1188-char v82 review dump) that can contain an
        # inline [POSSIBLY EXHAUSTED] reference but is NOT a direction — extracted
        # verbatim it becomes a parasitic 84-token keyword that matches almost
        # any plan. Only top-level strategy sections hold real directions.
        if current_section.upper() == "RECENT_LESSONS":
            continue
        # Extract the topic phrase: everything before the explanation
        cleaned = marker_re.sub("", line).strip(" -•")
        if not cleaned:
            continue
        # Length cap: a genuine direction phrase is a clause, not a paragraph.
        # Real directions run ~300-400 chars; over-long entries (e.g. a 1188-char
        # critic-review dump) are commentary, not directions. 500 keeps real
        # directions while excluding dumps.
        if len(cleaned) > 500:
            continue
        # Take the first clause (before common joiners) as the core topic
        for sep in [" are exhausted", " has not ", " have repeatedly ", " shows "]:
            if sep in cleaned:
                cleaned = cleaned[:cleaned.index(sep)]
                break
        keywords.append((current_section.lower(), cleaned.lower()))
    return keywords


# Generic poker action/street vocabulary that appears in almost every plan.
# Excluded from "distinctive" token matching so that a legitimate novel plan
# mentioning fold/call/sizing isn't falsely flagged as repeating an EXHAUSTED
# direction. Only direction-characteristic words (parameter/tuning/structural/
# commitment/barrel/archetype/...) count as distinctive.
_EXHAUSTED_BLOCKLIST = frozenset({
    "fold", "call", "raise", "bet", "bets", "check", "allin", "pot",
    "sizing", "threshold", "thresholds", "margin", "margins",
    "equity", "hand", "hands", "street", "streets",
    "flop", "turn", "river", "preflop", "postflop",
})


# Direction-characteristic tokens that uniquely identify an EXHAUSTED direction
# (as opposed to generic poker vocabulary). The HARD gate (_validate_master_plan)
# additionally requires >=1 of these in the prompt so a legitimate novel plan
# that merely shares generic strategy words (value/strategy/strong/tier/...) is
# not falsely rejected. Excludes constant/margin/fold/grounded — too generic,
# they appear in legitimate opponent-stat / continuous-stat reframes (the very
# reframe v82's critic asked for).
_EXHAUSTED_DIRECTION_TOKENS = frozenset({
    "parameter", "tuning", "commitment",
    # NOTE: "mechanism", "canonical", "archetype", "refactor" REMOVED — these
    # are generic structural-improvement verbs that fire on legitimate novel
    # plans (the source of 5+ false-positive blocks observed). Only keep tokens
    # that unambiguously characterize the truly-exhausted constant-tuning /
    # commitment-axis patterns.
    # NOTE: "continuous" is deliberately EXCLUDED — the POSTFLOP_STRATEGY
    # exhausted phrase says "refactor old archetype guard to continuous-stat"
    # where "continuous" is the refactor TARGET, not the exhausted pattern.
    # Including it would reject legitimate continuous-stat opponent-modeling
    # plans (the exact reframe v82's critic asked for).
})


def _fuzzy_match_exhausted(prompt_text: str, keywords: list, require_direction_token: bool = False) -> bool:
    """Check if prompt_text matches an EXHAUSTED keyword using fuzzy token matching.

    Distinctive tokens EXCLUDE generic poker vocabulary (_EXHAUSTED_BLOCKLIST) so
    that a legitimate novel plan isn't rejected just for mentioning fold/call/
    sizing. A match requires >=2 distinctive tokens (the BLOCKLIST makes "2
    tokens" meaningful — direction-characteristic words, not fold/call/sizing).

    When require_direction_token=True (HARD gate in _validate_master_plan), also
    requires >=1 _EXHAUSTED_DIRECTION_TOKEN in the prompt. This eliminates the
    remaining false-positive class where a long EXHAUSTED prose entry shares
    generic words (value/strategy/strong/tier) with a legitimate novel plan,
    without losing true positives (a real fold-gate reintroduction mentions
    mechanism/canonical/archetype; a real constant-tuning plan mentions
    parameter/tuning). The soft warning path (execute_workers) keeps the default
    False to preserve recall — warnings are cheap.
    """
    import re
    prompt_clean = re.sub(r'[^a-z0-9\s]', '', prompt_text.lower())

    for section, phrase in keywords:
        distinctive = set()
        for src in (phrase, section):
            # Split on any non-alphanumeric (spaces, underscores, slashes, dashes)
            # so 'parameter_tuning' and 'constant/margin' tokenize the same way
            # the prompt does ('parameter tuning', 'constant margin').
            for t in re.split(r'[^a-z0-9]+', src):
                if len(t) > 3 and t not in _EXHAUSTED_BLOCKLIST:
                    distinctive.add(t)
        if not distinctive:
            continue
        matches = sum(1 for t in distinctive if t in prompt_clean)
        # Match on >=2 distinctive (non-generic) tokens; for very short keywords
        # (<=2 distinctive, e.g. a bare section name) require all to match.
        if matches < min(2, len(distinctive)):
            continue
        # HARD gate: additionally require a direction-characteristic token, so a
        # plan that merely shares generic words (value/strategy/strong/tier) is
        # not rejected.
        if require_direction_token:
            direction_hits = sum(1 for t in _EXHAUSTED_DIRECTION_TOKENS if t in prompt_clean)
            if direction_hits < 1:
                continue
        return True
    return False


_EXHAUSTED_NEGATIVE_CUES = (
    "do not", "don't", "must not", "never", "avoid", "forbidden",
    "prohibit", "prohibited", "unchanged", "preserve", "no retune",
    "no tuning", "without modifying", "do n't", "should not",
    "may violate an exhausted", "marks this area as exhausted",
    "not the current bottleneck", "shelve until",
)


_REFACTOR_AWAY_CLAUSES = (
    re.compile(
        r"\b(replace|replaces|replaced|replacing)\s+[^.;:\n]+?\s+\b(with|by)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"[,;]\s*(replace|replaces|replaced|replacing|instead of|rather than)\b[^.;:\n]*",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(structural\s+)?replacement\s+for\b[^.;:\n]*",
        re.IGNORECASE,
    ),
)


def _strip_refactor_away_clauses(text: str) -> str:
    """Remove prose describing the obsolete mechanism being replaced."""
    cleaned = text
    for pattern in _REFACTOR_AWAY_CLAUSES:
        cleaned = pattern.sub(" ", cleaned)
    return cleaned


def _positive_execution_text_from_task(task: dict) -> str:
    """Extract the task's positive implementation intent.

    Fields that are explicitly prohibitions (`prohibited_files`, `do_not_touch`)
    are excluded. Free-form prompts are split into sentences and negative
    constraint sentences are dropped.
    """
    if not isinstance(task, dict):
        return ""
    positive_fields = (
        "behavior_hypothesis",
        "expected_diff_shape",
        "targeted_failure",
        "worker_goal",
        "implementation_plan",
        "merge_policy",
        "worker_prompt",
        "instruction",
    )
    chunks = [str(task.get(field, "")) for field in positive_fields]
    splitter = re.compile(r"[\n;]+|(?<=[A-Za-z0-9_)])\.(?=\s+|$)")
    segments = []
    for chunk in chunks:
        for segment in splitter.split(chunk):
            text = _strip_refactor_away_clauses(segment).strip().lower()
            if not text:
                continue
            if any(cue in text for cue in _EXHAUSTED_NEGATIVE_CUES):
                continue
            segments.append(text)
    return "\n".join(segments)


def _plan_repeats_exhausted_direction(plan: dict, exhausted_directions: list[str]) -> tuple[bool, str]:
    """Return whether a Master plan actively repeats a currently exhausted axis.

    Cross-gen exhaustion is historical evidence, not proof that the new plan is
    bad. Only positive execution fields are inspected; `do_not_touch` and audit
    constraint prose are ignored so "avoid fold calibration" is not treated as
    fold calibration.
    """
    if not isinstance(plan, dict) or not exhausted_directions:
        return False, ""

    chunks = [
        str(plan.get("targeted_failure", "")),
        str(plan.get("expected_behavior_change", "")),
    ]
    for task in plan.get("tasks", []) or []:
        if not isinstance(task, dict):
            continue
        chunks.append(_positive_execution_text_from_task(task))
    sentence_splitter = re.compile(r"[\n;]+|(?<=[A-Za-z0-9_)])\.(?=\s+|$)")
    positive_segments = []
    for chunk in chunks:
        for segment in sentence_splitter.split(str(chunk)):
            segment_l = segment.lower()
            if not segment_l.strip():
                continue
            if any(cue in segment_l for cue in _EXHAUSTED_NEGATIVE_CUES):
                continue
            positive_segments.append(segment_l)
    plan_text = "\n".join(positive_segments)
    if not plan_text.strip():
        return False, ""
    target_files = set()
    for task in plan.get("tasks", []) or []:
        if not isinstance(task, dict):
            continue
        for f in task.get("target_files", []) or []:
            target_files.add(str(f).split("/")[-1].lower())
    fold_axis = any(
        any(term in str(direction).lower() for term in (
            "fold-side", "fold-threshold", "fold threshold", "fold gate",
            "opponent.py", "state.py", "_multibarrel_line_fold",
            "_estimate_bluff_frequency",
        ))
        for direction in exhausted_directions
    )
    offense_constructor = any(term in plan_text for term in (
        "semi-bluff", "semibluff", "raise constructor", "raise_construct",
        "draw equity", "fold equity", "chip path", "constructs a raise",
    ))
    fold_edit_terms = (
        "_multibarrel_line_fold", "_allin_polarized_equity_fold",
        "_river_potodds_equity_margin", "_estimate_bluff_frequency",
        "betsize_polarity", "fold threshold", "fold thresholds",
        "fold ceiling", "fold ceilings", "fold gate", "fold gates",
        "fold-side", "made_strength cutoff", "made_strength cutoffs",
        "bluff frequency",
    )
    fold_edit_verbs = (
        "adjust", "alter", "calibrate", "change", "edit", "increase",
        "lower", "modify", "narrow", "raise the", "recalibrate",
        "retune", "tune", "widen",
    )
    fold_position_cues = (
        "before the existing fold", "before any fold", "before fold",
        "ahead of the existing fold", "ahead of fold", "runs before",
        "run before", "not a fold", "never in a fold", "fold equity",
        "fold_to_raise",
    )

    def _is_explicit_fold_edit(segment: str) -> bool:
        if not any(term in segment for term in fold_edit_terms):
            return False
        # A new action constructor often has to be placed before existing fold
        # gates. That is a control-flow position, not a fold-gate retune.
        if any(cue in segment for cue in fold_position_cues):
            return False
        return any(verb in segment for verb in fold_edit_verbs)

    explicit_fold_edit = bool(target_files & {"opponent.py", "state.py"}) or any(
        _is_explicit_fold_edit(segment) for segment in positive_segments
    )
    if fold_axis and offense_constructor and not explicit_fold_edit:
        # A structural raise/semi-bluff constructor is the intended escape from
        # the fold-side axis. Do not treat mentions of "fold equity" or guarded
        # opponent fold-to-raise signals as fold-threshold calibration unless the
        # plan explicitly edits the exhausted fold code or targets those files.
        if not (target_files & {"opponent.py", "state.py"}):
            return False, ""
    plan_tokens = {
        t for t in re.split(r"[^a-z0-9]+", plan_text)
        if len(t) > 3 and t not in _EXHAUSTED_BLOCKLIST
    }

    for direction in exhausted_directions:
        tokens = {
            t for t in re.split(r"[^a-z0-9]+", str(direction).lower())
            if len(t) > 3 and t not in _EXHAUSTED_BLOCKLIST
        }
        if not tokens:
            continue
        matches = sorted(tokens & plan_tokens)
        if len(matches) >= min(2, len(tokens)):
            pivot_direction_tokens = _EXHAUSTED_DIRECTION_TOKENS | {
                "calibration", "frequency", "floor", "ceiling", "continuation",
            }
            if not any(t in pivot_direction_tokens for t in matches):
                continue
            return True, str(direction)
    return False, ""


# ──────────────────────────────────────────────
# Cross-generation local-optima constraint (mechanical backstop)
# ──────────────────────────────────────────────
# When the previous generation was rejected by the Critic as a local optimum,
# or the experience pool marks a direction EXHAUSTED, inject a hard constraint
# into the Master so it stops re-proposing the same exhausted direction
# (observed: v82 master re-proposed constant-tuning after critic rejected it
# for exactly that). This is independent of the direction_audit LLM gate
# (which historically under-detects — v82 repetition_detected=false despite
# the pool flagging constant-tuning EXHAUSTED), so it works even when the
# LLM auditor fails to flag repetition.

CROSS_GEN_MARKER = "# CROSS-GEN LOCAL-OPTIMA CONSTRAINT"


def _load_recent_critic_local_optima(next_v, max_entries=3):
    """Load recent critic local-optima rejections from worker_failures.jsonl.

    The file is append-only and accumulates across generations. Selects critic
    entries with local_optima_warning=True and gen <= next_v (the just-rejected
    version is included — that is the loop we want to break), dedups by gen
    (latest timestamp wins; retry_workers can reject the same gen repeatedly).

    Returns [(gen, reason, error_first_line), ...] most-recent-gen first.

    _record_quality_failure (tool_gates.py) filters `v is not False`, so
    local_optima_warning=False is never written — `is True` selection is exact,
    and reviewer/worker records (which lack the field) are skipped.
    """
    try:
        from evolution_infra import WORKER_FAILURES_FILE, locked_file
    except Exception:
        return []
    if not WORKER_FAILURES_FILE.exists():
        return []
    by_gen = {}
    try:
        with locked_file(WORKER_FAILURES_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if e.get("local_optima_warning") is not True:
                    continue
                if str(e.get("worker_id", "")) != "critic":
                    continue
                g = e.get("gen")
                if g is None or g > next_v or g < next_v - 8:
                    continue
                ts = e.get("timestamp", 0)
                if g not in by_gen or ts > by_gen[g][3]:
                    reason = (e.get("local_optima_reason") or "").strip()
                    err_first = (e.get("error", "")).split("\n")[0][:150]
                    by_gen[g] = (g, reason, err_first, ts)
    except Exception:
        return []
    return [t[:3] for t in sorted(by_gen.values(), key=lambda x: -x[0])][:max_entries]


def _build_cross_gen_constraint_block(next_v):
    """Build a cross-generation mandatory constraint block from prior critic
    local-optima rejections + experience-pool EXHAUSTED directions.

    Returns "" (no injection) when there is neither a recent critic local-optima
    rejection nor any EXHAUSTED direction — so first-ever generations and
    crossovers with no prior rejection are unaffected.

    Wording is deliberately NOT an unconditional FORBIDDEN: a Master that brings
    a structural new method + H2H evidence may still proceed in the direction,
    and legitimate opponent-stat-driven sizing (the very reframe v82's critic
    asked for) is explicitly permitted — this prevents over-generalized refusal.
    """
    lo_entries = _load_recent_critic_local_optima(next_v)
    exhausted = _extract_exhausted_keywords()
    if not lo_entries and not exhausted:
        return ""
    parts = [f"\n\n{CROSS_GEN_MARKER} (MANDATORY)\n"]
    if lo_entries:
        parts.append(
            "The PREVIOUS generation(s) were REJECTED by the Critic as a LOCAL OPTIMUM "
            "(stuck repeating the same exhausted direction). To proceed in that same "
            "direction you MUST provide a STRUCTURAL new method AND H2H evidence "
            "(>=100g vs a confirmed weak matchup); pure constant/margin tuning will be "
            "rejected again.\n"
            "Recent critic local-optima rejections:\n"
        )
        for g, reason, err_short in lo_entries:
            parts.append(f"- v{g}: {reason or err_short}\n")
    if exhausted:
        parts.append(
            "\nDirections the experience pool marks EXHAUSTED (tried repeatedly, no gain):\n"
        )
        for sec, phrase in exhausted:
            parts.append(f"- [{sec}] {phrase}\n")
    parts.append(
        "\nUnless you have a genuinely structural alternative + H2H evidence, AVOID these "
        "exact patterns. Do NOT over-generalize: legitimate opponent-stat-driven sizing, "
        "new decision systems, or structural refactors are still permitted and encouraged.\n"
    )
    return "".join(parts)


# ──────────────────────────────────────────────
# fix-5: Cross-gen direction pivot (consecutive exhaustion detector)
# ──────────────────────────────────────────────
# Tracks exhausted directions per generation in a JSONL file so that when the
# SAME semantic axis is flagged exhausted with HIGH confidence for >=2
# consecutive generations, run_master forces a structural pivot rather than
# allowing the bot to keep tuning constants on the dead axis (v138-v143
# stackoff guard, 6 gens same direction, 0 improvement).
#
# The JSONL file is append-only and fcntl-locked (consistent with daemon writes).
# Each record: {version, source_v, exhausted_directions: [...], confidence, ts}
# Semantic axis matching: compares exhausted_directions lists by SET INTERSECTION
# (not literal string match) so "river fold gate" and "postflop fold threshold"
# are recognized as the same axis when they share >=1 direction keyword.


def _record_cross_gen_exhausted(next_v, source_v, exhausted_directions, confidence):
    """Append a cross-gen exhausted history record for this generation.

    Called from run_master after direction_audit completes with non-empty
    exhausted_directions. Uses fcntl locking for safe concurrent writes.
    """
    from evolution_infra import CROSS_GEN_EXHAUSTED_HISTORY, locked_file
    record = {
        "version": next_v,
        "source_v": source_v,
        "exhausted_directions": list(exhausted_directions),
        "confidence": confidence,
        "timestamp": time.time(),
    }
    try:
        with locked_file(CROSS_GEN_EXHAUSTED_HISTORY, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        _log.warning("Failed to record cross-gen exhausted history: %s", e)


def _check_consecutive_exhaustion(next_v, current_exhausted, lookback=5, min_consecutive=2):
    """Check if any exhausted direction axis persisted across >=2 consecutive gens.

    Reads the last `lookback` records from cross_gen_exhausted_history.jsonl and
    checks for semantic overlap (set intersection of direction keywords) between
    consecutive entries. Returns the matched axis description if found, else None.

    Semantic matching: tokenizes each exhausted direction string into words,
    then checks if consecutive entries share >=1 direction-characteristic token
    (reuses _EXHAUSTED_DIRECTION_TOKENS for consistency).
    """
    from evolution_infra import CROSS_GEN_EXHAUSTED_HISTORY, locked_file
    if not CROSS_GEN_EXHAUSTED_HISTORY.exists():
        return None

    records = []
    try:
        with locked_file(CROSS_GEN_EXHAUSTED_HISTORY, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    # Only include records for generations BEFORE this one
                    # (don't compare the just-written record against itself)
                    if rec.get("version", 0) < next_v:
                        records.append(rec)
                except (json.JSONDecodeError, TypeError):
                    continue
    except Exception:
        return None

    # Take the last `lookback` records (most recent first)
    records.sort(key=lambda r: r.get("version", 0), reverse=True)
    records = records[:lookback]

    if len(records) < min_consecutive - 1:
        return None  # Not enough history

    def _tokenize_directions(directions):
        """Extract distinctive tokens from exhausted direction strings."""
        tokens = set()
        for d in directions:
            for word in re.split(r'[^a-z0-9]+', str(d).lower()):
                if len(word) > 3 and word not in _EXHAUSTED_BLOCKLIST:
                    tokens.add(word)
        return tokens

    current_tokens = _tokenize_directions(current_exhausted)
    if not current_tokens:
        return None

    # Check consecutive records (sorted newest-first) for axis overlap
    # We need >= min_consecutive-1 consecutive prior records that share axis
    consecutive_count = 0
    matched_axis = None
    for rec in records:
        rec_tokens = _tokenize_directions(rec.get("exhausted_directions", []))
        overlap = current_tokens & rec_tokens
        if overlap:
            consecutive_count += 1
            if matched_axis is None:
                matched_axis = ", ".join(sorted(overlap)[:3])
        else:
            break  # Non-consecutive breaks the streak

    if consecutive_count >= min_consecutive - 1:
        return matched_axis or "unknown_axis"
    return None


def _mark_axis_exhausted_in_pool(axis, version):
    """H5 (2026-06-29): write a cross-gen-pivot result back into experience_pool.md.

    `_check_consecutive_exhaustion` reads cross_gen_exhausted_history.jsonl and
    fires a pivot directive, but until that axis is marked in the experience
    pool the next run_master call cannot see it via `_extract_exhausted_keywords`
    (which only reads the pool). The result: master keeps proposing the same
    exhausted axis and the pivot keeps firing. This helper appends an
    `[EXHAUSTED — cross_gen_pivot auto-mark]` line to the pool so the marker_re
    regex in `_extract_exhausted_keywords` (matches `[A-Z ]*EXHAUSTED`) picks it
    up on the very next generation.

    Idempotent within a generation: a per-version tag in the line prevents the
    same (version, axis) from being appended twice.
    """
    try:
        from evolution_infra import locked_file
        if not EXPERIENCE_FILE.exists():
            return
        marker_line = f"- [EXHAUSTED — cross_gen_pivot auto-mark v{version}] {axis}"
        with locked_file(EXPERIENCE_FILE, "r", encoding="utf-8") as f:
            text = f.read()
        if marker_line in text:
            return  # already marked for this version+axis
        addition = marker_line + "\n"
        if "## EXHAUSTED" in text:
            # Insert right after the section header
            idx = text.index("## EXHAUSTED")
            nl = text.index("\n", idx)
            new_text = text[:nl + 1] + addition + text[nl + 1:]
        else:
            # No section yet — append one
            new_text = text.rstrip() + f"\n\n## EXHAUSTED (cross-gen pivot auto-marks)\n{addition}"
        with locked_file(EXPERIENCE_FILE, "w", encoding="utf-8") as f:
            f.write(new_text)
        log_system_event(
            "pipeline.cross_gen_pivot_marked", "warn",
            f"Marked axis '{axis}' EXHAUSTED in experience_pool for v{version}",
            {"version": version, "axis": axis},
        )
    except Exception as e:
        _log.warning("H5: _mark_axis_exhausted_in_pool failed for v%d axis=%s: %s", version, axis, e)


def _incremental_reset_next_dir(next_dir, source_dir):
    """Incremental reset: overwrite files present in source (undo worker edits to
    existing files), PRESERVE worker-created NEW files (absent from source). Returns
    the list of preserved NEW filenames.

    Invariants after this call:
      - files in both source+next -> identical to source (authoritative overwrite)
      - files only in next (worker-created NEW) -> untouched (survive the reset)
      - files only in source -> created
      - parent .completed sentinels are removed; commit_bot is the only writer
        allowed to mark a candidate complete
    """
    from evolution_infra import candidate_copy_ignore, is_candidate_copy_ignored_name

    source_names = {
        item.name
        for item in source_dir.iterdir()
        if not is_candidate_copy_ignored_name(item.name)
    }
    preserved = []
    # Walk next_dir entries: clean stale bytecode, preserve NEW files, remove files
    # that exist in source so the source copy overwrites authoritatively.
    for item in next_dir.iterdir():
        if is_candidate_copy_ignored_name(item.name):
            # Clean parent/runtime artifacts. .task_context is generated per
            # current plan by plan_compiler and must not survive resets.
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
        elif item.name not in source_names:
            # Worker-created NEW file absent from source: PRESERVE it.
            preserved.append(item.name)
        else:
            # Exists in source: remove so source copy overwrites authoritatively.
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
    # Copy all source entries into next_dir (skip parent/runtime artifacts).
    # Source files are recreated/overwritten; NEW files preserved above are untouched.
    for item in source_dir.iterdir():
        if is_candidate_copy_ignored_name(item.name):
            continue
        if item.is_dir():
            shutil.copytree(item, next_dir / item.name,
                            ignore=candidate_copy_ignore)
        else:
            shutil.copy2(item, next_dir / item.name)
    return preserved


def _checkpoint_plan_with_tasks(ckpt, tasks, replace_existing_tasks=False):
    """Return a checkpoint master_plan that can resume the given worker tasks."""
    existing_plan = ckpt.get("master_plan") if ckpt else None
    if isinstance(existing_plan, dict):
        if existing_plan.get("tasks") and not replace_existing_tasks:
            return existing_plan
        return {**existing_plan, "tasks": tasks}
    return {"tasks": tasks}


def _task_declared_scope_files(task, next_v):
    files = set()
    if not isinstance(task, dict):
        return files
    for key in ("target_files", "files_allowed", "must_change_files"):
        for target in task.get(key, []) or []:
            rel = _target_rel(target, next_v)
            if rel:
                files.add(rel)
    return files


def _plan_repair_scope_files(plan, next_v):
    files = set()
    if not isinstance(plan, dict):
        return files
    raw_scope = plan.get("repair_scope_files", []) or []
    if not isinstance(raw_scope, list):
        raw_scope = []
    for item in raw_scope:
        rel = _target_rel(item, next_v)
        if rel:
            files.add(rel)
    raw_tasks = plan.get("tasks", []) or []
    if not isinstance(raw_tasks, list):
        raw_tasks = []
    for task in raw_tasks:
        files.update(_task_declared_scope_files(task, next_v))
    return files


def _plan_with_accumulated_repair_scope(ckpt, plan, tasks, next_v):
    """Preserve final declared-scope coverage across in-place repair rounds.

    Rework execution may refresh ``tasks`` to only the newest blocker, but the
    candidate diff is cumulative from the source bot. Store a separate scope
    ledger so quality gates still recognize earlier successful repair edits
    without re-running those old workers.
    """
    if not isinstance(plan, dict):
        return plan
    existing_plan = ckpt.get("master_plan") if isinstance(ckpt, dict) else {}
    scope = set()
    scope.update(_plan_repair_scope_files(existing_plan, next_v))
    scope.update(_plan_repair_scope_files(plan, next_v))
    scope.update(_declared_scope_ledger_files(ckpt))
    for task in tasks or []:
        scope.update(_task_declared_scope_files(task, next_v))
    work_item = plan.get("work_item") if isinstance(plan.get("work_item"), dict) else {}
    existing_work_item = existing_plan.get("work_item") if isinstance(existing_plan.get("work_item"), dict) else {}
    is_crossover = (
        bool(ckpt.get("parent2_v"))
        or plan.get("strategy") == "crossover"
        or existing_plan.get("strategy") == "crossover"
        or str(work_item.get("kind", "")).startswith("crossover_")
        or str(existing_work_item.get("kind", "")).startswith("crossover_")
    )
    if is_crossover and ckpt.get("source_v") is not None:
        try:
            source_dir = get_bot_dir(ckpt.get("source_v"))
            next_dir = get_bot_dir(next_v)
            if source_dir.exists() and next_dir.exists():
                scope.update(
                    rel for rel in _py_files_changed_between(source_dir, next_dir)
                    if rel and "backup" not in rel
                )
        except Exception as exc:
            _log.debug("Could not accumulate crossover repair scope for v%s: %s", next_v, exc)
    if not scope:
        return plan
    return {**plan, "repair_scope_files": sorted(scope)}


def _task_matches_quality_blocker(task, blocker):
    if str(task.get("repair_blocker") or "") == blocker:
        return True
    if blocker == "size" and str(task.get("repair_blocker") or "") == "file_size":
        return True
    text = " ".join([
        str(task.get("worker_id", "")),
        str(task.get("role", "")),
        str(task.get("repair_blocker", "")),
        " ".join(str(x) for x in task.get("target_files", []) or []),
        str(task.get("worker_prompt", task.get("instruction", ""))),
    ]).lower()
    if blocker == "size":
        return (
            "file_size" in text
            or "line_count" in text
            or "line count" in text
            or "loc limit" in text
            or "oversized" in text
            or "wc -l" in text
            or re.search(r"\bsize\b", text) is not None
            or re.search(r"\d+L/\d+L", text) is not None
        )
    if blocker == "position_semantics":
        return any(marker in text for marker in ("position_semantics", "dealer", "small blind", "big blind", "sb", "bb"))
    return False


def _task_quality_recheck_blockers(task):
    """Return cheap static quality blockers this task is trying to repair.

    Generic ``quality_gate`` tasks are only skippable when their evidence maps to
    a checker we can rerun cheaply. Compile, smoke, decision, and national
    acceptance repairs still run because this callback is intentionally not a
    replacement for the full quality gate.
    """
    if not isinstance(task, dict):
        return set()
    contract = task.get("repair_contract") if isinstance(task.get("repair_contract"), dict) else {}
    blocker = _normalize_repair_blocker(contract.get("blocker") or task.get("repair_blocker"))
    text = " ".join([
        str(task.get("worker_id", "")),
        str(task.get("role", "")),
        str(task.get("repair_blocker", "")),
        str(contract.get("blocker", "")),
        str(contract.get("evidence", "")),
        " ".join(str(x) for x in task.get("target_files", []) or []),
        str(task.get("worker_prompt", task.get("instruction", ""))),
    ]).lower()

    blockers = set()
    if blocker == "file_size" or _task_matches_quality_blocker(task, "size"):
        blockers.add("file_size")
    if blocker == "position_semantics" or _task_matches_quality_blocker(task, "position_semantics"):
        blockers.add("position_semantics")
    if blocker == "national_native_contract" or _is_national_native_contract_failure_text(text):
        blockers.add("national_native_contract")
    if (
        "protected_contract" in text
        or "tcp action text" in text
        or "output must be json response int" in text
    ):
        blockers.add("protected_contract")
    if "reachability" in text:
        blockers.add("reachability")
    return blockers


def _normalize_repair_blocker(value):
    text = str(value or "").strip().lower()
    if text in {"size", "file_size", "line_count", "loc"}:
        return "file_size"
    if text in {"position", "position_semantics"}:
        return "position_semantics"
    if text in {"national_native", "national_native_contract", "native_tcp_contract"}:
        return "national_native_contract"
    if text in {"quality", "quality_gate", "protected_contract", "compile", "smoke_test"}:
        return "quality_gate"
    return text


def _task_target_filenames(tasks):
    files = set()
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        for target in task.get("target_files", []) or []:
            name = Path(str(target)).name
            if name:
                files.add(name)
    return files


def _quality_contract_signature(contract):
    if not isinstance(contract, dict):
        return ("", "")
    blocker = _normalize_repair_blocker(contract.get("blocker"))
    filename = Path(str(contract.get("file", ""))).name
    return (blocker, filename) if blocker and filename else ("", "")


def _quality_contract_signatures(ckpt, reviewer_feedback=""):
    return {
        signature
        for signature in (
            _quality_contract_signature(contract)
            for contract in _quality_repair_contracts(ckpt, reviewer_feedback)
        )
        if all(signature)
    }


def _task_quality_contract_signatures(tasks):
    signatures = set()
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        files = _task_must_change_filenames(task)
        contract = task.get("repair_contract") if isinstance(task.get("repair_contract"), dict) else {}
        blocker = _normalize_repair_blocker(
            contract.get("blocker")
            or task.get("repair_blocker")
        )
        contract_file = Path(str(contract.get("file", ""))).name
        if contract_file:
            files.add(contract_file)
        if blocker:
            for filename in files:
                signatures.add((blocker, filename))
            continue
        if _task_matches_quality_blocker(task, "size"):
            for filename in files:
                signatures.add(("file_size", filename))
        if _task_matches_quality_blocker(task, "position_semantics"):
            for filename in files:
                signatures.add(("position_semantics", filename))
        text = " ".join([
            str(task.get("worker_id", "")),
            str(task.get("role", "")),
            str(task.get("task_kind", "")),
            str(task.get("worker_prompt", task.get("instruction", ""))),
        ]).lower()
        if "quality_gate" in text or "protected_contract" in text:
            for filename in files:
                signatures.add(("quality_gate", filename))
    return signatures


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _quality_task_contract_refresh_reason(task, current_contract):
    """Return why a saved quality repair task should be regenerated."""
    if not isinstance(task, dict) or not isinstance(current_contract, dict):
        return ""
    signature = _quality_contract_signature(current_contract)
    blocker, filename = signature
    if blocker != "file_size":
        return ""

    saved = task.get("repair_contract") if isinstance(task.get("repair_contract"), dict) else {}
    saved_current = _int_or_none(saved.get("current_lines"))
    saved_limit = _int_or_none(saved.get("line_limit"))
    current_lines = _int_or_none(current_contract.get("current_lines"))
    line_limit = _int_or_none(current_contract.get("line_limit"))

    if line_limit is not None and saved_limit != line_limit:
        return f"{blocker}:{filename}:line_limit_changed"
    if current_lines is not None and saved_current != current_lines:
        return f"{blocker}:{filename}:current_lines_changed"

    prompt = str(task.get("worker_prompt", task.get("instruction", "")))
    if (
        current_lines is not None
        and line_limit is not None
        and current_lines - line_limit >= 200
        and "Large-overage requirement" not in prompt
    ):
        return f"{blocker}:{filename}:large_overage_prompt_outdated"
    return ""


def _is_file_size_repair_task(task):
    if not isinstance(task, dict):
        return False
    contract = task.get("repair_contract") if isinstance(task.get("repair_contract"), dict) else {}
    blocker = _normalize_repair_blocker(
        contract.get("blocker")
        or task.get("repair_blocker")
    )
    if blocker == "file_size":
        return True
    return _task_matches_quality_blocker(task, "size")


def _order_quality_repair_tasks(tasks):
    """Run semantic/protocol repairs before final file-size cleanup.

    Multiple quality blockers can target the same file. If a file-size cleanup
    runs first, a later semantic repair can add a line or two and re-break the
    size gate. Keep ordering stable except for moving file-size repairs to the
    end of the quality-rework batch.
    """
    indexed = list(enumerate(tasks or []))
    ordered = [
        task for _idx, task in sorted(
            indexed,
            key=lambda item: (1 if _is_file_size_repair_task(item[1]) else 0, item[0]),
        )
    ]
    return ordered


def _stale_quality_task_reason(tasks, ckpt, reviewer_feedback=""):
    """Return a refresh reason when saved quality tasks no longer match gate blockers."""
    if (
        not isinstance(ckpt, dict)
        or ckpt.get("stage") not in {"quality_failed", "repair_planned", "rework_running"}
    ):
        return ""
    current_contracts = {
        signature: contract
        for contract in _quality_repair_contracts(ckpt, reviewer_feedback)
        for signature in [_quality_contract_signature(contract)]
        if all(signature)
    }
    current = set(current_contracts)
    if not current:
        return ""
    task_signatures = _task_quality_contract_signatures(tasks)
    missing = sorted(current - task_signatures)
    if not missing:
        stale = []
        for task in tasks or []:
            for signature in sorted(_task_quality_contract_signatures([task]) & current):
                reason = _quality_task_contract_refresh_reason(task, current_contracts[signature])
                if reason:
                    stale.append(reason)
        if not stale:
            return ""
        return "stale current quality repair contract(s): " + ", ".join(sorted(set(stale)))
    return "missing current quality repair contract(s): " + ", ".join(
        f"{blocker}:{filename}" for blocker, filename in missing
    )


def _task_must_change_filenames(task):
    files = set()
    if not isinstance(task, dict):
        return files
    for key in ("must_change_files", "target_files"):
        for target in task.get(key, []) or []:
            name = Path(str(target)).name
            if name:
                files.add(name)
        if files:
            break
    return files


def _quality_failure_target_files(ckpt, reviewer_feedback=""):
    failures = [
        item for item in _quality_failure_items(ckpt)
        if not _is_declared_scope_failure_text(item)
    ]
    files = _extract_quality_failure_files(failures)
    if not files and reviewer_feedback and not _is_declared_scope_failure_text(reviewer_feedback):
        files = _extract_quality_failure_files([reviewer_feedback])
    return set(files)


def _quality_rework_skipper(next_dir, source_dir, next_v, source_v):
    """Return a per-task skip callback for cheap quality-repair rechecks.

    Full quality validation remains owned by run_quality_gates. This callback
    only avoids wasting LLM calls for blockers that are already cleared by an
    earlier repair worker in the same rework batch.
    """
    def remaining_blockers():
        blockers = {}
        checked = set()
        try:
            _total, oversized = check_code_size(next_dir, source_dir=source_dir)
            checked.add("file_size")
            if oversized:
                blockers["file_size"] = {Path(name).name for name, _lines, _limit in oversized}
        except Exception:
            pass
        try:
            from tool_gates import detect_position_semantics_errors
            position_errors = detect_position_semantics_errors(next_dir)
            checked.add("position_semantics")
            if position_errors:
                files = _extract_quality_failure_files(position_errors)
                blockers["position_semantics"] = set(files)
        except Exception:
            pass
        try:
            from protected_contracts import check_bot_protocol_contract
            protected_errors = check_bot_protocol_contract(next_dir)
            checked.add("protected_contract")
            if protected_errors:
                files = _extract_quality_failure_files(protected_errors)
                blockers["protected_contract"] = set(files)
        except Exception:
            pass
        try:
            from national_native import check_native_contract
            native_errors = check_native_contract(next_dir)
            checked.add("national_native_contract")
            if native_errors:
                files = _extract_quality_failure_files(native_errors)
                blockers["national_native_contract"] = set(files or ["national_bot.py"])
        except Exception:
            pass
        try:
            from code_verification import detect_new_function_reachability_warnings
            changed = _py_files_changed_between(source_dir, next_dir)
            reachability = detect_new_function_reachability_warnings(
                source_dir,
                next_dir,
                changed_files=changed,
            )
            checked.add("reachability")
            if reachability:
                files = _extract_quality_failure_files(reachability)
                blockers["reachability"] = set(files)
        except Exception:
            pass
        return blockers, checked

    def skipper(task):
        blockers, checked = remaining_blockers()
        task_blockers = _task_quality_recheck_blockers(task)
        if not task_blockers:
            return ""
        unchecked = task_blockers - checked
        if unchecked:
            return ""
        if not blockers:
            return "all cheap quality rework blockers already cleared by current code"
        task_files = _task_must_change_filenames(task)
        active_task_blockers = set(task_blockers) & set(blockers)
        if not active_task_blockers:
            return (
                "quality blocker(s) already cleared by current code: "
                + ", ".join(sorted(task_blockers))
            )
        if task_files:
            still_relevant = False
            for blocker in active_task_blockers:
                remaining_files = blockers.get(blocker) or set()
                if not remaining_files or task_files & remaining_files:
                    still_relevant = True
                    break
            if not still_relevant:
                return (
                    "quality blocker file(s) already cleared by current code: "
                    + ", ".join(sorted(task_files))
                )
        return ""

    return skipper


def _checkpoint_master_plan(ckpt):
    if not isinstance(ckpt, dict):
        return {}
    plan = ckpt.get("master_plan")
    return plan if isinstance(plan, dict) else {}


def _checkpoint_work_item(ckpt):
    plan = _checkpoint_master_plan(ckpt)
    work_item = plan.get("work_item")
    return work_item if isinstance(work_item, dict) else {}


def _is_precommit_rework_checkpoint(ckpt):
    if not isinstance(ckpt, dict):
        return False
    if ckpt.get("stage") == "precommit_failed":
        return True
    work_item = _checkpoint_work_item(ckpt)
    route = work_item.get("route") if isinstance(work_item.get("route"), dict) else {}
    return (
        work_item.get("kind") == "precommit_repair"
        or work_item.get("source_stage") == "precommit_failed"
        or route.get("intent") == "precommit_rework"
    )


def _precommit_failure_items(ckpt):
    if not isinstance(ckpt, dict):
        return []
    precommit = (ckpt.get("gate_results") or {}).get("precommit_eval") or {}
    items = []

    def add(value):
        if isinstance(value, dict):
            reason = value.get("reason")
            details = value.get("details")
            if reason or details:
                items.append(": ".join(str(x) for x in (reason, details) if x))
            evidence = value.get("evidence")
            if isinstance(evidence, (list, tuple)):
                for item in evidence[:5]:
                    add(item)
            elif evidence:
                add(evidence)
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                add(item)
        elif value is not None:
            text = str(value).strip()
            if text:
                items.append(text)

    add(precommit.get("directive"))
    add(ckpt.get("reviewer_feedback"))
    add(precommit.get("blockers"))
    add(precommit.get("failures"))

    for matchup in (precommit.get("matchups") or [])[:6]:
        if not isinstance(matchup, dict):
            continue
        opponent = matchup.get("opponent") or matchup.get("bot_b") or matchup.get("label") or "unknown"
        wins = matchup.get("wins", matchup.get("wins_a"))
        losses = matchup.get("losses", matchup.get("wins_b"))
        draws = matchup.get("draws", 0)
        reason = matchup.get("reason")
        net = matchup.get("net_chips")
        if isinstance(net, list):
            net = sum(x for x in net if isinstance(x, (int, float)))
        parts = [f"vs {opponent}"]
        if reason:
            parts.append(f"reason={reason}")
        if wins is not None and losses is not None:
            parts.append(f"result={wins}W-{losses}L-{draws}D")
        if net is not None:
            parts.append(f"net_chips={net}")
        items.append("; ".join(parts))

    deduped = []
    seen = set()
    for item in items:
        if item not in seen:
            seen.add(item)
            deduped.append(item)
    return deduped


def _precommit_changed_python_files(ckpt):
    """Return candidate .py files that actually differ from the source parent."""
    if not isinstance(ckpt, dict):
        return []
    source_v = ckpt.get("source_v")
    next_v = ckpt.get("next_v")
    if source_v is None or next_v is None:
        return []
    try:
        source_dir = get_bot_dir(source_v)
        next_dir = get_bot_dir(next_v)
        changed = _py_files_changed_between(source_dir, next_dir)
    except Exception:
        return []

    preferred_order = {
        "strategy.py": 0,
        "postflop.py": 1,
        "preflop.py": 2,
        "strategy_helpers.py": 3,
        "opponent.py": 4,
        "state.py": 5,
        "national_bot.py": 6,
        "main.py": 7,
    }
    normalized = []
    seen = set()
    for item in changed:
        rel = _target_rel(item, next_v)
        if not rel or "backup" in rel:
            continue
        if rel.endswith(".py") and rel not in seen:
            seen.add(rel)
            normalized.append(rel)
    return sorted(normalized, key=lambda rel: (preferred_order.get(rel, 100), rel))


def _limit_precommit_repair_targets(files):
    try:
        limit = int(os.environ.get("POK_PRECOMMIT_REPAIR_MAX_TARGETS", "3"))
    except ValueError:
        limit = 3
    limit = max(1, limit)
    targets = []
    seen = set()
    for item in files or []:
        rel = Path(str(item)).name
        if rel and rel.endswith(".py") and rel not in seen:
            seen.add(rel)
            targets.append(rel)
        if len(targets) >= limit:
            break
    return targets


def _precommit_repair_target_files(ckpt, feedback):
    failures = _precommit_failure_items(ckpt)
    evidence_files = _extract_quality_failure_files(failures)
    if not evidence_files and feedback:
        evidence_files = _extract_quality_failure_files([feedback])

    changed_files = _precommit_changed_python_files(ckpt)
    if changed_files and evidence_files:
        evidence_set = set(evidence_files)
        intersected = [name for name in changed_files if name in evidence_set]
        if intersected:
            return _limit_precommit_repair_targets(intersected)
    if changed_files:
        return _limit_precommit_repair_targets(changed_files)
    if evidence_files:
        return _limit_precommit_repair_targets(evidence_files)

    preferred = ["strategy.py", "postflop.py", "preflop.py", "strategy_helpers.py", "opponent.py"]
    try:
        next_v = ckpt.get("next_v") if isinstance(ckpt, dict) else None
        bot_dir = get_bot_dir(next_v) if next_v is not None else None
        if bot_dir:
            existing = [name for name in preferred if (bot_dir / name).exists()]
            if existing:
                return _limit_precommit_repair_targets(existing[:1])
    except Exception:
        pass
    return ["strategy.py"]


def _checkpoint_rework_feedback(ckpt):
    if not isinstance(ckpt, dict):
        return ""
    if ckpt.get("reviewer_feedback"):
        return str(ckpt.get("reviewer_feedback") or "")
    stage = ckpt.get("stage")
    gates = ckpt.get("gate_results") or {}
    if _is_precommit_rework_checkpoint(ckpt):
        failed = _precommit_failure_items(ckpt)
        if failed:
            return "Precommit failed:\n- " + "\n- ".join(str(item) for item in failed[:20])
    if stage in {"quality_failed", "repair_planned", "rework_running"}:
        failed = _quality_failure_items(ckpt)
        if failed:
            return "Quality gates failed:\n- " + "\n- ".join(str(item) for item in failed[:20])
    if stage == "precommit_failed":
        precommit = gates.get("precommit_eval") or {}
        blockers = precommit.get("blockers") or precommit.get("failures") or []
        if blockers:
            return "Precommit failed: " + json.dumps(blockers[:10], ensure_ascii=False)
    return ""


def _quality_failure_items(ckpt):
    if not isinstance(ckpt, dict):
        return []
    quality = (ckpt.get("gate_results") or {}).get("quality") or {}
    items = []

    def add(value):
        if isinstance(value, dict):
            for key, val in value.items():
                if str(key).endswith(".py"):
                    items.append(f"{key}: {val}")
                else:
                    items.append(f"{key}={val}")
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                add(item)
        elif value is not None:
            text = str(value).strip()
            if text:
                items.append(text)

    add(quality.get("failed_gates"))
    add(quality.get("failures"))
    for key in (
        "compile_errors",
        "import_errors",
        "protected_contract_errors",
        "national_native_contract_errors",
        "smoke_errors",
        "national_protocol_errors",
        "national_acceptance_errors",
        "declared_scope_errors",
        "critical_failures",
        "position_semantics_errors",
        "reachability_warnings",
    ):
        add(quality.get(key))
    oversized = quality.get("oversized_files")
    if isinstance(oversized, dict):
        for filename, lines in oversized.items():
            add(f"file_size({filename}:{lines}L)")

    deduped = []
    seen = set()
    for item in items:
        if item not in seen:
            seen.add(item)
            deduped.append(item)
    return deduped


def _extract_quality_failure_files(failures):
    files = []
    seen = set()
    for failure in failures or []:
        for match in re.finditer(r"([A-Za-z0-9_./-]+\.py)(?::\d+)?", str(failure)):
            rel = Path(match.group(1)).name
            if rel and rel not in seen:
                seen.add(rel)
                files.append(rel)
    return files


def _flatten_text_items(value):
    items = []

    def add(item):
        if isinstance(item, dict):
            for key, val in item.items():
                add(f"{key}: {val}")
        elif isinstance(item, (list, tuple, set)):
            for sub in item:
                add(sub)
        elif item is not None:
            text = str(item).strip()
            if text:
                items.append(text)

    add(value)
    return items


def _is_declared_scope_failure_text(item):
    text = str(item or "").lower()
    return (
        "declared_scope" in text
        or "declared scope" in text
        or "outside master plan target_files/files_allowed" in text
        or "outside declared target_files/files_allowed" in text
    )


def _is_position_semantics_failure_text(item):
    text = str(item or "").lower()
    return (
        "position_semantics" in text
        or "sb must be dealer_id" in text
        or "bb must be 1 - dealer_id" in text
        or "dealer is sb" in text
        or "bb acts first postflop" in text
        or "postflop oop helper" in text
        or "must key on my_is_bb" in text
        or "must key on my_is_bb/bb" in text
        or "not my_is_sb" in text
    )


def _is_national_native_contract_failure_text(item):
    text = str(item or "").lower()
    return (
        "national_native_contract" in text
        or "native national tcp contract" in text
        or "national_bot.py missing" in text
        or (
            "national_bot.py" in text
            and (
                "sanitizer failure" in text
                or "raw action" in text
                or "direct tcp" in text
                or "botzone integer" in text
            )
        )
    )


def _declared_scope_ledger_files(ckpt, reviewer_feedback=""):
    """Files that should be added to repair_scope_files without spawning workers.

    declared_scope failures can happen after in-place crossover/repair rounds when
    the candidate already legitimately changed a file but the accumulated scope
    ledger did not include it yet. That is an accounting update, not a request to
    make another code edit in that file.
    """
    if not isinstance(ckpt, dict):
        return set()
    quality = (ckpt.get("gate_results") or {}).get("quality") or {}
    if not isinstance(quality, dict) or quality.get("declared_scope_ok") is True:
        return set()

    next_v = ckpt.get("next_v")
    evidence = []
    evidence.extend(_flatten_text_items(quality.get("declared_scope_errors")))
    evidence.extend(
        item for item in _quality_failure_items(ckpt)
        if _is_declared_scope_failure_text(item)
    )
    if reviewer_feedback and _is_declared_scope_failure_text(reviewer_feedback):
        evidence.append(reviewer_feedback)

    files = set()
    for filename in _extract_quality_failure_files(evidence):
        rel = _target_rel(filename, next_v)
        if rel:
            files.add(rel)

    scope_metrics = quality.get("declared_scope") or {}
    if not files and isinstance(scope_metrics, dict):
        changed = {
            rel for rel in (
                _target_rel(item, next_v)
                for item in scope_metrics.get("changed_files", []) or []
            )
            if rel
        }
        allowed = {
            rel for rel in (
                _target_rel(item, next_v)
                for item in scope_metrics.get("allowed_files", []) or []
            )
            if rel
        }
        files.update(changed - allowed)
    return files


def _is_declared_scope_ledger_task(task):
    if not isinstance(task, dict):
        return False
    contract = task.get("repair_contract") if isinstance(task.get("repair_contract"), dict) else {}
    text = " ".join([
        str(task.get("worker_id", "")),
        str(task.get("repair_blocker", "")),
        str(contract.get("blocker", "")),
        str(contract.get("evidence", "")),
        str(task.get("worker_prompt", task.get("instruction", ""))),
    ])
    return _is_declared_scope_failure_text(text)


def _prune_declared_scope_ledger_tasks(tasks, ckpt, reviewer_feedback=""):
    ledger_files = _declared_scope_ledger_files(ckpt, reviewer_feedback)
    if not ledger_files:
        return list(tasks or []), set()
    kept = []
    for task in tasks or []:
        task_files = {
            rel for rel in (
                _target_rel(target, ckpt.get("next_v") if isinstance(ckpt, dict) else None)
                for target in task.get("target_files", []) or []
            )
            if rel
        }
        if task_files and task_files <= ledger_files and _is_declared_scope_ledger_task(task):
            continue
        kept.append(task)
    return kept, ledger_files


def _task_id_suffix(filename):
    return re.sub(r"[^a-z0-9]+", "_", Path(str(filename)).name.lower()).strip("_")


def _line_count_contracts(quality, failures):
    """Return structured file_size blocker contracts from quality gate output."""
    by_file = {}

    def add(filename, current=None, limit=None, evidence=""):
        rel = Path(str(filename)).name
        if not rel:
            return
        existing = by_file.get(rel, {})
        evidences = []
        if existing.get("evidence"):
            evidences.append(str(existing["evidence"]))
        if evidence and evidence not in evidences:
            evidences.append(evidence)
        by_file[rel] = {
            "blocker": "file_size",
            "file": rel,
            "current_lines": current if current is not None else existing.get("current_lines"),
            "line_limit": limit if limit is not None else existing.get("line_limit"),
            "evidence": "; ".join(evidences),
        }

    oversized = quality.get("oversized_files")
    if isinstance(oversized, dict):
        for filename, lines in oversized.items():
            try:
                current = int(lines)
            except (TypeError, ValueError):
                current = None
            add(filename, current=current, evidence=f"oversized_files[{filename}]={lines}")

    text = "\n".join(str(item) for item in failures or [])
    for group in re.finditer(r"file_size\(([^)]*)\)", text):
        body = group.group(1)
        for match in re.finditer(
            r"([A-Za-z0-9_./-]+\.py):(\d+)L(?:/(\d+)L)?",
            body,
        ):
            current = int(match.group(2))
            limit = int(match.group(3)) if match.group(3) else None
            add(match.group(1), current=current, limit=limit, evidence=f"file_size({body})")
    return [by_file[name] for name in sorted(by_file)]


def _position_contracts(quality):
    """Return structured position_semantics contracts grouped by file."""
    source_items = []
    source_items.extend(_flatten_text_items(quality.get("position_semantics_errors")))
    for item in _flatten_text_items(quality.get("failed_gates")):
        if "position_semantics(" in item:
            source_items.append(item)

    by_file = {}
    for item in source_items:
        text = str(item)
        for match in re.finditer(
            r"([A-Za-z0-9_./-]+\.py):(\d+):?\s*([^;\n)]*)",
            text,
        ):
            rel = Path(match.group(1)).name
            if not rel:
                continue
            detail = {
                "line": int(match.group(2)),
                "message": match.group(3).strip() or text.strip(),
                "evidence": text.strip(),
            }
            by_file.setdefault(rel, []).append(detail)

    contracts = []
    for rel, details in by_file.items():
        deduped = []
        seen = set()
        for detail in details:
            key = (detail["line"], detail["message"])
            if key not in seen:
                seen.add(key)
                deduped.append(detail)
        contracts.append({
            "blocker": "position_semantics",
            "file": rel,
            "details": deduped,
            "evidence": "; ".join(d["evidence"] for d in deduped[:4]),
        })
    return sorted(contracts, key=lambda c: c["file"])


def _national_native_contracts(quality, failures):
    """Return file-scoped contracts for direct national TCP entrypoint blockers."""
    source_items = []
    source_items.extend(_flatten_text_items(quality.get("national_native_contract_errors")))
    source_items.extend(
        item for item in _flatten_text_items(quality.get("failed_gates"))
        if _is_national_native_contract_failure_text(item)
    )
    source_items.extend(
        item for item in failures or []
        if _is_national_native_contract_failure_text(item)
    )
    if quality.get("national_native_contract_ok") is False and not source_items:
        source_items.append(
            "national_native_contract failed; national native bots must keep a direct TCP entrypoint"
        )

    deduped = []
    seen = set()
    for item in source_items:
        text = str(item).strip()
        if text and text not in seen:
            seen.add(text)
            deduped.append(text)

    files = _extract_quality_failure_files(deduped)
    if not files and (deduped or quality.get("national_native_contract_ok") is False):
        files = ["national_bot.py"]

    return [
        {
            "blocker": "national_native_contract",
            "file": rel,
            "evidence": "\n".join(deduped[:8]) or "national_native_contract failed",
        }
        for rel in files
    ]


def _generic_quality_contracts(quality, failures, claimed_files):
    """Build file-scoped fallback contracts for non-mechanical quality blockers."""
    evidence_items = []
    for key in (
        "compile_errors",
        "import_errors",
        "protected_contract_errors",
        "national_native_contract_errors",
        "smoke_errors",
        "national_protocol_errors",
        "national_acceptance_errors",
        "critical_failures",
        "reachability_warnings",
    ):
        evidence_items.extend(_flatten_text_items(quality.get(key)))
    evidence_items = [
        item for item in evidence_items
        if not _is_declared_scope_failure_text(item)
        and not _is_national_native_contract_failure_text(item)
    ]
    if not evidence_items:
        evidence_items = [
            item for item in failures
            if not str(item).startswith("file_size(")
            and not _is_position_semantics_failure_text(item)
            and not _is_declared_scope_failure_text(item)
            and not _is_national_native_contract_failure_text(item)
        ]
    evidence_files = _extract_quality_failure_files(evidence_items)
    mechanical_files = {c["file"] for c in _line_count_contracts(quality, failures)}
    mechanical_files.update(c["file"] for c in _position_contracts(quality))
    mechanical_files.update(c["file"] for c in _national_native_contracts(quality, failures))
    generic_files = evidence_files or [f for f in claimed_files if f not in mechanical_files]
    if not generic_files:
        return []

    contracts = []
    for rel in generic_files:
        matching = [item for item in evidence_items if rel in str(item)]
        contracts.append({
            "blocker": "quality_gate",
            "file": rel,
            "evidence": "\n".join(str(item) for item in (matching or evidence_items)[:8]),
        })
    return contracts


def _quality_repair_contracts(ckpt, feedback=""):
    if not isinstance(ckpt, dict):
        return []
    quality = (ckpt.get("gate_results") or {}).get("quality") or {}
    failures = _quality_failure_items(ckpt)
    claimed_files = _extract_quality_failure_files(failures)
    if not claimed_files and feedback:
        claimed_files = _extract_quality_failure_files([feedback])
    ledger_files = _declared_scope_ledger_files(ckpt, feedback)
    if ledger_files:
        claimed_files = [filename for filename in claimed_files if filename not in ledger_files]
    contracts = []
    contracts.extend(_line_count_contracts(quality, failures))
    contracts.extend(_position_contracts(quality))
    contracts.extend(_national_native_contracts(quality, failures))
    contracts.extend(_generic_quality_contracts(quality, failures, claimed_files))

    ordered = []
    seen = set()
    for contract in contracts:
        key = (contract.get("blocker"), contract.get("file"))
        if key in seen:
            continue
        seen.add(key)
        ordered.append(contract)
    return ordered


def _format_position_details(details):
    lines = []
    for detail in details or []:
        line = detail.get("line")
        message = detail.get("message") or detail.get("evidence") or ""
        lines.append(f"- line {line}: {message}" if line else f"- {message}")
    return "\n".join(lines) if lines else "- gate reported a position_semantics violation in this file"


def _quality_contract_task(contract, ckpt, preservation, task_kind):
    next_v = ckpt.get("next_v")
    filename = contract["file"]
    suffix = _task_id_suffix(filename)
    blocker = contract.get("blocker")
    if blocker == "file_size":
        current = contract.get("current_lines")
        limit = contract.get("line_limit")
        overage = None
        required = (
            f"Reduce `{filename}` to <= {limit} lines."
            if limit else f"Reduce `{filename}` enough to clear the file_size gate."
        )
        if current is not None and limit is not None:
            try:
                overage = int(current) - int(limit)
            except (TypeError, ValueError):
                overage = None
            required += f" Current gate reading: {current}L/{limit}L."
        large_overage = ""
        if overage is not None and overage >= 200:
            target_removal = overage + 50
            large_overage = (
                "\nLarge-overage requirement:\n"
                f"- This file is {overage} lines over the gate. Do not spend the attempt "
                "on tiny comment trimming alone.\n"
                f"- Before editing, identify a removal/consolidation plan worth at least "
                f"{target_removal} lines so the final file has margin under the limit.\n"
                "- Remove whole dead/debug/self-test blocks, duplicated historical notes, "
                "and unreferenced helper wrappers first. If comments cannot meet the target, "
                "delete or consolidate unreachable helper code verified by local grep/references.\n"
                "- A script-based rewrite is acceptable when it writes only this assigned file; "
                "run `wc -l` early and again before finishing.\n"
            )
        prompt = (
            f"{preservation.format(next_v=next_v)}\n\n"
            f"Repair contract: file_size\n"
            f"- Target file: `{filename}`\n"
            f"- Evidence: {contract.get('evidence') or 'file_size gate failed'}\n"
            f"- Required outcome: {required}\n\n"
            f"{large_overage}"
            "Required method:\n"
            f"- Edit `{filename}`. This file is listed in `must_change_files`; a no-op or editing only other files is failure.\n"
            "- Prefer deleting duplicated/dead comments, stale historical notes, or redundant helper wrappers before touching active decisions.\n"
            "- Do not remove active strategy branches just to save lines.\n"
            f"- Verify with `wc -l bots/national_v{next_v}/{filename}` before finishing.\n"
            "- End your output with the exact line count you observed."
        )
        return {
            "worker_id": f"auto_quality_repair_file_size_{suffix}",
            "role": "Algorithmic Logic Architect",
            "target_files": [filename],
            "must_change_files": [filename],
            "worker_prompt": prompt,
            "task_kind": task_kind,
            "repair_blocker": "file_size",
            "repair_contract": contract,
        }
    if blocker == "position_semantics":
        prompt = (
            f"{preservation.format(next_v=next_v)}\n\n"
            f"Repair contract: position_semantics\n"
            f"- Target file: `{filename}`\n"
            f"- Flagged locations:\n{_format_position_details(contract.get('details'))}\n\n"
            "Authoritative heads-up contract:\n"
            "- dealer_id is the small blind.\n"
            "- big blind is `1 - dealer_id`.\n"
            "- small blind acts first preflop; big blind acts first postflop.\n\n"
            "- A helper named or documented as postflop/OOP must key on `my_is_bb`/BB, not `my_is_sb`/SB; SB/dealer is in position postflop.\n\n"
            "Required method:\n"
            f"- Edit `{filename}`. This file is listed in `must_change_files`; a no-op or editing only another file is failure.\n"
            "- Replace code patterns exactly when present: `sb = next_player(dealer_id, 1)` -> `sb = dealer_id`; `bb = next_player(dealer_id, 2)` -> `bb = 1 - dealer_id`.\n"
            "- Also fix same-family dealer variables when present: `*_sb = next_player(<dealer_var>, 1)` -> `*_sb = <dealer_var>`; `*_bb = next_player(<dealer_var>, 2)` -> `*_bb = 1 - <dealer_var>`.\n"
            "- If the flagged line is prose/comment/test text, update that text to the authoritative contract above.\n"
            "- Do not change card mapping, action protocol, or unrelated strategy behavior.\n"
            "- Before finishing, grep the file to confirm no sb/bb assignment remains derived from `next_player(...dealer..., 1/2)`."
        )
        return {
            "worker_id": f"auto_quality_repair_position_{suffix}",
            "role": "Algorithmic Logic Architect",
            "target_files": [filename],
            "must_change_files": [filename],
            "worker_prompt": prompt,
            "task_kind": task_kind,
            "repair_blocker": "position_semantics",
            "repair_contract": contract,
        }
    if blocker == "national_native_contract":
        prompt = (
            f"{preservation.format(next_v=next_v)}\n\n"
            "Repair contract: national_native_contract\n"
            f"- Target file: `{filename}`\n"
            f"- Evidence:\n{contract.get('evidence') or 'national_native_contract failed'}\n\n"
            "Authoritative national-native entrypoint contract:\n"
            "- `national_bot.py` is the formal submitted entrypoint for new bots.\n"
            "- It must be a direct TCP client for the national line protocol; do not depend on `sever/bot_adapter.py`.\n"
            "- It must emit only legal line actions such as `raise <amount>`, `call`, `check`, `fold`, or `allin`; never emit Botzone JSON as the formal response.\n"
            "- If state reconstruction or action sanitization fails, choose a bounded legal fallback action instead of passing through a raw Botzone integer action.\n\n"
            "Required method:\n"
            f"- Edit `{filename}`. This file is listed in `must_change_files`; a no-op or editing only another file is failure.\n"
            "- Keep card mapping and national protocol formatting intact.\n"
            "- Fix the exact native contract violation shown above without weakening existing legal-action validation.\n"
            "- Run `python -m py_compile` on the edited entrypoint before finishing."
        )
        return {
            "worker_id": f"auto_quality_repair_national_native_{suffix}",
            "role": "Protocol Integration Architect",
            "target_files": [filename],
            "must_change_files": [filename],
            "worker_prompt": prompt,
            "task_kind": task_kind,
            "repair_blocker": "national_native_contract",
            "repair_contract": contract,
        }
    prompt = (
        f"{preservation.format(next_v=next_v)}\n\n"
        f"Repair contract: quality_gate\n"
        f"- Target file: `{filename}`\n"
        f"- Evidence:\n{contract.get('evidence') or 'quality gate failed'}\n\n"
        "Required method:\n"
        f"- Edit `{filename}`. This file is listed in `must_change_files`; a no-op or editing only another file is failure.\n"
        "- Fix only the listed gate blocker.\n"
        "- Preserve national protocol/card mapping and previously passing behavior.\n"
        "- Run the smallest relevant compile/import check before finishing."
    )
    return {
        "worker_id": f"auto_quality_repair_gate_{suffix}",
        "role": "Algorithmic Logic Architect",
        "target_files": [filename],
        "must_change_files": [filename],
        "worker_prompt": prompt,
        "task_kind": task_kind,
        "repair_blocker": "quality_gate",
        "repair_contract": contract,
    }


def _text_line_count(text):
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def _docstring_line_ranges(text):
    ranges = set()
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return ranges
    node_types = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    for node in ast.walk(tree):
        if not isinstance(node, node_types) or not getattr(node, "body", None):
            continue
        first = node.body[0]
        if not (
            isinstance(first, ast.Expr)
            and isinstance(getattr(first, "value", None), ast.Constant)
            and isinstance(first.value.value, str)
        ):
            continue
        if not isinstance(node, ast.Module) and len(node.body) == 1:
            continue
        end_lineno = getattr(first, "end_lineno", first.lineno)
        ranges.update(range(first.lineno, end_lineno + 1))
    return ranges


def _tokenized_comment_and_string_lines(text):
    comment_lines = set()
    string_lines = set()
    try:
        tokens = tokenize.generate_tokens(io.StringIO(text).readline)
        for tok in tokens:
            if tok.type == tokenize.COMMENT:
                line = tok.line or ""
                if not line[:tok.start[1]].strip():
                    comment_lines.add(tok.start[0])
            elif tok.type == tokenize.STRING:
                string_lines.update(range(tok.start[0], tok.end[0] + 1))
    except (tokenize.TokenError, IndentationError):
        pass
    return comment_lines, string_lines


def _mechanically_trim_python_text(text):
    """Remove non-behavioral Python text and return ``(new_text, stats)``."""
    lines = text.splitlines(keepends=True)
    before = len(lines)
    if not lines:
        return text, {"before": 0, "after": 0, "removed": 0}

    docstring_lines = _docstring_line_ranges(text)
    comment_lines, string_lines = _tokenized_comment_and_string_lines(text)
    protected_string_lines = string_lines - docstring_lines
    remove_lines = set(docstring_lines)
    remove_lines.update(comment_lines - protected_string_lines)
    for idx, line in enumerate(lines, start=1):
        if idx not in protected_string_lines and not line.strip():
            remove_lines.add(idx)

    trimmed_lines = [
        line for idx, line in enumerate(lines, start=1)
        if idx not in remove_lines
    ]
    new_text = "".join(trimmed_lines)
    if new_text and not new_text.endswith("\n"):
        new_text += "\n"
    after = _text_line_count(new_text)
    return new_text, {
        "before": before,
        "after": after,
        "removed": before - after,
        "docstring_lines": len(docstring_lines),
        "comment_lines": len(comment_lines),
        "blank_lines": sum(
            1 for idx, line in enumerate(lines, start=1)
            if idx in remove_lines and not line.strip()
        ),
    }


def _mechanical_trim_python_file(path, limit):
    path = Path(path)
    try:
        old_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {"changed": False, "error": str(exc), "file": str(path)}
    before = _text_line_count(old_text)
    if limit is not None and before <= int(limit):
        return {"changed": False, "file": str(path), "before": before, "after": before, "limit": limit}

    new_text, stats = _mechanically_trim_python_text(old_text)
    after = _text_line_count(new_text)
    if after >= before:
        return {"changed": False, "file": str(path), "before": before, "after": after, "limit": limit}

    try:
        path.write_text(new_text, encoding="utf-8")
        py_compile.compile(str(path), doraise=True)
    except Exception as exc:
        try:
            path.write_text(old_text, encoding="utf-8")
        except OSError:
            pass
        return {
            "changed": False,
            "rolled_back": True,
            "error": str(exc),
            "file": str(path),
            "before": before,
            "after": before,
            "attempted_after": after,
            "limit": limit,
        }
    return {"changed": True, "file": str(path), "limit": limit, **stats}


def _apply_mechanical_file_size_trims(tasks, next_dir, source_dir, next_v, source_v):
    """Apply behavior-preserving text trims before expensive file_size workers."""
    try:
        _total, oversized = check_code_size(next_dir, source_dir=source_dir)
    except Exception as exc:
        log_system_event(
            "pipeline.file_size_mechanical_trim_check_failed",
            "warn",
            f"Could not compute file_size mechanical trim inputs for v{next_v}: {exc}",
            {"next_v": next_v, "source_v": source_v},
        )
        return []
    oversized_by_name = {Path(name).name: (lines, limit) for name, lines, limit in oversized}
    results = []
    for task in tasks or []:
        if not _is_file_size_repair_task(task):
            continue
        for target in task.get("target_files", []) or []:
            rel = _target_rel(target, next_v)
            if not rel:
                continue
            filename = Path(rel).name
            current = oversized_by_name.get(filename)
            if not current:
                continue
            lines, limit = current
            if int(lines) - int(limit) < 200:
                continue
            path = next_dir / rel
            result = _mechanical_trim_python_file(path, limit)
            result.update({
                "next_v": next_v,
                "source_v": source_v,
                "target": rel,
                "initial_lines": lines,
            })
            results.append(result)
            if result.get("changed"):
                log_system_event(
                    "pipeline.file_size_mechanical_trim_applied",
                    "warn",
                    (
                        f"Applied mechanical file_size trim to v{next_v}/{rel}: "
                        f"{result.get('before')}L -> {result.get('after')}L "
                        f"(limit {limit})"
                    ),
                    result,
                )
            elif result.get("error"):
                log_system_event(
                    "pipeline.file_size_mechanical_trim_failed",
                    "warn",
                    f"Mechanical file_size trim failed for v{next_v}/{rel}: {result.get('error')}",
                    result,
                )
    return results


def _precommit_repair_task(filename, ckpt, feedback):
    next_v = ckpt.get("next_v")
    source_v = ckpt.get("source_v")
    suffix = _task_id_suffix(filename)
    line_note = ""
    try:
        path = get_bot_dir(next_v) / filename
        if path.exists():
            line_count = sum(1 for _ in path.open("r", encoding="utf-8", errors="ignore"))
            if line_count >= 2300:
                line_note = (
                    f"\n- `{filename}` is near the hard size cap ({line_count} lines). "
                    "Prefer deleting or tightening an existing risky branch over adding a new subsystem."
                )
    except Exception:
        line_note = ""

    prompt = (
        "This is one file-scoped precommit regression repair from a failed native "
        f"national TCP final gate for bots/national_v{next_v}.\n\n"
        f"Target file: `{filename}`\n"
        f"Source parent for diff: bots/national_v{source_v}/\n"
        f"Failed candidate: bots/national_v{next_v}/\n\n"
        f"Exact precommit feedback:\n{feedback}\n\n"
        "Required method:\n"
        f"- Only edit `{filename}`. Other files are intentionally out of scope for this worker.\n"
        f"- First inspect `diff bots/national_v{source_v}/{filename} bots/national_v{next_v}/{filename}` "
        "and identify which changed behavior could explain the losing 70-hand national TCP matchups.\n"
        "- Make one bounded EV/matchup correction in this file. Prefer tightening, gating, or partially "
        "rolling back a risky new branch over adding broad new logic.\n"
        "- Do not wholesale replace the candidate with the source parent; the final candidate must remain "
        "a real code change after repair.\n"
        "- Preserve native TCP protocol/card mapping, national action legality, and previously passed quality gates.\n"
        "- Run a compile check for the bot package before finishing."
        f"{line_note}"
    )
    return {
        "worker_id": f"auto_precommit_repair_{suffix}",
        "role": "Strategic Regression Repair Architect",
        "target_files": [filename],
        "must_change_files": [filename],
        "worker_prompt": prompt,
        "task_kind": "precommit_repair",
        "repair_blocker": "precommit_regression",
        "repair_contract": {
            "blocker": "precommit_regression",
            "file": filename,
            "evidence": feedback[:2000],
        },
    }


def _precommit_repair_tasks(ckpt, feedback):
    return [
        _precommit_repair_task(filename, ckpt, feedback)
        for filename in _precommit_repair_target_files(ckpt, feedback)
    ]


def _precommit_repair_task_refresh_reason(tasks, ckpt, feedback=""):
    if not _is_precommit_rework_checkpoint(ckpt):
        return ""
    if not tasks:
        return "missing precommit repair task(s)"

    expected = set(_precommit_repair_target_files(ckpt, feedback))
    task_targets = []
    for task in tasks or []:
        if not isinstance(task, dict):
            return "invalid precommit repair task"
        task_kind = str(task.get("task_kind") or "").lower()
        task_text = " ".join([
            str(task.get("worker_id", "")),
            str(task.get("role", "")),
            str(task.get("worker_prompt", task.get("instruction", "")))[:500],
        ]).lower()
        if "precommit_repair" not in task_kind and "precommit" not in task_text:
            return "checkpoint task is not a precommit repair"
        targets = [
            rel for rel in (
                _target_rel(target, ckpt.get("next_v"))
                for target in task.get("target_files", []) or []
            )
            if rel
        ]
        must_change = [
            rel for rel in (
                _target_rel(target, ckpt.get("next_v"))
                for target in task.get("must_change_files", []) or []
            )
            if rel
        ]
        if len(targets) != 1:
            return "precommit repair task is not file-scoped"
        if must_change and must_change != targets:
            return "precommit repair must_change_files do not match its single target"
        task_targets.extend(targets)

    task_set = set(task_targets)
    if expected and task_set != expected:
        return "precommit repair targets are stale"
    if len(task_targets) != len(task_set):
        return "duplicate precommit repair targets"
    return ""


def _synthesize_rework_tasks_from_checkpoint(ckpt, reviewer_feedback=""):
    """Build bounded repair tasks when a checkpoint has gate feedback but no plan.

    Crossover output checkpoints intentionally store a synthetic plan with no
    worker tasks because the code was produced by run_crossover. If quality gates
    fail at that point, the next action is still a repair worker pass; leaving task
    creation to the Orchestrator LLM made recovery nondeterministic.
    """
    if not isinstance(ckpt, dict):
        return []
    stage = ckpt.get("stage")
    if stage not in {"quality_failed", "repair_planned", "rework_running", "precommit_failed"}:
        return []

    feedback = str(reviewer_feedback or _checkpoint_rework_feedback(ckpt) or "").strip()
    if not feedback:
        return []

    master_plan = _checkpoint_master_plan(ckpt)
    is_precommit_rework = _is_precommit_rework_checkpoint(ckpt)
    quality_contracts = [] if is_precommit_rework else _quality_repair_contracts(ckpt, feedback)
    if is_precommit_rework:
        return _precommit_repair_tasks(ckpt, feedback)
    elif quality_contracts:
        target_files = [contract["file"] for contract in quality_contracts]
    else:
        failures = _quality_failure_items(ckpt)
        target_files = _extract_quality_failure_files(failures)
        if not target_files:
            target_files = _extract_quality_failure_files([feedback])
    if not target_files:
        return []

    targets = target_files
    is_crossover = bool(ckpt.get("parent2_v")) or master_plan.get("strategy") == "crossover"
    if is_crossover and stage in {"quality_failed", "repair_planned", "rework_running"}:
        preservation = (
            "This is a crossover quality repair. Preserve the current candidate's "
            "crossover behavior in bots/national_v{next_v}; fix only the blocking "
            "quality-gate issues unless a tiny local cleanup is required."
        )
        method = (
            "- Read the listed target files before editing.\n"
            "- For file_size blockers, remove dead/duplicated code or consolidate helper logic; do not weaken strategy by deleting active decisions blindly.\n"
            "- For position_semantics blockers, follow the national heads-up position contract exactly: small blind is dealer_id, big blind is 1 - dealer_id.\n"
            "- Do not change protocol/card mapping behavior outside the named blockers.\n"
            "- Leave stderr telemetry honest if touched."
        )
        worker_id = "auto_quality_repair"
        role = "Algorithmic Logic Architect"
        task_kind = "quality_repair"
    else:
        preservation = (
            "This is a gate repair. Make the smallest structural correction that "
            "clears the listed blockers while preserving the intended strategy."
        )
        method = (
            "- Read the listed target files before editing.\n"
            "- For file_size blockers, remove dead/duplicated code or consolidate helper logic; do not weaken strategy by deleting active decisions blindly.\n"
            "- For position_semantics blockers, follow the national heads-up position contract exactly: small blind is dealer_id, big blind is 1 - dealer_id.\n"
            "- Do not change protocol/card mapping behavior outside the named blockers.\n"
            "- Leave stderr telemetry honest if touched."
        )
        worker_id = "auto_quality_repair"
        role = "Algorithmic Logic Architect"
        task_kind = "quality_repair"

    if quality_contracts:
        return _order_quality_repair_tasks([
            _quality_contract_task(contract, ckpt, preservation, task_kind)
            for contract in quality_contracts
        ])

    prompt = (
        f"{preservation.format(next_v=ckpt.get('next_v'))}\n\n"
        f"Exact gate feedback:\n{feedback}\n\n"
        f"Required method:\n{method}"
    )
    return [{
        "worker_id": worker_id,
        "role": role,
        "target_files": targets,
        "must_change_files": targets,
        "worker_prompt": prompt,
        "task_kind": task_kind,
    }]


def _should_reset_before_rework(ckpt, tasks):
    """Return False for in-place repairs that must preserve the current candidate."""
    if not isinstance(ckpt, dict):
        return True
    if _is_precommit_rework_checkpoint(ckpt):
        return False
    stage = ckpt.get("stage")
    if stage not in {"quality_failed", "repair_planned", "rework_running"}:
        return True
    master_plan = ckpt.get("master_plan") if isinstance(ckpt.get("master_plan"), dict) else {}
    work_item = master_plan.get("work_item") if isinstance(master_plan.get("work_item"), dict) else {}
    work_kind = str(work_item.get("kind") or "")
    task_kinds = {
        str(task.get("task_kind") or "")
        for task in tasks or []
        if isinstance(task, dict)
    }
    is_quality_repair = (
        stage == "quality_failed"
        or "quality_repair" in work_kind
        or work_kind == "crossover_gate_rework"
        or any("quality_repair" in kind for kind in task_kinds)
    )
    if is_quality_repair and "precommit" not in work_kind:
        return False
    is_crossover = (
        bool(ckpt.get("parent2_v"))
        or master_plan.get("strategy") == "crossover"
        or work_kind.startswith("crossover_")
    )
    if not is_crossover:
        return True
    return True


@tool("execute_workers", "Execute worker tasks to modify bot code. Each task has worker_id, role, target_files, worker_prompt.", {"tasks": list, "next_v": int, "source_v": int, "reviewer_feedback": str})
async def execute_workers(args):
    _t0 = time.time()
    tasks = args.get("tasks", [])
    tasks_provided = bool(tasks)
    next_v = args.get("next_v")
    source_v = args.get("source_v")
    if next_v is None or source_v is None:
        next_v, source_v = _resolve_version_args(args)
    if next_v is None or source_v is None:
        return _json_tool_result({"error": "Missing next_v/source_v and no active checkpoint"})
    reviewer_feedback = args.get("reviewer_feedback", "")

    _set_pipeline_status(f"Executing workers for v{next_v}")

    next_dir = get_bot_dir(next_v)
    prompts_dir = PROJECT_ROOT / "web" / "core" / "prompts"
    worker_template = (prompts_dir / "worker_prompt.md").read_text()

    ckpt = _matching_checkpoint(next_v, source_v)
    if not ckpt:
        return _state_blocked(
            "execute_workers requires a matching checkpoint from prepare_next_gen.",
            next_v,
            source_v,
        )
    rework_stages = {"quality_failed", "precommit_failed", "repair_planned", "rework_running"}
    if not ckpt.get("master_plan") and ckpt.get("stage") not in rework_stages:
        return _json_tool_result({
            "error": "execute_workers requires a master plan. Call run_master first to produce a task plan.",
            "next_v": next_v,
            "source_v": source_v,
        })

    if not reviewer_feedback and ckpt.get("stage") in rework_stages:
        reviewer_feedback = _checkpoint_rework_feedback(ckpt)

    replace_checkpoint_tasks = False

    def _finish_declared_scope_ledger_only(ledger_files):
        base_kind = "quality_repair" if ckpt.get("stage") == "quality_failed" else "gate_rework"
        if ckpt.get("stage") == "precommit_failed":
            base_kind = "precommit_repair"
        elif ckpt.get("parent2_v") is not None:
            base_kind = f"crossover_{base_kind}"
        plan = _checkpoint_plan_with_tasks(ckpt, [], replace_existing_tasks=True)
        plan = {
            **plan,
            "work_item": {
                "kind": base_kind,
                "source_stage": ckpt.get("stage"),
                "reset_performed": False,
                "route": route_policy(ckpt),
                "scope_ledger_only": True,
            },
        }
        plan = _plan_with_accumulated_repair_scope(ckpt, plan, [], next_v)
        write_pipeline_checkpoint(
            next_v,
            source_v,
            "workers_done",
            master_plan=plan,
            reviewer_feedback=reviewer_feedback,
            worker_failure_count=ckpt.get("worker_failure_count", 0),
        )
        log_system_event(
            "pipeline.declared_scope_ledger_repaired",
            "warn",
            f"Updated declared-scope ledger for v{next_v}; no bot code worker needed",
            {
                "next_v": next_v,
                "source_v": source_v,
                "files": sorted(ledger_files),
                "stage": ckpt.get("stage"),
            },
        )
        return _json_tool_result({
            "success": True,
            "scope_ledger_only": True,
            "repair_scope_files": sorted(ledger_files),
            "logs": _get_ui().get_output() if _get_ui() else "",
            "costs": getattr(_get_ui(), "costs", {}) if _get_ui() else {},
            "audit_focus_areas": [],
        })

    # Fallback: if tasks not provided, load from checkpoint master_plan.
    # This happens when the orchestrator session is fresh (not resumed) and
    # the LLM doesn't have the task list in its conversation history.
    if not tasks:
        plan = _checkpoint_master_plan(ckpt)
        checkpoint_tasks = plan.get("tasks", [])
        precommit_stale_reason = (
            _precommit_repair_task_refresh_reason(checkpoint_tasks, ckpt, reviewer_feedback)
            if checkpoint_tasks and _is_precommit_rework_checkpoint(ckpt) else ""
        )
        quality_stale_reason = (
            _stale_quality_task_reason(checkpoint_tasks, ckpt, reviewer_feedback)
            if checkpoint_tasks and not _is_precommit_rework_checkpoint(ckpt) else ""
        )
        if ckpt.get("stage") in rework_stages and (
            not checkpoint_tasks
            or quality_stale_reason
            or precommit_stale_reason
        ):
            tasks = _synthesize_rework_tasks_from_checkpoint(ckpt, reviewer_feedback)
            if tasks:
                replace_checkpoint_tasks = bool(checkpoint_tasks)
                event_type = (
                    "pipeline.workers_tasks_refreshed"
                    if checkpoint_tasks else "pipeline.workers_tasks_synthesized"
                )
                if checkpoint_tasks and _is_precommit_rework_checkpoint(ckpt):
                    event_message = (
                        f"Refreshed precommit repair task(s) for v{next_v}: {precommit_stale_reason}"
                    )
                elif quality_stale_reason:
                    event_message = (
                        f"Refreshed quality repair task(s) for v{next_v}: {quality_stale_reason}"
                    )
                else:
                    event_message = (
                        f"Synthesized {len(tasks)} rework task(s) for v{next_v} from checkpoint gate feedback"
                    )
                log_system_event(
                    event_type,
                    "warn",
                    event_message,
                    {
                        "next_v": next_v,
                        "source_v": source_v,
                        "stage": ckpt.get("stage"),
                        "parent2_v": ckpt.get("parent2_v"),
                        "old_target_files": sorted(_task_target_filenames(checkpoint_tasks)),
                        "new_target_files": sorted(_task_target_filenames(tasks)),
                        "refresh_reason": precommit_stale_reason or quality_stale_reason,
                        "num_tasks": len(tasks),
                        "task_kind": tasks[0].get("task_kind") if tasks else None,
                    },
                )
        elif checkpoint_tasks:
            tasks = checkpoint_tasks
            log_system_event("pipeline.workers_tasks_from_checkpoint", "info",
                             f"Tasks loaded from checkpoint for v{next_v} (LLM omitted tasks arg)",
                             {"next_v": next_v, "num_tasks": len(tasks)})
        else:
            ledger_files = _declared_scope_ledger_files(ckpt, reviewer_feedback)
            if ledger_files and ckpt.get("stage") in rework_stages:
                return _finish_declared_scope_ledger_only(ledger_files)
            return _json_tool_result({
                "error": "No tasks provided and checkpoint has no task plan. Call run_master first.",
                "next_v": next_v,
                "source_v": source_v,
                })
        if not tasks:
            ledger_files = _declared_scope_ledger_files(ckpt, reviewer_feedback)
            if ledger_files and ckpt.get("stage") in rework_stages:
                return _finish_declared_scope_ledger_only(ledger_files)
            return _json_tool_result({
                "error": "No tasks provided and checkpoint has no task plan. Call run_master first.",
                "next_v": next_v,
                "source_v": source_v,
                "stage": ckpt.get("stage"),
            })

    if tasks and ckpt.get("stage") in rework_stages:
        pruned_tasks, ledger_files = _prune_declared_scope_ledger_tasks(
            tasks,
            ckpt,
            reviewer_feedback,
        )
        if len(pruned_tasks) != len(tasks):
            old_files = sorted(_task_target_filenames(tasks))
            tasks = pruned_tasks
            replace_checkpoint_tasks = True
            log_system_event(
                "pipeline.declared_scope_ledger_tasks_pruned",
                "warn",
                f"Pruned declared-scope ledger-only repair task(s) for v{next_v}",
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "old_target_files": old_files,
                    "new_target_files": sorted(_task_target_filenames(tasks)),
                    "ledger_files": sorted(ledger_files),
                    "num_tasks": len(tasks),
                },
            )
            if not tasks:
                return _finish_declared_scope_ledger_only(ledger_files)

    if (
        tasks
        and ckpt.get("stage") in {"quality_failed", "repair_planned", "rework_running"}
        and not _is_precommit_rework_checkpoint(ckpt)
    ):
        failure_files = _quality_failure_target_files(ckpt, reviewer_feedback)
        task_files = _task_target_filenames(tasks)
        missing_files = sorted(failure_files - task_files)
        quality_stale_reason = _stale_quality_task_reason(tasks, ckpt, reviewer_feedback)
        if missing_files or quality_stale_reason:
            refreshed_tasks = _synthesize_rework_tasks_from_checkpoint(ckpt, reviewer_feedback)
            if refreshed_tasks:
                tasks = refreshed_tasks
                replace_checkpoint_tasks = True
                refresh_reason = (
                    f"old task targets missed {missing_files}" if missing_files else quality_stale_reason
                )
                log_system_event(
                    "pipeline.workers_tasks_refreshed",
                    "warn",
                    f"Refreshed quality repair task(s) for v{next_v}; {refresh_reason}",
                    {
                        "next_v": next_v,
                        "source_v": source_v,
                        "missing_files": missing_files,
                        "refresh_reason": quality_stale_reason,
                        "old_target_files": sorted(task_files),
                        "new_target_files": sorted(_task_target_filenames(refreshed_tasks)),
                        "num_tasks": len(refreshed_tasks),
                    },
                )

    if tasks and _is_precommit_rework_checkpoint(ckpt):
        precommit_stale_reason = _precommit_repair_task_refresh_reason(tasks, ckpt, reviewer_feedback)
    else:
        precommit_stale_reason = ""
    if tasks and _is_precommit_rework_checkpoint(ckpt) and precommit_stale_reason:
        refreshed_tasks = _synthesize_rework_tasks_from_checkpoint(ckpt, reviewer_feedback)
        if refreshed_tasks:
            old_files = sorted(_task_target_filenames(tasks))
            tasks = refreshed_tasks
            replace_checkpoint_tasks = True
            log_system_event(
                "pipeline.workers_tasks_refreshed",
                "warn",
                f"Refreshed precommit repair task(s) for v{next_v}; {precommit_stale_reason}",
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "old_target_files": old_files,
                    "new_target_files": sorted(_task_target_filenames(refreshed_tasks)),
                    "num_tasks": len(refreshed_tasks),
                    "task_kind": refreshed_tasks[0].get("task_kind") if refreshed_tasks else None,
                    "refresh_reason": precommit_stale_reason,
                },
            )

    if tasks and ckpt.get("stage") in rework_stages and not _is_precommit_rework_checkpoint(ckpt):
        ordered_tasks = _order_quality_repair_tasks(tasks)
        old_order = [str(task.get("worker_id", idx + 1)) for idx, task in enumerate(tasks)]
        new_order = [str(task.get("worker_id", idx + 1)) for idx, task in enumerate(ordered_tasks)]
        if new_order != old_order:
            tasks = ordered_tasks
            replace_checkpoint_tasks = True
            log_system_event(
                "pipeline.quality_repair_tasks_reordered",
                "info",
                f"Reordered quality repair tasks for v{next_v}; file_size cleanup will run last",
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "old_order": old_order,
                    "new_order": new_order,
                },
            )

    # B6 (2026-06-30): redundant-call guard. execute_workers is NOT idempotent —
    # a redundant call (no reviewer_feedback) when workers already ran resets code
    # from source + re-runs every Worker-LLM (the single most expensive pipeline
    # step), wasting cost and mutating already-gated code. Only allow a re-run when
    # there is reviewer_feedback (a legitimate retry-after-reviewer-reject). A pure
    # redundant call must be refused so the orchestrator proceeds to the next gate.
    _b6_stage = ckpt.get("stage")
    if (not reviewer_feedback
            and _b6_stage in ("workers_done", "quality_failed", "quality_passed", "reviewed", "critic_checked", "precommit_failed", "verified")):
        if _b6_stage == "precommit_failed":
            return _json_tool_result({
                "error": (
                    "Precommit failed, but execute_workers was called without reviewer_feedback. "
                    "Pass the exact precommit_eval directive/blockers as reviewer_feedback."
                ),
                "next_v": next_v,
                "source_v": source_v,
                "stage": _b6_stage,
                "intent": {
                    "kind": "rework",
                    "next_tool": "execute_workers",
                    "failure_class": "regression",
                    "authority": "tool:execute_workers",
                    "safe_to_auto_execute": False,
                },
            })
        try:
            log_system_event(
                "pipeline.workers_redundant_call_blocked", "warn",
                f"execute_workers called again for v{next_v} at stage={_b6_stage} with no "
                f"reviewer_feedback — refusing re-run (would reset code + waste Worker-LLM "
                f"cost). Proceed to the next gate instead.",
                {"next_v": next_v, "source_v": source_v, "stage": _b6_stage},
            )
        except Exception:
            pass
        return _json_tool_result({
            "info": (f"Workers already ran for v{next_v} (stage={_b6_stage}). The code is in place. "
                     f"Do NOT call execute_workers again — proceed to the next pipeline gate "
                     f"(run_quality_gates / run_review / run_critic / run_precommit_eval / commit_bot)."),
            "next_v": next_v,
            "source_v": source_v,
            "stage": _b6_stage,
            "redundant_call_blocked": True,
        })

    # Circuit breaker: limit total worker failures per generation
    # Backward compat: old checkpoints used worker_invocation_count instead of worker_failure_count
    failure_count = ckpt.get("worker_failure_count", ckpt.get("worker_invocation_count", 0))
    MAX_WORKER_FAILURES = 6
    if failure_count >= MAX_WORKER_FAILURES:
        try:
            log_system_event('pipeline.circuit_breaker', 'error',
                f'Circuit breaker: {failure_count} worker failures',
                {'next_v': next_v, 'source_v': source_v, 'failure_count': failure_count})
        except Exception:
            pass
        return _json_tool_result({
            "error": f"CIRCUIT BREAKER: {failure_count} worker failures already recorded this generation (max {MAX_WORKER_FAILURES}). Abandon this generation and start a new one.",
            "failure_count": failure_count,
            "next_v": next_v,
            "source_v": source_v,
        })

    # H6 (2026-06-29): CROSS-GENERATION circuit breaker. The single-gen breaker
    # above limits failures within one generation; this one catches a different
    # failure mode — workers failing across >= N DISTINCT recent generations
    # (v214-from-v212 retried workers on the exhausted commitment/defense/gate
    # axis across gens 213/214 without converging). When this trips, direct
    # master to pivot axis/parent rather than spawning more identical workers.
    # Only counts category="worker" exec failures (not reviewer/critic gates).
    # Uses a dynamic path from evolution_infra.RESULTS_DIR so tests that
    # monkeypatch RESULTS_DIR also isolate this check (the module-level
    # WORKER_FAILURES_FILE in agent_workers would otherwise read the real file).
    #
    # NOTE (P1 root-cause analysis, 2026-06-29): this breaker only guards the
    # execute_workers MCP tool. The v218 logs showed that after the breaker
    # tripped and execute_workers returned an error, the orchestrator LLM used
    # its BUILT-IN Bash/Edit tools to write bot code directly into active bot dirs,
    # completely bypassing execute_workers (and thus this breaker, boundary
    # validation, CoT audit, etc.). The breaker is therefore necessary but NOT
    # sufficient. The real fix is a PreToolUse hook that blocks the orchestrator's
    # Bash/Edit/Write from touching active bot dirs — see _make_bot_dir_guard_hook
    # in orchestrator_context.py. The breaker here stays as defense-in-depth.
    _H6_CROSS_GEN_THRESHOLD = 2
    try:
        from evolution_infra import RESULTS_DIR as _h6_results
        _h6_wf_file = _h6_results / "worker_failures.jsonl"
        _recent = []
        if _h6_wf_file.exists():
            try:
                from evolution_infra import locked_file
                with locked_file(_h6_wf_file, "r", encoding="utf-8") as f:
                    for _line in f:
                        _line = _line.strip()
                        if _line:
                            try:
                                _recent.append(json.loads(_line))
                            except (json.JSONDecodeError, TypeError):
                                pass
            except Exception:
                pass
        _recent = _recent[-(_H6_CROSS_GEN_THRESHOLD * 4):]
        _worker_fails = [r for r in _recent if r.get("category", "worker") == "worker"]
        _distinct_gens_failed = sorted({r.get("gen") for r in _worker_fails
                                        if isinstance(r.get("gen"), int)}, reverse=True)
        if len(_distinct_gens_failed) >= _H6_CROSS_GEN_THRESHOLD:
            try:
                log_system_event(
                    "pipeline.worker_circuit_breaker_cross_gen", "error",
                    f"Cross-gen worker circuit breaker tripped for v{next_v}: workers "
                    f"failed across {len(_distinct_gens_failed)} distinct gens "
                    f"{_distinct_gens_failed[:3]}. Directing master to pivot axis/parent.",
                    {"next_v": next_v, "source_v": source_v,
                     "failed_gens": _distinct_gens_failed[:5]},
                )
            except Exception:
                pass
            return {"content": [{"type": "text", "text": json.dumps({
                "error": "WORKER_CIRCUIT_BREAKER_CROSS_GEN",
                "failed_gens": _distinct_gens_failed[:5],
                "directive": (
                    f"Workers failed across {len(_distinct_gens_failed)} distinct recent "
                    f"generations ({_distinct_gens_failed[:3]}). The current strategic "
                    f"axis (source v{source_v}) is unlikely to converge via more worker "
                    f"retries. Produce a FUNDAMENTALLY different plan: new structural "
                    f"mechanism, different strategic axis, or pivot to a different "
                    f"parent via run_crossover. Do NOT repeat the same worker tasks, "
                    f"and do NOT edit bot files directly with Bash/Edit."
                ),
                "logs": _get_ui().get_output() if _get_ui() else "",
            })}]}
    except Exception as _ce:
        _log.debug("H6 cross-gen circuit breaker check failed (non-fatal): %s", _ce)

    # [fix-13a] require_new_plan guard removed: generation_attempt is never incremented
    # (all assignments are identity-preserves or run_master resets to 0).
    # design intent: critic is advisory-only, precommit is final gate.

    # When retrying after workers already ran, actually reset code from source first.
    # Previous claim that code was reset was FALSE — now we actually do it.
    force_sequential_rework = False
    task_skipper = None
    rework_plan_metadata = None
    precommit_rework_count_for_write = None
    mechanical_trim_results = []
    if reviewer_feedback and ckpt.get("stage") in (
        "workers_done", "quality_failed", "quality_passed", "reviewed", "critic_checked",
        "precommit_failed", "repair_planned", "rework_running"
    ):
        rework_kind = "quality_repair" if ckpt.get("stage") == "quality_failed" else "gate_rework"
        if ckpt.get("stage") == "precommit_failed":
            rework_kind = "precommit_repair"
        elif ckpt.get("parent2_v") is not None:
            rework_kind = f"crossover_{rework_kind}"
        existing_work_item = (
            (ckpt.get("master_plan") or {}).get("work_item")
            if isinstance(ckpt.get("master_plan"), dict) else None
        )
        if (
            ckpt.get("stage") in {"repair_planned", "rework_running"}
            and isinstance(existing_work_item, dict)
            and existing_work_item.get("kind")
        ):
            rework_kind = str(existing_work_item.get("kind"))
        is_precommit_rework = rework_kind == "precommit_repair" or _is_precommit_rework_checkpoint(ckpt)
        if is_precommit_rework:
            prior_rework_count = int(ckpt.get("precommit_rework_count") or 0)
            precommit_rework_count_for_write = prior_rework_count + 1
            if precommit_rework_count_for_write > MAX_PRECOMMIT_REWORK_ROUNDS:
                message = (
                    f"PRECOMMIT_REWORK_CIRCUIT_BREAKER: v{next_v} already used "
                    f"{prior_rework_count} precommit repair round(s) (max {MAX_PRECOMMIT_REWORK_ROUNDS}). "
                    "Abandon this generation and start a fresh direction."
                )
                log_system_event(
                    "pipeline.precommit_rework_circuit_breaker",
                    "error",
                    message,
                    {
                        "next_v": next_v,
                        "source_v": source_v,
                        "stage": ckpt.get("stage"),
                        "precommit_rework_count": prior_rework_count,
                        "max_rework_rounds": MAX_PRECOMMIT_REWORK_ROUNDS,
                        "task_targets": sorted(_task_target_filenames(tasks)),
                    },
                )
                return _json_tool_result({
                    "error": "PRECOMMIT_REWORK_CIRCUIT_BREAKER",
                    "message": message,
                    "next_v": next_v,
                    "source_v": source_v,
                    "precommit_rework_count": prior_rework_count,
                    "max_rework_rounds": MAX_PRECOMMIT_REWORK_ROUNDS,
                    "directive": "Abandon this generation; repeated precommit repair did not converge.",
                })
        source_dir_r = get_bot_dir(source_v)
        reset_before_rework = _should_reset_before_rework(ckpt, tasks)
        if reset_before_rework and source_dir_r.exists() and next_dir.exists():
            _log.info(f"Resetting v{next_v} code from source v{source_v} before worker retry (incremental, preserves NEW files)")
            # Incremental reset: overwrite source files (undo worker edits) but
            # PRESERVE worker-created NEW files absent from source. This avoids
            # wiping NEW files on redundant orchestrator re-calls of execute_workers
            # (which would otherwise cause zero-changes wasted retries).
            preserved = _incremental_reset_next_dir(next_dir, source_dir_r)
            if preserved:
                _log.info("Preserved %d worker-created NEW file(s) across reset: %s",
                          len(preserved), preserved)
        elif not reset_before_rework:
            if rework_kind == "precommit_repair" or _is_precommit_rework_checkpoint(ckpt):
                log_system_event(
                    "pipeline.precommit_repair_in_place",
                    "warn",
                    f"Repairing v{next_v} in place after precommit failure; preserving candidate code",
                    {"next_v": next_v, "source_v": source_v, "parent2_v": ckpt.get("parent2_v")},
                )
            else:
                in_place_kind = (
                    "crossover_quality_repair"
                    if rework_kind.startswith("crossover_") or ckpt.get("parent2_v") is not None
                    else "quality_repair"
                )
                event_type = (
                    "pipeline.crossover_quality_repair_in_place"
                    if in_place_kind == "crossover_quality_repair"
                    else "pipeline.quality_repair_in_place"
                )
                event_message = (
                    f"Repairing crossover v{next_v} in place after quality failure; preserving fused candidate code"
                    if in_place_kind == "crossover_quality_repair"
                    else f"Repairing v{next_v} in place after quality failure; preserving generated candidate code"
                )
                log_system_event(
                    event_type,
                    "warn",
                    event_message,
                    {"next_v": next_v, "source_v": source_v, "parent2_v": ckpt.get("parent2_v")},
                )

        # Re-apply known fixes after resetting from source (source may be older/unfixed)
        from fix_injection import apply_known_fixes, log_fix_application
        applied, skipped = apply_known_fixes(next_dir)
        if applied or skipped:
            log_fix_application(applied, skipped, next_dir, source_v)
        try:
            from candidate_hygiene import sanitize_candidate_dir
            from workflow_profiles import get_workflow_profile
            native_tcp = getattr(get_workflow_profile(), "national_execution_mode", "adapter") == "native_tcp"
            sanitize_candidate_dir(next_dir, require_native_tcp=native_tcp)
        except Exception as exc:
            log_system_event(
                "pipeline.candidate_hygiene_failed",
                "error",
                f"Candidate hygiene failed for v{next_v}: {exc}",
                {"next_v": next_v, "source_v": source_v, "stage": ckpt.get("stage")},
            )
            return _json_tool_result({"error": f"Candidate hygiene failed: {exc}"})

        # Write intermediate checkpoint so pipeline state reflects the in-progress retry.
        # Without this, a crash between code reset and worker execution would leave
        # the checkpoint at a stale stage (e.g. "reviewed" or "critic_checked")
        # while the actual code has been wiped back to source.
        retry_plan = _checkpoint_plan_with_tasks(
            ckpt, tasks, replace_existing_tasks=replace_checkpoint_tasks
        )
        rework_plan_metadata = {
            "kind": rework_kind,
            "source_stage": ckpt.get("stage"),
            "reset_performed": reset_before_rework,
            "route": route_policy(ckpt),
        }
        retry_plan = {
            **retry_plan,
            "work_item": rework_plan_metadata,
        }
        for task in tasks:
            if isinstance(task, dict):
                task.setdefault("task_kind", rework_kind)
        retry_plan = _plan_with_accumulated_repair_scope(ckpt, retry_plan, tasks, next_v)
        write_pipeline_checkpoint(next_v, source_v, "repair_planned",
                                  master_plan=retry_plan,
                                  reviewer_feedback=reviewer_feedback,
                                  worker_failure_count=ckpt.get("worker_failure_count", 0),
                                  precommit_rework_count=precommit_rework_count_for_write)
        task_kinds = {
            str(task.get("task_kind") or "")
            for task in tasks or []
            if isinstance(task, dict)
        }
        is_quality_rework = (
            ckpt.get("stage") == "quality_failed"
            or "quality_repair" in rework_kind
            or any("quality_repair" in kind for kind in task_kinds)
        )
        if (
            is_quality_rework
            and not _is_precommit_rework_checkpoint(ckpt)
            and ckpt.get("stage") in {"quality_failed", "repair_planned", "rework_running"}
        ):
            force_sequential_rework = True
            task_skipper = _quality_rework_skipper(next_dir, source_dir_r, next_v, source_v)
            mechanical_trim_results = _apply_mechanical_file_size_trims(
                tasks,
                next_dir,
                source_dir_r,
                next_v,
                source_v,
            )

        if reset_before_rework:
            reviewer_feedback += (
                f"\n\nNOTE: This is a retry. The code in bots/national_v{next_v}/ has been ACTUALLY RESET "
                f"from source bots/national_v{source_v}/. Any modifications described in the feedback "
                f"above no longer exist in the code — you must re-implement them from scratch."
            )
        elif rework_kind == "precommit_repair" or _is_precommit_rework_checkpoint(ckpt):
            reviewer_feedback += (
                f"\n\nNOTE: This is an in-place precommit regression repair. The current code in "
                f"bots/national_v{next_v}/ is the candidate that failed precommit; preserve it except "
                f"for targeted EV/matchup regression fixes."
            )
        else:
            if rework_kind.startswith("crossover_") or ckpt.get("parent2_v") is not None:
                reviewer_feedback += (
                    f"\n\nNOTE: This is an in-place crossover quality repair. The current code in "
                    f"bots/national_v{next_v}/ is the generated crossover candidate and must be preserved "
                    f"except for the exact quality-gate blockers above."
                )
            else:
                reviewer_feedback += (
                    f"\n\nNOTE: This is an in-place quality repair. The current code in "
                    f"bots/national_v{next_v}/ is the generated candidate and must be preserved "
                    f"except for the exact quality-gate blockers above."
                )
        changed_trims = [item for item in mechanical_trim_results if item.get("changed")]
        if changed_trims:
            trim_summary = "; ".join(
                f"{Path(item.get('target', item.get('file', ''))).name}: "
                f"{item.get('before')}L->{item.get('after')}L"
                for item in changed_trims
            )
            reviewer_feedback += (
                "\n\nNOTE: Before LLM workers, the pipeline mechanically removed "
                "non-behavioral Python text (comments/docstrings/blank lines) from "
                f"large file_size targets: {trim_summary}. Continue only if a blocker remains."
            )

    # P2: Validate positive worker intent against EXHAUSTED directions from the
    # experience pool. Negative guardrail prose is ignored to prevent warnings
    # from firing merely because the prompt quotes a forbidden axis.
    exhausted_keywords = _extract_exhausted_keywords()
    exhausted_violations = _exhausted_plan_violations(
        {"tasks": tasks},
        next_v=next_v,
        precomputed_exhausted_keywords=exhausted_keywords,
    )
    if exhausted_violations and ckpt.get("stage") == "master_planned" and not reviewer_feedback:
        audit_attempt = int(ckpt.get("audit_attempt") or 0) + 1
        audit_context = {
            "worker_exhausted_plan_blocked": {
                "validation_errors": exhausted_violations,
                "source_stage": ckpt.get("stage"),
            }
        }
        write_pipeline_checkpoint(
            next_v,
            source_v,
            "direction_audited",
            master_plan={},
            direction_audit=ckpt.get("direction_audit"),
            worker_failure_count=ckpt.get("worker_failure_count", 0),
            audit_attempt=audit_attempt,
            audit_context=audit_context,
            touch_stage_timestamp=True,
        )
        log_system_event(
            "pipeline.worker_exhausted_plan_blocked",
            "error",
            f"Blocked worker execution for v{next_v}: saved Master plan repeats EXHAUSTED direction",
            {
                "next_v": next_v,
                "source_v": source_v,
                "validation_errors": exhausted_violations,
                "audit_attempt": audit_attempt,
            },
        )
        return _json_tool_result({
            "error": "WORKER_EXHAUSTED_PLAN_BLOCKED",
            "next_v": next_v,
            "source_v": source_v,
            "validation_errors": exhausted_violations,
            "next_tool": "run_master",
            "directive": (
                "The saved Master plan repeats an EXHAUSTED direction and was "
                "not allowed to reach workers. The checkpoint has been rolled "
                "back to direction_audited with the invalid master_plan cleared. "
                "Call run_master to produce a fundamentally different execution "
                "axis before execute_workers."
            ),
        })
    if exhausted_keywords:
        for task in tasks:
            prompt_text = _positive_execution_text_from_task(task)
            if _fuzzy_match_exhausted(prompt_text, exhausted_keywords, require_direction_token=True):
                original = task.get("worker_prompt", task.get("instruction", ""))
                task["worker_prompt"] = (
                    original +
                    "\n\n⚠️ WARNING: This task may violate an EXHAUSTED direction. "
                    "Verify carefully — the experience pool marks this area as exhausted "
                    "with no measurable H2H gain. Consider an alternative approach."
                )
                log_system_event(
                    "pipeline.worker_exhausted_warning", "warn",
                    f"Worker {task.get('worker_id', '?')} prompt matches EXHAUSTED direction",
                    {"next_v": next_v},
                )
                break  # One warning per task is sufficient

    if reviewer_feedback and rework_plan_metadata:
        running_plan = (
            _checkpoint_plan_with_tasks(
                ckpt, tasks, replace_existing_tasks=replace_checkpoint_tasks
            )
            if ckpt else {"tasks": tasks}
        )
        running_plan = {**running_plan, "work_item": rework_plan_metadata}
        running_plan = _plan_with_accumulated_repair_scope(ckpt, running_plan, tasks, next_v)
        write_pipeline_checkpoint(next_v, source_v, "rework_running",
                                  master_plan=running_plan,
                                  reviewer_feedback=reviewer_feedback,
                                  worker_failure_count=ckpt.get("worker_failure_count", 0) if ckpt else 0,
                                  precommit_rework_count=precommit_rework_count_for_write)

    ui = _get_ui()
    success, worker_snapshots, audit_focus_areas = await _execute_workers(
        tasks, worker_template, next_dir, next_v,
        [], ui, reviewer_feedback=reviewer_feedback,
        source_v=source_v,
        force_sequential=force_sequential_rework,
        task_skipper=task_skipper,
    )

    boundary_errors = []
    if success:
        # Pre-gate: check that code actually changed before proceeding to quality gates.
        # This catches zero-change workers early, saving Reviewer + Critic LLM calls.
        src_dir = get_bot_dir(source_v)
        if src_dir.exists() and next_dir.exists():
            changed = [p for p in _py_files_changed_between(src_dir, next_dir) if 'backup' not in p]
            if not changed:
                success = False
                log_system_event("pipeline.workers_zero_changes", "error",
                                 f"Workers reported success but zero .py files changed for v{next_v}",
                                 {"next_v": next_v, "source_v": source_v})

    if success:
        boundary_errors = _validate_worker_boundaries(tasks, source_v, next_v,
                                                          worker_snapshots=worker_snapshots)
        if boundary_errors:
            success = False
            # Selective reset: only revert files modified by violating workers
            src_dir = get_bot_dir(source_v)
            if src_dir.exists() and next_dir.exists():
                violated_files = set()
                for err in boundary_errors:
                    # Only revert files from hyperparameter boundary violations.
                    # target_file_violation and new_file_violation are logged but should
                    # not trigger selective reset — they may flag files that the Architect
                    # correctly modified outside declared targets.
                    if err.get("type") == "hyperparameter_boundary_violation":
                        f = err.get("file", "")
                        if f:
                            violated_files.add(f)
                for rel in violated_files:
                    src_file = src_dir / rel
                    dst_file = next_dir / rel
                    if src_file.exists():
                        dst_file.write_text(src_file.read_text())
                    elif dst_file.exists():
                        dst_file.unlink()
                # After resetting files, check if ANY .py files still differ.
                # If not, the code is back to source state — revert checkpoint stage
                # so the orchestrator knows workers need to re-run from scratch.
                remaining_changes = [p for p in _py_files_changed_between(src_dir, next_dir) if 'backup' not in p]
                if not remaining_changes:
                    # All changes were reset — code is identical to source.
                    # Do NOT advance checkpoint to workers_done.
                    log_system_event("pipeline.workers_all_reset", "warn",
                                     f"All worker changes reset for v{next_v} — code identical to v{source_v}",
                                     {"next_v": next_v, "source_v": source_v})

    if success:
        # Preserve the master plan structure (with analysis) from checkpoint,
        # rather than replacing it with the raw tasks list
        plan = (
            _checkpoint_plan_with_tasks(
                ckpt, tasks, replace_existing_tasks=replace_checkpoint_tasks
            )
            if ckpt else {"tasks": tasks}
        )
        if ckpt and ckpt.get("stage") in {"quality_failed", "precommit_failed", "repair_planned", "rework_running"}:
            existing_work = rework_plan_metadata or (
                (ckpt.get("master_plan") or {}).get("work_item")
                if isinstance(ckpt.get("master_plan"), dict) else None
            )
            if existing_work:
                plan = {**plan, "work_item": existing_work}
            plan = _plan_with_accumulated_repair_scope(ckpt, plan, tasks, next_v)
        # Store audit_focus_areas in audit_context so reviewer can read them
        _audit_ctx = None
        if audit_focus_areas:
            _existing_audit = ckpt.get("audit_context", {}) if ckpt else {}
            _audit_ctx = {**_existing_audit, "worker_cot_focus_areas": audit_focus_areas}
        write_pipeline_checkpoint(next_v, source_v, "workers_done",
                                  master_plan=plan, reviewer_feedback=reviewer_feedback,
                                  worker_failure_count=failure_count,
                                  audit_context=_audit_ctx,
                                  precommit_rework_count=precommit_rework_count_for_write)
    else:
        # Increment failure count on worker failure; successful batches do not consume the budget.
        # Always set stage to 'master_planned' on failure — this clearly indicates
        # that workers need re-execution, rather than preserving a stale stage
        # from before the failure (e.g. "reviewed" or "critic_checked").
        plan = (
            _checkpoint_plan_with_tasks(
                ckpt, tasks, replace_existing_tasks=replace_checkpoint_tasks
            )
            if ckpt else {"tasks": tasks}
        )
        if reviewer_feedback:
            existing_work = rework_plan_metadata or (
                (ckpt.get("master_plan") or {}).get("work_item")
                if ckpt and isinstance(ckpt.get("master_plan"), dict) else None
            )
            plan = {
                **plan,
                "work_item": existing_work or {
                    "kind": "worker_retry_after_failure",
                    "source_stage": ckpt.get("stage") if ckpt else None,
                    "route": route_policy(ckpt) if ckpt else {},
                },
            }
            plan = _plan_with_accumulated_repair_scope(ckpt, plan, tasks, next_v)
        write_pipeline_checkpoint(next_v, source_v,
                                  "repair_planned" if reviewer_feedback else "master_planned",
                                  master_plan=plan, reviewer_feedback=reviewer_feedback,
                                  worker_failure_count=failure_count + 1,
                                  precommit_rework_count=precommit_rework_count_for_write)

    sev = "success" if success else "error"
    log_system_event("pipeline.workers_done", sev,
                     f"Workers {'passed' if success else 'failed'} for v{next_v}",
                     {"next_v": next_v, "num_workers": len(tasks), "success": success,
                      "elapsed_sec": round(time.time() - _t0, 2)})

    result = {
        "success": success,
        "boundary_errors": boundary_errors,
        "logs": ui.get_output(),
        "costs": ui.costs,
        "audit_focus_areas": audit_focus_areas,
    }
    return _json_tool_result(result)
