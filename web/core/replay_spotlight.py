"""Replay spotlight: identify critical hands with largest chip swings.

Pure data transformation — no LLM calls. Returns a compact structured summary
suitable for prompt injection.
"""

import json
import os
from glob import glob


def _card_str(card_int):
    """Convert integer card to human-readable string."""
    if card_int is None:
        return "?"
    ranks = "23456789TJQKA"
    suits = "hdsc"
    rank = card_int // 4
    suit = card_int % 4
    return ranks[rank] + suits[suit]


def _board_str(public_cards):
    """Convert list of public card ints to board string."""
    if not public_cards:
        return ""
    return " ".join(_card_str(c) for c in public_cards)


def _action_str(act_val):
    """Convert action integer to readable string."""
    if act_val == -1:
        return "fold"
    if act_val == -2:
        return "allin"
    if act_val == 0:
        return "call/check"
    if act_val > 0:
        return f"raise_to {act_val}"
    return "?"


def _hand_strength_assessment(bot_cards, public_cards):
    """Very rough hand strength estimate for decision assessment."""
    if not bot_cards or len(bot_cards) != 2:
        return "unknown"
    all_cards = bot_cards + public_cards
    if len(all_cards) < 5:
        return "preflop"
    # Simple heuristic: count pairs / trips / quads among all cards
    rank_counts = {}
    for c in all_cards:
        rank = c // 4
        rank_counts[rank] = rank_counts.get(rank, 0) + 1
    max_count = max(rank_counts.values()) if rank_counts else 1
    if max_count >= 4:
        return "quads+"
    if max_count == 3:
        return "set/trips"
    pairs = sum(1 for v in rank_counts.values() if v == 2)
    if pairs >= 2:
        return "two_pair+"
    if pairs == 1:
        return "one_pair"
    # Check flush potential (4+ of same suit)
    suit_counts = {}
    for c in all_cards:
        suit = c % 4
        suit_counts[suit] = suit_counts.get(suit, 0) + 1
    if max(suit_counts.values()) >= 5:
        return "flush"
    if max(suit_counts.values()) == 4:
        return "flush_draw"
    # Check straight potential (4+ connected)
    unique_ranks = sorted(set(rank_counts.keys()))
    if len(unique_ranks) >= 5:
        return "straight_possible"
    return "high_card/weak"


def _extract_hand_swing(game, bot_idx, opp_idx):
    """Extract chip swing and key details for a single game/hand.

    Returns dict with hand details or None if insufficient data.
    """
    logs = game.get("logs", [])
    if not logs:
        return None

    bot_cards = None
    public_cards = []
    bot_actions = []
    opp_actions = []
    pot_before = 0
    pot_after = 0
    stage = "preflop"
    last_bot_action = None
    last_opp_action = None

    for log in logs:
        out = log.get("output")
        if not out or not isinstance(out, dict):
            continue

        # Extract bot hole cards from request content
        content = out.get("content", {})
        if isinstance(content, dict):
            player_data = content.get(str(bot_idx), {})
            if isinstance(player_data, dict) and bot_cards is None:
                hist = player_data.get("history", [])
                if hist and len(hist) >= 2:
                    # First two entries are usually hole cards
                    bot_cards = hist[:2]

        display = out.get("display", {})
        if not display or not isinstance(display, dict):
            continue

        # Track public cards / stage
        pc = display.get("public_cards", [])
        if pc and len(pc) > len(public_cards):
            public_cards = pc
            stage_map = {0: "preflop", 3: "flop", 4: "turn", 5: "river"}
            stage = stage_map.get(len(public_cards), stage)

        # Track pot
        pot = display.get("pot", 0)
        if pot > 0:
            pot_before = pot_after or pot
            pot_after = pot

        # Track actions
        action = display.get("last_action", {})
        if action and isinstance(action, dict):
            pid = action.get("player_id")
            act_val = action.get("action", 0)
            if pid == bot_idx:
                bot_actions.append((stage, act_val, pot))
                last_bot_action = (stage, act_val)
            elif pid == opp_idx:
                opp_actions.append((stage, act_val, pot))
                last_opp_action = (stage, act_val)

    chip_delta = game.get(f"bot{bot_idx}_chips", 0)
    swing = abs(chip_delta)

    # Determine key stage where most action happened
    key_stage = stage
    if bot_actions:
        # Stage of last significant action (raise/allin/fold)
        for st, act, _ in reversed(bot_actions):
            if act != 0:
                key_stage = st
                break

    # Decision assessment
    assessment = ""
    if last_bot_action:
        st, act = last_bot_action
        act_desc = _action_str(act)
        strength = _hand_strength_assessment(bot_cards or [], public_cards)
        if act == -1:
            assessment = f"folded with {strength}"
        elif act == -2:
            assessment = f"allin with {strength}"
        elif act > 0:
            assessment = f"raised with {strength}"
        else:
            assessment = f"called with {strength}"

    return {
        "hand_num": game.get("game", "?"),
        "stage": key_stage,
        "board": _board_str(public_cards),
        "bot_cards": _board_str(bot_cards or []),
        "bot_action": _action_str(last_bot_action[1]) if last_bot_action else "?",
        "opp_action": _action_str(last_opp_action[1]) if last_opp_action else "?",
        "pot_before": pot_before,
        "pot_after": pot_after,
        "chip_delta": chip_delta,
        "swing": swing,
        "assessment": assessment,
    }


