from __future__ import annotations

from pathlib import Path
import sys

import pytest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
WEB_CORE = Path(__file__).resolve().parents[3] / "web" / "core"
for path in (TOOLS, WEB_CORE):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import national_native  # noqa: E402
import native_tcp_conditional_runout_probe as probe  # noqa: E402


def _cards(deck) -> list[tuple[int, int]]:
    return [(card.suit, card.rank) for card in deck.cards]


def test_conditional_deck_keeps_prefix_and_resamples_suffix() -> None:
    target_seed = 101
    original = national_native.Deck(seed=target_seed)
    factory_a = probe._conditional_deck_factory(
        national_native.Deck,
        target_seed=target_seed,
        known_cards=4,
        runout_seed=9001,
    )
    factory_b = probe._conditional_deck_factory(
        national_native.Deck,
        target_seed=target_seed,
        known_cards=4,
        runout_seed=9002,
    )
    first = factory_a(seed=target_seed)
    second = factory_b(seed=target_seed)
    assert _cards(first)[:4] == _cards(original)[:4]
    assert _cards(second)[:4] == _cards(original)[:4]
    assert set(_cards(first)[4:]) == set(_cards(original)[4:])
    assert set(_cards(second)[4:]) == set(_cards(original)[4:])
    assert _cards(first)[4:] != _cards(second)[4:]


def test_conditional_deck_leaves_other_hands_unchanged() -> None:
    factory = probe._conditional_deck_factory(
        national_native.Deck,
        target_seed=101,
        known_cards=7,
        runout_seed=9001,
    )
    assert _cards(factory(seed=102)) == _cards(national_native.Deck(seed=102))


@pytest.mark.parametrize(
    ("public_cards", "expected"),
    [([], 4), ([1, 2, 3], 7), ([1, 2, 3, 4], 8), ([1, 2, 3, 4, 5], 9)],
)
def test_known_card_count(public_cards: list[int], expected: int) -> None:
    assert probe._known_card_count({
        "request": {"public_cards": public_cards}
    }) == expected


def test_forced_action_requires_one_confirmed_probe() -> None:
    row = {"probes": [
        {
            "forced_label": "call",
            "forced_action": 0,
            "status": "ok",
            "force_confirmed": True,
        },
        {
            "forced_label": "allin",
            "forced_action": -2,
            "status": "forced_issues",
            "force_confirmed": True,
        },
    ]}
    assert probe._forced_action(row, "call") == 0
    with pytest.raises(ValueError):
        probe._forced_action(row, "allin")


def test_bootstrap_ci_is_deterministic() -> None:
    first = probe._bootstrap_ci([-10.0, 0.0, 20.0], samples=200, seed=7)
    second = probe._bootstrap_ci([-10.0, 0.0, 20.0], samples=200, seed=7)
    assert first == second
    assert first["mean"] == pytest.approx(10.0 / 3.0)


def test_context_match_requires_exact_pre_force_state_and_rule_action() -> None:
    source = {"request": {"hand": 5}, "state": {"pot": 300}}
    decision = {
        "request": {"hand": 5},
        "state": {"pot": 300},
        "sanitized_action": -1,
    }
    assert probe._context_matches_source(decision, source, rule_action=-1)
    decision["state"] = {"pot": 301}
    assert not probe._context_matches_source(decision, source, rule_action=-1)
