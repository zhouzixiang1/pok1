"""Review-stage LLM agents: Critic, Performance Verification, and Crossover.

These agents evaluate worker output and verify strategic improvements.
"""

import json

from logging_config import get_logger
_log = get_logger("review")

from evolution_infra import (
    run_claude_query, parse_json_output, substitute_template,
    locked_file, get_bot_dir, get_logs_dir, get_active_bots,
    verify_code, run_smoke_test,
    PROMPTS_DIR, RESULTS_DIR, MATCH_HISTORY_FILE, H2H_FILE, BOT_STATS_FILE,
    MAX_CROSSOVER_RETRIES,
    Glicko2Player,
)


async def _run_critic(next_v, source_v, master_plan_str, ui, prev_critic_result=None):
    """Poker Strategy Critic — independently scores the strategic value of worker changes.

    Separate from the Reviewer (which checks code correctness and role boundaries).
    The Critic evaluates whether the diff will actually improve poker win rate.

    Returns a dict: {score, approved, strategic_assessment, feedback, local_optima_warning}.
    Returns a safe default on failure so the pipeline can always proceed.
    """
    critic_prompt_path = PROMPTS_DIR / "critic_prompt.md"
    if not critic_prompt_path.exists():
        ui.log_history("Critic prompt not found — defaulting to REJECTED.", "error")
        return {"score": 0, "approved": False, "feedback": "Critic prompt not found — defaulting to rejected."}

    critic_prompt = critic_prompt_path.read_text()
    critic_prompt = substitute_template(critic_prompt, {
        "master_plan": master_plan_str,
        "version": str(next_v),
        "parent_version": str(source_v),
    })

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
        calibration_file = RESULTS_DIR / "critic_calibration.jsonl"
        if calibration_file.exists():
            lines = calibration_file.read_text().strip().split('\n')
            recent = [json.loads(l) for l in lines[-10:] if l.strip()]
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
        )
        data = parse_json_output(output)
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
        ui.log_history(f"Critic error: {e}. Defaulting to rejected.", "warn")
        return {"score": 0, "approved": False, "feedback": str(e), "local_optima_warning": False}

    return {"score": 0, "approved": False, "feedback": "Critic output was not valid JSON.", "local_optima_warning": False}


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
    bot_name = f"claude_v{source_v}"
    win_rate_lines = []
    if MATCH_HISTORY_FILE.exists():
        try:
            wins, losses = 0, 0
            with locked_file(MATCH_HISTORY_FILE, "r") as mf:
                all_lines = mf.readlines()
            for line in all_lines[-100:]:
                try:
                    entry = json.loads(line.strip())
                    b0, b1 = entry.get("bot0"), entry.get("bot1")
                    w0, w1 = entry.get("bot0_wins", 0), entry.get("bot1_wins", 0)
                    if b0 == bot_name:
                        wins += w0; losses += w1
                    elif b1 == bot_name:
                        wins += w1; losses += w0
                except (json.JSONDecodeError, KeyError):
                    continue
            total = wins + losses
            if total > 0:
                win_rate_lines.append(f"  {bot_name} recent: {wins}W / {losses}L ({wins*100//total}% win rate)")
        except Exception as e:
            _log.warning("Failed to read match history for perf verification: %s", e)

    # ── Top-5 active bots for context ──
    active_bots = get_active_bots()
    from tool_helpers import load_h2h_avg_winrates
    h2h_winrates = load_h2h_avg_winrates()
    sorted_bots = sorted(
        [(b, ratings.get(b, Glicko2Player())) for b in active_bots],
        key=lambda x: h2h_winrates.get(x[0], 0.0), reverse=True
    )[:5]
    ratings_lines = [f"  {b}: h2h_avg_wr={h2h_winrates.get(b, 0.0):.2%} (r={p.r:.0f} rd={p.rd:.0f})" for b, p in sorted_bots]

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
                if bot_name not in (a_name, b_name):
                    continue
                opponent = b_name if bot_name == a_name else a_name
                g = v.get("games", 0)
                if g == 0:
                    continue
                # Figure out which side our bot is
                if bot_name == a_name:
                    bot_w = v.get("a_wins", 0)
                else:
                    bot_w = v.get("b_wins", 0)
                opp_w = g - bot_w - v.get("draws", 0)
                wr = bot_w / g
                tag = ""
                if wr < 0.40:
                    tag = " ← WEAKNESS"
                elif wr > 0.60:
                    tag = " ← STRENGTH"
                h2h_lines.append((wr, f"  vs {opponent}: {bot_w}W-{opp_w}L ({wr:.0%}){tag}"))
            h2h_lines.sort(key=lambda x: x[0])
        except Exception as e:
            _log.warning("Failed to read H2H data for perf verification: %s", e)

    # ── Bot stats (overall win rate) ──
    bot_stats_line = ""
    if BOT_STATS_FILE.exists():
        try:
            with locked_file(BOT_STATS_FILE, "r") as bsf:
                bs_data = json.load(bsf)
            bs = bs_data.get(bot_name, {})
            g = bs.get("games", 0)
            wr = bs.get("win_rate", 0.0)
            if g > 0:
                bot_stats_line = f"  {bot_name}: {wr:.0%} overall ({g} games)"
        except Exception as e:
            _log.warning("Failed to read bot stats for perf verification: %s", e)

    # ── Build prompt ──
    # Check rd (rating deviation) for the current bot to flag unreliable data
    bot_rd = ratings.get(bot_name, Glicko2Player()).rd if ratings else 350
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
        "bot_name": bot_name,
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
        data = parse_json_output(output)
        if data:
            return json.dumps(data, ensure_ascii=False)
    except Exception as e:
        ui.log_history(f"Performance verification failed: {e}", "warn")

    return ""


async def _run_crossover(parent_a_v, parent_b_v, target_v, ui):
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

    target_dir = get_bot_dir(target_v)
    parent_a_dir = get_bot_dir(parent_a_v)
    log_file = get_logs_dir(target_v) / "crossover_io.txt"

    for attempt in range(MAX_CROSSOVER_RETRIES):
        # Reset target dir from parent A baseline to avoid corrupted state from previous attempt
        if target_dir.exists():
            shutil.rmtree(target_dir)
        shutil.copytree(parent_a_dir, target_dir, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))

        # Apply known critical fixes to crossover child
        from fix_injection import apply_known_fixes, log_fix_application
        applied, skipped = apply_known_fixes(target_dir)
        if applied or skipped:
            log_fix_application(applied, skipped, target_dir, parent_a_v)

        (target_dir / ".completed").unlink(missing_ok=True)

        ui.clear_io()
        ui.set_status(f"Crossover v{parent_a_v}×v{parent_b_v}→v{target_v} (Try {attempt+1})", is_working=True)
        try:
            await run_claude_query(
                crossover_prompt, [], ui,
                f"CROSSOVER v{parent_a_v}×v{parent_b_v}→v{target_v}",
                log_file,
                tools=["Bash", "Read", "Edit"],
            )
        except Exception as e:
            # SDK error (e.g. ClaudeSDKError now propagates from run_claude_query)
            # — retry the crossover attempt instead of escaping the retry loop.
            ui.log_history(f"Crossover LLM error: {e}", "warn")
            continue

        compile_errors = verify_code(target_dir)
        if compile_errors:
            ui.log_history("Crossover compile error, retrying...", "warn")
            continue

        smoke_errors = run_smoke_test(target_dir)
        if smoke_errors:
            ui.log_history("Crossover smoke test failed, retrying...", "warn")
            continue

        return True

    return False
