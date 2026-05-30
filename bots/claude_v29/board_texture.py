"""Board texture classification and position-aware aggression scaling."""
from card_utils import card_number, card_suit


def classify_board_texture(board_cards):
    """Classify board texture into strategic categories.

    Returns one of: 'dry', 'semi-wet', 'wet', 'completed'

    Criteria:
    - 'completed': trips/quad on board, 4+ same suit on turn+, or straight on board
    - 'wet': flush draw (3+ same suit) or strong straight draw (4+ connected within 4-gap)
    - 'semi-wet': paired board, moderate connectivity, or backdoor draws on turn
    - 'dry': rainbow, unconnected, unpaired
    """
    if len(board_cards) < 3:
        return 'dry'

    board_ranks = [card_number(c) for c in board_cards]
    board_suits = [card_suit(c) for c in board_cards]

    rank_counts = {}
    for r in board_ranks:
        rank_counts[r] = rank_counts.get(r, 0) + 1
    max_rank_count = max(rank_counts.values())

    suit_counts = {}
    for s in board_suits:
        suit_counts[s] = suit_counts.get(s, 0) + 1
    max_suit_count = max(suit_counts.values())

    unique_ranks = sorted(set(board_ranks))

    # ---- Completed: trips/quad, 4+ flush, or straight on board ----
    if max_rank_count >= 3:
        return 'completed'
    if max_suit_count >= 4 and len(board_cards) >= 4:
        return 'completed'
    if len(unique_ranks) >= 5:
        for i in range(len(unique_ranks) - 4):
            if unique_ranks[i + 4] - unique_ranks[i] == 4:
                return 'completed'
    # A-low straight (A-2-3-4-5)
    if 14 in unique_ranks and len(unique_ranks) >= 5:
        ace_low = sorted(set(1 if r == 14 else r for r in unique_ranks))
        for i in range(len(ace_low) - 4):
            if ace_low[i + 4] - ace_low[i] == 4:
                return 'completed'

    # ---- Wet: flush draw or strong straight draw ----
    flush_draw = max_suit_count >= 3

    straight_draw = False
    if len(unique_ranks) >= 4:
        for i in range(len(unique_ranks) - 3):
            if unique_ranks[i + 3] - unique_ranks[i] <= 4:
                straight_draw = True
                break
    if not straight_draw and 14 in unique_ranks:
        ace_low = sorted(set(1 if r == 14 else r for r in unique_ranks))
        if len(ace_low) >= 4:
            for i in range(len(ace_low) - 3):
                if ace_low[i + 3] - ace_low[i] <= 4:
                    straight_draw = True
                    break

    if flush_draw or straight_draw:
        return 'wet'

    # ---- Semi-wet: paired, moderate connectivity, or backdoor ----
    paired = max_rank_count == 2

    moderate_connected = False
    if len(unique_ranks) >= 3:
        for i in range(len(unique_ranks) - 1):
            if unique_ranks[i + 1] - unique_ranks[i] <= 3:
                moderate_connected = True
                break

    backdoor = max_suit_count == 2 and len(board_cards) >= 4

    if paired or moderate_connected or backdoor:
        return 'semi-wet'

    return 'dry'


def get_position_aggression_factor(my_hand, board_cards, street, pot_size, is_sb):
    """Compute position-aware aggression multiplier based on board texture.

    Returns factor:
    - > 1.0: widen range, more aggressive (bluffs on dry boards)
    - < 1.0: tighten range, more cautious (wet/completed boards)
    - == 1.0: neutral (preflop)

    Strategy:
    - Dry boards: increase bluff/semi-bluff; opponent less likely connected
    - Wet boards: decrease bluff, tighten value range
    - SB (first postflop): check-raise lines on dry boards
    - BB: donk-bet lines on wet boards to deny free cards
    - Completed boards: purely value-oriented, no bluffs
    """
    if street <= 0:
        return 1.0

    texture = classify_board_texture(board_cards)

    factor = 1.0

    if texture == 'dry':
        factor += 0.07
        if is_sb:
            factor += 0.04
        else:
            factor += 0.03

    elif texture == 'semi-wet':
        factor -= 0.02
        if not is_sb:
            factor += 0.02

    elif texture == 'wet':
        factor -= 0.06
        if is_sb:
            factor -= 0.04
        if not is_sb:
            factor += 0.03

    elif texture == 'completed':
        factor -= 0.10
        if is_sb:
            factor -= 0.04

    return factor
