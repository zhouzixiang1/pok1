"""Content-bound post-commit annotations for the strict TCP policy epoch.

Archivist output is stored only beside the exact committed archive snapshot. It
is an operator-facing annotation, not strategy evidence, and is never copied
into prompts. There is no active lesson, experience, or replay-memory store;
this annotation must never be promoted into one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bot_artifact import canonical_digest
from bot_namespace import EVALUATION_EPOCH, bot_name, bot_tag
from evolution_infra import get_logs_dir, run_claude_query


ARCHIVIST_SCHEMA_VERSION = 1
ARCHIVIST_KIND = "national-tcp-policy-cycle-annotation"

_PROMPT_PROJECTION_KEYS = frozenset({
    "schema_version",
    "kind",
    "evaluation_epoch",
    "version",
    "source_v",
    "bot_name",
    "git_tag",
    "publication",
    "strength_evidence_identity",
    "gate_summary",
    "post_publication_handoff",
})


def _cycle_archivist_prompt_projection(
    snapshot: dict[str, Any],
    *,
    version: int,
    source_v: int,
) -> dict[str, Any]:
    """Return the only archive fields an Archivist provider may observe.

    In particular, the publishing checkpoint, selection/replay history,
    reviewer prose, critic prose, runtime logs, and unknown archive extensions
    are deliberately absent.  The strength identity has already been rebuilt
    and compared by the offline validator before this projection is called.
    """

    publication = snapshot.get("publication_identity") or {}
    handoff = snapshot.get("post_publication_handoff") or {}
    projection = {
        "schema_version": 1,
        "kind": "national-policy-cycle-archivist-prompt-projection",
        "evaluation_epoch": snapshot.get("evaluation_epoch"),
        "version": int(version),
        "source_v": int(source_v),
        "bot_name": snapshot.get("bot_name"),
        "git_tag": snapshot.get("git_tag"),
        "publication": {
            "publication_id": publication.get("publication_id"),
            "commit_oid": publication.get("commit_oid"),
            "candidate_artifact_hash": publication.get(
                "candidate_artifact_hash"
            ),
        },
        "strength_evidence_identity": snapshot.get(
            "strength_evidence_identity"
        ),
        "gate_summary": {
            "review_score": snapshot.get("review_score"),
            "critic_score": snapshot.get("critic_score"),
            "precommit_passed": snapshot.get("precommit_passed"),
        },
        "post_publication_handoff": {
            "identity_digest": handoff.get("identity_digest"),
            "publication_id": handoff.get("publication_id"),
        },
    }
    # Keep this assertion adjacent to the producer.  Adding a new archive
    # field can never silently widen the provider boundary.
    if set(projection) != _PROMPT_PROJECTION_KEYS:
        raise ValueError("Cycle Archivist prompt projection contract mismatch")
    return json.loads(json.dumps(
        projection,
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
    ))


def _render_cycle_archivist_provider_prompt(inputs):
    from llm_query import LLMRenderedMaterial

    if not isinstance(inputs, dict) or set(inputs) != {
        "snapshot", "version", "source_v",
    }:
        raise ValueError("Cycle Archivist renderer input contract mismatch")
    snapshot = inputs["snapshot"]
    if not isinstance(snapshot, dict) or set(snapshot) != _PROMPT_PROJECTION_KEYS:
        raise ValueError("Cycle Archivist snapshot projection mismatch")
    snapshot_json = json.dumps(snapshot, indent=2, ensure_ascii=False)

    template = (
        Path(__file__).resolve().parent / "prompts" / "cycle_archivist.md"
    ).read_text(encoding="utf-8")
    return LLMRenderedMaterial(
        text=template.replace("{snapshot}", snapshot_json),
        evidence_kind="content_bound_cycle_snapshot",
        evidence_provenance={
            "version": int(inputs["version"]),
            "source_v": int(inputs["source_v"]),
            "snapshot_digest": canonical_digest(snapshot),
        },
    )


def snapshot_identity_errors(
    snapshot: Any,
    *,
    version: int,
    source_v: int,
) -> list[str]:
    if not isinstance(snapshot, dict):
        return ["cycle_archivist_snapshot_missing"]
    errors: list[str] = []
    if snapshot.get("evaluation_epoch") != EVALUATION_EPOCH:
        errors.append("cycle_archivist_epoch_mismatch")
    if snapshot.get("version") != version or snapshot.get("source_v") != source_v:
        errors.append("cycle_archivist_version_binding_mismatch")
    if snapshot.get("bot_name") != bot_name(version):
        errors.append("cycle_archivist_bot_name_mismatch")
    if snapshot.get("git_tag") != bot_tag(version):
        errors.append("cycle_archivist_tag_mismatch")
    handoff = snapshot.get("post_publication_handoff")
    publication = snapshot.get("publication_identity")
    if not isinstance(handoff, dict):
        errors.append("cycle_archivist_handoff_missing")
    else:
        for field in ("identity_digest", "publication_id"):
            value = str(handoff.get(field) or "")
            if len(value) != 64 or any(
                char not in "0123456789abcdef" for char in value
            ):
                errors.append(f"cycle_archivist_handoff_{field}_invalid")
    if not isinstance(publication, dict):
        errors.append("cycle_archivist_publication_identity_missing")
    else:
        if publication.get("version") != version or publication.get(
            "source_v"
        ) != source_v:
            errors.append("cycle_archivist_publication_subject_mismatch")
        if publication.get("publication_id") != (handoff or {}).get(
            "publication_id"
        ):
            errors.append("cycle_archivist_publication_handoff_mismatch")
    evidence = snapshot.get("strength_evidence_identity")
    projection = snapshot.get("publishing_checkpoint_projection")
    if not isinstance(evidence, dict):
        errors.append("cycle_archivist_strength_evidence_identity_missing")
    try:
        from generation_evidence import generation_evidence_identity_errors

        errors.extend(generation_evidence_identity_errors(
            evidence,
            projection,
            version=version,
            source_v=source_v,
        ))
    except Exception as exc:
        errors.append(
            f"cycle_archivist_strength_validation_error:{type(exc).__name__}"
        )
    return list(dict.fromkeys(errors))


def _offline_cycle_input_errors(
    snapshot: Any,
    handoff_record: Any,
    *,
    version: int,
    source_v: int,
) -> list[str]:
    """Reopen and validate the complete journal/archive pair before any LLM.

    This validation is intentionally offline: it verifies durable record,
    archive, checkpoint/evidence and receipt bindings without querying a live
    provider.  Live Git/remote publication proof is performed when the journal
    is claimed by the effect executor.
    """

    errors = snapshot_identity_errors(
        snapshot,
        version=version,
        source_v=source_v,
    )
    if not isinstance(handoff_record, dict):
        errors.append("cycle_archivist_handoff_record_missing")
        return list(dict.fromkeys(errors))
    try:
        from post_publication_handoff import (
            load_archive_snapshot,
            validate_handoff_record,
        )

        errors.extend(
            f"cycle_archivist_record:{item}"
            for item in validate_handoff_record(
                handoff_record,
                reopen_archive=True,
            )
        )
        identity = handoff_record.get("identity") or {}
        if (
            identity.get("version") != int(version)
            or identity.get("source_v") != int(source_v)
            or identity.get("publication_id")
            != (snapshot.get("publication_identity") or {}).get(
                "publication_id"
            )
            or handoff_record.get("identity_digest")
            != (snapshot.get("post_publication_handoff") or {}).get(
                "identity_digest"
            )
        ):
            errors.append("cycle_archivist_record_subject_mismatch")
        reopened = load_archive_snapshot(version)
        if reopened != snapshot:
            errors.append("cycle_archivist_archive_changed_after_read")
    except Exception as exc:
        errors.append(
            f"cycle_archivist_offline_validation_error:{type(exc).__name__}"
        )
    return list(dict.fromkeys(errors))


def annotation_identity_errors(
    annotation: Any,
    snapshot: Any,
    *,
    version: int,
    source_v: int,
) -> list[str]:
    """Validate a persisted annotation before accepting crash recovery."""

    if not isinstance(annotation, dict):
        return ["cycle_annotation_not_object"]
    expected_keys = {
        "schema_version", "kind", "subject", "status", "issues",
        "analysis", "annotation_digest",
    }
    errors = []
    if set(annotation) != expected_keys:
        errors.append("cycle_annotation_fields_mismatch")
    unsigned = {
        key: value for key, value in annotation.items()
        if key != "annotation_digest"
    }
    if annotation.get("annotation_digest") != canonical_digest(unsigned):
        errors.append("cycle_annotation_digest_mismatch")
    if annotation.get("schema_version") != ARCHIVIST_SCHEMA_VERSION:
        errors.append("cycle_annotation_schema_mismatch")
    if annotation.get("kind") != ARCHIVIST_KIND:
        errors.append("cycle_annotation_kind_mismatch")
    publication = (snapshot or {}).get("publication_identity") or {}
    expected_subject = {
        "epoch": EVALUATION_EPOCH,
        "version": int(version),
        "source_v": int(source_v),
        "bot": bot_name(version),
        "tag": bot_tag(version),
        "artifact_hash": str(
            publication.get("candidate_artifact_hash") or ""
        ),
        "strength_evidence_identity": (snapshot or {}).get(
            "strength_evidence_identity"
        ),
    }
    if annotation.get("subject") != expected_subject:
        errors.append("cycle_annotation_subject_mismatch")
    if annotation.get("status") != "annotated" or annotation.get("issues") != []:
        errors.append("cycle_annotation_not_annotated")
    analysis = annotation.get("analysis")
    if not isinstance(analysis, dict) or set(analysis) != {
        "generation_assessment", "archive_notes",
    }:
        errors.append("cycle_annotation_analysis_shape_mismatch")
    else:
        if analysis.get("generation_assessment") not in {
            "improvement", "neutral", "regression", "mixed",
        }:
            errors.append("cycle_annotation_assessment_invalid")
        notes = analysis.get("archive_notes")
        if (
            not isinstance(notes, str)
            or not notes.strip()
            or len(notes) > 1200
            or any(ord(char) == 0 for char in notes)
        ):
            errors.append("cycle_annotation_notes_invalid")
    return list(dict.fromkeys(errors))


async def run_cycle_archivist_analysis(
    version: int,
    source_v: int,
    snapshot: dict[str, Any],
    ui: Any,
    *,
    handoff_record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return one identity-bound annotation or a fail-closed error record."""

    errors = _offline_cycle_input_errors(
        snapshot,
        handoff_record,
        version=version,
        source_v=source_v,
    )
    prompt_projection = None
    if not errors:
        try:
            prompt_projection = _cycle_archivist_prompt_projection(
                snapshot,
                version=version,
                source_v=source_v,
            )
        except Exception as exc:
            errors.append(
                f"cycle_archivist_prompt_projection_error:{type(exc).__name__}"
            )
    publication = (
        prompt_projection.get("publication")
        if isinstance(prompt_projection, dict)
        else {}
    )
    subject = {
        "epoch": EVALUATION_EPOCH,
        "version": version,
        "source_v": source_v,
        "bot": bot_name(version),
        "tag": bot_tag(version),
        "artifact_hash": str(publication.get("candidate_artifact_hash") or ""),
        "strength_evidence_identity": (
            prompt_projection or {}
        ).get("strength_evidence_identity"),
    }
    if errors:
        payload = {
            "schema_version": ARCHIVIST_SCHEMA_VERSION,
            "kind": ARCHIVIST_KIND,
            "subject": subject,
            "status": "identity_rejected",
            "issues": errors,
            "analysis": {},
        }
        return {**payload, "annotation_digest": canonical_digest(payload)}

    log_file = get_logs_dir(version) / "cycle_archivist_io.txt"
    try:
        from llm_query import render_llm_prompt

        rendered_prompt = render_llm_prompt(
            "CYCLE ARCHIVIST",
            producer=_render_cycle_archivist_provider_prompt,
            renderer_inputs={
                "snapshot": prompt_projection,
                "version": int(version),
                "source_v": int(source_v),
            },
        )
        output, _, _ = await run_claude_query(
            rendered_prompt,
            [],
            ui,
            "CYCLE ARCHIVIST",
            log_file,
            tools=[],
        )
        from llm_query import parse_json_output_with_mode

        parsed, failure_mode = parse_json_output_with_mode(output)
        if not isinstance(parsed, dict):
            raise ValueError(f"archivist_output_{failure_mode}")
        assessment = str(parsed.get("generation_assessment") or "")
        notes = str(parsed.get("archive_notes") or "").strip()
        if assessment not in {"improvement", "neutral", "regression", "mixed"}:
            raise ValueError("archivist_assessment_invalid")
        evidence = prompt_projection.get("strength_evidence_identity") or {}
        if (
            evidence.get("strength_evidence_admitted") is False
            and assessment != "neutral"
        ):
            raise ValueError("archivist_zero_strength_assessment_must_be_neutral")
        if not notes or len(notes) > 1200:
            raise ValueError("archivist_notes_invalid")
        analysis = {
            "generation_assessment": assessment,
            "archive_notes": notes,
        }
        status = "annotated"
        issues = []
    except Exception as exc:
        from llm_availability import LLMAvailabilityBlocked

        if isinstance(exc, LLMAvailabilityBlocked):
            raise
        analysis = {}
        status = "analysis_unavailable"
        issues = [f"{type(exc).__name__}:{str(exc)[:180]}"]
    payload = {
        "schema_version": ARCHIVIST_SCHEMA_VERSION,
        "kind": ARCHIVIST_KIND,
        "subject": subject,
        "status": status,
        "issues": issues,
        "analysis": analysis,
    }
    return {**payload, "annotation_digest": canonical_digest(payload)}


__all__ = [
    "annotation_identity_errors",
    "run_cycle_archivist_analysis",
    "snapshot_identity_errors",
]
