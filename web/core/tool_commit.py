"""Pipeline tools: commit, archivist, and crossover."""

import json
import logging
import time
from pathlib import Path
from typing import Annotated, TypedDict

_log = logging.getLogger("pok.commit")

from claude_agent_sdk import tool

from evolution_core import (
    get_bot_dir,
    get_active_bots,
    load_ratings,
    git_commit_bot,
    git_has_tag,
    clear_pipeline_checkpoint,
    RESULTS_DIR,
    MAX_ACTIVE_BOTS,
    _run_crossover,
)
from tool_helpers import (
    _get_ui, _json_tool_result,
    _matching_checkpoint, _resolve_version_args,
    PROJECT_ROOT,
)
from system_log import log_system_event


# ──────────────────────────────────────────────
# Commit Stage
# ──────────────────────────────────────────────

class CommitBotInput(TypedDict):
    version: Annotated[int, "Bot version to commit"]
    source_v: Annotated[int, "Parent version"]
    strategy: Annotated[str, "Strategy description"]
    review_approved: Annotated[bool, "Must be true — confirms run_review() returned approved:true"]


@tool("commit_bot", "Commit a bot generation with git commit and tag. review_approved must be true (set after run_review returns approved:true).", {"version": int, "source_v": int, "strategy": str, "review_approved": bool})
async def commit_bot(args):
    v, source_v = _resolve_version_args(args)
    if v is None or source_v is None:
        return _json_tool_result({"error": "Missing version/source_v and no active pipeline checkpoint"})
    v = int(v)
    source_v = int(source_v)
    strategy = args.get("strategy", "")
    review_approved = args.get("review_approved", False)

    bot_dir = get_bot_dir(v)

    ckpt = _matching_checkpoint(v, source_v)
    missing_gates = []
    failed_gates = []
    gate_results = {}
    if not ckpt:
        missing_gates.append("pipeline_checkpoint")
    else:
        gate_results = ckpt.get("gate_results", {}) or {}

        quality = gate_results.get("quality")
        if not quality:
            missing_gates.append("quality")
        else:
            if quality.get("all_passed") is not True:
                failed_gates.append({"gate": "quality", "reason": "all_passed is not true", "value": quality})
            if quality.get("critical_scenarios_passed") is not True:
                failed_gates.append({"gate": "quality", "reason": "critical_scenarios_passed is not true", "value": quality})

        review = gate_results.get("review")
        if not review:
            missing_gates.append("review")
        elif review.get("approved") is not True:
            failed_gates.append({"gate": "review", "reason": "reviewer did not approve", "value": review})

        critic = gate_results.get("critic")
        if not critic:
            missing_gates.append("critic")
        else:
            try:
                critic_score = float(critic.get("score", 0))
            except (TypeError, ValueError):
                critic_score = 0.0
            if critic.get("approved") is not True or critic_score < 6:
                if critic.get("force_advanced") is True:
                    pass  # Force-advanced: allow despite low score
                else:
                    failed_gates.append({
                        "gate": "critic",
                        "reason": "critic score must be >= 6 and approved must be true",
                        "value": critic,
                    })

        precommit = gate_results.get("precommit_eval")
        if not precommit:
            missing_gates.append("precommit_eval")
        elif precommit.get("passed") is not True:
            failed_gates.append({"gate": "precommit_eval", "reason": "precommit eval did not pass", "value": precommit})

    if missing_gates or failed_gates:
        return _json_tool_result({
            "error": "COMMIT BLOCKED: gate ledger incomplete or failed.",
            "version": v,
            "source_v": source_v,
            "checkpoint_stage": ckpt.get("stage") if ckpt else None,
            "missing_gates": missing_gates,
            "failed_gates": failed_gates,
            "gate_results": gate_results,
        })

    # Guard: quality gates already verified in checkpoint — no need to re-run
    # (compile, smoke, decision, size all checked in run_quality_gates)
    quality = gate_results.get("quality")
    if not quality or not quality.get("all_passed"):
        return _json_tool_result({
            "error": "COMMIT BLOCKED: quality gates not passed in checkpoint.",
            "gate_summary": {k: {"passed": v.get("passed")} for k, v in gate_results.items()},
        })

    # Guard: reviewer approval required
    if not review_approved:
        return _json_tool_result({
            "error": "COMMIT BLOCKED: review_approved=false. Call run_review() first; only pass review_approved=true if it returns approved:true.",
        })

    ratings = load_ratings()
    p = ratings.get(f"claude_v{v}")
    h2h_wr = None
    try:
        from tool_helpers import compute_h2h_avg_winrate, _load_h2h_data
        h2h_wr = compute_h2h_avg_winrate(f"claude_v{v}", _load_h2h_data())
    except Exception as e:
        _log.warning("H2H win rate computation failed for v%d: %s", v, e)
    wr_str = f" h2h_avg_wr={h2h_wr:.2%}" if h2h_wr is not None else ""
    rating_info = f"rating: r={p.r:.1f} rd={p.rd:.1f}{wr_str}" if p else ""

    parent2_v = ckpt.get("parent2_v") if ckpt else None
    push_ok = git_commit_bot(v, source_v, strategy, rating_info=rating_info, parent2_v=parent2_v)

    # Verify tag was created
    if not git_has_tag(v):
        return _json_tool_result({
            "error": f"Git tag bot-v{v} not found after commit. Git operations may have failed.",
            "version": v,
        })

    (bot_dir / ".completed").touch()

    log_system_event("pipeline.committed", "success", f"Committed v{v} from v{source_v}: {strategy[:80]}",
                     {"version": v, "source_v": source_v, "strategy": strategy[:100]})

    # Archive this generation's state snapshot
    try:
        from evolution_infra import archive_generation, archive_rotate_files, archive_old_logs
        archive_generation(v, source_v, ckpt)
        archive_rotate_files(v)
        archive_old_logs()
    except Exception as e:
        _log.warning("Archive generation failed for v%d: %s", v, e)

    clear_pipeline_checkpoint()

    try:
        from server.state import app_state
        app_state.set_generation(v, v + 1)
    except Exception as e:
        _log.warning("App state update failed for v%d: %s", v, e)

    # Signal daemon to pick up the new bot
    reap_signal = RESULTS_DIR / ".reap_signal"
    reap_signal.write_text(str(time.time()))

    # Write priority eval signal so daemon schedules this bot heavily
    priority_file = Path(__file__).parent / "results" / "priority_eval.json"
    try:
        from evolution_infra import locked_file
        with locked_file(priority_file, "w") as f:
            json.dump({"bot": f"claude_v{v}", "min_games": 100, "since": time.time()}, f)
    except Exception as e:
        _log.warning("Priority eval signal write failed for v%d: %s", v, e)

    result = {"committed": True, "version": v, "source_v": source_v, "push_ok": push_ok}
    active_bots = get_active_bots()
    if len(active_bots) > MAX_ACTIVE_BOTS:
        result["needs_reap"] = True
        result["pool_size"] = len(active_bots)
    return _json_tool_result(result)


