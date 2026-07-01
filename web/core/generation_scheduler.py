"""Generation scheduler — three-phase evolution cycle.

Phase 1 (prepare_generation): Code-layer analysis and strategy decision.
Phase 2 (run_one_generation): LLM-driven pipeline execution (in orchestrator.py).
Phase 3 (post_generation_cleanup): Code-layer cleanup and maintenance.

Phase 1 is disposable (interrupt → re-run with fresh data).
Phase 2 preserves state on interrupt (session + checkpoint files).
Phase 3 is idempotent (interrupt → re-run safely).
"""

import asyncio
import json
import logging
import os
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from system_log import log_system_event, SYSTEM_EVENTS_FILE

log = logging.getLogger("pok.scheduler")

OSCILLATION_BREAKOUT_SCORE_TOLERANCE = 0.02
OSCILLATION_BREAKOUT_MIN_MARGIN = 0.01
POST_CLEANUP_EXPERIENCE_TIMEOUT = int(os.environ.get("POK_POST_CLEANUP_EXPERIENCE_TIMEOUT", "600"))


# Single-flight guard for the background exploitability probe thread (see the
# fire-and-forget block in post_generation_cleanup). post_generation_cleanup is
# async on a single event loop, so the is_set()/set() pair is atomic within a
# generation — overlapping cleanups (fast crossover gens) cannot pile up probe
# threads or race on exploitability.json.
_probe_running = threading.Event()


def _save_committed_bot_fingerprint(committed_v: int) -> str:
    """Compute and persist the behavior fingerprint for a committed bot."""
    from behavior_diversity import compute_decision_fingerprint, save_fingerprint

    bot_name = f"claude_v{int(committed_v)}"
    fp = compute_decision_fingerprint(bot_name)
    save_fingerprint(bot_name, fp)
    log.info("Behavior fingerprint saved for %s", bot_name)
    log_system_event(
        "pipeline.fingerprint_saved",
        "info",
        f"Behavior fingerprint saved for {bot_name}",
        {"version": int(committed_v), "bot": bot_name},
    )
    return bot_name


def _wilson_lower_bound(wins, games, z=1.96):
    """95% lower confidence bound on the true win rate (Wilson score interval).

    Used by the H2H anomaly detector (prepare_generation) so small-sample
    matchups (e.g. n=20 games) do not manufacture fake regressions from pure
    binomial noise. Under the null (true wr=0.5), n=20 has ~12% chance of a
    point estimate |wr-0.5|>0.15; the Wilson lower bound raises the bar to
    "statistically confident below even".
    """
    if games <= 0:
        return 0.0
    p = wins / games
    denom = 1.0 + z * z / games
    center = (p + z * z / (2 * games)) / denom
    margin = z * ((p * (1 - p) + z * z / (4 * games)) / games) ** 0.5 / denom
    return max(0.0, center - margin)


@dataclass
class GenerationContext:
    """Pre-computed context for one generation."""
    current_v: int
    next_v: int
    strategy: str              # "master" | "crossover"
    source_v: int              # branch_from or current_v
    crossover_parents: tuple = ()  # (parent_a, parent_b) if crossover
    stagnation_info: str = ""
    match_analysis: str = ""
    performance_verification: str = ""
    replay_spotlight: str = ""
    gen_count: int = 0         # legacy alias for current_v; use next_v for post-generation triggers
    battle_experience: str = ""


def _bind_prepare_log_context(current_v: int, max_committed_v: int) -> int:
    """Bind structured logs emitted during disposable Phase-1 prepare."""
    planned_next_v = max(current_v, max_committed_v) + 1
    attempt = {"generation": 0, "audit": 0, "precommit": 0}
    try:
        from event_bus import update_last_known, invalidate_ckpt_cache
        update_last_known(run_id=f"{planned_next_v}#0", stage="preparing", attempt=attempt)
        invalidate_ckpt_cache()
    except Exception:
        pass
    try:
        log_system_event(
            "pipeline.prepare_context_bound",
            "info",
            f"Prepare log context bound for v{planned_next_v}",
            {"next_v": planned_next_v, "current_v": current_v,
             "max_committed_v": max_committed_v, "stage": "preparing"},
        )
    except Exception:
        pass
    return planned_next_v


