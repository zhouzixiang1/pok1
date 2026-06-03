"""Replay analysis: extract structured statistics from match replay JSON.

Pure data transformation — no LLM calls. Used by the match analyst agent
to summarize replay data before sending to the LLM.
"""

import json
from collections import defaultdict


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

            # Count from request content (bot's own actions)
            content = out.get("content", {})
            if isinstance(content, dict):
                player_data = content.get(str(bot_idx), {})
                if isinstance(player_data, dict):
                    history = player_data.get("history", [])
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
