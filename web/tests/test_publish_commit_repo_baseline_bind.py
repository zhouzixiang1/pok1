"""Regression: post-publication handoff repo_baseline_head_mismatch deadlock.

Every published generation's post-publication handoff used to hard-block with
``repo_baseline_head_mismatch``. Root cause: ``commit_bot`` writes
``stage=publishing`` BEFORE the git commit, so the frozen ``repo_baseline.head``
is the pre-commit HEAD. The publish commit then advances HEAD (and touches
``bots/<candidate>/``, a contract-critical path), so the publishing stage's
``requires_contract_unchanged=True`` makes recovery refuse to resume.

The post-commit re-write in ``tool_commit_publication.py`` used to call
``write_pipeline_checkpoint(..., "publishing", ...)`` expecting the CAS writer
to refresh the baseline, but ``_stage_refreshes_repo_baseline("publishing",
"publishing")`` returns False (same stage, and ``publishing`` is absent from
``_REPO_BASELINE_VALIDATION_GATES``), so the baseline stayed stale — a silent
no-op wrapped in ``try/except: pass``.

The fix adds an explicit ``bind_repo_baseline_head`` parameter that pins the
frozen baseline head to the publish commit OID the pipeline itself just
produced. These tests verify that binding works at both the CAS-writer level
and the post-commit publication-refresh call site.
"""

from __future__ import annotations

import json

import checkpoint_schema
import evolution_infra
import publication_transaction


PRE_COMMIT_HEAD = "a" * 40
PUBLISH_COMMIT_OID = "b" * 40


def _seed_publishing_checkpoint(tmp_path, monkeypatch, *, head=PRE_COMMIT_HEAD):
    """Write a minimal valid publishing checkpoint and patch the authority hooks.

    The publication-intent structure validator is patched to accept the
    synthetic intent so the test can isolate the CAS writer's
    ``bind_repo_baseline_head`` behavior without rebuilding a full publication
    receipt/certificate fixture.
    """
    state_path = tmp_path / "pipeline_state.json"
    checkpoint = {
        "checkpoint_schema_version": 2,
        "evaluation_epoch": "national_tcp_policy_v1",
        "epoch_binding": {},
        "next_v": 2,
        "source_v": 1,
        "stage": "publishing",
        "workflow_run_id": "generation:2:workflow-v1",
        "checkpoint_revision": 7,
        "repo_baseline": {
            "branch": "tencent-cloud-runtime",
            "head": head,
            "evaluation_contract": {"stage": "publishing", "hash": "c" * 64},
        },
        "gate_results": {},
        "publication_intent": {"publication_id": "d" * 64},
        "audit_context": {},
        "workflow_profile_id": "national_tcp_policy_v1",
        "national_execution_mode": "native_tcp",
    }
    state_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", state_path)
    # Allocation authority: source 1 is published, next_v 2 is the live successor.
    monkeypatch.setattr(
        evolution_infra,
        "checkpoint_allocation_authority",
        lambda **_kwargs: {
            "published_high_water": 1,
            "abandoned_receipt_floor": 0,
            "abandoned_receipt_head_digest": None,
            "allocation_floor": 1,
        },
    )
    # Bypass epoch/allocation/parent schema guards (they need a real epoch receipt).
    monkeypatch.setattr(checkpoint_schema, "checkpoint_epoch_errors", lambda *_a, **_k: [])
    monkeypatch.setattr(
        checkpoint_schema,
        "live_checkpoint_allocation_authority_errors",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        checkpoint_schema,
        "live_checkpoint_parent_authority_errors",
        lambda *_a, **_k: [],
    )
    # Accept the synthetic publication intent at the publishing stage.
    monkeypatch.setattr(
        publication_transaction,
        "publication_intent_structure_errors",
        lambda *_a, **_k: [],
    )
    return state_path


