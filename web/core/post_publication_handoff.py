"""Crash-safe handoff from immutable bot publication to Cycle Archivist.

The publishing checkpoint is cleared only after both this handoff and its
archive snapshot are durable.  The mutable handoff is a small journal: one
owner claim, content-bound step receipts, and a completed state that is valid
only while every receipt and archive annotation revalidates.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import stat
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from bot_artifact import canonical_digest
from bot_namespace import (
    ARCHIVED_VERSION_HIGH_WATER,
    EVALUATION_EPOCH,
    EVOLUTION_BRANCH,
    FIRST_STRICT_POLICY_VERSION,
    bot_name,
    bot_tag,
    high_water_tag,
)
from generation_evidence import build_generation_evidence_identity


SCHEMA_VERSION = 2
KIND = "national-policy-post-publication-handoff"
REQUIRED_STEPS = (
    "stability_observation",
    "reap_signal",
    "priority_eval",
    "archive_rotation",
    "log_cleanup",
    "pool_reap",
    "cycle_annotation",
    "housekeeping",
)
RECORD_SUFFIX = ".handoff.json"
ACTIVE_POINTER_SCHEMA_VERSION = 1
ACTIVE_POINTER_KIND = "national-policy-active-post-publication-handoff"

_RECORD_KEYS = frozenset({
    "schema_version", "kind", "identity", "identity_digest", "state",
    "owner", "steps", "revision", "updated_at", "last_error",
    "record_digest",
})
_IDENTITY_KEYS = frozenset({
    "schema_version", "evaluation_epoch", "version", "source_v",
    "workflow_run_id", "checkpoint_revision",
    "publishing_checkpoint_digest", "publication_id", "commit_oid",
    "candidate_artifact_hash", "source_binding_digest",
    "local_paired_refs", "local_publication_proof",
    "certificate_digest", "remote_publication",
    "archive_base_snapshot_digest",
})
_POINTER_KEYS = frozenset({
    "schema_version", "kind", "state", "version", "source_v",
    "publication_id", "identity_digest", "record_name", "pointer_digest",
})
_OWNER_KEYS = frozenset({
    "claim_id", "pid", "process_start_token", "claimed_at", "heartbeat_at",
})
_STEP_RECEIPT_KEYS = frozenset({
    "schema_version", "step", "publication_id", "completed_at",
    "plan_digest", "output", "receipt_digest",
})
_LOCAL_PROOF_KEYS = frozenset({
    "schema_version", "kind", "bot", "version", "artifact_hash", "tag",
    "tag_object", "commit_oid", "completion_tree_oid", "main_commit_oid",
    "proof_digest",
})

_ACTIVE_CLAIMS: set[str] = set()
_ACTIVE_CLAIMS_LOCK = threading.Lock()


class PostPublicationHandoffError(RuntimeError):
    """The handoff journal cannot safely advance."""


def _paths() -> tuple[Path, Path]:
    from evolution_infra import ARCHIVE_DIR, POST_PUBLICATION_HANDOFF_DIR

    return Path(POST_PUBLICATION_HANDOFF_DIR), Path(ARCHIVE_DIR)


def _handoff_path(version: int, publication_id: str) -> Path:
    root, _ = _paths()
    return root / f"v{int(version)}-{publication_id}{RECORD_SUFFIX}"


def _active_pointer_path() -> Path:
    root, _ = _paths()
    return root / "active-handoff.json"


def _archive_path(version: int) -> Path:
    _, root = _paths()
    return root / f"v{int(version)}.json"


def _is_hex(value: Any, length: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and all(char in "0123456789abcdef" for char in value)
    )


def _record_digest(record: dict[str, Any]) -> str:
    return canonical_digest({
        key: value for key, value in record.items() if key != "record_digest"
    })


def _receipt(payload: dict[str, Any]) -> dict[str, Any]:
    return {**payload, "receipt_digest": canonical_digest(payload)}


def _fsync_directory(path: Path) -> None:
    from evolution_infra import _fsync_directory as fsync_directory

    fsync_directory(path)


def _ensure_durable_authority_directory(path: Path) -> None:
    """Create one authority directory and durably bind it in its parent."""

    path = Path(path)
    parent = path.parent
    parent_before = os.lstat(parent)
    if not stat.S_ISDIR(parent_before.st_mode):
        raise PostPublicationHandoffError(
            f"authority_directory_parent_not_directory:{parent}"
        )
    try:
        os.mkdir(path, mode=0o700)
    except FileExistsError:
        pass
    directory_before = os.lstat(path)
    if not stat.S_ISDIR(directory_before.st_mode):
        raise PostPublicationHandoffError(
            f"authority_directory_not_directory:{path}"
        )

    # The child fsync covers its inode; the parent fsync makes the directory
    # entry durable.  Re-check both identities so a concurrent replacement
    # cannot silently redirect the authority files after the proof.
    _fsync_directory(path)
    _fsync_directory(parent)
    parent_after = os.lstat(parent)
    directory_after = os.lstat(path)
    if (
        not stat.S_ISDIR(parent_after.st_mode)
        or (parent_before.st_dev, parent_before.st_ino)
        != (parent_after.st_dev, parent_after.st_ino)
    ):
        raise PostPublicationHandoffError(
            f"authority_directory_parent_changed:{parent}"
        )
    if (
        not stat.S_ISDIR(directory_after.st_mode)
        or (directory_before.st_dev, directory_before.st_ino)
        != (directory_after.st_dev, directory_after.st_ino)
    ):
        raise PostPublicationHandoffError(
            f"authority_directory_changed:{path}"
        )


def _read_json(path: Path, *, missing_ok: bool = False) -> dict[str, Any] | None:
    from evolution_infra import _read_regular_state_text

    try:
        raw = _read_regular_state_text(path, allow_missing=missing_ok)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise
    if missing_ok and not raw:
        return None
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise PostPublicationHandoffError(f"state_file_not_object:{path.name}")
    return payload


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    from evolution_infra import _atomic_publish_state_text

    encoded = json.dumps(
        payload,
        indent=2,
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
    ) + "\n"
    _atomic_publish_state_text(path, encoded)


def _reprove_durable(path: Path) -> None:
    from evolution_infra import _fsync_regular_state_file_and_parent

    _fsync_regular_state_file_and_parent(path)


class _JournalLock:
    """Linearize the active pointer and record through one stable sidecar."""

    def __init__(self, path: Path):
        del path
        self.target = _active_pointer_path()
        self.path = self.target.with_suffix(self.target.suffix + ".lock")
        self.handle = None
        self._context = None

    def __enter__(self):
        from evolution_infra import _locked_state_sidecar

        self._context = _locked_state_sidecar(
            self.target,
            lock_type=fcntl.LOCK_EX,
        )
        try:
            self.handle = self._context.__enter__()
        except BaseException:
            self._context = None
            self.handle = None
            raise
        return self

    def __exit__(self, exc_type, exc, tb):
        context = self._context
        self._context = None
        self.handle = None
        if context is None:
            return False
        return context.__exit__(exc_type, exc, tb)


def publishing_checkpoint_projection(checkpoint: dict[str, Any]) -> dict[str, Any]:
    """Keep every field needed to reproduce evidence and workflow identity."""

    keys = (
        "checkpoint_schema_version",
        "evaluation_epoch",
        "epoch_binding",
        "next_v",
        "source_v",
        "parent2_v",
        "generation_mode",
        "workflow_run_id",
        "checkpoint_revision",
        "stage",
        "workflow_profile_id",
        "national_execution_mode",
        "generation_attempt",
        "precommit_rework_count",
        "official_rework_count",
        "repair_baseline_artifact_hash",
        "audit_context",
        "publication_intent",
    )
    # JSON round-trip rejects non-finite/non-serializable data and detaches the
    # durable projection from caller-owned mutable objects.
    return json.loads(json.dumps(
        {key: checkpoint.get(key) for key in keys},
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
    ))


def _archive_base(
    *,
    version: int,
    source_v: int,
    checkpoint: dict[str, Any],
    publication_identity: dict[str, Any],
) -> dict[str, Any]:
    projection = publishing_checkpoint_projection(checkpoint)
    from checkpoint_schema import (
        live_checkpoint_parent_authority_errors,
        live_policy_epoch_reset_receipt_errors,
    )
    from evolution_infra import PROJECT_ROOT

    live_authority_errors = [
        *live_policy_epoch_reset_receipt_errors(
            projection,
            project_root=PROJECT_ROOT,
        ),
        *live_checkpoint_parent_authority_errors(
            projection,
            repo_root=PROJECT_ROOT,
        ),
    ]
    if live_authority_errors:
        raise PostPublicationHandoffError(
            "publishing_checkpoint_live_authority_invalid:"
            + ";".join(live_authority_errors[:30])
        )
    selection = ((projection.get("audit_context") or {}).get("selection") or {})
    if not ((projection.get("audit_context") or {}).get("protocol_bootstrap")):
        from generation_scheduler import _read_source_v_history

        selected_history = selection.get("selection_view_source_history")
        live_history = _read_source_v_history()
        if (
            not isinstance(selected_history, list)
            or live_history != [*selected_history, int(source_v)]
        ):
            raise PostPublicationHandoffError(
                "selection_view_source_history_publication_mismatch"
            )
    evidence = build_generation_evidence_identity(
        projection,
        version=version,
        source_v=source_v,
    )
    gates = checkpoint.get("gate_results") or {}
    review = gates.get("review") or {}
    critic = gates.get("critic") or {}
    base = {
        "schema_version": 2,
        "kind": "national-policy-generation-archive",
        "evaluation_epoch": EVALUATION_EPOCH,
        "version": int(version),
        "source_v": int(source_v),
        "bot_name": bot_name(version),
        "git_tag": bot_tag(version),
        "publication_identity": publication_identity,
        "publishing_checkpoint_projection": projection,
        "publishing_checkpoint_digest": canonical_digest(projection),
        "strength_evidence_identity": evidence,
        "review_score": review.get("quality_score", review.get("score", 0)),
        "reviewer_change_summary": review.get("change_summary", ""),
        "reviewer_risk_areas": review.get("risk_areas", []),
        "critic_score": critic.get("score", 0),
        "precommit_passed": bool((gates.get("precommit_eval") or {}).get("passed")),
    }
    return base


def archive_semantic_digest(snapshot: dict[str, Any]) -> str:
    """Digest archive content while excluding only its completion marker."""

    semantic = {
        key: value
        for key, value in snapshot.items()
        if key != "finalization"
    }
    # The lifecycle marker changes only after all semantic effects have been
    # receipted.  Normalize it so a crash between archive-complete and
    # handoff-complete remains replayable without invalidating the annotation.
    handoff = semantic.get("post_publication_handoff")
    if isinstance(handoff, dict):
        semantic["post_publication_handoff"] = {
            key: value for key, value in handoff.items() if key != "state"
        }
    return canonical_digest(semantic)


def _publication_identity(
    *,
    version: int,
    source_v: int,
    checkpoint: dict[str, Any],
    publication_result: dict[str, Any],
    allow_local_only: bool,
) -> dict[str, Any]:
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or not isinstance(source_v, int)
        or isinstance(source_v, bool)
    ):
        raise PostPublicationHandoffError("publication_subject_type_invalid")
    intent = checkpoint.get("publication_intent")
    if checkpoint.get("stage") != "publishing" or not isinstance(intent, dict):
        raise PostPublicationHandoffError("publishing_checkpoint_invalid")
    if checkpoint.get("next_v") != version or checkpoint.get("source_v") != source_v:
        raise PostPublicationHandoffError("publishing_checkpoint_subject_mismatch")
    if publication_result.get("committed") is not True:
        raise PostPublicationHandoffError("publication_not_committed")
    publication_id = str(intent.get("publication_id") or "")
    commit_oid = str(publication_result.get("commit_oid") or "")
    if (
        not _is_hex(publication_id, 64)
        or publication_result.get("publication_id") != publication_id
        or not _is_hex(commit_oid, 40)
    ):
        raise PostPublicationHandoffError("publication_identity_invalid")
    local_refs = publication_result.get("local_refs") or {}
    paired: dict[str, Any] = {}
    for name in (bot_tag(version), high_water_tag(version)):
        row = local_refs.get(name)
        if not isinstance(row, dict):
            raise PostPublicationHandoffError(f"local_publication_ref_missing:{name}")
        object_oid = str(row.get("object_oid") or "")
        peeled = str(row.get("peeled_commit_oid") or "")
        if row.get("type") != "tag" or not _is_hex(object_oid, 40) or peeled != commit_oid:
            raise PostPublicationHandoffError(f"local_publication_ref_invalid:{name}")
        paired[name] = {
            "object_oid": object_oid,
            "peeled_commit_oid": peeled,
        }
    remote_required = intent.get("remote_publication_required") is True
    remote = publication_result.get("remote_proof") or {}
    if not remote_required and not allow_local_only:
        raise PostPublicationHandoffError(
            "post_publication_handoff_requires_remote_publication"
        )
    remote_binding: dict[str, Any]
    if remote_required:
        if remote.get("valid") is not True:
            raise PostPublicationHandoffError("remote_publication_proof_invalid")
        remote_refs = remote.get("remote_refs") or {}
        remote_main = str(remote.get("remote_main_oid") or "")
        if not _is_hex(remote_main, 40):
            raise PostPublicationHandoffError("remote_main_invalid")
        remote_paired: dict[str, Any] = {}
        for name in paired:
            object_oid = str(remote_refs.get(f"refs/tags/{name}") or "")
            peeled = str(remote_refs.get(f"refs/tags/{name}^{{}}") or "")
            if object_oid != paired[name]["object_oid"] or peeled != commit_oid:
                raise PostPublicationHandoffError(
                    f"remote_publication_ref_invalid:{name}"
                )
            remote_paired[name] = {
                "object_oid": object_oid,
                "peeled_commit_oid": peeled,
            }
        remote_binding = {
            "required": True,
            "remote_main_oid": remote_main,
            "paired_refs": remote_paired,
        }
    else:
        remote_binding = {
            "required": False,
            "explicit_test_mode": True,
            "remote_main_oid": None,
            "paired_refs": {},
        }
    artifact_hash = str(intent.get("candidate_artifact_hash") or "")
    certificate_digest = str(intent.get("official_certificate_digest") or "")
    local_proof = publication_result.get("local_publication_proof")
    if not isinstance(local_proof, dict) or set(local_proof) != _LOCAL_PROOF_KEYS:
        raise PostPublicationHandoffError("local_publication_proof_shape_invalid")
    unsigned_proof = {
        key: value for key, value in local_proof.items() if key != "proof_digest"
    }
    if local_proof.get("proof_digest") != canonical_digest(unsigned_proof):
        raise PostPublicationHandoffError("local_publication_proof_digest_invalid")
    completion_name = bot_tag(version)
    if (
        not _is_hex(artifact_hash, 64)
        or not _is_hex(certificate_digest, 64)
        or local_proof.get("schema_version") != 1
        or local_proof.get("kind")
        != "national-tcp-policy-pending-local-publication"
        or local_proof.get("bot") != bot_name(version)
        or local_proof.get("version") != version
        or local_proof.get("artifact_hash") != artifact_hash
        or local_proof.get("tag") != completion_name
        or local_proof.get("tag_object")
        != paired[completion_name]["object_oid"]
        or local_proof.get("commit_oid") != commit_oid
        or local_proof.get("main_commit_oid") != commit_oid
        or not _is_hex(local_proof.get("completion_tree_oid"), 40)
    ):
        raise PostPublicationHandoffError("candidate_artifact_hash_mismatch")
    projection = publishing_checkpoint_projection(checkpoint)
    source_binding = {
        "source_v": int(source_v),
        "parent2_v": checkpoint.get("parent2_v"),
        "epoch_binding": checkpoint.get("epoch_binding"),
        "protocol_bootstrap_receipt_digest": (
            ((checkpoint.get("audit_context") or {}).get("protocol_bootstrap") or {})
            .get("receipt_digest")
        ),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "evaluation_epoch": EVALUATION_EPOCH,
        "version": int(version),
        "source_v": int(source_v),
        "workflow_run_id": str(checkpoint.get("workflow_run_id") or ""),
        "checkpoint_revision": int(checkpoint.get("checkpoint_revision") or 0),
        "publishing_checkpoint_digest": canonical_digest(projection),
        "publication_id": publication_id,
        "commit_oid": commit_oid,
        "candidate_artifact_hash": artifact_hash,
        "source_binding_digest": canonical_digest(source_binding),
        "local_paired_refs": paired,
        "local_publication_proof": json.loads(json.dumps(local_proof)),
        "certificate_digest": certificate_digest,
        "remote_publication": remote_binding,
    }


def _base_record(identity: dict[str, Any], archive_digest: str) -> dict[str, Any]:
    identity = {**identity, "archive_base_snapshot_digest": archive_digest}
    identity_digest = canonical_digest(identity)
    record = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "identity": identity,
        "identity_digest": identity_digest,
        "state": "pending",
        "owner": None,
        "steps": {name: {"status": "pending"} for name in REQUIRED_STEPS},
        "revision": 1,
        "updated_at": time.time(),
        "last_error": None,
    }
    record["record_digest"] = _record_digest(record)
    return record


def _pointer_digest(pointer: dict[str, Any]) -> str:
    return canonical_digest({
        key: value for key, value in pointer.items() if key != "pointer_digest"
    })


def _active_pointer(record: dict[str, Any], record_path: Path) -> dict[str, Any]:
    identity = record["identity"]
    payload = {
        "schema_version": ACTIVE_POINTER_SCHEMA_VERSION,
        "kind": ACTIVE_POINTER_KIND,
        "state": "active",
        "version": identity["version"],
        "source_v": identity["source_v"],
        "publication_id": identity["publication_id"],
        "identity_digest": record["identity_digest"],
        "record_name": record_path.name,
    }
    return {**payload, "pointer_digest": canonical_digest(payload)}


def _active_pointer_errors(pointer: Any) -> list[str]:
    if not isinstance(pointer, dict):
        return ["handoff_active_pointer_not_object"]
    errors: list[str] = []
    if set(pointer) != _POINTER_KEYS:
        errors.append("handoff_active_pointer_fields_mismatch")
    if pointer.get("schema_version") != ACTIVE_POINTER_SCHEMA_VERSION:
        errors.append("handoff_active_pointer_schema_mismatch")
    if pointer.get("kind") != ACTIVE_POINTER_KIND:
        errors.append("handoff_active_pointer_kind_mismatch")
    if pointer.get("state") != "active":
        errors.append("handoff_active_pointer_state_mismatch")
    for field in ("version", "source_v"):
        value = pointer.get(field)
        if not isinstance(value, int) or isinstance(value, bool):
            errors.append(f"handoff_active_pointer_{field}_type_invalid")
    if not _is_hex(pointer.get("publication_id"), 64):
        errors.append("handoff_active_pointer_publication_id_invalid")
    if not _is_hex(pointer.get("identity_digest"), 64):
        errors.append("handoff_active_pointer_identity_digest_invalid")
    expected_name = None
    if (
        isinstance(pointer.get("version"), int)
        and not isinstance(pointer.get("version"), bool)
        and _is_hex(pointer.get("publication_id"), 64)
    ):
        expected_name = _handoff_path(
            pointer["version"], pointer["publication_id"]
        ).name
    if pointer.get("record_name") != expected_name:
        errors.append("handoff_active_pointer_record_name_mismatch")
    try:
        expected_digest = _pointer_digest(pointer)
    except Exception:
        expected_digest = ""
    if pointer.get("pointer_digest") != expected_digest:
        errors.append("handoff_active_pointer_digest_mismatch")
    return list(dict.fromkeys(errors))


def _load_active_pair(*, reopen_archive: bool = True) -> tuple[
    Path | None, dict[str, Any] | None, dict[str, Any] | None
]:
    """Read only the durable active pointer; completed history is never scanned."""

    pointer_path = _active_pointer_path()
    pointer = _read_json(pointer_path, missing_ok=True)
    if pointer is None:
        return None, None, None
    pointer_errors = _active_pointer_errors(pointer)
    if pointer_errors:
        raise PostPublicationHandoffError(";".join(pointer_errors))
    root, _ = _paths()
    record_path = root / pointer["record_name"]
    if record_path.parent != root or not record_path.name.endswith(RECORD_SUFFIX):
        raise PostPublicationHandoffError("handoff_active_record_path_invalid")
    record = _read_json(record_path)
    record_errors = validate_handoff_record(
        record,
        reopen_archive=reopen_archive,
    )
    if record_errors:
        raise PostPublicationHandoffError(";".join(record_errors))
    identity = record["identity"]
    for field in ("version", "source_v", "publication_id"):
        if pointer.get(field) != identity.get(field):
            raise PostPublicationHandoffError(
                f"handoff_active_pointer_{field}_mismatch"
            )
    if pointer.get("identity_digest") != record.get("identity_digest"):
        raise PostPublicationHandoffError(
            "handoff_active_pointer_identity_mismatch"
        )
    # Detect an atomic pointer replacement between the pointer and record read.
    if _read_json(pointer_path) != pointer:
        raise PostPublicationHandoffError("handoff_active_pointer_changed")
    if record.get("state") == "completed" and reopen_archive:
        # A record replace may have succeeded immediately before its parent
        # fsync failed.  Re-prove all three durable boundaries before a stale
        # completed pointer is allowed to project no active work.
        _reprove_durable(_archive_path(record["identity"]["version"]))
        _reprove_durable(record_path)
        _reprove_durable(pointer_path)
        if _read_json(pointer_path) != pointer:
            raise PostPublicationHandoffError(
                "handoff_active_pointer_changed_during_reproof"
            )
    return record_path, pointer, record


def ensure_post_publication_handoff(
    *,
    version: int,
    source_v: int,
    publishing_checkpoint: dict[str, Any],
    publication_result: dict[str, Any],
    allow_local_only: bool = False,
) -> dict[str, Any]:
    """Durably publish archive then handoff, re-proving both on every retry."""

    version = int(version)
    source_v = int(source_v)
    publication_identity = _publication_identity(
        version=version,
        source_v=source_v,
        checkpoint=publishing_checkpoint,
        publication_result=publication_result,
        allow_local_only=allow_local_only,
    )
    base = _archive_base(
        version=version,
        source_v=source_v,
        checkpoint=publishing_checkpoint,
        publication_identity=publication_identity,
    )
    base_digest = canonical_digest(base)
    expected_record = _base_record(publication_identity, base_digest)
    archive = {
        **base,
        "base_snapshot_digest": base_digest,
        "post_publication_handoff": {
            "identity_digest": expected_record["identity_digest"],
            "publication_id": publication_identity["publication_id"],
            "state": "pending",
        },
        "finalization": {"state": "pending"},
    }
    handoff_path = _handoff_path(version, publication_identity["publication_id"])
    archive_path = _archive_path(version)
    _ensure_durable_authority_directory(handoff_path.parent)
    _ensure_durable_authority_directory(archive_path.parent)
    with _JournalLock(handoff_path):
        active_path, _active, active_record = _load_active_pair(
            reopen_archive=True
        )
        if (
            active_record is not None
            and active_record.get("state") != "completed"
            and active_record.get("identity_digest")
            != expected_record["identity_digest"]
        ):
            raise PostPublicationHandoffError(
                "another_post_publication_handoff_is_active"
            )
        existing_archive = _read_json(archive_path, missing_ok=True)
        if existing_archive is None:
            _atomic_write(archive_path, archive)
        else:
            if existing_archive.get("base_snapshot_digest") != base_digest:
                raise PostPublicationHandoffError(
                    "archive_snapshot_preimage_mismatch"
                )
            _reprove_durable(archive_path)
        existing = _read_json(handoff_path, missing_ok=True)
        if existing is None:
            _atomic_write(handoff_path, expected_record)
            existing = expected_record
        else:
            if (
                existing.get("identity_digest")
                != expected_record["identity_digest"]
                or existing.get("identity") != expected_record["identity"]
            ):
                raise PostPublicationHandoffError("handoff_identity_mismatch")
            errors = validate_handoff_record(existing, reopen_archive=False)
            if errors:
                raise PostPublicationHandoffError(";".join(errors))
            _reprove_durable(handoff_path)
        pointer = _active_pointer(existing, handoff_path)
        _atomic_write(_active_pointer_path(), pointer)
        # Re-open both after their independent replace/fsync boundaries.  A
        # half-published archive/record/pointer set cannot authorize clear.
        archive_check = _read_json(archive_path)
        handoff_check = _read_json(handoff_path)
        pointer_check = _read_json(_active_pointer_path())
        if (
            archive_check.get("base_snapshot_digest") != base_digest
            or handoff_check.get("identity_digest")
            != expected_record["identity_digest"]
            or (archive_check.get("post_publication_handoff") or {}).get(
                "identity_digest"
            )
            != expected_record["identity_digest"]
            or pointer_check != pointer
        ):
            raise PostPublicationHandoffError("handoff_archive_pair_not_durable")
        _reprove_durable(archive_path)
        _reprove_durable(handoff_path)
        _reprove_durable(_active_pointer_path())
    return handoff_check


def _owner_alive(owner: Any) -> bool:
    if not isinstance(owner, dict):
        return False
    try:
        pid = int(owner.get("pid") or 0)
        expected = str(owner.get("process_start_token") or "")
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        closing = raw.rfind(")")
        fields = raw[closing + 2:].split()
        return bool(expected and len(fields) > 19 and fields[19] == expected)
    except Exception:
        return False


def _process_start_token() -> str:
    raw = Path(f"/proc/{os.getpid()}/stat").read_text(encoding="utf-8")
    closing = raw.rfind(")")
    fields = raw[closing + 2:].split()
    if len(fields) <= 19:
        raise PostPublicationHandoffError("handoff_owner_identity_unavailable")
    return fields[19]


def _strict_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _finite_time(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) >= 0.0
    )


def _owner_errors(owner: Any, state: str) -> list[str]:
    if state != "running":
        return [] if owner is None else ["handoff_owner_forbidden"]
    if not isinstance(owner, dict) or set(owner) != _OWNER_KEYS:
        return ["handoff_owner_shape_invalid"]
    errors: list[str] = []
    if not _is_hex(owner.get("claim_id"), 32):
        errors.append("handoff_owner_claim_id_invalid")
    pid = _strict_int(owner.get("pid"))
    if pid is None or pid <= 0:
        errors.append("handoff_owner_pid_invalid")
    token = owner.get("process_start_token")
    if not isinstance(token, str) or not token.isdecimal():
        errors.append("handoff_owner_start_token_invalid")
    claimed = owner.get("claimed_at")
    heartbeat = owner.get("heartbeat_at")
    if not _finite_time(claimed) or not _finite_time(heartbeat):
        errors.append("handoff_owner_timestamp_invalid")
    elif float(heartbeat) < float(claimed):
        errors.append("handoff_owner_heartbeat_precedes_claim")
    return errors


def _contract_error(step: str, subject: str, detail: str) -> str:
    return f"handoff_step_{subject}_contract_invalid:{step}:{detail}"


def _exact_object(
    value: Any,
    keys: set[str] | frozenset[str],
) -> bool:
    return isinstance(value, dict) and set(value) == set(keys)


def _step_plan_contract_errors(
    name: str,
    plan: Any,
    identity: dict[str, Any],
) -> list[str]:
    """Validate the exact producer contract before any effect may execute."""

    if not isinstance(plan, dict):
        return [_contract_error(name, "plan", "not_object")]
    errors: list[str] = []
    publication_id = identity.get("publication_id")
    version = identity.get("version")

    def reject(detail: str) -> None:
        errors.append(_contract_error(name, "plan", detail))

    if name == "stability_observation":
        if not _exact_object(plan, {
            "schema_version", "kind", "publication_id",
            "publishing_checkpoint_digest",
            "strength_evidence_identity_digest",
        }):
            reject("fields")
        if plan.get("schema_version") != 1 or plan.get(
            "kind"
        ) != "stability-observation-plan":
            reject("identity")
        if plan.get("publication_id") != publication_id:
            reject("publication")
        if plan.get("publishing_checkpoint_digest") != identity.get(
            "publishing_checkpoint_digest"
        ):
            reject("checkpoint")
        if not _is_hex(plan.get("strength_evidence_identity_digest"), 64):
            reject("evidence_digest")
    elif name == "reap_signal":
        if not _exact_object(plan, {
            "schema_version", "kind", "publication_id", "signal_text",
            "signal_sha256",
        }):
            reject("fields")
        signal = plan.get("signal_text")
        if (
            plan.get("schema_version") != 1
            or plan.get("kind") != "rating-daemon-refresh-plan"
            or plan.get("publication_id") != publication_id
        ):
            reject("identity")
        if not isinstance(signal, str) or not signal or len(signal) > 128:
            reject("signal_text")
        elif hashlib.sha256(signal.encode("utf-8")).hexdigest() != plan.get(
            "signal_sha256"
        ):
            reject("signal_digest")
    elif name == "priority_eval":
        payload = plan.get("payload")
        if not _exact_object(plan, {"schema_version", "kind", "payload"}):
            reject("fields")
        if (
            plan.get("schema_version") != 1
            or plan.get("kind") != "priority-evaluation-plan"
        ):
            reject("identity")
        if not _exact_object(
            payload, {"bot", "min_games", "since", "publication_id"}
        ):
            reject("payload_fields")
        elif (
            payload.get("bot") != bot_name(int(version or -1))
            or payload.get("min_games") != 500
            or not _finite_time(payload.get("since"))
            or payload.get("publication_id") != publication_id
        ):
            reject("payload_identity")
    elif name == "archive_rotation":
        try:
            from evolution_infra import _validate_archive_rotation_plan_shape

            _validate_archive_rotation_plan_shape(
                plan,
                version=int(version or -1),
                publication_id=publication_id,
            )
        except Exception as exc:
            reject(f"identity:{type(exc).__name__}:{str(exc)[:160]}")
    elif name == "log_cleanup":
        if not _exact_object(plan, {
            "schema_version", "kind", "handoff_version",
            "first_strict_version", "keep_generations", "cutoff_version",
            "archives", "publication_id",
        }):
            reject("fields")
        keep = plan.get("keep_generations")
        cutoff = plan.get("cutoff_version")
        archives = plan.get("archives")
        if (
            plan.get("schema_version") != 1
            or plan.get("kind") != "strict-log-cleanup-plan"
            or plan.get("handoff_version") != version
            or plan.get("first_strict_version")
            != FIRST_STRICT_POLICY_VERSION
            or keep != 5
            or type(cutoff) is not int
            or cutoff != int(version or -1) - 5
            or plan.get("publication_id") != publication_id
            or not isinstance(archives, list)
        ):
            reject("identity")
        if isinstance(archives, list):
            seen: set[int] = set()
            for item in archives:
                if not _exact_object(item, {
                    "schema_version", "kind", "version",
                    "source_relative_path", "entries", "tree_digest",
                    "archive_relative_path", "manifest_relative_path",
                    "quarantine_relative_path",
                }):
                    reject("archive_fields")
                    continue
                item_version = item.get("version")
                tree_payload = {
                    key: item.get(key)
                    for key in (
                        "schema_version", "kind", "version",
                        "source_relative_path", "entries",
                    )
                }
                if (
                    type(item_version) is not int
                    or item_version < FIRST_STRICT_POLICY_VERSION
                    or type(cutoff) is not int
                    or item_version > cutoff
                    or item_version in seen
                    or item.get("schema_version") != 1
                    or item.get("kind") != "strict-generation-log-tree"
                    or item.get("source_relative_path")
                    != f"v{item_version}/logs"
                    or not isinstance(item.get("entries"), list)
                    or item.get("tree_digest")
                    != canonical_digest(tree_payload)
                ):
                    reject("archive_identity")
                seen.add(item_version) if type(item_version) is int else None
    elif name == "pool_reap":
        # Reuse the pure schema-2 verifier so journal discovery, claim, and the
        # executor all prove the same frozen conservative-Glicko target
        # sequence without reopening mutable rating files.
        try:
            from tool_commit import _validate_pool_reap_plan

            _validate_pool_reap_plan(plan, {"identity": identity})
        except Exception as exc:
            reject(f"selection:{type(exc).__name__}:{str(exc)[:160]}")
    elif name == "cycle_annotation":
        if not _exact_object(plan, {
            "schema_version", "kind", "publication_id",
            "archive_pre_annotation_digest",
        }):
            reject("fields")
        if (
            plan.get("schema_version") != 1
            or plan.get("kind") != "cycle-archivist-annotation-plan"
            or plan.get("publication_id") != publication_id
            or not _is_hex(plan.get("archive_pre_annotation_digest"), 64)
        ):
            reject("identity")
    elif name == "housekeeping":
        dependencies = plan.get("dependency_receipts")
        if not _exact_object(plan, {
            "schema_version", "kind", "expected_head_oid",
            "expected_dirty_paths", "tracked_housekeeping_commit_allowed",
            "dependency_receipts",
        }):
            reject("fields")
        if (
            plan.get("schema_version") != 1
            or plan.get("kind")
            != "post-publication-worktree-verification-plan"
            or plan.get("expected_head_oid") != identity.get("commit_oid")
            or plan.get("expected_dirty_paths") != []
            or plan.get("tracked_housekeeping_commit_allowed") is not False
            or not _exact_object(dependencies, {
                "archive_rotation", "log_cleanup", "pool_reap",
                "cycle_annotation",
            })
            or any(not _is_hex(value, 64) for value in (
                dependencies.values() if isinstance(dependencies, dict) else []
            ))
        ):
            reject("identity")
    return list(dict.fromkeys(errors))


def _step_output_contract_errors(
    name: str,
    output: Any,
    plan: dict[str, Any],
    plan_digest: str,
    identity: dict[str, Any],
) -> list[str]:
    if not isinstance(output, dict):
        return [_contract_error(name, "output", "not_object")]
    errors: list[str] = []
    publication_id = identity.get("publication_id")
    version = identity.get("version")

    def reject(detail: str) -> None:
        errors.append(_contract_error(name, "output", detail))

    if output.get("plan_digest") != plan_digest:
        reject("plan_binding")
    if name == "stability_observation":
        if not _exact_object(output, {
            "plan_digest", "publication_id", "continuity_id", "count",
            "target", "complete",
        }):
            reject("fields")
        if (
            output.get("publication_id") != publication_id
            or not _is_hex(output.get("continuity_id"), 32)
            or type(output.get("count")) is not int
            or output.get("count") < 0
            or output.get("count") > 10
            or output.get("target") != 10
            or type(output.get("complete")) is not bool
            or (output.get("complete") is True and output.get("count") < 10)
        ):
            reject("identity")
    elif name == "reap_signal":
        if not _exact_object(
            output, {"plan_digest", "publication_id", "signal_sha256"}
        ):
            reject("fields")
        if (
            output.get("publication_id") != publication_id
            or output.get("signal_sha256") != plan.get("signal_sha256")
        ):
            reject("identity")
    elif name == "priority_eval":
        if not _exact_object(output, {
            "plan_digest", "bot", "min_games", "publication_id",
            "payload_sha256",
        }):
            reject("fields")
        payload = plan.get("payload")
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ) + "\n"
        if (
            output.get("bot") != bot_name(int(version or -1))
            or output.get("min_games") != 500
            or output.get("publication_id") != publication_id
            or output.get("payload_sha256")
            != hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        ):
            reject("identity")
    elif name == "archive_rotation":
        rotations = output.get("rotations")
        if not _exact_object(output, {
            "plan_digest", "version", "rotations", "rotation_set_digest",
        }):
            reject("fields")
        if (
            output.get("version") != version
            or not isinstance(rotations, list)
            or output.get("rotation_set_digest") != canonical_digest(rotations)
        ):
            reject("identity")
        if isinstance(rotations, list):
            for item in rotations:
                if not _exact_object(item, {
                    "source", "rotation_id", "plan_digest",
                    "archive_sha256", "start_offset", "end_offset",
                    "source_preserved_append_only",
                }) or any(
                    not _is_hex(item.get(key), 64)
                    for key in (
                        "rotation_id", "plan_digest", "archive_sha256",
                    )
                ) or (
                    type(item.get("start_offset")) is not int
                    or type(item.get("end_offset")) is not int
                    or item.get("start_offset") < 0
                    or item.get("end_offset") <= item.get("start_offset")
                    or item.get("source_preserved_append_only") is not True
                ):
                    reject("rotation_receipt")
            try:
                from evolution_infra import expected_archive_rotation_receipts

                expected_rotations = expected_archive_rotation_receipts(
                    plan,
                    version=int(version or -1),
                    publication_id=identity.get("publication_id"),
                )
            except Exception as exc:
                reject(
                    f"rotation_plan:{type(exc).__name__}:{str(exc)[:160]}"
                )
            else:
                if rotations != expected_rotations:
                    reject("rotation_set_mismatch")
    elif name == "log_cleanup":
        archives = output.get("archives")
        if not _exact_object(output, {
            "plan_digest", "version", "archives", "archive_set_digest",
        }):
            reject("fields")
        if (
            output.get("version") != version
            or not isinstance(archives, list)
            or output.get("archive_set_digest") != canonical_digest(archives)
        ):
            reject("identity")
        if isinstance(archives, list) and len(archives) != len(
            plan.get("archives") or []
        ):
            reject("archive_count")
        if isinstance(archives, list):
            planned = {
                item.get("version"): item
                for item in (plan.get("archives") or [])
                if isinstance(item, dict)
            }
            for receipt in archives:
                if not _exact_object(receipt, {
                    "version", "tree_digest", "archive_relative_path",
                    "archive_sha256", "manifest_relative_path",
                    "manifest_digest", "effect_mode",
                    "live_source_relative_path", "live_log_tree_preserved",
                    "quarantine_log_tree_touched",
                    "generation_siblings_preserved",
                }):
                    reject("archive_receipt_fields")
                    continue
                subject = planned.get(receipt.get("version")) or {}
                if (
                    receipt.get("tree_digest") != subject.get("tree_digest")
                    or receipt.get("archive_relative_path")
                    != subject.get("archive_relative_path")
                    or receipt.get("manifest_relative_path")
                    != subject.get("manifest_relative_path")
                    or receipt.get("live_source_relative_path")
                    != subject.get("source_relative_path")
                    or not _is_hex(receipt.get("archive_sha256"), 64)
                    or not _is_hex(receipt.get("manifest_digest"), 64)
                    or receipt.get("effect_mode")
                    != "nondestructive-immutable-archive"
                    or receipt.get("live_log_tree_preserved") is not True
                    or receipt.get("quarantine_log_tree_touched") is not False
                    or receipt.get("generation_siblings_preserved") is not True
                ):
                    reject("archive_receipt_identity")
    elif name == "pool_reap":
        if not _exact_object(output, {
            "plan_digest", "removed_bots", "required_reaps", "reap_proofs",
            "reap_proof_set_digest",
        }):
            reject("fields")
        proofs = output.get("reap_proofs")
        removed = output.get("removed_bots")
        target_names = [
            row.get("candidate")
            for row in (plan.get("targets") or [])
            if isinstance(row, dict)
        ]
        if (
            not isinstance(removed, list)
            or not isinstance(proofs, list)
            or output.get("required_reaps") != plan.get("required_reaps")
            or removed != sorted(target_names)
            or len(proofs) != len(target_names)
            or sorted(
                proof.get("bot")
                for proof in proofs
                if isinstance(proof, dict)
            ) != sorted(target_names)
            or output.get("reap_proof_set_digest") != canonical_digest(proofs)
        ):
            reject("identity")
    elif name == "cycle_annotation":
        if not _exact_object(output, {
            "plan_digest", "annotation_digest", "archive_semantic_digest",
        }):
            reject("fields")
        if not _is_hex(output.get("annotation_digest"), 64) or not _is_hex(
            output.get("archive_semantic_digest"), 64
        ):
            reject("identity")
    elif name == "housekeeping":
        if not _exact_object(output, {
            "plan_digest", "head_oid", "worktree_status_digest",
            "tracked_housekeeping_commit", "archive_rotation_revalidated",
            "strict_log_archives_revalidated", "reap_proofs",
            "reap_proof_set_digest",
        }):
            reject("fields")
        proofs = output.get("reap_proofs")
        if (
            output.get("head_oid") != identity.get("commit_oid")
            or not _is_hex(output.get("worktree_status_digest"), 64)
            or output.get("tracked_housekeeping_commit") is not False
            or output.get("archive_rotation_revalidated") is not True
            or output.get("strict_log_archives_revalidated") is not True
            or not isinstance(proofs, list)
            or output.get("reap_proof_set_digest") != canonical_digest(proofs)
        ):
            reject("identity")
    return list(dict.fromkeys(errors))


def _reprove_operational_steps(record: dict[str, Any]) -> dict[str, bool]:
    """Re-open stability state and idempotently reissue daemon capabilities.

    Receipt digests prove journal integrity, not that an external effect still
    exists.  A retry and finalization therefore re-prove the durable stability
    row and re-publish the exact content-bound signal/priority payload.  The
    daemon may consume either file immediately after the sidecar lock releases;
    that consumption is the intended effect.
    """

    steps = record.get("steps") or {}
    identity = record.get("identity") or {}
    result = {
        "stability_observation": False,
        "reap_signal": False,
        "priority_eval": False,
    }
    stability = steps.get("stability_observation") or {}
    if stability.get("status") == "completed":
        from stability_observation import stability_observation_projection

        projection = stability_observation_projection()
        output = (stability.get("receipt") or {}).get("output") or {}
        expected = {
            "continuity_id": projection.get("continuity_id"),
            "count": projection.get("count"),
            "target": projection.get("target"),
            "complete": projection.get("complete"),
        }
        if any(output.get(key) != value for key, value in expected.items()):
            raise PostPublicationHandoffError(
                "handoff_stability_observation_reproof_mismatch"
            )
        publication_id = identity.get("publication_id")
        observations = projection.get("observations")
        if not isinstance(observations, list) or not any(
            isinstance(row, dict) and row.get("publication_id") == publication_id
            for row in observations
        ):
            raise PostPublicationHandoffError(
                "handoff_stability_publication_row_missing"
            )
        result["stability_observation"] = True

    from evolution_infra import (
        RESULTS_DIR,
        _atomic_publish_state_text,
        _locked_state_sidecar,
        _read_regular_state_text,
    )

    signal = steps.get("reap_signal") or {}
    if signal.get("status") == "completed":
        plan = signal.get("plan") or {}
        output = (signal.get("receipt") or {}).get("output") or {}
        raw = str(plan.get("signal_text") or "")
        path = Path(RESULTS_DIR) / ".reap_signal"
        with _locked_state_sidecar(path, lock_type=fcntl.LOCK_EX):
            _atomic_publish_state_text(path, raw)
            reopened = _read_regular_state_text(path, allow_missing=False)
        digest = hashlib.sha256(reopened.encode("utf-8")).hexdigest()
        if digest != plan.get("signal_sha256") or digest != output.get(
            "signal_sha256"
        ):
            raise PostPublicationHandoffError(
                "handoff_reap_signal_reproof_mismatch"
            )
        result["reap_signal"] = True

    priority = steps.get("priority_eval") or {}
    if priority.get("status") == "completed":
        payload = (priority.get("plan") or {}).get("payload")
        output = (priority.get("receipt") or {}).get("output") or {}
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ) + "\n"
        path = Path(RESULTS_DIR) / "priority_eval.json"
        with _locked_state_sidecar(path, lock_type=fcntl.LOCK_EX):
            _atomic_publish_state_text(path, encoded)
            reopened = _read_regular_state_text(path, allow_missing=False)
        digest = hashlib.sha256(reopened.encode("utf-8")).hexdigest()
        if reopened != encoded or digest != output.get("payload_sha256"):
            raise PostPublicationHandoffError(
                "handoff_priority_eval_reproof_mismatch"
            )
        result["priority_eval"] = True
    return result


def _reprove_external_steps(record: dict[str, Any]) -> None:
    """Unconditionally reopen every non-operational external effect at commit.

    This is the final semantic boundary: public receipt digests can detect
    accidental damage but are not permission to trust a re-signed boolean.
    Rotation archives, log archives, tombstones, Cycle Archivist output, and
    the Git worktree are therefore re-derived from their frozen plans.
    """

    identity = record["identity"]
    version = int(identity["version"])
    source_v = int(identity["source_v"])
    steps = record["steps"]

    rotation_output = steps["archive_rotation"]["receipt"]["output"]
    rotations = rotation_output["rotations"]
    from evolution_infra import validate_archive_rotation_receipts

    if validate_archive_rotation_receipts(
        version,
        rotations,
        rotation_plan=steps["archive_rotation"]["plan"],
    ) != rotations:
        raise PostPublicationHandoffError(
            "handoff_archive_rotation_external_reproof_mismatch"
        )

    from tool_commit import (
        _converge_and_verify_reaped_target,
        _revalidate_strict_log_archives,
        _validate_pool_reap_plan,
        _verify_post_publication_worktree,
    )

    log_row = steps["log_cleanup"]
    _revalidate_strict_log_archives(
        log_row["plan"],
        log_row["receipt"]["output"]["archives"],
        expected_handoff_version=version,
        expected_publication_id=identity["publication_id"],
    )

    pool_row = steps["pool_reap"]
    _initial, target_names, _snapshot = _validate_pool_reap_plan(
        pool_row["plan"], record
    )
    pool_output = pool_row["receipt"]["output"]
    if (
        pool_output.get("required_reaps") != len(target_names)
        or pool_output.get("removed_bots") != sorted(target_names)
    ):
        raise PostPublicationHandoffError(
            "handoff_pool_reap_target_set_mismatch"
        )
    prior_proofs = {
        proof.get("bot"): proof
        for proof in pool_output.get("reap_proofs") or []
        if isinstance(proof, dict)
    }
    final_proofs = []
    for name in target_names:
        proof = _converge_and_verify_reaped_target(name, record)
        prior = prior_proofs.get(name) or {}
        for field in (
            "version", "completion_commit_oid", "tombstone_tag",
            "tombstone_object_oid", "tombstone_commit_oid",
        ):
            if proof.get(field) != prior.get(field):
                raise PostPublicationHandoffError(
                    "handoff_pool_reap_external_reproof_mismatch"
                )
        final_proofs.append(proof)

    archive = _read_json(_archive_path(version))
    annotation = archive.get("archivist_notes")
    from cycle_archivist import annotation_identity_errors

    annotation_errors = annotation_identity_errors(
        annotation,
        archive,
        version=version,
        source_v=source_v,
    )
    cycle_output = steps["cycle_annotation"]["receipt"]["output"]
    if annotation_errors:
        raise PostPublicationHandoffError(
            "handoff_cycle_annotation_external_reproof_invalid:"
            + ";".join(annotation_errors[:20])
        )
    if (
        annotation.get("annotation_digest")
        != cycle_output.get("annotation_digest")
        or archive_semantic_digest(archive)
        != cycle_output.get("archive_semantic_digest")
    ):
        raise PostPublicationHandoffError(
            "handoff_cycle_annotation_external_reproof_mismatch"
        )

    housekeeping_row = steps["housekeeping"]
    housekeeping_plan = housekeeping_row["plan"]
    expected_dependencies = {
        name: steps[name]["receipt"]["receipt_digest"]
        for name in (
            "archive_rotation", "log_cleanup", "pool_reap",
            "cycle_annotation",
        )
    }
    if housekeeping_plan.get("dependency_receipts") != expected_dependencies:
        raise PostPublicationHandoffError(
            "handoff_housekeeping_dependency_mismatch"
        )
    actual_worktree = _verify_post_publication_worktree(
        expected_head=housekeeping_plan["expected_head_oid"],
        expected_dirty=set(housekeeping_plan["expected_dirty_paths"]),
    )
    housekeeping_output = housekeeping_row["receipt"]["output"]
    for key, value in actual_worktree.items():
        if housekeeping_output.get(key) != value:
            raise PostPublicationHandoffError(
                "handoff_housekeeping_worktree_reproof_mismatch"
            )
    if (
        housekeeping_output.get("reap_proofs") != final_proofs
        or housekeeping_output.get("reap_proof_set_digest")
        != canonical_digest(final_proofs)
    ):
        raise PostPublicationHandoffError(
            "handoff_housekeeping_reap_reproof_mismatch"
        )


def _step_receipt_errors(
    name: str,
    row: Any,
    identity: dict[str, Any],
) -> list[str]:
    if not isinstance(row, dict) or set(row) != {
        "status", "plan", "plan_digest", "receipt",
    }:
        return [f"handoff_step_receipt_row_shape_invalid:{name}"]
    if row.get("status") != "completed":
        return [f"handoff_step_incomplete:{name}"]
    receipt = row.get("receipt")
    if not isinstance(receipt, dict) or set(receipt) != _STEP_RECEIPT_KEYS:
        return [f"handoff_step_receipt_shape_invalid:{name}"]
    errors: list[str] = []
    publication_id = str(identity.get("publication_id") or "")
    row_plan_digest = None
    if not isinstance(row.get("plan"), dict):
        errors.append(f"handoff_step_completed_plan_invalid:{name}")
    else:
        errors.extend(_step_plan_contract_errors(name, row["plan"], identity))
        try:
            row_plan_digest = canonical_digest(row["plan"])
        except Exception:
            row_plan_digest = ""
        if row.get("plan_digest") != row_plan_digest:
            errors.append(
                f"handoff_step_completed_plan_digest_mismatch:{name}"
            )
    unsigned = {
        key: value for key, value in receipt.items() if key != "receipt_digest"
    }
    try:
        expected_digest = canonical_digest(unsigned)
    except Exception:
        expected_digest = ""
    if receipt.get("receipt_digest") != expected_digest:
        errors.append(f"handoff_step_receipt_digest_mismatch:{name}")
    if receipt.get("schema_version") != 1:
        errors.append(f"handoff_step_receipt_schema_mismatch:{name}")
    if receipt.get("step") != name:
        errors.append(f"handoff_step_receipt_name_mismatch:{name}")
    if receipt.get("publication_id") != publication_id:
        errors.append(f"handoff_step_receipt_publication_mismatch:{name}")
    if not _finite_time(receipt.get("completed_at")):
        errors.append(f"handoff_step_receipt_time_invalid:{name}")
    if not isinstance(receipt.get("output"), dict):
        errors.append(f"handoff_step_receipt_output_invalid:{name}")
    plan_digest = receipt.get("plan_digest")
    if plan_digest is not None and not _is_hex(plan_digest, 64):
        errors.append(f"handoff_step_receipt_plan_digest_invalid:{name}")
    if plan_digest != row_plan_digest:
        errors.append(f"handoff_step_receipt_plan_binding_mismatch:{name}")
    if row_plan_digest is not None and isinstance(receipt.get("output"), dict):
        if receipt["output"].get("plan_digest") != row_plan_digest:
            errors.append(f"handoff_step_output_plan_binding_mismatch:{name}")
        errors.extend(_step_output_contract_errors(
            name,
            receipt["output"],
            row["plan"],
            row_plan_digest,
            identity,
        ))
    return errors


def _step_row_errors(
    name: str,
    row: Any,
    identity: dict[str, Any],
) -> list[str]:
    if not isinstance(row, dict):
        return [f"handoff_step_row_invalid:{name}"]
    status = row.get("status")
    if status == "pending":
        return [] if set(row) == {"status"} else [
            f"handoff_step_pending_shape_invalid:{name}"
        ]
    if status == "planned":
        errors = []
        if set(row) != {"status", "plan", "plan_digest"}:
            errors.append(f"handoff_step_plan_shape_invalid:{name}")
        plan = row.get("plan")
        if not isinstance(plan, dict):
            errors.append(f"handoff_step_plan_not_object:{name}")
        else:
            errors.extend(_step_plan_contract_errors(name, plan, identity))
            try:
                expected = canonical_digest(plan)
            except Exception:
                expected = ""
            if row.get("plan_digest") != expected:
                errors.append(f"handoff_step_plan_digest_mismatch:{name}")
        return errors
    if status == "completed":
        return _step_receipt_errors(name, row, identity)
    return [f"handoff_step_state_invalid:{name}"]


def validate_handoff_record(
    record: Any,
    *,
    reopen_archive: bool = True,
) -> list[str]:
    if not isinstance(record, dict):
        return ["handoff_not_object"]
    errors: list[str] = []
    if set(record) != _RECORD_KEYS:
        errors.append("handoff_record_fields_mismatch")
    if record.get("schema_version") != SCHEMA_VERSION:
        errors.append("handoff_schema_mismatch")
    if record.get("kind") != KIND:
        errors.append("handoff_kind_mismatch")
    identity = record.get("identity")
    if not isinstance(identity, dict):
        errors.append("handoff_identity_missing")
        identity = {}
    elif set(identity) != _IDENTITY_KEYS:
        errors.append("handoff_identity_fields_mismatch")
    try:
        expected_identity_digest = canonical_digest(identity)
    except Exception:
        expected_identity_digest = ""
    if record.get("identity_digest") != expected_identity_digest:
        errors.append("handoff_identity_digest_mismatch")
    try:
        expected_record_digest = _record_digest(record)
    except Exception:
        expected_record_digest = ""
    if record.get("record_digest") != expected_record_digest:
        errors.append("handoff_record_digest_mismatch")
    if identity.get("evaluation_epoch") != EVALUATION_EPOCH:
        errors.append("handoff_epoch_mismatch")
    if identity.get("schema_version") != SCHEMA_VERSION:
        errors.append("handoff_identity_schema_mismatch")
    version = _strict_int(identity.get("version"))
    source_v = _strict_int(identity.get("source_v"))
    checkpoint_revision = _strict_int(identity.get("checkpoint_revision"))
    if (
        version is None
        or source_v is None
        or checkpoint_revision is None
        or version < FIRST_STRICT_POLICY_VERSION
        or source_v < ARCHIVED_VERSION_HIGH_WATER
        or source_v >= version
        or checkpoint_revision <= 0
    ):
        version, source_v, checkpoint_revision = -1, -1, -1
        errors.append("handoff_subject_identity_invalid")
    workflow_run_id = identity.get("workflow_run_id")
    if not isinstance(workflow_run_id, str) or not workflow_run_id or len(
        workflow_run_id
    ) > 240 or any(
        ord(char) < 32 for char in workflow_run_id
    ):
        errors.append("handoff_workflow_identity_invalid")
        workflow_run_id = ""
    for field, length in (
        ("publication_id", 64),
        ("commit_oid", 40),
        ("candidate_artifact_hash", 64),
        ("publishing_checkpoint_digest", 64),
        ("source_binding_digest", 64),
        ("certificate_digest", 64),
        ("archive_base_snapshot_digest", 64),
    ):
        if not _is_hex(identity.get(field), length):
            errors.append(f"handoff_{field}_invalid")
    expected_names = (
        {bot_tag(version), high_water_tag(version)}
        if version >= FIRST_STRICT_POLICY_VERSION
        else set()
    )
    local_refs = identity.get("local_paired_refs")
    if not isinstance(local_refs, dict) or set(local_refs) != expected_names:
        errors.append("handoff_local_paired_refs_shape_invalid")
        local_refs = {}
    for name, row in local_refs.items():
        if (
            not isinstance(row, dict)
            or set(row) != {"object_oid", "peeled_commit_oid"}
            or not _is_hex(row.get("object_oid"), 40)
            or row.get("peeled_commit_oid") != identity.get("commit_oid")
        ):
            errors.append(f"handoff_local_ref_invalid:{name}")
    local_proof = identity.get("local_publication_proof")
    if not isinstance(local_proof, dict) or set(local_proof) != _LOCAL_PROOF_KEYS:
        errors.append("handoff_local_publication_proof_shape_invalid")
        local_proof = {}
    else:
        unsigned_proof = {
            key: value for key, value in local_proof.items()
            if key != "proof_digest"
        }
        if local_proof.get("proof_digest") != canonical_digest(unsigned_proof):
            errors.append("handoff_local_publication_proof_digest_invalid")
    completion_name = (
        bot_tag(version) if version >= FIRST_STRICT_POLICY_VERSION else ""
    )
    expected_bot_name = (
        bot_name(version) if version >= FIRST_STRICT_POLICY_VERSION else ""
    )
    if (
        local_proof.get("schema_version") != 1
        or local_proof.get("kind")
        != "national-tcp-policy-pending-local-publication"
        or local_proof.get("bot") != expected_bot_name
        or local_proof.get("version") != version
        or local_proof.get("artifact_hash")
        != identity.get("candidate_artifact_hash")
        or local_proof.get("tag") != completion_name
        or local_proof.get("tag_object")
        != (local_refs.get(completion_name) or {}).get("object_oid")
        or local_proof.get("commit_oid") != identity.get("commit_oid")
        or local_proof.get("main_commit_oid") != identity.get("commit_oid")
        or not _is_hex(local_proof.get("completion_tree_oid"), 40)
    ):
        errors.append("handoff_local_publication_proof_identity_mismatch")
    remote = identity.get("remote_publication")
    if not isinstance(remote, dict):
        errors.append("handoff_remote_publication_missing")
        remote = {}
    elif remote.get("required") is True:
        if set(remote) != {"required", "remote_main_oid", "paired_refs"}:
            errors.append("handoff_remote_publication_fields_mismatch")
        if not _is_hex(remote.get("remote_main_oid"), 40):
            errors.append("handoff_remote_main_invalid")
        paired = remote.get("paired_refs")
        if not isinstance(paired, dict) or set(paired) != expected_names:
            errors.append("handoff_remote_paired_refs_shape_invalid")
        else:
            for name, row in paired.items():
                if row != local_refs.get(name):
                    errors.append(f"handoff_remote_local_ref_mismatch:{name}")
    else:
        if (
            set(remote) != {
                "required", "explicit_test_mode", "remote_main_oid", "paired_refs"
            }
            or remote.get("required") is not False
            or remote.get("explicit_test_mode") is not True
            or remote.get("remote_main_oid") is not None
            or remote.get("paired_refs") != {}
        ):
            errors.append("handoff_local_only_projection_invalid")
    state = record.get("state")
    if state not in {"pending", "running", "completed"}:
        errors.append("handoff_state_invalid")
        state = "invalid"
    revision = _strict_int(record.get("revision"))
    if revision is None or revision <= 0:
        errors.append("handoff_revision_invalid")
    if not _finite_time(record.get("updated_at")):
        errors.append("handoff_updated_at_invalid")
    last_error = record.get("last_error")
    if last_error is not None and (
        not isinstance(last_error, str) or len(last_error) > 1000
    ):
        errors.append("handoff_last_error_invalid")
    errors.extend(_owner_errors(record.get("owner"), state))
    steps = record.get("steps")
    if not isinstance(steps, dict) or set(steps) != set(REQUIRED_STEPS):
        errors.append("handoff_steps_shape_invalid")
        steps = {}
    if steps:
        statuses: list[str] = []
        for name in REQUIRED_STEPS:
            row = steps.get(name)
            errors.extend(_step_row_errors(name, row, identity))
            statuses.append(row.get("status") if isinstance(row, dict) else "invalid")
        first_incomplete = next(
            (index for index, status in enumerate(statuses) if status != "completed"),
            len(statuses),
        )
        if any(status == "completed" for status in statuses[first_incomplete:]):
            errors.append("handoff_step_completed_prefix_invalid")
        planned_indexes = [
            index for index, status in enumerate(statuses) if status == "planned"
        ]
        if len(planned_indexes) > 1 or (
            planned_indexes and planned_indexes[0] != first_incomplete
        ):
            errors.append("handoff_step_plan_position_invalid")
        if any(
            status not in {"pending", "planned"}
            for status in statuses[first_incomplete:]
        ):
            errors.append("handoff_step_suffix_invalid")
        housekeeping = steps.get("housekeeping") or {}
        housekeeping_plan = housekeeping.get("plan")
        if isinstance(housekeeping_plan, dict):
            expected_dependencies = {
                name: (steps.get(name, {}).get("receipt") or {}).get(
                    "receipt_digest"
                )
                for name in (
                    "archive_rotation", "log_cleanup", "pool_reap",
                    "cycle_annotation",
                )
            }
            if housekeeping_plan.get(
                "dependency_receipts"
            ) != expected_dependencies:
                errors.append("handoff_housekeeping_dependency_mismatch")
        if state == "completed" and first_incomplete != len(REQUIRED_STEPS):
            errors.append("handoff_completed_steps_incomplete")
    if reopen_archive and not errors:
        try:
            archive = _read_json(_archive_path(int(identity["version"])))
        except Exception as exc:
            errors.append(f"handoff_archive_unavailable:{type(exc).__name__}")
        else:
            base = {
                key: value
                for key, value in archive.items()
                if key not in {
                    "base_snapshot_digest",
                    "post_publication_handoff",
                    "finalization",
                    "archivist_notes",
                }
            }
            if canonical_digest(base) != archive.get("base_snapshot_digest"):
                errors.append("handoff_archive_base_preimage_mismatch")
            if archive.get("base_snapshot_digest") != identity.get(
                "archive_base_snapshot_digest"
            ):
                errors.append("handoff_archive_base_digest_mismatch")
            if archive.get("publication_identity") != {
                key: value
                for key, value in identity.items()
                if key != "archive_base_snapshot_digest"
            }:
                errors.append("handoff_archive_publication_identity_mismatch")
            projection = archive.get("publishing_checkpoint_projection")
            if not isinstance(projection, dict):
                errors.append("handoff_archive_checkpoint_projection_missing")
                projection = {}
            if canonical_digest(projection) != identity.get(
                "publishing_checkpoint_digest"
            ):
                errors.append("handoff_archive_checkpoint_digest_mismatch")
            for field, expected in (
                ("next_v", version),
                ("source_v", source_v),
                ("workflow_run_id", workflow_run_id),
                ("checkpoint_revision", checkpoint_revision),
                ("stage", "publishing"),
            ):
                if projection.get(field) != expected:
                    errors.append(f"handoff_archive_checkpoint_{field}_mismatch")
            intent = projection.get("publication_intent") or {}
            if (
                intent.get("publication_id") != identity.get("publication_id")
                or intent.get("candidate_artifact_hash")
                != identity.get("candidate_artifact_hash")
                or intent.get("official_certificate_digest")
                != identity.get("certificate_digest")
            ):
                errors.append("handoff_archive_intent_identity_mismatch")
            source_binding = {
                "source_v": source_v,
                "parent2_v": projection.get("parent2_v"),
                "epoch_binding": projection.get("epoch_binding"),
                "protocol_bootstrap_receipt_digest": (
                    ((projection.get("audit_context") or {}).get(
                        "protocol_bootstrap"
                    ) or {}).get("receipt_digest")
                ),
            }
            if canonical_digest(source_binding) != identity.get(
                "source_binding_digest"
            ):
                errors.append("handoff_archive_source_binding_mismatch")
            try:
                from generation_evidence import generation_evidence_identity_errors

                errors.extend(generation_evidence_identity_errors(
                    archive.get("strength_evidence_identity"),
                    projection,
                    version=version,
                    source_v=source_v,
                ))
            except Exception as exc:
                errors.append(
                    f"handoff_archive_evidence_validation_error:{type(exc).__name__}"
                )
            stability_row = steps.get("stability_observation") or {}
            stability_plan = stability_row.get("plan")
            if stability_plan is not None and stability_plan.get(
                "strength_evidence_identity_digest"
            ) != canonical_digest(archive.get("strength_evidence_identity")):
                errors.append("handoff_stability_evidence_digest_mismatch")
            handoff = archive.get("post_publication_handoff") or {}
            if handoff.get("identity_digest") != record.get("identity_digest"):
                errors.append("handoff_archive_identity_mismatch")
            if state == "completed":
                finalization = archive.get("finalization") or {}
                if finalization.get("state") != "completed":
                    errors.append("handoff_archive_not_completed")
                cycle = (steps.get("cycle_annotation") or {}).get("receipt") or {}
                output = cycle.get("output") or {}
                if output.get("archive_semantic_digest") != archive_semantic_digest(
                    archive
                ):
                    errors.append("handoff_archive_semantic_digest_mismatch")
                annotation = archive.get("archivist_notes")
                if not isinstance(annotation, dict) or annotation.get(
                    "annotation_digest"
                ) != output.get("annotation_digest"):
                    errors.append("handoff_archive_annotation_mismatch")
                step_digest = canonical_digest({
                    name: (steps[name].get("receipt") or {}).get("receipt_digest")
                    for name in REQUIRED_STEPS
                })
                if finalization.get("completed_steps_digest") != step_digest:
                    errors.append("handoff_archive_step_digest_mismatch")
    return list(dict.fromkeys(errors))


def local_handoff_identity_errors(record: Any) -> list[str]:
    """Re-open local artifact, paired refs, tag tree, and signed certificate."""

    structural = validate_handoff_record(record, reopen_archive=True)
    if structural:
        return structural
    identity = record["identity"]
    version = identity["version"]
    commit_oid = identity["commit_oid"]
    errors: list[str] = []
    try:
        from evolution_infra import PROJECT_ROOT, _git
        from national_runtime_authority import build_pending_local_publication_proof
        from bot_namespace import ROLE_PARENT_SOURCE, resolve_national_bot_spec

        candidate = Path(PROJECT_ROOT) / "bots" / bot_name(version)
        fresh_proof = build_pending_local_publication_proof(candidate)
        if fresh_proof != identity["local_publication_proof"]:
            errors.append("handoff_live_local_publication_proof_mismatch")
        spec = resolve_national_bot_spec(
            candidate,
            ROLE_PARENT_SOURCE,
            repo_root=PROJECT_ROOT,
        )
        if not spec.eligible:
            errors.extend(
                f"handoff_live_published_candidate:{item}"
                for item in spec.issues[:30]
            )
        if spec.certificate_digest != identity["certificate_digest"]:
            errors.append("handoff_live_certificate_digest_mismatch")
        for name, expected in identity["local_paired_refs"].items():
            ref = f"refs/tags/{name}"
            if _git("cat-file", "-t", ref, check=False).strip() != "tag":
                errors.append(f"handoff_live_tag_not_annotated:{name}")
            if _git("rev-parse", ref, check=False).strip() != expected["object_oid"]:
                errors.append(f"handoff_live_tag_object_mismatch:{name}")
            if _git("rev-parse", f"{ref}^{{commit}}", check=False).strip() != commit_oid:
                errors.append(f"handoff_live_tag_commit_mismatch:{name}")
        remote = identity["remote_publication"]
        if remote.get("required") is not True and os.environ.get(
            "POK_ALLOW_LOCAL_ONLY_POST_PUBLICATION_HANDOFF_FOR_TESTS"
        ) != "1":
            errors.append("handoff_local_only_mode_not_explicit")
    except Exception as exc:
        errors.append(f"handoff_live_local_identity_error:{type(exc).__name__}")
    return list(dict.fromkeys(errors))


def _remote_handoff_identity_errors(record: dict[str, Any]) -> list[str]:
    identity = record["identity"]
    commit_oid = identity["commit_oid"]
    remote = identity["remote_publication"]
    if remote.get("required") is not True:
        return []
    errors: list[str] = []
    try:
        from evolution_infra import _git, _git_command_succeeds

        wanted = [f"refs/heads/{EVOLUTION_BRANCH}"]
        for name in identity["local_paired_refs"]:
            wanted.extend((f"refs/tags/{name}", f"refs/tags/{name}^{{}}"))
        raw = _git("ls-remote", "origin", *wanted)
        refs: dict[str, str] = {}
        for line in raw.splitlines():
            oid, separator, ref = line.partition("\t")
            if separator and oid and ref:
                refs[ref] = oid
        remote_main = refs.get(f"refs/heads/{EVOLUTION_BRANCH}", "")
        if not _is_hex(remote_main, 40):
            errors.append("handoff_live_remote_main_missing")
        else:
            tracking = _git(
                "rev-parse", f"refs/remotes/origin/{EVOLUTION_BRANCH}", check=False
            ).strip()
            if tracking != remote_main:
                _git(
                    "fetch", "--no-tags", "origin",
                    f"refs/heads/{EVOLUTION_BRANCH}:refs/remotes/origin/{EVOLUTION_BRANCH}",
                )
            if not _git_command_succeeds(
                "merge-base", "--is-ancestor", commit_oid, remote_main
            ):
                errors.append("handoff_commit_not_on_remote_main")
        for name, expected in remote.get("paired_refs", {}).items():
            if refs.get(f"refs/tags/{name}") != expected.get("object_oid"):
                errors.append(f"handoff_live_remote_tag_object_mismatch:{name}")
            if refs.get(f"refs/tags/{name}^{{}}") != commit_oid:
                errors.append(f"handoff_live_remote_tag_commit_mismatch:{name}")
    except Exception as exc:
        errors.append(f"handoff_live_remote_identity_error:{type(exc).__name__}")
    return list(dict.fromkeys(errors))


def live_handoff_identity_errors(record: Any) -> list[str]:
    """Perform the claim/final boundary proof, including origin."""

    local = local_handoff_identity_errors(record)
    if local:
        return local
    return _remote_handoff_identity_errors(record)


def discover_post_publication_handoffs(
    *,
    include_completed: bool = False,
) -> dict[str, Any]:
    root, _ = _paths()
    if not root.exists():
        return {"ok": True, "records": [], "issues": []}
    if root.is_symlink() or not root.is_dir():
        return {
            "ok": False,
            "records": [],
            "issues": ["handoff_directory_unsafe"],
        }
    try:
        path, _pointer, record = _load_active_pair(reopen_archive=False)
        if record is not None and record.get("state") == "completed":
            path, _pointer, record = _load_active_pair(reopen_archive=True)
    except Exception as exc:
        detail = str(exc)[:600] if isinstance(
            exc, PostPublicationHandoffError
        ) else type(exc).__name__
        return {
            "ok": False,
            "records": [],
            "issues": [f"handoff_active_read_failed:{detail}"],
        }
    if record is None or path is None:
        return {"ok": True, "records": [], "issues": []}
    if record.get("state") == "completed" and not include_completed:
        return {"ok": True, "records": [], "issues": []}
    local_errors = local_handoff_identity_errors(record)
    if local_errors:
        return {
            "ok": False,
            "records": [],
            "issues": [f"handoff_local_identity:{item}" for item in local_errors],
        }
    return {
        "ok": True,
        "records": [{**record, "_path": str(path)}],
        "issues": [],
    }


def pending_handoff_route() -> dict[str, Any]:
    discovered = discover_post_publication_handoffs()
    if not discovered["ok"]:
        return {
            "status": "blocked",
            "issues": discovered["issues"],
            "records": discovered["records"],
        }
    records = discovered["records"]
    if not records:
        return {"status": "none", "records": [], "issues": []}
    record = records[0]
    identity = record["identity"]
    durable_state = record["state"]
    effective_state = durable_state
    owner_scope = "none"
    if durable_state == "running":
        owner = record.get("owner") or {}
        claim_id = str(owner.get("claim_id") or "")
        with _ACTIVE_CLAIMS_LOCK:
            active_here = claim_id in _ACTIVE_CLAIMS
        owner_alive = _owner_alive(owner)
        current_process_owner = False
        if owner_alive and int(owner.get("pid") or 0) == os.getpid():
            try:
                current_process_owner = (
                    str(owner.get("process_start_token") or "")
                    == _process_start_token()
                )
            except Exception:
                current_process_owner = False
        if active_here:
            # The volatile claim id plus PID/start-token pair is the exact
            # same-process ownership proof.  PID equality alone is insufficient:
            # a stale/reused owner row must never be presented as our live work.
            owner_scope = (
                "current_process" if current_process_owner else "unknown"
            )
        elif owner_alive and not current_process_owner:
            owner_scope = "foreign_process"
        else:
            # A crashed owner leaves a durable running row for takeover, but it
            # is not truthful to tell operators that Archivist is still doing
            # work.  A same-process row whose volatile claim was already
            # released is likewise resumable; claim CAS intentionally permits
            # that exact case.  The next route replaces the dead/stale lease.
            effective_state = "pending"
            owner_scope = "none"
    return {
        "status": "pending",
        "version": int(identity["version"]),
        "source_v": int(identity["source_v"]),
        "workflow_run_id": identity["workflow_run_id"],
        "identity_digest": record["identity_digest"],
        "publication_id": identity["publication_id"],
        "state": effective_state,
        "durable_state": durable_state,
        "owner_scope": owner_scope,
        "record": record,
        "issues": [],
    }


def pending_handoff_route_checkpoint(route: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a non-persistent checkpoint-shaped deterministic route token."""

    route = pending_handoff_route() if route is None else dict(route)
    if route.get("status") != "pending":
        raise PostPublicationHandoffError("post_publication_handoff_route_missing")
    return {
        "post_publication_handoff_route": True,
        "post_publication_handoff_identity_digest": route["identity_digest"],
        "post_publication_id": route["publication_id"],
        "next_v": int(route["version"]),
        "source_v": int(route["source_v"]),
        "parent2_v": None,
        "workflow_run_id": str(route["workflow_run_id"]),
        "checkpoint_revision": 0,
        "stage": "archived",
    }