# ──────────────────────────────────────────────
# Archivist Stage
# ──────────────────────────────────────────────

def _append_experience_updates(version: int, updates: list[str],
                                strategic_advice: str = "", generation_assessment: str = ""):
    """Append archivist experience_updates, strategic_advice, and assessment to experience_pool.md."""
    from evolution_infra import EXPERIENCE_FILE, locked_file

    # Build the lines to insert
    new_lines = [f"- **v{version}**: {u}" for u in updates if u.strip()]

    # Add strategic_advice as a separate line so Master sees it
    if strategic_advice and strategic_advice.strip():
        label = f" ({generation_assessment})" if generation_assessment and generation_assessment != "neutral" else ""
        new_lines.append(f"- **v{version} 归档建议{label}**: {strategic_advice.strip()}")

    if not new_lines:
        return

    with locked_file(EXPERIENCE_FILE, "r") as f:
        content = f.read()

    lines = content.split("\n")

    # Find the RECENT_LESSONS section and append after it
    recent_idx = None
    for i, line in enumerate(lines):
        if line.strip() == "## RECENT_LESSONS":
            recent_idx = i
            break

    if recent_idx is not None:
        # Insert after the ## RECENT_LESSONS header
        insert_at = recent_idx + 1
        for j, new_line in enumerate(new_lines):
            lines.insert(insert_at + j, new_line)
    else:
        # Fallback: append at end
        lines.append("")
        lines.append("## RECENT_LESSONS")
        lines.extend(new_lines)

    with locked_file(EXPERIENCE_FILE, "w") as f:
        f.write("\n".join(lines) + "\n")


