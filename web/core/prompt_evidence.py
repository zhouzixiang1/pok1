"""Digest-bound prompt evidence policy for one evolution generation.

The national protocol bootstrap deliberately has no admissible historical
strength population.  Historically each LLM role implemented that rule on its
own, which left several side doors (eval rounds in the outer orchestrator,
Worker failure memory, Critic calibration, and Archivist sidecars).  This
module is the single system-owned handoff used by every prompt producer.

The envelope contains *content*, not paths.  In bootstrap mode every historical
section is empty and consumers must not fall back to a live/global sidecar.
The digest makes the empty policy safe to copy through checkpoints, durable
Worker envelopes, and the post-commit archive handoff.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any


PROMPT_EVIDENCE_SCHEMA_VERSION = "prompt-evidence-v1"
PROTOCOL_BOOTSTRAP_NO_STRENGTH = "protocol_bootstrap_no_strength"

# Keep the section names explicit.  Adding a new historical prompt input must
# update this registry instead of silently creating another global sidecar.
PROMPT_EVIDENCE_SECTIONS = (
    "strength",
    "lessons",
    "failures",
    "guardian",
    "eval_rounds",
    "spotlight",
    "official_feedback",
    "critic_calibration",
    "generation_history",
    "battle_experience",
    "action_stats",
    "opponent_profiles",
    "exploitability",
)


def _canonical_digest(payload: dict[str, Any]) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _payload(envelope: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": envelope.get("schema_version"),
        "policy": envelope.get("policy"),
        "next_v": envelope.get("next_v"),
        "source_v": envelope.get("source_v"),
        "protocol_bootstrap_receipt_digest": envelope.get(
            "protocol_bootstrap_receipt_digest"
        ),
        "sections": envelope.get("sections"),
    }


def build_protocol_bootstrap_prompt_evidence(
    *,
    next_v: int,
    source_v: int,
    protocol_bootstrap_receipt: dict[str, Any],
) -> dict[str, Any]:
    """Build the immutable no-history envelope for a bootstrap generation."""

    receipt_digest = str(
        (protocol_bootstrap_receipt or {}).get("receipt_digest") or ""
    )
    payload = {
        "schema_version": PROMPT_EVIDENCE_SCHEMA_VERSION,
        "policy": PROTOCOL_BOOTSTRAP_NO_STRENGTH,
        "next_v": int(next_v),
        "source_v": int(source_v),
        "protocol_bootstrap_receipt_digest": receipt_digest,
        "sections": {name: "" for name in PROMPT_EVIDENCE_SECTIONS},
    }
    return {**payload, "envelope_digest": _canonical_digest(payload)}


def validate_prompt_evidence_envelope(
    envelope: dict[str, Any] | None,
    *,
    next_v: int | None = None,
    source_v: int | None = None,
    protocol_bootstrap_receipt: dict[str, Any] | None = None,
) -> list[str]:
    """Validate a persisted envelope without reading any evidence source."""

    if not isinstance(envelope, dict):
        return ["prompt_evidence_missing_or_not_object"]
    errors: list[str] = []
    expected_keys = {
        "schema_version",
        "policy",
        "next_v",
        "source_v",
        "protocol_bootstrap_receipt_digest",
        "sections",
        "envelope_digest",
    }
    unknown = sorted(set(envelope).difference(expected_keys))
    missing = sorted(expected_keys.difference(envelope))
    if unknown:
        errors.append(f"prompt_evidence_unknown_fields:{unknown}")
    if missing:
        errors.append(f"prompt_evidence_missing_fields:{missing}")
    if envelope.get("schema_version") != PROMPT_EVIDENCE_SCHEMA_VERSION:
        errors.append("prompt_evidence_schema_mismatch")
    if envelope.get("policy") != PROTOCOL_BOOTSTRAP_NO_STRENGTH:
        errors.append("prompt_evidence_policy_mismatch")
    for field in ("next_v", "source_v"):
        value = envelope.get(field)
        if not isinstance(value, int) or isinstance(value, bool):
            errors.append(f"prompt_evidence_{field}_not_integer")
    sections = envelope.get("sections")
    if not isinstance(sections, dict):
        errors.append("prompt_evidence_sections_not_object")
    else:
        if set(sections) != set(PROMPT_EVIDENCE_SECTIONS):
            errors.append("prompt_evidence_sections_shape_mismatch")
        for name in PROMPT_EVIDENCE_SECTIONS:
            if sections.get(name) != "":
                errors.append(f"prompt_evidence_bootstrap_section_not_empty:{name}")
    if next_v is not None and envelope.get("next_v") != int(next_v):
        errors.append("prompt_evidence_next_v_mismatch")
    if source_v is not None and envelope.get("source_v") != int(source_v):
        errors.append("prompt_evidence_source_v_mismatch")
    if protocol_bootstrap_receipt is not None:
        expected_receipt = str(
            protocol_bootstrap_receipt.get("receipt_digest") or ""
        )
        if envelope.get("protocol_bootstrap_receipt_digest") != expected_receipt:
            errors.append("prompt_evidence_bootstrap_receipt_mismatch")
    if not errors and envelope.get("envelope_digest") != _canonical_digest(
        _payload(envelope)
    ):
        errors.append("prompt_evidence_digest_mismatch")
    return errors


def prompt_evidence_from_checkpoint(
    checkpoint: dict[str, Any] | None,
    *,
    next_v: int | None = None,
    source_v: int | None = None,
) -> dict[str, Any] | None:
    """Resolve the frozen prompt policy from a checkpoint.

    Normal generations return ``None`` and retain their existing evidence
    pipeline.  A bootstrap checkpoint always returns a validated empty
    envelope.  Older active bootstrap checkpoints that predate this field are
    converted *in memory* to the same empty envelope; this is deliberately a
    fail-closed compatibility path and never reads a global sidecar.
    """

    if not isinstance(checkpoint, dict):
        return None
    audit_context = checkpoint.get("audit_context") or {}
    if not isinstance(audit_context, dict):
        return None
    receipt = audit_context.get("protocol_bootstrap")
    if not isinstance(receipt, dict):
        return None
    if next_v is not None and checkpoint.get("next_v") != int(next_v):
        return None
    if source_v is not None and checkpoint.get("source_v") != int(source_v):
        return None
    resolved_next_v = int(
        checkpoint.get("next_v") if next_v is None else next_v
    )
    resolved_source_v = int(
        checkpoint.get("source_v") if source_v is None else source_v
    )
    stored = audit_context.get("prompt_evidence")
    if not validate_prompt_evidence_envelope(
        stored,
        next_v=resolved_next_v,
        source_v=resolved_source_v,
        protocol_bootstrap_receipt=receipt,
    ):
        return deepcopy(stored)
    return build_protocol_bootstrap_prompt_evidence(
        next_v=resolved_next_v,
        source_v=resolved_source_v,
        protocol_bootstrap_receipt=receipt,
    )


def resolve_prompt_evidence(
    *,
    envelope: dict[str, Any] | None = None,
    checkpoint: dict[str, Any] | None = None,
    next_v: int | None = None,
    source_v: int | None = None,
    protocol_bootstrap_receipt: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Resolve an explicit or checkpoint-owned envelope, failing closed.

    Explicit invalid bootstrap input is replaced by a deterministic empty
    envelope.  It is never repaired from live ratings, lessons, logs, replay
    manifests, certification statuses, or other mutable files.
    """

    if isinstance(checkpoint, dict):
        resolved = prompt_evidence_from_checkpoint(
            checkpoint,
            next_v=next_v,
            source_v=source_v,
        )
        if resolved is not None:
            return resolved
    if isinstance(envelope, dict):
        envelope_errors = validate_prompt_evidence_envelope(
            envelope,
            next_v=next_v,
            source_v=source_v,
            protocol_bootstrap_receipt=protocol_bootstrap_receipt,
        )
        if not envelope_errors:
            return deepcopy(envelope)
        if envelope.get("policy") == PROTOCOL_BOOTSTRAP_NO_STRENGTH:
            resolved_next_v = next_v if next_v is not None else envelope.get("next_v")
            resolved_source_v = (
                source_v if source_v is not None else envelope.get("source_v")
            )
            if resolved_next_v is None or resolved_source_v is None:
                raise ValueError("invalid bootstrap prompt evidence lacks identity")
            receipt = protocol_bootstrap_receipt or {
                "receipt_digest": envelope.get(
                    "protocol_bootstrap_receipt_digest"
                )
            }
            return build_protocol_bootstrap_prompt_evidence(
                next_v=int(resolved_next_v),
                source_v=int(resolved_source_v),
                protocol_bootstrap_receipt=receipt,
            )
    if isinstance(protocol_bootstrap_receipt, dict):
        if next_v is None or source_v is None:
            raise ValueError(
                "bootstrap prompt evidence requires next_v and source_v"
            )
        return build_protocol_bootstrap_prompt_evidence(
            next_v=int(next_v),
            source_v=int(source_v),
            protocol_bootstrap_receipt=protocol_bootstrap_receipt,
        )
    return None