async def prepare_generation(shutdown_mgr, ui=None, min_games=None) -> GenerationContext | None:
    """Phase 1: Analyze state, decide strategy. Disposable on interrupt."""
    from evolution_infra import (
        MAX_ACTIVE_BOTS, MIN_GAMES_FOR_EVAL, find_current_v, find_latest_active_v, get_active_bots, load_ratings,
        find_max_committed_v, git_dir_is_committed, git_has_tag,
        wait_for_daemon_eval,
    )

    if shutdown_mgr and shutdown_mgr.is_shutting_down:
        return None

    current_v = find_current_v()       # 版本编号（含 graveyard），用于 next_v
    # 裸 commit 对账（v117 反复重生循环根因修复, 2026-06-18）：find_max_committed_v()
    # 返回含裸 commit（绕过 commit_bot 直接 git commit、无 tag+.completed）的最大版本号。
    # 用它抬高 next_v 下界，使裸 commit 占用的版本号不会被下一代重生覆盖。
    max_committed_v = find_max_committed_v()
    # P2 (2026-06-29 reboot analysis): also account for abandoned versions.
    # _do_abandon_generation rmtree's the dir (so it's not git-tracked and
    # invisible to find_max_committed_v), then logs the version to
    # abandoned_versions.jsonl. Without this, next_v reuses the just-abandoned
    # number (find_current_v returns the last TAGGED v, so next_v = tagged+1 ==
    # the abandoned v), causing a dead-end retry (observed: v218 abandon→re-prepare
    # as v218). Read the max abandoned v and fold it into the next_v floor.
    _abandoned_floor = 0
    try:
        from evolution_infra import RESULTS_DIR as _ab_results
        _ab_file = _ab_results / "abandoned_versions.jsonl"
        if _ab_file.exists():
            with open(_ab_file, "r", encoding="utf-8") as _af:
                for _line in _af:
                    _line = _line.strip()
                    if not _line:
                        continue
                    try:
                        _av = json.loads(_line).get("v")
                        if isinstance(_av, int) and _av > _abandoned_floor:
                            _abandoned_floor = _av
                    except (json.JSONDecodeError, TypeError):
                        continue
    except Exception as _ab_e:
        # LOG GAP FIX (2026-06-29): if abandoned_versions.jsonl is unreadable,
        # next_v floor falls back to max_committed_v and may reuse an abandoned
        # version — the exact bug P2 was written to prevent. Warn so it's visible.
        try:
            log_system_event(
                "pipeline.abandoned_floor_unavailable", "warn",
                f"abandoned_versions.jsonl unreadable; next_v floor may reuse an "
                f"abandoned version: {_ab_e}",
                {"error": str(_ab_e)[:200]},
            )
        except Exception:
            pass
    if _abandoned_floor > max_committed_v:
        max_committed_v = _abandoned_floor
        log.info(
            "P2: next_v floor raised to %d based on abandoned_versions.jsonl "
            "(preventing reuse of just-abandoned version)", max_committed_v + 1,
        )
    if max_committed_v > current_v:
        _bare = [v for v in range(current_v + 1, max_committed_v + 1)
                 if git_dir_is_committed(v) and not git_has_tag(v)]
        if _bare:
            log_system_event(
                "pipeline.bare_commit_detected", "error",
                f"Bare commit(s) v{_bare} are git-tracked but untagged (bypassed commit_bot). "
                f"next_v floored to {max_committed_v + 1} to prevent regeneration loop.",
                {"bare_versions": _bare, "current_v": current_v,
                 "max_committed_v": max_committed_v,
                 "next_v": max_committed_v + 1},
            )
            if ui:
                ui.log_history(
                    f"⚠️ 裸commit检测: v{_bare} 已git提交但无tag(绕过commit_bot)。"
                    f"next_v={max_committed_v + 1}(跳过裸commit版本,避免反复重生)。"
                    f"如需保留该版本请用commit_bot补全tag+.completed,否则它将孤立。",
                    "warn",
                )
    _planned_next_v = _bind_prepare_log_context(current_v, max_committed_v)
    active_v = find_latest_active_v()  # 活跃 bot（排除 graveyard），用于 eval/分析
    active_bots = get_active_bots()
    ratings = load_ratings()
    bot_name = f"claude_v{active_v}"   # 等待活跃 bot 的 eval（核心 fix）

    # Reap bots if pool exceeds limit — reduces starvation in match selection
    if len(active_bots) > MAX_ACTIVE_BOTS:
        from tool_bot_management import _do_reap_weakest
        reap_count = 0
        while len(get_active_bots()) > MAX_ACTIVE_BOTS and reap_count < 10:
            try:
                result = await _do_reap_weakest(quiet=True)
                if not result.get("reaped"):
                    break
                if ui:
                    ui.log_history(f"淘汰 {result['culled']} (池 {result['remaining']}/{MAX_ACTIVE_BOTS})", "info")
            except Exception as e:
                log.warning("Pre-eval reap failed: %s\n%s", e, traceback.format_exc())
                if ui:
                    ui.log_history(f"淘汰失败: {e}", "warn")
                break
            reap_count += 1

    # Wait for sufficient evaluation
    eval_kwargs = {"ui": ui, "shutdown_event": shutdown_mgr}
    if min_games is not None:
        eval_kwargs["min_games"] = min_games
    eval_ok = await wait_for_daemon_eval(bot_name, **eval_kwargs)
    if shutdown_mgr and shutdown_mgr.is_shutting_down:
        return None
    if not eval_ok:
        if ui:
            ui.log_history("Waiting for evaluation (insufficient games)...", "info")
        return None

    # Cleanup incomplete bot dirs from previous interrupted cycles
    _cleanup_incomplete()
    if shutdown_mgr and shutdown_mgr.is_shutting_down:
        return None

    # Load prev critic insights from archive
    prev_critic_info = ""
    try:
        from evolution_infra import RESULTS_DIR
        archive_dir = RESULTS_DIR / "archive"
        if archive_dir.exists():
            archives = sorted(archive_dir.glob("v*.json"), reverse=True)
            if archives:
                latest = json.loads(archives[0].read_text())
                critic_data = latest.get("critic_data", {})
                if critic_data:
                    sa = critic_data.get("strategic_assessment", "")
                    lo = critic_data.get("local_optima_warning", False)
                    if sa or lo:
                        prev_critic_info = f"Previous Critic assessment: {sa}"
                        if lo:
                            prev_critic_info += "\n⚠ LOCAL OPTIMA WARNING: Critic detected potential local optimum in previous generation."
    except Exception:
        pass

    # Combined analysis (stagnation + performance) + match analysis — run in parallel
    from combined_analyst import _run_combined_analysis
    from agent_master import _analyze_recent_matches

    combined_result, match_result = await asyncio.gather(
        _run_combined_analysis(active_v, active_bots, ratings, ui, prev_critic_info),
        _analyze_recent_matches(active_v, ui),
        return_exceptions=True,
    )

    if shutdown_mgr and shutdown_mgr.is_shutting_down:
        return None

    # Unpack results, treating exceptions as failures
    combined = combined_result if not isinstance(combined_result, BaseException) else None
    match_analysis = match_result if not isinstance(match_result, BaseException) else ""

    if isinstance(combined_result, BaseException):
        log.warning("Combined analysis failed: %s", combined_result)
    if isinstance(match_result, BaseException):
        log.warning("Match analysis failed: %s", match_result)

    # Strategy decision (code-layer, deterministic)
    strategy, source_v, parents = _decide_strategy(combined, active_v, ratings)

    # --- P1-1: Continuous Degeneration Diagnosis ---
    if combined and combined.get("trend") == "declining":
        try:
            from audit_agents import _run_degeneration_diagnosis
            from evolution_infra import _git
            # Build recent commit history
            recent_commits_text = ""
            try:
                import subprocess
                result = subprocess.run(
                    ["git", "log", f"bot-v{active_v}", "-5", "--format=%h %s%n%b"],
                    capture_output=True, text=True, timeout=10,
                    cwd=str(Path(__file__).resolve().parent.parent.parent),
                )
                if result.returncode == 0:
                    recent_commits_text = result.stdout.strip()[:3000]
            except Exception:
                pass

            # Build rating curve
            rating_curve_text = ""
            try:
                from evolution_infra import RATING_HISTORY_FILE
                if RATING_HISTORY_FILE.exists():
                    lines = RATING_HISTORY_FILE.read_text().strip().split('\n')
                    recent_lines = lines[-10:]
                    rating_curve_text = "\n".join(recent_lines)[:2000]
            except Exception:
                pass

            diag = await _run_degeneration_diagnosis(
                active_v, recent_commits_text, "See commits above", rating_curve_text, ui
            )
            if diag.get("urgent_intervention"):
                log_system_event("pipeline.urgent_degeneration", "error",
                                 f"Urgent degeneration detected for v{active_v}: {diag.get('root_causes', [])}",
                                 {"source_v": active_v, "diagnosis": diag})
                # Override strategy to crossover for recovery
                if strategy != "crossover":
                    strategy = "crossover"
                    log_system_event("pipeline.degeneration_strategy_override", "warn",
                                     f"Overriding strategy to crossover due to degeneration", {})
            elif diag.get("is_degenerating"):
                log_system_event("pipeline.degeneration_detected", "warn",
                                 f"Degeneration detected for v{active_v}: {diag.get('recommendation', '')}",
                                 {"source_v": active_v, "diagnosis": diag})
        except Exception as e:
            log.warning("Degeneration diagnosis error (skipping): %s", e)

    stagnation_text = json.dumps(combined, ensure_ascii=False) if combined else ""
    if combined and combined.get("is_stagnant"):
        stagnation_text = ("STAGNATION_DETECTED (is_stagnant=true): You MUST call run_literature_probe BEFORE run_master "
                           "(governance-gated; if it returns skipped:true, proceed to run_master).\n" + stagnation_text)
    perf_text = stagnation_text  # Combined result serves as both
    match_text = match_analysis or ""

    # --- Replay Spotlight Analysis ---
    spotlight_text = ""
    try:
        from replay_spotlight import find_critical_hands
        from evolution_infra import RESULTS_DIR
        replays_dir = str(RESULTS_DIR / "match_replay")
        spotlight_text = find_critical_hands(
            bot_name=f"claude_v{active_v}",
            replays_dir=replays_dir,
            max_hands=10,
            recent_n_files=20,
        )
    except Exception as e:
        log.warning("Replay spotlight analysis failed: %s", e)

    # --- P1-2: H2H Anomaly Root Cause Analysis ---
    # NOTE (root-cause-audit 2026-06-17): the stored `win_rate` field is the
    # win rate of the lexicographically-FIRST bot in the pair_key (see
    # elo_daemon pair_key: "a vs b" if a < b), NOT necessarily active_v's win
    # rate. Reading it directly inverts the sign when active_v is the "b" side
    # (e.g. "claude_v104 vs claude_v114" stores 0.35 = v104's rate, which was
    # mis-attributed to v114 as a fake regression). Fix: recompute active_v's
    # win rate from a_wins/b_wins by pair position — the same perspective
    # correction compute_h2h_avg_winrate (tool_helpers) already applies.
    if combined:
        try:
            from evolution_infra import H2H_FILE
            if H2H_FILE.exists():
                h2h_data = json.loads(H2H_FILE.read_text())
                regressions = []    # active_v LOSING — genuine concern for Master
                dominations = []    # active_v WINNING — informational only, NOT "attention"
                v_key = f"claude_v{active_v}"
                for pair_key, pair_data in h2h_data.items():
                    parts = pair_key.split(" vs ")
                    if len(parts) != 2 or v_key not in parts:
                        continue
                    games = pair_data.get("games", 0)
                    if games < 20:
                        continue
                    # Perspective-correct win rate for active_v.
                    if parts[0] == v_key:
                        bot_wins = pair_data.get("a_wins", 0)
                        opp = parts[1]
                    else:
                        bot_wins = pair_data.get("b_wins", 0)
                        opp = parts[0]
                    wr = bot_wins / games
                    delta = wr - 0.5
                    lb = _wilson_lower_bound(bot_wins, games)
                    entry = {
                        "opponent": opp,
                        "win_rate": round(wr, 3),
                        "games": games,
                        "delta": round(delta, 2),
                        "ci_lower": round(lb, 3),
                    }
                    # Single-sided regression: only flag matchups active_v is
                    # genuinely LOSING (wr < 0.40, i.e. delta < -0.10). The old
                    # abs(wr-0.5)>0.15 two-sided threshold treated "crushing a
                    # weak opponent" as a problem and, combined with the
                    # perspective bug, manufactured fake regressions. The Wilson
                    # lower bound is kept as a confidence column but not a hard
                    # gate (n=20 makes it too wide to discriminate — a 0.50
                    # point estimate already has lb≈0.30).
                    if wr < 0.40:
                        regressions.append(entry)
                    elif delta > 0.15:
                        dominations.append(entry)
                # Most-severe regressions first (lowest win rate), so the
                # top-5 surfaced to Master are the most diagnostic.
                regressions.sort(key=lambda a: a["win_rate"])
                if regressions:
                    log_system_event("pipeline.h2h_anomaly", "warn",
                                     f"H2H regressions for v{active_v}: {len(regressions)} "
                                     f"matchups with win rate < 40% (genuine losses); "
                                     f"{len(dominations)} strong dominations (informational)",
                                     {"source_v": active_v, "regressions": regressions[:5],
                                      "domination_count": len(dominations)})
                    # Inject into stagnation_text for Master context
                    anomaly_text = "\n\n## H2H Regression Alert\n"
                    for a in regressions[:5]:
                        anomaly_text += (
                            f"- vs {a['opponent']}: WR={a['win_rate']:.0%} "
                            f"(delta={a['delta']:+.0%}, {a['games']}g, "
                            f"95% CI lower={a['ci_lower']:.0%})\n"
                        )
                    anomaly_text += (
                        "These matchups show genuine regression — "
                        "prioritize fixing them in the next generation.\n"
                    )
                    stagnation_text += anomaly_text
        except Exception as e:
            log.warning("H2H anomaly check error (skipping): %s", e)

    battle_experience_text = ""
    try:
        from battle_experience import get_battle_experience
        battle_experience_text = get_battle_experience()
    except Exception as e:
        log.warning("Battle experience read failed: %s", e)

    # Phase 4: 10%-elite periodic re-evaluation (QD diversity housekeeping).
    # Every QD_REEVAL_EVERY generations, mark the top-fitness niche occupants for
    # daemon single-eval re-evaluation by writing them into the priority eval
    # queue. This prevents stale-elite lock-in (a niche occupied by a bot whose
    # fitness was measured long ago). NO fire-and-forget here — the daemon is
    # already asynchronous and consumes priority_eval.json on its own loop.
    # Best-effort: any failure is observed and swallowed (idempotent phase).
    try:
        from qd_fitness import QD_REEVAL_EVERY, reevaluate_top_elites
        next_v_planned = max(current_v, max_committed_v) + 1
        if next_v_planned > 0 and next_v_planned % QD_REEVAL_EVERY == 0:
            from map_elites import read_behavior_archive
            archive = read_behavior_archive()
            elites = reevaluate_top_elites(archive)
            if elites:
                log.info("QD elite re-eval: %d elites queued (%s)",
                         len(elites), elites[:3])
                log_system_event(
                    "pipeline.qd_elite_reeval", "info",
                    f"v{next_v_planned}: {len(elites)} elites queued for re-eval",
                    {"version": next_v_planned, "elites": elites},
                )
    except Exception as e:
        log.warning("QD elite re-eval trigger failed (non-fatal): %s", e)

    # LOG GAP FIX (2026-06-29): record the final next_v decision with all inputs
    # so the version-number allocation is fully auditable. Previously only the
    # abnormal paths (bare commit, abandoned floor) logged; the normal case left
    # no trace of how next_v was computed. This is only a scheduler selection,
    # not proof that prepare_next_gen/run_crossover has materialized the bot dir.
    _final_next_v = _planned_next_v
    try:
        log_system_event(
            "pipeline.generation_selected", "info",
            f"Selected v{_final_next_v} from v{source_v} (strategy={strategy[:40]})",
            {"next_v": _final_next_v, "current_v": current_v,
             "max_committed_v": max_committed_v,
             "abandoned_floor": _abandoned_floor,
             "source_v": source_v, "strategy": strategy[:80],
             "selection_stage": "selected",
             "next_step": "prepare_next_gen_or_run_crossover"},
        )
    except Exception:
        pass

    return GenerationContext(
        current_v=current_v,
        next_v=_final_next_v,
        strategy=strategy,
        source_v=source_v,
        crossover_parents=parents,
        stagnation_info=stagnation_text,
        match_analysis=match_text,
        performance_verification=perf_text,
        replay_spotlight=spotlight_text,
        gen_count=current_v,
        battle_experience=battle_experience_text,
    )


