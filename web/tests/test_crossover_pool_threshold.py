"""Regression guard for the crossover pool-size dead-loop.

Crossover recombines two distinct evolved lineages.  With fewer than
``_MIN_CROSSOVER_POOL_SIZE`` active bots, the pool is dominated by a single
evolved line plus the first-strict bootstrap, so the only available parent B
is structurally incapable of contributing new capabilities — every crossover
child regresses a parent-A capability, the architecture-policy gate correctly
rejects it, and the generation is abandoned after exhausting retries (a
deterministic dead-loop that wasted ~30 generations: v30-v75).

``_pick_crossover_parents`` now returns ``None`` when the active pool is below
the threshold, disabling crossover until more bots are certified into the
pool.  The system falls back to single-parent Master evolution.
"""

from types import MappingProxyType

from conftest import STRICT_TARGET_V
from generation_scheduler import SelectionView
import generation_scheduler_source_selection as source_selection
from bot_namespace import bot_name


def _selection_view(active_bot_versions):
    """Build a real SelectionView for parent-selection tests."""
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


def test_crossover_disabled_when_pool_below_threshold():
    """With fewer than _MIN_CROSSOVER_POOL_SIZE active bots, crossover returns
    None (no valid parent B exists) so the system falls back to single-parent."""
    # Pool of 2 (the dead-loop case: strongest + bootstrap)
    sv = _selection_view([1, STRICT_TARGET_V])
    result = source_selection._pick_crossover_parents({}, STRICT_TARGET_V, selection_view=sv)
    assert result is None, (
        "crossover must be disabled when the active pool has fewer than "
        f"{source_selection._MIN_CROSSOVER_POOL_SIZE} bots (bootstrap dead-loop)"
    )


def test_crossover_disabled_with_single_bot_pool():
    """A single-bot pool cannot crossover."""
    sv = _selection_view([STRICT_TARGET_V])
    result = source_selection._pick_crossover_parents({}, STRICT_TARGET_V, selection_view=sv)
    assert result is None


def test_crossover_enabled_when_pool_meets_threshold():
    """When the pool has >= _MIN_CROSSOVER_POOL_SIZE bots, crossover selection
    is not suppressed by the pool-size guard (it may still return None for
    other reasons, but not the pool-size guard)."""
    # Enough distinct versions to clear the raised threshold.
    versions = list(range(STRICT_TARGET_V, STRICT_TARGET_V + source_selection._MIN_CROSSOVER_POOL_SIZE + 5))
    sv = _selection_view(versions)
    v_a = versions[-1]
    result = source_selection._pick_crossover_parents({}, v_a, selection_view=sv)
    assert result is not None, (
        "crossover should not be suppressed by the pool-size guard when the "
        f"pool has >= {source_selection._MIN_CROSSOVER_POOL_SIZE} bots"
    )
    assert isinstance(result, tuple) and len(result) == 2


def test_min_crossover_pool_size_value():
    """The threshold constant is set high enough to keep crossover disabled
    until the rating pool is diverse enough for reliable crossover (raised
    from 3→12 after crossover infrastructure failures dead-looped v128-v152)."""
    assert source_selection._MIN_CROSSOVER_POOL_SIZE == 12