def _find_exact(
    version: int,
    source_v: int,
    *,
    include_completed: bool = False,
) -> tuple[Path, dict[str, Any]]:
    discovered = discover_post_publication_handoffs(
        include_completed=include_completed
    )
    records = discovered.get("records") or []
    if not discovered.get("ok") or not records:
        raise PostPublicationHandoffError(
            ";".join(
                discovered.get("issues")
                or ["post_publication_handoff_missing"]
            )
        )
    record = records[0]
    identity = record["identity"]
    if identity["version"] != version or identity["source_v"] != source_v:
        raise PostPublicationHandoffError("post_publication_handoff_subject_mismatch")
    path = Path(record["_path"])
    record = dict(record)
    record.pop("_path", None)
    return path, record


def claim_post_publication_handoff(
    version: int,
    source_v: int,
    *,
    now: float | None = None,
) -> tuple[dict[str, Any], str]:
    path, _ = _find_exact(version, source_v, include_completed=True)
    current_time = time.time() if now is None else float(now)
    if not _finite_time(current_time):
        raise PostPublicationHandoffError("handoff_claim_time_invalid")
    with _JournalLock(path):
        active_path, _pointer, record = _load_active_pair(reopen_archive=True)
        if active_path != path or record is None:
            raise PostPublicationHandoffError("handoff_active_pointer_changed")
        errors = live_handoff_identity_errors(record)
        if errors:
            raise PostPublicationHandoffError(";".join(errors))
        if record.get("state") == "completed":
            with _ACTIVE_CLAIMS_LOCK:
                _ACTIVE_CLAIMS.clear()
            try:
                pointer = _read_json(_active_pointer_path(), missing_ok=True)
                if pointer is not None and pointer.get(
                    "identity_digest"
                ) == record.get("identity_digest"):
                    os.unlink(_active_pointer_path())
                    _fsync_directory(_active_pointer_path().parent)
            except Exception:
                pass
            return record, ""
        owner = record.get("owner")
        if record.get("state") == "running" and isinstance(owner, dict):
            old_claim = str(owner.get("claim_id") or "")
            with _ACTIVE_CLAIMS_LOCK:
                active_here = old_claim in _ACTIVE_CLAIMS
            owner_alive = _owner_alive(owner)
            # A live foreign process owns the effect lease indefinitely; a
            # long Cycle Archivist LLM call must never be duplicated merely
            # because a wall-clock timeout elapsed. Same-process re-entry is
            # allowed only after the prior call released its in-memory claim.
            if active_here or (owner_alive and int(owner.get("pid")) != os.getpid()):
                raise PostPublicationHandoffError("post_publication_handoff_already_running")
        _reprove_operational_steps(record)
        claim_id = uuid.uuid4().hex
        owner = {
            "claim_id": claim_id,
            "pid": os.getpid(),
            "process_start_token": _process_start_token(),
            "claimed_at": current_time,
            "heartbeat_at": current_time,
        }
        record["state"] = "running"
        record["owner"] = owner
        record["revision"] = record["revision"] + 1
        record["updated_at"] = current_time
        record["last_error"] = None
        record["record_digest"] = _record_digest(record)
        _atomic_write(path, record)
        with _ACTIVE_CLAIMS_LOCK:
            _ACTIVE_CLAIMS.add(claim_id)
    return record, claim_id


