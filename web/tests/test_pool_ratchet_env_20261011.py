"""Pool-ratchet env semantics regressions (2026-10-11).

must-fix 2 anchors: unset POK_PUBLISH_MODE falls back to ``legacy`` in code
(rollback = delete the env keys), an invalid mode value fails fast, and the
ratchet target size N is validated against [12, 30] — the lower bound is the
crossover diversity floor (_MIN_CROSSOVER_POOL_SIZE = 12), so a pool
configuration that would silently disable crossover is rejected outright.
"""

import pytest

import pool_ratchet


def test_unset_mode_falls_back_to_legacy(monkeypatch):
    monkeypatch.delenv("POK_PUBLISH_MODE", raising=False)
    assert pool_ratchet.publish_mode() == "legacy"
    # Empty/whitespace counts as unset (code fallback, not an error).
    monkeypatch.setenv("POK_PUBLISH_MODE", "   ")
    assert pool_ratchet.publish_mode() == "legacy"
    monkeypatch.setenv("POK_PUBLISH_MODE", "")
    assert pool_ratchet.publish_mode() == "legacy"


def test_invalid_mode_fails_fast(monkeypatch):
    monkeypatch.setenv("POK_PUBLISH_MODE", "ratchet")
    with pytest.raises(pool_ratchet.PoolRatchetConfigError):
        pool_ratchet.publish_mode()


def test_legacy_target_is_the_absolute_backstop(monkeypatch):
    monkeypatch.delenv("POK_PUBLISH_MODE", raising=False)
    monkeypatch.delenv("POK_POOL_RATCHET_TARGET_SIZE", raising=False)
    assert pool_ratchet.pool_target_size() == 30
    # A stray target-size key has no effect in legacy mode.
    monkeypatch.setenv("POK_POOL_RATCHET_TARGET_SIZE", "12")
    assert pool_ratchet.pool_target_size() == 30


def test_ratchet_target_defaults_to_12_when_unset(monkeypatch):
    monkeypatch.setenv("POK_PUBLISH_MODE", "pool_ratchet")
    monkeypatch.delenv("POK_POOL_RATCHET_TARGET_SIZE", raising=False)
    assert pool_ratchet.pool_target_size() == 12


@pytest.mark.parametrize("bad", ["11", "31", "0", "-5", "abc", "12.5", ""])
def test_ratchet_target_out_of_range_is_rejected(monkeypatch, bad):
    monkeypatch.setenv("POK_PUBLISH_MODE", "pool_ratchet")
    monkeypatch.setenv("POK_POOL_RATCHET_TARGET_SIZE", bad)
    if bad == "":
        # Empty behaves as unset -> default 12, not an error.
        assert pool_ratchet.pool_target_size() == 12
        return
    with pytest.raises(pool_ratchet.PoolRatchetConfigError):
        pool_ratchet.pool_target_size()


@pytest.mark.parametrize("good", ["12", "13", "20", "30"])
def test_ratchet_target_in_range_is_accepted(monkeypatch, good):
    monkeypatch.setenv("POK_PUBLISH_MODE", "pool_ratchet")
    monkeypatch.setenv("POK_POOL_RATCHET_TARGET_SIZE", good)
    assert pool_ratchet.pool_target_size() == int(good)


def test_mode_resolved_policy_and_profile_labels(monkeypatch):
    monkeypatch.delenv("POK_PUBLISH_MODE", raising=False)
    assert pool_ratchet.reap_selection_policy_for_mode() == (
        "conservative_glicko_v1"
    )
    assert pool_ratchet.reap_protection_profile_for_mode() == "legacy"
    monkeypatch.setenv("POK_PUBLISH_MODE", "pool_ratchet")
    assert pool_ratchet.reap_selection_policy_for_mode() == (
        "conservative_glicko_pool_ratchet_v1"
    )
    assert pool_ratchet.reap_protection_profile_for_mode() == "pool_ratchet"


def test_lower_bound_equals_crossover_diversity_floor():
    """N < 12 would silently disable crossover preparation; rejected."""
    from generation_scheduler_source_selection import _MIN_CROSSOVER_POOL_SIZE

    assert pool_ratchet.POOL_TARGET_MIN_SIZE == _MIN_CROSSOVER_POOL_SIZE == 12