def _log_crossover_decision(trigger, source_v, parents, cons_a=None, cons_b=None):
    """LOG GAP FIX (2026-06-30): record WHY crossover was chosen + which parents,
    so the parent-selection rationale is auditable (previously only the result was
    logged via pipeline.generation_selected's strategy field)."""
    try:
        parent_a_metrics = _strength_payload(parents[0])
        parent_b_metrics = _strength_payload(parents[1])
        log_system_event(
            "pipeline.crossover_decided", "info",
            f"Crossover decided (trigger={trigger}): v{parents[0]}×v{parents[1]} "
            f"(source v{source_v})",
            {"trigger": trigger, "source_v": source_v,
             "parent_a": parents[0], "parent_b": parents[1],
             "version_gap": abs(parents[0] - parents[1]),
             "conservative_a": round(cons_a, 0) if cons_a else None,
             "conservative_b": round(cons_b, 0) if cons_b else None,
             "parent_a_metrics": parent_a_metrics,
             "parent_b_metrics": parent_b_metrics},
        )
    except Exception:
        pass


def _strength_payload(version):
    name = f"claude_v{version}"
    try:
        from tool_helpers import load_h2h_avg_winrates_with_coverage, load_strength_scores
        coverage = load_h2h_avg_winrates_with_coverage().get(name, {})
        scores = load_strength_scores()
        return {
            "bot": name,
            "leaderboard_score": round(scores.get(name, 0.0), 4),
            "h2h_avg_wr": round(coverage.get("h2h_avg_wr", 0.0), 4),
            "h2h_coverage": round(coverage.get("opponent_coverage", 0.0), 4),
            "h2h_games": coverage.get("h2h_games", 0),
            "h2h_source": coverage.get("h2h_source", ""),
            "rank_basis": coverage.get("rank_basis", ""),
            "strength_confidence": coverage.get("strength_confidence", "low"),
        }
    except Exception:
        return {"bot": name}


def _log_source_selection_decision(trigger, selected_v, current_v, combined=None):
    try:
        log_system_event(
            "pipeline.source_selection_decided",
            "info",
            f"Source selected by {trigger}: v{selected_v} (latest v{current_v})",
            {
                "trigger": trigger,
                "selected_source_v": selected_v,
                "current_v": current_v,
                "llm_recommended_source": (combined or {}).get("recommended_source"),
                "source_rationale": (combined or {}).get("source_rationale"),
                "selected_metrics": _strength_payload(selected_v),
                "current_metrics": _strength_payload(current_v),
            },
        )
    except Exception:
        pass


