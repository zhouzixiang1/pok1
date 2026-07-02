"""Pipeline tools: quality gates, code preparation, review, and critic."""

import asyncio
import hashlib
import json
import os
import shutil
import time
from pathlib import Path

from tool_runtime_guard import tool

from logging_config import get_logger
_log = get_logger("gates")

from evolution_core import (
    get_bot_dir,
    get_logs_dir,
    find_current_v,
    verify_code,
    run_import_contract_test,
    check_code_size,
    run_smoke_test,
    run_decision_test_details,
    run_national_protocol_tests,
    parse_json_output,
    run_claude_query,
    _run_critic,
)
from tool_helpers import (
    _get_ui, _json_tool_result,
    _matching_checkpoint, _record_gate, _gate_payload, _state_blocked,
    _quality_gate_ok, _review_gate_ok, _critic_gate_ok,
    _py_files_changed_between, _resolve_version_args, PROJECT_ROOT,
    _set_pipeline_status, read_pipeline_checkpoint,
)
from fix_verification import verify_fixes
from system_log import log_system_event
from llm_failure import is_llm_infra_error, infra_payload
from llm_query import llm_cancel_scope
import spot_analyzer
from pipeline_schema import GateResult, ScoreCard
from workflow_profiles import get_workflow_profile
from worker_boundary import audit_changed_files_against_plan, hash_changed_files

try:
    from candidate_store import append_candidate_event
except Exception:  # pragma: no cover - import fallback for unusual test paths
    append_candidate_event = None


# ── Phase 2: AgentAssay SPRT decision-test gate (feature-flagged) ──
# When True, the decision-test quality gate uses run_decision_tests_sprt_aggregate
# (per-scenario Wald SPRT with sequential early-stop) instead of the classic
# single-shot run_decision_test_details. SPRT gives tighter type-I control for
# stochastic LLM bots but has only been validated on synthetic unit tests, so the
# default is OFF (zero-regression: byte-for-byte the classic path). Flip to True
# only after a gray-run confirms the type-I rate on real generation traffic.
# Mirrors the PRECOMMIT_SEQUENTIAL_EARLY_STOP flag convention (module constant,
# not an env var).
DECISION_TEST_SPRT_ENABLED = False
DYNAMIC_TEST_LLM_TIMEOUT = int(os.environ.get("POK_DYNAMIC_TEST_LLM_TIMEOUT", "25"))
DYNAMIC_TEST_HEURISTIC_SUFFICIENT = int(os.environ.get("POK_DYNAMIC_TEST_HEURISTIC_SUFFICIENT", "4"))

_POSITION_SEMANTICS_PATTERNS = {
    "dealer==bb": "dealer is SB in heads-up; BB is 1 - dealer_id",
    "dealer == bb": "dealer is SB in heads-up; BB is 1 - dealer_id",
    "dealer is bb": "dealer is SB in heads-up; BB is 1 - dealer_id",
    "dealer=bb": "dealer is SB in heads-up; BB is 1 - dealer_id",
    "sb acts first every street": "SB acts first preflop only; BB acts first postflop",
    "sb first postflop": "BB acts first postflop",
    "bb postflop in-position": "SB is in position postflop; BB acts first",
    "flop_sb_act_first": "decision templates must use BB-first postflop semantics",
}


