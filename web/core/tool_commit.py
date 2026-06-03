"""Pipeline tools: commit, archivist, and crossover."""

import json
import os
import time
from pathlib import Path
from typing import Annotated, TypedDict

from claude_agent_sdk import tool

from evolution_core import (
    get_bot_dir,
    get_active_bots,
    load_ratings,
    verify_code,
    check_code_size,
    run_smoke_test,
    run_decision_test_details,
    git_commit_bot,
    git_has_tag,
    clear_pipeline_checkpoint,
    RESULTS_DIR,
    MAX_ACTIVE_BOTS,
    _run_crossover,
)
from tool_helpers import (
    _get_ui, _json_tool_result,
    _matching_checkpoint,
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
    v = args["version"]
    source_v = args["source_v"]
    strategy = args["strategy"]
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

    # Guard: compile check
    compile_errors = verify_code(bot_dir)
    if compile_errors:
        return _json_tool_result({
            "error": "COMMIT BLOCKED: compile errors present. Run run_quality_gates() first and fix errors.",
            "compile_errors": compile_errors[:3],
        })

    smoke_errors = run_smoke_test(bot_dir)
    if smoke_errors:
        return _json_tool_result({
            "error": "COMMIT BLOCKED: smoke test failed. Run run_quality_gates() first and fix runtime errors.",
            "smoke_errors": smoke_errors[:3],
        })

    decision_detail = run_decision_test_details(bot_dir)
    decision_rate = decision_detail.get("pass_rate", 0.0)
    critical_failures = decision_detail.get("critical_failures", [])
    if decision_rate < 0.7 or critical_failures:
        return _json_tool_result({
            "error": "COMMIT BLOCKED: decision tests failed. Fix catastrophic blunders first.",
            "decision_pass_rate": round(decision_rate, 2),
            "critical_failures": critical_failures,
            "decision_failures": decision_detail.get("failures", []),
        })

    _, oversized = check_code_size(bot_dir)
    if oversized:
        return _json_tool_result({
            "error": "COMMIT BLOCKED: code size gate failed.",
            "oversized_files": {name: lines for name, lines in oversized},
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
    except Exception:
        pass
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
    except Exception:
        pass

    clear_pipeline_checkpoint()

    try:
        from server.state import app_state
        app_state.set_generation(v, v + 1)
    except Exception:
        pass

    # Signal daemon to pick up the new bot
    reap_signal = RESULTS_DIR / ".reap_signal"
    reap_signal.write_text(str(time.time()))

    # Write priority eval signal so daemon schedules this bot heavily
    priority_file = Path(__file__).parent / "results" / "priority_eval.json"
    try:
        from evolution_infra import locked_file
        with locked_file(priority_file, "w") as f:
            json.dump({"bot": f"claude_v{v}", "min_games": 100, "since": time.time()}, f)
    except Exception:
        pass

    result = {"committed": True, "version": v, "source_v": source_v, "push_ok": push_ok}
    active_bots = get_active_bots()
    if len(active_bots) > MAX_ACTIVE_BOTS:
        result["needs_reap"] = True
        result["pool_size"] = len(active_bots)
    return _json_tool_result(result)


# ──────────────────────────────────────────────
# Archivist Stage
# ──────────────────────────────────────────────

@tool("run_archivist", "Run post-commit archive audit for a completed generation. Verifies consistency, auto-reaps if needed, optionally calls LLM for strategic assessment.", {"version": int, "source_v": int})
async def run_archivist(args):
    v = args["version"]
    source_v = args["source_v"]
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

    # 4. Conditional LLM archivist analysis
    llm_result = None
    needs_llm = False
    # Check if rating has been declining for 3+ gens
    try:
        rating_trend = []
        for check_v in range(max(1, v - 4), v + 1):
            check_archive = ARCHIVE_DIR / f"v{check_v}.json"
            if check_archive.exists():
                with open(check_archive, "r") as f:
                    s = json.load(f)
                r = s.get("rating", {}).get("r")
                if r:
                    rating_trend.append((check_v, r))
        if len(rating_trend) >= 3:
            declining = all(rating_trend[i][1] > rating_trend[i + 1][1] for i in range(len(rating_trend) - 1))
            if declining:
                needs_llm = True
    except Exception:
        pass

    if needs_llm or os.environ.get("EVOLUTION_ALWAYS_ARCHIVE_LLM") == "1":
        try:
            from experience_archivist import _run_archivist_analysis
            llm_result = await _run_archivist_analysis(v, source_v, snapshot, ui)
            # Append LLM notes to archive snapshot
            if llm_result and archive_path.exists():
                snapshot["archivist_notes"] = llm_result
                from evolution_infra import locked_file
                with locked_file(archive_path, "w") as f:
                    json.dump(snapshot, f, indent=2, ensure_ascii=False)
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
    parent_a = args["parent_a"]
    parent_b = args["parent_b"]
    target_v = args["target_v"]

    # Prepare target directory from parent A
    target_dir = get_bot_dir(target_v)

    # Guard: refuse to overwrite a completed bot
    if target_dir.exists() and (target_dir / ".completed").exists():
        return _json_tool_result({"error": f"Target v{target_v} already exists and is completed. Refusing to overwrite."})

    # Guard: parent must exist
    parent_a_dir = get_bot_dir(parent_a)
    if not parent_a_dir.exists():
        return _json_tool_result({"error": f"Parent A bot v{parent_a} not found"})

    parent_b_dir = get_bot_dir(parent_b)
    if not parent_b_dir.exists():
        return _json_tool_result({"error": f"Parent B bot v{parent_b} not found"})

    ui = _get_ui()
    success = await _run_crossover(parent_a, parent_b, target_v, ui)

    # Write checkpoint so quality gates → review → critic → commit can proceed
    if success:
        from evolution_infra import write_pipeline_checkpoint
        write_pipeline_checkpoint(target_v, parent_a, "workers_done",
                                  parent2_v=parent_b)

    result = {"success": success, "logs": ui.get_output()}
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2, ensure_ascii=False)}]}
