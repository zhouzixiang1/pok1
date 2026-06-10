"""
Bot action statistics extraction from replay files.

Pure Python, no external dependencies beyond json / os / pathlib.
"""

import json
import os
from pathlib import Path


def extract_hands_from_replay(replay_json):
    """
    Extract all hands from a single replay JSON.

    Returns a list of hand dicts, each with:
      - stages: list of stage names in order
      - actions: list of dicts {player, action, amount, stage}
      - hole_cards: dict mapping player name -> [card1, card2]
      - community: dict mapping stage -> list of cards
      - pot: final pot size (from last stage's pot or settlement)
      - winner: player name who won, or None
      - showdown: bool, whether hand went to showdown
      - win_amount: amount won by winner (net, from settlement)
    """
    if isinstance(replay_json, (str, bytes)):
        replay_json = json.loads(replay_json)

    players = replay_json.get("players", [])
    if not players:
        return []

    raw_hands = replay_json.get("hands", [])
    if not raw_hands:
        return []

    hands = []
    for hand in raw_hands:
        stages = []
        actions = []
        hole_cards = {}
        community = {}
        pot = 0
        winner = None
        win_amount = 0
        showdown = False

        # Hole cards
        for p in players:
            hc = hand.get("hole_cards", {}).get(p)
            if hc:
                hole_cards[p] = list(hc)

        # Process each stage
        for stage_name in ["preflop", "flop", "turn", "river", "showdown"]:
            stage_data = hand.get(stage_name)
            if not stage_data:
                continue
            stages.append(stage_name)

            # Community cards
            cards = stage_data.get("community")
            if cards is not None:
                community[stage_name] = list(cards)

            # Actions
            for act in stage_data.get("actions", []):
                actions.append({
                    "player": act.get("player"),
                    "action": act.get("action"),
                    "amount": act.get("amount", 0),
                    "stage": stage_name,
                })

            # Pot from stage
            stage_pot = stage_data.get("pot")
            if stage_pot is not None:
                pot = stage_pot

            # Showdown / winner
            if stage_name == "showdown":
                showdown = True
                win = stage_data.get("winner")
                if win:
                    winner = win
                amt = stage_data.get("win_amount")
                if amt is not None:
                    win_amount = amt

        # Fallback winner from settlement if not at showdown
        if winner is None:
            settlement = hand.get("settlement")
            if settlement:
                for p in players:
                    amt = settlement.get(p)
                    if amt is not None and amt > 0:
                        winner = p
                        win_amount = amt
                        break

        # Fallback pot from settlement total
        if pot == 0:
            settlement = hand.get("settlement")
            if settlement:
                pot = sum(v for v in settlement.values() if v and v > 0)

        hands.append({
            "stages": stages,
            "actions": actions,
            "hole_cards": hole_cards,
            "community": community,
            "pot": pot,
            "winner": winner,
            "showdown": showdown,
            "win_amount": win_amount,
        })

    return hands


def _classify_hand_strength(hand, bot_name):
    """
    Rough classification of whether the bot had a 'made hand' at showdown.
    Uses showdown info if available; otherwise returns 'unknown'.
    """
    if not hand.get("showdown"):
        return "unknown"
    # If bot won at showdown, treat as made hand
    if hand.get("winner") == bot_name:
        return "made"
    # If bot lost at showdown, treat as not-made (simplification)
    return "air"