def detect_position_semantics_errors(bot_dir: Path) -> list[str]:
    """Detect old heads-up position assumptions in candidate bot code.

    Authoritative convention: dealer_id is SB, BB is ``1 - dealer_id`` in
    heads-up, BB acts first on flop/turn/river, and SB is in position postflop.
    """
    errors = []
    for path in sorted(Path(bot_dir).rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue
        for lineno, line in enumerate(lines, 1):
            lowered = line.lower()
            compact = "".join(lowered.split())
            for pattern, explanation in _POSITION_SEMANTICS_PATTERNS.items():
                if pattern in lowered:
                    rel = path.relative_to(bot_dir)
                    errors.append(f"{rel}:{lineno}: {explanation} ({pattern})")
            if "sb=next_player(dealer_id,1)" in compact:
                rel = path.relative_to(bot_dir)
                errors.append(f"{rel}:{lineno}: SB must be dealer_id, not next_player(dealer_id, 1)")
            if "bb=next_player(dealer_id,2)" in compact:
                rel = path.relative_to(bot_dir)
                errors.append(f"{rel}:{lineno}: BB must be 1 - dealer_id, not next_player(dealer_id, 2)")
    return errors[:20]


def _record_quality_failure(gen, worker_id, role, error, **extra):
    """Record a quality gate rejection (reviewer/critic) to worker_failures.jsonl.

    RC5: category="gate" separates these strategic rejections from real
    worker-exec failures (_record_worker_failure writes category="worker") so
    the Worker Failures view can filter out the 49 critic / 9 reviewer noise
    and surface only genuine compile/timeout crashes.
    """
    from evolution_core import WORKER_FAILURES_FILE, locked_file
    entry = {"gen": gen, "worker_id": worker_id, "role": role, "error": error,
             "timestamp": time.time(), "category": "gate"}
    entry.update({k: v for k, v in extra.items() if v is not None and v is not False})
    with locked_file(WORKER_FAILURES_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _idempotency_check(v, source_v, stage_set, gate_name, approval_key="approved",
                       extra_ok_keys=(), directive="", cache_validator=None):
    """Check if a pipeline stage has already been completed; return cached result or None.

    Args:
        v: Bot version.
        source_v: Parent version.
        stage_set: Tuple/list of stage strings that mean "this stage passed".
        gate_name: Key inside gate_results (e.g. "quality", "review", "critic").
        approval_key: The key to check for truthiness (default "approved").
        extra_ok_keys: Additional keys that count as truthy (e.g. ("force_advanced",)).
        directive: Message to include when returning cached result.

    Returns:
        An MCP-formatted result dict if the stage already passed, or None.
    """
    ckpt = _matching_checkpoint(v, source_v)
    if not ckpt or ckpt.get("stage") not in stage_set:
        return None
    gate = ckpt.get("gate_results", {}).get(gate_name, {})
    if gate.get(approval_key) is True or any(gate.get(k) is True for k in extra_ok_keys):
        if cache_validator is not None and not cache_validator(gate):
            return None
        gate["idempotent_cache"] = True
        gate["checkpoint_recorded"] = True
        gate["directive"] = directive
        return _json_tool_result(gate)
    return None


def _bot_code_fingerprint(bot_dir):
    """Stable hash of the bot's Python source files for gate cache validity."""
    root = Path(bot_dir)
    if not root.exists():
        return ""
    h = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts):
        rel = path.relative_to(root).as_posix()
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        try:
            h.update(path.read_bytes())
        except OSError:
            continue
        h.update(b"\0")
    return h.hexdigest()


# ──────────────────────────────────────────────
# Quality Gates
# ──────────────────────────────────────────────

@tool("run_quality_gates", "Run all quality gates on a bot: code_changed, declared_scope, compile/runtime import, protected contracts, smoke, national protocol/acceptance, decision, size, fix verification, telemetry fidelity, and reachability.", {"version": int, "source_v": int})
async def run_quality_gates(args):
    _t0 = time.time()
    v, source_v = _resolve_version_args(args)
    if v is None:
        return _json_tool_result({"error": "Missing version and no active pipeline checkpoint"})
    v = int(v)
    source_v = int(source_v) if source_v is not None else None
    bot_dir = get_bot_dir(v)
    workflow_profile = get_workflow_profile()
    candidate_id = f"claude_v{v}_from_v{source_v}" if source_v is not None else f"claude_v{v}"
    if append_candidate_event:
        try:
            append_candidate_event(
                "quality_started",
                version=v,
                source_v=source_v,
                candidate_id=candidate_id,
                profile_id=workflow_profile.profile_id,
                workflow_profile_id=workflow_profile.profile_id,
                run_id=f"{v}#0",
                stage="quality",
                parent_ids=[f"claude_v{source_v}"] if source_v is not None else [],
            )
        except Exception as e:
            _log.warning("candidate ledger quality_started write failed: %s", e)

    _set_pipeline_status(f"Running quality gates for v{v}")

    # CRITICAL: Check that code actually changed vs source before considering
    # quality-gate cache reuse. A reviewer/precommit rejection may re-run workers
    # while the checkpoint is still at quality_passed; stale quality results must
    # not be reused for different code.
    code_changed = True
    changed_files_list = []
    source_dir = None
    if source_v is not None:
        source_dir = get_bot_dir(source_v)
        changed_files_list = [p for p in _py_files_changed_between(source_dir, bot_dir) if 'backup' not in p]
        code_changed = len(changed_files_list) > 0
        if not code_changed:
            log_system_event("pipeline.quality_no_changes", "error",
                             f"Quality gates: v{v} is byte-for-byte identical to v{source_v} -- workers made zero changes",
                             {"version": v, "source_v": source_v})
    code_fingerprint = _bot_code_fingerprint(bot_dir)
    diff_hash = hash_changed_files(bot_dir, changed_files_list) if changed_files_list else ""

    declared_scope_ok = True
    declared_scope_errors = []
    declared_scope_metrics = {}
    declared_skill_layers = []
    _quality_ckpt_for_scope = _matching_checkpoint(v, source_v) if source_v is not None else _matching_checkpoint(v)
    _master_plan_for_scope = (_quality_ckpt_for_scope or {}).get("master_plan", {})
    _plan_tasks = _master_plan_for_scope.get("tasks", []) if isinstance(_master_plan_for_scope, dict) else []
    if _plan_tasks and changed_files_list:
        try:
            declared_skill_layers = sorted({
                str(task.get("skill_layer", "")).strip()
                for task in _plan_tasks
                if str(task.get("skill_layer", "")).strip()
            })
            _scope_audit = audit_changed_files_against_plan(
                changed_files_list,
                _plan_tasks,
                next_v=v,
            )
            declared_scope_ok = _scope_audit.passed
            declared_scope_errors = _scope_audit.violations
            declared_scope_metrics = _scope_audit.to_gate_metrics()
            if not declared_scope_ok:
                log_system_event(
                    "pipeline.declared_scope_failed",
                    "error",
                    f"Declared scope gate failed for v{v}: {len(declared_scope_errors)} undeclared file change(s)",
                    {
                        "version": v,
                        "source_v": source_v,
                        "changed_files": changed_files_list[:20],
                        "allowed_files": _scope_audit.allowed_files[:30],
                        "violations": declared_scope_errors[:10],
                    },
                )
        except Exception as e:
            declared_scope_ok = False
            declared_scope_errors = [f"declared_scope_check_error: {type(e).__name__}: {str(e)[:200]}"]
    elif changed_files_list:
        declared_scope_metrics = {
            "skipped": True,
            "reason": "master_plan_tasks_unavailable",
            "changed_files": changed_files_list[:20],
        }

    def _quality_cache_current(gate):
        cached_fingerprint = gate.get("code_fingerprint")
        if cached_fingerprint and cached_fingerprint == code_fingerprint:
            return True
        log_system_event(
            "pipeline.quality_cache_stale", "warn",
            f"Quality gate cache stale for v{v}; bot code changed since cached gate, rerunning quality gates.",
            {
                "version": v,
                "source_v": source_v,
                "cached_fingerprint": cached_fingerprint,
                "current_fingerprint": code_fingerprint,
            },
        )
        return False

    # Idempotency guard: skip if quality gates already passed for this version
    _cached = _idempotency_check(
        v, source_v,
        stage_set=("quality_passed", "reviewed", "critic_checked", "verified", "archived"),
        gate_name="quality",
        approval_key="all_passed",
        directive="Quality gates ALREADY PASSED. Call run_review next.",
        cache_validator=_quality_cache_current,
    )
    if _cached:
        return _cached

    compile_errors = verify_code(bot_dir)
    import_errors = run_import_contract_test(bot_dir)
    try:
        from protected_contracts import check_bot_protocol_contract
        protected_contract_errors = check_bot_protocol_contract(bot_dir)
    except Exception as e:
        protected_contract_errors = [f"protected_contract_check_error: {type(e).__name__}: {str(e)[:200]}"]
    if import_errors:
        log_system_event(
            "pipeline.import_contract_failed", "error",
            f"Runtime import contract failed for v{v}: "
            f"{import_errors[0].get('module')} {import_errors[0].get('exception')}: "
            f"{import_errors[0].get('message')}",
            {"version": v, "source_v": source_v, "errors": import_errors[:3]},
        )
    else:
        log_system_event(
            "pipeline.import_contract_passed", "info",
            f"Runtime import contract passed for v{v}",
            {"version": v, "source_v": source_v},
        )
    # A3 (evolution-plan-refresh-jun21): placement-shadow advisory (NON-blocking).
    # Flags detector call-sites placed after a to_call>=my_chips early-return — the
    # INERTNESS root cause that recurred v138-v143 (guards wired at strategy.py:1041
    # after the allin-cover early-return at :1018 = unreachable for stack-off spots).
    placement_shadow_warnings = []
    try:
        from code_verification import detect_placement_shadow_warnings
        placement_shadow_warnings = detect_placement_shadow_warnings(bot_dir)
        if placement_shadow_warnings:
            log_system_event(
                "pipeline.placement_shadow", "warn",
                f"Placement-shadow detectors in v{v}: {len(placement_shadow_warnings)} "
                f"call-site(s) after to_call>=my_chips early-return (INERTNESS risk — "
                f"relocate call-site, do not re-tune)",
                {"version": v, "warnings": placement_shadow_warnings[:6]},
            )
    except Exception as e:
        _log.warning("placement_shadow check error: %s", e)
    # M6 (b057ead follow-up): telemetry-fidelity AST gate — BLOCKING.
    # Flags multi-arm margin/delta detectors whose stderr.write telemetry is nested
    # inside a bucket/signal If-gate (sub-arm-scoped) instead of hoisted to function
    # scope. Sub-arm-only telemetry yields a false-INERT verdict on daemon grep (v154
    # 99.98%-delta=+0 artifact → v155 Master misread the LIVE framework as dead and
    # listed it in do_not_touch). Unlike placement_shadow (advisory — its warnings
    # never reach reviewer_prompt, so it has zero enforcement), M6 is BLOCKING:
    # master_prompt.md M6 explicitly acknowledges "Reviewer MUST reject" clauses are
    # NON-enforceable (Reviewer only receives {master_plan} JSON), so only a hard
    # precommit gate can stop the 9-gen INERTNESS loop (M5 advisory precedent failed).
    telemetry_fidelity_warnings = []
    try:
        from code_verification import detect_telemetry_fidelity_warnings
        telemetry_fidelity_warnings = detect_telemetry_fidelity_warnings(bot_dir)
        if telemetry_fidelity_warnings:
            log_system_event(
                "pipeline.telemetry_fidelity", "error",
                f"Telemetry-fidelity violations in v{v}: {len(telemetry_fidelity_warnings)} "
                f"multi-arm detector(s) with sub-arm-scoped stderr.write (false-INERT risk)",
                {"version": v, "warnings": telemetry_fidelity_warnings[:6]},
            )
    except Exception as e:
        _log.warning("telemetry_fidelity check error: %s", e)
    telemetry_fidelity_ok = len(telemetry_fidelity_warnings) == 0

    # R1 (2026-07-01): newly-added top-level helpers must be wired into the bot.
    # This blocks the observed v239 failure mode where a Worker appended a good
    # postflop helper but never imported/called it; compile, smoke, and decision
    # scenarios all passed because the change was inert.
    reachability_warnings = []
    if source_dir is not None and changed_files_list:
        try:
            from code_verification import detect_new_function_reachability_warnings
            reachability_warnings = detect_new_function_reachability_warnings(
                source_dir, bot_dir, changed_files_list
            )
            if reachability_warnings:
                log_system_event(
                    "pipeline.reachability_gate", "error",
                    f"Reachability violations in v{v}: {len(reachability_warnings)} "
                    "new function(s) have no non-import references (dead-code risk)",
                    {"version": v, "warnings": reachability_warnings[:6]},
                )
        except Exception as e:
            _log.warning("reachability check error: %s", e)
    reachability_ok = len(reachability_warnings) == 0

    position_semantics_errors = detect_position_semantics_errors(bot_dir)
    position_semantics_ok = len(position_semantics_errors) == 0
    if position_semantics_errors:
        log_system_event(
            "pipeline.position_semantics_failed",
            "error",
            f"Position semantics violations in v{v}: {len(position_semantics_errors)} issue(s)",
            {"version": v, "errors": position_semantics_errors[:10]},
        )

    smoke_errors = run_smoke_test(bot_dir)
    national_protocol_errors = run_national_protocol_tests()
    national_acceptance_ok = True
    national_acceptance_errors = []
    national_acceptance_payload = {}
    national_acceptance_enabled = os.environ.get("POK_NATIONAL_ACCEPTANCE_GATE", "1") != "0"
    national_acceptance_hands = int(
        os.environ.get(
            "POK_NATIONAL_ACCEPTANCE_HANDS",
            str(workflow_profile.national_acceptance_hands),
        )
    )
    if national_acceptance_enabled and source_v is not None:
        try:
            _bot_under_project_bots = bot_dir.resolve().is_relative_to((PROJECT_ROOT / "bots").resolve())
        except AttributeError:
            _bot_under_project_bots = str(bot_dir.resolve()).startswith(str((PROJECT_ROOT / "bots").resolve()))
        can_run_national_acceptance = (
            len(compile_errors) == 0
            and len(import_errors) == 0
            and len(protected_contract_errors) == 0
            and len(smoke_errors) == 0
            and (bot_dir / "main.py").exists()
            and _bot_under_project_bots
        )
        if can_run_national_acceptance:
            try:
                from national_acceptance import run_acceptance_for_candidate
                _acceptance = await run_acceptance_for_candidate(
                    bot_dir,
                    source_v=source_v,
                    hands=national_acceptance_hands,
                    max_opponents=2,
                    strict=bool(workflow_profile.national_acceptance_hard),
                )
                national_acceptance_ok = bool(_acceptance.passed)
                national_acceptance_errors = _acceptance.issues[:5]
                national_acceptance_payload = _acceptance.model_dump()
                log_system_event(
                    "pipeline.national_acceptance_passed" if national_acceptance_ok else "pipeline.national_acceptance_failed",
                    "success" if national_acceptance_ok else "error",
                    f"National acceptance {'passed' if national_acceptance_ok else 'failed'} for v{v} "
                    f"({national_acceptance_hands} hands/pair)",
                    {
                        "version": v,
                        "source_v": source_v,
                        "hands": national_acceptance_hands,
                        "opponents": _acceptance.opponents,
                        "issues": national_acceptance_errors,
                        "summary": _acceptance.summary,
                    },
                )
            except Exception as e:
                national_acceptance_ok = False
                national_acceptance_errors = [f"national_acceptance_exception: {type(e).__name__}: {str(e)[:200]}"]
                national_acceptance_payload = {"error": national_acceptance_errors[0]}
                _log.warning("national acceptance gate failed to run for v%s: %s", v, e)
        else:
            national_acceptance_ok = True
            national_acceptance_errors = ["national_acceptance_skipped_due_to_failed_prerequisites"]
            national_acceptance_payload = {"skipped": True, "reason": national_acceptance_errors[0]}

    # --- B3: Heuristic Dynamic Regression Tests from Diff ---
    # Deterministic coverage runs first. The LLM generator is now an augmenting
    # source, not the gatekeeper for dynamic coverage, so a timeout cannot leave
    # changed branches completely untested.
    dynamic_test_meta = {
        "heuristic_count": 0,
        "llm_count": 0,
        "llm_status": "not_run",
        "llm_timeout_sec": DYNAMIC_TEST_LLM_TIMEOUT,
    }
    heuristic_scenarios = []
    existing_ids = []
    if source_v is not None and changed_files_list:
        try:
            import difflib as _difflib
            from decision_tester import (
                SCENARIOS_FILE,
                generate_scenarios_from_diff,
                save_dynamic_scenarios,
                load_dynamic_scenarios,
            )
            if SCENARIOS_FILE.exists():
                with open(SCENARIOS_FILE) as _f:
                    for s in json.load(_f):
                        existing_ids.append(s.get("id", ""))
            _src_dir = get_bot_dir(source_v)
            _dst_dir = get_bot_dir(v)

            _diff_parts = []
            for _rel in changed_files_list:
                _src_file = _src_dir / _rel
                _dst_file = _dst_dir / _rel
                _before = _src_file.read_text() if _src_file.exists() else ""
                _after = _dst_file.read_text() if _dst_file.exists() else ""
                if _before != _after:
                    _diff = _difflib.unified_diff(
                        _before.splitlines(keepends=True),
                        _after.splitlines(keepends=True),
                        fromfile=f"v{source_v}/{_rel}", tofile=f"v{v}/{_rel}",
                        n=2,
                    )
                    _diff_text = "".join(_diff)
                    if _diff_text:
                        _diff_parts.append(_diff_text)

            if _diff_parts:
                _full_diff = "\n".join(_diff_parts)[-8000:]
                heuristic_scenarios = generate_scenarios_from_diff(
                    _full_diff, str(_src_dir), str(_dst_dir)
                )
                dynamic_test_meta["heuristic_count"] = len(heuristic_scenarios)
                if heuristic_scenarios:
                    _existing = load_dynamic_scenarios()
                    _existing_ids = {s.get("id") for s in _existing}
                    _new_to_save = [s for s in heuristic_scenarios
                                    if s.get("id") not in _existing_ids]
                    save_dynamic_scenarios(_existing + _new_to_save)
                    _log.info(
                        "B3: Generated %d heuristic scenarios from diff for v%d",
                        len(heuristic_scenarios), v
                    )
        except Exception as e:
            dynamic_test_meta["heuristic_error"] = str(e)[:300]
            _log.warning("B3 heuristic scenario generation error: %s", e)

    # --- P0-3: LLM-Generated Dynamic Decision Tests (augment only) ---
    dynamic_scenarios = []
    if source_v is not None and changed_files_list:
        try:
            from audit_agents import _generate_dynamic_tests
            if len(heuristic_scenarios) >= DYNAMIC_TEST_HEURISTIC_SUFFICIENT:
                dynamic_test_meta["llm_status"] = "skipped_heuristic_sufficient"
                log_system_event(
                    "pipeline.dynamic_test_gen_skipped",
                    "info",
                    f"v{v}: skipped LLM dynamic test generation; "
                    f"{len(heuristic_scenarios)} deterministic scenarios already generated",
                    {"version": v, "source_v": source_v,
                     "heuristic_count": len(heuristic_scenarios)},
                )
            else:
                ckpt_dt = _matching_checkpoint(v, source_v)
                master_plan_dt = ckpt_dt.get("master_plan", {}) if ckpt_dt else {}
                ui = _get_ui()
                with llm_cancel_scope(
                    "dynamic_test_gen",
                    reason="parent_timeout",
                    timeout_sec=DYNAMIC_TEST_LLM_TIMEOUT,
                ):
                    dynamic_scenarios = await asyncio.wait_for(
                        _generate_dynamic_tests(
                            v, source_v, changed_files_list, master_plan_dt, existing_ids, ui
                        ),
                        timeout=DYNAMIC_TEST_LLM_TIMEOUT,
                    )
                dynamic_test_meta["llm_status"] = "ok"
                dynamic_test_meta["llm_count"] = len(dynamic_scenarios or [])
        except asyncio.TimeoutError:
            dynamic_test_meta["llm_status"] = "timeout"
            log_system_event(
                "pipeline.dynamic_test_gen_timeout",
                "warn",
                f"v{v}: LLM dynamic test generation timed out after "
                f"{DYNAMIC_TEST_LLM_TIMEOUT}s; using deterministic scenarios",
                {"version": v, "source_v": source_v,
                 "timeout_s": DYNAMIC_TEST_LLM_TIMEOUT,
                 "heuristic_count": len(heuristic_scenarios)},
            )
        except Exception as e:
            dynamic_test_meta["llm_status"] = "error"
            dynamic_test_meta["llm_error"] = str(e)[:300]
            _log.warning("Dynamic test generation error: %s", e)

    # Combine both dynamic sources
    _all_dynamic = (dynamic_scenarios or []) + heuristic_scenarios
    dynamic_test_meta["combined_count"] = len(_all_dynamic)

    # Decision-test gate: classic single-shot path by default; optional Phase-2
    # per-scenario SPRT aggregation when DECISION_TEST_SPRT_ENABLED. The SPRT
    # path returns the SAME dict shape (pass_rate/total/critical_*/failures), so
    # the downstream gate logic below is unchanged — only the per-scenario
    # verdict source differs.
    if DECISION_TEST_SPRT_ENABLED:
        from decision_tester import run_decision_tests_sprt_aggregate
        decision_detail = run_decision_tests_sprt_aggregate(
            bot_dir, extra_scenarios=_all_dynamic or None
        )
    else:
        decision_detail = run_decision_test_details(bot_dir, extra_scenarios=_all_dynamic or None)
    decision_rate = decision_detail.get("pass_rate", 0.0)
    decision_total = decision_detail.get("total", 0)
    decision_skill_layers = decision_detail.get("skill_layers", {})
    critical_failures = decision_detail.get("critical_failures", [])
    critical_ok = len(critical_failures) == 0
    total_lines, oversized = check_code_size(bot_dir, source_dir=source_dir)
    decision_ok = decision_rate >= 0.7 and critical_ok and decision_total > 0

    # --- P1-3: Structural fix-verification gate (authoritative fix-present judgment) ---
    # fix_injection.py uses substring matching which silently misses when a worker
    # refactors the target function. verify_fixes() runs STRUCTURAL/RUNTIME checks in
    # subprocess isolation so a confirmed invariant violation blocks the pipeline
    # regardless of how the code was written. A verifier FAILURE (exception) is never
    # blocking — only a CONFIRMED invariant violation is.
    fix_results = verify_fixes(bot_dir)
    fix_ok = all(r.get("ok", False) for r in fix_results.values())
    fix_failed = {fid: r for fid, r in fix_results.items() if not r.get("ok", False)}

    # fix-3: TRUE-SHADOW placement is a blocking gate (INERTNESS root cause).
    # v156-v165 produced 10 generations of TRUE-SHADOW `_river_stackoff_guard`
    # that passed quality gates because placement_shadow was advisory-only.
    true_shadows = [w for w in placement_shadow_warnings if 'TRUE SHADOW' in w]
    if true_shadows:
        for w in true_shadows:
            compile_errors.append(f"BLOCKING: {w}")
            _record_quality_failure(v, "placement_shadow", "placement_shadow",
                f"TRUE-SHADOW detector call-site unreachable for stack-covering all-ins. {w}")

    all_passed = (
        len(compile_errors) == 0
        and len(import_errors) == 0
        and len(protected_contract_errors) == 0
        and len(smoke_errors) == 0
        and len(national_protocol_errors) == 0
        and national_acceptance_ok
        and decision_ok
        and len(oversized) == 0
        and code_changed  # MUST have at least one changed .py file
        and declared_scope_ok  # worker/candidate diff must match declared target files
        and fix_ok  # P1-3: missing mandatory fix blocks the pipeline
        and telemetry_fidelity_ok  # M6: multi-arm detector telemetry must be function-scope (false-INERT prevention)
        and reachability_ok  # R1: newly-added helper code must be wired/called
        and position_semantics_ok  # National/local heads-up identity: dealer=SB, BB first postflop
    )

    result = {
        "version": v,
        "code_changed": code_changed,
        "changed_files": changed_files_list,
        "compile_ok": len(compile_errors) == 0,
        "compile_errors": compile_errors[:3] if compile_errors else [],
        "import_ok": len(import_errors) == 0,
        "import_errors": import_errors[:3] if import_errors else [],
        "protected_contract_ok": len(protected_contract_errors) == 0,
        "protected_contract_errors": protected_contract_errors[:3] if protected_contract_errors else [],
        "smoke_ok": len(smoke_errors) == 0,
        "smoke_errors": smoke_errors[:3] if smoke_errors else [],
        "national_protocol_ok": len(national_protocol_errors) == 0,
        "national_protocol_errors": national_protocol_errors[:3] if national_protocol_errors else [],
        "national_acceptance_ok": national_acceptance_ok,
        "national_acceptance_errors": national_acceptance_errors[:5] if national_acceptance_errors else [],
        "national_acceptance": national_acceptance_payload,
        "decision_pass_rate": round(decision_rate, 2),
        "decision_ok": decision_ok,
        "critical_scenarios_passed": critical_ok,
        "critical_passed": decision_detail.get("critical_passed", 0),
        "critical_total": decision_detail.get("critical_total", 0),
        "critical_failures": critical_failures,
        "decision_failures": decision_detail.get("failures", []),
        "scenario_results": decision_detail.get("scenarios", []),
        "decision_skill_layers": decision_skill_layers,
        "dynamic_test_generation": dynamic_test_meta,
        "total_lines": total_lines,
        "oversized_files": {name: lines for name, lines, _ in oversized} if oversized else {},
        "size_ok": len(oversized) == 0,
        "fix_verification": fix_results,
        "fix_ok": fix_ok,
        "all_passed": all_passed,
        # A3/fix-3: TRUE-SHADOW is BLOCKING (added to compile_errors above);
        # "review" level warnings remain advisory. Reviewer/critic/orchestrator
        # can read these to distinguish blocking vs advisory.
        "placement_shadow_warnings": placement_shadow_warnings,
        # M6: BLOCKING (unlike placement_shadow "review" level which is advisory).
        # detector whose stderr.write is nested in a bucket If-gate yields sub-arm-only
        # telemetry → false-INERT on daemon grep (v154 99.98%-delta=+0 artifact).
        "telemetry_fidelity_warnings": telemetry_fidelity_warnings,
        "telemetry_fidelity_ok": telemetry_fidelity_ok,
        "reachability_warnings": reachability_warnings,
        "reachability_ok": reachability_ok,
        "position_semantics_ok": position_semantics_ok,
        "position_semantics_errors": position_semantics_errors[:10],
        "code_fingerprint": code_fingerprint,
        "diff_hash": diff_hash,
        "declared_scope_ok": declared_scope_ok,
        "declared_scope_errors": declared_scope_errors[:10],
        "declared_scope": declared_scope_metrics,
        "skill_layers": declared_skill_layers,
    }

    # Build list of which specific gates failed (for diagnostics)
    failed_gates_detail = []
    if compile_errors:
        failed_gates_detail.append("compile")
    if import_errors:
        first_import = import_errors[0]
        failed_gates_detail.append(
            f"runtime_import({first_import.get('module')}: "
            f"{first_import.get('exception')} {first_import.get('message')})"
        )
    if protected_contract_errors:
        failed_gates_detail.append("protected_contract")
    if true_shadows:
        failed_gates_detail.append(
            f"placement_shadow({'; '.join(w[:120] for w in true_shadows[:3])})"
        )
    if smoke_errors:
        failed_gates_detail.append("smoke_test")
    if national_protocol_errors:
        failed_gates_detail.append("national_protocol_tests")
    if not national_acceptance_ok:
        failed_gates_detail.append("national_acceptance")
    if not decision_ok:
        failed_gates_detail.append(f"decision_tests({decision_rate:.0%})")
    if not code_changed:
        failed_gates_detail.append(f"no_code_changes(v{v} identical to v{source_v})")
    if not declared_scope_ok:
        failed_gates_detail.append(f"declared_scope({'; '.join(declared_scope_errors[:3])})")
    if oversized:
        failed_gates_detail.append(f"file_size({', '.join(f'{n}:{l}L/{lim}L' for n, l, lim in oversized)})")
    if not fix_ok:
        detail_parts = [f"{fid}: {r.get('reason', 'unknown')[:160]}" for fid, r in fix_failed.items()]
        failed_gates_detail.append(f"missing_fix({'; '.join(detail_parts)})")
        # Record to worker_failures.jsonl so future worker prompts see the missing fix
        # (this is the primary feedback path into workers; reviewer_feedback injection
        # is intentionally omitted to avoid an out-of-order _ckpt reference here).
        for fid, r in fix_failed.items():
            _record_quality_failure(
                v, "fix_verifier", fid,
                f"Mandatory fix {fid} NOT present: {r.get('reason', '')[:2000]}",
            )
    if not telemetry_fidelity_ok:
        failed_gates_detail.append(
            f"telemetry_fidelity({'; '.join(w[:120] for w in telemetry_fidelity_warnings[:3])})"
        )
        # Record the M6 telemetry-fidelity violation to worker_failures so the next
        # worker attempt sees the hoist recipe (function-scope stderr.write + fixture).
        for w in telemetry_fidelity_warnings:
            _record_quality_failure(
                v, "telemetry_fidelity", "multi_arm_detector",
                f"M6 telemetry-fidelity violation (false-INERT risk): {w[:2000]}",
            )
    if not reachability_ok:
        failed_gates_detail.append(
            f"reachability({'; '.join(w[:120] for w in reachability_warnings[:3])})"
        )
        for w in reachability_warnings:
            _record_quality_failure(
                v, "reachability", "dead_code",
                f"R1 reachability violation: {w[:2000]}",
            )
    if not position_semantics_ok:
        failed_gates_detail.append(
            f"position_semantics({'; '.join(e[:120] for e in position_semantics_errors[:3])})"
        )
        for err in position_semantics_errors[:6]:
            _record_quality_failure(
                v, "position_semantics", "national_rules",
                f"Position semantics violation: {err}",
            )

    result["failed_gates"] = failed_gates_detail if not all_passed else []
    quality_detail = {
        "all_passed": all_passed,
        "critical_scenarios_passed": critical_ok,
        "decision_pass_rate": round(decision_rate, 4),
        "decision_ok": decision_ok,
        "failed_gates": result["failed_gates"],
        "compile_ok": result["compile_ok"],
        "compile_errors": result["compile_errors"],
        "import_ok": result["import_ok"],
        "import_errors": result["import_errors"],
        "protected_contract_ok": result["protected_contract_ok"],
        "protected_contract_errors": result["protected_contract_errors"],
        "smoke_ok": result["smoke_ok"],
        "smoke_errors": result["smoke_errors"],
        "national_protocol_ok": result["national_protocol_ok"],
        "national_protocol_errors": result["national_protocol_errors"],
        "national_acceptance_ok": result["national_acceptance_ok"],
        "national_acceptance_errors": result["national_acceptance_errors"],
        "national_acceptance": national_acceptance_payload,
        "dynamic_test_generation": dynamic_test_meta,
        "size_ok": result["size_ok"],
        "oversized_files": result["oversized_files"],
        "code_changed": code_changed,
        "changed_files": changed_files_list[:20],
        "declared_scope_ok": declared_scope_ok,
        "declared_scope_errors": declared_scope_errors[:6],
        "declared_scope": declared_scope_metrics,
        "skill_layers": declared_skill_layers,
        "diff_hash": diff_hash,
        "fix_ok": fix_ok,
        "placement_shadow_warnings": placement_shadow_warnings[:10],
        "placement_shadow_review_count": len([w for w in placement_shadow_warnings if "TRUE SHADOW" not in w]),
        "telemetry_fidelity_ok": telemetry_fidelity_ok,
        "reachability_ok": reachability_ok,
        "reachability_warnings": reachability_warnings[:6],
        "position_semantics_ok": position_semantics_ok,
        "position_semantics_errors": position_semantics_errors[:10],
        "code_fingerprint": code_fingerprint,
        "critical_failures": critical_failures[:3],
        "decision_skill_layers": decision_skill_layers,
    }
    scorecard = ScoreCard(name="quality")
    scorecard.add(GateResult.from_bool("code_changed", code_changed, failures=[] if code_changed else ["bot code is byte-for-byte identical to source"]))
    scorecard.add(GateResult.from_bool(
        "declared_scope",
        declared_scope_ok,
        metrics=declared_scope_metrics,
        failures=declared_scope_errors[:6],
    ))
    scorecard.add(GateResult.from_bool("compile", len(compile_errors) == 0, failures=compile_errors[:3]))
    scorecard.add(GateResult.from_bool("runtime_import", len(import_errors) == 0, failures=[str(e) for e in import_errors[:3]]))
    scorecard.add(GateResult.from_bool("protected_contract", len(protected_contract_errors) == 0, failures=protected_contract_errors[:3]))
    scorecard.add(GateResult.from_bool("smoke", len(smoke_errors) == 0, failures=smoke_errors[:3]))
    scorecard.add(GateResult.from_bool(
        "placement_shadow_review",
        not bool([w for w in placement_shadow_warnings if "TRUE SHADOW" not in w]),
        blocking=False,
        failures=[w for w in placement_shadow_warnings if "TRUE SHADOW" not in w][:6],
        metrics={"review_count": len([w for w in placement_shadow_warnings if "TRUE SHADOW" not in w])},
    ))
    scorecard.add(GateResult.from_bool("national_protocol", len(national_protocol_errors) == 0, failures=national_protocol_errors[:3]))
    scorecard.add(GateResult.from_bool(
        "national_acceptance",
        national_acceptance_ok,
        failures=national_acceptance_errors[:5],
        metrics=national_acceptance_payload.get("summary", {}) if isinstance(national_acceptance_payload, dict) else {},
        artifacts={"report": national_acceptance_payload} if national_acceptance_payload else {},
    ))
    scorecard.add(GateResult.from_bool(
        "decision",
        decision_ok,
        metrics={"pass_rate": round(decision_rate, 4), "total": decision_total},
        artifacts={"skill_layers": decision_skill_layers} if decision_skill_layers else {},
        failures=[str(f)[:300] for f in (decision_detail.get("failures", []) or [])[:5]],
    ))
    scorecard.add(GateResult.from_bool("size", len(oversized) == 0, failures=[f"{n}:{l}/{lim}" for n, l, lim in oversized]))
    scorecard.add(GateResult.from_bool("fix_verification", fix_ok, failures=[f"{fid}: {r.get('reason', '')[:160]}" for fid, r in fix_failed.items()]))
    scorecard.add(GateResult.from_bool("telemetry_fidelity", telemetry_fidelity_ok, failures=telemetry_fidelity_warnings[:6]))
    scorecard.add(GateResult.from_bool("reachability", reachability_ok, failures=reachability_warnings[:6]))
    scorecard.add(GateResult.from_bool("position_semantics", position_semantics_ok, failures=position_semantics_errors[:6]))
    result["scorecard"] = scorecard.model_dump()
    quality_detail["scorecard"] = result["scorecard"]

    log_system_event(
        "pipeline.quality_passed" if all_passed else "pipeline.quality_failed",
        "success" if all_passed else "error",
        f"Quality gates {'passed' if all_passed else 'failed'} for v{v}: {', '.join(failed_gates_detail) or 'all checks passed'}",
        {"version": v, "pass_rate": round(decision_rate, 2), **quality_detail},
    )

    _ckpt = _matching_checkpoint(v, source_v) if source_v is not None else _matching_checkpoint(v)
    if _ckpt:
        source_v = _ckpt["source_v"]
        gate = _gate_payload(
            v,
            source_v,
            all_passed,
            **quality_detail,
        )
        _record_gate(
            v,
            source_v,
            "quality",
            gate,
            stage="quality_passed" if all_passed else "quality_failed",
        )
        result["checkpoint_recorded"] = True
        result["source_v"] = source_v
    else:
        result["checkpoint_recorded"] = False

    if append_candidate_event:
        try:
            append_candidate_event(
                "quality_finished",
                version=v,
                source_v=source_v,
                candidate_id=candidate_id,
                profile_id=workflow_profile.profile_id,
                workflow_profile_id=workflow_profile.profile_id,
                run_id=f"{v}#0",
                stage="quality_passed" if all_passed else "quality_failed",
                parent_ids=[f"claude_v{source_v}"] if source_v is not None else [],
                changed_files=changed_files_list,
                skill_layers=declared_skill_layers,
                diff_hash=diff_hash,
                gate="quality",
                scorecard=scorecard,
                gate_results=scorecard.gates,
                metrics={
                    "all_passed": all_passed,
                    "decision_pass_rate": round(decision_rate, 4),
                    "decision_skill_layers": decision_skill_layers,
                    "national_acceptance_ok": national_acceptance_ok,
                    "declared_scope_ok": declared_scope_ok,
                },
                failures=failed_gates_detail if not all_passed else [],
                failure_class="quality_gate" if not all_passed else "",
                artifacts={"national_acceptance": national_acceptance_payload} if national_acceptance_payload else {},
            )
        except Exception as e:
            _log.warning("candidate ledger quality_finished write failed: %s", e)

    try:
        log_system_event("pipeline.quality_gates", "info",
                         f"Quality gates finished for v{v} in {time.time() - _t0:.1f}s",
                         {"version": v, "all_passed": all_passed, "elapsed_sec": round(time.time() - _t0, 2)})
    except Exception:
        pass

    return _json_tool_result(result)


# ──────────────────────────────────────────────
# Prepare Next Gen
# ──────────────────────────────────────────────

@tool("prepare_next_gen", "Prepare the next generation directory by copying from source bot.", {"source_v": int, "next_v": int})
async def prepare_next_gen(args):
    _t0 = time.time()
    source_v = args.get("source_v")
    next_v = args.get("next_v")
    if source_v is None or next_v is None:
        _v, source_v = _resolve_version_args(args)
        next_v = next_v or _v
    if source_v is None or next_v is None:
        return _json_tool_result({"error": "Missing source_v/next_v and no active checkpoint"})

    _set_pipeline_status(f"Preparing v{next_v}")

    if next_v <= source_v:
        return _json_tool_result({"error": f"next_v ({next_v}) must be greater than source_v ({source_v})"})

    # Guard against clearly invalid version numbers (test artifacts)
    if next_v >= 900:
        return _json_tool_result({"error": f"next_v ({next_v}) is invalid. Version numbers must be < 900."})

    current_v = find_current_v()
    if next_v > current_v + 10:
        return _json_tool_result({"error": f"next_v ({next_v}) is too far ahead of current_v ({current_v}). Use next_v = {current_v + 1}."})

    source_dir = get_bot_dir(source_v)
    next_dir = get_bot_dir(next_v)

    if not source_dir.exists():
        return _json_tool_result({"error": f"Source bot v{source_v} not found"})

    # Guard: warn if source bot is not completed (may be broken)
    if not (source_dir / ".completed").exists():
        return _json_tool_result({"error": f"Source bot v{source_v} is not marked completed. Cannot use incomplete code as source."})

    # Guard: verify git tag exists for source bot (authoritative commit proof)
    from evolution_infra import git_has_tag, git_dir_is_committed
    if not git_has_tag(source_v):
        return _json_tool_result({"error": f"Source bot v{source_v} has .completed but no git tag 'bot-v{source_v}'. Cannot evolve from uncommitted code. Try a different source version."})

    # Guard: refuse to overwrite a completed bot
    if next_dir.exists() and (next_dir / ".completed").exists():
        return _json_tool_result({"error": f"Target v{next_v} already exists and is completed. Refusing to overwrite."})

    # Guard: refuse to overwrite a bare-committed target (root-cause fix for the
    # v117 repeated-regeneration loop, 2026-06-18; mirrors run_crossover).
    if next_dir.exists() and git_dir_is_committed(next_v) and not git_has_tag(next_v):
        return _json_tool_result({
            "error": f"Target v{next_v} is git-committed but has no bot-v{next_v} tag (bare commit bypassing commit_bot). "
                     f"Refusing to overwrite — re-preparing here causes infinite regeneration. "
                     f"Run commit_bot for v{next_v} to finalize it, or abandon/clear the untagged dir first."
        })

    # Guard: refuse to re-prepare if pipeline has already progressed past "prepared"
    _ckpt = _matching_checkpoint(next_v, source_v)
    if _ckpt and _ckpt.get("stage") not in (None, "selected", "preparing", "prepared", "timed_out"):
        return _json_tool_result({"error": f"Pipeline for v{next_v} already at stage '{_ckpt['stage']}'. Refusing to overwrite worker output. Call abandon_generation first if you want to restart."})

    from evolution_infra import write_pipeline_checkpoint
    if not write_pipeline_checkpoint(next_v, source_v, "preparing", worker_failure_count=0):
        return _json_tool_result({
            "error": f"Failed to persist preparing checkpoint for v{next_v}; refusing to mutate bot directory."
        })

    if next_dir.exists():
        # Guard against silent cross-source overwrite. v107 (2026-06-16) was
        # repeatedly re-prepared from DIFFERENT ancestors (106/105/102) because
        # each crashed cycle reset the checkpoint, _matching_checkpoint(next_v,
        # source_v) returned None for the new source, the stage guard was
        # bypassed, and this rmtree silently destroyed the previous attempt's
        # worker output. Refuse unless the existing dir was prepared from the
        # SAME source (a legitimate same-generation retry) or explicitly cleared.
        prior_ckpt = read_pipeline_checkpoint() or {}
        prior_source = prior_ckpt.get("source_v")
        if prior_source is not None and prior_source != source_v:
            log_system_event(
                "pipeline.prepare_cross_source_refused", "error",
                f"Refusing to overwrite v{next_v}: dir was prepared from "
                f"v{prior_source} but request is from v{source_v}. "
                f"Call abandon_generation first to clear it.",
                {"version": next_v, "source_v": source_v, "prior_source_v": prior_source},
            )
            return _json_tool_result({"error": f"Target v{next_v} already exists, prepared from v{prior_source} (not v{source_v}). Refusing silent cross-source overwrite. Call abandon_generation first."})
        shutil.rmtree(next_dir)
    shutil.copytree(source_dir, next_dir, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))

    # Apply known critical fixes regardless of source bot state
    from fix_injection import apply_known_fixes, log_fix_application
    applied, skipped = apply_known_fixes(next_dir)
    if applied or skipped:
        log_fix_application(applied, skipped, next_dir, source_v)
    if skipped:
        _log.info("Fix patches skipped for v%d: %s", next_v, skipped)

    (next_dir / ".completed").unlink(missing_ok=True)

    # Write "prepared" checkpoint so a kill+restart shows "Workers not yet run → call run_direction_audit"
    if not write_pipeline_checkpoint(next_v, source_v, "prepared", worker_failure_count=0):
        return _json_tool_result({
            "error": f"Failed to persist prepared checkpoint for v{next_v}; generation recovery remains at preparing."
        })

    log_system_event("pipeline.prepare_done", "info", f"Prepared v{next_v} from v{source_v}",
                     {"next_v": next_v, "source_v": source_v, "elapsed_sec": round(time.time() - _t0, 2)})
    try:
        from repo_state import log_git_worktree_snapshot
        log_git_worktree_snapshot(
            "repo.worktree_snapshot",
            f"Worktree snapshot after preparing v{next_v}",
            next_v=next_v,
            source_v=source_v,
            stage="prepared",
            emit_delta=True,
        )
    except Exception:
        pass

    return _json_tool_result({"prepared": True, "next_v": next_v, "source_v": source_v})