def release_post_publication_handoff_claim(
    version: int,
    source_v: int,
    claim_id: str,
    *,
    error: str | None = None,
) -> None:
    if not claim_id:
        return
    try:
        with _JournalLock(_active_pointer_path()):
            path, _pointer, record = _load_active_pair(reopen_archive=False)
            if path is None or record is None:
                return
            identity = record.get("identity") or {}
            if identity.get("version") != version or identity.get("source_v") != source_v:
                return
            if record.get("state") == "completed":
                pass
            elif (
                record.get("state") == "running"
                and (record.get("owner") or {}).get("claim_id") == claim_id
            ):
                record["state"] = "pending"
                record["owner"] = None
                record["last_error"] = str(error or "")[:1000] or None
                record["updated_at"] = time.time()
                record["revision"] = record["revision"] + 1
                record["record_digest"] = _record_digest(record)
                _atomic_write(path, record)
    except Exception:
        # The original required failure remains the caller's result.  A failed
        # diagnostic update must not replace or falsely complete it.
        return
    finally:
        # The in-process lease must never outlive the call that owns it.  A
        # durable release failure intentionally leaves the journal in
        # ``running`` so recovery can see the interrupted effect, but retaining
        # this volatile claim would make same-process recovery impossible.
        with _ACTIVE_CLAIMS_LOCK:
            _ACTIVE_CLAIMS.discard(str(claim_id))


