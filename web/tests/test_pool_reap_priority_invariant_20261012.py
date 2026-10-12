"""Pool-reap priority invariant regressions (2026-10-12).

``tool_commit_archivist._validate_pool_reap_plan`` must prove the frozen
selection snapshot's ``priority_bot`` equals the bot THIS handoff published
(``record["identity"]["version"]``) before any reap effect executes.  The
handoff step order (``priority_eval`` BEFORE ``pool_reap``) exists to make
that true; a stale or missing priority proves the priority_eval step failed
or was skipped and must fail closed with the same shape as every other plan
validation mismatch (RuntimeError -> the handoff stays incomplete / paused),
never silently continue under last cycle's protection.
"""

import pytest

from bot_namespace import bot_name

PUBLISHED_V = 13


def _record(version=PUBLISHED_V):
    identity = {
        "publication_id": "a" * 64,
        "commit_oid": "b" * 40,
        "remote_publication": {"remote_main_oid": "c" * 40},
    }
    if version is not None:
        identity["version"] = version
    return {"identity": identity}


def _plan(monkeypatch, tmp_path, *, priority_version):
    import test_pool_reap_target_size_20261011 as helper
    import tool_commit

    monkeypatch.setenv("POK_PUBLISH_MODE", "pool_ratchet")
    monkeypatch.setenv("POK_POOL_RATCHET_TARGET_SIZE", "12")
    versions = list(range(1, 14))  # 13 bots, cap 12 -> exactly 1 reap
    ratings = {v: (1200.0 + (v - 1) * 50.0, 100.0) for v in versions}
    helper._setup_pool(
        monkeypatch,
        tmp_path,
        versions,
        rating_by_version=ratings,
        priority_version=priority_version,
    )
    return tool_commit._build_pool_reap_plan(_record())


def test_matching_priority_passes_validation(monkeypatch, tmp_path):
    """Invariant holds: snapshot priority == the handoff's published bot."""
    import tool_commit

    plan = _plan(monkeypatch, tmp_path, priority_version=PUBLISHED_V)
    assert plan["selection_snapshot"]["priority_bot"] == bot_name(PUBLISHED_V)

    initial, targets, snapshot = tool_commit._validate_pool_reap_plan(
        plan, _record()
    )
    # The plan's active pool is lexicographically sorted, as everywhere else.
    assert initial == sorted(bot_name(v) for v in range(1, 14))
    assert targets == [bot_name(1)]
    assert snapshot is plan["selection_snapshot"]


def test_stale_priority_fails_closed(monkeypatch, tmp_path):
    """priority_eval skipped/failed -> stale older priority -> pause."""
    import tool_commit

    # The pool snapshot froze priority at v12 while THIS handoff published
    # v13: exactly the shape a skipped/failed priority_eval step leaves.
    plan = _plan(monkeypatch, tmp_path, priority_version=PUBLISHED_V - 1)
    assert plan["selection_snapshot"]["priority_bot"] == bot_name(
        PUBLISHED_V - 1
    )

    with pytest.raises(
        RuntimeError, match="pool_reap_priority_bot_not_published_bot"
    ):
        tool_commit._validate_pool_reap_plan(plan, _record())


def test_missing_priority_fails_closed(monkeypatch, tmp_path):
    """No priority at all (None) cannot prove the invariant -> pause."""
    import tool_commit

    plan = _plan(monkeypatch, tmp_path, priority_version=None)
    assert plan["selection_snapshot"]["priority_bot"] is None

    with pytest.raises(
        RuntimeError, match="pool_reap_priority_bot_not_published_bot"
    ):
        tool_commit._validate_pool_reap_plan(plan, _record())


def test_missing_published_identity_fails_closed(monkeypatch, tmp_path):
    """A record that cannot prove its published version fails closed."""
    import tool_commit

    plan = _plan(monkeypatch, tmp_path, priority_version=PUBLISHED_V)
    with pytest.raises(
        RuntimeError, match="pool_reap_priority_bot_not_published_bot"
    ):
        tool_commit._validate_pool_reap_plan(plan, _record(version=None))


@pytest.mark.asyncio
async def test_execute_path_pauses_before_any_reap_effect(monkeypatch, tmp_path):
    """The executor validates first: a stale-priority plan reaps nothing."""
    import test_pool_reap_target_size_20261011 as helper
    import tool_bot_management
    import tool_commit

    plan = _plan(monkeypatch, tmp_path, priority_version=PUBLISHED_V - 1)

    async def forbidden_reap(**_kwargs):
        raise AssertionError("stale-priority plan reached the reap effect")

    def forbidden_prove(*_args):
        raise AssertionError("stale-priority plan reached tombstone convergence")

    monkeypatch.setattr(tool_bot_management, "_do_reap_weakest", forbidden_reap)
    monkeypatch.setattr(
        tool_commit,
        "_converge_and_verify_reaped_target",
        forbidden_prove,
    )
    # _setup_pool already ran inside _plan(); nothing else may mutate bots/.
    with pytest.raises(
        RuntimeError, match="pool_reap_priority_bot_not_published_bot"
    ):
        await tool_commit._execute_pool_reap_plan(plan, _record())
