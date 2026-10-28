"""Persistent dead-crossover-pair hard avoidance (2026-10-28 audit).

75 crossover_llm_exhausted abandons, 71% concentrated in 3 parent pairs
(v11xv173 burned 21 times): the strength-ranked picker re-selected the same
pair every generation because the LAST ABANDON block is advisory text for
the LLM and the drought rotation only excludes the single most recent
pairing. ``_pick_crossover_parents`` now counts pair exhaustion from the
durable abandon ledger and HARD-excludes partners that reached
``_DEAD_PAIR_EXHAUSTION_THRESHOLD``; if nothing distinct is left, it returns
None so the scheduler falls back to single-parent Master evolution.
"""

from types import MappingProxyType

import pytest

from conftest import STRICT_TARGET_V
from generation_scheduler import SelectionView
import generation_scheduler_source_selection as source_selection
from bot_namespace import bot_name


def _selection_view(active_bot_versions):
    active = tuple(bot_name(v) for v in active_bot_versions)
    return SelectionView(
        active_bots=active,
        active_versions=frozenset(active_bot_versions),
        rows=tuple(),
        metrics=MappingProxyType({}),
        selection_scores=MappingProxyType(
            {n: float(v) for v, n in zip(active_bot_versions, active)}
        ),
        order_keys=MappingProxyType(
            {n: (v,) for v, n in zip(active_bot_versions, active)}
        ),
        rating_values=MappingProxyType({}),
        h2h=MappingProxyType({}),
        source_history=tuple(),
        digest="test",
    )


def _pool(strongest_gap=None):
    """Pool whose strongest is STRICT_TARGET_V + 20, with deterministic gaps."""
    base = STRICT_TARGET_V + 20
    return list(range(base - (source_selection._MIN_CROSSOVER_POOL_SIZE + 6), base + 1))


def _ledger_rows(counts):
    """counts: {frozenset({a,b}): n} -> abandon receipt rows."""
    rows = []
    version = 900
    for (a, b), n in counts.items():
        for _ in range(n):
            version += 1
            rows.append({
                "version": version,
                "reason": "crossover_llm_exhausted: llm retries exhausted",
                "checkpoint_envelope": {"source_v": a, "parent2_v": b},
            })
    return rows


@pytest.fixture
def fake_ledger(monkeypatch):
    import abandoned_version_ledger

    state = {"rows": []}
    monkeypatch.setattr(
        abandoned_version_ledger,
        "load_abandoned_version_receipts",
        lambda *a, **k: list(state["rows"]),
    )
    return state


def test_dead_partner_is_hard_excluded(fake_ledger):
    pool = _pool()
    strongest = pool[-1]
    sv = _selection_view(pool)
    # sanity: without the ledger the natural second pick is the strongest
    # >=3-gap candidate
    baseline = source_selection._pick_crossover_parents(
        {}, strongest, selection_view=sv
    )
    assert baseline is not None
    dead_b = baseline[1]
    fake_ledger["rows"] = _ledger_rows(
        {frozenset({strongest, dead_b}): source_selection._DEAD_PAIR_EXHAUSTION_THRESHOLD}
    )
    after = source_selection._pick_crossover_parents(
        {}, strongest, selection_view=sv
    )
    assert after is not None
    assert after[1] != dead_b, "the exhausted pairing must be excluded"
    assert after[0] == strongest


def test_direction_normalization_of_pair_identity(fake_ledger):
    """Ledger rows recorded as (b, a) must kill the pair for parent a."""
    pool = _pool()
    strongest = pool[-1]
    sv = _selection_view(pool)
    baseline = source_selection._pick_crossover_parents(
        {}, strongest, selection_view=sv
    )
    dead_b = baseline[1]
    fake_ledger["rows"] = [
        {
            "version": 950 + i,
            "reason": "crossover_llm_exhausted",
            # reversed direction
            "checkpoint_envelope": {"source_v": dead_b, "parent2_v": strongest},
        }
        for i in range(source_selection._DEAD_PAIR_EXHAUSTION_THRESHOLD)
    ]
    after = source_selection._pick_crossover_parents(
        {}, strongest, selection_view=sv
    )
    assert after is not None and after[1] != dead_b


def test_threshold_not_yet_reached_keeps_the_pair(fake_ledger):
    pool = _pool()
    strongest = pool[-1]
    sv = _selection_view(pool)
    baseline = source_selection._pick_crossover_parents(
        {}, strongest, selection_view=sv
    )
    dead_b = baseline[1]
    fake_ledger["rows"] = _ledger_rows(
        {
            frozenset({strongest, dead_b}): source_selection._DEAD_PAIR_EXHAUSTION_THRESHOLD - 1
        }
    )
    after = source_selection._pick_crossover_parents(
        {}, strongest, selection_view=sv
    )
    assert after == baseline, "below threshold the selection must not shift"


def test_all_partners_dead_falls_back_to_single_parent(fake_ledger):
    pool = _pool()
    strongest = pool[-1]
    sv = _selection_view(pool)
    # every candidate partner, gap and adjacent alike, has exhausted crossover
    others = [v for v in pool if v != strongest]
    counts = {frozenset({strongest, v}): source_selection._DEAD_PAIR_EXHAUSTION_THRESHOLD for v in others}
    fake_ledger["rows"] = _ledger_rows(counts)
    result = source_selection._pick_crossover_parents(
        {}, strongest, selection_view=sv
    )
    assert result is None, "every candidate pair exhausted => no crossover, not a dead-pair re-burn"


def test_unreadable_ledger_fails_open_to_previous_behaviour(fake_ledger, monkeypatch):
    """Ledger corruption must not disable crossover for a fresh pool."""
    pool = _pool()
    strongest = pool[-1]
    sv = _selection_view(pool)
    expected = source_selection._pick_crossover_parents(
        {}, strongest, selection_view=sv
    )
    import abandoned_version_ledger

    def boom(*_a, **_k):
        raise RuntimeError("ledger unreadable")

    monkeypatch.setattr(abandoned_version_ledger, "load_abandoned_version_receipts", boom)
    got = source_selection._pick_crossover_parents({}, strongest, selection_view=sv)
    assert got == expected is not None


def test_non_crossover_reasons_do_not_count(fake_ledger):
    pool = _pool()
    strongest = pool[-1]
    sv = _selection_view(pool)
    baseline = source_selection._pick_crossover_parents(
        {}, strongest, selection_view=sv
    )
    dead_b = baseline[1]
    fake_ledger["rows"] = [
        {
            "version": 970 + i,
            "reason": "precommit_rework_circuit_breaker:something",
            "checkpoint_envelope": {"source_v": strongest, "parent2_v": dead_b},
        }
        for i in range(10)
    ]
    after = source_selection._pick_crossover_parents(
        {}, strongest, selection_view=sv
    )
    assert after == baseline, "non-crossover abandons are not pair exhaustion"