def complete_handoff_step(
    version: int,
    source_v: int,
    claim_id: str,
    step: str,
    output: dict[str, Any],
) -> dict[str, Any]:
    if step not in REQUIRED_STEPS:
        raise PostPublicationHandoffError(f"unknown_handoff_step:{step}")
    path, _ = _find_exact(version, source_v)
    with _JournalLock(path):
        active_path, _pointer, record = _load_active_pair(reopen_archive=True)
        if active_path != path or record is None:
            raise PostPublicationHandoffError("handoff_active_pointer_changed")
        errors = local_handoff_identity_errors(record)
        if errors:
            raise PostPublicationHandoffError(";".join(errors))
        owner = record.get("owner") or {}
        if record.get("state") != "running" or owner.get("claim_id") != claim_id:
            raise PostPublicationHandoffError("handoff_claim_identity_mismatch")
        existing = (record.get("steps") or {}).get(step) or {}
        try:
            normalized_output = json.loads(json.dumps(
                output, ensure_ascii=False, sort_keys=True, allow_nan=False
            ))
        except Exception as exc:
            raise PostPublicationHandoffError(
                f"handoff_step_output_invalid:{type(exc).__name__}"
            ) from exc
        if not isinstance(normalized_output, dict):
            raise PostPublicationHandoffError("handoff_step_output_not_object")
        if existing.get("status") == "completed":
            existing_output = (existing.get("receipt") or {}).get("output")
            if existing_output != normalized_output:
                raise PostPublicationHandoffError(
                    "handoff_completed_step_output_mismatch"
                )
            return record
        step_index = REQUIRED_STEPS.index(step)
        incomplete_predecessors = [
            name
            for name in REQUIRED_STEPS[:step_index]
            if record["steps"][name].get("status") != "completed"
        ]
        if incomplete_predecessors:
            raise PostPublicationHandoffError(
                "handoff_step_order_violation:" + ",".join(incomplete_predecessors)
            )
        plan_digest = (
            existing.get("plan_digest")
            if existing.get("status") == "planned"
            else None
        )
        if plan_digest is None or not isinstance(existing.get("plan"), dict):
            raise PostPublicationHandoffError(
                "handoff_step_must_be_planned_before_completion"
            )
        if normalized_output.get("plan_digest") != plan_digest:
            raise PostPublicationHandoffError(
                "handoff_step_output_plan_binding_mismatch"
            )
        contract_errors = _step_output_contract_errors(
            step,
            normalized_output,
            existing["plan"],
            plan_digest,
            record["identity"],
        )
        if contract_errors:
            raise PostPublicationHandoffError(";".join(contract_errors))
        payload = {
            "schema_version": 1,
            "step": step,
            "publication_id": record["identity"]["publication_id"],
            "completed_at": time.time(),
            "plan_digest": plan_digest,
            "output": normalized_output,
        }
        completed_row = {
            "status": "completed",
            "receipt": _receipt(payload),
        }
        if plan_digest is not None:
            completed_row.update({
                "plan": existing["plan"],
                "plan_digest": plan_digest,
            })
        record["steps"][step] = completed_row
        owner["heartbeat_at"] = time.time()
        record["owner"] = owner
        record["revision"] = record["revision"] + 1
        record["updated_at"] = time.time()
        record["record_digest"] = _record_digest(record)
        _atomic_write(path, record)
    return record


