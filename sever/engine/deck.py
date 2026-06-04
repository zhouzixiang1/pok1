"""卡牌与牌组模块。

卡牌格式严格遵循国赛协议文档：
  <suit,rank> 其中 suit ∈ {0=♠, 1=♥, 2=♦, 3=♣}，rank ∈ {0=2, ..., 12=A}
"""
import random

SUIT_NAMES = {0: "♠", 1: "♥", 2: "♦", 3: "♣"}
RANK_NAMES = {
    0: "2", 1: "3", 2: "4", 3: "5", 4: "6", 5: "7", 6: "8",
    7: "9", 8: "10", 9: "J", 10: "Q", 11: "K", 12: "A",
}


class Card:
    """协议卡牌：<suit,rank>，suit 0-3=♠♥♦♣，rank 0-12=2-A"""
    __slots__ = ("suit", "rank")

    def __init__(self, suit: int, rank: int):
        assert 0 <= suit <= 3, f"suit must be 0-3, got {suit}"
        assert 0 <= rank <= 12, f"rank must be 0-12, got {rank}"
        self.suit = suit
        self.rank = rank

    def to_str(self) -> str:
        """协议格式：<suit,rank>"""
        return f"<{self.suit},{self.rank}>"

    def display(self) -> str:
        """可读格式：♠A, ♥10, ♦K 等"""
        return f"{SUIT_NAMES[self.suit]}{RANK_NAMES[self.rank]}"

    def __repr__(self):
        return self.display()

    def __eq__(self, other):
        return isinstance(other, Card) and self.suit == other.suit and self.rank == other.rank

    def __hash__(self):
        return hash((self.suit, self.rank))


class Deck:
    """52 张标准牌，Fisher-Yates 洗牌后依次发牌。"""

    def __init__(self, seed=None):
        rng = random.Random(seed) if seed is not None else random
        self.cards = [Card(s, r) for s in range(4) for r in range(13)]
        rng.shuffle(self.cards)

    def deal(self, n: int) -> list:
        """从牌堆顶部发 n 张牌。"""
        dealt = self.cards[:n]
        self.cards = self.cards[n:]
        return dealt


def cards_to_str(cards: list) -> str:
    """将多个 Card 对象拼接为协议字符串。"""
    return "".join(c.to_str() for c in cards)


def str_to_card(s: str) -> Card:
    """解析 '<suit,rank>' 为 Card 对象。"""
    s = s.strip().strip("<>")
    suit, rank = s.split(",")
    return Card(int(suit), int(rank))


def str_to_cards(s: str) -> list:
    """解析 '<s,r><s,r>...' 为 Card 列表。"""
    import re
    parts = re.findall(r"<\d+,\d+>", s)
    return [str_to_card(p) for p in parts]