def _decide_strategy(combined, current_v, ratings):
    """Deterministic strategy selection based on combined analysis results.

    The combined analysis merges stagnation and performance data into one dict:
    - is_stagnant + confidence → branch or crossover
    - diversity_needed → crossover injection
    - recommendation + branch_from → branch from specific ancestor
    """
    if combined is None:
        return "master", current_v, ()

    # Load behavior archive for niche-diverse crossover parent selection (fix-6)
    _archive = None
    try:
        from behavior_diversity import load_fingerprints
        _archive = load_fingerprints()
    except Exception:
        pass

    # B-class control-flow guard: if the Combined Analyst's LLM call crashed
    # (infrastructure failure, NOT a business judgement), stagnation status is
    # UNKNOWN. The combined result's safe default claims "improving / not
    # stagnant", but that is a guess — we must NOT act on it. In particular we
    # must avoid misfiring the crossover/stagnation branches (which assume a
    # trustworthy stagnation signal) and also avoid misreading the optimistic
    # default. Fall back to a conservative master evolution from current_v with
    # no crossover parents. The cross-gen mechanical backstop
    # (_build_cross_gen_constraint_block in run_master) still runs and provides
    # diversity protection independent of this LLM gate.
    if combined.get("llm_failed"):
        log.warning(
            "Combined analyst reported LLM infrastructure failure — stagnation "
            "unknown. Proceeding conservatively with master from v%d (no crossover).",
            current_v,
        )
        try:
            log_system_event(
                "pipeline.combined_analyst_infra", "warn",
                f"Stagnation analysis unavailable for v{current_v} (LLM infra error). "
                "Master proceeding, confidence=low — no crossover triggered.",
                {"source_v": current_v},
            )
        except Exception:
            pass
        return "master", current_v, ()

    # Source-v loop detection: if recent generations all branched from the same
    # ancestor (typically because LLM analysis anchors on a "stable" intermediate),
    # force branching from the Glicko-rated leader instead.
    _source_loop = _detect_source_loop(n=3)
    if _source_loop:
        leader_v = _get_unified_leader_v(ratings)
        if leader_v is not None and leader_v != _source_loop:
            log.warning(
                "Source-v loop detected (last 3+ gens from v%d). "
                "Forcing source_v=%d (unified selection leader) to break the loop.",
                _source_loop, leader_v,
            )
            _log_source_selection_decision("source_loop_unified_leader", leader_v, current_v, combined)
            return "master", leader_v, ()

    # Source-v oscillation detection: if recent gens cycle among a small set
    # of ancestors, force crossover between the highest and lowest rated bots
    # from that oscillating set to break out of the cycle.
    oscillating = _detect_source_oscillation(n=8, max_unique=3)
    if oscillating:
        # Find highest and lowest rated bots within the oscillating set, using the
        # conservative rating (r - 2*rd) so RD-inflated point estimates don't bias
        # which bots are treated as "strongest"/"weakest" crossover parents.
        osc_ratings = {}
        for sv in oscillating:
            bot_key = f"claude_v{sv}"
            if bot_key in ratings:
                osc_ratings[sv] = ratings[bot_key].conservative_rating()
        # E2: convergence guard. If the Glicko leader (strongest active bot by
        # conservative rating) is itself inside the oscillating set, the lineage
        # has converged ONTO an elite ancestor rather than truly oscillating
        # without progress — forcing crossover here would blow apart a winning
        # lineage (BUG2). Only force crossover when none of the recurring sources
        # is the current leader, i.e. genuine stuckness on weaker ancestors.
        leader_v = _get_unified_leader_v(ratings)
        force_oscillation_crossover = True
        if leader_v is not None and leader_v in osc_ratings:
            force_oscillation_crossover = False
            log.info(
                "Source-v oscillation suppressed: leader v%d (%.0f cons) is within the "
                "recurring set %s — treating as convergence, not oscillation (E2).",
                leader_v, osc_ratings[leader_v], sorted(oscillating),
            )
        elif combined.get("is_stagnant") and combined.get("confidence") != "low":
            force_oscillation_crossover = False
            log.info(
                "Source-v oscillation detected but deferred to the normal stagnation "
                "crossover selector; recurring set=%s.",
                sorted(oscillating),
            )
            try:
                log_system_event(
                    "pipeline.source_oscillation_deferred",
                    "info",
                    "Source-v oscillation deferred to stagnation crossover selector",
                    {
                        "oscillating_sources": sorted(oscillating),
                        "current_v": current_v,
                        "confidence": combined.get("confidence"),
                    },
                )
            except Exception:
                pass
        else:
            breakout = _pick_oscillation_breakout_source(oscillating, current_v)
            if breakout:
                selected_v = breakout["version"]
                log.info(
                    "Source-v oscillation broken by credible outside source v%d "
                    "(selection=%.4f, confidence=%s, osc_best=%.4f).",
                    selected_v,
                    breakout["selection_score"],
                    breakout["strength_confidence"],
                    breakout["osc_best_score"],
                )
                try:
                    log_system_event(
                        "pipeline.source_oscillation_breakout",
                        "info",
                        (
                            f"Source-v oscillation broken by v{selected_v} "
                            f"(selection={breakout['selection_score']:.4f})"
                        ),
                        {
                            "selected_source_v": selected_v,
                            "current_v": current_v,
                            "oscillating_sources": sorted(oscillating),
                            "selection_score": round(breakout["selection_score"], 4),
                            "strength_confidence": breakout["strength_confidence"],
                            "osc_best_score": round(breakout["osc_best_score"], 4),
                            "score_margin": round(breakout["score_margin"], 4),
                            "basis": breakout["basis"],
                        },
                    )
                except Exception:
                    pass
                _log_source_selection_decision(
                    "source_oscillation_breakout", selected_v, current_v, combined
                )
                return "master", selected_v, ()
        if force_oscillation_crossover and len(osc_ratings) >= 2:
            highest_v = max(osc_ratings, key=osc_ratings.get)
            lowest_v = min(osc_ratings, key=osc_ratings.get)
            if highest_v != lowest_v:
                log.warning(
                    "Source-v oscillation: forcing crossover between highest-rated v%d (%.0f cons) "
                    "and lowest-rated v%d (%.0f cons) from oscillating set %s",
                    highest_v, osc_ratings[highest_v],
                    lowest_v, osc_ratings[lowest_v],
                    sorted(oscillating),
                )
                _log_crossover_decision("oscillation", highest_v, (highest_v, lowest_v),
                                        osc_ratings.get(highest_v), osc_ratings.get(lowest_v))
                return "crossover", highest_v, (highest_v, lowest_v)

    # Priority 1: Stagnation with high/medium confidence → crossover
    # This is the PRIMARY escape hatch from local optima — must fire before
    # recommended_source so stagnation always triggers diversity injection.
    if combined.get("is_stagnant") and combined.get("confidence") != "low":
        parents = _pick_crossover_parents(ratings, current_v, archive=_archive)
        if parents:
            _log_crossover_decision("stagnation", parents[0], parents)
            return "crossover", parents[0], parents

    # Priority 2: LLM-recommended source (only for non-stagnant systems).
    # Validates that the recommended bot is active (not in graveyard).
    rec_source = combined.get("recommended_source", "")
    if rec_source:
        rec_v = _parse_branch_from(rec_source)
        if rec_v is not None and rec_v >= 1:
            from evolution_infra import get_active_bots, get_bot_dir
            # Only accept active bots (not graveyard) as evolution source
            active = get_active_bots()
            if f"claude_v{rec_v}" in active:
                if rec_v != current_v:
                    rationale = combined.get("source_rationale", "")
                    log.info("LLM recommended source: v%d (instead of latest v%d). %s",
                             rec_v, current_v, rationale[:200])
                _log_source_selection_decision("llm_recommended_source", rec_v, current_v, combined)
                return "master", rec_v, ()

    # Priority 3: Explicit branch recommendation
    if combined.get("recommendation") == "branch" and combined.get("branch_from"):
        branch_v = _parse_branch_from(combined["branch_from"])
        if branch_v is not None and branch_v >= 1:
            _log_source_selection_decision("branch_recommendation", branch_v, current_v, combined)
            return "master", branch_v, ()

    # Priority 4: Diversity injection
    if combined.get("diversity_needed"):
        parents = _pick_crossover_parents(ratings, current_v, archive=_archive)
        if parents:
            log.info("Diversity injection: forcing crossover (%s, %s) to break local optimum",
                     f"v{parents[0]}", f"v{parents[1]}")
            _log_crossover_decision("diversity", parents[0], parents)
            return "crossover", parents[0], parents

    # Fallback: LLM did not recommend a source, use current_v
    _log_source_selection_decision("latest_fallback", current_v, current_v, combined)
    return "master", current_v, ()


def _parse_branch_from(branch_str: str) -> int | None:
    try:
        return int(branch_str)
    except ValueError:
        pass
    try:
        return int(branch_str.split("_v")[1])
    except (ValueError, IndexError):
        pass
    try:
        return int(branch_str.lstrip("v"))
    except (ValueError, IndexError):
        return None


def _read_source_v_history():
    """Read successful lineage source_v values from system_events.jsonl.

    Prefer ``pipeline.committed`` because source oscillation is about the lineage
    that actually survived gates. ``pipeline.prepare_done`` is only a fallback for
    very old logs without commit events; prepare attempts can be duplicated by
    restarts or abandoned before commit and should not dominate lineage analysis.

    Returns a list of source_v values in chronological order.
    """
    try:
        if not SYSTEM_EVENTS_FILE.exists():
            return []
        committed = []
        prepared = []
        with open(SYSTEM_EVENTS_FILE, "r") as f:
            for line in f:
                try:
                    evt = json.loads(line)
                    data = evt.get("data", {}) or {}
                    evt_type = evt.get("type")
                    if evt_type == "pipeline.committed":
                        sv = data.get("source_v")
                        if sv is not None:
                            committed.append((evt.get("ts", 0), data.get("version", 0), int(sv)))
                    elif evt_type == "pipeline.prepare_done":
                        sv = data.get("source_v")
                        if sv is not None:
                            prepared.append((evt.get("ts", 0), data.get("next_v", 0), int(sv)))
                except (ValueError, KeyError):
                    continue
        if committed:
            committed.sort(key=lambda item: (item[0], item[1]))
            return [sv for _ts, _version, sv in committed]
        prepared.sort(key=lambda item: (item[0], item[1]))
        return [sv for _ts, _version, sv in prepared]
    except Exception:
        return []