def compute_bot_action_stats(bot_name, replays_dir):
    """
    Compute aggregate action statistics for a bot across all replays in a directory.

    Returns a dict with:
      - vpip, pfr, fold_to_3bet, flop_cbet, turn_barrel,
        river_value_bet, river_bluff, fold_to_river_bet,
        showdown_win, avg_won_pot, avg_lost_pot,
        wtsd, aggression_freq
    """
    replays_dir = Path(replays_dir)
    if not replays_dir.exists():
        return {}

    # Counters
    total_hands = 0

    # VPIP / PFR
    vpip_opportunities = 0
    vpip_count = 0
    pfr_count = 0

    # 3-bet fold
    faced_3bet = 0
    folded_to_3bet = 0

    # C-bet
    cbet_opportunities = 0
    cbet_count = 0

    # Turn barrel
    barrel_opportunities = 0
    barrel_count = 0

    # River value bet / bluff
    river_value_bet_opportunities = 0
    river_value_bet_count = 0
    river_bluff_opportunities = 0
    river_bluff_count = 0

    # Fold to river bet
    faced_river_bet = 0
    folded_to_river_bet = 0

    # Showdown
    showdowns = 0
    showdown_wins = 0

    # Pots
    won_pots = []
    lost_pots = []

    # WTSD
    wtsd_opportunities = 0
    wtsd_count = 0

    # Aggression frequency
    aggressive_actions = 0
    passive_actions = 0

    for entry in os.listdir(replays_dir):
        if not entry.endswith(".json"):
            continue
        filepath = replays_dir / entry
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                replay_json = json.load(f)
        except Exception:
            continue

        hands = extract_hands_from_replay(replay_json)
        for hand in hands:
            total_hands += 1
            actions = hand["actions"]
            players_in_hand = set(a["player"] for a in actions)
            if bot_name not in players_in_hand:
                continue

            # --- VPIP / PFR ---
            preflop_actions = [a for a in actions if a["stage"] == "preflop"]
            bot_preflop = [a for a in preflop_actions if a["player"] == bot_name]
            if bot_preflop:
                vpip_opportunities += 1
                first_action = bot_preflop[0]["action"]
                if first_action in ("raise", "allin"):
                    vpip_count += 1
                    pfr_count += 1
                elif first_action == "call":
                    vpip_count += 1

            # --- Fold to 3bet ---
            # Detect if bot faced a 3bet preflop and folded
            # Simplification: if there are 2+ raises preflop and bot's last preflop action is fold
            preflop_raises = [a for a in preflop_actions if a["action"] in ("raise", "allin")]
            if len(preflop_raises) >= 2:
                bot_last_preflop = None
                for a in reversed(preflop_actions):
                    if a["player"] == bot_name:
                        bot_last_preflop = a["action"]
                        break
                if bot_last_preflop is not None:
                    faced_3bet += 1
                    if bot_last_preflop == "fold":
                        folded_to_3bet += 1

            # --- C-bet on flop ---
            # Bot was PFR (made last preflop raise) and flop exists
            if preflop_raises:
                last_preflop_raiser = preflop_raises[-1]["player"]
                if last_preflop_raiser == bot_name:
                    flop_actions = [a for a in actions if a["stage"] == "flop"]
                    if flop_actions:
                        cbet_opportunities += 1
                        first_flop = flop_actions[0]
                        if first_flop["player"] == bot_name and first_flop["action"] in ("raise", "bet", "allin"):
                            cbet_count += 1

            # --- Turn barrel ---
            # Bot c-bet flop (first flop action is bet/raise by bot) and turn exists
            flop_actions = [a for a in actions if a["stage"] == "flop"]
            if flop_actions and flop_actions[0]["player"] == bot_name and flop_actions[0]["action"] in ("raise", "bet", "allin"):
                turn_actions = [a for a in actions if a["stage"] == "turn"]
                if turn_actions:
                    barrel_opportunities += 1
                    first_turn = turn_actions[0]
                    if first_turn["player"] == bot_name and first_turn["action"] in ("raise", "bet", "allin"):
                        barrel_count += 1

            # --- River value bet / bluff ---
            river_actions = [a for a in actions if a["stage"] == "river"]
            if river_actions:
                # Find bot's first action on river
                for a in river_actions:
                    if a["player"] == bot_name:
                        if a["action"] in ("raise", "bet", "allin"):
                            strength = _classify_hand_strength(hand, bot_name)
                            if strength == "made":
                                river_value_bet_opportunities += 1
                                river_value_bet_count += 1
                            elif strength == "air":
                                river_bluff_opportunities += 1
                                river_bluff_count += 1
                        break

            # --- Fold to river bet ---
            if river_actions:
                # Did an opponent bet/raise on river and bot fold?
                river_bet_made = any(
                    a["player"] != bot_name and a["action"] in ("raise", "bet", "allin")
                    for a in river_actions
                )
                if river_bet_made:
                    bot_last_river = None
                    for a in reversed(river_actions):
                        if a["player"] == bot_name:
                            bot_last_river = a["action"]
                            break
                    if bot_last_river is not None:
                        faced_river_bet += 1
                        if bot_last_river == "fold":
                            folded_to_river_bet += 1

            # --- Showdown win ---
            if hand.get("showdown"):
                showdowns += 1
                if hand.get("winner") == bot_name:
                    showdown_wins += 1

            # --- Pots won / lost ---
            if hand.get("winner") == bot_name:
                won_pots.append(hand.get("pot", 0))
            elif hand.get("winner") is not None:
                lost_pots.append(hand.get("pot", 0))

            # --- WTSD (went to showdown) ---
            # Opportunity: bot saw flop (i.e., didn't fold preflop)
            if bot_preflop and bot_preflop[-1]["action"] != "fold":
                wtsd_opportunities += 1
                if hand.get("showdown"):
                    wtsd_count += 1

            # --- Aggression frequency ---
            for a in actions:
                if a["player"] != bot_name:
                    continue
                act = a["action"]
                if act in ("raise", "bet", "allin"):
                    aggressive_actions += 1
                elif act in ("call", "check"):
                    passive_actions += 1

    def _pct(num, den):
        return round(num / den, 4) if den > 0 else 0.0

    def _avg(vals):
        return round(sum(vals) / len(vals), 2) if vals else 0.0

    stats = {
        "total_hands": total_hands,
        "vpip": _pct(vpip_count, vpip_opportunities),
        "pfr": _pct(pfr_count, vpip_opportunities),
        "fold_to_3bet": _pct(folded_to_3bet, faced_3bet),
        "flop_cbet": _pct(cbet_count, cbet_opportunities),
        "turn_barrel": _pct(barrel_count, barrel_opportunities),
        "river_value_bet": _pct(river_value_bet_count, river_value_bet_opportunities),
        "river_bluff": _pct(river_bluff_count, river_bluff_opportunities),
        "fold_to_river_bet": _pct(folded_to_river_bet, faced_river_bet),
        "showdown_win": _pct(showdown_wins, showdowns),
        "avg_won_pot": _avg(won_pots),
        "avg_lost_pot": _avg(lost_pots),
        "wtsd": _pct(wtsd_count, wtsd_opportunities),
        "aggression_freq": _pct(aggressive_actions, aggressive_actions + passive_actions),
    }

    return stats
