"""Backend projections for the checkpoint-free post-publication phase."""

from __future__ import annotations

from bot_namespace import bot_name
from conftest import STRICT_SOURCE_V, STRICT_TARGET_V


def _route(*, state="pending", revision=2, owner_scope=None):
    owner_scope = owner_scope or (
        "current_process" if state == "running" else "none"
    )
    return {
        "status": "pending",
        "version": STRICT_TARGET_V,
        "source_v": STRICT_SOURCE_V,
        "workflow_run_id": f"generation:{STRICT_TARGET_V}:workflow-v1",
        "identity_digest": "a" * 64,
        "publication_id": "b" * 64,
        "state": state,
        "owner_scope": owner_scope,
        "record": {
            "revision": revision,
            "private_checkpoint_and_receipts": "must-not-reach-http",
        },
        "issues": [],
    }


def test_handoff_projection_is_whitelisted_and_revision_bound(monkeypatch):
    import post_publication_handoff
    from server.routes._helpers import post_publication_handoff_projection

    monkeypatch.setattr(
        post_publication_handoff,
        "pending_handoff_route",
        lambda: _route(),
    )
    first = post_publication_handoff_projection()

    assert first["status"] == "pending"
    assert first["record_revision"] == 2
    assert first["identity_digest"] == "a" * 64
    assert first["owner_scope"] == "none"
    assert "record" not in first
    assert "private_checkpoint_and_receipts" not in str(first)
    assert len(first["projection_digest"]) == 64
    assert len(first["steps"]) == 8
    assert first["current_step"] == "stability_observation"
    assert first["completed_count"] == 0
    assert all(step["status"] == "pending" for step in first["steps"])
    for step in first["steps"]:
        assert "plan" not in step  # no plan body key
        assert set(step) == {
            "id", "ordinal", "status", "plan_digest", "receipt_digest", "updated_at",
        }

    monkeypatch.setattr(
        post_publication_handoff,
        "pending_handoff_route",
        lambda: _route(state="running", revision=3),
    )
    second = post_publication_handoff_projection()
    assert second["status"] == "running"
    assert second["owner_scope"] == "current_process"
    assert second["projection_digest"] != first["projection_digest"]


def test_handoff_projection_includes_whitelisted_steps(monkeypatch):
    import post_publication_handoff
    from post_publication_handoff import REQUIRED_STEPS
    from server.routes._helpers import (
        post_publication_handoff_projection,
        project_handoff_steps,
    )

    plan_digest = "c" * 64
    receipt_digest = "d" * 64
    steps = {
        name: {"status": "pending"} for name in REQUIRED_STEPS
    }
    steps["stability_observation"] = {
        "status": "completed",
        "plan_digest": plan_digest,
        "plan": {"secret": "must-not-leak"},
        "receipt": {
            "receipt_digest": receipt_digest,
            "completed_at": 100.0,
            "output": {"secret": "must-not-leak"},
        },
    }
    steps["reap_signal"] = {
        "status": "planned",
        "plan_digest": "e" * 64,
        "plan": {"inner": True},
    }
    route = _route(state="running", revision=4)
    route["record"] = {
        **route["record"],
        "steps": steps,
        "updated_at": 200.0,
    }
    monkeypatch.setattr(
        post_publication_handoff,
        "pending_handoff_route",
        lambda: route,
    )
    projection = post_publication_handoff_projection()

    assert projection["completed_count"] == 1
    assert projection["current_step"] == "reap_signal"
    assert len(projection["steps"]) == len(REQUIRED_STEPS)
    first = projection["steps"][0]
    assert first["id"] == "stability_observation"
    assert first["ordinal"] == 1
    assert first["status"] == "completed"
    assert first["plan_digest"] == plan_digest
    assert first["receipt_digest"] == receipt_digest
    assert first["updated_at"] == 100.0
    assert "secret" not in str(projection)
    assert "plan" not in first
    assert "receipt" not in first
    second = projection["steps"][1]
    assert second["id"] == "reap_signal"
    assert second["status"] == "running"
    assert second["plan_digest"] == "e" * 64
    assert second["receipt_digest"] is None

    empty = project_handoff_steps(None, handoff_status="none")
    assert empty == {"steps": [], "current_step": None, "completed_count": 0}