def _detect_source_loop(n=3):
    """Check if the last n generations all used the same source_v.

    Returns the repeated source_v if a loop is detected, None otherwise.
    """
    try:
        sources = _read_source_v_history()
        if not sources:
            return None
        # Check last n entries
        recent = sources[-(n + 1):] if len(sources) >= n + 1 else sources[-n:] if len(sources) >= n else []
        if len(recent) >= n and len(set(recent)) == 1:
            return recent[0]
    except Exception:
        pass
    return None


def _detect_source_oscillation(n=8, max_unique=3):
    """Check if recent generations oscillate among a small set of source_v values.

    If the unique count among the last n source_v values is max_unique or fewer,
    the system is oscillating — repeatedly switching between the same small set
    of ancestors without convergence.

    Returns the set of oscillating source_v values if detected, None otherwise.
    """
    try:
        sources = _read_source_v_history()
        if not sources:
            return None
        recent = sources[-n:]
        if len(recent) < max_unique + 1:
            return None  # Not enough data to detect oscillation
        unique_sources = set(recent)
        if len(unique_sources) <= max_unique:
            log.warning("Source-v oscillation detected: last %d gens used only %d unique sources: %s",
                        len(recent), len(unique_sources), sorted(unique_sources))
            return unique_sources
    except Exception:
        pass
    return None


def _get_unified_leader_v(ratings):
    """Return the version number of the strongest active bot for source repair.

    Prefer the confidence-discounted ``selection_score`` used by the dashboard
    and crossover/precommit mechanics. Fall back to conservative Glicko
    (r - 2*rd) if the unified snapshot is unavailable, so source-loop recovery
    still works during partial data or cache failures.
    """
    if not ratings:
        return None
    try:
        from tool_helpers import load_selection_scores
        selection_scores = load_selection_scores()
    except Exception:
        selection_scores = {}

    def _score(name):
        raw = selection_scores.get(name)
        if raw is not None:
            try:
                return float(raw)
            except (TypeError, ValueError):
                pass
        rating = ratings.get(name)
        if rating is None:
            return float("-inf")
        try:
            return max(0.0, min(1.0, 0.5 + (rating.conservative_rating() - 1500.0) / 800.0))
        except Exception:
            return float("-inf")

    best_bot = max(ratings, key=lambda b: (_score(b), _parse_branch_from(b) or -1))
    try:
        return int(best_bot.split("_v")[1])
    except (ValueError, IndexError):
        return None


def _pick_oscillation_breakout_source(oscillating: set[int], current_v: int) -> dict | None:
    """Pick a credible source outside an oscillating ancestor set.

    The oscillation backstop is supposed to break stale source loops, not erase a
    newly validated elite. Use the same confidence-discounted selection score
    exposed to the dashboard and evolution mechanics. When several credible bots
    are effectively tied for first, prefer the newest version so the system keeps
    moving forward instead of snapping back to an old historical champion.
    """
    try:
        from tool_helpers import load_h2h_avg_winrates_with_coverage

        metrics = load_h2h_avg_winrates_with_coverage()
    except Exception:
        return None

    if not metrics:
        return None

    def _score(data: dict) -> float:
        raw = data.get("selection_score", data.get("leaderboard_score", 0.0))
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0

    osc_scores = []
    for sv in oscillating:
        osc_metrics = metrics.get(f"claude_v{sv}")
        if osc_metrics:
            osc_scores.append(_score(osc_metrics))
    if not osc_scores:
        return None
    osc_best = max(osc_scores)

    candidates = []
    for name, data in metrics.items():
        version = _parse_branch_from(name)
        if version is None or version in oscillating:
            continue
        confidence = data.get("strength_confidence", "low")
        if confidence == "low":
            continue
        score = _score(data)
        if score < osc_best + OSCILLATION_BREAKOUT_MIN_MARGIN:
            continue
        candidates.append({
            "version": version,
            "selection_score": score,
            "strength_confidence": confidence,
            "osc_best_score": osc_best,
            "score_margin": score - osc_best,
            "basis": data.get("rank_basis", ""),
        })

    if not candidates:
        return None

    best_score = max(c["selection_score"] for c in candidates)
    near_best = [
        c for c in candidates
        if c["selection_score"] >= best_score - OSCILLATION_BREAKOUT_SCORE_TOLERANCE
    ]
    for candidate in near_best:
        if candidate["version"] == current_v:
            return candidate
    return max(near_best, key=lambda c: (c["version"], c["selection_score"]))


def _pick_crossover_parents(ratings, current_v, archive=None) -> tuple | None:
    """Select two diverse parents for crossover.

    Parent A: highest unified strength score.
    Parent B: highest strength score from a different niche than parent A (if
    archive is available), with version gap >= 3. Falls back to second-highest
    strength score if no niche-diverse candidate exists.

    Args:
        ratings: Glicko-2 ratings dict.
        current_v: Current version number.
        archive: Optional fingerprint archive (dict[str, np.ndarray]).
            When provided, parent_b is chosen from a different behavioral niche
            to maximize crossover diversity (fix-6).
    """
    from evolution_infra import get_active_bots
    from tool_helpers import load_selection_scores

    active = get_active_bots()
    if len(active) < 2:
        return None
    strength = load_selection_scores()
    ranked = sorted(
        active,
        key=lambda b: strength.get(b, 0.0),
        reverse=True,
    )
    if len(ranked) < 2:
        return None

    parent_a = ranked[0]
    try:
        va = int(parent_a.split("_v")[1])
    except (ValueError, IndexError):
        return None

    # fix-6: Prefer niche-diverse parent_b if archive is available
    parent_b = None
    if archive:
        try:
            from behavior_diversity import get_niche_for_bot
            parent_a_niche = get_niche_for_bot(parent_a, archive)
            if parent_a_niche is not None:
                niche_candidates = []
                for candidate in ranked[1:]:
                    try:
                        vc = int(candidate.split("_v")[1])
                    except (ValueError, IndexError):
                        continue
                    if abs(vc - va) < 3:
                        continue
                    cand_niche = get_niche_for_bot(candidate, archive)
                    if cand_niche is not None and cand_niche != parent_a_niche:
                        niche_candidates.append(candidate)
                if niche_candidates:
                    parent_b = niche_candidates[0]  # already sorted by h2h
        except Exception as e:
            log.warning("Niche-diverse parent selection failed (falling back): %s", e)

    # Original logic: find diverse parent B with version gap >= 3
    if parent_b is None:
        for candidate in ranked[1:]:
            try:
                vc = int(candidate.split("_v")[1])
            except (ValueError, IndexError):
                continue
            if abs(vc - va) >= 3:
                parent_b = candidate
                break

    # Fallback: second highest if no gap candidate
    if parent_b is None:
        parent_b = ranked[1]

    try:
        vb = int(parent_b.split("_v")[1])
        return (va, vb)
    except (ValueError, IndexError):
        return None


