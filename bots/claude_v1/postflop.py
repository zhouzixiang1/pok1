"""Postflop logic: hand evaluation, Monte Carlo win rate estimation, and strategy."""

import random
from itertools import combinations


# ── Card helpers ──────────────────────────────────────────────────────────────

def card_rank(card):
    return card // 4 + 2


def card_suit(card):
    return card % 4


def evaluate_5(cards):
    """Evaluate a 5-card hand. Returns a comparable tuple (higher = better)."""
    ranks = sorted((card_rank(c) for c in cards), reverse=True)
    suits = [card_suit(c) for c in cards]
    rank_counts = {}
    for r in ranks:
        rank_counts[r] = rank_counts.get(r, 0) + 1
    groups = sorted(((cnt, r) for r, cnt in rank_counts.items()), reverse=True)

    is_flush = len(set(suits)) == 1
    unique = sorted(set(ranks), reverse=True)
    is_straight = False
    straight_high = 0
    if len(unique) == 5:
        if unique[0] - unique[4] == 4:
            is_straight = True
            straight_high = unique[0]
        # Wheel (A-2-3-4-5)
        if unique == [14, 5, 4, 3, 2]:
            is_straight = True
            straight_high = 5

    if is_flush and is_straight:
        return (8, straight_high)
    if groups[0][0] == 4:
        quad = groups[0][1]
        kicker = max(r for r in ranks if r != quad)
        return (7, quad, kicker)
    if groups[0][0] == 3 and len(groups) > 1 and groups[1][0] == 2:
        return (6, groups[0][1], groups[1][1])
    if is_flush:
        return (5,) + tuple(ranks)
    if is_straight:
        return (4, straight_high)
    if groups[0][0] == 3:
        trips = groups[0][1]
        kickers = sorted((r for r in ranks if r != trips), reverse=True)
        return (3, trips) + tuple(kickers)
    if groups[0][0] == 2 and len(groups) > 1 and groups[1][0] == 2:
        hp = max(groups[0][1], groups[1][1])
        lp = min(groups[0][1], groups[1][1])
        kicker = max(r for r in ranks if r not in (hp, lp))
        return (2, hp, lp, kicker)
    if groups[0][0] == 2:
        pair = groups[0][1]
        kickers = sorted((r for r in ranks if r != pair), reverse=True)
        return (1, pair) + tuple(kickers)
    return (0,) + tuple(ranks)


def evaluate_best(cards):
    """Find the best 5-card hand from 5, 6, or 7 cards."""
    if len(cards) <= 5:
        return evaluate_5(cards[:5])
    best = None
    for combo in combinations(cards, 5):
        score = evaluate_5(combo)
        if best is None or score > best:
            best = score
    return best


# ── Monte Carlo win rate estimation ──────────────────────────────────────────

def estimate_win_rate(my_cards, public_cards, n_simulations=200):
    """Estimate win rate via Monte Carlo simulation.

    Samples random opponent hands and remaining board cards.
    Returns (win_rate, tie_rate) in [0.0, 1.0].
    """
    full_deck = list(range(52))
    known = set(my_cards) | set(public_cards)
    remaining = [c for c in full_deck if c not in known]

    cards_needed = 5 - len(public_cards)  # board cards still to come
    wins = 0
    ties = 0

    for _ in range(n_simulations):
        random.shuffle(remaining)
        idx = 0
        # Assign opponent 2 hole cards
        opp_cards = remaining[idx:idx + 2]
        idx += 2
        # Fill remaining board
        board = list(public_cards) + remaining[idx:idx + cards_needed]

        my_hand = evaluate_best(my_cards + board)
        opp_hand = evaluate_best(opp_cards + board)

        if my_hand > opp_hand:
            wins += 1
        elif my_hand == opp_hand:
            ties += 1

    return wins / n_simulations, ties / n_simulations


# ── Draw detection ────────────────────────────────────────────────────────────

def count_flush_draw(my_cards, public_cards):
    """Return max count of cards in any single suit (from hole + board)."""
    all_cards = my_cards + public_cards
    suit_counts = {}
    for c in all_cards:
        s = card_suit(c)
        suit_counts[s] = suit_counts.get(s, 0) + 1
    return max(suit_counts.values()) if suit_counts else 0


def has_straight_draw(my_cards, public_cards):
    """Check for an open-ended or gutshot straight draw."""
    all_ranks = sorted(set(card_rank(c) for c in my_cards + public_cards))
    # Check consecutive runs
    for i in range(len(all_ranks) - 2):
        if all_ranks[i + 2] - all_ranks[i] <= 4:
            return True
    return False


# ── Postflop strategy ────────────────────────────────────────────────────────

def postflop_action(my_cards, public_cards, state, my_chips):
    """Return a raw action for postflop play.

    Uses Monte Carlo win rate estimation combined with pot odds.
    """
    to_call = state["to_call"]
    pot = state["pot"]
    big_blind = 100

    # Number of board cards determines the street
    n_public = len(public_cards)

    # Run Monte Carlo simulation
    n_sim = 300 if n_public >= 4 else 200
    win_rate, tie_rate = estimate_win_rate(my_cards, public_cards, n_sim)
    equity = win_rate + tie_rate * 0.5

    # Draw bonuses: boost equity slightly for draws on earlier streets
    draw_bonus = 0.0
    if n_public < 5:
        flush_cnt = count_flush_draw(my_cards, public_cards)
        if flush_cnt == 4:
            draw_bonus += 0.08
        if has_straight_draw(my_cards, public_cards):
            draw_bonus += 0.05

    adjusted_equity = min(1.0, equity + draw_bonus)

    # Pot odds
    if to_call > 0:
        pot_odds = to_call / (pot + to_call)
    else:
        pot_odds = 0.0

    # --- Decision logic ---
    if to_call == 0:
        # No bet facing us: check or bet for value
        if adjusted_equity >= 0.70:
            # Strong hand: bet for value (2/3 pot)
            bet_size = max(big_blind, int(pot * 0.66))
            if bet_size >= my_chips:
                return -2
            return bet_size
        elif adjusted_equity >= 0.55:
            # Medium-strong: small bet or check
            bet_size = max(big_blind, int(pot * 0.4))
            if bet_size >= my_chips:
                return 0
            return bet_size
        else:
            return 0  # check

    # Facing a bet
    if adjusted_equity >= 0.75:
        # Very strong: raise for value
        raise_size = max(int(pot * 0.75), to_call * 2)
        if raise_size >= my_chips:
            return -2
        return raise_size

    if adjusted_equity >= 0.55:
        # Strong enough to call
        return 0

    if adjusted_equity >= pot_odds + 0.05:
        # +EV call with draw or decent hand
        return 0

    if adjusted_equity >= 0.25 and to_call <= big_blind * 2:
        # Small bet, speculative call
        return 0

    # Fold
    return -1
