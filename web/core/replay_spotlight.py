"""Replay spotlight: identify critical hands with largest chip swings.

Pure data transformation — no LLM calls. Returns a compact structured summary
suitable for prompt injection.

IMPORTANT — data model (verified against real replay JSONs):

Each ``games[i]`` entry is NOT a single poker hand. It is a full 70-hand
MIRROR HALF-GAME (botzone batches the whole match, then emits one ``games[]``
entry whose ``logs[]`` contains every request/response of all 70 hands).

Within ``logs[]``, request entries alternate with response entries:

* request entry: ``output`` is a dict carrying ``display.matchdata`` (with
  ``hand`` 0..69 and the cumulative ``total_win_chips`` = ``[c0, c1]``), plus
  ``display.public_cards``, ``display.last_action`` and ``content`` keyed by
  player id (``content[str(bot_idx)]["my_cards"]`` = that player's hole cards).
* response entry: ``output`` is ``None`` and the log is keyed by player id.

``total_win_chips[bot_idx]`` at the START of hand *k* equals the cumulative net
chips won over the first *k* hands. Therefore the TRUE single-hand swing is::

    delta(k) = twc_start(k+1) - twc_start(k)        # for k < last hand
    delta(last) = game["bot{idx}_chips"] - twc_start(last)   # last hand

The previous implementation treated each ``games[i]`` as one hand, using
``hand 0``'s hole cards together with ``hand 69``'s board and the full 70-hand
cumulative chip delta — i.e. it constructed fictional hands. This module splits
each half-game into its real constituent hands and ranks them by the true
single-hand swing.
"""

import json
import os
from glob import glob
from typing import Iterator, List


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


def _twc_value(total_win_chips, idx):
    """Safely extract cumulative chips for player ``idx`` from a twc list."""
    if not total_win_chips or not isinstance(total_win_chips, list):
        return 0
    if idx < 0 or idx >= len(total_win_chips):
        return 0
    val = total_win_chips[idx]
    return val if isinstance(val, (int, float)) else 0


def _stage_for_board_len(n):
    """Map public-card count to a stage label."""
    return {0: "preflop", 3: "flop", 4: "turn", 5: "river"}.get(n, "preflop")


def _iter_hands(game, bot_idx, opp_idx) -> Iterator[dict]:
    """Yield one dict per REAL poker hand inside a mirror half-game.

    Splits ``game["logs"]`` by ``display.matchdata.hand`` and derives each
    hand's true single-hand chip delta by differencing adjacent cumulative
    ``total_win_chips`` values (the last hand falls back to
    ``game["bot{idx}_chips"]``).

    Each yielded dict has::

        hand_num, bot_cards (list[int]), public_cards (list[int] max-len seen),
        stage, pot_before, pot_after, bot_actions, opp_actions,
        last_bot_action, last_opp_action, chip_delta, swing

    Hands with no usable data are skipped.
    """
    logs = game.get("logs", [])
    if not logs:
        return

    # Bucket request entries by matchdata.hand, preserving first-seen order.
    hand_buckets = {}
    hand_order: List[int] = []
    for log in logs:
        out = log.get("output")
        if not isinstance(out, dict):
            continue
        display = out.get("display")
        if not isinstance(display, dict) or "matchdata" not in display:
            continue
        matchdata = display["matchdata"]
        if not isinstance(matchdata, dict):
            continue
        hn = matchdata.get("hand")
        if hn is None:
            continue
        if hn not in hand_buckets:
            hand_buckets[hn] = []
            hand_order.append(hn)
        hand_buckets[hn].append(log)

    hand_nums = sorted(hand_buckets.keys())
    final_bot_chips = game.get(f"bot{bot_idx}_chips", 0)
    if not isinstance(final_bot_chips, (int, float)):
        final_bot_chips = 0

    for i, hn in enumerate(hand_nums):
        entries = hand_buckets[hn]

        # twc at the START of this hand = first request entry's cumulative.
        first_md = entries[0]["output"]["display"]["matchdata"]
        start_twc = _twc_value(first_md.get("total_win_chips"), bot_idx)

        # twc at the END = next hand's start twc; last hand uses final chips.
        if i + 1 < len(hand_nums):
            next_md = hand_buckets[hand_nums[i + 1]][0]["output"]["display"]["matchdata"]
            end_twc = _twc_value(next_md.get("total_win_chips"), bot_idx)
        else:
            end_twc = final_bot_chips
        chip_delta = end_twc - start_twc

        bot_cards = None
        public_cards: List[int] = []
        bot_actions = []
        opp_actions = []
        pot_before = 0
        pot_after = 0
        last_bot_action = None
        last_opp_action = None

        for log in entries:
            out = log["output"]
            content = out.get("content", {})
            if isinstance(content, dict):
                player_data = content.get(str(bot_idx), {})
                if (
                    isinstance(player_data, dict)
                    and bot_cards is None
                    and player_data.get("my_cards")
                ):
                    bot_cards = list(player_data["my_cards"])

            display = out.get("display", {})
            if not isinstance(display, dict):
                continue

            pc = display.get("public_cards", [])
            if pc and len(pc) > len(public_cards):
                public_cards = list(pc)

            stage = _stage_for_board_len(len(public_cards))

            pot = display.get("pot", 0)
            if pot and isinstance(pot, (int, float)) and pot > 0:
                if pot_before == 0:
                    pot_before = pot
                pot_after = pot

            action = display.get("last_action")
            if action and isinstance(action, dict):
                pid = action.get("player_id")
                act_val = action.get("action", 0)
                if pid == bot_idx:
                    bot_actions.append((stage, act_val, pot))
                    last_bot_action = (stage, act_val)
                elif pid == opp_idx:
                    opp_actions.append((stage, act_val, pot))
                    last_opp_action = (stage, act_val)

        if bot_cards is None and not public_cards and chip_delta == 0:
            # Pure summary entry with no real hand data — skip.
            continue

        yield {
            "hand_num": hn,
            "bot_cards": bot_cards or [],
            "public_cards": public_cards,
            "stage": _stage_for_board_len(len(public_cards)),
            "pot_before": pot_before,
            "pot_after": pot_after,
            "bot_actions": bot_actions,
            "opp_actions": opp_actions,
            "last_bot_action": last_bot_action,
            "last_opp_action": last_opp_action,
            "chip_delta": chip_delta,
            "swing": abs(chip_delta),
        }


