"""Review-stage LLM agents: Critic, Performance Verification, and Crossover.

These agents evaluate worker output and verify strategic improvements.
"""

import json
import time

from logging_config import get_logger
_log = get_logger("review")

from bot_namespace import bot_name
from llm_failure import is_llm_infra_error, infra_payload
from strength_order import match_score

from evolution_infra import (
    run_claude_query, parse_json_output, substitute_template,
    locked_file, get_bot_dir, get_logs_dir, get_active_bots,
    verify_code, run_import_contract_test, run_smoke_test,
    PROMPTS_DIR, RESULTS_DIR, MATCH_HISTORY_FILE, H2H_FILE, BOT_STATS_FILE,
    MAX_CROSSOVER_RETRIES, copy_bot_tree_for_candidate,
    Glicko2Player,
)


async def _run_critic(
    next_v,
    source_v,
    master_plan_str,
    ui,
    prev_critic_result=None,
    prompt_evidence=None,
):
    """Poker Strategy Critic — independently scores the strategic value of worker changes.

    Separate from the Reviewer (which checks code correctness and role boundaries).
    The Critic evaluates whether the diff will actually improve poker win rate.

    Returns a dict: {score, approved, strategic_assessment, feedback, local_optima_warning}.
    Returns ``llm_failed`` on role/tooling failure so the caller can retry the
    same gate without fabricating a strategic rejection.
    """
    from prompt_evidence import (
        bootstrap_prompt_policy_text,
        is_protocol_bootstrap_prompt_evidence,
        resolve_prompt_evidence,
    )

    if prompt_evidence is None:
        try:
            from evolution_infra import read_pipeline_checkpoint

            checkpoint = read_pipeline_checkpoint()
        except Exception:
            checkpoint = None
    else:
        checkpoint = None
    prompt_evidence = resolve_prompt_evidence(
        envelope=prompt_evidence,
        checkpoint=checkpoint,
        next_v=int(next_v),
        source_v=int(source_v),
    )
    protocol_bootstrap_no_strength = is_protocol_bootstrap_prompt_evidence(
        prompt_evidence
    )

    critic_prompt_path = PROMPTS_DIR / "critic_prompt.md"
    if not critic_prompt_path.exists():
        ui.log_history("Critic prompt not found; critic verdict is unavailable.", "error")
        return {
            "llm_failed": True,
            "error": "critic_prompt_missing",
            "approved": None,
        }

    critic_prompt = critic_prompt_path.read_text()
    critic_prompt = substitute_template(critic_prompt, {
        "master_plan": master_plan_str,
        "version": str(next_v),
        "parent_version": str(source_v),
    })
    if protocol_bootstrap_no_strength:
        critic_prompt = (
            bootstrap_prompt_policy_text(prompt_evidence)
            + "\n\nBOOTSTRAP SCORING OVERRIDE: judge the current candidate diff and "
            "typed protocol/runtime contracts only. Historical H2H, replay, "
            "experience, calibration, and certification prose cannot be required "
            "or cited. Keep h2h_weaknesses and experience_pool_refs empty; diff "
            "evidence is sufficient for an advisory score. Do not inspect git "
            "history or web/core/experience_pool.md.\n\n"
            + critic_prompt
        )
        critic_prompt = critic_prompt.replace(
            f"4. Check recent history: `git log --oneline --max-count=20 national-bot-v{source_v}..HEAD`",
            "4. Protocol bootstrap: do not inspect historical git commits.",
        ).replace(
            "5. Read `web/core/experience_pool.md` for `[POSSIBLY EXHAUSTED]` tags",
            "5. Protocol bootstrap: the experience pool is intentionally unavailable.",
        )
    if protocol_bootstrap_no_strength:
        critic_prompt += (
            "\n\n# Stable H2H Snapshot Contract\n"
            "Protocol bootstrap intentionally has no strength snapshot. Treat "
            "all matchup claims as unknown and do not read live H2H files.\n"
        )
    else:
        try:
            from evidence_snapshot import h2h_snapshot_contract_text

            critic_prompt += "\n\n" + h2h_snapshot_contract_text(
                next_v,
                source_v=source_v,
                include_json=True,
                max_chars=12000,
            )
        except Exception as exc:
            critic_prompt += (
                "\n\n# Stable H2H Snapshot Contract\n"
                "The generation snapshot is unavailable. Treat all matchup strength "
                f"claims as unknown; do not read live H2H files. ({type(exc).__name__})\n"
            )

    if prev_critic_result:
        prev_score = prev_critic_result.get("score", 0)
        prev_feedback = (prev_critic_result.get("feedback") or "")[:1000]
        critic_prompt += (
            f"\n\n# Previous Critic Evaluation (for context — you are evaluating an UPDATED version):\n"
            f"- Previous Score: {prev_score}\n"
            f"- Previous Approved: {prev_critic_result.get('approved', False)}\n"
            f"- Previous Feedback (each point MUST be explicitly addressed):\n{prev_feedback}\n"
            f"\nYou MUST verify that EACH specific point from the previous feedback was addressed.\n"
            f"If ANY previous issue remains unresolved, do NOT raise the score above the previous score.\n"
            f"If improvements were made that address ALL feedback points, raise the score accordingly.\n"
        )

    # --- Meta-3: Critic Bias Calibration ---
    try:
        if protocol_bootstrap_no_strength:
            calibration_file = None
        else:
            calibration_file = RESULTS_DIR / "critic_calibration.jsonl"
        if calibration_file is not None and calibration_file.exists():
            lines = calibration_file.read_text().strip().split('\n')
            all_rows = [json.loads(l) for l in lines[-10:] if l.strip()]
            # fix-2: skip rows where rating_delta is None (unreconciled).
            # Old rows without "reconciled" field are backward-compat: they
            # have a real delta value from the old r-2*rd calculation.
            recent = [
                r for r in all_rows
                if r.get("rating_delta") is not None
            ]
            if len(recent) >= 3:
                scores = [r.get("critic_score", 0) for r in recent]
                deltas = [r.get("rating_delta", 0) for r in recent]
                avg_score = sum(scores) / len(scores)
                avg_delta = sum(deltas) / len(deltas)
                if avg_score > 7 and avg_delta < 0:
                    critic_prompt += (
                        f"\n\n# Critic Calibration Note\n"
                        f"Over the last {len(recent)} generations, your average score was {avg_score:.1f} "
                        f"but actual rating change was {avg_delta:+.0f} points. "
                        f"You may be OVERESTIMATING improvements, especially in strategy complexity. "
                        f"Please be more critical this time — demand concrete evidence of improvement.\n"
                    )
                elif avg_score < 4 and avg_delta > 0:
                    critic_prompt += (
                        f"\n\n# Critic Calibration Note\n"
                        f"Over the last {len(recent)} generations, your average score was {avg_score:.1f} "
                        f"but actual rating improved by {avg_delta:+.0f} points. "
                        f"You may be TOO HARSH. Consider giving credit for small but real improvements.\n"
                    )
    except Exception:
        pass  # Calibration is advisory

    log_file = get_logs_dir(next_v) / "critic_io.txt"
    try:
        output, _, _ = await run_claude_query(
            critic_prompt, [], ui, "STRATEGY CRITIC", log_file,
            tools=["Bash", "Read"],
            deny_live_prompt_evidence=protocol_bootstrap_no_strength,
        )
        from llm_query import parse_json_output_with_mode
        data, failure_mode = parse_json_output_with_mode(output)
        if data and "score" in data:
            # Coerce non-string feedback to string (LLM sometimes returns null/list/dict)
            if "feedback" in data and not isinstance(data["feedback"], str):
                data["feedback"] = str(data["feedback"]) if data["feedback"] is not None else ""
            # Normalise: score >= 6 → approved
            from output_schema import validate_agent_output
            data, errors = validate_agent_output("critic", data)
            if errors:
                ui.log_history(f"Critic validation issues: {'; '.join(errors[:3])}", "warn")
            if "approved" not in data:
                data["approved"] = data["score"] >= 6
            data.setdefault("local_optima_warning", False)
            return data
    except Exception as e:
        ui.log_history(f"Critic execution error (NOT a strategic rejection): {e}", "warn")
        return infra_payload(e, approved=None)

    # Parse collapse: reaching here means the LLM output failed to parse
    # (NO_JSON/NO_FENCE/PARSE_ERROR) or lacked the score key, OR an exception
    # skipped the parse entirely. Previously this was an opaque "not valid JSON"
    # default. Emit a classifiable failure event so the parse collapse is visible.
    _fm = locals().get("failure_mode", "EXCEPTION")
    _out = locals().get("output", "") or ""
    try:
        from event_bus import warn
        warn("pipeline.critic_parse_failed",
             f"Critic v{next_v} parse failed (mode={_fm}); defaulting to rejected",
             version=next_v, source_v=source_v, failure_mode=_fm, output_len=len(_out))
    except Exception:
        pass
    return {
        "llm_failed": True,
        "approved": None,
        "error": f"critic_output_unusable:{_fm}",
        "feedback": "Critic output was not valid JSON.",
        "parse_failed": True,
    }


