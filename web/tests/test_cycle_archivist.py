from __future__ import annotations

import pytest


def _snapshot() -> dict:
    return {
        "evaluation_epoch": "national_tcp_policy_v1",
        "version": 143,
        "source_v": 142,
        "bot_name": "national_v143",
        "git_tag": "national-bot-v143",
        "strength_evidence_identity": {
            "schema_version": 1,
            "mode": "empty_first_strict_bootstrap",
            "strength_evidence_admitted": False,
            "reason": "strict_policy_pool_empty",
        },
        "post_commit_archivist_receipt": {
            "status": "consumed",
            "version": 143,
            "source_v": 142,
            "bot_tag": "national-bot-v143",
            "artifact_hash": "a" * 64,
            "receipt_digest": "b" * 64,
        },
    }


def test_cycle_archivist_rejects_unbound_strength_evidence():
    from cycle_archivist import snapshot_identity_errors

    snapshot = _snapshot()
    snapshot.pop("strength_evidence_identity")
    assert snapshot_identity_errors(snapshot, version=143, source_v=142) == [
        "cycle_archivist_strength_evidence_identity_missing"
    ]


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
    result = await cycle_archivist.run_cycle_archivist_analysis(
        143,
        142,
        _snapshot(),
        object(),
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
