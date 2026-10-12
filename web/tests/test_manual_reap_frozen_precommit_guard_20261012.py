"""Manual-reap in-flight precommit-plan guard regressions (2026-10-12).

The operator-facing ``reap_weakest`` MCP entry must refuse (typed error,
reason contains ``存在冻结 precommit 计划``) whenever any active checkpoint
slot carries a frozen ``audit_context.precommit_eval_plan``: reaping a
frozen gate opponent between plan freeze and gate execution makes the
plan's retry misjudge the match set.  The handoff step-6 automatic reap
(``_do_reap_weakest(quiet=True, expected_culled=..., selection_snapshot=...)``)
never passes through that entry and must stay unguarded; with no frozen
plan the manual entry proceeds unchanged.
"""

import json

import pytest

from bot_namespace import bot_name


def _frozen_plan_slots():
    return {
        "primary": {
            "next_v": 14,
            "source_v": 13,
            "stage": "precommit_running",
            "workflow_run_id": "workflow-v14#1",
            "checkpoint_revision": 3,
            "audit_context": {
                "precommit_eval_plan": {
                    "schema_version": 1,
                    "opponents": [
                        {"bot": bot_name(13), "games": 12},
                        {"bot": bot_name(1), "games": 12},
                    ],
                },
            },
        }
    }


def _patch_checkpoints(monkeypatch, slots):
    import evolution_infra

    monkeypatch.setattr(
        evolution_infra, "read_all_pipeline_checkpoints", lambda: slots
    )


def _mcp_payload(result):
    assert result["content"][0]["type"] == "text"
    return json.loads(result["content"][0]["text"])


@pytest.mark.asyncio
async def test_manual_reap_blocked_by_frozen_precommit_plan(monkeypatch):
    import tool_bot_management
    import tool_bot_management_reap as tbmr

    _patch_checkpoints(monkeypatch, _frozen_plan_slots())

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("manual reap reached _do_reap_weakest under a "
                             "frozen precommit plan")

    monkeypatch.setattr(tool_bot_management, "_do_reap_weakest", forbidden)

    # Drive the real MCP entry (the guarded wrapper the SDK would invoke;
    # the runtime git guard is disabled under pytest).
    result = await tbmr.reap_weakest.handler({})

    data = _mcp_payload(result)
    assert data["reaped"] is False
    assert data["error"] == "manual_reap_blocked_frozen_precommit_plan"
    assert "存在冻结 precommit 计划" in data["reason"]
    assert data["frozen_precommit_slots"] == ["primary"]


@pytest.mark.asyncio
async def test_automatic_handoff_reap_not_blocked_by_the_guard(
    monkeypatch, tmp_path
):
    """Handoff step-6 shape bypasses the manual-entry guard entirely."""
    import test_pool_reap_target_size_20261011 as helper
    import tool_bot_management as tbm

    # The frozen plan stays visible the whole time: the automatic path must
    # still reap.
    _patch_checkpoints(monkeypatch, _frozen_plan_slots())

    monkeypatch.setenv("POK_PUBLISH_MODE", "pool_ratchet")
    monkeypatch.setenv("POK_POOL_RATCHET_TARGET_SIZE", "12")
    versions = list(range(1, 14))
    ratings = {v: (1200.0 + (v - 1) * 50.0, 100.0) for v in versions}
    helper._setup_pool(
        monkeypatch,
        tmp_path,
        versions,
        rating_by_version=ratings,
        priority_version=13,
    )
    captured = []
    monkeypatch.setattr(
        tbm,
        "record_reaped_bot",
        lambda name, *, reason, data: captured.append((name, reason)),
    )

    snapshot = tbm._capture_reap_selection_snapshot()
    result = await tbm._do_reap_weakest(
        quiet=True,
        expected_culled=bot_name(1),
        selection_snapshot=snapshot,
    )

    assert result["reaped"] is True
    assert result["culled"] == bot_name(1)
    assert captured == [(bot_name(1), "pool_ratchet_replace_weakest")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "slots",
    [
        {},  # no active checkpoint at all
        {  # active checkpoint without a frozen plan
            "primary": {
                "next_v": 14,
                "source_v": 13,
                "stage": "workers_done",
                "workflow_run_id": "workflow-v14#1",
                "checkpoint_revision": 2,
                "audit_context": {},
            }
        },
        {  # a plan entry that is not a frozen dict is not a plan
            "draft1": {
                "next_v": 14,
                "source_v": 13,
                "stage": "master_planned",
                "workflow_run_id": "workflow-v14#2",
                "checkpoint_revision": 1,
                "audit_context": {"precommit_eval_plan": None},
            }
        },
    ],
)
async def test_manual_reap_allowed_without_frozen_plan(monkeypatch, slots):
    import tool_bot_management
    import tool_bot_management_reap as tbmr

    _patch_checkpoints(monkeypatch, slots)

    calls = []

    async def reap(*, quiet=False, **kwargs):
        calls.append((quiet, kwargs))
        return {"reaped": True, "culled": bot_name(1)}

    monkeypatch.setattr(tool_bot_management, "_do_reap_weakest", reap)

    result = await tbmr.reap_weakest.handler({})

    data = _mcp_payload(result)
    assert data == {"reaped": True, "culled": bot_name(1)}
    assert calls == [(False, {})]
