"""Master Architect agent + analysis/summarization helpers.

All functions revolve around analyzing current state and generating evolution plans.
The Master produces worker task plans; analysis helpers prepare data for the Master.
"""

import json
import time
from collections import defaultdict

from evolution_infra import (
    run_claude_query, parse_json_output, substitute_template,
    locked_file, get_bot_dir, get_logs_dir, load_ratings, get_active_bots,
    _trim_to_budget, RESULTS_DIR, PROMPTS_DIR, EXPERIENCE_FILE,
    MATCH_HISTORY_FILE, REPLAY_DIR, WORKER_FAILURES_FILE,
    ARCHIVE_DIR,
    MAX_MASTER_RETRIES,
    Glicko2Player,
)


# ──────────────────────────────────────────────
# Master Analysis
# ──────────────────────────────────────────────

async def _run_master_analysis(source_v, next_v, stagnation_info, ui,
                               match_analysis="", performance_verification=""):
    """Run Master analysis — can run concurrently with daemon evaluation."""
    master_prompt = (PROMPTS_DIR / "master_prompt.md").read_text()
    # Apply section budgets to avoid experience_pool crowding out match_analysis
    match_analysis_trimmed = _trim_to_budget(match_analysis, 10_000, tail=True)
    perf_trimmed = _trim_to_budget(
        performance_verification if performance_verification
        else "No performance verification data available.",
        4_000
    )
    master_prompt = substitute_template(master_prompt, {
        "stagnation_info": stagnation_info,
        "match_analysis": match_analysis_trimmed,
        "performance_verification": perf_trimmed,
        "source_v": str(source_v),
    })
    master_ctx = (
        f"Current evolution: v{source_v} → v{next_v}\n"
        f"Bot directory: bots/claude_v{source_v}/\n"
        f"Ratings file: web/core/results/glicko_ratings.json\n"
        f"Rating history: web/core/results/rating_history.jsonl\n"
        f"Head-to-Head data: web/core/results/head_to_head.json\n"
        f"Bot stats: web/core/results/bot_stats.json\n"
        f"Experience pool: web/core/experience_pool.md  ← READ THIS, not evolution_workspace/experience_pool.md\n"
    )
    master_log_file = get_logs_dir(next_v) / "master_io.txt"

    for attempt in range(MAX_MASTER_RETRIES):
        ui.clear_io()
        output, _, _ = await run_claude_query(
            master_prompt + "\n" + master_ctx, [], ui,
            f"MASTER (Try {attempt+1})", master_log_file,
            tools=["Bash", "Read"],
        )
        data = parse_json_output(output)
        if data and "tasks" in data:
            ui.log_history("Master analysis complete.", "success")
            return data
        ui.log_history("Master output malformed JSON. Retrying...", "warn")
        import asyncio
        await asyncio.sleep(2)

    ui.log_history(f"Master failed to plan after {MAX_MASTER_RETRIES} retries.", "error")
    return None


async def _consolidate_experience_pool(ui):
    """Use LLM to deduplicate and consolidate the experience pool.

    Reads the current experience_pool.md, asks LLM to merge redundant entries,
    and writes back a consolidated version. Runs every 3 generations.

    Strategy: ask LLM to output the consolidated text directly (not edit in-place),
    then write it back here as a guaranteed fallback. The LLM's text output is the
    source of truth — no dependency on the agent using Edit tool.
    """
    if not EXPERIENCE_FILE.exists():
        return

    with locked_file(EXPERIENCE_FILE, "r") as ef:
        content = ef.read()
    if not content or len(content.split("\n")) < 20:
        return  # Too short to bother consolidating

    consolidate_prompt = (
        "You are an Experience Pool Consolidator. Your job is to clean up the experience pool file.\n\n"
        "RULES:\n"
        "1. Read the current experience pool content provided below.\n"
        "2. Merge duplicate or near-duplicate lessons into single, concise bullet points.\n"
        "3. Keep the most recent/relevant version of each lesson.\n"
        "4. Remove entries superseded by newer findings.\n"
        "5. Keep the total output under 70 lines.\n"
        "6. Output ONLY the consolidated markdown — no explanation, no code fences.\n\n"
        "CRITICAL — Output MUST use exactly these category headers (in this order):\n"
        "## OPPONENT_MODELING\n"
        "## POSTFLOP_STRATEGY\n"
        "## BLUFF_CALIBRATION\n"
        "## PARAMETER_TUNING\n"
        "## GENERAL\n"
        "## RECENT_LESSONS\n\n"
        "Sort each lesson into the most relevant category.\n"
        "RECENT_LESSONS should contain only lessons from the last 3 generations.\n\n"
        "LOCAL OPTIMA FLAG: If the same type of lesson appears for 3+ consecutive "
        "generations (e.g. 3 gens of constant-tuning in the same direction with no gain), "
        "append ' [POSSIBLY EXHAUSTED]' to that bullet so Master avoids repeating it.\n\n"
        "## Current experience_pool.md content:\n\n"
        f"{content}\n\n"
        "## Output the consolidated version now (plain markdown, no fences):"
    )
    log_file = get_logs_dir(0) / "experience_consolidation_io.txt"

    try:
        ui.clear_io()
        output, _, _ = await run_claude_query(
            consolidate_prompt, [], ui,
            "EXPERIENCE CONSOLIDATOR", log_file,
        )
        consolidated = output.strip() if output else ""
        # Strip accidental code fences if LLM added them
        if consolidated.startswith("```"):
            lines = consolidated.split("\n")
            consolidated = "\n".join(
                l for l in lines if not l.strip().startswith("```")
            ).strip()

        if consolidated and len(consolidated) > 50:
            tmp = EXPERIENCE_FILE.with_suffix(".tmp")
            tmp.write_text(consolidated + "\n", encoding="utf-8")
            tmp.replace(EXPERIENCE_FILE)
            ui.log_history("Experience pool consolidated and written back.", "success")
        else:
            ui.log_history("Experience pool consolidation produced no output — skipping write.", "warn")
    except Exception as e:
        ui.log_history(f"Experience pool consolidation failed: {e}", "warn")


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
        "3. If recent failures show critic demanding 'structural innovation' but workers being restricted\n"
        "   to 'constants only', this is a system deadlock, NOT real stagnation. Recommend 'continue'.\n"
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