def _finalize_bare_commit(v, ckpt=None):
    """H3 (2026-06-29): finalize a bare-committed generation.

    A bare commit (code landed via `git commit` but no bot-v{N} tag and no
    .completed sentinel) happens when CYCLE_TIMEOUT/503 interrupts commit_bot
    mid-way (e.g. crossover's git_commit_bot ran the commit but not the tag).
    Previously `_cleanup_incomplete` would rmtree such a dir on the next restart,
    silently destroying committed code. This helper re-runs the idempotent
    git_commit_bot to retroactively tag + mark .completed, so the generation is
    recovered instead of lost.

    Returns True if finalized (or already finalized), False if it could not be
    finalized (caller should leave the dir intact — git history still holds it).
    """
    try:
        from evolution_infra import git_has_tag, git_commit_bot
        from tool_commit import get_bot_dir, RESULTS_DIR
    except Exception as e:
        log.warning("_finalize_bare_commit imports failed for v%d: %s", v, e)
        return False
    if git_has_tag(v):
        return True  # already tagged by a prior finalize or normal commit
    bot_dir = get_bot_dir(v)
    if not bot_dir.exists():
        return False
    source_v = (ckpt or {}).get("source_v")
    parent2_v = (ckpt or {}).get("parent2_v")
    if source_v is None:
        log.warning(
            "bare-commit v%d has no source_v in checkpoint — leaving dir intact "
            "(git history preserves it), not finalizing.", v)
        return False
    strategy = ((ckpt or {}).get("master_plan") or {}).get("strategy_summary") \
        or ((ckpt or {}).get("master_plan") or {}).get("strategy") \
        or f"bare-commit recovery for v{v}"
    try:
        git_commit_bot(v, source_v, strategy, rating_info="", parent2_v=parent2_v)
        if not git_has_tag(v):
            log.warning("finalize for v%d ran git_commit_bot but tag still absent — leaving dir.", v)
            return False
        (bot_dir / ".completed").touch()
        log.info("H3: finalized bare-commit v%d (source v%d, parent2=%s)", v, source_v, parent2_v)
        try:
            log_system_event("pipeline.bare_commit_finalized", "success",
                             f"Recovered bare-commit v{v} via finalize (source v{source_v})",
                             {"version": v, "source_v": source_v, "parent2_v": parent2_v})
        except Exception:
            pass
        return True
    except Exception as e:
        log.warning("H3: finalize failed for v%d (%s) — leaving dir intact", v, e)
        return False


def _cleanup_incomplete():
    """Remove incomplete bot directories that have no git tag and no active checkpoint."""
    import shutil
    from evolution_infra import PROJECT_ROOT, git_has_tag, git_dir_is_committed, RESULTS_DIR

    bots_dir = PROJECT_ROOT / "bots"
    if not bots_dir.exists():
        return
    for d in sorted(bots_dir.iterdir()):
        if d.is_dir() and d.name.startswith("claude_v"):
            if not (d / ".completed").exists():
                try:
                    v = int(d.name.split("_v")[1])
                except (ValueError, IndexError):
                    continue
                if not git_has_tag(v):
                    # H3 (2026-06-29): a bare-commit dir (git-tracked files but no
                    # tag) is committed code interrupted mid-finalize. rmtree here
                    # would destroy it. Attempt finalize instead; only rmtree if
                    # finalize cannot recover AND there's no active checkpoint.
                    if git_dir_is_committed(v):
                        _ckpt = None
                        checkpoint_file = RESULTS_DIR / "pipeline_state.json"
                        if checkpoint_file.exists():
                            try:
                                _ckpt = json.loads(checkpoint_file.read_text())
                            except Exception:
                                _ckpt = None
                        finalized = _finalize_bare_commit(v, _ckpt)
                        if finalized:
                            continue
                        # Could not finalize — preserve git-tracked dir (history
                        # still holds it). Do NOT rmtree committed code.
                        log.warning(
                            "H3: preserving bare-commit v%d dir (git-tracked, no tag, "
                            "finalize failed) — not removing committed code.", v)
                        continue
                    # Skip if there's an active pipeline checkpoint for this version
                    checkpoint_file = RESULTS_DIR / "pipeline_state.json"
                    if checkpoint_file.exists():
                        try:
                            ckpt = json.loads(checkpoint_file.read_text())
                            if ckpt.get("next_v") == v and ckpt.get("stage") not in (None, "archived"):
                                continue
                        except Exception:
                            pass
                    shutil.rmtree(d, ignore_errors=True)


def _cleanup_dirty_paths() -> set[str]:
    """Return porcelain dirty paths before cleanup writes."""
    try:
        from evolution_infra import _git

        out = _git("status", "--porcelain", check=False)
    except Exception:
        return set()

    paths: set[str] = set()
    for line in out.splitlines():
        if not line:
            continue
        path = line[3:] if len(line) > 3 else line
        if " -> " in path:
            old, new = path.split(" -> ", 1)
            paths.add(old.strip())
            paths.add(new.strip())
        else:
            paths.add(path.strip())
    return paths


def _commit_post_cleanup_experience_change(version: int, preexisting_dirty: set[str]) -> dict:
    """Commit post-cleanup experience_pool consolidation as scoped housekeeping."""
    from evolution_infra import EXPERIENCE_FILE, PROJECT_ROOT, _git, _git_ensure_main_branch

    try:
        rel = str(EXPERIENCE_FILE.relative_to(PROJECT_ROOT))
    except ValueError:
        rel = str(EXPERIENCE_FILE)

    if preexisting_dirty:
        log_system_event(
            "pipeline.post_cleanup_experience_commit_skipped",
            "warn",
            f"v{version}: skipped experience consolidation commit because worktree was already dirty",
            {"version": version, "preexisting_dirty": sorted(preexisting_dirty)[:40], "path": rel},
        )
        return {"committed": False, "reason": "preexisting_dirty", "path": rel}

    dirty_now = _git("status", "--porcelain", "--", rel, check=False).strip()
    if not dirty_now:
        return {"committed": False, "reason": "no_change", "path": rel}

    preexisting_staged = [
        p for p in _git("diff", "--cached", "--name-only", check=False).splitlines()
        if p
    ]
    if preexisting_staged:
        log_system_event(
            "pipeline.post_cleanup_experience_commit_skipped",
            "warn",
            f"v{version}: skipped experience consolidation commit because staged files already exist",
            {"version": version, "staged_files": preexisting_staged[:40], "path": rel},
        )
        return {"committed": False, "reason": "preexisting_staged", "path": rel}

    _git_ensure_main_branch()
    _git("add", "--", rel, check=False)
    staged = [
        p for p in _git("diff", "--cached", "--name-only", check=False).splitlines()
        if p
    ]
    unexpected = sorted(set(staged) - {rel})
    if unexpected:
        _git("restore", "--staged", "--", rel, check=False)
        log_system_event(
            "pipeline.post_cleanup_experience_commit_skipped",
            "warn",
            f"v{version}: skipped experience consolidation commit because unrelated staged files appeared",
            {"version": version, "unexpected_staged": unexpected[:40], "path": rel},
        )
        return {"committed": False, "reason": "unexpected_staged", "path": rel}

    log_system_event(
        "pipeline.post_cleanup_experience_commit_staged",
        "info",
        f"v{version}: staging post-cleanup experience consolidation",
        {"version": version, "staged_files": [rel]},
    )
    _git("commit", "-m", f"chore: consolidate v{version} experience pool", "--", rel)
    commit_hash = _git("rev-parse", "--short", "HEAD", check=False).strip()
    log_system_event(
        "pipeline.post_cleanup_experience_commit_done",
        "success",
        f"v{version}: committed post-cleanup experience consolidation {commit_hash}",
        {"version": version, "commit": commit_hash, "path": rel},
    )
    return {"committed": True, "commit": commit_hash, "path": rel}


