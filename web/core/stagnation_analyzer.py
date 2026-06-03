"""Stagnation analysis: detect whether evolution is stuck in a local optimum.

Uses LLM to analyze rating trends, lineage, daemon period history, and
worker failures to determine if the evolution strategy needs adjustment.
"""

import json

from evolution_infra import (
    run_claude_query, parse_json_output,
    locked_file, get_logs_dir, load_ratings,
    RESULTS_DIR, WORKER_FAILURES_FILE,
    Glicko2Player,
)


async def _analyze_stagnation(source_v, active_bots, ratings, ui):
    """Use LLM to analyze rating trends and determine if stagnation is real.

    Returns a dict with: is_stagnant, confidence, recommendation, branch_from, reason.
    Returns None on failure.
    """
    from tool_helpers import load_h2h_avg_winrates, load_h2h_avg_winrates_with_coverage
    h2h_winrates = load_h2h_avg_winrates()
    coverage_data = load_h2h_avg_winrates_with_coverage()

    # ── Data sufficiency check ──
    bot_name = f"claude_v{source_v}"
    bot_cov = coverage_data.get(bot_name, {})
    opp_coverage = bot_cov.get("opponent_coverage", 1.0)
    opp_eval = bot_cov.get("opponents_evaluated", 0)
    opp_total = bot_cov.get("opponents_total", 0)

    if opp_coverage < 0.8:
        return {
            "is_stagnant": False,
            "confidence": "low",
            "recommendation": "continue",
            "branch_from": None,
            "reason": f"Insufficient opponent coverage for stagnation analysis: {opp_eval}/{opp_total} opponents evaluated ({opp_coverage:.0%}). Need more daemon evaluation games before stagnation can be assessed.",
        }

    # ── Generation-level trend (from git tags, not daemon periods) ──
    gen_trend_lines = []
    try:
        from evolution_core import _git
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
    except Exception:
        pass

    # ── Lineage info (parent chain) ──
    lineage_lines = []
    try:
        from evolution_infra import git_get_parent
        for check_v in range(max(1, source_v - 5), source_v + 1):
            parent = git_get_parent(check_v)
            if parent is not None:
                lineage_lines.append(f"  v{check_v} ← parent: v{parent}")
    except Exception:
        pass

    # ── Daemon period history (top-3, not just top-1) ──
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

    # ── Recent worker failures (for context) ──
    failure_ctx = ""
    try:
        from evolution_infra import WORKER_FAILURES_FILE
        if WORKER_FAILURES_FILE.exists():
            with locked_file(WORKER_FAILURES_FILE, "r") as f:
                flines = f.readlines()
            recent = [json.loads(l.strip()) for l in flines[-5:] if l.strip()]
            if recent:
                failure_ctx = "Recent critic/worker rejections:\n"
                for e in recent:
                    failure_ctx += f"  - v{e.get('gen','?')} {e.get('role','?')}: {e.get('error','')[:120]}\n"
    except Exception:
        pass

    sorted_bots = sorted(active_bots, key=lambda b: h2h_winrates.get(b, 0.0), reverse=True)[:5]

    prompt = (
        "You are a rating trend analyst for a poker bot evolution system.\n"
        "Analyze whether the evolution is truly stagnating.\n\n"
        f"Current bot: {bot_name} (coverage: {opp_eval}/{opp_total} opponents = {opp_coverage:.0%})\n"
        f"Top 5 bots by H2H avg win rate:\n"
    )
    for b in sorted_bots:
        p = ratings.get(b, Glicko2Player())
        wr = h2h_winrates.get(b, 0.0)
        cov_info = coverage_data.get(b, {})
        cov_pct = cov_info.get("opponent_coverage", 1.0)
        cov_tag = f" [LOW COVERAGE {cov_pct:.0%}]" if cov_pct < 0.8 else ""
        prompt += f"  {b}: h2h_avg_wr={wr:.2%} (r={p.r:.0f} rd={p.rd:.0f}){cov_tag}\n"

    if gen_trend_lines:
        prompt += f"\nGeneration-level trend (most recent 8 bots):\n" + "\n".join(gen_trend_lines) + "\n"
    if lineage_lines:
        prompt += f"\nLineage (parent chain):\n" + "\n".join(lineage_lines) + "\n"
    if history_ctx:
        prompt += f"\nDaemon period history (last 10 periods, top-3):\n{history_ctx}\n"
    if failure_ctx:
        prompt += f"\n{failure_ctx}\n"

    prompt += (
        "IMPORTANT CONSIDERATIONS:\n"
        "1. A bot with coverage < 80% may have an inflated or deflated h2h_avg_wr — treat with caution.\n"
        "2. 'Stagnation' means multiple consecutive generations FAILED to improve. If the last successful\n"
        "   bot is strong and only 1-2 generations failed, that's not stagnation — it's normal iteration.\n"
        "3. If recent failures show critic repeatedly demanding 'structural innovation' but workers keep\n"
        "   producing constant-tuning changes, this is a system deadlock. Recommend 'crossover' to break\n"
        "   the impasse — forcing a combination of diverse strategies is more effective than retrying.\n"
        "4. If recommending branch_from, check lineage: do NOT branch from an ancestor if a later\n"
        "   descendant already improved from that ancestor.\n\n"
        "Is this real stagnation? Answer in JSON only:\n"
        '```json\n'
        '{"is_stagnant": true/false, "confidence": "high/medium/low", '
        '"recommendation": "continue|branch|crossover", '
        '"branch_from": "claude_vN" or null, '
        '"reason": "brief explanation"}\n'
        '```'
    )

    log_file = get_logs_dir(source_v) / "stagnation_analysis.txt"
    for attempt in range(3):
        try:
            output, _, _ = await run_claude_query(
                prompt, [], ui, "STAGNATION ANALYST", log_file,
            )
            result = parse_json_output(output)
            if result:
                return result
            # Empty output (529/timeout) — retry with backoff
            ui.log_history(f"Stagnation analysis returned empty (attempt {attempt+1}/3), retrying...", "warn")
        except Exception as e:
            ui.log_history(f"Stagnation analysis failed: {e} (attempt {attempt+1}/3)", "warn")
        if attempt < 2:
            import asyncio
            await asyncio.sleep(30 * (attempt + 1))
    return None