def test_pipeline_health_projects_handoff_when_checkpoint_is_cleared():
    import server.routes.control as control

    handoff = {
        "status": "pending",
        "state": "pending",
        "blocked": False,
        "version": STRICT_TARGET_V,
        "source_v": STRICT_SOURCE_V,
        "workflow_run_id": f"generation:{STRICT_TARGET_V}:workflow-v1",
        "identity_digest": "a" * 64,
        "publication_id": "b" * 64,
        "record_revision": 3,
        "projection_digest": "c" * 64,
        "owner_scope": "none",
        "issues": [],
    }
    snapshot = control._read_pipeline_health({
        "epoch_initialized": True,
        "epoch_state": "strict_published",
        "active_generation": None,
        "post_publication_handoff": handoff,
    })

    assert snapshot["exists"] is True
    assert snapshot["stage"] == "post_publication_handoff"
    assert snapshot["authority"] == "post_publication_handoff_journal"
    assert snapshot["route"]["next_tool"] == "run_archivist"
    assert snapshot["handoff_identity_digest"] == "a" * 64
    assert snapshot["handoff_projection_digest"] == "c" * 64


def test_pipeline_health_blocks_foreign_handoff_owner_but_accepts_exact_current_owner(
    monkeypatch,
):
    import server.routes.control as control

    base_handoff = {
        "status": "running",
        "state": "running",
        "blocked": False,
        "version": STRICT_TARGET_V,
        "source_v": STRICT_SOURCE_V,
        "workflow_run_id": f"generation:{STRICT_TARGET_V}:workflow-v1",
        "identity_digest": "a" * 64,
        "publication_id": "b" * 64,
        "record_revision": 3,
        "projection_digest": "c" * 64,
        "issues": [],
    }
    foreign = control._read_pipeline_health({
        "running": False,
        "epoch_initialized": True,
        "epoch_state": "strict_published",
        "active_generation": None,
        "post_publication_handoff": {
            **base_handoff,
            "owner_scope": "foreign_process",
        },
    })

    assert foreign["blocked"] is True
    assert foreign["route"] is None
    assert foreign["handoff_owner_scope"] == "foreign_process"
    assert "post_publication_handoff_foreign_owner_active" in foreign["issues"]

    monkeypatch.setattr(
        control.app_state,
        "task_snapshot",
        lambda: {
            "present": True,
            "done": False,
            "cancelled": False,
            "shutdown_requested": False,
            "owner_id": "runtime-owner",
        },
    )
    monkeypatch.setattr(
        control.app_state,
        "runtime_owner_id",
        lambda: "runtime-owner",
    )
    current = control._read_pipeline_health({
        "running": True,
        "epoch_initialized": True,
        "epoch_state": "strict_published",
        "active_generation": None,
        "post_publication_handoff": {
            **base_handoff,
            "owner_scope": "current_process",
        },
    })

    assert current["blocked"] is False
    assert current["route"]["next_tool"] == "run_archivist"
    assert current["handoff_owner_scope"] == "current_process"


def test_active_generation_and_handoff_overlap_is_blocked():
    import server.routes.control as control

    snapshot = control._read_pipeline_health({
        "epoch_initialized": True,
        "active_generation": {"next_v": STRICT_TARGET_V + 1},
        "post_publication_handoff": {
            "status": "pending",
            "blocked": False,
            "version": STRICT_TARGET_V,
            "source_v": STRICT_SOURCE_V,
            "workflow_run_id": f"generation:{STRICT_TARGET_V}:workflow-v1",
            "identity_digest": "a" * 64,
            "publication_id": "b" * 64,
            "record_revision": 2,
            "projection_digest": "c" * 64,
            "issues": [],
        },
    })

    assert snapshot["blocked"] is True
    assert snapshot["route"] is None
    assert "active_generation_and_handoff_overlap" in snapshot["issues"]


def test_effective_handoff_conflict_degrades_health(monkeypatch):
    import time

    import server.routes.control as control

    monkeypatch.setattr(
        control.app_state,
        "task_snapshot",
        lambda: {
            "present": True,
            "done": False,
            "cancelled": False,
            "shutdown_requested": False,
        },
    )
    monkeypatch.setattr(
        control,
        "_daemon_health_snapshot",
        lambda: {
            "configured": False,
            "alive": False,
            "heartbeat_status": "not_applicable",
        },
    )
    monkeypatch.setattr(
        control,
        "_read_pipeline_health",
        lambda _status: {
            "exists": True,
            "stage": "post_publication_handoff",
            "blocked": True,
            "issues": ["active_generation_and_handoff_overlap"],
        },
    )
    health = control._health_summary({
        "running": True,
        "daemon_enabled": False,
        "epoch_initialized": True,
        "active_generation": {"next_v": STRICT_TARGET_V + 1},
        "post_publication_handoff": {"blocked": False},
        "stability_observation": {
            "continuity_valid": True,
            "verification": {
                "state": "fresh",
                "fresh_until": time.time() + 60,
            },
        },
    })

    assert health["overall"] == "degraded"
    assert "pipeline_blocked" in health["issues"]