# ──────────────────────────────────────────────
# Replay Analysis Helpers
# ──────────────────────────────────────────────

def _num_public_cards_to_street(n):
    """Map community-card count to street name."""
    return {0: "preflop", 3: "flop", 4: "turn", 5: "river"}.get(n, f"street_{n}")


def extract_street_patterns(games, bot_idx):
    """Extract per-street action frequencies from a list of game dicts.

    Returns a dict mapping street name → action counts, plus a compact text summary.
    Used by summarize_replay_for_analysis() to detect street-specific weaknesses.
    """
    streets = {s: defaultdict(int) for s in ("preflop", "flop", "turn", "river")}

    for g in games:
        for log in g.get("logs", []):
            out = log.get("output")
            if not out or not isinstance(out, dict):
                continue
            display = out.get("display")
            if not display or not isinstance(display, dict):
                continue
            action_info = display.get("last_action")
            if not action_info or not isinstance(action_info, dict):
                continue
            if action_info.get("player_id") != bot_idx:
                continue

            # Determine street from number of community cards present BEFORE this action
            n_community = len(display.get("public_cards", []))
            street = _num_public_cards_to_street(n_community)
            if street not in streets:
                continue

            act_val = action_info.get("action", 0)
            if act_val == -1:
                streets[street]["fold"] += 1
            elif act_val == -2:
                streets[street]["allin"] += 1
            elif act_val > 0:
                streets[street]["raise"] += 1
                # Track raise size relative to pot (pot available from display)
                pot = display.get("pot", 0)
                if pot > 0:
                    streets[street]["raise_size_sum"] += act_val
                    streets[street]["raise_size_pot_sum"] += act_val / pot
                    streets[street]["raise_size_count"] += 1
            elif act_val == 0:
                streets[street]["call"] += 1
            # Other values (e.g. timeout) are ignored

    # Build compact text lines
    lines = []
    for street in ("preflop", "flop", "turn", "river"):
        s = streets[street]
        total = s["fold"] + s["raise"] + s["call"] + s["allin"]
        if total == 0:
            continue
        parts = [
            f"fold={s['fold']*100//total}%",
            f"raise={s['raise']*100//total}%",
            f"call={s['call']*100//total}%",
        ]
        if s["allin"] > 0:
            parts.append(f"allin={s['allin']*100//total}%")
        if s.get("raise_size_count", 0) > 0:
            avg_ratio = s["raise_size_pot_sum"] / s["raise_size_count"]
            parts.append(f"avg_raise={avg_ratio:.1f}x_pot")
        lines.append(f"  {street.capitalize()}: {', '.join(parts)}")

    return "\n".join(lines) if lines else ""