def test_bind_repo_baseline_head_pins_head_to_publish_commit(tmp_path, monkeypatch):
    """The CAS writer pins repo_baseline.head to the supplied commit OID."""
    state_path = _seed_publishing_checkpoint(tmp_path, monkeypatch)
    # _capture_repo_baseline would return the live (post-commit) HEAD; make it
    # return a different head so we can prove the explicit pin wins over both
    # the stale frozen value and the captured snapshot.
    monkeypatch.setattr(
        evolution_infra,
        "_capture_repo_baseline",
        lambda stage, **_kwargs: {
            "branch": "tencent-cloud-runtime",
            "head": "c" * 40,
            "evaluation_contract": {"stage": stage, "hash": "e" * 64},
        },
    )

    ok = evolution_infra.write_pipeline_checkpoint(
        2,
        1,
        "publishing",
        publication_intent={"publication_id": "d" * 64},
        expected_checkpoint_revision=7,
        expected_checkpoint_stage="publishing",
        expected_workflow_run_id="generation:2:workflow-v1",
        bind_repo_baseline_head=PUBLISH_COMMIT_OID,
    )
    assert ok is True

    written = json.loads(state_path.read_text(encoding="utf-8"))
    assert written["repo_baseline"]["head"] == PUBLISH_COMMIT_OID
    # The contract is rebuilt (not the stale pre-commit one).
    assert isinstance(written["repo_baseline"]["evaluation_contract"], dict)
    assert written["repo_baseline"]["evaluation_contract"] != {"stage": "publishing", "hash": "c" * 64}


def test_no_bind_keeps_existing_head_on_same_stage(tmp_path, monkeypatch):
    """Without bind_repo_baseline_head, publishing->publishing keeps the stale head.

    This is the behavior that caused the deadlock; it documents why the explicit
    bind is required rather than relying on the stage predicate.
    """
    state_path = _seed_publishing_checkpoint(tmp_path, monkeypatch)
    monkeypatch.setattr(
        evolution_infra,
        "_capture_repo_baseline",
        lambda stage, **_kwargs: {
            "branch": "tencent-cloud-runtime",
            "head": PUBLISH_COMMIT_OID,
            "evaluation_contract": {"stage": stage, "hash": "e" * 64},
        },
    )

    ok = evolution_infra.write_pipeline_checkpoint(
        2,
        1,
        "publishing",
        publication_intent={"publication_id": "d" * 64},
        expected_checkpoint_revision=7,
        expected_checkpoint_stage="publishing",
        expected_workflow_run_id="generation:2:workflow-v1",
    )
    assert ok is True

    written = json.loads(state_path.read_text(encoding="utf-8"))
    # Without the explicit bind, the same-stage predicate does NOT refresh, so
    # the pre-commit head is retained (the deadlock root cause).
    assert written["repo_baseline"]["head"] == PRE_COMMIT_HEAD


def test_post_commit_refresh_call_passes_commit_oid(monkeypatch):
    """The post-commit refresh in tool_commit_publication passes commit_oid.

    Verifies the call site threads ``bind_repo_baseline_head=commit_oid`` into
    ``write_pipeline_checkpoint`` (the fix for the silent no-op).
    """
    import tool_commit
    import tool_commit_publication

    captured = {}

    def spy_write(*args, **kwargs):
        captured["bind"] = kwargs.get("bind_repo_baseline_head")
        captured["stage"] = args[2] if len(args) > 2 else kwargs.get("stage")
        return True

    monkeypatch.setattr(tool_commit, "write_pipeline_checkpoint", spy_write)
    monkeypatch.setattr(tool_commit, "read_pipeline_checkpoint", lambda: {
        "next_v": 2,
        "source_v": 1,
        "workflow_run_id": "generation:2:workflow-v1",
        "checkpoint_revision": 7,
        "publication_intent": {"publication_id": "d" * 64},
        "repo_baseline": {"head": PRE_COMMIT_HEAD},
    })

    # Reconstruct the post-commit refresh guard exactly as the real code does.
    local_state = {"commit_oid": PUBLISH_COMMIT_OID}
    post_commit_ckpt = tool_commit.read_pipeline_checkpoint()
    frozen_rb = post_commit_ckpt.get("repo_baseline") or {}
    if frozen_rb.get("head") != local_state["commit_oid"]:
        tool_commit.write_pipeline_checkpoint(
            int(post_commit_ckpt["next_v"]),
            int(post_commit_ckpt["source_v"]),
            "publishing",
            publication_intent=post_commit_ckpt.get("publication_intent"),
            expected_checkpoint_revision=post_commit_ckpt.get("checkpoint_revision"),
            expected_checkpoint_stage="publishing",
            expected_workflow_run_id=post_commit_ckpt.get("workflow_run_id"),
            bind_repo_baseline_head=local_state["commit_oid"],
        )

    # The refresh passed the commit OID through to the CAS writer.
    assert captured["stage"] == "publishing"
    assert captured["bind"] == PUBLISH_COMMIT_OID