# ──────────────────────────────────────────────
# fix-2: Async calibration backfill
# ──────────────────────────────────────────────

def reconcile_critic_calibration(ratings, bot_stats, rd_threshold=60, min_games=100):
    """Backfill real rating_delta into critic_calibration.jsonl.

    Called from the daemon's save_cycle so it runs every save cycle (every
    ~20 games or ~60s). For each row where reconciled=False and rating_delta
    is None, checks whether the bot (version) has converged (rd < rd_threshold
    and games >= min_games). If so, computes the real delta = r_bot - r_source
    and freezes it (reconciled=True). Once frozen, the delta is never recomputed
    even if source bot's rating changes.

    Args:
        ratings: dict of bot_name -> Glicko2Player (current daemon ratings).
        bot_stats: dict of bot_name -> stats dict (must have 'games' key).
        rd_threshold: max rd to consider a bot converged (default 60).
        min_games: min games to consider a bot converged (default 100).
    """
    import fcntl

    cal_file = RESULTS_DIR / "critic_calibration.jsonl"
    if not cal_file.exists():
        return

    try:
        # Read all rows under shared lock
        with locked_file(cal_file, "r", encoding="utf-8") as f:
            raw = f.read()
        if not raw.strip():
            return

        lines = raw.strip().split('\n')
        rows = []
        changed = False
        for line in lines:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                rows.append(line)
                continue

            # Skip rows that are already reconciled or have a non-None delta
            if row.get("reconciled") is True or row.get("rating_delta") is not None:
                rows.append(row)
                continue

            # This row needs backfill — check if the bot has converged
            version = row.get("version")
            source_v = row.get("source_v")
            if version is None:
                rows.append(row)
                continue

            review_bot_name = bot_name(version)
            source_name = bot_name(source_v) if source_v is not None else None

            bot_player = ratings.get(review_bot_name)
            bot_games = bot_stats.get(review_bot_name, {}).get("games", 0)

            if bot_player is None or bot_games < min_games:
                rows.append(row)
                continue
            if bot_player.rd >= rd_threshold:
                rows.append(row)
                continue

            # Bot has converged — compute real rating delta
            source_player = ratings.get(source_name) if source_name else None
            if source_player is not None:
                delta = round(bot_player.r - source_player.r, 1)
            else:
                # Source bot not found (reaped?) — use absolute rating delta from default
                delta = round(bot_player.r - 1500.0, 1)

            row["rating_delta"] = delta
            row["reconciled"] = True
            row["reconciled_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            changed = True
            rows.append(row)
            _log.debug("Reconciled calibration v%d: delta=%.1f (rd=%.1f, %d games)",
                       version, delta, bot_player.rd, bot_games)

        if not changed:
            return

        # Write back under exclusive lock
        with locked_file(cal_file, "w", encoding="utf-8") as f:
            for row in rows:
                if isinstance(row, str):
                    f.write(row + "\n")
                else:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")

    except Exception:
        pass  # Calibration reconciliation is advisory


async def _run_performance_verification(source_v, ratings, ui):
    """SATLUTION-style LLM performance verification.

    Synthesises rating history + win-rate trends into a structured JSON insight
    that Master uses to prioritise improvements and avoid local optima.

    Returns a JSON-formatted string (to be injected into master prompt).
    Returns "" on failure so master prompt degrades gracefully.
    """
    # ── Build rating history for last 10 periods ──
    history_file = RESULTS_DIR / "rating_history.jsonl"
    gen_trend_lines = []
    if history_file.exists():
        try:
            with locked_file(history_file, "r") as hf:
                raw_lines = hf.readlines()
            for line in raw_lines[-10:]:
                try:
                    snap = json.loads(line.strip())
                    wr_data = snap.get("win_rates", {})
                    wrs = [v["h2h_avg_wr"] for v in wr_data.values() if v.get("h2h_avg_wr") is not None]
                    if wrs:
                        gen_trend_lines.append(f"  Period {snap.get('period','?')}: top_h2h_wr={max(wrs):.4f}")
                    else:
                        bots_in_snap = snap.get("ratings", {})
                        top_r = max((v.get("r", 1500) for v in bots_in_snap.values()), default=1500)
                        gen_trend_lines.append(f"  Period {snap.get('period','?')}: top_r={top_r:.0f}")
                except (json.JSONDecodeError, KeyError):
                    continue
        except Exception as e:
            _log.warning("Failed to read rating history for perf verification: %s", e)

    # ── Win-rate summary for source_v (last 30 matches) ──
    source_bot_name = bot_name(source_v)
    win_rate_lines = []
    if MATCH_HISTORY_FILE.exists():
        try:
            wins, losses, draws = 0, 0, 0
            from rating_snapshot import _admitted_70_hand_history_sample
            with locked_file(MATCH_HISTORY_FILE, "r") as mf:
                all_lines = mf.readlines()
            for line in all_lines[-100:]:
                try:
                    entry = json.loads(line.strip())
                    if _admitted_70_hand_history_sample(entry) is None:
                        continue
                    b0, b1 = entry.get("bot0"), entry.get("bot1")
                    w0, w1 = entry.get("bot0_wins", 0), entry.get("bot1_wins", 0)
                    d = entry.get("draws", 0)
                    if b0 == source_bot_name:
                        wins += w0; losses += w1; draws += d
                    elif b1 == source_bot_name:
                        wins += w1; losses += w0; draws += d
                except (json.JSONDecodeError, KeyError):
                    continue
            total = wins + losses + draws
            win_rate = match_score(wins, draws, total)
            if win_rate is not None:
                win_rate_lines.append(
                    f"  {source_bot_name} recent: {wins}W / {losses}L / "
                    f"{draws}D ({win_rate:.0%} score)"
                )
        except Exception as e:
            _log.warning("Failed to read match history for perf verification: %s", e)

    # ── Top-5 active bots for context ──
    active_bots = get_active_bots()
    from tool_helpers import (
        load_h2h_avg_winrates,
        load_selection_order_keys,
        load_selection_scores,
    )
    h2h_winrates = load_h2h_avg_winrates()
    strength_scores = load_selection_scores()
    selection_order_keys = load_selection_order_keys()
    sorted_bots = sorted(
        [(b, ratings.get(b, Glicko2Player())) for b in active_bots],
        key=lambda x: selection_order_keys.get(
            x[0],
            (strength_scores.get(x[0], 0.0),),
        ),
        reverse=True,
    )[:5]
    ratings_lines = [
        f"  {b}: score={strength_scores.get(b, 0.0):.4f}, h2h_avg_wr={h2h_winrates.get(b, 0.0):.2%} (r={p.r:.0f} rd={p.rd:.0f})"
        for b, p in sorted_bots
    ]

    # ── Head-to-Head data ──
    h2h_lines = []
    if H2H_FILE.exists():
        try:
            with locked_file(H2H_FILE, "r") as hf:
                h2h_data = json.load(hf)
            for k, v in h2h_data.items():
                parts = k.split(" vs ")
                if len(parts) != 2:
                    continue
                a_name, b_name = parts
                if source_bot_name not in (a_name, b_name):
                    continue
                opponent = b_name if source_bot_name == a_name else a_name
                g = v.get("games", 0)
                if g == 0:
                    continue
                # Figure out which side our bot is
                if source_bot_name == a_name:
                    bot_w = v.get("a_wins", 0)
                else:
                    bot_w = v.get("b_wins", 0)
                draws = v.get("draws", 0)
                opp_w = g - bot_w - draws
                wr = match_score(bot_w, draws, g)
                if wr is None:
                    continue
                tag = ""
                if wr < 0.40:
                    tag = " ← WEAKNESS"
                elif wr > 0.60:
                    tag = " ← STRENGTH"
                h2h_lines.append((wr, f"  vs {opponent}: {bot_w}W-{opp_w}L-{draws}D ({wr:.0%}){tag}"))
            h2h_lines.sort(key=lambda x: x[0])
        except Exception as e:
            _log.warning("Failed to read H2H data for perf verification: %s", e)

    # ── Bot stats (overall win rate) ──
    bot_stats_line = ""
    if BOT_STATS_FILE.exists():
        try:
            with locked_file(BOT_STATS_FILE, "r") as bsf:
                bs_data = json.load(bsf)
            bs = bs_data.get(source_bot_name, {})
            g = bs.get("games", 0)
            wr = bs.get("win_rate", 0.0)
            if g > 0:
                bot_stats_line = f"  {source_bot_name}: {wr:.0%} overall ({g} games)"
        except Exception as e:
            _log.warning("Failed to read bot stats for perf verification: %s", e)

    # ── Build prompt ──
    # Check rd (rating deviation) for the current bot to flag unreliable data
    bot_rd = ratings.get(source_bot_name, Glicko2Player()).rd if ratings else 350
    rd_warning = ""
    if bot_rd > 200:
        rd_warning = (
            f"\n⚠️ IMPORTANT: This bot has rd={bot_rd:.0f} (>200), meaning its rating is VERY uncertain.\n"
            "Trend analysis is unreliable — period-to-period fluctuations are likely noise, not signal.\n"
            "You MUST note this explicitly and treat any 'trend' with extreme skepticism.\n"
        )
    elif bot_rd > 100:
        rd_warning = (
            f"\nNOTE: This bot has rd={bot_rd:.0f} (>100), meaning its rating is moderately uncertain.\n"
            "Be cautious about interpreting small period-to-period changes as meaningful trends.\n"
        )

    # Build prompt from template
    template_file = PROMPTS_DIR / "performance_analyst.md"
    if not template_file.exists():
        return ""
    prompt = template_file.read_text()
    prompt = substitute_template(prompt, {
        "bot_name": source_bot_name,
        "rd_warning": rd_warning,
        "performance_history": "\n".join(gen_trend_lines) if gen_trend_lines else "  No history available",
        "bot_stats": bot_stats_line if bot_stats_line else "  No stats available",
        "h2h_results": "\n".join(l for _, l in h2h_lines) if h2h_lines else "  No H2H data available",
        "top_bots": "\n".join(ratings_lines),
    })

    log_file = get_logs_dir(source_v) / "performance_verification_io.txt"
    try:
        output, _, _ = await run_claude_query(
            prompt, [], ui, "PERFORMANCE ANALYST", log_file,
        )
        from llm_query import parse_json_output_with_mode
        data, failure_mode = parse_json_output_with_mode(output)
        if data:
            return json.dumps(data, ensure_ascii=False)
    except Exception as e:
        # C-class: distinguish LLM infrastructure crash from "no data".
        # Return a sentinel string so the Master prompt builder can surface
        # "analysis unavailable due to LLM failure" instead of the misleading
        # "No performance verification data available". Return type stays str.
        if is_llm_infra_error(e):
            ui.log_history(f"Performance verification LLM infrastructure error: {e}", "warn")
            from system_log import log_system_event
            log_system_event("pipeline.performance_analyst_infra", "warn",
                             f"Performance analyst v{source_v} LLM crashed (infra): {e}",
                             {"source_v": source_v, "error": str(e)})
            return "[LLM_INFRA_ERROR: analysis unavailable]"
        ui.log_history(f"Performance verification failed: {e}", "warn")

    # Parse collapse: reaching here means the LLM output failed to parse
    # (NO_JSON/NO_FENCE/PARSE_ERROR), or the LLM call threw a non-infra
    # exception. Previously this was a silent "" return that the Master
    # prompt builder surfaced as "No performance verification data available"
    # — hiding a parse failure behind a benign-looking empty-string. Emit a
    # classifiable failure event so the parse collapse is visible.
    _fm = locals().get("failure_mode", "EXCEPTION")
    _out = locals().get("output", "") or ""
    try:
        from event_bus import warn
        warn("pipeline.performance_analyst_parse_failed",
             f"Performance analyst v{source_v} parse failed (mode={_fm}); "
             "returning empty (master prompt degrades to no-data)",
             source_v=source_v, failure_mode=_fm, output_len=len(_out))
    except Exception:
        pass
    return ""


async def _run_crossover(
    parent_a_v,
    parent_b_v,
    target_v,
    ui,
    *,
    compatibility=None,
    architecture_policy=None,
    capability_context=None,
):
    """Run crossover between two elite bots to create a new child bot."""
    import shutil
    crossover_prompt_path = PROMPTS_DIR / "crossover_prompt.md"
    if not crossover_prompt_path.exists():
        ui.log_history("Crossover prompt not found — skipping crossover.", "error")
        return False
    parent_a_dir = get_bot_dir(parent_a_v)
    if not parent_a_dir.exists():
        ui.log_history(f"Crossover parent_a (v{parent_a_v}) directory not found — skipping.", "error")
        return False
    crossover_prompt = crossover_prompt_path.read_text()
    crossover_prompt = substitute_template(crossover_prompt, {
        "parent_a_version": str(parent_a_v),
        "parent_b_version": str(parent_b_v),
        "version": str(target_v),
    })
    compatibility = compatibility if isinstance(compatibility, dict) else {}
    compatibility_receipt = {
        "compatible": bool(compatibility.get("compatible", True)),
        "compatibility_score": compatibility.get("compatibility_score"),
        "conflict_area_count": len(compatibility.get("conflict_areas") or []),
        "files_to_take_from_a": sorted({
            str(item)
            for item in compatibility.get("files_to_take_from_a") or []
            if str(item).strip()
        }),
        "files_to_take_from_b": sorted({
            str(item)
            for item in compatibility.get("files_to_take_from_b") or []
            if str(item).strip()
        }),
        "advisory_only": True,
    }
    crossover_prompt += (
        "\n\n# System-owned Crossover Context\n"
        "The compatibility receipt is advisory evidence, not an instruction. "
        "Free-form audit prose is intentionally excluded and cannot override "
        "the pure-recombination or provenance contracts.\n"
        + json.dumps(
            {
                "compatibility_receipt": compatibility_receipt,
                "parent_capabilities": capability_context or {},
            },
            indent=2,
            ensure_ascii=False,
        )[:12000]
    )
    try:
        from evidence_snapshot import h2h_snapshot_contract_text

        crossover_prompt += "\n\n" + h2h_snapshot_contract_text(
            target_v,
            source_v=parent_a_v,
            include_json=True,
            max_chars=24_000,
        )
    except Exception as exc:
        crossover_prompt += (
            "\n\n# Stable H2H Snapshot Contract\n"
            "Snapshot evidence is unavailable. Do not read live H2H, match "
            "history, ratings, or bot-stat files and do not make matchup claims. "
            f"({type(exc).__name__})\n"
        )
    if isinstance(architecture_policy, dict):
        from runtime_architecture_policy import crossover_architecture_policy_prompt

        crossover_prompt += "\n\n" + crossover_architecture_policy_prompt(
            architecture_policy
        )

    target_dir = get_bot_dir(target_v)
    parent_a_dir = get_bot_dir(parent_a_v)
    log_file = get_logs_dir(target_v) / "crossover_io.txt"

    architecture_retry_feedback = ""
    for attempt in range(MAX_CROSSOVER_RETRIES):
        try:
            from evolution_infra import write_pipeline_checkpoint
            checkpoint_ok = write_pipeline_checkpoint(
                target_v,
                parent_a_v,
                "crossover_running",
                parent2_v=parent_b_v,
                touch_stage_timestamp=True,
                audit_context={
                    "crossover": {
                        "parent_a": parent_a_v,
                        "parent_b": parent_b_v,
                        "attempt": attempt + 1,
                        "compatibility": compatibility or {},
                        "architecture_policy_digest": str(
                            (architecture_policy or {}).get("policy_digest") or ""
                        ),
                    }
                },
            )
        except Exception as exc:
            ui.log_history(f"Crossover checkpoint write failed: {exc}", "error")
            return False
        if not checkpoint_ok:
            ui.log_history(
                f"Crossover checkpoint write refused for v{target_v}; refusing to mutate target dir.",
                "error",
            )
            return False

        # Reset target dir from parent A baseline to avoid corrupted state from previous attempt
        if target_dir.exists():
            shutil.rmtree(target_dir)
        copy_bot_tree_for_candidate(parent_a_dir, target_dir)

        # Apply known critical fixes to crossover child
        from fix_injection import apply_known_fixes, log_fix_application
        applied, skipped = apply_known_fixes(target_dir)
        if applied or skipped:
            log_fix_application(applied, skipped, target_dir, parent_a_v)

        try:
            from candidate_hygiene import sanitize_candidate_dir
            from workflow_profiles import get_workflow_profile
            native_tcp = getattr(get_workflow_profile(), "national_execution_mode", "adapter") == "native_tcp"
            sanitize_candidate_dir(target_dir, require_native_tcp=native_tcp)
        except Exception as exc:
            ui.log_history(f"Crossover native TCP entry preparation failed: {exc}", "warn")
            continue

        from crossover_provenance import python_source_snapshot

        # Freeze the exact Parent-A-plus-system-fixes baseline before the LLM.
        # This keeps mandatory fix/hygiene changes out of the provenance diff.
        system_prepared_baseline = python_source_snapshot(target_dir)

        ui.clear_io()
        ui.set_status(f"Crossover v{parent_a_v}×v{parent_b_v}→v{target_v} (Try {attempt+1})", is_working=True)
        try:
            await run_claude_query(
                crossover_prompt + architecture_retry_feedback, [], ui,
                f"CROSSOVER v{parent_a_v}×v{parent_b_v}→v{target_v}",
                log_file,
                tools=["Bash", "Read", "Edit"],
                allowed_write_dir=target_dir,  # A1: scope writes to target bot dir only
            )
        except Exception as e:
            # SDK error (e.g. ClaudeSDKError now propagates from run_claude_query)
            # — retry the crossover attempt instead of escaping the retry loop.
            ui.log_history(f"Crossover LLM error: {e}", "warn")
            continue

        # The crossover agent may rebuild the target by copying a parent after
        # the pre-LLM baseline was fixed. Re-apply mandatory fixes before any
        # downstream gate sees the candidate.
        from fix_injection import apply_known_fixes, log_fix_application
        post_applied, post_skipped = apply_known_fixes(target_dir)
        if post_applied:
            log_fix_application(post_applied, post_skipped, target_dir, parent_a_v)

        try:
            from candidate_hygiene import sanitize_candidate_dir
            from workflow_profiles import get_workflow_profile
            native_tcp = getattr(get_workflow_profile(), "national_execution_mode", "adapter") == "native_tcp"
            hygiene = sanitize_candidate_dir(target_dir, require_native_tcp=native_tcp)
            if hygiene.get("completed_removed") or hygiene.get("native_entry"):
                try:
                    from system_log import log_system_event
                    log_system_event(
                        "pipeline.candidate_hygiene_applied",
                        "info",
                        f"Candidate hygiene applied for crossover v{target_v}",
                        {
                            "target_v": target_v,
                            "parent_a": parent_a_v,
                            "parent_b": parent_b_v,
                            "attempt": attempt + 1,
                            **hygiene,
                        },
                    )
                except Exception:
                    pass
        except Exception as exc:
            ui.log_history(f"Crossover candidate hygiene failed: {exc}", "warn")
            continue

        compile_errors = verify_code(target_dir)
        if compile_errors:
            ui.log_history("Crossover compile error, retrying...", "warn")
            continue

        import_errors = run_import_contract_test(target_dir)
        if import_errors:
            try:
                from system_log import log_system_event
                log_system_event(
                    "pipeline.crossover_import_contract_failed", "error",
                    f"Crossover v{target_v} import contract failed on attempt {attempt+1}: "
                    f"{import_errors[0].get('module')} {import_errors[0].get('exception')}: "
                    f"{import_errors[0].get('message')}",
                    {"target_v": target_v, "parent_a": parent_a_v, "parent_b": parent_b_v,
                     "attempt": attempt + 1, "errors": import_errors[:3]},
                )
            except Exception:
                pass
            ui.log_history("Crossover runtime import contract failed, retrying...", "warn")
            continue

        smoke_errors = run_smoke_test(target_dir)
        if smoke_errors:
            ui.log_history("Crossover smoke test failed, retrying...", "warn")
            continue

        from code_verification import check_code_size

        _total_lines, oversized_files = check_code_size(
            target_dir,
            source_dir=parent_a_dir,
        )
        if oversized_files:
            architecture_retry_feedback = (
                "\n\n# Previous Attempt Rejected By Code Size Contract\n"
                "Rebuild from Parent A and keep every file within the exact "
                "source-relative limit below. Do not postpone this debt to Master.\n"
                + json.dumps(
                    [
                        {"file": name, "lines": lines, "limit": limit}
                        for name, lines, limit in oversized_files[:12]
                    ],
                    indent=2,
                    ensure_ascii=False,
                )
            )
            try:
                from system_log import log_system_event

                log_system_event(
                    "pipeline.crossover_code_size_rejected",
                    "warn",
                    f"Crossover v{target_v} attempt {attempt + 1} exceeded code-size limits",
                    {
                        "target_v": target_v,
                        "parent_a": parent_a_v,
                        "parent_b": parent_b_v,
                        "attempt": attempt + 1,
                        "total_lines": _total_lines,
                        "oversized_files": oversized_files[:12],
                    },
                )
            except Exception:
                pass
            ui.log_history(
                "Crossover code-size contract failed, retrying from Parent A baseline...",
                "warn",
            )
            continue

        from crossover_provenance import (
            validate_crossover_recombination_provenance,
        )

        provenance_issues = validate_crossover_recombination_provenance(
            system_prepared_baseline,
            get_bot_dir(parent_b_v),
            target_dir,
        )
        if provenance_issues:
            architecture_retry_feedback = (
                "\n\n# Previous Attempt Rejected By Crossover Provenance Contract\n"
                "This stage is pure recombination. Every strategic diff must "
                "contain a traceable Parent-B component; independent threshold, "
                "heuristic, deletion, or novel-file mutations belong to the later "
                "Master/Worker stage. Rebuild from Parent A and either import an "
                "actual Parent-B component or leave Parent A unchanged.\n"
                + json.dumps(
                    provenance_issues[:12],
                    indent=2,
                    ensure_ascii=False,
                )[:5000]
            )
            try:
                from system_log import log_system_event

                log_system_event(
                    "pipeline.crossover_provenance_rejected",
                    "warn",
                    f"Crossover v{target_v} attempt {attempt + 1} contained an independent mutation",
                    {
                        "target_v": target_v,
                        "parent_a": parent_a_v,
                        "parent_b": parent_b_v,
                        "attempt": attempt + 1,
                        "issues": provenance_issues[:12],
                    },
                )
            except Exception:
                pass
            ui.log_history(
                "Crossover provenance contract failed, retrying from Parent A baseline...",
                "warn",
            )
            continue

        try:
            from national_position_contract import detect_position_semantics_errors

            position_errors = detect_position_semantics_errors(target_dir)
        except Exception as exc:
            position_errors = [
                "position_contract_check_error:"
                f"{type(exc).__name__}:{str(exc)[:200]}"
            ]
        if position_errors:
            architecture_retry_feedback = (
                "\n\n# Previous Attempt Rejected By National Position Contract\n"
                "Rebuild from parent A and correct these hard protocol errors.\n"
                + json.dumps(position_errors[:10], indent=2, ensure_ascii=False)
            )
            try:
                from system_log import log_system_event

                log_system_event(
                    "pipeline.crossover_position_contract_rejected",
                    "warn",
                    f"Crossover v{target_v} attempt {attempt + 1} failed position contract",
                    {
                        "target_v": target_v,
                        "parent_a": parent_a_v,
                        "parent_b": parent_b_v,
                        "attempt": attempt + 1,
                        "errors": position_errors[:10],
                    },
                )
            except Exception:
                pass
            ui.log_history(
                "Crossover national position contract failed, retrying from parent A baseline...",
                "warn",
            )
            continue

        if isinstance(architecture_policy, dict):
            try:
                from runtime_architecture_policy import (
                    ARCHITECTURE_TRANSITION_PHASE_PREPLAN,
                    evaluate_architecture_transition,
                )

                transition = evaluate_architecture_transition(
                    parent_a_dir,
                    target_dir,
                    expected_policy=architecture_policy,
                    evaluation_phase=ARCHITECTURE_TRANSITION_PHASE_PREPLAN,
                )
            except Exception as exc:
                transition = {
                    "ok": False,
                    "conclusive": False,
                    "outcome": "infrastructure_failure",
                    "failure_class": "infrastructure",
                    "infrastructure_failures": [{
                        "component": "runtime_architecture_policy",
                        "failure_class": "internal_infrastructure",
                        "issues": [
                            f"transition_exception:{type(exc).__name__}:{str(exc)[:200]}"
                        ],
                    }],
                    "policy_identity_errors": [
                        f"transition_exception:{type(exc).__name__}:{str(exc)[:200]}"
                    ],
                    "regressions": [],
                    "unresolved_focus_checks": [],
                }
            if transition.get("outcome") == "infrastructure_failure":
                failures = transition.get("infrastructure_failures") or [{
                    "component": "national_runtime_probe",
                    "failure_class": "probe_infrastructure",
                    "issues": ["preplan architecture assessment was inconclusive"],
                }]
                try:
                    from system_log import log_system_event

                    log_system_event(
                        "pipeline.crossover_architecture_infrastructure",
                        "error",
                        f"Crossover v{target_v} preplan architecture probe was inconclusive",
                        {
                            "target_v": target_v,
                            "parent_a": parent_a_v,
                            "parent_b": parent_b_v,
                            "attempt": attempt + 1,
                            "infrastructure_failures": failures,
                        },
                    )
                except Exception:
                    pass
                return {
                    "success": False,
                    "failure_class": "infrastructure",
                    "outcome": "infrastructure_failure",
                    "component": str(
                        (failures[0] or {}).get("component")
                        if isinstance(failures[0], dict)
                        else "national_runtime_probe"
                    ),
                    "infrastructure_failures": failures,
                    "transition": transition,
                }
            if not transition.get("ok"):
                candidate_capabilities = transition.get("candidate_capabilities") or {}
                candidate_checks = candidate_capabilities.get("checks_by_id") or {}
                blocking_ids = {
                    str(check_id)
                    for check_id in transition.get("unresolved_focus_checks") or []
                }
                blocking_ids.update(
                    str(item.get("check_id") or "")
                    for item in transition.get("runtime_floor_failures") or []
                    if item.get("check_id")
                )
                blocking_ids.update(
                    str(item.get("check_id") or "")
                    for item in transition.get("regressions") or []
                    if item.get("check_id")
                )
                blocking_check_details = {}
                for check_id in sorted(blocking_ids):
                    check = candidate_checks.get(check_id) or {}
                    evidence = check.get("evidence") or {}
                    detail = {
                        "guidance": check.get("guidance") or "",
                        "locations": list(evidence.get("locations") or [])[:8],
                        "facts": evidence.get("facts") or {},
                    }
                    if check_id == "killable_decision_runtime":
                        runtime_evidence = (
                            candidate_capabilities.get("decision_runtime_evidence") or {}
                        )
                        detail["safety_issues"] = list(
                            runtime_evidence.get("safety_issues") or []
                        )[:8]
                    blocking_check_details[check_id] = detail
                architecture_retry_feedback = (
                    "\n\n# Previous Attempt Rejected By Runtime Architecture Gate\n"
                    "Rebuild from parent A and correct every blocking item below. "
                    "Do not merely add labels. Items under deferred_to_master are "
                    "not crossover work and must remain deferred.\n"
                    + json.dumps(
                        {
                            "policy_identity_errors": transition.get("policy_identity_errors") or [],
                            "regressions": transition.get("regressions") or [],
                            "runtime_floor_failures": transition.get("runtime_floor_failures") or [],
                            "unresolved_focus_checks": transition.get("unresolved_focus_checks") or [],
                            "blocking_check_details": blocking_check_details,
                            "deferred_to_master": (
                                transition.get("deferred_unresolved_focus_checks") or []
                            ),
                        },
                        indent=2,
                        ensure_ascii=False,
                    )[:5000]
                )
                try:
                    from system_log import log_system_event

                    log_system_event(
                        "pipeline.crossover_architecture_rejected",
                        "warn",
                        f"Crossover v{target_v} attempt {attempt + 1} failed runtime architecture policy",
                        {
                            "target_v": target_v,
                            "parent_a": parent_a_v,
                            "parent_b": parent_b_v,
                            "attempt": attempt + 1,
                            "evaluation_phase": transition.get("evaluation_phase"),
                            "regressions": transition.get("regressions") or [],
                            "runtime_floor_failures": transition.get("runtime_floor_failures") or [],
                            "unresolved_focus_checks": transition.get("unresolved_focus_checks") or [],
                            "blocking_check_details": blocking_check_details,
                            "deferred_to_master": (
                                transition.get("deferred_unresolved_focus_checks") or []
                            ),
                            "policy_identity_errors": transition.get("policy_identity_errors") or [],
                        },
                    )
                except Exception:
                    pass
                ui.log_history(
                    "Crossover runtime architecture policy failed, retrying from parent A baseline...",
                    "warn",
                )
                continue
            deferred_checks = list(
                transition.get("deferred_unresolved_focus_checks") or []
            )
            if deferred_checks:
                try:
                    from system_log import log_system_event

                    log_system_event(
                        "pipeline.crossover_architecture_debt_deferred",
                        "info",
                        f"Crossover v{target_v} baseline accepted with downstream architecture debt",
                        {
                            "target_v": target_v,
                            "parent_a": parent_a_v,
                            "parent_b": parent_b_v,
                            "attempt": attempt + 1,
                            "evaluation_phase": transition.get("evaluation_phase"),
                            "deferred_to_master": deferred_checks,
                        },
                    )
                except Exception:
                    pass

        # LOG GAP FIX (2026-06-30): record which files the crossover LLM actually
        # changed vs parent_a, so the modification is auditable (parity with the
        # worker_files_reset event on the evolve path).
        try:
            parent_a_dir = get_bot_dir(parent_a_v)
            changed = []
            if parent_a_dir.exists():
                import os as _os
                src_files = {f.name for f in parent_a_dir.glob("*.py")}
                for f in target_dir.glob("*.py"):
                    src_f = parent_a_dir / f.name
                    if f.name not in src_files:
                        changed.append(f.name + " (new)")
                    elif src_f.exists() and f.read_text() != src_f.read_text():
                        changed.append(f.name + " (modified)")
            from system_log import log_system_event
            log_system_event(
                "pipeline.crossover_files_changed", "info",
                f"Crossover v{target_v} (v{parent_a_v}×v{parent_b_v}): {len(changed)} "
                f"file(s) changed vs parent v{parent_a_v} (attempt {attempt+1})",
                {"target_v": target_v, "parent_a": parent_a_v, "parent_b": parent_b_v,
                 "attempt": attempt + 1, "changed_files": changed[:20]},
            )
        except Exception:
            pass

        return True

    return False
