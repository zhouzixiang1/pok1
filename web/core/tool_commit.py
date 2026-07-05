"""Pipeline tools: commit, archivist, and crossover."""

import json
import os
import time
from typing import Annotated, TypedDict

from logging_config import get_logger
_log = get_logger("commit")

from bot_namespace import bot_name, bot_tag
from tool_runtime_guard import tool

from evolution_core import (
    get_bot_dir,
    get_active_bots,
    load_ratings,
    git_commit_bot,
    git_has_tag,
    git_dir_is_committed,
    clear_pipeline_checkpoint,
    RESULTS_DIR,
    MAX_ACTIVE_BOTS,
    _run_crossover,
    locked_file,
    EXPERIENCE_FILE,
    ARCHIVE_DIR,
    write_pipeline_checkpoint,
    archive_generation,
    archive_rotate_files,
    archive_old_logs,
)
from evolution_infra import _git, _git_ensure_main_branch, git_push_refs, publish_runtime_expected_head
from tool_helpers import (
    _get_ui, _json_tool_result,
    _matching_checkpoint, _resolve_version_args,
    PROJECT_ROOT,
    _set_pipeline_status,
    compute_h2h_avg_winrate, _load_h2h_data,
    read_pipeline_checkpoint,
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


def validate_commit_gate_ledger(v, source_v, ckpt, bot_dir=None):
    """Validate the gate ledger and code fingerprint for finalizing a bot.

    This is intentionally shared by normal ``commit_bot`` and bare-commit
    recovery. Recovery must not tag code unless the current files still match
    the exact code that passed quality and precommit.
    """
    v = int(v)
    source_v = int(source_v) if source_v is not None else None
    bot_dir = bot_dir or get_bot_dir(v)
    try:
        from tool_gates import _bot_code_fingerprint
        current_code_fingerprint = _bot_code_fingerprint(bot_dir)
    except Exception:
        current_code_fingerprint = ""

    missing_gates = []
    failed_gates = []
    gate_results = {}
    if not ckpt:
        missing_gates.append("pipeline_checkpoint")
    else:
        try:
            from workflow_profiles import get_workflow_profile
            workflow_profile = get_workflow_profile()
            expected_profile_id = getattr(workflow_profile, "profile_id", "")
            expected_execution_mode = getattr(workflow_profile, "national_execution_mode", "adapter")
        except Exception:
            expected_profile_id = ""
            expected_execution_mode = ""
        checkpoint_profile_id = str(ckpt.get("workflow_profile_id") or "")
        checkpoint_execution_mode = str(ckpt.get("national_execution_mode") or "")
        if expected_profile_id and checkpoint_profile_id and checkpoint_profile_id != expected_profile_id:
            failed_gates.append({
                "gate": "pipeline_checkpoint",
                "reason": "workflow_profile_id mismatch",
                "expected": expected_profile_id,
                "current": checkpoint_profile_id,
            })
        if expected_execution_mode and checkpoint_execution_mode and checkpoint_execution_mode != expected_execution_mode:
            failed_gates.append({
                "gate": "pipeline_checkpoint",
                "reason": "national_execution_mode mismatch",
                "expected": expected_execution_mode,
                "current": checkpoint_execution_mode,
            })
        gate_results = ckpt.get("gate_results", {}) or {}
        if source_v is not None and int(ckpt.get("source_v") or -1) != source_v:
            failed_gates.append({
                "gate": "pipeline_checkpoint",
                "reason": "source_v mismatch",
                "expected": source_v,
                "current": ckpt.get("source_v"),
            })
        if int(ckpt.get("next_v") or -1) != v:
            failed_gates.append({
                "gate": "pipeline_checkpoint",
                "reason": "next_v mismatch",
                "expected": v,
                "current": ckpt.get("next_v"),
            })
        if not current_code_fingerprint:
            failed_gates.append({
                "gate": "code_fingerprint",
                "reason": "current candidate code fingerprint is unavailable",
                "path": str(bot_dir),
            })

        quality = gate_results.get("quality")
        if not quality:
            missing_gates.append("quality")
        else:
            quality_profile_id = str(quality.get("workflow_profile_id") or quality.get("profile_id") or "")
            quality_execution_mode = str(quality.get("national_execution_mode") or "")
            if expected_profile_id and quality_profile_id != expected_profile_id:
                failed_gates.append({
                    "gate": "quality",
                    "reason": "workflow_profile_id mismatch",
                    "expected": expected_profile_id,
                    "current": quality_profile_id or "missing",
                })
            if expected_execution_mode and quality_execution_mode != expected_execution_mode:
                failed_gates.append({
                    "gate": "quality",
                    "reason": "national_execution_mode mismatch",
                    "expected": expected_execution_mode,
                    "current": quality_execution_mode or "missing",
                })
            if expected_execution_mode == "native_tcp" and quality.get("national_native_contract_ok") is not True:
                failed_gates.append({
                    "gate": "quality",
                    "reason": "national native TCP contract did not pass",
                    "value": quality.get("national_native_contract_ok"),
                })
            if quality.get("all_passed") is not True:
                failed_gates.append({"gate": "quality", "reason": "all_passed is not true", "value": quality})
            if quality.get("critical_scenarios_passed") is not True:
                failed_gates.append({"gate": "quality", "reason": "critical_scenarios_passed is not true", "value": quality})
            quality_fingerprint = quality.get("code_fingerprint")
            if not quality_fingerprint:
                missing_gates.append("quality_code_fingerprint")
            elif current_code_fingerprint and quality_fingerprint != current_code_fingerprint:
                failed_gates.append({
                    "gate": "quality",
                    "reason": "code_fingerprint changed since quality gates",
                    "expected": quality_fingerprint,
                    "current": current_code_fingerprint,
                })

        review = gate_results.get("review")
        if not review:
            missing_gates.append("review")
        elif review.get("approved") is not True:
            failed_gates.append({"gate": "review", "reason": "reviewer did not approve", "value": review})

        critic = gate_results.get("critic")
        if not critic:
            missing_gates.append("critic")
        elif critic.get("approved") is not True:
            # Critic is advisory; require the tool to have run and recorded the
            # advisory gate, while preserving force_advanced compatibility.
            if critic.get("force_advanced") is True:
                pass
            else:
                failed_gates.append({
                    "gate": "critic",
                    "reason": "critic did not approve (critic is advisory; precommit is final judge)",
                    "value": critic,
                })

        precommit = gate_results.get("precommit_eval")
        if not precommit:
            missing_gates.append("precommit_eval")
        elif precommit.get("passed") is not True:
            failed_gates.append({"gate": "precommit_eval", "reason": "precommit eval did not pass", "value": precommit})
        else:
            precommit_profile_id = str(precommit.get("workflow_profile_id") or precommit.get("profile_id") or "")
            precommit_execution_mode = str(precommit.get("national_execution_mode") or "")
            if expected_profile_id and precommit_profile_id != expected_profile_id:
                failed_gates.append({
                    "gate": "precommit_eval",
                    "reason": "workflow_profile_id mismatch",
                    "expected": expected_profile_id,
                    "current": precommit_profile_id or "missing",
                })
            if expected_execution_mode and precommit_execution_mode != expected_execution_mode:
                failed_gates.append({
                    "gate": "precommit_eval",
                    "reason": "national_execution_mode mismatch",
                    "expected": expected_execution_mode,
                    "current": precommit_execution_mode or "missing",
                })
            precommit_fingerprint = precommit.get("code_fingerprint")
            if not precommit_fingerprint:
                missing_gates.append("precommit_code_fingerprint")
            elif current_code_fingerprint and precommit_fingerprint != current_code_fingerprint:
                failed_gates.append({
                    "gate": "precommit_eval",
                    "reason": "code_fingerprint changed since precommit eval",
                    "expected": precommit_fingerprint,
                    "current": current_code_fingerprint,
                })

        if expected_execution_mode == "native_tcp":
            try:
                from national_native import check_native_contract
                native_contract_errors = check_native_contract(bot_dir)
            except Exception as exc:
                native_contract_errors = [f"{type(exc).__name__}: {str(exc)[:200]}"]
            if native_contract_errors:
                failed_gates.append({
                    "gate": "native_contract",
                    "reason": "candidate is not a valid native national TCP bot",
                    "errors": native_contract_errors[:5],
                })

    return {
        "ok": not missing_gates and not failed_gates,
        "missing_gates": missing_gates,
        "failed_gates": failed_gates,
        "gate_results": gate_results,
        "current_code_fingerprint": current_code_fingerprint,
        "checkpoint_stage": ckpt.get("stage") if ckpt else None,
    }


@tool("commit_bot", "Commit a bot generation with git commit and tag. review_approved must be true (set after run_review returns approved:true).", {"version": int, "source_v": int, "strategy": str, "review_approved": bool})
async def commit_bot(args):
    _t0 = time.time()
    v, source_v = _resolve_version_args(args)
    if v is None or source_v is None:
        return _json_tool_result({"error": "Missing version/source_v and no active pipeline checkpoint"})
    v = int(v)
    source_v = int(source_v)
    strategy = args.get("strategy", "")
    review_approved = args.get("review_approved", False)

    _set_pipeline_status(f"Committing v{v}")

    bot_dir = get_bot_dir(v)
    ckpt = _matching_checkpoint(v, source_v)
    ledger = validate_commit_gate_ledger(v, source_v, ckpt, bot_dir=bot_dir)
    missing_gates = ledger["missing_gates"]
    failed_gates = ledger["failed_gates"]
    gate_results = ledger["gate_results"]

    if missing_gates or failed_gates:
        try:
            log_system_event('pipeline.commit_blocked', 'error',
                f'Commit blocked for v{v}: missing={missing_gates} failed={failed_gates}',
                {'version': v, 'source_v': source_v, 'missing_gates': missing_gates,
                 'failed_gates': failed_gates})
        except Exception:
            pass
        return _json_tool_result({
            "error": "COMMIT BLOCKED: gate ledger incomplete or failed.",
            "version": v,
            "source_v": source_v,
            "checkpoint_stage": ledger["checkpoint_stage"],
            "missing_gates": missing_gates,
            "failed_gates": failed_gates,
            "gate_results": gate_results,
        })

    # Guard: reviewer approval required
    if not review_approved:
        return _json_tool_result({
            "error": "COMMIT BLOCKED: review_approved=false. Call run_review() first; only pass review_approved=true if it returns approved:true.",
        })

    # fix-6: novelty gate — warn (advisory) if new bot doesn't add behavioral
    # diversity. This is advisory-only: it does NOT block the commit, because
    # a bot can improve by fine-tuning within a niche. The warning feeds the
    # archivist and next generation's Master context.
    novelty_info = {}
    try:
        from behavior_diversity import (
            compute_decision_fingerprint, compute_delta_vendi,
            load_fingerprints, save_fingerprint,
        )
        from evolution_infra import get_active_bots
        candidate_bot = bot_name(v)
        new_fp = compute_decision_fingerprint(candidate_bot)
        pool_bots = get_active_bots()
        # Build pool fingerprints from stored data
        stored = load_fingerprints()
        pool_fps = [stored[b] for b in pool_bots if b in stored and b != candidate_bot]
        if pool_fps:
            import numpy as np
            pool_arr = np.stack(pool_fps)
            delta_vs = compute_delta_vendi(pool_arr, new_fp)
            novelty_info = {
                "delta_vendi_score": round(float(delta_vs), 4),
                "pool_size": len(pool_fps),
            }
            if delta_vs < 0.05:
                novelty_info["novelty_warning"] = (
                    f"Low behavioral novelty: delta_VS={delta_vs:.4f} < 0.05. "
                    f"The new bot occupies a similar behavioral niche as existing pool bots."
                )
                _log.warning(
                    "Novelty gate advisory for v%d: delta_VS=%.4f < 0.05",
                    v, delta_vs,
                )
        # Save the new bot's fingerprint for future novelty checks
        save_fingerprint(candidate_bot, new_fp)
    except Exception as e:
        _log.warning("Novelty gate skipped (non-fatal): %s", e)

    ratings = load_ratings()
    p = ratings.get(bot_name(v))
    h2h_wr = None
    try:
        h2h_wr = compute_h2h_avg_winrate(bot_name(v), _load_h2h_data())
    except Exception as e:
        _log.warning("H2H win rate computation failed for v%d: %s", v, e)
    wr_str = f" h2h_avg_wr={h2h_wr:.2%}" if h2h_wr is not None else ""
    rating_info = f"rating: r={p.r:.1f} rd={p.rd:.1f}{wr_str}" if p else ""

    parent2_v = ckpt.get("parent2_v") if ckpt else None
    push_ok = git_commit_bot(v, source_v, strategy, rating_info=rating_info, parent2_v=parent2_v)

    # Verify tag was created
    if not git_has_tag(v):
        return _json_tool_result({
            "error": f"Git tag {bot_tag(v)} not found after commit. Git operations may have failed.",
            "version": v,
        })

    (bot_dir / ".completed").touch()

    # Write reap_signal early so daemon discovers new bot immediately, even if archive/timeout interrupts later
    reap_signal = RESULTS_DIR / ".reap_signal"
    reap_signal.write_text(str(time.time()))

    # Write priority eval signal so daemon schedules this bot heavily
    priority_file = RESULTS_DIR / "priority_eval.json"
    try:
        with locked_file(priority_file, "w") as f:
            json.dump({"bot": bot_name(v), "min_games": 500, "since": time.time()}, f)
    except Exception as e:
        _log.warning("Priority eval signal write failed for v%d: %s", v, e)

    # LOG GAP FIX (2026-06-29): enrich the commit audit event with rating,
    # file_size, and gate_results summary so a committed generation is fully
    # auditable from the event log alone (previously only version/source/strategy).
    _commit_audit = {"version": v, "source_v": source_v, "strategy": strategy[:120]}
    try:
        if p is not None:
            _commit_audit["rating"] = {"r": round(p.r, 1), "rd": round(p.rd, 1)}
        if h2h_wr is not None:
            _commit_audit["h2h_avg_wr"] = round(h2h_wr, 4)
    except Exception:
        pass
    try:
        _py_files = list(bot_dir.glob("*.py"))
        _commit_audit["file_size_total"] = sum(f.stat().st_size for f in _py_files)
        _commit_audit["n_py_files"] = len(_py_files)
    except Exception:
        pass
    try:
        _gr = (ckpt or {}).get("gate_results", {}) or {}
        _commit_audit["gate_results"] = {
            "quality_passed": (_gr.get("quality") or {}).get("passed"),
            "review_score": (_gr.get("review") or {}).get("score"),
            "critic_score": (_gr.get("critic") or {}).get("score"),
            "precommit_passed": (_gr.get("precommit_eval") or {}).get("passed"),
        }
    except Exception:
        pass
    log_system_event("pipeline.committed", "success",
                     f"Committed v{v} from v{source_v}: {strategy[:80]}", _commit_audit)

    _set_pipeline_status(f"Committed v{v}", is_working=False)

    # Archive this generation's state snapshot
    try:
        archive_generation(v, source_v, ckpt)
        archive_rotate_files(v)
        archive_old_logs()
    except Exception as e:
        _log.warning("Archive generation failed for v%d: %s", v, e)

    # --- Meta-3: Record Critic Calibration Data (before clearing checkpoint) ---
    # fix-2: Write rating_delta=None as placeholder. The real delta is backfilled
    # asynchronously by reconcile_critic_calibration() once the daemon converges
    # the bot's rating (rd < 60, games >= MIN_GAMES_FOR_EVAL). Writing the stale
    # r-2*rd value at commit time was 98% zero (new bot rd=350 → delta~0),
    # rendering calibration inert.
    try:
        if ckpt:
            critic_gate = ckpt.get("gate_results", {}).get("critic", {})
            critic_score = critic_gate.get("score", 0)
            cal_file = RESULTS_DIR / "critic_calibration.jsonl"
            cal_entry = json.dumps({
                "version": v, "source_v": source_v,
                "critic_score": critic_score,
                "rating_delta": None,  # backfilled by reconcile_critic_calibration()
                "reconciled": False,   # marker for reconcile to find unfilled rows
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            })
            with open(cal_file, "a", encoding="utf-8") as _cf:
                _cf.write(cal_entry + "\n")
    except Exception:
        pass  # Calibration recording is advisory

    # --- Phase 3: FAMOU nemesis archive (advisory) ---
    # Recompute the nemesis/champion relationships from the on-disk h2h so the
    # next generation's precommit nemesis probe has a fallback snapshot when
    # the live h2h scan finds no qualifying nemesis. The new bot itself has no
    # h2h yet (it just got tagged), but committing it refreshes every other
    # bot's nemesis mapping. Best-effort: never blocks the commit path.
    try:
        from nemesis_archive import write_nemesis_archive
        write_nemesis_archive(get_active_bots())
    except Exception as e:
        _log.warning("Nemesis archive write failed for v%d: %s", v, e)

    clear_pipeline_checkpoint()

    try:
        from server.state import app_state
        app_state.set_generation(v, v + 1)
    except Exception as e:
        _log.warning("App state update failed for v%d: %s", v, e)

    # ── Update eval table + metrics in evolution state snapshot ──
    try:
        ratings = load_ratings()
        active_bots = get_active_bots()
        ui = _get_ui()
        ui.update_eval_table(ratings, active_bots)
        ui.update_metrics({
            "current_v": v,
            "next_v": v + 1,
            "success_rate": 1.0,  # generation succeeded
        })
    except Exception:
        pass  # non-blocking enrichment

    result = {"committed": True, "version": v, "source_v": source_v, "push_ok": push_ok}
    if novelty_info:
        result["novelty_gate"] = novelty_info
    active_bots = get_active_bots()
    if len(active_bots) > MAX_ACTIVE_BOTS:
        result["needs_reap"] = True
        result["pool_size"] = len(active_bots)
    try:
        log_system_event("pipeline.commit_done", "info",
                         f"Commit finished for v{v} in {time.time() - _t0:.1f}s",
                         {"version": v, "elapsed_sec": round(time.time() - _t0, 2)})
    except Exception:
        pass
    return _json_tool_result(result)


# ──────────────────────────────────────────────
# Archivist Stage
# ──────────────────────────────────────────────

def _append_experience_updates(version: int, updates: list[str],
                                strategic_advice: str = "", generation_assessment: str = "",
                                require_committed: bool = True):
    """Append archivist experience_updates, strategic_advice, and assessment to experience_pool.md."""
    if require_committed and not git_has_tag(version):
        try:
            log_system_event(
                "pipeline.experience_write_blocked_uncommitted", "warn",
                f"Blocked experience_pool.md write for uncommitted v{version}",
                {"version": version, "updates": updates[:5],
                 "generation_assessment": generation_assessment},
            )
        except Exception:
            pass
        return

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


def _git_dirty_paths() -> set[str]:
    """Return porcelain dirty paths without mutating git state."""
    out = _git("status", "--porcelain", check=False)
    paths: set[str] = set()
    for line in out.splitlines():
        if not line:
            continue
        # Porcelain v1: XY<space>path, rename: XY old -> new.
        path = line[3:] if len(line) > 3 else line
        if " -> " in path:
            old, new = path.split(" -> ", 1)
            paths.add(old.strip())
            paths.add(new.strip())
        else:
            paths.add(path.strip())
    return paths


def _path_was_dirty(path: str, preexisting_dirty: set[str]) -> bool:
    prefix = path.rstrip("/") + "/"
    return any(p == path or p.startswith(prefix) for p in preexisting_dirty)


def _archive_housekeeping_commit(version: int, reap_result: dict | None,
                                 experience_touched: bool,
                                 preexisting_dirty: set[str]) -> dict:
    """Commit archivist/reap tracked-file side effects so the worktree stays clean.

    commit_bot owns the bot commit and tag. run_archivist can still create tracked
    housekeeping changes after that point: experience_pool.md updates and tracked
    bot deletions from auto-reap. Those must be explicit, path-scoped commits
    rather than hidden user-facing dirty state.
    """
    _git_ensure_main_branch()

    preexisting_staged = [
        p for p in _git("diff", "--cached", "--name-only", check=False).splitlines()
        if p
    ]
    if preexisting_staged:
        log_system_event(
            "pipeline.archivist_housekeeping_skip_staged", "warn",
            f"v{version}: skipped housekeeping commit because staged files already exist",
            {"version": version, "staged_files": preexisting_staged[:40]},
        )
        return {
            "committed": False,
            "reason": "preexisting_staged_files",
            "preexisting_staged": preexisting_staged,
        }

    candidates: list[tuple[str, str]] = []
    if experience_touched:
        try:
            candidates.append((str(EXPERIENCE_FILE.relative_to(PROJECT_ROOT)), "add"))
        except ValueError:
            pass
    if reap_result and reap_result.get("reaped") and reap_result.get("culled"):
        candidates.append((f"bots/{reap_result['culled']}", "add-u"))

    staged_paths: list[str] = []
    skipped_preexisting: list[str] = []
    for path, mode in candidates:
        if _path_was_dirty(path, preexisting_dirty):
            skipped_preexisting.append(path)
            continue
        dirty_now = _git("status", "--porcelain", "--", path, check=False).strip()
        if not dirty_now:
            continue
        if mode == "add-u":
            _git("add", "-u", "--", path, check=False)
        else:
            _git("add", "--", path, check=False)
        staged_paths.extend(
            p for p in _git("diff", "--cached", "--name-only", "--", path, check=False).splitlines()
            if p and p not in staged_paths
        )

    if skipped_preexisting:
        log_system_event(
            "pipeline.archivist_housekeeping_skip_dirty", "warn",
            f"v{version}: skipped pre-existing dirty housekeeping path(s)",
            {"version": version, "paths": skipped_preexisting},
        )
    if not staged_paths:
        return {
            "committed": False,
            "reason": "no_housekeeping_changes",
            "skipped_preexisting": skipped_preexisting,
        }
    staged_set = {
        p for p in _git("diff", "--cached", "--name-only", check=False).splitlines()
        if p
    }
    allowed_set = set(staged_paths)
    unexpected = sorted(staged_set - allowed_set)
    if unexpected:
        for path in staged_paths:
            _git("restore", "--staged", "--", path, check=False)
        log_system_event(
            "pipeline.archivist_housekeeping_skip_unexpected_staged", "warn",
            f"v{version}: skipped housekeeping commit because unrelated staged files appeared",
            {"version": version, "unexpected_staged": unexpected[:40],
             "housekeeping_paths": staged_paths[:40]},
        )
        return {
            "committed": False,
            "reason": "unexpected_staged_files",
            "unexpected_staged": unexpected,
            "staged_files": staged_paths,
            "skipped_preexisting": skipped_preexisting,
        }

    log_system_event(
        "pipeline.archivist_git_commit_staged", "info",
        f"v{version}: staging {len(staged_paths)} archivist housekeeping file(s)",
        {"version": version, "staged_files": staged_paths[:40]},
    )
    _git("commit", "-m", f"chore: archive v{version} evolution housekeeping", "--", *staged_paths)
    commit_hash = _git("rev-parse", "--short", "HEAD", check=False).strip()
    publish_runtime_expected_head("archivist_housekeeping_commit", version=version)
    push_ok = False
    if os.environ.get("EVOLUTION_GIT_PUSH") == "1":
        push_ok = git_push_refs("main")
        publish_runtime_expected_head("archivist_housekeeping_push", version=version)
    log_system_event(
        "pipeline.archivist_git_commit_done", "success",
        f"v{version}: committed archivist housekeeping {commit_hash}",
        {"version": version, "commit": commit_hash, "push_ok": push_ok},
    )
    return {
        "committed": True,
        "commit": commit_hash,
        "push_ok": push_ok,
        "staged_files": staged_paths,
        "skipped_preexisting": skipped_preexisting,
    }


@tool("run_archivist", "Run post-commit archive audit for a completed generation. Verifies consistency, auto-reaps if needed, calls LLM for strategic assessment and experience pool updates.", {"version": int, "source_v": int})
async def run_archivist(args):
    v, source_v = _resolve_version_args(args)
    if v is None or source_v is None:
        return _json_tool_result({"error": "Missing version/source_v and no active pipeline checkpoint"})
    v = int(v)
    source_v = int(source_v)

    _set_pipeline_status(f"Archiving v{v}")

    ui = _get_ui()
    preexisting_dirty = _git_dirty_paths()

    # 1. Verify post-commit consistency
    bot_dir = get_bot_dir(v)
    consistency_issues = []
    if not (bot_dir / ".completed").exists():
        consistency_issues.append(f".completed missing for v{v}")
    if not git_has_tag(v):
        consistency_issues.append(f"git tag {bot_tag(v)} missing")
    ratings = load_ratings()
    if bot_name(v) not in ratings:
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
    experience_touched = False
    try:
        from experience_archivist import _run_archivist_analysis
        llm_result = await _run_archivist_analysis(v, source_v, snapshot, ui)
        # Append LLM notes to archive snapshot
        if llm_result and archive_path.exists():
            snapshot["archivist_notes"] = llm_result
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
                experience_touched = True
    except Exception as e:
        llm_result = {"error": str(e)}

    housekeeping_commit = None
    try:
        housekeeping_commit = _archive_housekeeping_commit(
            v, reap_result, experience_touched, preexisting_dirty
        )
    except Exception as e:
        housekeeping_commit = {"error": str(e)}
        log_system_event(
            "pipeline.archivist_git_commit_failed", "error",
            f"v{v}: archivist housekeeping commit failed: {str(e)[:180]}",
            {"version": v, "error": str(e)[:500]},
        )

    result = {
        "version": v,
        "source_v": source_v,
        "consistency_ok": len(consistency_issues) == 0,
        "consistency_issues": consistency_issues if consistency_issues else None,
        "reap_result": reap_result,
        "pool_size": len(active_bots),
        "snapshot": snapshot,
        "llm_analysis": llm_result,
        "housekeeping_commit": housekeeping_commit,
    }

    # Record archived stage in checkpoint (then clear)
    _ckpt = _matching_checkpoint(v, source_v)
    if _ckpt:
        write_pipeline_checkpoint(v, source_v, "archived",
                                  master_plan=_ckpt.get("master_plan"),
                                  gate_results=_ckpt.get("gate_results"))
    clear_pipeline_checkpoint()

    try:
        log_system_event('pipeline.archivist_done', 'info',
            f'Archivist completed for v{v}',
            {'version': v, 'source_v': source_v,
             'consistency_ok': len(consistency_issues) == 0,
             'pool_size': len(active_bots)})
    except Exception:
        pass

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

    _set_pipeline_status(f"Crossover for v{target_v}")

    # Guard: prevent self-crossover
    if parent_a == parent_b:
        return _json_tool_result({"error": "Cannot crossover with self (parent_a == parent_b)"})

    # Prepare target directory from parent A
    target_dir = get_bot_dir(target_v)

    # Guard: refuse to overwrite a completed bot
    if target_dir.exists() and (target_dir / ".completed").exists():
        return _json_tool_result({"error": f"Target v{target_v} already exists and is completed. Refusing to overwrite."})

    # Guard: refuse to overwrite a BARE-COMMITTED target (root-cause fix for the
    # v117 repeated-regeneration loop, 2026-06-18). A target dir that is
    # git-tracked but lacks an active-epoch tag was created by a bare `git commit`
    # bypassing commit_bot. Silently re-running crossover on it regenerates the
    # same version forever — find_current_v() only trusts tags, so it stays
    # stale and the orchestrator keeps picking the same target_v. Require
    # commit_bot finalization or explicit abandon/clear first. (This is the
    # crossover-side mirror of prepare_next_gen's stage guard, which crossover
    # previously lacked — the deepest root cause per adversarial verification.)
    if target_dir.exists() and git_dir_is_committed(target_v) and not git_has_tag(target_v):
        return _json_tool_result({
            "error": f"Target v{target_v} is git-committed but has no {bot_tag(target_v)} tag (bare commit bypassing commit_bot). "
                     f"Refusing to overwrite — re-running crossover here causes infinite regeneration. "
                     f"Run commit_bot for v{target_v} to finalize it, or abandon/clear the untagged dir first."
        })

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
    if not git_has_tag(parent_a):
        return _json_tool_result({"error": f"Parent A v{parent_a} has no git tag '{bot_tag(parent_a)}'. Cannot use uncommitted code."})
    if not git_has_tag(parent_b):
        return _json_tool_result({"error": f"Parent B v{parent_b} has no git tag '{bot_tag(parent_b)}'. Cannot use uncommitted code."})

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
        _log.warning("Crossover compat audit error (skipping): %s", e)

    success = await _run_crossover(parent_a, parent_b, target_v, ui)

    # Write checkpoint so quality gates → review → critic → commit can proceed
    if success:
        crossover_plan = {
            "strategy": "crossover",
            "tasks": [],
            "parents": [parent_a, parent_b],
            "source_v": parent_a,
            "next_v": target_v,
            "note": "Crossover already generated bot code. Skip run_master and execute_workers; proceed to run_quality_gates.",
        }
        write_pipeline_checkpoint(target_v, parent_a, "workers_done",
                                  master_plan=crossover_plan,
                                  parent2_v=parent_b,
                                  audit_context={"crossover": {"parent_a": parent_a, "parent_b": parent_b}})
        try:
            log_system_event('pipeline.crossover_done', 'info',
                f'Crossover v{parent_a}×v{parent_b} → v{target_v} succeeded',
                {'target_v': target_v, 'parent_a': parent_a, 'parent_b': parent_b})
            log_system_event(
                "pipeline.crossover_resume_quality", "info",
                f"Crossover v{target_v} checkpoint ready; next step is run_quality_gates",
                {"target_v": target_v, "parent_a": parent_a,
                 "parent_b": parent_b, "next_step": "run_quality_gates"},
            )
        except Exception:
            pass
    else:
        try:
            log_system_event('pipeline.crossover_failed', 'error',
                f'Crossover v{parent_a}×v{parent_b} → v{target_v} failed',
                {'target_v': target_v, 'parent_a': parent_a, 'parent_b': parent_b})
        except Exception:
            pass

    result = {"success": success, "logs": ui.get_output()}
    return _json_tool_result(result)