# ──────────────────────────────────────────────
# Review Stage
# ──────────────────────────────────────────────

@tool("run_review", "Run Lead Code Reviewer on the bot changes. Returns approval decision with quality score.", {"version": int, "source_v": int, "plan": list})
async def run_review(args):
    _t0 = time.time()
    v, source_v = _resolve_version_args(args)
    if v is None or source_v is None:
        return _json_tool_result({"error": "Missing version/source_v and no active pipeline checkpoint"})
    v = int(v)
    source_v = int(source_v)
    plan = args.get("plan", [])

    _set_pipeline_status(f"Reviewing v{v}")

    # Idempotency guard: skip if review already approved
    _cached = _idempotency_check(
        v, source_v,
        stage_set=("reviewed", "critic_checked", "verified", "archived"),
        gate_name="review",
        directive="Review ALREADY PASSED. Call run_critic next.",
    )
    if _cached:
        return _cached

    ckpt = _matching_checkpoint(v, source_v)
    if not _quality_gate_ok(ckpt):
        return _state_blocked(
            "run_review requires run_quality_gates all_passed=true and critical_scenarios_passed=true for the same version/source_v.",
            v,
            source_v,
            ckpt,
        )

    prompts_dir = PROJECT_ROOT / "web" / "core" / "prompts"
    reviewer_prompt = (prompts_dir / "reviewer_prompt.md").read_text()
    reviewer_prompt = reviewer_prompt.replace("{master_plan}", json.dumps(plan, indent=2))
    reviewer_prompt = reviewer_prompt.replace("{version}", str(v))
    reviewer_prompt = reviewer_prompt.replace("{parent_version}", str(source_v))

    # Inject Worker CoT audit_focus_areas into reviewer prompt
    _review_ckpt = _matching_checkpoint(v, source_v)
    if _review_ckpt:
        _audit_context = _review_ckpt.get("audit_context", {}) or {}
        _focus_areas = _audit_context.get("worker_cot_focus_areas", [])
        if not _focus_areas:
            # Also check gate_results for audit_focus_areas stored by execute_workers
            _worker_gate = _review_ckpt.get("gate_results", {}).get("workers", {})
            _focus_areas = _worker_gate.get("audit_focus_areas", [])
        if _focus_areas:
            _focus_block = (
                "\n\n# Worker CoT Audit Findings (from execute_workers)\n"
                "The Worker Chain-of-Thought audit detected these concerns.\n"
                "Pay EXTRA attention to these areas during your review:\n"
            )
            for _fa in _focus_areas:
                _focus_block += f"- {_fa}\n"
            _focus_block += "\n"
            reviewer_prompt += _focus_block

    log_file = get_logs_dir(v) / "reviewer_io.txt"

    ui = _get_ui()
    try:
        output, _, _ = await run_claude_query(
            reviewer_prompt, [], ui, "LEAD CODE REVIEWER", log_file, tools=["Bash", "Read"]
        )
    except Exception as e:
        # ── LLM infrastructure error short-circuit ──
        # If the Reviewer LLM call crashed (SDK error / timeout / connection), do NOT
        # treat it as an approved:False rejection (which would block the pipeline).
        # Retry the review gate (not the workers), and soft-abandon after 3 attempts
        # while keeping stage=quality_passed so the orchestrator re-calls run_review.
        # No generation_attempt increment, no quality-failure record, no rejection gate.
        if is_llm_infra_error(e):
            prev = ckpt.get("gate_results", {}).get("review", {}).get("review_infra_retry", 0) if ckpt else 0
            infra_count = prev + 1
            _record_gate(
                v, source_v, "review",
                {"llm_failed": True, "approved": False,
                 "review_infra_retry": 0 if infra_count >= 3 else infra_count,  # reset on abandon
                 "error": str(e)},
                stage=None,                                    # keep current stage (quality_passed)
                master_plan=ckpt.get("master_plan") if ckpt else plan,
                reviewer_feedback=f"Reviewer LLM infra error: {e}",
                generation_attempt=ckpt.get("generation_attempt", 0),  # do NOT increment
            )
            try:
                log_system_event(
                    "pipeline.review_infra_error", "warn",
                    f"Reviewer v{v} LLM crashed (infra) attempt {infra_count}/3",
                    {"version": v, "infra_retry": infra_count},
                )
            except Exception:
                pass
            ui.log_history(f"Reviewer LLM infrastructure error (NOT a rejection): {e}", "warn")
            if infra_count >= 3:
                return _json_tool_result({"action": "abandon_cycle",
                    "directive": (f"Reviewer LLM crashed {infra_count}x (infrastructure, NOT a code rejection). "
                                  f"Soft-abandon: stage stays 'quality_passed', next cycle resumes v{v} at run_review. "
                                  f"Do NOT retry_workers or run_master. End this cycle."),
                    "llm_failed": True})
            return _json_tool_result({"action": "retry_review",
                "directive": (f"Reviewer LLM crashed (infra, NOT a code rejection). Call run_review AGAIN "
                              f"(attempt {infra_count}/3). Do NOT retry_workers or run_master."),
                "llm_failed": True})
        ui.log_history(f"Reviewer error: {e}. Defaulting to rejected.", "warn")
        output = None
    from llm_query import parse_json_output_with_mode
    data, _review_mode = parse_json_output_with_mode(output)

    if data and "approved" in data:
        approved = data["approved"] is True
        feedback = data.get("feedback", "")
        try:
            log_system_event(
                "pipeline.review_passed" if approved else "pipeline.review_rejected",
                "success" if approved else "warn",
                f"Review {'approved' if approved else 'rejected'} v{v} (score={data.get('quality_score', 0)})",
                {"version": v, "score": data.get("quality_score", 0), "approved": approved},
            )
        except Exception:
            pass
        gate = _gate_payload(
            v,
            source_v,
            approved,
            approved=approved,
            quality_score=data.get("quality_score", 0),
            feedback=feedback,
            change_summary=data.get("change_summary", ""),
            risk_areas=data.get("risk_areas", []),
        )
        checkpoint_recorded = _record_gate(
            v,
            source_v,
            "review",
            gate,
            stage="reviewed" if approved else None,
            master_plan=ckpt.get("master_plan") if ckpt else plan,
            reviewer_feedback=feedback,
        )
        if not approved:
            _record_quality_failure(v, "reviewer", "Code Reviewer",
                                    f"Rejected (score={data.get('quality_score', 0)}): {feedback[:2000]}")
        result = {
            "approved": approved,
            "quality_score": data.get("quality_score", 0),
            "change_summary": data.get("change_summary", ""),
            "risk_areas": data.get("risk_areas", []),
            "feedback": feedback,
            "checkpoint_recorded": checkpoint_recorded,
            "logs": ui.get_output(),
        }
    else:
        error_msg = (
            "Reviewer returned valid JSON but missing 'approved' field"
            if data and isinstance(data, dict)
            else f"Reviewer failed to produce valid JSON (mode={_review_mode})"
        )
        gate = _gate_payload(
            v,
            source_v,
            False,
            approved=False,
            error=error_msg,
            raw_output=output[:500] if output else "",
        )
        checkpoint_recorded = _record_gate(
            v,
            source_v,
            "review",
            gate,
            stage=None,
            master_plan=ckpt.get("master_plan") if ckpt else plan,
            reviewer_feedback=error_msg,
        )
        result = {
            "approved": False,
            "error": error_msg,
            "raw_output": output[:500] if output else "",
            "checkpoint_recorded": checkpoint_recorded,
            "logs": ui.get_output(),
        }

    try:
        log_system_event("pipeline.review_done", "info",
                         f"Review finished for v{v} in {time.time() - _t0:.1f}s",
                         {"version": v, "approved": result.get("approved", False),
                          "score": result.get("quality_score", 0), "elapsed_sec": round(time.time() - _t0, 2)})
    except Exception:
        pass

    return _json_tool_result(result)