def find_critical_hands(bot_name, replays_dir, max_hands=10, recent_n_files=20):
    """Find the hands with largest chip swings for a given bot.

    Args:
        bot_name: Name of the bot to analyze (e.g. "claude_v27")
        replays_dir: Directory containing replay JSON files
        max_hands: Max number of critical hands to return (default 10)
        recent_n_files: Number of most recent replay files to scan (default 20)

    Returns:
        Compact structured summary string (under 2000 chars) suitable for prompt injection.
    """
    if not os.path.isdir(replays_dir):
        return f"No replays directory: {replays_dir}"

    # Find recent replay files
    pattern = os.path.join(replays_dir, "*.json")
    files = glob(pattern)
    if not files:
        return "No replay files found."

    # Sort by mtime descending, take recent_n_files
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    files = files[:recent_n_files]

    all_swings = []

    for path in files:
        try:
            with open(path, "r", encoding="utf-8") as f:
                replay = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        # Determine bot_idx
        bot_idx = None
        opp_idx = None
        if replay.get("bot0") == bot_name:
            bot_idx, opp_idx = 0, 1
        elif replay.get("bot1") == bot_name:
            bot_idx, opp_idx = 1, 0
        else:
            continue

        games = replay.get("games", [])
        for game in games:
            hand = _extract_hand_swing(game, bot_idx, opp_idx)
            if hand and hand["swing"] > 0:
                all_swings.append(hand)

    if not all_swings:
        return f"No hands with chip swings found for {bot_name}."

    # Sort by swing descending, take top max_hands
    all_swings.sort(key=lambda h: h["swing"], reverse=True)
    top = all_swings[:max_hands]

    # Build compact summary
    lines = [f"Critical hands for {bot_name} (top {len(top)} by swing):"]
    for h in top:
        line = (
            f"H{h['hand_num']} {h['stage']}: "
            f"board=[{h['board']}] "
            f"bot=[{h['bot_cards']}] "
            f"act={h['bot_action']} "
            f"opp={h['opp_action']} "
            f"pot={h['pot_before']}->{h['pot_after']} "
            f"delta={h['chip_delta']:+.0f} "
            f"({h['assessment']})"
        )
        lines.append(line)

    total_swing = sum(h["swing"] for h in top)
    avg_swing = total_swing / len(top) if top else 0
    lines.append(f"Summary: {len(top)} hands, avg_swing={avg_swing:.0f}, total_swing={total_swing:.0f}")

    result = "\n".join(lines)
    # Hard truncate to 2000 chars if needed
    if len(result) > 2000:
        result = result[:1997] + "..."
    return result