@tool("run_archivist", "Run post-commit archive audit for a completed generation. Verifies consistency, auto-reaps if needed, calls LLM for strategic assessment and experience pool updates.", {"version": int, "source_v": int})
async def run_archivist(args):
    v, source_v = _resolve_version_args(args)
    if v is None or source_v is None:
        return _json_tool_result({"error": "Missing version/source_v and no active pipeline checkpoint"})
    v = int(v)
    source_v = int(source_v)
    ui = _get_ui()

    # 1. Verify post-commit consistency
    bot_dir = get_bot_dir(v)
    consistency_issues = []
    if not (bot_dir / ".completed").exists():
        consistency_issues.append(f".completed missing for v{v}")
    if not git_has_tag(v):
        consistency_issues.append(f"git tag bot-v{v} missing")
    ratings = load_ratings()
    if f"claude_v{v}" not in ratings:
        consistency_issues.append(f"v{v} not in glicko_ratings.json")

    # 2. Auto-reap if pool exceeds limit
    reap_result = None
    active_bots = get_active_bots()
    if len(active_bots) > MAX_ACTIVE_BOTS:
        try:
            from tool_bot_management import _do_reap_weakest
            reap_result = await _do_reap_weakest()
        except Exception as e:
            reap_result = {"error": str(e)}

    # 3. Load archive snapshot for LLM context
    from evolution_infra import ARCHIVE_DIR
    archive_path = ARCHIVE_DIR / f"v{v}.json"
    snapshot = {}
    if archive_path.exists():
        try:
            with open(archive_path, "r") as f:
                snapshot = json.load(f)
        except Exception:
            pass

    # Inject reviewer context into snapshot — prefer archive data (checkpoint is cleared by commit_bot)
    review_info = ""
    reviewer_context = snapshot.get("reviewer_context", "")
    if reviewer_context:
        review_info = reviewer_context
    else:
        # Fallback: try checkpoint (only works if run_archivist is called before commit clears it)
        try:
            from tool_helpers import read_pipeline_checkpoint
            ckpt = read_pipeline_checkpoint()
            if ckpt:
                review_gate = ckpt.get("gate_results", {}).get("review", {})
                cs = review_gate.get("change_summary", "")
                ra = review_gate.get("risk_areas", [])
                if cs:
                    review_info += f"\nReviewer Change Summary: {cs}"
                if ra:
                    review_info += f"\nReviewer Risk Areas: {', '.join(ra) if isinstance(ra, list) else str(ra)}"
        except Exception:
            pass

    # Also extract reviewer info from archive snapshot fields
    if not review_info:
        cs = snapshot.get("reviewer_change_summary", "")
        ra = snapshot.get("reviewer_risk_areas", [])
        if cs:
            review_info += f"\nReviewer Change Summary: {cs}"
        if ra:
            review_info += f"\nReviewer Risk Areas: {', '.join(ra) if isinstance(ra, list) else str(ra)}"

    # Inject review info into snapshot for archivist LLM
    if review_info:
        snapshot["reviewer_context"] = review_info

    # 4. LLM archivist analysis — run every commit to populate experience pool
    llm_result = None
    try:
        from experience_archivist import _run_archivist_analysis
        llm_result = await _run_archivist_analysis(v, source_v, snapshot, ui)
        # Append LLM notes to archive snapshot
        if llm_result and archive_path.exists():
            snapshot["archivist_notes"] = llm_result
            from evolution_infra import locked_file
            with locked_file(archive_path, "w") as f:
                json.dump(snapshot, f, indent=2, ensure_ascii=False)

        # Write experience_updates + strategic_advice to experience_pool.md
        if llm_result and isinstance(llm_result, dict):
            updates = llm_result.get("experience_updates", [])
            advice = llm_result.get("strategic_advice", "")
            assessment = llm_result.get("generation_assessment", "")
            if updates or (advice and advice.strip()):
                _append_experience_updates(
                    v, updates,
                    strategic_advice=advice,
                    generation_assessment=assessment,
                )
    except Exception as e:
        llm_result = {"error": str(e)}

    result = {
        "version": v,
        "source_v": source_v,
        "consistency_ok": len(consistency_issues) == 0,
        "consistency_issues": consistency_issues if consistency_issues else None,
        "reap_result": reap_result,
        "pool_size": len(active_bots),
        "snapshot": snapshot,
        "llm_analysis": llm_result,
    }

    # Record archived stage in checkpoint (then clear)
    _ckpt = _matching_checkpoint(v, source_v)
    if _ckpt:
        from evolution_infra import write_pipeline_checkpoint
        write_pipeline_checkpoint(v, source_v, "archived",
                                  master_plan=_ckpt.get("master_plan"),
                                  gate_results=_ckpt.get("gate_results"))
    clear_pipeline_checkpoint()

    return _json_tool_result(result)