# ──────────────────────────────────────────────
# Critic Stage
# ──────────────────────────────────────────────

@tool("run_critic", "Run Poker Strategy Critic on bot changes. Returns score 1-10 and strategic feedback. ADVISORY ONLY: precommit is the final regression gate; score does NOT block the pipeline.", {"version": int, "source_v": int, "plan": list, "reviewer_feedback": str, "force_advance": bool})
async def run_critic(args):
    _t0 = time.time()
    v, source_v = _resolve_version_args(args)
    if v is None or source_v is None:
        return _json_tool_result({"error": "Missing version/source_v and no active pipeline checkpoint"})
    v = int(v)
    source_v = int(source_v)
    plan = args.get("plan", [])
    reviewer_feedback = args.get("reviewer_feedback", "")
    force_advance = args.get("force_advance", False)

    _set_pipeline_status(f"Critic evaluating v{v}")

    # Idempotency guard: skip if critic already approved
    _cached = _idempotency_check(
        v, source_v,
        stage_set=("critic_checked", "verified", "archived"),
        gate_name="critic",
        extra_ok_keys=("force_advanced",),
        directive="Critic ALREADY PASSED. Call run_precommit_eval next.",
    )
    if _cached:
        return _cached

    ckpt = _matching_checkpoint(v, source_v)
    if not _quality_gate_ok(ckpt) or not _review_gate_ok(ckpt):
        return _state_blocked(
            "run_critic requires passing quality gates and reviewer approval for the same version/source_v.",
            v,
            source_v,
            ckpt,
        )

    master_plan_str = json.dumps(plan, indent=2)
    prev_critic = ckpt.get("gate_results", {}).get("critic", {}).get("prev_critic") if ckpt else None
    ui = _get_ui()
    data = await _run_critic(v, source_v, master_plan_str, ui, prev_critic_result=prev_critic)

    # ── LLM infrastructure error short-circuit ──
    # If the Critic LLM call crashed (NOT a strategic rejection), do NOT treat it
    # as a score=0 rejection. Retry the critic gate (not the workers), and soft-
    # abandon after 3 attempts while keeping stage=reviewed so the next cycle
    # resumes here. No generation_attempt increment, no quality-failure record,
    # no guardian trigger, no critic_rejected log.
    if isinstance(data, dict) and data.get("llm_failed"):
        prev = ckpt.get("gate_results", {}).get("critic", {}).get("critic_infra_retry", 0) if ckpt else 0
        infra_count = prev + 1
        _record_gate(
            v, source_v, "critic",
            {"llm_failed": True, "approved": False,
             "critic_infra_retry": 0 if infra_count >= 3 else infra_count,  # reset on abandon
             "error": data.get("error", "")},
            stage=None,                                    # keep current stage (reviewed)
            master_plan=ckpt.get("master_plan") if ckpt else plan,
            reviewer_feedback=reviewer_feedback,
            generation_attempt=ckpt.get("generation_attempt", 0),  # do NOT increment
        )
        try:
            log_system_event(
                "pipeline.critic_infra_error", "warn",
                f"Critic v{v} LLM crashed (infra) attempt {infra_count}/3",
                {"version": v, "infra_retry": infra_count},
            )
        except Exception:
            pass
        if infra_count >= 3:
            return _json_tool_result({"action": "abandon_cycle",
                "directive": (f"Critic LLM crashed {infra_count}x (infrastructure, NOT strategy). "
                              f"Soft-abandon: stage stays 'reviewed', next cycle resumes v{v} at run_critic. "
                              f"Do NOT retry_workers or run_master. End this cycle."),
                "llm_failed": True})
        return _json_tool_result({"action": "retry_critic",
            "directive": (f"Critic LLM crashed (infra, NOT strategy). Call run_critic AGAIN "
                          f"(attempt {infra_count}/3). Do NOT retry_workers or run_master."),
            "llm_failed": True})

    if not isinstance(data, dict):
        data = {}
    score = data.get("score", 0)
    try:
        score_num = float(score)
    except (TypeError, ValueError):
        score_num = 0.0
    raw_approved = data.get("approved", score_num >= 6)
    # Critic is now ADVISORY — final approve/reject is decided by precommit
    # (Step 2's paired-bootstrap statistical gate). score and feedback still
    # surface to workers as improvement hints, but do NOT block the pipeline.
    advisory_approved = bool(raw_approved) and score_num >= 6  # for telemetry/logging
    approved = True  # advisory: precommit statistical gate (Step 2) is the final judge
    # In advisory mode approved is always True, so force_advanced follows
    # force_advance directly (kept for backward-compat with downstream gates).
    force_advanced = bool(force_advance)
    gate = _gate_payload(
        v,
        source_v,
        approved,
        approved=approved,
        raw_approved=raw_approved,
        advisory_approved=advisory_approved,
        advisory_score=score_num,
        score=score_num,
        feedback=data.get("feedback", ""),
        strategic_assessment=data.get("strategic_assessment", ""),
        local_optima_warning=data.get("local_optima_warning", False),
        force_advanced=force_advanced,
    )

    # Track intra-gen retry count: increment when critic rejects (retry_workers).
    # ADVISORY-ONLY: critic no longer blocks, so we never bump current_attempt or
    # emit retry_workers here. Keep the read for downstream telemetry only.
    current_attempt = (ckpt.get("generation_attempt", 0) or 0) if ckpt else 0

    checkpoint_recorded = _record_gate(
        v,
        source_v,
        "critic",
        gate,
        stage="critic_checked",  # always advance: critic is advisory, precommit is final judge
        master_plan=ckpt.get("master_plan") if ckpt else plan,
        reviewer_feedback=reviewer_feedback,
        generation_attempt=current_attempt,
    )
    guardian_diagnosis = None
    if not advisory_approved:
        # Telemetry only: record critic rejection diagnostics so they surface to
        # the next worker prompt as improvement hints. Does NOT block the pipeline.
        _record_quality_failure(v, "critic", "Strategy Critic",
                                f"Rejected (score={score_num}): {data.get('feedback', '')[:2000]}",
                                local_optima_warning=data.get("local_optima_warning", False),
                                local_optima_reason=data.get("local_optima_reason"))
        # Meta-2: Trigger Regression Guardian on very low critic score.
        # Run synchronously so the diagnosis is visible to the Orchestrator
        # (merged into the tool result below). This is advisory only — it is
        # NOT a hard second gate; precommit remains the final judge.
        # _run_regression_guardian has a safe_default so it never throws.
        if score_num < 4:
            try:
                import audit_agents as _aa
                _c = _matching_checkpoint(v, source_v)
                _history = {
                    "score": score_num,
                    "feedback": data.get("feedback", "")[:500],
                    "strategic_assessment": data.get("strategic_assessment", "")[:500],
                    "master_plan": _c.get("master_plan", {}) if _c else {},
                    "gate_results": _c.get("gate_results", {}) if _c else {},
                }
                guardian_diagnosis = await _aa._run_regression_guardian(
                    v, source_v, _history,
                    f"Critic score {score_num} < 4: {data.get('feedback', '')[:200]}",
                    ui,
                )
            except Exception as e:
                _log.warning("Regression guardian dispatch failed for v%s: %s", v, e)

    try:
        # LOG GAP FIX (2026-06-30): enrich critic event with feedback/reasoning so
        # the reject rationale is visible in the event stream (not just worker_failures.jsonl).
        _critic_payload = {"version": v, "score": score_num, "approved": approved,
                           "advisory_approved": advisory_approved}
        if not advisory_approved:
            _critic_payload["feedback"] = str(data.get("feedback", ""))[:500] if isinstance(data, dict) else ""
            _critic_payload["local_optima_warning"] = data.get("local_optima_warning") if isinstance(data, dict) else None
            _critic_payload["strategic_assessment"] = str(data.get("strategic_assessment", ""))[:300] if isinstance(data, dict) else ""
        log_system_event(
            "pipeline.critic_passed" if advisory_approved else "pipeline.critic_rejected",
            "success" if advisory_approved else "warn",
            f"Critic {'approved' if advisory_approved else 'rejected (advisory)'} v{v} (score={score_num})",
            _critic_payload,
        )
        # 4b: when critic rejects but is advisory-only (approved stays True), record
        # the explicit "reject but proceed" decision so it's not mistaken for a bug.
        if not advisory_approved and approved:
            log_system_event(
                "pipeline.critic_advisory_skip", "info",
                f"Critic rejected v{v} (score={score_num}) but advisory-only — proceeding "
                f"to precommit (the final regression gate)",
                {"version": v, "score": score_num,
                 "guardian_triggered": score_num < 4},
            )
    except Exception:
        pass

    # Extract Critic evidence and append to experience pool
    evidence = data.get("evidence") if isinstance(data, dict) else None
    if evidence:
        try:
            from tool_commit import _append_experience_updates
            ev_parts = []
            h2h_w = evidence.get("h2h_weaknesses", [])
            if h2h_w:
                ev_parts.append(f"H2H weaknesses: {', '.join(str(w) for w in h2h_w[:5])}")
            ep_refs = evidence.get("experience_pool_refs", [])
            if ep_refs:
                ev_parts.append(f"Experience pool refs: {', '.join(str(r) for r in ep_refs[:3])}")
            diff_refs = evidence.get("diff_refs", [])
            if diff_refs:
                ev_parts.append(f"Diff refs: {', '.join(str(r) for r in diff_refs[:3])}")
            if ev_parts:
                evidence_summary = "; ".join(ev_parts)
                _append_experience_updates(
                    version=v,
                    updates=[f"Critic evidence: {evidence_summary}"],
                    strategic_advice="",
                    generation_assessment="info",
                )
        except Exception:
            pass  # Non-critical: evidence write failure should not block pipeline

    # fix-8: check for fabricated replay citations in critic output
    critic_citation_errors = []
    try:
        from tool_planning import _check_citations, _load_replay_anchor_map
        if evidence:
            critic_texts = evidence.get("h2h_weaknesses", []) + evidence.get("diff_refs", [])
            critic_citation_errors = _check_citations(
                [str(t) for t in critic_texts], _load_replay_anchor_map()
            )
        if critic_citation_errors:
            log_system_event("fabricated_citation", "warn",
                             f"Critic cited {len(critic_citation_errors)} fabricated replay(s)",
                             {"version": v, "errors": critic_citation_errors})
    except Exception:
        pass  # Non-critical: citation check should not block pipeline

    result = {
        **data,
        "approved": approved,
        "raw_approved": raw_approved,
        "score": score_num,
        "advisory_score": score_num,
        "advisory_approved": advisory_approved,
        "citation_penalties": len(critic_citation_errors),
        "logs": ui.get_output(),
        "action": "approve",  # advisory: orchestrator proceeds to run_precommit_eval (final judge)
        "force_advanced": force_advanced,
        "checkpoint_recorded": checkpoint_recorded,
    }
    if guardian_diagnosis:
        result["regression_guardian"] = {
            "severity": guardian_diagnosis.get("severity", "minor"),
            "failure_stage": guardian_diagnosis.get("failure_stage", "unknown"),
            "recovery_recommendation": guardian_diagnosis.get("recovery_recommendation", ""),
            "diagnosis": guardian_diagnosis.get("diagnosis", ""),
            "root_cause": guardian_diagnosis.get("root_cause", ""),
            "confidence": guardian_diagnosis.get("confidence", "low"),
        }
        # fix-9: surface guardian_diagnosis to experience_pool for next-gen Master.
        _guardian_text = guardian_diagnosis.get("diagnosis", "")
        if _guardian_text:
            try:
                log_system_event("regression_guardian", "info",
                                 f"Critic score<4 guardian diagnosis: {_guardian_text[:200]}",
                                 {"version": v, "source_v": source_v, "score": score_num})
            except Exception:
                pass
            try:
                import evolution_infra as _ei
                _rd = _ei.RESULTS_DIR
                os.makedirs(_rd, exist_ok=True)
                _gf_path = _rd / "regression_guardian.jsonl"
                _entry = json.dumps({
                    "version": v, "source_v": source_v, "score": score_num,
                    "diagnosis": _guardian_text,
                    "root_cause": guardian_diagnosis.get("root_cause", ""),
                    "severity": guardian_diagnosis.get("severity", "minor"),
                    "recovery_recommendation": guardian_diagnosis.get("recovery_recommendation", ""),
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }, ensure_ascii=False)
                import fcntl as _fl
                with open(_gf_path, "a", encoding="utf-8") as _fh:
                    _fl.flock(_fh, _fl.LOCK_EX)
                    _fh.write(_entry + "\n")
                    _fl.flock(_fh, _fl.LOCK_UN)
            except Exception:
                pass
        result["fabricated_citations"] = critic_citation_errors
    try:
        log_system_event("pipeline.critic_done", "info",
                         f"Critic finished for v{v} in {time.time() - _t0:.1f}s",
                         {"version": v, "approved": approved, "score": score_num,
                          "elapsed_sec": round(time.time() - _t0, 2)})
    except Exception:
        pass
    return _json_tool_result(result)


