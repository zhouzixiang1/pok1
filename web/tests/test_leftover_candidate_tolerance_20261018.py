"""Self-healing leftover candidate (2026-10-18, v567 wedge): a checkpoint
loss orphans the in-flight candidate's untracked staging dir under bots/,
and the next fresh prepare (candidate_v=None) was blocked FOREVER by the
runtime git guard classifying it as a foreign active bot.  An untracked
generated bot dir whose version is above the published high-water was
never published (published bots are committed) and is exactly the version
the next prepare re-allocates — tolerate it with an audit event instead
of wedging."""

import pytest


def _snapshot(entries):
    return {"entries": list(entries), "branch": "tencent-cloud-runtime", "head": "a" * 40}


def test_scope_tolerates_above_high_water_leftover(monkeypatch):
    import tool_runtime_guard as guard
    import evolution_infra

    monkeypatch.setattr(evolution_infra, "find_current_v", lambda: 566)
    scope = guard._scope(
        _snapshot(["?? bots/national_cloud_v567/"]), candidate_v=None
    )
    assert scope["blocking_entries"] == []
    assert scope["leftover_candidate_allowance"] == [567]


def test_scope_still_blocks_below_or_equal_high_water(monkeypatch):
    import tool_runtime_guard as guard
    import evolution_infra

    monkeypatch.setattr(evolution_infra, "find_current_v", lambda: 566)
    # A committed bot's directory content showing as modified (tracked) or a
    # version at/below the high-water is NOT a leftover candidate: blocked.
    scope = guard._scope(
        _snapshot(["?? bots/national_cloud_v566/"]), candidate_v=None
    )
    assert "?? bots/national_cloud_v566/" in scope["blocking_entries"]
    assert scope["leftover_candidate_allowance"] == []


def test_scope_candidate_v_still_owns_only_its_own_dir(monkeypatch):
    import tool_runtime_guard as guard
    import evolution_infra

    monkeypatch.setattr(evolution_infra, "find_current_v", lambda: 566)
    # With a live candidate_v=568, the leftover 567 dir stays FOREIGN: only
    # the no-checkpoint fresh-prepare path gains the self-healing allowance.
    scope = guard._scope(
        _snapshot(["?? bots/national_cloud_v567/"]), candidate_v=568
    )
    assert "?? bots/national_cloud_v567/" in scope["blocking_entries"]