async def post_generation_cleanup(shutdown_mgr, ui, ctx: GenerationContext):
    """Phase 3: Idempotent post-generation cleanup."""
    from evolution_infra import MAX_ACTIVE_BOTS, get_active_bots

    cleanup_started = time.time()

    def _finish(status: str = "done", reason: str = ""):
        elapsed = time.time() - cleanup_started
        severity = "info" if status in {"done", "skipped"} else "warn"
        log_system_event(
            "pipeline.post_cleanup_done",
            severity,
            f"Post-cleanup {status} for v{ctx.next_v} in {elapsed:.1f}s",
            {
                "version": ctx.next_v,
                "source_v": ctx.source_v,
                "status": status,
                "reason": reason,
                "elapsed_sec": round(elapsed, 2),
            },
        )

    log_system_event(
        "pipeline.post_cleanup_start",
        "info",
        f"Post-cleanup starting for v{ctx.next_v}",
        {"version": ctx.next_v, "source_v": ctx.source_v, "strategy": ctx.strategy},
    )

    if shutdown_mgr and shutdown_mgr.is_shutting_down:
        _finish("skipped", "shutdown_before_start")
        return

    # Auto-reap if pool exceeds limit
    active_bots = get_active_bots()
    if len(active_bots) > MAX_ACTIVE_BOTS:
        log_system_event(
            "pipeline.post_cleanup_reap_start",
            "info",
            f"Auto-reap starting: {len(active_bots)} active bot(s)",
            {"version": ctx.next_v, "active_count": len(active_bots), "max_active": MAX_ACTIVE_BOTS},
        )
        try:
            from tool_bot_management import _do_reap_weakest
            reap_count = 0
            while len(get_active_bots()) > MAX_ACTIVE_BOTS and reap_count < 10:
                result = await _do_reap_weakest(quiet=True)
                if not result.get("reaped"):
                    break
                reap_count += 1
            log_system_event(
                "pipeline.post_cleanup_reap_done",
                "info",
                f"Auto-reap finished: reaped {reap_count} bot(s)",
                {"version": ctx.next_v, "reap_count": reap_count,
                 "active_count": len(get_active_bots())},
            )
        except Exception as e:
            log.warning("Auto-reap failed: %s\n%s", e, traceback.format_exc())
            log_system_event(
                "pipeline.post_cleanup_reap_failed",
                "warn",
                f"Auto-reap failed: {str(e)[:180]}",
                {"version": ctx.next_v, "error": str(e)[:500]},
            )
            if ui:
                ui.log_history(f"Auto-reap failed: {e}", "warn")

    if shutdown_mgr and shutdown_mgr.is_shutting_down:
        _finish("skipped", "shutdown_after_reap")
        return

    # Experience pool consolidation (every 3 generations, or when too many unconsolidated entries)
    should_consolidate = ctx.next_v > 0 and ctx.next_v % 3 == 0
    consolidation_reason = "multiple_of_3" if should_consolidate else ""
    if not should_consolidate:
        # Also trigger when RECENT_LESSONS has too many entries (prevents stale/contradictory data)
        from evolution_infra import EXPERIENCE_FILE
        if EXPERIENCE_FILE.exists():
            try:
                content = EXPERIENCE_FILE.read_text()
                recent_section = content.split("## RECENT_LESSONS")[-1] if "## RECENT_LESSONS" in content else ""
                recent_entries = [line for line in recent_section.split("\n")
                                  if line.strip().startswith("- **")]
                if len(recent_entries) >= 4:
                    should_consolidate = True
                    consolidation_reason = "recent_lessons_threshold"
                    log.info("Triggering experience consolidation: %d RECENT_LESSONS entries (threshold: 4)",
                             len(recent_entries))
            except Exception:
                pass

    if should_consolidate:
        try:
            from evolution_infra import git_has_tag
            if not git_has_tag(ctx.next_v):
                try:
                    log_system_event(
                        "pipeline.experience_write_blocked_uncommitted", "warn",
                        f"Skipped experience consolidation for uncommitted v{ctx.next_v}",
                        {"version": ctx.next_v, "writer": "post_generation_cleanup"},
                    )
                except Exception:
                    pass
                _finish("skipped", "experience_uncommitted")
                return
            from experience_archivist import _consolidate_experience_pool
            preexisting_dirty = _cleanup_dirty_paths()
            # Extract exhausted_directions from pipeline checkpoint
            exhausted_dirs = ""
            try:
                from evolution_infra import read_pipeline_checkpoint
                ckpt = read_pipeline_checkpoint()
                if ckpt:
                    da = ckpt.get("direction_audit", {})
                    ed = da.get("exhausted_directions", [])
                    if ed:
                        exhausted_dirs = ", ".join(ed)
            except Exception:
                pass
            exp_started = time.time()
            log_system_event(
                "pipeline.post_cleanup_experience_start",
                "info",
                f"v{ctx.next_v}: experience consolidation starting",
                {
                    "version": ctx.next_v,
                    "source_v": ctx.source_v,
                    "reason": consolidation_reason or "unknown",
                    "timeout_s": POST_CLEANUP_EXPERIENCE_TIMEOUT,
                    "preexisting_dirty_count": len(preexisting_dirty),
                    "exhausted_directions": exhausted_dirs,
                },
            )
            await asyncio.wait_for(
                _consolidate_experience_pool(ui, exhausted_directions=exhausted_dirs),
                timeout=POST_CLEANUP_EXPERIENCE_TIMEOUT,
            )
            log_system_event(
                "pipeline.post_cleanup_experience_done",
                "info",
                f"v{ctx.next_v}: experience consolidation finished in {time.time() - exp_started:.1f}s",
                {
                    "version": ctx.next_v,
                    "source_v": ctx.source_v,
                    "elapsed_sec": round(time.time() - exp_started, 2),
                },
            )
            try:
                _commit_post_cleanup_experience_change(ctx.next_v, preexisting_dirty)
            except Exception as e:
                log_system_event(
                    "pipeline.post_cleanup_experience_commit_failed",
                    "error",
                    f"v{ctx.next_v}: experience consolidation commit failed: {str(e)[:180]}",
                    {"version": ctx.next_v, "error": str(e)[:500]},
                )
        except asyncio.TimeoutError:
            log_system_event(
                "pipeline.post_cleanup_experience_timeout",
                "warn",
                f"v{ctx.next_v}: experience consolidation exceeded {POST_CLEANUP_EXPERIENCE_TIMEOUT}s; continuing",
                {
                    "version": ctx.next_v,
                    "source_v": ctx.source_v,
                    "timeout_s": POST_CLEANUP_EXPERIENCE_TIMEOUT,
                    "reason": consolidation_reason or "unknown",
                },
            )
            if ui:
                ui.log_history(
                    f"Experience consolidation timed out after {POST_CLEANUP_EXPERIENCE_TIMEOUT}s; continuing.",
                    "warn",
                )
        except Exception as e:
            log_system_event(
                "pipeline.post_cleanup_experience_failed",
                "warn",
                f"v{ctx.next_v}: experience consolidation failed: {str(e)[:180]}",
                {"version": ctx.next_v, "source_v": ctx.source_v, "error": str(e)[:500]},
            )
            if ui:
                ui.log_history(f"Experience consolidation failed: {e}", "warn")
    else:
        log_system_event(
            "pipeline.post_cleanup_experience_skipped",
            "info",
            f"v{ctx.next_v}: experience consolidation skipped",
            {"version": ctx.next_v, "source_v": ctx.source_v, "reason": "threshold_not_met"},
        )

    # Exploitability probes against the new bot (FIRE-AND-FORGET background).
    #
    # This block is a post-commit side-effect (bot already committed/tagged),
    # code-layer housekeeping driven by post_generation_cleanup — NOT an MCP
    # tool and NOT a commit gate. Result feeds the NEXT generation's Master
    # prompt via exploitability.json (consumed in tool_planning.run_master).
    # It MUST stay a direct code-layer call: making it an MCP tool would hand
    # the trigger to the Orchestrator LLM, which is not forced to call any tool
    # (create_sdk_mcp_server only exposes the list) — exactly the structural
    # cause of the original "probe never ran" bug.
    #
    # HISTORY of this block:
    #  (1) Original bug — 8 generations of ZERO pipeline.exploitability_probe
    #      events. A bare `if is_shutting_down: return` swallowed the block with
    #      no log/event, AND the probe call had no timeout, so a hang in the
    #      nested ProcessPoolExecutor (run_exploitability_probes with workers>1
    #      forks a subprocess pool INSIDE this asyncio executor thread — a known
    #      deadlock mode) froze the orchestrator loop with no trace.
    #  (2) First fix (ae6c17e) — made every exit observable + asyncio.wait_for
    #      + workers=1 (serial, no nested fork). Smoke testing later revealed
    #      the 180s budget was FAR below real wall-clock: num_hands=50 needs
    #      ~920s under daemon load (even num_hands=5 took 247s). So the probe
    #      ALWAYS hit the wait_for ceiling and was recorded as failed — never
    #      producing usable data, just trading "silent useless" for "loud
    #      useless". Blocking the orchestrator loop for ~15min to raise the
    #      timeout was unacceptable (halves generation throughput).
    #  (3) THIS fix — probe runs on a background DAEMON thread (fire-and-forget).
    #      post_generation_cleanup returns immediately (does NOT block the loop),
    #      the thread runs the serial probe (workers=1, no nested fork) to
    #      completion and writes exploitability.json. Result is available to the
    #      NEXT generation's Master (~1 generation latency — acceptable for a
    #      lagging adversarial health signal). Every exit still emits a
    #      log_system_event: the canary in the main thread before launch, and
    #      complete/failed in the worker thread. The worker uses ONLY logging +
    #      log_system_event (both thread-safe: fcntl-locked file write, and SSE
    #      via EventBroadcaster.call_soon_threadsafe) — it deliberately does NOT
    #      call ui.log_history, to avoid asyncio-Queue races from a non-loop
    #      thread.
    log.info("Exploitability probe scheduled for v%s", ctx.next_v)

    # Respect an in-progress shutdown, but OBSERVE it (never silent).
    if shutdown_mgr and shutdown_mgr.is_shutting_down:
        log.info("Exploitability probe skipped for v%s (shutting down)", ctx.next_v)
        log_system_event(
            "pipeline.exploitability_probe_skipped", "info",
            f"v{ctx.next_v} probe skipped: shutting down",
            {"version": ctx.next_v, "reason": "shutdown"},
        )
        _finish("skipped", "shutdown_before_exploitability")
        return

    try:
        from exploitability_prober import run_exploitability_probes
        from evolution_infra import get_bot_dir
        new_bot_dir = get_bot_dir(ctx.next_v)  # P2: pass bare int — get_bot_dir already prefixes "claude_v"
        new_bot_main = new_bot_dir / "main.py"
        if not new_bot_main.exists():
            log.warning("Exploitability probe skipped: %s missing", new_bot_main)
            log_system_event(
                "pipeline.exploitability_probe_skipped", "warn",
                f"v{ctx.next_v} probe skipped: bot main.py missing",
                {"version": ctx.next_v, "reason": "bot_main_missing",
                 "path": str(new_bot_main)},
            )
        else:
            # Entry nail (canary): emitted in the MAIN thread, synchronously,
            # before launching the background worker. If this is ever absent for
            # a committed generation, the block was not reached — a mechanical
            # canary so the 8-generation blackout can never repeat silently.
            log_system_event(
                "pipeline.exploitability_probe", "info",
                f"probe starting v{ctx.next_v}",
                {"version": ctx.next_v, "num_hands": 50, "workers": 1,
                 "mode": "background"},
            )

            # Single-flight: skip if a previous probe is still running, so
            # overlapping cleanups (fast crossover gens) cannot pile up probe
            # threads or race on exploitability.json. The check+set is atomic
            # because post_generation_cleanup runs on one event loop (no await
            # between is_set() and set()).
            if _probe_running.is_set():
                log.info(
                    "Exploitability probe skipped for v%s (previous still running)",
                    ctx.next_v,
                )
                log_system_event(
                    "pipeline.exploitability_probe_skipped", "info",
                    f"v{ctx.next_v} probe skipped: previous probe still running",
                    {"version": ctx.next_v, "reason": "single_flight_skip"},
                )
            else:
                _probe_running.set()
                _bot_main = str(new_bot_main)
                _next_v = ctx.next_v

                def _probe_worker():
                    """Run the serial probe off the event loop, then record result.

                    Runs on a daemon thread. Uses run_exploitability_probes (sync,
                    workers=1 -> serial branch -> no nested ProcessPoolExecutor
                    fork). Logs ONLY via logging + log_system_event (thread-safe);
                    deliberately does NOT touch ui (asyncio-Queue races from a
                    non-loop thread).
                    """
                    try:
                        result = run_exploitability_probes(
                            _bot_main, num_hands=50, workers=1
                        )
                        overall = result.get("overall_score", 0.5)
                        weaknesses = result.get("weaknesses", [])
                        log_system_event(
                            "pipeline.exploitability_probe", "info",
                            f"v{_next_v} exploitability: {overall:.2f}/1.0, "
                            f"{len(weaknesses)} weaknesses",
                            {"version": _next_v, "overall_score": overall,
                             "weaknesses": weaknesses, "num_hands": 50},
                        )
                    except Exception as e:
                        log.warning(
                            "Exploitability probe failed for v%s: %s\n%s",
                            _next_v, e, traceback.format_exc(),
                        )
                        log_system_event(
                            "pipeline.exploitability_probe_failed", "error",
                            f"v{_next_v} probe failed: {e}",
                            {"version": _next_v, "error": str(e)[:300],
                             "traceback": traceback.format_exc()[:2000]},
                        )
                    finally:
                        _probe_running.clear()

                threading.Thread(
                    target=_probe_worker, daemon=True,
                    name=f"exploitability-probe-v{ctx.next_v}",
                ).start()
                log.info(
                    "Exploitability probe launched in background for v%s "
                    "(serial, num_hands=50, ~10-15min)", ctx.next_v,
                )
                if ui:
                    ui.log_history(
                        f"Exploitability probe launched for v{ctx.next_v} "
                        f"(background, ~10-15min)", "info",
                    )
    except Exception as e:
        # Launch/import/get_bot_dir failure: observe it (never silent).
        log.warning(
            "Exploitability probe launch failed: %s\n%s", e, traceback.format_exc()
        )
        log_system_event(
            "pipeline.exploitability_probe_failed", "error",
            f"v{ctx.next_v} probe launch failed: {e}",
            {"version": ctx.next_v, "error": str(e)[:300]},
        )

    # Phase 4: Async QD k=3 fitness evaluation (FIRE-AND-FORGET background).
    #
    # Re-evaluates the just-committed candidate v{k} against a few opponents k=3
    # times and merges the median fitness into behavior_archive.json. Same
    # fire-and-forget discipline as the exploitability probe above: direct
    # code-layer call (NOT an MCP tool), single-flight guard, daemon thread,
    # thread-safe logging only. Result feeds the NEXT generation's Master via
    # the archive (~1 generation latency, acceptable for a lagging diversity
    # signal). Failure here MUST NOT abort post_generation_cleanup — every exit
    # in launch_qd_eval is observed via a system_event.
    try:
        # Committed gate (root-cause fix for qd_eval_failed bot_main_missing, 2026-06-19):
        # post_generation_cleanup runs on abandoned cycles too: orchestrator.py:968
        # `if cost >= 0:` triggers cleanup whenever _run_one_cycle returns accumulated
        # cost (always >=0, even after abandon — abandon has no special return value).
        # An abandoned generation's bot dir is deleted (abandon_generation rmtree, or the
        # LLM's own Bash `rm -rf`), but ctx.next_v still holds the planned version, so
        # launch_qd_eval would fire against a missing main.py and emit qd_eval_failed.
        # Measured: 100% of the 5 post-restart qd_eval_failed events were preceded by the
        # dir being deleted. Gate on git_has_tag (authoritative commit proof) so QD eval
        # only runs for genuinely-committed candidates; symmetric to the exploitability
        # probe block's main.py guard (generation_scheduler.py:797).
        from evolution_infra import git_has_tag
        if ctx.next_v > 0 and git_has_tag(ctx.next_v):
            from qd_async_eval import launch_qd_eval
            launch_qd_eval(ctx.next_v, ctx.source_v, k=3, n_games=8,
                           ui=ui, shutdown_mgr=shutdown_mgr)
        elif ctx.next_v > 0:
            log_system_event(
                "pipeline.qd_eval_skipped", "info",
                f"v{ctx.next_v} QD eval skipped: not committed "
                f"(no bot-v{ctx.next_v} tag - abandoned/uncleaned cycle)",
                {"version": ctx.next_v, "reason": "not_committed",
                 "source_v": ctx.source_v},
            )
    except Exception as e:
        log.warning("QD async eval launch failed: %s\n%s", e, traceback.format_exc())
        log_system_event(
            "pipeline.qd_eval_failed", "error",
            f"v{ctx.next_v} QD eval launch failed: {e}",
            {"version": ctx.next_v, "error": str(e)[:300]},
        )

    # fix-6: Compute and store decision fingerprint for the just-committed bot.
    # This feeds behavior_diversity fingerprints.jsonl consumed by crossover
    # parent selection and the novelty gate in commit_bot.
    try:
        log_system_event(
            "pipeline.post_cleanup_fingerprint_start",
            "info",
            f"Behavior fingerprint starting for claude_v{ctx.next_v}",
            {"version": ctx.next_v, "source_v": ctx.source_v},
        )
        _save_committed_bot_fingerprint(ctx.next_v)
    except Exception as e:
        log.warning("Fingerprint computation failed (non-fatal): %s", e)
        log_system_event(
            "pipeline.post_cleanup_fingerprint_failed",
            "warn",
            f"Behavior fingerprint failed for claude_v{ctx.next_v}: {str(e)[:180]}",
            {"version": ctx.next_v, "source_v": ctx.source_v, "error": str(e)[:500]},
        )

    _finish("done")