def is_protocol_bootstrap_prompt_evidence(
    envelope: dict[str, Any] | None,
) -> bool:
    return bool(
        isinstance(envelope, dict)
        and envelope.get("policy") == PROTOCOL_BOOTSTRAP_NO_STRENGTH
        and not validate_prompt_evidence_envelope(envelope)
    )


def bootstrap_prompt_policy_text(envelope: dict[str, Any]) -> str:
    """Render a bounded role instruction containing no historical content."""

    if not is_protocol_bootstrap_prompt_evidence(envelope):
        return ""
    return (
        "PROTOCOL BOOTSTRAP PROMPT EVIDENCE POLICY: historical strength, "
        "ratings/H2H, lessons, Worker failures, guardian diagnoses, eval "
        "rounds, replay spotlight, official-certification prose, calibration, "
        "generation history, battle memory, action profiles, opponent profiles, "
        "and exploitability sidecars are intentionally empty. Do not read or "
        "reconstruct them from global files. Use only the current candidate "
        "code, system-owned protocol/runtime contracts, typed strategy "
        "references, and current gate inputs. "
        f"envelope={envelope.get('envelope_digest')}"
    )


__all__ = [
    "PROMPT_EVIDENCE_SCHEMA_VERSION",
    "PROMPT_EVIDENCE_SECTIONS",
    "PROTOCOL_BOOTSTRAP_NO_STRENGTH",
    "bootstrap_prompt_policy_text",
    "build_protocol_bootstrap_prompt_evidence",
    "is_protocol_bootstrap_prompt_evidence",
    "prompt_evidence_from_checkpoint",
    "resolve_prompt_evidence",
    "validate_prompt_evidence_envelope",
]