# ──────────────────────────────────────────────
# Crossover
# ──────────────────────────────────────────────

class RunCrossoverInput(TypedDict):
    parent_a: Annotated[int, "First parent version"]
    parent_b: Annotated[int, "Second parent version"]
    target_v: Annotated[int, "Target child version"]


@tool("run_crossover", "Run crossover between two elite bots to create a child bot.", {"parent_a": int, "parent_b": int, "target_v": int})
async def run_crossover(args):
    parent_a = args.get("parent_a")
    parent_b = args.get("parent_b")
    target_v = args.get("target_v")
    if target_v is None:
        _v, parent_a = _resolve_version_args(args)
        target_v = target_v or _v
    if parent_a is None or parent_b is None or target_v is None:
        return _json_tool_result({"error": "Missing parent_a/parent_b/target_v"})

    # Guard: prevent self-crossover
    if parent_a == parent_b:
        return _json_tool_result({"error": "Cannot crossover with self (parent_a == parent_b)"})

    # Prepare target directory from parent A
    target_dir = get_bot_dir(target_v)

    # Guard: refuse to overwrite a completed bot
    if target_dir.exists() and (target_dir / ".completed").exists():
        return _json_tool_result({"error": f"Target v{target_v} already exists and is completed. Refusing to overwrite."})

    # Guard: parent must exist and be completed
    parent_a_dir = get_bot_dir(parent_a)
    if not parent_a_dir.exists():
        return _json_tool_result({"error": f"Parent A bot v{parent_a} not found"})
    if not (parent_a_dir / ".completed").exists():
        return _json_tool_result({"error": f"Parent A bot v{parent_a} is incomplete (no .completed sentinel)"})

    parent_b_dir = get_bot_dir(parent_b)
    if not parent_b_dir.exists():
        return _json_tool_result({"error": f"Parent B bot v{parent_b} not found"})
    if not (parent_b_dir / ".completed").exists():
        return _json_tool_result({"error": f"Parent B bot v{parent_b} is incomplete (no .completed sentinel)"})

    # Guard: both parents must have git tags (authoritative commit proof)
    from evolution_infra import git_has_tag
    if not git_has_tag(parent_a):
        return _json_tool_result({"error": f"Parent A v{parent_a} has no git tag 'bot-v{parent_a}'. Cannot use uncommitted code."})
    if not git_has_tag(parent_b):
        return _json_tool_result({"error": f"Parent B v{parent_b} has no git tag 'bot-v{parent_b}'. Cannot use uncommitted code."})

    ui = _get_ui()

    # --- P1-3: Crossover Parent Compatibility Audit ---
    try:
        from audit_agents import _run_crossover_compatibility_audit
        compat = await _run_crossover_compatibility_audit(parent_a, parent_b, ui)
        if not compat.get("compatible", True):
            log_system_event("pipeline.crossover_incompatible", "warn",
                             f"Parents v{parent_a}×v{parent_b} may be incompatible: {compat.get('conflict_areas', [])[:3]}",
                             {"parent_a": parent_a, "parent_b": parent_b, "compat": compat})
            if compat.get("compatibility_score", 10) <= 3:
                return _json_tool_result({
                    "error": f"Parents v{parent_a} and v{parent_b} are fundamentally incompatible (score={compat.get('compatibility_score')}). "
                             f"Conflicts: {', '.join(compat.get('conflict_areas', [])[:3])}. "
                             f"Suggestion: {compat.get('suggested_merge_approach', 'Select different parents.')}",
                    "compatibility": compat,
                })
    except Exception as e:
        import logging as _logging
        _logging.getLogger(__name__).warning("Crossover compat audit error (skipping): %s", e)

    success = await _run_crossover(parent_a, parent_b, target_v, ui)

    # Write checkpoint so quality gates → review → critic → commit can proceed
    if success:
        from evolution_infra import write_pipeline_checkpoint
        write_pipeline_checkpoint(target_v, parent_a, "workers_done",
                                  parent2_v=parent_b)

    result = {"success": success, "logs": ui.get_output()}
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2, ensure_ascii=False)}]}
