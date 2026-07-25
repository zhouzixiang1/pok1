"""The retired cleanup helper cannot turn stale bot debris into authority."""

from __future__ import annotations

import asyncio
import json

import tool_bot_management as management
from bot_namespace import bot_name
from conftest import STRICT_SOURCE_V, STRICT_TARGET_V


def _decode(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


def _checkpoint(
    *,
    next_v=STRICT_TARGET_V,
    source_v=STRICT_SOURCE_V,
    workflow=f"generation:{STRICT_TARGET_V}:current",
    revision=7,
):
    return {
        "next_v": next_v,
        "source_v": source_v,
        "stage": "master_planned",
        "workflow_run_id": workflow,
        "checkpoint_revision": revision,
    }


def _initialized(_operation):
    return {
        "initialized": True,
        "evaluation_epoch": "national_tcp_policy_v1",
        "state": "fresh_bootstrap_ready",
    }


# A debris version strictly greater than the current target so it can never be
# the active workflow candidate.  Branch-portable: STRICT_TARGET_V + N.
_DEBRIS_V = STRICT_TARGET_V + 12


def test_cleanup_is_not_exposed_in_mcp_or_compatibility_catalog():
    import tools

    assert "cleanup_incomplete" not in {tool.name for tool in tools.mcp_tools}
    assert "cleanup_incomplete" not in {tool.name for tool in tools.all_tools}


def test_cleanup_refuses_before_strict_epoch_initialization(tmp_path, monkeypatch):
    import epoch_authority

    candidate = tmp_path / bot_name(_DEBRIS_V)
    candidate.mkdir()

    class NotInitialized(RuntimeError):
        state = {"initialized": False, "state": "reset_required"}

    monkeypatch.setattr(
        epoch_authority,
        "require_policy_epoch_initialized",
        lambda _operation: (_ for _ in ()).throw(NotInitialized()),
    )
    monkeypatch.setattr(
        management,
        "read_pipeline_checkpoint",
        lambda: _checkpoint(next_v=_DEBRIS_V),
    )

    payload = _decode(asyncio.run(management.cleanup_incomplete({
        "workflow_run_id": f"generation:{_DEBRIS_V}:stale",
        "next_v": _DEBRIS_V,
        "checkpoint_revision": 7,
    })))

    assert payload["error"] == "policy_epoch_not_initialized"
    assert candidate.exists()


def test_cleanup_stale_request_cannot_delete_current_or_v155_debris(
    tmp_path, monkeypatch
):
    import checkpoint_schema
    import epoch_authority

    bot_root = tmp_path / "bots"
    bot_root.mkdir()
    current = bot_root / bot_name(STRICT_TARGET_V)
    debris = bot_root / bot_name(_DEBRIS_V)
    current.mkdir()
    debris.mkdir()
    checkpoint = _checkpoint()

    monkeypatch.setattr(
        epoch_authority, "require_policy_epoch_initialized", _initialized
    )
    monkeypatch.setattr(management, "read_pipeline_checkpoint", lambda: checkpoint)
    monkeypatch.setattr(
        checkpoint_schema,
        "strict_checkpoint_event_identity",
        lambda *_args, **_kwargs: {
            "gen": STRICT_TARGET_V,
            "workflow_run_id": checkpoint["workflow_run_id"],
        },
    )
    monkeypatch.setattr(management, "BOTS_DIR", bot_root)
    monkeypatch.setattr(
        management,
        "get_bot_dir",
        lambda version: bot_root / bot_name(version),
    )
    called = []

    async def forbidden_abandon(**_kwargs):
        called.append(True)
        raise AssertionError("stale identity must stop before abandon")

    monkeypatch.setattr(management, "_do_abandon_generation", forbidden_abandon)

    payload = _decode(asyncio.run(management.cleanup_incomplete({
        "workflow_run_id": f"generation:{_DEBRIS_V}:retired",
        "next_v": _DEBRIS_V,
        "checkpoint_revision": 1,
    })))

    assert payload["error"] == "explicit_cleanup_identity_mismatch"
    assert called == []
    assert current.exists()
    assert debris.exists()


def test_raw_stale_checkpoint_cannot_authorize_cleanup(tmp_path, monkeypatch):
    import checkpoint_schema
    import epoch_authority

    bot_root = tmp_path / "bots"
    bot_root.mkdir()
    debris = bot_root / bot_name(_DEBRIS_V)
    debris.mkdir()
    checkpoint = _checkpoint(
        next_v=_DEBRIS_V,
        workflow=f"generation:{_DEBRIS_V}:legacy-wrapper",
        revision=1,
    )
    monkeypatch.setattr(
        epoch_authority, "require_policy_epoch_initialized", _initialized
    )
    monkeypatch.setattr(management, "read_pipeline_checkpoint", lambda: checkpoint)
    monkeypatch.setattr(management, "BOTS_DIR", bot_root)
    monkeypatch.setattr(management, "get_bot_dir", lambda _version: debris)

    def reject_stale(*_args, **_kwargs):
        raise checkpoint_schema.CheckpointSchemaError(
            ["checkpoint_epoch_binding_missing"]
        )

    monkeypatch.setattr(
        checkpoint_schema, "strict_checkpoint_event_identity", reject_stale
    )

    payload = _decode(asyncio.run(management.cleanup_incomplete({
        "workflow_run_id": checkpoint["workflow_run_id"],
        "next_v": _DEBRIS_V,
        "checkpoint_revision": 1,
    })))

    assert payload["error"] == "strict_checkpoint_invalid"
    assert debris.exists()


def test_exact_current_workflow_delegates_to_fenced_abandon_only(
    tmp_path, monkeypatch
):
    import checkpoint_schema
    import epoch_authority

    bot_root = tmp_path / "bots"
    bot_root.mkdir()
    candidate = bot_root / bot_name(STRICT_TARGET_V)
    candidate.mkdir()
    checkpoint = _checkpoint()

    monkeypatch.setattr(
        epoch_authority, "require_policy_epoch_initialized", _initialized
    )
    monkeypatch.setattr(management, "read_pipeline_checkpoint", lambda: checkpoint)
    monkeypatch.setattr(
        checkpoint_schema,
        "strict_checkpoint_event_identity",
        lambda *_args, **_kwargs: {
            "gen": STRICT_TARGET_V,
            "workflow_run_id": checkpoint["workflow_run_id"],
        },
    )
    monkeypatch.setattr(management, "BOTS_DIR", bot_root)
    monkeypatch.setattr(management, "get_bot_dir", lambda _version: candidate)
    monkeypatch.setattr(management, "git_has_tag", lambda _version: False)
    monkeypatch.setattr(management, "git_dir_is_committed", lambda _version: False)
    calls = []

    async def fenced_abandon(**kwargs):
        calls.append(kwargs)
        return {
            "abandoned": True,
            "removed_directory": bot_name(STRICT_TARGET_V),
            "workflow_run_id": checkpoint["workflow_run_id"],
        }

    monkeypatch.setattr(management, "_do_abandon_generation", fenced_abandon)

    payload = _decode(asyncio.run(management.cleanup_incomplete({
        "workflow_run_id": checkpoint["workflow_run_id"],
        "next_v": STRICT_TARGET_V,
        "checkpoint_revision": 7,
    })))

    assert payload["cleaned"] is True
    assert len(calls) == 1
    assert calls[0] == {
        "reason": "cleanup_incomplete_exact_workflow",
        "expected_workflow_run_id": checkpoint["workflow_run_id"],
        "expected_next_v": STRICT_TARGET_V,
        "expected_source_v": STRICT_SOURCE_V,
        "expected_checkpoint_revision": 7,
        "expected_checkpoint_stage": "master_planned",
    }
