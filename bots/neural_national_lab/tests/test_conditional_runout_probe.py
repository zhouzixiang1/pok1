from __future__ import annotations

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace

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


def test_long_horizon_delta_separates_target_hand_from_future_tail() -> None:
    match_delta, tail_delta = probe._long_horizon_deltas(
        hand_delta=-100.0,
        baseline_match_value=1000.0,
        forced_match_value=1300.0,
    )

    assert match_delta == 300.0
    assert tail_delta == 400.0


def test_long_horizon_delta_requires_complete_branch_values() -> None:
    assert probe._long_horizon_deltas(
        hand_delta=10.0,
        baseline_match_value=None,
        forced_match_value=20.0,
    ) == (None, None)


def test_metric_summary_uses_independent_deterministic_bootstrap() -> None:
    first = probe._metric_summary(
        [-100.0, 50.0, 200.0], bootstrap_samples=200, bootstrap_seed=11
    )
    second = probe._metric_summary(
        [-100.0, 50.0, 200.0], bootstrap_samples=200, bootstrap_seed=11
    )

    assert first == second
    assert first["valid_replicates"] == 3
    assert first["mean"] == 50.0
    assert first["positive_rate"] == pytest.approx(2.0 / 3.0)


def test_collect_through_match_reports_hand_tail_and_match_deltas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = {
        "hand": 2,
        "hand_decision_index": 0,
        "deck_seed_base": 100,
        "bot_seed_base": 200,
        "rule_final": -1,
        "rule_value": -100,
        "baseline_match_net_chips": 50,
        "request": {"max_hand": 3, "public_cards": []},
        "state": {"pot": 150},
        "opponent": "national_v1",
        "probes": [{
            "forced_label": "call",
            "forced_action": 0,
            "status": "ok",
            "force_confirmed": True,
        }],
    }
    source_path = tmp_path / "source.jsonl"
    source_path.write_text("{}\n", encoding="utf-8")
    calls = []

    async def run_with_deck(_candidate, _opponent, **kwargs):
        calls.append(kwargs)
        return {
            "passed_compliance": True,
            "bot_a": "candidate",
            "issues": [],
            "settlements": [
                {"hand": 1, "earnings": [100, -100]},
                {"hand": 2, "earnings": [200, -200]},
                {"hand": 3, "earnings": [-50, 50]},
            ],
        }

    monkeypatch.setattr(probe, "_read_row", lambda _path, _index: source)
    monkeypatch.setattr(probe, "_resolve", lambda value: Path(value))
    monkeypatch.setattr(probe, "_run_with_deck", run_with_deck)
    monkeypatch.setattr(
        probe,
        "_decision_trace",
        lambda *_args, **_kwargs: {
            "request": source["request"],
            "state": source["state"],
            "sanitized_action": -1,
        },
    )
    monkeypatch.setattr(probe, "_force_confirmed", lambda *_args, **_kwargs: True)
    args = SimpleNamespace(
        source=source_path,
        row_index=0,
        candidate="candidate",
        opponent="opponent",
        alternative_label="call",
        replicates=1,
        runout_seed_base=300,
        catastrophe_threshold=5000.0,
        bootstrap_samples=20,
        bootstrap_seed=7,
        timeout_sec=1.0,
        reuse_terminal_fold=True,
        through_match=True,
    )

    payload = asyncio.run(probe._collect(args))

    assert len(calls) == 1
    assert calls[0]["hands"] == 3
    row = payload["rows"][0]
    assert row["baseline_reused_terminal_fold"] is True
    assert row["delta_vs_rule"] == 300.0
    assert row["match_delta_vs_rule"] == 200.0
    assert row["tail_delta_vs_rule"] == -100.0
    assert payload["summary"]["metrics"]["match_delta_vs_rule"][
        "valid_replicates"
    ] == 1


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