def _summarize_hand(hand, game_num):
    """Build the public output dict (with board/card strings + assessment)."""
    bot_cards = hand["bot_cards"]
    public_cards = hand["public_cards"]
    last_bot_action = hand["last_bot_action"]
    last_opp_action = hand["last_opp_action"]

    stage = hand["stage"]
    if hand["bot_actions"]:
        # Stage of last significant action (raise/allin/fold) if present.
        for st, act, _ in reversed(hand["bot_actions"]):
            if act != 0:
                stage = st
                break

    assessment = ""
    if last_bot_action:
        _, act = last_bot_action
        strength = _hand_strength_assessment(bot_cards, public_cards)
        if act == -1:
            assessment = f"folded with {strength}"
        elif act == -2:
            assessment = f"allin with {strength}"
        elif act > 0:
            assessment = f"raised with {strength}"
        else:
            assessment = f"called with {strength}"

    return {
        "game_num": game_num,
        "hand_num": hand["hand_num"],
        "stage": stage,
        "board": _board_str(public_cards),
        "bot_cards": _board_str(bot_cards),
        "bot_action": _action_str(last_bot_action[1]) if last_bot_action else "?",
        "opp_action": _action_str(last_opp_action[1]) if last_opp_action else "?",
        "pot_before": hand["pot_before"],
        "pot_after": hand["pot_after"],
        "chip_delta": hand["chip_delta"],
        "swing": hand["swing"],
        "assessment": assessment,
    }


def _extract_hand_swing(game, bot_idx, opp_idx):
    """Thin wrapper: return the largest single-hand swing within one half-game.

    Preserved for backward compatibility with existing tests. Internally this
    now iterates the REAL per-hand generator (``_iter_hands``) and picks the
    hand with the largest true single-hand swing, rather than treating the
    whole 70-hand half-game as one fictional hand. Per-hand hole cards are read
    from ``content[str(bot_idx)]["my_cards"]`` inside ``_iter_hands``.

    Returns the summarized hand dict or None if the half-game has no hands.
    """
    best = None
    best_swing = -1
    game_num = game.get("game", "?")
    for hand in _iter_hands(game, bot_idx, opp_idx):
        if hand["swing"] > best_swing:
            best_swing = hand["swing"]
            best = hand
    if best is None:
        return None
    return _summarize_hand(best, game_num)


def find_critical_hands(bot_name, replays_dir, max_hands=10, recent_n_files=20):
    """Find the hands with largest chip swings for a given bot.

    Ranks by the TRUE single-hand swing (each ``games[i]`` is a 70-hand mirror
    half-game; we split it into its real constituent hands via
    ``_iter_hands``).

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
            game_num = game.get("game", "?")
            for hand in _iter_hands(game, bot_idx, opp_idx):
                if hand["swing"] > 0:
                    all_swings.append(_summarize_hand(hand, game_num))

    if not all_swings:
        return f"No hands with chip swings found for {bot_name}."

    # Sort by swing descending, take top max_hands
    all_swings.sort(key=lambda h: h["swing"], reverse=True)
    top = all_swings[:max_hands]

    # Build compact summary
    lines = [f"Critical hands for {bot_name} (top {len(top)} by swing):"]
    for h in top:
        gprefix = f"G{h['game_num']}" if h["game_num"] != "?" else ""
        line = (
            f"{gprefix}H{h['hand_num']} {h['stage']}: "
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
