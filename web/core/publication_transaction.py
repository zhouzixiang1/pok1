"""Crash-safe publication intent for one national-native generation.

The official certificate, gate ledger, Git commit, annotated completion tag,
remote refs, and local ``.completed`` cache form one publication transaction.
This module owns the immutable intent and its live content checks.  Git effects
remain in :mod:`evolution_infra`, while checkpoint persistence remains in the
pipeline state writer.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

from bot_namespace import bot_name, bot_tag, format_version


PUBLICATION_INTENT_SCHEMA_VERSION = 2
PUBLICATION_INTENT_KIND = "national-native-publication-intent"
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def canonical_digest(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def publication_gate_ledger_digest(ledger: dict[str, Any]) -> str:
    """Digest semantic gate evidence while allowing verified -> publishing."""

    return canonical_digest({
        key: value
        for key, value in dict(ledger or {}).items()
        if key != "checkpoint_stage"
    })


def bot_commit_message(
    version: int,
    source_v: int,
    strategy_tag: str,
    *,
    rating_info: str = "",
    parent2_v: int | None = None,
) -> str:
    parent_line = f"parent: {bot_name(source_v)}"
    if parent2_v is not None:
        parent_line += f"\nparent2: {bot_name(parent2_v)}"
    return (
        f"evolve: v{source_v} → v{version}\n\n"
        f"{parent_line}\n"
        f"strategy: {strategy_tag}\n"
        f"{rating_info}"
    )


def publication_commit_message(
    version: int,
    source_v: int,
    strategy_tag: str,
    *,
    certificate_digest: str,
    candidate_hash: str,
    policy_id: str,
    rating_info: str = "",
    parent2_v: int | None = None,
) -> str:
    return bot_commit_message(
        version,
        source_v,
        strategy_tag,
        rating_info=rating_info,
        parent2_v=parent2_v,
    ) + (
        f"\nofficial-certificate: {certificate_digest}"
        f"\nofficial-candidate-hash: {candidate_hash}"
        f"\nofficial-policy: {policy_id}"
    )


def publication_tag_message(
    version: int,
    strategy_tag: str,
    *,
    certificate_digest: str,
    candidate_hash: str,
    policy_id: str,
) -> str:
    return (
        f"National bot v{format_version(version)}: {strategy_tag}\n\n"
        f"official-certificate: {certificate_digest}\n"
        f"official-candidate-hash: {candidate_hash}\n"
        f"official-policy: {policy_id}"
    )


def build_publication_intent(
    *,
    checkpoint: dict[str, Any],
    candidate_artifact_hash: str,
    certificate_digest: str,
    certificate_policy_id: str,
    official_status: dict[str, Any],
    certificate_relative_path: str,
    certificate_file_sha256: str,
    certificate_attestation_digest: str,
    final_gate_ledger_digest: str,
    strategy_tag: str,
    rating_info: str,
    baseline_head: str,
    baseline_remote_main: str,
    baseline_remote_completion_refs: dict[str, str],
    prepublication_strict_bots: Iterable[str],
    remote_publication_required: bool,
    remote_publication_enabled: bool,
) -> dict[str, Any]:
    """Build the immutable record persisted before the first Git mutation."""

    version = int(checkpoint["next_v"])
    source_v = int(checkpoint["source_v"])
    parent2_raw = checkpoint.get("parent2_v")
    parent2_v = int(parent2_raw) if parent2_raw is not None else None
    strategy_tag = str(strategy_tag)
    rating_info = str(rating_info)
    commit_message = publication_commit_message(
        version,
        source_v,
        strategy_tag,
        certificate_digest=certificate_digest,
        candidate_hash=candidate_artifact_hash,
        policy_id=certificate_policy_id,
        rating_info=rating_info,
        parent2_v=parent2_v,
    )
    tag_message = publication_tag_message(
        version,
        strategy_tag,
        certificate_digest=certificate_digest,
        candidate_hash=candidate_artifact_hash,
        policy_id=certificate_policy_id,
    )
    payload = {
        "schema_version": PUBLICATION_INTENT_SCHEMA_VERSION,
        "kind": PUBLICATION_INTENT_KIND,
        "bot": bot_name(version),
        "version": version,
        "source_v": source_v,
        "parent2_v": parent2_v,
        "workflow_run_id": str(checkpoint.get("workflow_run_id") or ""),
        "origin_checkpoint_revision": int(
            checkpoint.get("checkpoint_revision") or 0
        ),
        "origin_checkpoint_stage": str(checkpoint.get("stage") or ""),
        "candidate_artifact_hash": str(candidate_artifact_hash),
        "official_certificate_digest": str(certificate_digest),
        "official_policy_id": str(certificate_policy_id),
        "official_status_digest": canonical_digest(official_status),
        "certificate_relative_path": str(certificate_relative_path),
        "certificate_file_sha256": str(certificate_file_sha256),
        "certificate_attestation_digest": str(certificate_attestation_digest),
        "final_gate_ledger_digest": str(final_gate_ledger_digest),
        "strategy_tag": strategy_tag,
        "rating_info": rating_info,
        "commit_message": commit_message,
        "commit_message_sha256": hashlib.sha256(
            commit_message.encode("utf-8")
        ).hexdigest(),
        "tag_message": tag_message,
        "tag_message_sha256": hashlib.sha256(
            tag_message.encode("utf-8")
        ).hexdigest(),
        "completion_tag": bot_tag(version),
        "high_water_tag": f"national-high-water-v{version}",
        "prepublication_strict_bots": sorted(
            {str(item) for item in prepublication_strict_bots}
        ),
        "baseline_head": str(baseline_head),
        "baseline_remote_main": str(baseline_remote_main),
        "baseline_remote_completion_refs": dict(
            sorted((baseline_remote_completion_refs or {}).items())
        ),
        "remote_publication_required": bool(remote_publication_required),
        "remote_publication_enabled": bool(remote_publication_enabled),
    }
    return {**payload, "publication_id": canonical_digest(payload)}


def publication_intent_structure_errors(intent: Any) -> list[str]:
    if not isinstance(intent, dict):
        return ["publication_intent_missing_or_not_object"]
    errors: list[str] = []
    unsigned = {key: value for key, value in intent.items() if key != "publication_id"}
    if intent.get("schema_version") != PUBLICATION_INTENT_SCHEMA_VERSION:
        errors.append("publication_intent_schema_mismatch")
    if intent.get("kind") != PUBLICATION_INTENT_KIND:
        errors.append("publication_intent_kind_mismatch")
    try:
        expected_digest = canonical_digest(unsigned)
    except Exception as exc:
        errors.append(
            f"publication_intent_digest_error:{type(exc).__name__}"
        )
        expected_digest = ""
    if intent.get("publication_id") != expected_digest:
        errors.append("publication_intent_digest_mismatch")
    try:
        version = int(intent.get("version"))
        source_v = int(intent.get("source_v"))
    except (TypeError, ValueError):
        errors.append("publication_intent_version_identity_invalid")
        version = -1
        source_v = -1
    if version < 1 or source_v < 0:
        errors.append("publication_intent_version_identity_invalid")
    if intent.get("bot") != bot_name(version):
        errors.append("publication_intent_bot_mismatch")
    if intent.get("completion_tag") != bot_tag(version):
        errors.append("publication_intent_completion_tag_mismatch")
    if intent.get("high_water_tag") != f"national-high-water-v{version}":
        errors.append("publication_intent_high_water_tag_mismatch")
    for field in (
        "candidate_artifact_hash",
        "official_certificate_digest",
        "official_policy_id",
        "official_status_digest",
        "certificate_file_sha256",
        "certificate_attestation_digest",
        "final_gate_ledger_digest",
        "commit_message_sha256",
        "tag_message_sha256",
    ):
        value = str(intent.get(field) or "")
        if field == "official_policy_id":
            if not value:
                errors.append(f"publication_intent_{field}_missing")
        elif not _HEX64.fullmatch(value):
            errors.append(f"publication_intent_{field}_invalid")
    for field in ("baseline_head", "baseline_remote_main"):
        value = str(intent.get(field) or "")
        if value and not _HEX40.fullmatch(value):
            errors.append(f"publication_intent_{field}_invalid")
    if not _HEX40.fullmatch(str(intent.get("baseline_head") or "")):
        errors.append("publication_intent_baseline_head_missing")
    remote_completion_refs = intent.get("baseline_remote_completion_refs")
    if not isinstance(remote_completion_refs, dict):
        errors.append("publication_intent_remote_completion_refs_invalid")
    else:
        if any(not isinstance(ref, str) for ref in remote_completion_refs):
            errors.append("publication_intent_remote_completion_refs_invalid")
            ordered_remote_refs = []
        else:
            ordered_remote_refs = sorted(remote_completion_refs)
        if list(remote_completion_refs) != ordered_remote_refs:
            errors.append("publication_intent_remote_completion_refs_not_canonical")
        for ref, oid in remote_completion_refs.items():
            base_ref = str(ref).removesuffix("^{}")
            if (
                not str(ref).startswith("refs/tags/national-bot-v")
                or not base_ref[len("refs/tags/national-bot-v"):].isdigit()
                or not _HEX40.fullmatch(str(oid or ""))
            ):
                errors.append("publication_intent_remote_completion_ref_invalid")
                break
    relative = str(intent.get("certificate_relative_path") or "")
    if (
        not relative
        or relative.startswith("/")
        or ".." in Path(relative).parts
        or relative != f"official_certificates/{bot_name(version)}.json"
    ):
        errors.append("publication_intent_certificate_path_invalid")
    if not str(intent.get("workflow_run_id") or ""):
        errors.append("publication_intent_workflow_run_id_missing")
    try:
        if int(intent.get("origin_checkpoint_revision") or 0) < 1:
            errors.append("publication_intent_origin_revision_invalid")
    except (TypeError, ValueError):
        errors.append("publication_intent_origin_revision_invalid")
    if intent.get("origin_checkpoint_stage") not in {
        "verified",
        "official_certifying",
    }:
        errors.append("publication_intent_origin_stage_invalid")
    if not isinstance(intent.get("prepublication_strict_bots"), list):
        errors.append("publication_intent_strict_pool_invalid")
    else:
        pool = intent.get("prepublication_strict_bots") or []
        if pool != sorted(set(str(item) for item in pool)):
            errors.append("publication_intent_strict_pool_not_canonical")
    if intent.get("remote_publication_required") not in {True, False}:
        errors.append("publication_intent_remote_required_invalid")
    if intent.get("remote_publication_enabled") not in {True, False}:
        errors.append("publication_intent_remote_enabled_invalid")
    commit_message = str(intent.get("commit_message") or "")
    tag_message = str(intent.get("tag_message") or "")
    if hashlib.sha256(commit_message.encode("utf-8")).hexdigest() != intent.get(
        "commit_message_sha256"
    ):
        errors.append("publication_intent_commit_message_digest_mismatch")
    if hashlib.sha256(tag_message.encode("utf-8")).hexdigest() != intent.get(
        "tag_message_sha256"
    ):
        errors.append("publication_intent_tag_message_digest_mismatch")
    return list(dict.fromkeys(errors))


def publication_intent_checkpoint_errors(
    intent: Any,
    checkpoint: Any,
) -> list[str]:
    errors = publication_intent_structure_errors(intent)
    if not isinstance(intent, dict) or not isinstance(checkpoint, dict):
        if not isinstance(checkpoint, dict):
            errors.append("publication_intent_checkpoint_missing")
        return list(dict.fromkeys(errors))
    if checkpoint.get("publication_intent") != intent:
        errors.append("publication_intent_checkpoint_projection_mismatch")
    if checkpoint.get("stage") != "publishing":
        errors.append("publication_intent_checkpoint_stage_mismatch")
    identity_fields = {
        "version": checkpoint.get("next_v"),
        "source_v": checkpoint.get("source_v"),
        "parent2_v": checkpoint.get("parent2_v"),
        "workflow_run_id": checkpoint.get("workflow_run_id"),
    }
    for field, current in identity_fields.items():
        if intent.get(field) != current:
            errors.append(f"publication_intent_checkpoint_{field}_mismatch")
    try:
        current_revision = int(checkpoint.get("checkpoint_revision") or 0)
        origin_revision = int(intent.get("origin_checkpoint_revision") or 0)
        if current_revision < origin_revision + 1:
            errors.append("publication_intent_checkpoint_revision_regressed")
    except (TypeError, ValueError):
        errors.append("publication_intent_checkpoint_revision_invalid")
    return list(dict.fromkeys(errors))


def publication_intent_live_errors(
    intent: Any,
    *,
    checkpoint: dict[str, Any],
    candidate_dir: str | Path,
    repo_root: str | Path,
    official_status: dict[str, Any],
    final_gate_ledger_digest: str,
    current_strict_bots: Iterable[str],
    current_remote_required: bool,
) -> list[str]:
    """Rebind an intent to current files, checkpoint evidence, and pool state."""

    errors = publication_intent_checkpoint_errors(intent, checkpoint)
    if not isinstance(intent, dict):
        return errors
    try:
        from bot_artifact import hash_path

        if hash_path(candidate_dir) != intent.get("candidate_artifact_hash"):
            errors.append("publication_intent_candidate_artifact_drift")
    except Exception as exc:
        errors.append(
            f"publication_intent_candidate_hash_error:{type(exc).__name__}"
        )
    if canonical_digest(official_status) != intent.get("official_status_digest"):
        errors.append("publication_intent_official_status_drift")
    if final_gate_ledger_digest != intent.get("final_gate_ledger_digest"):
        errors.append("publication_intent_final_ledger_drift")
    certificate = Path(repo_root) / str(
        intent.get("certificate_relative_path") or ""
    )
    try:
        if file_sha256(certificate) != intent.get("certificate_file_sha256"):
            errors.append("publication_intent_certificate_file_drift")
        payload = json.loads(certificate.read_text(encoding="utf-8"))
        if payload.get("attestation_digest") != intent.get(
            "certificate_attestation_digest"
        ):
            errors.append("publication_intent_attestation_digest_drift")
        if payload.get("certificate_digest") != intent.get(
            "official_certificate_digest"
        ):
            errors.append("publication_intent_certificate_digest_drift")
    except Exception as exc:
        errors.append(
            f"publication_intent_certificate_read_error:{type(exc).__name__}"
        )
    current_pool = sorted({str(item) for item in current_strict_bots})
    candidate = str(intent.get("bot") or "")
    without_candidate = [item for item in current_pool if item != candidate]
    if without_candidate != intent.get("prepublication_strict_bots"):
        errors.append("publication_intent_strict_pool_drift")
    if current_remote_required and not intent.get("remote_publication_required"):
        errors.append("publication_intent_remote_requirement_weakened")
    return list(dict.fromkeys(errors))


__all__ = [
    "PUBLICATION_INTENT_KIND",
    "PUBLICATION_INTENT_SCHEMA_VERSION",
    "bot_commit_message",
    "build_publication_intent",
    "canonical_digest",
    "file_sha256",
    "publication_commit_message",
    "publication_gate_ledger_digest",
    "publication_intent_checkpoint_errors",
    "publication_intent_live_errors",
    "publication_intent_structure_errors",
    "publication_tag_message",
]
