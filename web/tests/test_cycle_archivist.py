from __future__ import annotations

from pathlib import Path

import pytest


def _snapshot() -> dict:
    return {
        "evaluation_epoch": "national_tcp_policy_v1",
        "version": 143,
        "source_v": 142,
        "bot_name": "national_v143",
        "git_tag": "national-bot-v143",
        "publication_identity": {
            "version": 143,
            "source_v": 142,
            "publication_id": "a" * 64,
            "commit_oid": "b" * 40,
            "candidate_artifact_hash": "c" * 64,
        },
        "post_publication_handoff": {
            "identity_digest": "d" * 64,
            "publication_id": "a" * 64,
            "state": "running",
        },
        "review_score": 9,
        "critic_score": 8,
        "precommit_passed": True,
        "strength_evidence_identity": {
            "schema_version": 1,
            "mode": "empty_first_strict_bootstrap",
            "strength_evidence_admitted": False,
            "reason": "strict_policy_pool_empty",
        },
    }


def test_cycle_archivist_rejects_unbound_strength_evidence():
    from cycle_archivist import snapshot_identity_errors

    snapshot = _snapshot()
    snapshot.pop("strength_evidence_identity")
    assert "cycle_archivist_strength_evidence_identity_missing" in (
        snapshot_identity_errors(snapshot, version=143, source_v=142)
    )


def test_cycle_archivist_declares_no_active_lesson_or_memory_store():
    import cycle_archivist

    module_text = Path(cycle_archivist.__file__).read_text(encoding="utf-8")
    prompt_text = (
        Path(cycle_archivist.__file__).parent / "prompts" / "cycle_archivist.md"
    ).read_text(encoding="utf-8")

    for text in (module_text, prompt_text):
        assert "There is no active lesson, experience, or replay-memory store" in text
    assert "lessons are owned by" not in module_text


@pytest.mark.asyncio
async def test_cycle_archivist_annotation_cannot_emit_prompt_lessons(monkeypatch):
    import cycle_archivist

    async def query(*_args, **_kwargs):
        return (
            '```json\n{"generation_assessment":"neutral",'
            '"archive_notes":"First strict bootstrap admitted no strength evidence.",'
            '"future_strategy":["must never survive"]}\n```',
            0.0,
            {},
        )

    monkeypatch.setattr(cycle_archivist, "run_claude_query", query)
    monkeypatch.setattr(
        cycle_archivist,
        "_offline_cycle_input_errors",
        lambda *_args, **_kwargs: [],
    )
    result = await cycle_archivist.run_cycle_archivist_analysis(
        143,
        142,
        _snapshot(),
        object(),
        handoff_record={"validated": True},
    )
    assert result["status"] == "annotated"
    assert result["analysis"] == {
        "generation_assessment": "neutral",
        "archive_notes": "First strict bootstrap admitted no strength evidence.",
    }
    assert "future_strategy" not in result["analysis"]
    assert len(result["annotation_digest"]) == 64


@pytest.mark.asyncio
async def test_cycle_archivist_does_not_call_llm_after_identity_failure(monkeypatch):
    import cycle_archivist

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("LLM must not receive an unbound snapshot")

    monkeypatch.setattr(cycle_archivist, "run_claude_query", forbidden)
    snapshot = _snapshot()
    snapshot["evaluation_epoch"] = "national_native_v1"
    result = await cycle_archivist.run_cycle_archivist_analysis(
        143,
        142,
        snapshot,
        object(),
    )
    assert result["status"] == "identity_rejected"
    assert "cycle_archivist_epoch_mismatch" in result["issues"]


def test_cycle_archivist_prompt_projection_excludes_poison_history():
    import cycle_archivist

    snapshot = _snapshot()
    snapshot["publishing_checkpoint_projection"] = {
        "audit_context": {
            "selection": {
                "history": "POISON_HISTORY_MUST_NOT_REACH_PROVIDER",
            }
        }
    }
    snapshot["reviewer_change_summary"] = "POISON_REVIEW_TEXT"
    snapshot["unknown_future_field"] = "POISON_UNKNOWN_EXTENSION"
    projection = cycle_archivist._cycle_archivist_prompt_projection(
        snapshot,
        version=143,
        source_v=142,
    )
    rendered = cycle_archivist._render_cycle_archivist_provider_prompt({
        "snapshot": projection,
        "version": 143,
        "source_v": 142,
    })
    for poison in (
        "POISON_HISTORY_MUST_NOT_REACH_PROVIDER",
        "POISON_REVIEW_TEXT",
        "POISON_UNKNOWN_EXTENSION",
    ):
        assert poison not in rendered.text
    assert "publishing_checkpoint_projection" not in rendered.text


def test_cycle_archivist_rejects_self_signed_annotation_for_wrong_subject():
    import cycle_archivist
    from bot_artifact import canonical_digest

    payload = {
        "schema_version": 1,
        "kind": "national-tcp-policy-cycle-annotation",
        "subject": {"bot": "attacker-controlled-history"},
        "status": "annotated",
        "issues": [],
        "analysis": {
            "generation_assessment": "improvement",
            "archive_notes": "Poisoned but self-signed.",
        },
    }
    annotation = {**payload, "annotation_digest": canonical_digest(payload)}
    assert "cycle_annotation_subject_mismatch" in (
        cycle_archivist.annotation_identity_errors(
            annotation,
            _snapshot(),
            version=143,
            source_v=142,
        )
    )