# ──────────────────────────────────────────────
# Spot Check Stage
# ──────────────────────────────────────────────

@tool("run_spot_check", "Run spot check on changed functions: parse diff, generate scenarios, run bot, verify behavior.", {"parent_version": int, "current_version": int, "master_plan": dict})
async def run_spot_check(args):
    parent_version = args.get("parent_version")
    current_version = args.get("current_version")
    master_plan = args.get("master_plan", {})

    if parent_version is None or current_version is None:
        return _json_tool_result({"error": "Missing parent_version or current_version"})

    parent_dir = str(get_bot_dir(int(parent_version)))
    current_dir = str(get_bot_dir(int(current_version)))

    changed_functions = spot_analyzer.parse_diff(parent_dir, current_dir)

    bot_code = {}
    for change in changed_functions:
        fp = change.get("file")
        if fp and Path(fp).exists():
            bot_code[fp] = Path(fp).read_text()

    scenarios = spot_analyzer.generate_test_scenarios(changed_functions, bot_code)

    bot_main = Path(current_dir) / "main.py"
    actual_actions = []
    for scenario in scenarios:
        result = spot_analyzer.run_bot_scenario(str(bot_main), scenario)
        actual_actions.append(result)

    verification = spot_analyzer.verify_behavior(master_plan, scenarios, actual_actions)

    result = {
        "status": "success",
        "result": {
            "passed": verification.get("passed", False),
            "assessment": f"Spot check {verification.get('passed_count', 0)}/{verification.get('total', 0)} passed, confidence={verification.get('confidence', 'unknown')}",
            "details": verification,
            "changed_functions": changed_functions,
            "scenarios_count": len(scenarios),
        },
    }
    return _json_tool_result(result)