def summarize_replay_for_analysis(replay_data, bot_name):
    """Extract structured statistics from replay JSON for LLM analysis.

    Compresses ~253 game logs into a compact ~500 token summary covering
    win rates, chip distribution, fold frequency, key action patterns,
    and per-street behaviour breakdown.
    """
    bot_idx = None
    opp_idx = None
    if replay_data.get("bot0") == bot_name:
        bot_idx, opp_idx = 0, 1
    elif replay_data.get("bot1") == bot_name:
        bot_idx, opp_idx = 1, 0
    if bot_idx is None:
        return ""

    games = replay_data.get("games", [])
    total_games = len(games)
    if total_games == 0:
        return ""

    wins = sum(1 for g in games if g.get("winner") == bot_idx)
    chip_deltas = [g.get(f"bot{bot_idx}_chips", 0.0) for g in games]

    lines = []
    draws = total_games - wins - sum(1 for g in games if g.get("winner") == opp_idx)
    losses = total_games - wins - draws
    result_str = f"{wins}W/{draws}D/{losses}L" if draws else f"{wins}W/{losses}L"
    lines.append(f"Match: {replay_data['bot0']} vs {replay_data['bot1']}, "
                 f"Result: {result_str} out of {total_games} games")
    lines.append(f"Chip delta: avg={sum(chip_deltas)/len(chip_deltas):.0f}, "
                 f"best={max(chip_deltas):.0f}, worst={min(chip_deltas):.0f}")

    # Per-game action analysis
    fold_count = 0
    raise_count = 0
    call_count = 0
    allin_count = 0
    big_pot_losses = []  # games where bot lost big pots

    for g in games:
        game_chip = g.get(f"bot{bot_idx}_chips", 0.0)
        logs = g.get("logs", [])

        for log in logs:
            out = log.get("output")
            if not out or not isinstance(out, dict):
                continue

            # Count actions from request content (bot's own actions)
            content = out.get("content", {})
            if isinstance(content, dict):
                player_data = content.get(str(bot_idx), {})
                if isinstance(player_data, dict):
                    history = player_data.get("history", [])
                    # Last entry in history is the most recent action
                    # But this is request data, action comes from response
                    continue

            # Count from display data
            display = out.get("display")
            if display and isinstance(display, dict):
                action = display.get("last_action")
                if action and isinstance(action, dict):
                    pid = action.get("player_id")
                    if pid == bot_idx:
                        act_val = action.get("action", 0)
                        if act_val == -1:
                            fold_count += 1
                        elif act_val == -2:
                            allin_count += 1
                        elif act_val > 0:
                            raise_count += 1
                        else:
                            call_count += 1

        if game_chip < -5000:
            big_pot_losses.append((g.get("game", "?"), game_chip))

    total_actions = fold_count + raise_count + call_count + allin_count
    if total_actions > 0:
        lines.append(f"Actions: fold={fold_count}({fold_count*100//total_actions}%), "
                     f"call={call_count}({call_count*100//total_actions}%), "
                     f"raise={raise_count}({raise_count*100//total_actions}%), "
                     f"allin={allin_count}({allin_count*100//total_actions}%)")

    if big_pot_losses:
        lines.append(f"Big losses (>-5000): {len(big_pot_losses)} games")
        for gid, delta in big_pot_losses[:3]:
            lines.append(f"  Game {gid}: {delta:.0f} chips")

    # Per-street action breakdown (StratFormer-style opponent modelling insight)
    street_summary = extract_street_patterns(games, bot_idx)
    if street_summary:
        lines.append("Per-street actions (bot):")
        lines.append(street_summary)

    return "\n".join(lines)


