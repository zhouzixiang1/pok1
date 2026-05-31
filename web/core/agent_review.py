"""Review-stage LLM agents: Critic, Performance Verification, and Crossover.

These agents evaluate worker output and verify strategic improvements.
"""

import json

from evolution_infra import (
    run_claude_query, parse_json_output, substitute_template,
    locked_file, get_bot_dir, get_logs_dir, load_ratings, get_active_bots,
    verify_code, run_smoke_test,
    PROMPTS_DIR, RESULTS_DIR, MATCH_HISTORY_FILE, H2H_FILE, BOT_STATS_FILE,
    MAX_CROSSOVER_RETRIES,
    Glicko2Player,
)


async def _run_critic(next_v, source_v, master_plan_str, ui):
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

    log_file = get_logs_dir(next_v) / "critic_io.txt"
    try:
        output, _, _ = await run_claude_query(
            critic_prompt, [], ui, "STRATEGY CRITIC", log_file,
            tools=["Bash", "Read"],
        )
        data = parse_json_output(output)
        if data and "score" in data:
            # Normalise: score >= 6 → approved
            data.setdefault("approved", data["score"] >= 6)
            return data
    except Exception as e:
        ui.log_history(f"Critic error: {e}. Defaulting to approved.", "warn")

    return {"score": 0, "approved": False, "feedback": "Critic unavailable — defaulting to rejected.", "local_optima_warning": False}


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
        except Exception:
            pass

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
        except Exception:
            pass

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
        except Exception:
            pass

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
        except Exception:
            pass

    # ── Build prompt ──
    prompt = (
        "You are a Performance Verification Analyst for a self-evolving poker bot system.\n"
        "Your job: synthesise the quantitative data below into actionable LLM-readable insight.\n\n"
        f"Current bot under analysis: {bot_name}\n\n"
        "## Performance History (last 10 periods)\n"
        + ("\n".join(gen_trend_lines) if gen_trend_lines else "  No history available") + "\n\n"
        "## Overall Win Rate\n"
        + (bot_stats_line if bot_stats_line else "  No stats available") + "\n\n"
        "## Head-to-Head Results (per-opponent)\n"
        + ("\n".join(l for _, l in h2h_lines) if h2h_lines else "  No H2H data available") + "\n\n"
        "## Top Active Bots (by H2H avg win rate)\n"
        + "\n".join(ratings_lines) + "\n\n"
        "Produce a JSON block answering:\n"
        "```json\n"
        '{"trend": "improving|stagnant|declining",\n'
        ' "verified_improvements": ["list of things that actually helped recent gens"],\n'
        ' "persistent_weaknesses": ["list of recurring problems not yet fixed"],\n'
        ' "diversity_needed": true|false,\n'
        ' "diversity_reason": "why diversity is needed (or null)",\n'
        ' "suggestion": "one concrete high-priority suggestion for next gen"}\n'
        "```\n"
        "Set `diversity_needed: true` if: trend is stagnant/declining for 2+ gens, "
        "OR the last 2 gens applied the same type of change. Be direct and concise."
    )

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
    crossover_prompt = (PROMPTS_DIR / "crossover_prompt.md").read_text()
    crossover_prompt = substitute_template(crossover_prompt, {
        "parent_a_version": str(parent_a_v),
        "parent_b_version": str(parent_b_v),
        "version": str(target_v),
    })

    target_dir = get_bot_dir(target_v)
    log_file = get_logs_dir(target_v) / "crossover_io.txt"

    for attempt in range(MAX_CROSSOVER_RETRIES):
        ui.clear_io()
        ui.set_status(f"Crossover v{parent_a_v}×v{parent_b_v}→v{target_v} (Try {attempt+1})", is_working=True)
        await run_claude_query(
            crossover_prompt, [], ui,
            f"CROSSOVER v{parent_a_v}×v{parent_b_v}→v{target_v}",
            log_file,
            tools=["Bash", "Read", "Edit"],
        )

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
