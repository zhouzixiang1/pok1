"""Content-bound post-commit annotations for the strict TCP policy epoch.

Archivist output is stored only beside the exact committed archive snapshot. It
is not strategy evidence and is never copied into prompts. Cross-generation
lessons are owned by the native replay-memory pipeline, which binds replay and
evaluation identities independently.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bot_artifact import canonical_digest
from bot_namespace import EVALUATION_EPOCH, bot_name, bot_tag
from evolution_infra import PROMPTS_DIR, get_logs_dir, run_claude_query


ARCHIVIST_SCHEMA_VERSION = 1
ARCHIVIST_KIND = "national-tcp-policy-cycle-annotation"


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
    receipt = snapshot.get("post_commit_archivist_receipt")
    if not isinstance(receipt, dict):
        errors.append("cycle_archivist_receipt_missing")
    else:
        if receipt.get("status") != "consumed":
            errors.append("cycle_archivist_receipt_not_consumed")
        if receipt.get("version") != version or receipt.get("source_v") != source_v:
            errors.append("cycle_archivist_receipt_subject_mismatch")
        if receipt.get("bot_tag") != bot_tag(version):
            errors.append("cycle_archivist_receipt_tag_mismatch")
        for field in ("artifact_hash", "receipt_digest"):
            value = str(receipt.get(field) or "")
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                errors.append(f"cycle_archivist_receipt_{field}_invalid")
    evidence = snapshot.get("strength_evidence_identity")
    if not isinstance(evidence, dict):
        errors.append("cycle_archivist_strength_evidence_identity_missing")
    elif evidence.get("mode") == "empty_first_strict_bootstrap":
        if version != 143 or evidence.get("strength_evidence_admitted") is not False:
            errors.append("cycle_archivist_bootstrap_evidence_identity_invalid")
    else:
        if evidence.get("mode") != "frozen_native_evaluation":
            errors.append("cycle_archivist_strength_evidence_mode_invalid")
        for field in (
            "generation_snapshot_manifest_digest",
            "cycle_manifest_digest",
            "h2h_snapshot_manifest_digest",
            "h2h_snapshot_sha256",
            "selection_view_digest",
        ):
            value = str(evidence.get(field) or "")
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                errors.append(f"cycle_archivist_strength_{field}_invalid")
    return list(dict.fromkeys(errors))


async def run_cycle_archivist_analysis(
    version: int,
    source_v: int,
    snapshot: dict[str, Any],
    ui: Any,
) -> dict[str, Any]:
    """Return one identity-bound annotation or a fail-closed error record."""

    errors = snapshot_identity_errors(snapshot, version=version, source_v=source_v)
    subject = {
        "epoch": EVALUATION_EPOCH,
        "version": version,
        "source_v": source_v,
        "bot": bot_name(version),
        "tag": bot_tag(version),
        "artifact_hash": str(
            (snapshot.get("post_commit_archivist_receipt") or {}).get("artifact_hash")
            or ""
        ),
        "strength_evidence_identity": snapshot.get("strength_evidence_identity"),
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

    prompt_path = PROMPTS_DIR / "cycle_archivist.md"
    prompt = prompt_path.read_text(encoding="utf-8").replace(
        "{snapshot}", json.dumps(snapshot, indent=2, ensure_ascii=False)
    )
    log_file = get_logs_dir(version) / "cycle_archivist_io.txt"
    try:
        output, _, _ = await run_claude_query(
            prompt,
            [],
            ui,
            "CYCLE ARCHIVIST",
            log_file,
            tools=["Read"],
        )
        from llm_query import parse_json_output_with_mode

        parsed, failure_mode = parse_json_output_with_mode(output)
        if not isinstance(parsed, dict):
            raise ValueError(f"archivist_output_{failure_mode}")
        assessment = str(parsed.get("generation_assessment") or "")
        notes = str(parsed.get("archive_notes") or "").strip()
        if assessment not in {"improvement", "neutral", "regression", "mixed"}:
            raise ValueError("archivist_assessment_invalid")
        if not notes or len(notes) > 1200:
            raise ValueError("archivist_notes_invalid")
        analysis = {
            "generation_assessment": assessment,
            "archive_notes": notes,
        }
        status = "annotated"
        issues = []
    except Exception as exc:
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


__all__ = ["run_cycle_archivist_analysis", "snapshot_identity_errors"]
