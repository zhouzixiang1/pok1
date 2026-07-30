"""Regression guard for the v12/v13 deterministic-route death loop.

The strict-authority abandon fence once rejected any reproof whose abandon
reason differed from the caller-supplied reason, even when the instance was
already fenced under a different (concrete executor) reason. That asymmetry --
the Worker journal tolerates reason drift (``WorkerWorkflow.abandon``'s
``accept_existing_reason``) but its strict-authority dual did not -- made the
persisted terminal journal impossible to reprove from a router replay that
supplies the abstract routing constant. The outer checkpoint could therefore
never clear, producing the deterministic-route death loop.

These tests pin the fix: for an already-abandoned instance the persisted
terminal event's reason is the single source of truth (self-verifying on read
via content_digest, bound to the fence causation id), so a reproof may supply a
different reason and still reproduce the original tombstone. Every non-reason
field remains exact-bound, so the relaxation does not weaken structural
integrity.
"""

from __future__ import annotations

import sqlite3

import pytest


def _checkpoint(workflow_run_id: str = "generation:901:existing-reason-regression") -> dict:
    return {
        "workflow_run_id": workflow_run_id,
        "source_v": 900,
        "next_v": 901,
        "stage": "workers_done",
        "checkpoint_revision": 7,
    }


@pytest.fixture
def authority(monkeypatch, tmp_path):
    import evolution_infra
    import strict_authority_workflow as module
    from workflow_kernel import WorkflowStore

    results_dir = tmp_path / "results"
    store = WorkflowStore(results_dir / "workflow" / "events.sqlite3")
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(module, "_store", lambda: store)
    return module, store


# A concrete executor reason whitelisted by the worker exit-path contract
# (worker_exit_path_fixture.EXPECTED_ABANDON_REASONS), i.e. exactly the kind of
# reason the v12/v13 worker journal terminalized under.
REASON_X = "worker_terminal_abandon_rework_task_authority_invalid"
# The abstract routing constant a router replay would have supplied.
REASON_Y = "worker_workflow_abandoned"


def test_existing_terminal_reason_lets_drifted_reproof_succeed(authority):
    """Reproof of an already-fenced instance tolerates reason drift.

    This is the direct regression for the v12/v13 death loop: tombstone written
    under concrete reason X, then reproof with abstract reason Y must succeed by
    reproducing the persisted X tombstone (not fail with fence_identity_invalid).
    """
    module, store = authority
    checkpoint = _checkpoint()
    run_id = module.authority_run_id(checkpoint["workflow_run_id"])

    # 1. Terminalize the strict child under the concrete executor reason X via
    #    the real fence (no mocking).
    first = module.abandon_authority(checkpoint, reason=REASON_X)
    assert first["abandoned"] is True
    assert first["fence_epoch"] == 1
    terminal_events = [
        event
        for event in store.events(run_id)
        if event.event_type == "StrictAuthorityAbandoned"
    ]
    assert len(terminal_events) == 1
    persisted_payload = terminal_events[0].payload
    assert persisted_payload == {
        "reason": REASON_X,
        "workflow_run_id": checkpoint["workflow_run_id"],
    }

    # 2. Re-fence with a DIFFERENT (abstract routing) reason. Before the fix this
    #    raised strict_authority_abandon_fence_identity_invalid and never cleared.
    drifted = module.abandon_authority(checkpoint, reason=REASON_Y)
    assert drifted["abandoned"] is True
    assert drifted["fence_epoch"] == 1
    # The persisted tombstone is unchanged: exactly one terminal event, same
    # payload/causation id as the original X tombstone (the drift is not written
    # back -- reproof only reproduces, it never rewrites).
    after = [
        event
        for event in store.events(run_id)
        if event.event_type == "StrictAuthorityAbandoned"
    ]
    assert after == terminal_events


def test_existing_terminal_reason_correct_reproof_still_succeeds(authority):
    """Normal idempotent reproof with the original reason is unaffected."""
    module, _store = authority
    checkpoint = _checkpoint()
    first = module.abandon_authority(checkpoint, reason=REASON_X)
    repeated = module.abandon_authority(checkpoint, reason=REASON_X)
    assert repeated == first
    assert repeated["fence_epoch"] == 1


