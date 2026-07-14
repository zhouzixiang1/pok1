"""Lightweight deterministic match commentary generator.

Extracts key events from replay data and generates concise descriptions.
No LLM required — works in real-time during playback.
"""

from pathlib import Path


# Card helpers
def _card_name(card: int) -> str:
    if card is None:
        return "?"
    number = card // 4 + 2
    suit = card % 4
    rank = {2: "2", 3: "3", 4: "4", 5: "5", 6: "6", 7: "7", 8: "8",
            9: "9", 10: "T", 11: "J", 12: "Q", 13: "K", 14: "A"}.get(number, str(number))
    suit_sym = {0: "♥", 1: "♦", 2: "♠", 3: "♣"}.get(suit, "?")
    return f"{rank}{suit_sym}"


def _action_name(action_val) -> str:
    if action_val is None:
        return "act"
    if action_val == 0:
        return "calls"
    if action_val == -1:
        return "folds"
    if action_val == -2:
        return "ALL IN"
    return f"raises to {action_val}"


def _hand_type_str(cards: list) -> str:
    if not cards or len(cards) < 5:
        return ""
    return ""  # Simplified — hand type evaluation would need judge.py


def generate_match_commentary(replay_data: dict) -> dict:
    """Generate per-game commentary from replay data.

    Returns {game_index: commentary_string}.
    """
    games = replay_data.get("games", [])
    bot0 = replay_data.get("bot0", "Bot 0")
    bot1 = replay_data.get("bot1", "Bot 1")

    result = {}

    for game in games:
        idx = game.get("game", 0)
        winner = game.get("winner")
        bot0_chips = game.get("bot0_chips", 0)
        logs = game.get("logs", [])

        events = []
        max_pot = 0
        allin_player = None
        showdown = False
        player_cards = {}

        for log in logs:
            out = log.get("output")
            if not out or not isinstance(out, dict):
                continue

            display = out.get("display")
            if display and isinstance(display, dict):
                pot = display.get("pot", 0)
                if pot > max_pot:
                    max_pot = pot

                # Track cards
                pc = display.get("player_cards")
                if pc and isinstance(pc, list) and len(pc) == 2:
                    if isinstance(pc[0], list) and len(pc[0]) == 2:
                        player_cards[0] = pc[0]
                    if isinstance(pc[1], list) and len(pc[1]) == 2:
                        player_cards[1] = pc[1]

                # Track last action
                action = display.get("last_action")
                if action and isinstance(action, dict):
                    pid = action.get("player_id")
                    act_val = action.get("action", 0)
                    pname = bot0 if pid == 0 else bot1

                    if act_val == -2:
                        allin_player = pname
                        events.append(f"{pname} goes ALL IN")
                    elif act_val > 0 and pot > 5000:
                        events.append(f"{pname} {_action_name(act_val)} (pot: {pot:,})")

                # Showdown detection
                round_num = display.get("round", 0)
                if round_num == 4:
                    showdown = True

        # Build commentary
        parts = []

        # Show cards if available
        if player_cards:
            c0 = " ".join(_card_name(c) for c in player_cards.get(0, []))
            c1 = " ".join(_card_name(c) for c in player_cards.get(1, []))
            if c0:
                parts.append(f"{bot0}: [{c0}]")
            if c1:
                parts.append(f"{bot1}: [{c1}]")

        # Key events
        if events:
            parts.append(events[-1])  # Most important event

        # Result
        if winner is not None:
            winner_name = bot0 if winner == 0 else bot1 if winner == 1 else "Draw"
            chip_str = f"+{abs(bot0_chips):,}" if bot0_chips > 0 else f"-{abs(bot0_chips):,}"
            if abs(bot0_chips) > 10000:
                parts.append(f"🏆 {winner_name} wins big! ({chip_str} chips)")
            elif allin_player:
                parts.append(f"🏆 {winner_name} wins the all-in showdown ({chip_str})")
            elif winner != -1:
                parts.append(f"🏆 {winner_name} wins ({chip_str})")

        result[str(idx)] = " | ".join(parts) if parts else "No notable events"

    return result