def plan_handoff_step(
    version: int,
    source_v: int,
    claim_id: str,
    step: str,
    plan: dict[str, Any],
) -> dict[str, Any]:
    """Durably bind a non-idempotent effect preimage before executing it."""

    if step not in REQUIRED_STEPS:
        raise PostPublicationHandoffError(f"unknown_handoff_step:{step}")
    path, _ = _find_exact(version, source_v)
    with _JournalLock(path):
        active_path, _pointer, record = _load_active_pair(reopen_archive=True)
        if active_path != path or record is None:
            raise PostPublicationHandoffError("handoff_active_pointer_changed")
        errors = local_handoff_identity_errors(record)
        if errors:
            raise PostPublicationHandoffError(";".join(errors))
        if (
            record.get("state") != "running"
            or (record.get("owner") or {}).get("claim_id") != claim_id
        ):
            raise PostPublicationHandoffError("handoff_claim_identity_mismatch")
        row = record["steps"][step]
        if not isinstance(plan, dict):
            raise PostPublicationHandoffError("handoff_step_plan_not_object")
        normalized = json.loads(json.dumps(
            plan, ensure_ascii=False, sort_keys=True, allow_nan=False
        ))
        contract_errors = _step_plan_contract_errors(
            step, normalized, record["identity"]
        )
        if step == "stability_observation":
            archive = _read_json(_archive_path(version))
            if normalized.get(
                "strength_evidence_identity_digest"
            ) != canonical_digest(archive.get("strength_evidence_identity")):
                contract_errors.append(
                    "handoff_stability_evidence_digest_mismatch"
                )
        elif step == "archive_rotation":
            try:
                from evolution_infra import validate_archive_rotation_plan

                validate_archive_rotation_plan(
                    normalized,
                    version=version,
                    publication_id=record["identity"]["publication_id"],
                )
            except Exception as exc:
                contract_errors.append(
                    "handoff_archive_rotation_plan_live_validation_failed:"
                    f"{type(exc).__name__}:{str(exc)[:200]}"
                )
        elif step == "log_cleanup":
            try:
                from tool_commit import _validate_strict_log_cleanup_plan

                _validate_strict_log_cleanup_plan(
                    normalized,
                    expected_handoff_version=version,
                    expected_publication_id=record["identity"]["publication_id"],
                )
            except Exception as exc:
                contract_errors.append(
                    "handoff_log_cleanup_plan_live_validation_failed:"
                    f"{type(exc).__name__}:{str(exc)[:200]}"
                )
        if contract_errors:
            raise PostPublicationHandoffError(";".join(contract_errors))
        plan_digest = canonical_digest(normalized)
        if row.get("status") == "completed":
            if (row.get("receipt") or {}).get("plan_digest") != plan_digest:
                raise PostPublicationHandoffError("handoff_completed_plan_mismatch")
            return record
        if row.get("status") == "planned":
            if row.get("plan") != normalized or row.get("plan_digest") != plan_digest:
                raise PostPublicationHandoffError("handoff_step_plan_mismatch")
            return record
        step_index = REQUIRED_STEPS.index(step)
        if any(
            record["steps"][name].get("status") != "completed"
            for name in REQUIRED_STEPS[:step_index]
        ):
            raise PostPublicationHandoffError("handoff_step_plan_order_violation")
        record["steps"][step] = {
            "status": "planned",
            "plan": normalized,
            "plan_digest": plan_digest,
        }
        record["revision"] = record["revision"] + 1
        record["updated_at"] = time.time()
        record["record_digest"] = _record_digest(record)
        _atomic_write(path, record)
    return record


