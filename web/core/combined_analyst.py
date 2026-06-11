"""Combined Evolution Analyst: merges stagnation detection and performance verification.

Replaces two separate LLM calls (_analyze_stagnation + _run_performance_verification)
with a single call that produces a unified JSON output.

Includes optional statistical pre-check to skip LLM for clear-cut cases.
"""

import json
import logging

log = logging.getLogger('pok.analyst')

from evolution_infra import (
    run_claude_query, parse_json_output, substitute_template,
    locked_file, get_logs_dir, load_ratings, get_active_bots,
    RESULTS_DIR, WORKER_FAILURES_FILE, PROMPTS_DIR,
    Glicko2Player,
)


def _statistical_stagnation_check(source_v, ratings):
    """Pure-code stagnation check using sliding window on rating history.

    Returns (is_stagnant, confidence, trend_delta) or None if insufficient data.
    - trend_delta < 5: stagnant (high confidence)
    - trend_delta > 20: improving (high confidence)
    - trend_delta in [5, 20]: ambiguous — needs LLM analysis
    """
    history_file = RESULTS_DIR / "rating_history.jsonl"
    if not history_file.exists():
        return None

    bot_name = f"claude_v{source_v}"
    try:
        with locked_file(history_file, "r") as f:
            lines = f.readlines()
    except Exception:
        return None

    # Extract bot's rating from last 10 periods
    recent_ratings = []
    for line in lines[-10:]:
        try:
            snap = json.loads(line.strip())
            bot_rating = snap.get("ratings", {}).get(bot_name, {})
            r = bot_rating.get("r")
            if r is not None:
                recent_ratings.append(r)
        except (json.JSONDecodeError, KeyError):
            continue

    if len(recent_ratings) < 6:
        return None  # Not enough data for comparison

    # Compare recent 3 vs previous 3
    recent_avg = sum(recent_ratings[-3:]) / 3
    previous_avg = sum(recent_ratings[-6:-3]) / 3
    delta = recent_avg - previous_avg

    bot_rd = ratings.get(bot_name, Glicko2Player()).rd if ratings else 350

    # High RD means rating is unreliable — statistical check is not trustworthy
    if bot_rd > 150:
        return None  # Let LLM decide

    if abs(delta) < 5:
        return (True, "high", delta)  # Flat — stagnant
    elif delta > 20:
        return (False, "high", delta)  # Clearly improving
    elif delta < -20:
        return (True, "high", delta)  # Clearly declining — needs intervention
    else:
        return None  # Ambiguous — needs LLM


