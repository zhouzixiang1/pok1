"""Frozen, cross-version deterministic national-TCP deal generation.

The generator deliberately does not use :mod:`random`: CPython does not make
``random.shuffle`` output a portable protocol contract.  Every integer below
has an explicit byte order and width, and every bounded draw uses rejection
sampling before the Fisher--Yates swap.

National TCP card IDs in this module are *not* the local ``engine/judge.py``
card integers.  They use the protocol's suit-major order::

    card_id = tcp_suit * 13 + tcp_rank
    tcp_suit: 0=Spade, 1=Heart, 2=Diamond, 3=Club
    tcp_rank: 0=2, 1=3, ..., 12=Ace

Thus IDs 0..12 are Spades, 13..25 Hearts, 26..38 Diamonds, and 39..51
Clubs.  A generated tuple is the complete top-to-bottom deal order.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import NamedTuple, Sequence

from .constants import HANDS_PER_MATCH


UINT256_LIMIT = 1 << 256
TCP_SUIT_NAMES = ("Spade", "Heart", "Diamond", "Club")
TCP_RANK_NAMES = (
    "2",
    "3",
    "4",
    "5",
    "6",
    "7",
    "8",
    "9",
    "10",
    "Jack",
    "Queen",
    "King",
    "Ace",
)
TCP_CARD_COUNT = 52

DEAL_GENERATOR_ALGORITHM_ID = "pok-national-tcp-deal-v1"
DEAL_GENERATOR_ALGORITHM_SPEC = (
    "id=pok-national-tcp-deal-v1;"
    "root=uint256be32;"
    "hand-seed=HMAC-SHA256(root32,"
    "id\\x00hand-seed\\x00||uint16be(hand-number:1..70)||uint32be(retry));"
    "retry=reject-prior-seed-or-deck-digest;"
    "cards=id=suit*13+rank,suit=0S1H2D3C,rank=0(2)..12(A),initial=0..51;"
    "stream=SHA256(id\\x00deck-stream\\x00||uint256be(seed)||uint64be(counter));"
    "bounded=reject-x>=2^256-(2^256%bound),then-x%bound;"
    "shuffle=Fisher-Yates(i=51..1,j=bounded(i+1));"
    "deck-digest=SHA256(bytes(card-id[0..51]))"
)
DEAL_GENERATOR_ALGORITHM_DIGEST = hashlib.sha256(
    DEAL_GENERATOR_ALGORITHM_SPEC.encode("ascii")
).hexdigest()

# Short aliases are convenient for manifest code while the long names make it
# impossible to confuse this digest with a generated deck digest.
ALGORITHM_ID = DEAL_GENERATOR_ALGORITHM_ID
ALGORITHM_DIGEST = DEAL_GENERATOR_ALGORITHM_DIGEST

_HAND_SEED_DOMAIN = DEAL_GENERATOR_ALGORITHM_ID.encode("ascii") + b"\x00hand-seed\x00"
_DECK_STREAM_DOMAIN = DEAL_GENERATOR_ALGORITHM_ID.encode("ascii") + b"\x00deck-stream\x00"
_MAX_HAND_SEED_RETRIES = 1 << 32
_MAX_STREAM_BLOCKS = 1 << 64


class DealWindowCommitment(NamedTuple):
    """The 70 derived seeds and their complete top-to-bottom deck digests."""

    hand_seeds: tuple[int, ...]
    deck_digests: tuple[str, ...]

    @property
    def hand_deal_digests(self) -> tuple[str, ...]:
        """Alias matching ``evaluation.DealSequenceCommitment`` terminology."""

        return self.deck_digests


def _strict_int(
    value: int,
    name: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in [{minimum}, {maximum}]")
    return value


def _uint256(value: int, name: str) -> int:
    return _strict_int(value, name, minimum=0, maximum=UINT256_LIMIT - 1)


def tcp_card_id(suit: int, rank: int) -> int:
    """Map a national protocol ``<suit,rank>`` pair to its frozen card ID."""

    suit = _strict_int(suit, "TCP suit", minimum=0, maximum=3)
    rank = _strict_int(rank, "TCP rank", minimum=0, maximum=12)
    return suit * 13 + rank


def tcp_card_from_id(card_id: int) -> tuple[int, int]:
    """Return the national protocol ``(suit, rank)`` for a frozen card ID."""

    card_id = _strict_int(card_id, "TCP card ID", minimum=0, maximum=51)
    return divmod(card_id, 13)


def tcp_card_to_wire(card_id: int) -> str:
    """Serialize one frozen card ID using the national ``<suit,rank>`` form."""

    suit, rank = tcp_card_from_id(card_id)
    return f"<{suit},{rank}>"


def derive_hand_seed(
    root_seed: int,
    hand_number: int,
    collision_retry: int = 0,
) -> int:
    """Derive one 256-bit hand seed with a domain-separated HMAC-SHA256.

    ``hand_number`` is one-based, matching the national 70-hand match.  Normal
    derivation uses retry zero.  :func:`build_70_hand_commitment` increments the
    retry only if a seed or complete deck digest has already occurred earlier
    in the same window.
    """

    root_seed = _uint256(root_seed, "deck root seed")
    hand_number = _strict_int(
        hand_number,
        "hand number",
        minimum=1,
        maximum=HANDS_PER_MATCH,
    )
    collision_retry = _strict_int(
        collision_retry,
        "hand seed collision retry",
        minimum=0,
        maximum=_MAX_HAND_SEED_RETRIES - 1,
    )
    message = (
        _HAND_SEED_DOMAIN
        + hand_number.to_bytes(2, "big")
        + collision_retry.to_bytes(4, "big")
    )
    digest = hmac.new(
        root_seed.to_bytes(32, "big"),
        message,
        digestmod=hashlib.sha256,
    ).digest()
    return int.from_bytes(digest, "big")


def _uniform_below(seed_bytes: bytes, bound: int, counter: int) -> tuple[int, int]:
    """Return an unbiased value in ``range(bound)`` and the next counter."""

    if not 1 <= bound <= TCP_CARD_COUNT:
        raise ValueError("shuffle bound must be in [1, 52]")
    rejection_limit = UINT256_LIMIT - (UINT256_LIMIT % bound)
    while counter < _MAX_STREAM_BLOCKS:
        block = hashlib.sha256(
            _DECK_STREAM_DOMAIN + seed_bytes + counter.to_bytes(8, "big")
        ).digest()
        counter += 1
        candidate = int.from_bytes(block, "big")
        if candidate < rejection_limit:
            return candidate % bound, counter
    raise RuntimeError("SHA-256 shuffle counter space exhausted")


def generate_tcp_deck(hand_seed: int) -> tuple[int, ...]:
    """Generate a complete national-TCP deck in top-to-bottom deal order."""

    hand_seed = _uint256(hand_seed, "hand seed")
    seed_bytes = hand_seed.to_bytes(32, "big")
    deck = list(range(TCP_CARD_COUNT))
    counter = 0
    for index in range(TCP_CARD_COUNT - 1, 0, -1):
        swap_index, counter = _uniform_below(seed_bytes, index + 1, counter)
        deck[index], deck[swap_index] = deck[swap_index], deck[index]
    return tuple(deck)


def canonical_deck_digest(deck: Sequence[int]) -> str:
    """Hash one full deck as exactly 52 card-ID bytes, top card first."""

    normalized = tuple(deck)
    if len(normalized) != TCP_CARD_COUNT:
        raise ValueError("a canonical full deck must contain exactly 52 cards")
    for card_id in normalized:
        _strict_int(card_id, "TCP card ID", minimum=0, maximum=51)
    if len(set(normalized)) != TCP_CARD_COUNT:
        raise ValueError("a canonical full deck must contain every card exactly once")
    return hashlib.sha256(bytes(normalized)).hexdigest()


def build_70_hand_commitment(root_seed: int) -> DealWindowCommitment:
    """Derive and commit the unique full deals for national hands 1 through 70."""

    root_seed = _uint256(root_seed, "deck root seed")
    hand_seeds: list[int] = []
    deck_digests: list[str] = []
    used_seeds: set[int] = set()
    used_deck_digests: set[str] = set()

    for hand_number in range(1, HANDS_PER_MATCH + 1):
        for collision_retry in range(_MAX_HAND_SEED_RETRIES):
            hand_seed = derive_hand_seed(root_seed, hand_number, collision_retry)
            if hand_seed in used_seeds:
                continue
            deck_digest = canonical_deck_digest(generate_tcp_deck(hand_seed))
            if deck_digest in used_deck_digests:
                continue
            used_seeds.add(hand_seed)
            used_deck_digests.add(deck_digest)
            hand_seeds.append(hand_seed)
            deck_digests.append(deck_digest)
            break
        else:
            raise RuntimeError(
                f"could not derive a unique deal for hand {hand_number}"
            )

    if len(hand_seeds) != HANDS_PER_MATCH or len(deck_digests) != HANDS_PER_MATCH:
        raise RuntimeError("incomplete 70-hand deal commitment")
    return DealWindowCommitment(tuple(hand_seeds), tuple(deck_digests))
