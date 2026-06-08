import itertools

from constants import N_PLAYERS


def clamp(value, low, high):
    return max(low, min(high, value))


def card_suit(card):
    return card % 4


def card_number(card):
    return card // 4 + 2


def next_player(player, offset):
    return (player + offset) % N_PLAYERS


def evaluate_5(cards):
    ranks = sorted((card_number(c) for c in cards), reverse=True)
    suits = [card_suit(c) for c in cards]
    rank_counts = {}
    for rank in ranks:
        rank_counts[rank] = rank_counts.get(rank, 0) + 1
    groups = sorted(((count, rank) for rank, count in rank_counts.items()), reverse=True)

    is_flush = len(set(suits)) == 1
    unique_ranks = sorted(set(ranks), reverse=True)

    is_straight = False
    straight_high = 0
    if len(unique_ranks) == 5:
        if unique_ranks[0] - unique_ranks[4] == 4:
            is_straight = True
            straight_high = unique_ranks[0]

    if is_flush and is_straight:
        return (8, straight_high)
    if groups[0][0] == 4:
        quad = groups[0][1]
        kicker = max(rank for rank in ranks if rank != quad)
        return (7, quad, kicker)
    if groups[0][0] == 3 and groups[1][0] == 2:
        return (6, groups[0][1], groups[1][1])
    if is_flush:
        return (5, *ranks)
    if is_straight:
        return (4, straight_high)
    if groups[0][0] == 3:
        trips = groups[0][1]
        kickers = sorted((rank for rank in ranks if rank != trips), reverse=True)
        return (3, trips, *kickers)
    if groups[0][0] == 2 and groups[1][0] == 2:
        high_pair = max(groups[0][1], groups[1][1])
        low_pair = min(groups[0][1], groups[1][1])
        kicker = max(rank for rank in ranks if rank not in (high_pair, low_pair))
        return (2, high_pair, low_pair, kicker)
    if groups[0][0] == 2:
        pair = groups[0][1]
        kickers = sorted((rank for rank in ranks if rank != pair), reverse=True)
        return (1, pair, *kickers)
    return (0, *ranks)


def evaluate_best(cards):
    if len(cards) == 5:
        return evaluate_5(cards)
    best = None
    for combo in itertools.combinations(cards, 5):
        score = evaluate_5(combo)
        if best is None or score > best:
            best = score
    return best


def evaluate_7(cards):
    return evaluate_best(cards)