def complete_post_publication_handoff(
    version: int,
    source_v: int,
    claim_id: str,
) -> dict[str, Any]:
    path, _ = _find_exact(version, source_v)
    with _JournalLock(path):
        active_path, pointer, record = _load_active_pair(reopen_archive=True)
        if active_path != path or pointer is None or record is None:
            raise PostPublicationHandoffError("handoff_active_pointer_changed")
        live_errors = live_handoff_identity_errors(record)
        if live_errors:
            raise PostPublicationHandoffError(";".join(live_errors))
        owner = record.get("owner") or {}
        if record.get("state") != "running" or owner.get("claim_id") != claim_id:
            raise PostPublicationHandoffError("handoff_claim_identity_mismatch")
        errors: list[str] = []
        for name in REQUIRED_STEPS:
            errors.extend(_step_receipt_errors(
                name,
                (record.get("steps") or {}).get(name),
                record["identity"],
            ))
        if errors:
            raise PostPublicationHandoffError(";".join(errors))
        _reprove_operational_steps(record)
        _reprove_external_steps(record)
        archive_path = _archive_path(version)
        archive = _read_json(archive_path)
        cycle = record["steps"]["cycle_annotation"]["receipt"]["output"]
        if cycle.get("archive_semantic_digest") != archive_semantic_digest(archive):
            raise PostPublicationHandoffError("archive_semantic_digest_mismatch")
        step_digest = canonical_digest({
            name: record["steps"][name]["receipt"]["receipt_digest"]
            for name in REQUIRED_STEPS
        })
        archive["finalization"] = {
            "state": "completed",
            "identity_digest": record["identity_digest"],
            "completed_steps_digest": step_digest,
        }
        archive["post_publication_handoff"]["state"] = "completed"
        _atomic_write(archive_path, archive)
        record["state"] = "completed"
        record["owner"] = None
        record["revision"] = record["revision"] + 1
        record["updated_at"] = time.time()
        record["last_error"] = None
        record["record_digest"] = _record_digest(record)
        _atomic_write(path, record)
        final_errors = validate_handoff_record(record)
        if final_errors:
            raise PostPublicationHandoffError(";".join(final_errors))
        # The completed archive/record pair is the completion authority.  The
        # active pointer is only a crash-recovery index and may be removed
        # afterwards; a failed cleanup never rolls completion back.
        try:
            current_pointer = _read_json(_active_pointer_path(), missing_ok=True)
            if current_pointer == pointer:
                os.unlink(_active_pointer_path())
                _fsync_directory(_active_pointer_path().parent)
        except Exception:
            pass
    with _ACTIVE_CLAIMS_LOCK:
        _ACTIVE_CLAIMS.discard(claim_id)
    return record


