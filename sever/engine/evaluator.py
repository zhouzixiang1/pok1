"""手牌评估模块。

从 engine/judge.py 移植并适配 (suit, rank) 元组格式。
支持完整 9 级牌型判定 + kicker 比较，含 wheel 顺子 A-2-3-4-5。
"""
from itertools import combinations


# ── 牌型常量 ──────────────────────────────────────────────
HAND_NAMES = {
    1: "高牌", 2: "一对", 3: "两对", 4: "三条",
    5: "顺子", 6: "同花", 7: "葫芦", 8: "四条", 9: "同花顺",
}


def evaluate_hand(cards):
    """评估 5 张牌的牌型，返回可比较的 rank tuple。

    返回 (hand_type, *tiebreakers)，可直接用 > < == 比较。
    hand_type: 1=高牌 ... 9=同花顺
    """
    assert len(cards) == 5
    ranks = sorted([c.rank for c in cards], reverse=True)
    suits = [c.suit for c in cards]

    is_flush = len(set(suits)) == 1

    # 顺子判定
    unique_ranks = sorted(set(ranks), reverse=True)
    is_straight = False
    straight_high = 0

    if len(unique_ranks) == 5:
        if unique_ranks[0] - unique_ranks[4] == 4:
            is_straight = True
            straight_high = unique_ranks[0]
        # Wheel: A-2-3-4-5 (rank 值: 12,3,2,1,0 → 排序后 12,3,2,1,0)
        if unique_ranks == [12, 3, 2, 1, 0]:
            is_straight = True
            straight_high = 3  # wheel 以 5 为最高牌 (rank=3)

    # 计数分组
    counts = {}
    for r in ranks:
        counts[r] = counts.get(r, 0) + 1
    groups = sorted(counts.items(), key=lambda x: (x[1], x[0]), reverse=True)
    pattern = tuple(g[1] for g in groups)
    kickers = tuple(g[0] for g in groups)

    # 牌型判定（从高到低）
    if is_flush and is_straight:
        return (9, straight_high)
    if pattern == (4, 1):
        return (8, kickers)
    if pattern == (3, 2):
        return (7, kickers)
    if is_flush:
        return (6, tuple(ranks))
    if is_straight:
        return (5, straight_high)
    if pattern == (3, 1, 1):
        return (4, kickers)
    if pattern == (2, 2, 1):
        return (3, kickers)
    if pattern == (2, 1, 1, 1):
        return (2, kickers)
    return (1, tuple(ranks))


def best_hand(cards):
    """从 N 张牌 (N≥5) 中选出最佳 5 张组合。

    返回 (rank_tuple, best_five_cards)。
    """
    assert len(cards) >= 5
    best_rank = None
    best_cards = None
    for combo in combinations(cards, 5):
        combo = list(combo)
        rank = evaluate_hand(combo)
        if best_rank is None or rank > best_rank:
            best_rank = rank
            best_cards = combo[:]
    return best_rank, best_cards


def compare_hands(cards1, cards2):
    """比较两组牌（各 ≥5 张）的强弱。

    返回 >0 表示 cards1 赢，<0 表示 cards2 赢，0 表示平局。
    """
    rank1, _ = best_hand(cards1)
    rank2, _ = best_hand(cards2)
    if rank1 > rank2:
        return 1
    if rank1 < rank2:
        return -1
    return 0


def hand_name(rank_tuple):
    """将 rank tuple 转为可读名称。"""
    if rank_tuple is None:
        return "无"
    return HAND_NAMES.get(rank_tuple[0], "未知")
