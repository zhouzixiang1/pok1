"""Crossover synthesis LLM-availability deferral is attempt-neutral (P5).

``_run_crossover``'s ``LLMAvailabilityBlocked`` handler used to record
``fail_effect(retryable=True)``: unlike the Worker availability defer, that
FAILS to roll the claim's attempt increment back, so a 1302 storm (pause every
3-7 minutes, each re-route claiming the same stable effect id) burned the
16-lease ``CROSSOVER_SYNTHESIS_MAX_LEASE_ATTEMPTS`` budget while no model
could run — the generation then died as ``crossover_llm_exhausted`` (~70
ledger entries at the wedge matrix).

Contract: the availability branch defers the lease
(``reason='llm_availability_blocked'`` with pause category/evidence_digest
metadata), the deferral restores the attempt count, and a post-pause re-entry
resumes the SAME effect and re-dispatches without the pause having consumed
any of the lease budget.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot_artifact import hash_path
from bot_namespace import bot_name
from llm_availability import LLMAvailabilityBlocked

from test_nonworker_llm_availability_deferral import (
    _UI,
    _blocked,
    _checkpoint,
    _write_checkpoint_bytes,
    _write_strict_bot,
)


@pytest.fixture
def harness(tmp_path, monkeypatch):
    import agent_review
    import audit_agents
    import candidate_hygiene
    import evolution_infra
    import workflow_profiles

    bots = tmp_path / "bots"
    prompts = tmp_path / "prompts"
    logs = tmp_path / "logs"
    results = tmp_path / "results"
    parent_a = _write_strict_bot(bots / bot_name(150), policy_value=1)
    parent_b = _write_strict_bot(bots / bot_name(149), policy_value=2)
    target = _write_strict_bot(bots / bot_name(151), policy_value=3)
    prompts.mkdir()
    logs.mkdir()
    (prompts / "crossover_prompt.md").write_text(
        "crossover {{version}}", encoding="utf-8"
    )
    checkpoint = _checkpoint(151, 150, "selected", parent2_v=149)
    checkpoint.update({
        "checkpoint_schema_version": 2,
        "evaluation_epoch": "national_tcp_policy_v1",
        "epoch_binding": {
            "binding_digest": "e" * 64,
            "published_parent_identities": [
                {"tag_artifact_hash": hash_path(parent_a)},
                {"tag_artifact_hash": hash_path(parent_b)},
            ],
        },
    })
    checkpoint_file = tmp_path / "pipeline_state.json"
    _write_checkpoint_bytes(checkpoint_file, checkpoint)

    def bot_dir(version):
        return {150: parent_a, 149: parent_b, 151: target}[int(version)]

    monkeypatch.setattr(agent_review, "PROMPTS_DIR", prompts)
    monkeypatch.setattr(agent_review, "RESULTS_DIR", results)
    monkeypatch.setattr(agent_review, "MAX_CROSSOVER_RETRIES", 3)
    monkeypatch.setattr(agent_review, "get_bot_dir", bot_dir)
    monkeypatch.setattr(agent_review, "get_logs_dir", lambda _v: logs)
    monkeypatch.setattr(
        evolution_infra, "read_pipeline_checkpoint", lambda: checkpoint
    )
    monkeypatch.setattr(
        evolution_infra,
        "write_pipeline_checkpoint",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("availability projected crossover checkpoint")
        ),
    )
    monkeypatch.setattr(
        candidate_hygiene, "sanitize_candidate_dir", lambda *_a, **_k: {}
    )
    monkeypatch.setattr(
        workflow_profiles,
        "get_workflow_profile",
        lambda: SimpleNamespace(national_execution_mode="native_tcp"),
    )
    # Isolate the pause-store reads used by the deferred-effect resume gate.
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)

    import checkpoint_schema
    from worker_workflow import WorkerArtifactStore

    monkeypatch.setattr(checkpoint_schema, "checkpoint_epoch_errors", lambda _c: [])
    monkeypatch.setattr(
        checkpoint_schema,
        "live_checkpoint_parent_authority_errors",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(audit_agents, "RESULTS_DIR", results)
    monkeypatch.setattr(audit_agents, "get_bot_dir", bot_dir)
    monkeypatch.setattr(
        audit_agents,
        "frozen_crossover_parent_architecture",
        lambda _bundle: {
            "architecture_policy": {},
            "capability_context": {},
        },
    )
    snapshot_bundle = audit_agents.capture_crossover_parent_snapshots(
        150,
        149,
        151,
        checkpoint=checkpoint,
        checkpoint_reader=lambda: checkpoint,
        artifact_store=WorkerArtifactStore(results / "workflow" / "artifacts"),
    )
    return SimpleNamespace(
        agent_review=agent_review,
        checkpoint=checkpoint,
        compatibility={
            "parent_snapshot_receipt": snapshot_bundle["receipt"],
        },
        effect_id="crossover-synthesis:wf-151:attempt-1",
    )


def _open_store(harness):
    from workflow_kernel import WorkflowStore

    return WorkflowStore(
        harness.agent_review.RESULTS_DIR / "workflow" / "events.sqlite3"
    )


def test_availability_pause_defers_effect_without_consuming_attempt(
    harness, monkeypatch
):
    blocked = _blocked("crossover")
    calls = []

    async def unavailable(*_args, **_kwargs):
        calls.append("blocked")
        raise blocked

    monkeypatch.setattr(harness.agent_review, "run_claude_query", unavailable)

    with pytest.raises(LLMAvailabilityBlocked):
        asyncio.run(harness.agent_review._run_crossover(
            150, 149, 151, _UI(), compatibility=harness.compatibility,
        ))

    assert calls == ["blocked"]
    effect = _open_store(harness).effect(harness.effect_id)
    assert effect["status"] == "deferred"
    assert int(effect["attempt"]) == 0
    assert "llm_availability_blocked" in str(effect.get("last_error") or "")


def test_1302_storm_never_exhausts_the_lease_budget(harness, monkeypatch):
    """Repeated pauses must not walk the 16-attempt lease budget."""

    calls = []

    async def storm(*_args, **_kwargs):
        calls.append("blocked")
        raise _blocked("crossover")

    monkeypatch.setattr(harness.agent_review, "run_claude_query", storm)

    for _ in range(6):
        with pytest.raises(LLMAvailabilityBlocked):
            asyncio.run(harness.agent_review._run_crossover(
                150, 149, 151, _UI(), compatibility=harness.compatibility,
            ))

    assert len(calls) == 6
    effect = _open_store(harness).effect(harness.effect_id)
    assert effect["status"] == "deferred"
    assert int(effect["attempt"]) == 0


def test_post_pause_reentry_resumes_same_effect_and_redispatches(
    harness, monkeypatch
):
    from workflow_kernel import WorkflowConflict

    calls = []

    async def blocked_then_generic(*_args, **_kwargs):
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            raise _blocked("crossover")
        # Post-pause dispatch reaches the provider and hits a generic SDK
        # failure; that semantic failure owns its attempt as usual.
        raise RuntimeError("sdk stream died")

    monkeypatch.setattr(
        harness.agent_review, "run_claude_query", blocked_then_generic
    )

    with pytest.raises(LLMAvailabilityBlocked):
        asyncio.run(harness.agent_review._run_crossover(
            150, 149, 151, _UI(), compatibility=harness.compatibility,
        ))

    store = _open_store(harness)
    deferred = store.effect(harness.effect_id)
    assert deferred["status"] == "deferred"
    assert int(deferred["attempt"]) == 0

    # The pause cleared (no active pause in the isolated store): re-entry
    # resumes the SAME deferred effect and re-dispatches the provider.
    result = asyncio.run(harness.agent_review._run_crossover(
        150, 149, 151, _UI(), compatibility=harness.compatibility,
    ))
    # The first post-pause dispatch is the SECOND provider call overall; the
    # generic failure then walks the legacy bounded attempts (attempt-2/3
    # effect ids) as before — unchanged pre-P5 behavior.
    assert calls[:2] == [1, 2]
    assert result is not None
    effect = store.effect(harness.effect_id)
    # The generic provider failure (not the pause) consumed exactly one
    # attempt on the same effect id; the availability pause consumed none.
    assert int(effect["attempt"]) == 1
    assert effect["status"] in {"retry", "exhausted"}


def test_deferred_effect_waits_while_pause_is_still_active(
    harness, monkeypatch
):
    import llm_availability_store as pause_store

    blocked = _blocked("crossover")
    monkeypatch.setattr(
        harness.agent_review, "run_claude_query",
        lambda *_a, **_k: (_ for _ in ()).throw(blocked),
    )

    with pytest.raises(LLMAvailabilityBlocked):
        asyncio.run(harness.agent_review._run_crossover(
            150, 149, 151, _UI(), compatibility=harness.compatibility,
        ))

    store = _open_store(harness)
    assert store.effect(harness.effect_id)["status"] == "deferred"

    # Simulate a still-active pause: re-entry must NOT claim/dispatch.
    active_pause = {
        "schema_version": 2,
        "active": True,
        "source": "llm_availability",
        "category": blocked.issue.category,
        "summary": blocked.issue.summary,
        "http_status": 403,
        "retry_policy": "manual_resume",
        "requires_manual_resume": True,
        "persistent_pause": True,
        "evidence_digest": blocked.issue.evidence_digest,
        "first_observed_at": "2026-10-05T00:00:00+00:00",
        "last_observed_at": "2026-10-05T00:00:00+00:00",
        "occurrences": 1,
        "auto_resume_at": None,
    }
    monkeypatch.setattr(
        pause_store, "active_llm_pause", lambda **_k: active_pause
    )

    async def must_not_dispatch(*_a, **_k):
        raise AssertionError("dispatched while the pause is still active")

    monkeypatch.setattr(
        harness.agent_review, "run_claude_query", must_not_dispatch
    )
    asyncio.run(harness.agent_review._run_crossover(
        150, 149, 151, _UI(), compatibility=harness.compatibility,
    ))
    effect = store.effect(harness.effect_id)
    assert effect["status"] == "deferred"
    assert int(effect["attempt"]) == 0