async def _analyze_recent_matches(source_v, ui, max_matches=8):
    """Use LLM to analyze recent replay data for the current bot.

    Collects both recent losses and close wins (margin < 3 games) to give
    the Master a balanced view of weaknesses and what's working.

    Returns a match analysis string to inject into Master's context, or ""
    if no replay data is available.
    """
    bot_name = f"claude_v{source_v}"

    if not MATCH_HISTORY_FILE.exists():
        return ""

    recent_losses = []
    close_wins = []

    with locked_file(MATCH_HISTORY_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            b0, b1 = entry.get("bot0"), entry.get("bot1")
            w0, w1 = entry.get("bot0_wins", 0), entry.get("bot1_wins", 0)

            if b0 == bot_name:
                bot_wins, opp_wins = w0, w1
            elif b1 == bot_name:
                bot_wins, opp_wins = w1, w0
            else:
                continue

            if opp_wins > bot_wins:
                recent_losses.append(entry)
            elif bot_wins > opp_wins and (bot_wins - opp_wins) <= 2:
                # Close win (margin ≤ 2 games) — reveals near-miss vulnerabilities
                close_wins.append(entry)

    if not recent_losses and not close_wins:
        return ""

    recent_losses = recent_losses[-max_matches:]
    close_wins = close_wins[-(max_matches // 2):]

    def _load_summaries(entries, label):
        result = []
        for entry in entries:
            replay_path = REPLAY_DIR / entry["id"]
            if not replay_path.exists():
                continue
            try:
                with locked_file(replay_path, "r") as rf:
                    replay_data = json.load(rf)
                summary = summarize_replay_for_analysis(replay_data, bot_name)
                if summary:
                    result.append(f"[{label}] {summary}")
            except (json.JSONDecodeError, OSError):
                continue
        return result

    summaries = _load_summaries(recent_losses, "LOSS") + _load_summaries(close_wins, "CLOSE WIN")

    if not summaries:
        return ""

    # Call LLM for analysis
    match_analyst_prompt = (
        "You are a Poker Hand Analyst specializing in Texas Hold'em bot strategy.\n"
        "Analyze the following match replay summaries (losses and close wins) for weaknesses and patterns.\n\n"
    )
    match_analyst_prompt += "## Recent Match Summaries (LOSS = bot lost, CLOSE WIN = bot won by ≤2 games)\n\n"
    for s in summaries:
        match_analyst_prompt += s + "\n\n"
    match_analyst_prompt += (
        "Based on the data above, identify:\n"
        "1. Key weaknesses (e.g., folding too much, not raising enough, poor all-in timing)\n"
        "2. Street-specific weaknesses from the Per-street actions data:\n"
        "   - River fold rate ≥40% → scared-money, consider expanding river calling range\n"
        "   - Flop raise rate ≤10% → too passive postflop, giving free cards\n"
        "   - Preflop raise rate ≤15% → limping too much, losing positional advantage\n"
        "   - avg_raise < 0.5x pot on river with big pot → underbetting strong hands\n"
        "3. Any detectable patterns (e.g., weak out-of-position, poor against aggressive opponents)\n"
        "4. What seems to be working (from close wins, if any)\n"
        "5. A concrete recommendation for improvement (be specific: which street, what change)\n\n"
        "Output ONLY a JSON block:\n"
        "```json\n"
        '{"weaknesses": ["..."], "street_weaknesses": {"river": "...", "flop": "..."}, '
        '"patterns": "...", "working": "...", "recommendation": "..."}\n'
        "```\n"
        "Keep it concise — 2-3 weaknesses, specific street observations, 1 recommendation."
    )

    log_file = get_logs_dir(source_v) / "match_analyst_io.txt"
    try:
        output, _, _ = await run_claude_query(
            match_analyst_prompt, [], ui,
            "MATCH ANALYST", log_file,
        )
        if not output or not output.strip():
            # Retry once if match analyst returned empty (529/timeout)
            output, _, _ = await run_claude_query(
                match_analyst_prompt, [], ui,
                "MATCH ANALYST (retry)", log_file,
            )
        return output or ""
    except Exception:
        return ""


# ──────────────────────────────────────────────
# Archivist Analysis
# ──────────────────────────────────────────────

async def _run_archivist_analysis(version, source_v, snapshot, ui):
    """Run Cycle Archivist LLM analysis on a completed generation.

    Called conditionally (rating decline, experience pool growth, or forced).
    Returns a JSON dict with assessment and strategic advice.
    """
    prompt_file = PROMPTS_DIR / "archivist.md"
    if prompt_file.exists():
        prompt = prompt_file.read_text()
    else:
        prompt = (
            "You are the Cycle Archivist for a poker bot evolution system.\n"
            "Analyze the completed generation and provide a strategic assessment.\n\n"
            "## Archive Snapshot\n{snapshot}\n\n"
            "Output ONLY a JSON block:\n"
            "```json\n"
            '{"generation_assessment": "improvement|neutral|regression", '
            '"archive_notes": "brief summary of what this generation achieved", '
            '"experience_updates": ["lesson to add to experience pool"], '
            '"strategic_advice": "suggestion for next generation"}\n'
            "```\n"
        )

    prompt = prompt.replace("{snapshot}", json.dumps(snapshot, indent=2, ensure_ascii=False))

    # Build recent rating trend context
    trend_lines = []
    for check_v in range(max(1, version - 4), version + 1):
        check_archive = ARCHIVE_DIR / f"v{check_v}.json"
        if check_archive.exists():
            try:
                with open(check_archive, "r") as f:
                    s = json.load(f)
                r = s.get("rating", {}).get("r", "?")
                wr = s.get("h2h_avg_wr", "?")
                trend_lines.append(f"v{check_v}: r={r}, h2h_avg_wr={wr}")
            except Exception:
                pass
    if trend_lines:
        prompt += f"\n\n## Recent Rating Trend\n" + "\n".join(trend_lines)

    log_file = get_logs_dir(version) / "archivist_io.txt"
    try:
        output, _, _ = await run_claude_query(
            prompt, [], ui,
            "CYCLE ARCHIVIST", log_file,
            tools=["Bash", "Read"],
        )
        data = parse_json_output(output)
        if data and isinstance(data, dict):
            return data
        return {"archive_notes": output[:300] if output else "No output"}
    except Exception as e:
        return {"error": str(e)}