async def _run_combined_analysis(source_v, active_bots, ratings, ui, prev_critic_info: str = ""):
    """Combined stagnation + performance analysis in a single LLM call.

    Returns a dict with unified fields:
    - is_stagnant, confidence, trend, diversity_needed, recommendation,
      branch_from, verified_improvements, persistent_weaknesses, reason, suggestion
    Returns a safe default on failure.
    """
    from tool_helpers import load_h2h_avg_winrates, load_h2h_avg_winrates_with_coverage

    safe_default = {
        "is_stagnant": False,
        "confidence": "low",
        "trend": "improving",
        "diversity_needed": False,
        "diversity_reason": None,
        "recommendation": "continue",
        "branch_from": None,
        "verified_improvements": [],
        "persistent_weaknesses": [],
        "reason": "Analysis failed, defaulting to continue",
        "suggestion": None,
        "recommended_source": "",
        "source_rationale": "",
    }

    h2h_winrates = load_h2h_avg_winrates()
    coverage_data = load_h2h_avg_winrates_with_coverage()

    # ── Data sufficiency check ──
    bot_name = f"claude_v{source_v}"
    bot_cov = coverage_data.get(bot_name, {})
    opp_coverage = bot_cov.get("opponent_coverage", 1.0)
    opp_eval = bot_cov.get("opponents_evaluated", 0)
    opp_total = bot_cov.get("opponents_total", 0)

    if opp_coverage < 0.8:
        safe_default["reason"] = (
            f"Insufficient opponent coverage: {opp_eval}/{opp_total} ({opp_coverage:.0%}). "
            "Need more daemon evaluation games before analysis is reliable."
        )
        return safe_default

    # ── Statistical pre-check — skip LLM if trend is clear-cut ──
    stat_result = _statistical_stagnation_check(source_v, ratings)
    if stat_result is not None:
        is_stagnant, confidence, delta = stat_result
        if confidence == "high":
            trend = "stagnant" if is_stagnant else ("improving" if delta > 0 else "declining")
            return {
                "is_stagnant": is_stagnant,
                "confidence": confidence,
                "trend": trend,
                "diversity_needed": is_stagnant,
                "diversity_reason": f"Rating delta={delta:.1f} over last 6 periods — {'flat' if is_stagnant else 'clear trend'}" if is_stagnant else None,
                "recommendation": "crossover" if is_stagnant else "continue",
                "branch_from": None,
                "verified_improvements": [],
                "persistent_weaknesses": [],
                "reason": f"Statistical check: rating delta={delta:.1f} (recent 3 vs previous 3 periods). {'Stagnation detected' if is_stagnant else 'Improvement trend'}.",
                "suggestion": None,
                "recommended_source": "",
                "source_rationale": "Statistical pre-check did not evaluate source recommendation — LLM call was skipped.",
            }

    # ── Build context data (merged from both old analysts) ──

    # Generation trend (from git tags)
    gen_trend_lines = []
    try:
        from evolution_infra import _git, git_get_parent
        tag_output = _git("tag", "-l", "bot-v*", "--sort=version:refname", check=False)
        tags = [t.strip() for t in tag_output.splitlines() if t.strip()]
        recent_tags = tags[-8:] if len(tags) > 8 else tags
        for tag in recent_tags:
            try:
                v_str = tag.replace("bot-v", "")
                v = int(v_str)
                v_name = f"claude_v{v}"
                cov = coverage_data.get(v_name, {})
                wr = cov.get("h2h_avg_wr", h2h_winrates.get(v_name, 0.0))
                cov_pct = cov.get("opponent_coverage", 0.0)
                gen_trend_lines.append(f"  v{v}: h2h_avg_wr={wr:.2%} (coverage={cov_pct:.0%})")
            except (ValueError, KeyError):
                continue
    except Exception as e:
        log.debug('Generation trend computation failed: %s', e)
    lineage_lines = []
    try:
        from evolution_infra import git_get_parent
        for check_v in range(max(1, source_v - 5), source_v + 1):
            parent = git_get_parent(check_v)
            if parent is not None:
                lineage_lines.append(f"  v{check_v} ← parent: v{parent}")
    except Exception as e:
        log.debug('Lineage analysis failed: %s', e)
    history_file = RESULTS_DIR / "rating_history.jsonl"
    history_ctx = ""
    if history_file.exists():
        with locked_file(history_file, "r") as f:
            lines = f.readlines()
        for line in lines[-10:]:
            try:
                snap = json.loads(line.strip())
                wr_data = snap.get("win_rates", {})
                wrs = [(k, v["h2h_avg_wr"]) for k, v in wr_data.items() if v.get("h2h_avg_wr") is not None]
                if wrs:
                    wrs.sort(key=lambda x: x[1], reverse=True)
                    top3 = ", ".join(f"{k}={v:.3f}" for k, v in wrs[:3])
                    history_ctx += f"  Period {snap['period']}: {top3}\n"
                else:
                    top = max(p["r"] for p in snap["ratings"].values())
                    history_ctx += f"  Period {snap['period']}: top_r={top:.0f}\n"
            except (json.JSONDecodeError, KeyError):
                continue

    # Worker failures
    failure_ctx = ""
    try:
        if WORKER_FAILURES_FILE.exists():
            with locked_file(WORKER_FAILURES_FILE, "r") as f:
                flines = f.readlines()
            recent = [json.loads(l.strip()) for l in flines[-5:] if l.strip()]
            if recent:
                failure_ctx = "Recent critic/worker rejections:\n"
                for e in recent:
                    failure_ctx += f"  - v{e.get('gen','?')} {e.get('role','?')}: {e.get('error','')[:120]}\n"
    except Exception as e:
        log.debug('Worker failure context load failed: %s', e)
    sorted_bots = sorted(active_bots, key=lambda b: h2h_winrates.get(b, 0.0), reverse=True)[:5]
    top_bots_lines = []
    for b in sorted_bots:
        p = ratings.get(b, Glicko2Player())
        wr = h2h_winrates.get(b, 0.0)
        cov_info = coverage_data.get(b, {})
        cov_pct = cov_info.get("opponent_coverage", 1.0)
        cov_tag = f" [LOW COVERAGE {cov_pct:.0%}]" if cov_pct < 0.8 else ""
        top_bots_lines.append(f"  {b}: h2h_avg_wr={wr:.2%} (r={p.r:.0f} rd={p.rd:.0f}){cov_tag}")

    # Bot stats
    bot_stats_line = "  No stats available"
    bot_stats_file = RESULTS_DIR / "bot_stats.json"
    if bot_stats_file.exists():
        try:
            with locked_file(bot_stats_file, "r") as f:
                bs_data = json.load(f)
            bs = bs_data.get(bot_name, {})
            g = bs.get("games", 0)
            wr = bs.get("win_rate", 0.0)
            if g > 0:
                bot_stats_line = f"  {bot_name}: {wr:.0%} overall ({g} games)"
        except Exception as e:
            log.debug('Bot stats computation failed: %s', e)

    # H2H per-opponent
    h2h_lines = []
    h2h_file = RESULTS_DIR / "head_to_head.json"
    if h2h_file.exists():
        try:
            with locked_file(h2h_file, "r") as f:
                h2h_data = json.load(f)
            for k, v in h2h_data.items():
                parts = k.split(" vs ")
                if len(parts) != 2:
                    continue
                a_name, b_name = parts
                if bot_name not in (a_name, b_name):
                    continue
                opponent = b_name if bot_name == a_name else a_name
                bot_w = v.get("a_wins", 0) if bot_name == a_name else v.get("b_wins", 0)
                opp_w = v.get("b_wins", 0) if bot_name == a_name else v.get("a_wins", 0)
                total = bot_w + opp_w
                if total > 0:
                    wr = bot_w / total
                    tag = " STRENGTH" if wr > 0.60 else " WEAKNESS" if wr < 0.40 else ""
                    h2h_lines.append((wr, f"  vs {opponent}: {bot_w}W-{opp_w}L ({wr:.0%}){tag}"))
            h2h_lines.sort(key=lambda x: x[0])
        except Exception as e:
            log.debug('H2H per-opponent analysis failed: %s', e)

    # RD warning
    bot_rd = ratings.get(bot_name, Glicko2Player()).rd if ratings else 350
    rd_warning = ""
    if bot_rd > 200:
        rd_warning = (
            f"IMPORTANT: rd={bot_rd:.0f} (>200) — rating is VERY uncertain. "
            "Trend analysis is unreliable. Treat any 'trend' with extreme skepticism."
        )
    elif bot_rd > 100:
        rd_warning = (
            f"NOTE: rd={bot_rd:.0f} (>100) — rating is moderately uncertain. "
            "Be cautious about interpreting small changes as meaningful trends."
        )

    # ── Build and run prompt ──
    template_file = PROMPTS_DIR / "combined_analyst.md"
    if not template_file.exists():
        return safe_default

    prompt = template_file.read_text()
    prompt = substitute_template(prompt, {
        "bot_name": bot_name,
        "opp_eval": str(opp_eval),
        "opp_total": str(opp_total),
        "opp_coverage": f"{opp_coverage:.0%}",
        "rd_warning": rd_warning,
        "top_bots": "\n".join(top_bots_lines),
        "critic_insights": prev_critic_info,
        "generation_trend": "\n".join(gen_trend_lines) if gen_trend_lines else "  No generation trend data",
        "lineage": "\n".join(lineage_lines) if lineage_lines else "  No lineage data",
        "daemon_history": history_ctx if history_ctx else "  No daemon history",
        "bot_stats": bot_stats_line,
        "h2h_results": "\n".join(l for _, l in h2h_lines) if h2h_lines else "  No H2H data",
        "failure_context": failure_ctx if failure_ctx else "  No recent failures",
    })

    log_file = get_logs_dir(source_v) / "combined_analysis.txt"
    for attempt in range(3):
        try:
            output, _, _ = await run_claude_query(
                prompt, [], ui, "COMBINED ANALYST", log_file,
            )
            result = parse_json_output(output)
            if result:
                from output_schema import validate_agent_output
                result, errors = validate_agent_output("combined_analyst", result)
                if errors:
                    ui.log_history(f"Combined analyst validation issues: {'; '.join(errors[:3])}", "warn")
                # Ensure all expected fields exist
                result.setdefault("is_stagnant", False)
                result.setdefault("confidence", "low")
                result.setdefault("trend", "stagnant")
                result.setdefault("diversity_needed", result.get("is_stagnant", False))
                result.setdefault("diversity_reason", None)
                result.setdefault("recommendation", "continue")
                result.setdefault("branch_from", None)
                result.setdefault("verified_improvements", [])
                result.setdefault("persistent_weaknesses", [])
                result.setdefault("reason", "")
                result.setdefault("suggestion", None)
                result.setdefault("recommended_source", "")
                result.setdefault("source_rationale", "")
                return result
            ui.log_history(f"Combined analyst returned empty (attempt {attempt+1}/3), retrying...", "warn")
        except Exception as e:
            ui.log_history(f"Combined analyst failed: {e} (attempt {attempt+1}/3)", "warn")
        if attempt < 2:
            import asyncio
            await asyncio.sleep(30 * (attempt + 1))

    return safe_default