def test_uninitialized_projection_never_opens_handoff_journal(monkeypatch):
    import post_publication_handoff
    from server.routes._helpers import post_publication_handoff_projection

    monkeypatch.setattr(
        post_publication_handoff,
        "pending_handoff_route",
        lambda: (_ for _ in ()).throw(AssertionError("legacy journal opened")),
    )
    projection = post_publication_handoff_projection(enabled=False)

    assert projection["status"] == "none"
    assert projection["identity_digest"] is None


def test_control_status_fails_closed_on_torn_epoch_handoff_sample(monkeypatch):
    import itertools

    import epoch_authority
    import post_publication_handoff
    import server.routes.control as control
    import stability_observation

    projections = itertools.cycle((
        {
            "evaluation_epoch": "national_tcp_policy_v1",
            "state": "fresh_bootstrap_ready",
            "initialized": True,
            "sample": "before-publication",
        },
        {
            "evaluation_epoch": "national_tcp_policy_v1",
            "state": "strict_published",
            "initialized": True,
            "sample": "after-publication",
        },
    ))
    monkeypatch.setattr(
        epoch_authority,
        "strict_epoch_projection",
        lambda **_kwargs: next(projections),
    )
    monkeypatch.setattr(
        post_publication_handoff,
        "pending_handoff_route",
        lambda: _route(state="pending", revision=2),
    )
    monkeypatch.setattr(
        stability_observation,
        "stability_observation_cached_projection",
        lambda: {},
    )

    snapshot = control._sync_evolution_fields({})

    assert snapshot["epoch_state"] == "epoch_authority_unavailable"
    assert snapshot["epoch_initialized"] is False
    assert snapshot["stream_authority_digest"] is None
    assert snapshot["active_generations"] == []
    assert snapshot["pipeline_mode"]["in_flight_count"] == 0
    assert snapshot["async_certification"]["items"] == []
    assert "canonical_epoch_changed_during_handoff_projection" in snapshot[
        "status_sync_error"
    ]
    assert snapshot["post_publication_handoff"]["status"] == "none"


def test_control_status_rejects_stability_cache_from_another_epoch(monkeypatch):
    import epoch_authority
    import post_publication_handoff
    import server.routes.control as control
    import stability_observation

    epoch = {
        "current_v": STRICT_TARGET_V,
        "next_v": STRICT_TARGET_V + 1,
        "strict_generation_count": 1,
        "active_generation": None,
        "evaluation_epoch": "national_tcp_policy_v1",
        "state": "strict_published",
        "initialized": True,
        "version_authority_high_water": STRICT_TARGET_V,
        "strict_published_versions": [STRICT_TARGET_V],
        "active_bots": [bot_name(STRICT_TARGET_V)],
        "reset_receipt_valid": True,
        "reset_receipt_digest": "a" * 64,
        "reset_receipt_issues": [],
        "operator_action": None,
        "operator_command": None,
        "ignored_checkpoint": None,
    }
    monkeypatch.setattr(
        epoch_authority,
        "strict_epoch_projection",
        lambda **_kwargs: dict(epoch),
    )
    monkeypatch.setattr(
        epoch_authority,
        "unpublished_candidate_versions",
        lambda: [],
    )
    monkeypatch.setattr(
        post_publication_handoff,
        "pending_handoff_route",
        lambda: {"status": "none", "records": [], "issues": []},
    )
    requested_authorities = []

    def stale_green(*, expected_epoch_authority_digest=None):
        requested_authorities.append(expected_epoch_authority_digest)
        return {
            "continuity_valid": True,
            "count": 10,
            "target": 10,
            "remaining": 0,
            "complete": True,
            "verification": {
                "state": "fresh",
                "authority": {
                    "evaluation_epoch": "national_tcp_policy_v1",
                    "epoch_stream_authority_digest": "f" * 64,
                    "repository_head": "b" * 40,
                    "repository_branch": "main",
                },
            },
        }

    monkeypatch.setattr(
        stability_observation,
        "stability_observation_cached_projection",
        stale_green,
    )

    snapshot = control._sync_evolution_fields({})

    assert len(requested_authorities) == 1
    assert requested_authorities[0] == snapshot["stream_authority_digest"]
    assert requested_authorities[0] != "f" * 64
    assert snapshot["stability_observation"]["count"] == 0
    assert snapshot["stability_observation"]["complete"] is False
    assert "projection_failed:RuntimeError" in snapshot[
        "stability_observation"
    ]["errors"]