def write_archive_annotation(
    version: int,
    source_v: int,
    claim_id: str,
    annotation: dict[str, Any],
) -> dict[str, Any]:
    """Durably attach exactly one identity-bound Cycle Archivist annotation."""

    path, _ = _find_exact(version, source_v)
    with _JournalLock(path):
        active_path, _pointer, record = _load_active_pair(reopen_archive=True)
        if active_path != path or record is None:
            raise PostPublicationHandoffError("handoff_active_pointer_changed")
        local_errors = local_handoff_identity_errors(record)
        if local_errors:
            raise PostPublicationHandoffError(";".join(local_errors))
        if (
            record.get("state") != "running"
            or (record.get("owner") or {}).get("claim_id") != claim_id
        ):
            raise PostPublicationHandoffError("handoff_claim_identity_mismatch")
        archive_path = _archive_path(version)
        archive = _read_json(archive_path)
        from cycle_archivist import annotation_identity_errors

        annotation_errors = annotation_identity_errors(
            annotation,
            archive,
            version=version,
            source_v=source_v,
        )
        if annotation_errors:
            raise PostPublicationHandoffError(
                "archive_annotation_identity_invalid:"
                + ";".join(annotation_errors[:20])
            )
        existing = archive.get("archivist_notes")
        if existing is not None and existing != annotation:
            raise PostPublicationHandoffError("archive_annotation_mismatch")
        archive["archivist_notes"] = annotation
        _atomic_write(archive_path, archive)
        verified = _read_json(archive_path)
        if verified.get("archivist_notes") != annotation:
            raise PostPublicationHandoffError("archive_annotation_not_durable")
        return {
            "annotation_digest": str(annotation.get("annotation_digest") or ""),
            "archive_semantic_digest": archive_semantic_digest(verified),
        }


def load_archive_snapshot(version: int) -> dict[str, Any]:
    return _read_json(_archive_path(int(version)))


__all__ = [
    "PostPublicationHandoffError",
    "REQUIRED_STEPS",
    "archive_semantic_digest",
    "claim_post_publication_handoff",
    "complete_handoff_step",
    "complete_post_publication_handoff",
    "discover_post_publication_handoffs",
    "ensure_post_publication_handoff",
    "load_archive_snapshot",
    "local_handoff_identity_errors",
    "live_handoff_identity_errors",
    "pending_handoff_route",
    "pending_handoff_route_checkpoint",
    "plan_handoff_step",
    "publishing_checkpoint_projection",
    "release_post_publication_handoff_claim",
    "validate_handoff_record",
    "write_archive_annotation",
]
