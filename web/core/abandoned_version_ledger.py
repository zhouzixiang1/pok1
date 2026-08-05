"""Abandoned-version receipt ledger.

Extracted from evolution_infra.py as a single business responsibility: the
schema-2 transaction shape for generation abandonment -- receipt construction,
bounded-field validation, checkpoint envelope validation, canonical bytes,
decode/load, authority derivation, identity matching, floor/attempts queries.
Publication-lock append (``append_abandoned_version_receipt``) stays in
evolution_infra because of its heavy publication-lock coupling.

Design (radically simplified 2026-08-05)
----------------------------------------
Abandon receipts are immutable structured records of one abandoned generation:
``{schema_version, kind, evaluation_epoch, version, source_v,
checkpoint_stage, workflow_run_id, checkpoint_revision, checkpoint_envelope,
reason, timestamp, infra_failure}``.  git provides tamper-evidence; the ledger
deliberately does **not** chain per-row digests (no ``receipt_digest`` /
``previous_receipt_digest``) and does **not** re-resolve each historical row's
``checkpoint_envelope.published_parent_identities`` against live git on load.
A legitimate parent re-publish (re-certification, tag rewrite) must not
invalidate historical rows -- that coupling previously wedged the entire ledger
whenever any published parent bot changed.  The live checkpoint being abandoned
still receives the full parent-drift check (``live_checkpoint_parent_authority_
errors``) before it is appended; only already-appended historical rows are
treated as frozen snapshots (``historical_receipt=True`` skips the live checks).

The allocation CAS fingerprint is a holistic sha256 over all current rows
(``_abandoned_ledger_head_digest``), computed at read time -- appending one row
reliably changes the fingerprint the checkpoint binding compares against, with
no fragile per-row chain.  ``abandoned_version_authority()["head_digest"]``
exposes this holistic fingerprint.

All public symbols are re-exported by evolution_infra.py (via thin delegate
shells) for backward compatibility, covering every ``from evolution_infra
import <name>`` site and every ``evolution_infra.<name>`` monkeypatch.

IMPORTANT -- shared-symbol access model
---------------------------------------
Many names referenced by these bodies remain in ``evolution_infra`` because
they are part of that module's monkeypatch surface -- the test suite patches
``evolution_infra.RESULTS_DIR``, ``evolution_infra.PROJECT_ROOT``,
``evolution_infra.ABANDONED_VERSIONS_FILE``, ``evolution_infra.find_current_v``
and reads them back through the abandoned-receipt code paths.  Binding them at
import time would freeze the pre-patch value and silently break the audit.

Every such reference in this file is written ``_ei.<name>`` so it resolves
against the live module attribute at call time.  References between members of
*this* module (e.g. ``_build_abandoned_version_receipt`` calling
``_validate_abandoned_checkpoint``) are written as bare globals, exactly as
they were inline.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import time
from pathlib import Path

import evolution_infra as _ei


def _abandoned_receipt_digest(payload):
    """Canonical sha256 of one receipt payload (legacy single-row digest).

    Retained for backwards-compat with historical rows that carry a per-row
    ``receipt_digest`` field; the live authority no longer chains on it.
    """
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise _ei.AbandonedVersionLedgerError(
            f"abandon receipt is not canonical JSON: {type(exc).__name__}"
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def _abandoned_ledger_head_digest(receipts):
    """Content-addressable fingerprint of the *whole* ledger.

    Replaces the former fragile chain-head digest.  A re-publish of a parent
    bot no longer invalidates historical rows (they are immutable records of
    the abandon-time state, never re-resolved against live git), so the
    authority fingerprint only needs to change when a row is added, removed,
    or mutated -- exactly what a holistic sha256 over the canonical rows
    provides.  ``None`` for an empty ledger preserves the CAS semantics for
    the bootstrap empty-ledger binding.
    """

    if not receipts:
        return None
    try:
        encoded = json.dumps(
            [
                {k: r.get(k) for k in _ei._ABANDONED_VERSION_RECEIPT_KEYS if k in r}
                for r in receipts
            ],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise _ei.AbandonedVersionLedgerError(
            f"abandon ledger is not canonical JSON: {type(exc).__name__}"
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def _abandoned_version_receipt_identity_digest(receipt):
    """Stable 64-char sha256 over a receipt's canonical known-key projection.

    Abandon receipts no longer carry a per-row ``receipt_digest`` field, but
    several handoff/result payloads still want a stable content fingerprint of
    the matched receipt (e.g. to surface ``abandon_receipt_digest`` to the
    orchestrator).  This projects the receipt down to its known keys (ignoring
    any legacy chain fields still present on old rows) and hashes the canonical
    JSON, giving a deterministic identifier that is independent of the
    whole-ledger holistic hash.
    """
    if not isinstance(receipt, dict):
        return None
    projected = {
        k: receipt[k]
        for k in sorted(_ei._ABANDONED_VERSION_RECEIPT_KEYS)
        if k in receipt
    }
    return _abandoned_receipt_digest(projected)


def _canonical_abandon_json_bytes(payload, *, label):
    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise _ei.AbandonedVersionLedgerError(
            f"{label} is not canonical JSON: {type(exc).__name__}"
        ) from exc


def _validate_abandon_receipt_bounded_fields(reason, infra_failure):
    if not isinstance(reason, str) or not reason.strip():
        raise _ei.AbandonedVersionLedgerError("abandon receipt reason is empty")
    if len(reason.encode("utf-8")) > _ei._ABANDONED_VERSION_REASON_MAX_BYTES:
        raise _ei.AbandonedVersionLedgerError("abandon receipt reason exceeds byte limit")
    if infra_failure is not None and not isinstance(infra_failure, dict):
        raise _ei.AbandonedVersionLedgerError(
            "abandon receipt infra_failure must be an object or null"
        )
    if infra_failure is not None and len(
        _canonical_abandon_json_bytes(
            infra_failure,
            label="abandon receipt infra_failure",
        )
    ) > _ei._ABANDONED_VERSION_INFRA_FAILURE_MAX_BYTES:
        raise _ei.AbandonedVersionLedgerError(
            "abandon receipt infra_failure exceeds byte limit"
        )


def _abandoned_checkpoint_envelope(checkpoint):
    checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
    audit = checkpoint.get("audit_context")
    audit = audit if isinstance(audit, dict) else {}
    protocol_bootstrap = audit.get("protocol_bootstrap")
    return {
        "checkpoint_schema_version": checkpoint.get("checkpoint_schema_version"),
        "evaluation_epoch": checkpoint.get("evaluation_epoch"),
        "next_v": checkpoint.get("next_v"),
        "source_v": checkpoint.get("source_v"),
        "parent2_v": checkpoint.get("parent2_v"),
        "generation_mode": checkpoint.get("generation_mode"),
        "epoch_binding": checkpoint.get("epoch_binding"),
        "audit_context": (
            {"protocol_bootstrap": protocol_bootstrap}
            if protocol_bootstrap is not None
            else {}
        ),
    }


def _validate_abandoned_checkpoint(checkpoint, *, project_root, historical_receipt=False):
    """Validate a checkpoint as abandon-bound.

    ``historical_receipt`` distinguishes an immutable historical receipt row
    (already-appended ledger record) from the *live* checkpoint about to be
    abandoned.  Historical rows bind the abandon-time parent identity captured
    in their envelope and must NOT be re-resolved against live git -- a
    legitimate parent re-publication (e.g. re-certification) would otherwise
    invalidate every historical row and wedge the whole ledger.  The live
    checkpoint still receives the full drift check so a stale-binding live
    checkpoint is caught before it is appended.
    """

    from checkpoint_schema import (
        checkpoint_epoch_errors,
        live_checkpoint_parent_authority_errors,
        live_policy_epoch_reset_receipt_errors,
    )

    errors = list(checkpoint_epoch_errors(checkpoint))
    if not errors and not historical_receipt:
        errors.extend(
            live_checkpoint_parent_authority_errors(
                checkpoint,
                repo_root=project_root,
            )
        )
    if not errors and not historical_receipt:
        errors.extend(
            live_policy_epoch_reset_receipt_errors(
                checkpoint,
                project_root=project_root,
            )
        )
    stage = checkpoint.get("stage") if isinstance(checkpoint, dict) else None
    if not isinstance(stage, str) or not stage.strip():
        errors.append("abandon_checkpoint_stage_missing")
    # ``timed_out`` is an active terminalization lease: the only legal next
    # route is the recorded canonical-abandon transaction, which appends this
    # receipt before quarantining bytes and clearing the exact checkpoint.
    # Infra timeouts preserve the candidate for precommit retry, while archived
    # and abandoned states have already crossed their terminal boundary.
    elif stage in {"infra_timed_out", "archived", "abandoned"}:
        errors.append("abandon_checkpoint_stage_not_active")
    workflow_run_id = (
        checkpoint.get("workflow_run_id") if isinstance(checkpoint, dict) else None
    )
    if not isinstance(workflow_run_id, str) or not workflow_run_id.strip():
        errors.append("abandon_checkpoint_workflow_run_id_missing")
    elif checkpoint.get("next_v") == _ei.FIRST_STRICT_POLICY_VERSION and re.fullmatch(
        rf"generation:{int(checkpoint['next_v'])}:workflow-v[1-9][0-9]*",
        workflow_run_id,
    ) is None:
        errors.append("abandon_checkpoint_workflow_run_id_invalid")
    revision = (
        checkpoint.get("checkpoint_revision") if isinstance(checkpoint, dict) else None
    )
    if type(revision) is not int or revision < 1:
        errors.append("abandon_checkpoint_revision_invalid")
    if errors:
        raise _ei.AbandonedVersionLedgerError(
            "abandon checkpoint is not current-epoch bound: "
            + "; ".join(dict.fromkeys(map(str, errors)))
        )


def _build_abandoned_version_receipt(
    checkpoint,
    *,
    reason,
    infra_failure=None,
    timestamp=None,
    previous_receipt_digest=None,  # accepted for backwards-compat, ignored
    project_root=None,
):
    project_root = Path(project_root) if project_root is not None else _ei.PROJECT_ROOT
    _validate_abandoned_checkpoint(checkpoint, project_root=project_root)
    reason = str(reason or "").strip()
    _validate_abandon_receipt_bounded_fields(reason, infra_failure)
    timestamp = time.time() if timestamp is None else timestamp
    if (
        type(timestamp) not in {int, float}
        or timestamp != timestamp
        or timestamp < 0
        or timestamp > 100_000_000_000
    ):
        raise _ei.AbandonedVersionLedgerError("abandon receipt timestamp is invalid")
    return {
        "schema_version": _ei.ABANDONED_VERSION_RECEIPT_SCHEMA_VERSION,
        "kind": _ei.ABANDONED_VERSION_RECEIPT_KIND,
        "evaluation_epoch": _ei.EVALUATION_EPOCH,
        "version": checkpoint["next_v"],
        "source_v": checkpoint["source_v"],
        "checkpoint_stage": checkpoint["stage"],
        "workflow_run_id": checkpoint["workflow_run_id"],
        "checkpoint_revision": checkpoint["checkpoint_revision"],
        "checkpoint_envelope": _abandoned_checkpoint_envelope(checkpoint),
        "reason": reason,
        "timestamp": timestamp,
        "infra_failure": infra_failure,
    }


def _abandoned_version_receipt_errors(
    receipt,
    *,
    expected_previous_digest=None,  # accepted for backwards-compat, ignored
    project_root,
):
    errors = []
    if not isinstance(receipt, dict):
        return ["abandon_receipt_not_object"]
    # Required keys must all be present.  Legacy rows written before the
    # radical simplification additionally carry ``receipt_digest`` /
    # ``previous_receipt_digest``; tolerate those extras so the one-time
    # migration can read an old archive before stripping them.
    receipt_keys = set(receipt)
    missing = _ei._ABANDONED_VERSION_RECEIPT_KEYS - receipt_keys
    unexpected = receipt_keys - _ei._ABANDONED_VERSION_RECEIPT_KEYS - _ei._ABANDONED_VERSION_RECEIPT_LEGACY_EXTRA_KEYS
    if missing:
        errors.append("abandon_receipt_fields_mismatch")
    if unexpected:
        errors.append("abandon_receipt_fields_unexpected")
    if receipt.get("schema_version") != _ei.ABANDONED_VERSION_RECEIPT_SCHEMA_VERSION:
        errors.append("abandon_receipt_schema_mismatch")
    if receipt.get("kind") != _ei.ABANDONED_VERSION_RECEIPT_KIND:
        errors.append("abandon_receipt_kind_mismatch")
    if receipt.get("evaluation_epoch") != _ei.EVALUATION_EPOCH:
        errors.append("abandon_receipt_epoch_mismatch")
    version = receipt.get("version")
    source_v = receipt.get("source_v")
    if type(version) is not int or version < _ei.FIRST_STRICT_POLICY_VERSION:
        errors.append("abandon_receipt_version_invalid")
    if type(source_v) is not int:
        errors.append("abandon_receipt_source_version_invalid")
    timestamp = receipt.get("timestamp")
    if (
        type(timestamp) not in {int, float}
        or timestamp != timestamp
        or timestamp < 0
        or timestamp > 100_000_000_000
    ):
        errors.append("abandon_receipt_timestamp_invalid")
    try:
        _validate_abandon_receipt_bounded_fields(
            receipt.get("reason"),
            receipt.get("infra_failure"),
        )
    except _ei.AbandonedVersionLedgerError as exc:
        errors.append(str(exc).replace(" ", "_"))

    envelope = receipt.get("checkpoint_envelope")
    if not isinstance(envelope, dict):
        errors.append("abandon_receipt_checkpoint_envelope_not_object")
    else:
        if set(envelope) != _ei._ABANDONED_CHECKPOINT_ENVELOPE_KEYS:
            errors.append("abandon_receipt_checkpoint_envelope_fields_mismatch")
        checkpoint = {
            **envelope,
            "stage": receipt.get("checkpoint_stage"),
            "workflow_run_id": receipt.get("workflow_run_id"),
            "checkpoint_revision": receipt.get("checkpoint_revision"),
        }
        try:
            # Historical receipt: validate structurally only.  The envelope's
            # published_parent_identities are the abandon-time snapshot and
            # are deliberately NOT re-resolved against live git.
            _validate_abandoned_checkpoint(
                checkpoint,
                project_root=project_root,
                historical_receipt=True,
            )
        except _ei.AbandonedVersionLedgerError as exc:
            errors.append(str(exc))
        if envelope.get("next_v") != version:
            errors.append("abandon_receipt_checkpoint_version_mismatch")
        if envelope.get("source_v") != source_v:
            errors.append("abandon_receipt_checkpoint_source_mismatch")
    return list(dict.fromkeys(errors))


def _decode_abandoned_version_receipts(
    raw,
    *,
    allow_empty,
    project_root,
):
    if not isinstance(raw, str):
        raise _ei.AbandonedVersionLedgerError("abandon receipt ledger is not text")
    if len(raw.encode("utf-8")) > _ei._ABANDONED_VERSION_LEDGER_MAX_BYTES:
        raise _ei.AbandonedVersionLedgerError("abandon receipt ledger exceeds byte limit")
    if not raw:
        if allow_empty:
            return []
        raise _ei.AbandonedVersionLedgerError("abandon receipt ledger is empty")
    if not raw.endswith("\n"):
        raise _ei.AbandonedVersionLedgerError(
            "abandon receipt ledger has an incomplete final row"
        )
    receipts = []
    previous_version = None
    for line_number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            raise _ei.AbandonedVersionLedgerError(
                f"abandon receipt ledger blank row at line {line_number}"
            )
        try:
            receipt = json.loads(line)
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise _ei.AbandonedVersionLedgerError(
                f"abandon receipt ledger malformed at line {line_number}: "
                f"{type(exc).__name__}"
            ) from exc
        errors = _abandoned_version_receipt_errors(
            receipt,
            project_root=project_root,
        )
        if errors:
            raise _ei.AbandonedVersionLedgerError(
                f"abandon receipt ledger invalid at line {line_number}: "
                + "; ".join(errors)
            )
        version = receipt["version"]
        if previous_version is not None and version < previous_version:
            raise _ei.AbandonedVersionLedgerError(
                f"abandon receipt version order regressed at line {line_number}"
            )
        receipts.append(receipt)
        previous_version = version
    return receipts


def load_abandoned_version_receipts(*, path=None, project_root=None):
    """Load the exact current-epoch receipt chain or raise on any ambiguity."""

    path = Path(path) if path is not None else Path(_ei.ABANDONED_VERSIONS_FILE)
    project_root = Path(project_root) if project_root is not None else _ei.PROJECT_ROOT
    if not os.path.lexists(path):
        return []
    try:
        with _ei._locked_state_sidecar(path, lock_type=fcntl.LOCK_SH):
            raw = _ei._read_regular_state_text(path, allow_missing=False)
            if len(raw.encode("utf-8")) > _ei._ABANDONED_VERSION_LEDGER_MAX_BYTES:
                raise _ei.AbandonedVersionLedgerError(
                    "abandon receipt ledger exceeds byte limit"
                )
    except _ei.AbandonedVersionLedgerError:
        raise
    except (OSError, UnicodeError) as exc:
        raise _ei.AbandonedVersionLedgerError(
            f"abandon receipt ledger unreadable: {type(exc).__name__}: {exc}"
        ) from exc
    return _decode_abandoned_version_receipts(
        raw,
        allow_empty=False,
        project_root=project_root,
    )


def _abandon_authority_from_receipts(
    receipts,
    *,
    published_high_water,
    retryable_first_strict,
):
    versions = [
        int(receipt["version"])
        for receipt in receipts
        if not (
            retryable_first_strict
            and int(receipt["version"]) == _ei.FIRST_STRICT_POLICY_VERSION
        )
    ]
    return {
        "published_high_water": int(published_high_water),
        "floor": max(versions, default=0),
        "head_digest": _abandoned_ledger_head_digest(receipts),
        "receipt_count": len(receipts),
    }


def abandoned_version_authority(
    *,
    initialization=None,
    published_high_water=None,
    path=None,
    project_root=None,
):
    """Return one validated ledger snapshot (floor and chain head together)."""

    from epoch_authority import policy_epoch_initialization

    if published_high_water is None:
        published_high_water = _ei.find_current_v()
    published_high_water = int(published_high_water)
    if initialization is None:
        initialization = policy_epoch_initialization(current_v=published_high_water)
    if not isinstance(initialization, dict) or not initialization.get("initialized"):
        return {
            "published_high_water": published_high_water,
            "floor": 0,
            "head_digest": None,
            "receipt_count": 0,
        }
    receipts = _ei.load_abandoned_version_receipts(
        path=path,
        project_root=project_root,
    )
    retryable_first_strict = bool(
        initialization.get("state") == "fresh_bootstrap_ready"
        and initialization.get("strict_published") is False
    )
    return _abandon_authority_from_receipts(
        receipts,
        published_high_water=published_high_water,
        retryable_first_strict=retryable_first_strict,
    )


def _receipt_identity_matches_checkpoint(receipt, checkpoint):
    return bool(
        isinstance(receipt, dict)
        and receipt.get("workflow_run_id") == checkpoint.get("workflow_run_id")
        and receipt.get("checkpoint_revision")
        == checkpoint.get("checkpoint_revision")
        and receipt.get("checkpoint_stage") == checkpoint.get("stage")
        and receipt.get("checkpoint_envelope")
        == _abandoned_checkpoint_envelope(checkpoint)
    )


def recorded_abandon_receipt_for_checkpoint(
    checkpoint,
    *,
    path=None,
    project_root=None,
):
    """Return the sole durable terminal receipt for an uncleared checkpoint."""

    receipts = _ei.load_abandoned_version_receipts(
        path=path,
        project_root=project_root,
    )
    matches = [
        receipt
        for receipt in receipts
        if _receipt_identity_matches_checkpoint(receipt, checkpoint)
    ]
    if not matches:
        return None
    if len(matches) != 1 or matches[0] is not receipts[-1]:
        raise _ei.AbandonedVersionLedgerError(
            "recorded abandon checkpoint identity is not the unique chain head"
        )
    return dict(matches[0])


def find_abandoned_version_floor():
    """Return the content-bound allocation floor for this initialized epoch.

    A retired/pre-reset file is ignored because it has no epoch authority.  Once
    the epoch is initialized, however, every row must be a valid structured
    receipt created from a valid active checkpoint; unreadable, legacy, partial
    or tampered state raises and therefore blocks allocation.
    """

    try:
        return int(abandoned_version_authority()["floor"])
    except _ei.AbandonedVersionLedgerError:
        raise
    except Exception as exc:
        raise _ei.AbandonedVersionLedgerError(
            "policy epoch initialization unavailable while reading abandon receipts: "
            f"{type(exc).__name__}: {exc}"
        ) from exc


def abandoned_version_attempt_count(version):
    """Return the greatest validated workflow attempt for a reusable label."""

    target = int(version)
    attempts = []
    pattern = re.compile(
        rf"^generation:{re.escape(str(target))}:workflow-v([1-9][0-9]*)$"
    )
    for receipt in _ei.load_abandoned_version_receipts():
        if receipt["version"] != target:
            continue
        match = pattern.fullmatch(str(receipt.get("workflow_run_id") or ""))
        if not match:
            raise _ei.AbandonedVersionLedgerError(
                f"abandon receipt workflow attempt is invalid for v{target}"
            )
        attempts.append(int(match.group(1)))
    return max(attempts, default=0)
