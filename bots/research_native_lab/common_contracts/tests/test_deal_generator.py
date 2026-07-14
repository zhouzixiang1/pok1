from __future__ import annotations

import pytest

from bots.research_native_lab.common_contracts.deal_generator import (
    DEAL_GENERATOR_ALGORITHM_DIGEST,
    DEAL_GENERATOR_ALGORITHM_ID,
    TCP_RANK_NAMES,
    TCP_SUIT_NAMES,
    UINT256_LIMIT,
    build_70_hand_commitment,
    canonical_deck_digest,
    derive_hand_seed,
    generate_tcp_deck,
    tcp_card_from_id,
    tcp_card_id,
    tcp_card_to_wire,
)


GOLDEN_ROOT = int.from_bytes(bytes(range(32)), "big")


def test_tcp_suit_rank_and_card_id_mapping_is_explicit() -> None:
    assert TCP_SUIT_NAMES == ("Spade", "Heart", "Diamond", "Club")
    assert TCP_RANK_NAMES == (
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
    assert tcp_card_id(0, 0) == 0
    assert tcp_card_id(0, 12) == 12
    assert tcp_card_id(1, 0) == 13
    assert tcp_card_id(2, 0) == 26
    assert tcp_card_id(3, 12) == 51
    assert tcp_card_from_id(0) == (0, 0)
    assert tcp_card_from_id(51) == (3, 12)
    assert tcp_card_to_wire(0) == "<0,0>"
    assert tcp_card_to_wire(51) == "<3,12>"


def test_fisher_yates_deck_is_deterministic_and_complete() -> None:
    hand_seed = derive_hand_seed(GOLDEN_ROOT, 1)
    first = generate_tcp_deck(hand_seed)
    second = generate_tcp_deck(hand_seed)

    assert first == second
    assert len(first) == 52
    assert len(set(first)) == 52
    assert set(first) == set(range(52))
    assert canonical_deck_digest(first) == canonical_deck_digest(second)


def test_root_changes_the_entire_commitment() -> None:
    first = build_70_hand_commitment(GOLDEN_ROOT)
    second = build_70_hand_commitment(GOLDEN_ROOT ^ 1)

    assert first != second
    assert first.hand_seeds != second.hand_seeds
    assert first.deck_digests != second.deck_digests


def test_cross_version_golden_vector() -> None:
    commitment = build_70_hand_commitment(GOLDEN_ROOT)
    first_deck = generate_tcp_deck(commitment.hand_seeds[0])

    assert DEAL_GENERATOR_ALGORITHM_ID == "pok-national-tcp-deal-v1"
    assert DEAL_GENERATOR_ALGORITHM_DIGEST == (
        "271ac29be0166cbc1724df2869b7f5252affdcd49049e948ec4e0f2a571207f1"
    )
    assert f"{commitment.hand_seeds[0]:064x}" == (
        "3ba6669b5b333db6643d2c553d63fb5256ba3afa7e5e39d06a268bf2a4746bb3"
    )
    assert f"{commitment.hand_seeds[1]:064x}" == (
        "588504ff5871f2f0d2cc4ef497bd16b32ea4334aedba60989dcc63e488cf74e6"
    )
    assert f"{commitment.hand_seeds[-1]:064x}" == (
        "395e85958247cd00a0dea76a58c91fc794d8c842165bc4103ff3c50f2f870a34"
    )
    assert first_deck == (
        30,
        15,
        29,
        9,
        28,
        12,
        37,
        25,
        45,
        21,
        44,
        2,
        35,
        6,
        5,
        42,
        23,
        14,
        50,
        48,
        43,
        7,
        49,
        34,
        27,
        31,
        38,
        47,
        11,
        33,
        1,
        46,
        41,
        20,
        26,
        4,
        0,
        17,
        13,
        39,
        18,
        36,
        22,
        10,
        32,
        19,
        8,
        24,
        3,
        40,
        51,
        16,
    )
    assert commitment.deck_digests[0] == (
        "fd8cdf5d1f4cab0305ce8f521b95483d8794bd2e30aeb4e016747c6e88825831"
    )
    assert commitment.deck_digests[1] == (
        "6bb1113cc94644f4c101448325352d63c7931ea3c34085903280b2f7e65df8f8"
    )
    assert commitment.deck_digests[-1] == (
        "f1dc201e51072bebce514128697ab3757ee9ff703480c2b453c671662c2e3cf5"
    )


def test_commitment_has_no_repeats_in_the_70_hand_window() -> None:
    commitment = build_70_hand_commitment(GOLDEN_ROOT)

    assert len(commitment.hand_seeds) == 70
    assert len(set(commitment.hand_seeds)) == 70
    assert len(commitment.deck_digests) == 70
    assert len(set(commitment.deck_digests)) == 70
    assert commitment.hand_deal_digests == commitment.deck_digests
    assert commitment.deck_digests == tuple(
        canonical_deck_digest(generate_tcp_deck(seed))
        for seed in commitment.hand_seeds
    )


@pytest.mark.parametrize("invalid", (False, True, -1, UINT256_LIMIT))
def test_root_and_hand_seed_reject_bool_and_out_of_range(invalid) -> None:
    with pytest.raises(ValueError):
        build_70_hand_commitment(invalid)
    with pytest.raises(ValueError):
        generate_tcp_deck(invalid)


@pytest.mark.parametrize("invalid", (False, True, 0, 71))
def test_hand_number_rejects_bool_and_out_of_range(invalid) -> None:
    with pytest.raises(ValueError):
        derive_hand_seed(GOLDEN_ROOT, invalid)


@pytest.mark.parametrize(
    ("suit", "rank"),
    ((False, 0), (True, 0), (-1, 0), (4, 0), (0, False), (0, True), (0, -1), (0, 13)),
)
def test_card_mapping_rejects_bool_and_out_of_range(suit, rank) -> None:
    with pytest.raises(ValueError):
        tcp_card_id(suit, rank)


@pytest.mark.parametrize("invalid", (False, True, -1, 52))
def test_card_id_rejects_bool_and_out_of_range(invalid) -> None:
    with pytest.raises(ValueError):
        tcp_card_from_id(invalid)


def test_canonical_digest_rejects_non_permutations_and_bool_cards() -> None:
    with pytest.raises(ValueError, match="exactly 52"):
        canonical_deck_digest(tuple(range(51)))
    with pytest.raises(ValueError, match="every card exactly once"):
        canonical_deck_digest((0,) + tuple(range(51)))
    with pytest.raises(ValueError, match="TCP card ID"):
        canonical_deck_digest((False,) + tuple(range(1, 52)))
