"""Checkpoint-free Archivist routing at every orchestrator recovery boundary."""

from __future__ import annotations


def _pending_route():
    return {
        "status": "pending",
        "version": 143,
        "source_v": 142,
        "workflow_run_id": "generation:143:workflow-v1",
        "identity_digest": "a" * 64,
        "publication_id": "b" * 64,
        "state": "pending",
        "record": {"revision": 2},
        "issues": [],
    }


def test_checkpoint_recovery_routes_pending_handoff_without_checkpoint(monkeypatch):
    import evolution_core
    import orchestrator
    import post_publication_handoff

    monkeypatch.setattr(evolution_core, "read_pipeline_checkpoint", lambda: None)
    monkeypatch.setattr(
        post_publication_handoff,
        "pending_handoff_route",
        _pending_route,
    )

    recovery = orchestrator._checkpoint_recovery_context("test")

    assert recovery["action"] == "resume"
    assert recovery["post_publication_handoff"] is True
    assert recovery["stage"] == "post_publication_handoff"
    assert recovery["checkpoint"]["post_publication_handoff_route"] is True
    assert recovery["checkpoint"]["post_publication_handoff_identity_digest"] == "a" * 64


def test_checkpoint_recovery_fails_closed_on_invalid_handoff(monkeypatch):
    import evolution_core
    import orchestrator
    import post_publication_handoff

    monkeypatch.setattr(evolution_core, "read_pipeline_checkpoint", lambda: None)
    monkeypatch.setattr(
        post_publication_handoff,
        "pending_handoff_route",
        lambda: {"status": "blocked", "issues": ["journal_tampered"]},
    )

    recovery = orchestrator._checkpoint_recovery_context("test")

    assert recovery["action"] == "blocked"
    assert recovery["diagnostics"]["post_publication_handoff"] is True
    assert "journal_tampered" in recovery["diagnostics"]["issues"]


def test_provider_stream_yields_immediately_to_new_handoff(monkeypatch):
    import evolution_core
    import orchestrator
    import post_publication_handoff

    monkeypatch.setattr(
        orchestrator,
        "_detect_actionable_stage_stall",
        lambda timeout_sec=0: None,
    )
    monkeypatch.setattr(evolution_core, "read_pipeline_checkpoint", lambda: None)
    monkeypatch.setattr(
        post_publication_handoff,
        "pending_handoff_route",
        _pending_route,
    )

    route = orchestrator._detect_actionable_stage_handoff()

    assert route == {
        "next_v": 143,
        "source_v": 142,
        "stage": "post_publication_handoff",
        "next_tool": "run_archivist",
        "directive": (
            "End the current provider stream and resume the exact durable "
            "Archivist handoff."
        ),
    }