def test_reason_drift_does_not_weaken_non_reason_strictness(authority):
    """Tampering with any non-reason structural field still fails the fence.

    Even though reason drift is tolerated for an already-fenced instance, the
    definition_version / stream_version / fence_epoch / seq / schema_version
    fields remain exact-bound. This guards against the fix opening an injection
    hole.
    """
    module, store = authority
    checkpoint = _checkpoint()
    run_id = module.authority_run_id(checkpoint["workflow_run_id"])
    module.abandon_authority(checkpoint, reason=REASON_X)

    # definition_version tampering -> still rejected, reason drift or not.
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE workflow_instances SET definition_version = 99 "
            "WHERE run_id = ?",
            (run_id,),
        )
        connection.commit()
    with pytest.raises(
        module.StrictAuthorityError,
        match="strict_authority_abandon_fence_identity_invalid",
    ):
        module.abandon_authority(checkpoint, reason=REASON_X)
    with pytest.raises(
        module.StrictAuthorityError,
        match="strict_authority_abandon_fence_identity_invalid",
    ):
        module.abandon_authority(checkpoint, reason=REASON_Y)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE workflow_instances SET definition_version = ? "
            "WHERE run_id = ?",
            (module.DEFINITION_VERSION, run_id),
        )
        connection.commit()

    # fence_epoch tampering -> rejected (drift must not mask a half-fenced row).
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE workflow_instances SET fence_epoch = 0 "
            "WHERE run_id = ?",
            (run_id,),
        )
        connection.commit()
    with pytest.raises(
        module.StrictAuthorityError,
        match="strict_authority_abandon_fence_identity_invalid",
    ):
        module.abandon_authority(checkpoint, reason=REASON_Y)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE workflow_instances SET fence_epoch = 1 "
            "WHERE run_id = ?",
            (run_id,),
        )
        connection.commit()


def test_reason_drift_does_not_let_a_second_terminal_event_through(authority):
    """Two persisted terminal events still fail even if one matches a drifted reason.

    The verified-reason derivation is gated on len(terminal) == 1, so a
    duplicated/foreign terminal event keeps the fence strict regardless of
    reason text. (We synthesize this by appending a second StrictAuthorityAbandoned
    row and updating stream_version to match, then expect the count guard to fire.)
    """
    module, store = authority
    checkpoint = _checkpoint()
    run_id = module.authority_run_id(checkpoint["workflow_run_id"])
    module.abandon_authority(checkpoint, reason=REASON_X)

    second_payload, second_causation = module.strict_authority_abandon_event_identity(
        checkpoint, reason=REASON_Y,
    )
    from workflow_kernel import canonical_json

    encoded_payload = canonical_json(second_payload)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "INSERT INTO workflow_events "
            "(run_id, seq, event_type, schema_version, payload, payload_digest, "
            "causation_id, created_at) VALUES (?, 2, 'StrictAuthorityAbandoned', "
            "1, ?, ?, ?, 2)",
            (
                run_id,
                encoded_payload,
                module.content_digest(second_payload),
                second_causation,
            ),
        )
        connection.execute(
            "UPDATE workflow_instances SET stream_version = 2 "
            "WHERE run_id = ?",
            (run_id,),
        )
        connection.commit()
    with pytest.raises(
        (module.StrictAuthorityError, module.WorkflowConflict),
    ):
        module.abandon_authority(checkpoint, reason=REASON_Y)


def test_reason_drift_does_not_apply_to_first_creation(authority):
    """First creation (instance not yet abandoned) still uses the caller reason.

    The persisted-reason derivation is gated on status == 'abandoned', so the
    very first tombstone is written under exactly the reason the caller supplies.
    """
    module, store = authority
    checkpoint = _checkpoint()
    run_id = module.authority_run_id(checkpoint["workflow_run_id"])
    instance = store.instance(run_id)
    assert not instance  # no instance row exists before first abandon

    module.abandon_authority(checkpoint, reason=REASON_X)
    terminal = [
        event
        for event in store.events(run_id)
        if event.event_type == "StrictAuthorityAbandoned"
    ][0]
    assert terminal.payload["reason"] == REASON_X
