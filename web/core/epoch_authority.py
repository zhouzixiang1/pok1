"""Canonical runtime projection for the strict national-policy epoch.

Version tags, the one-time reset receipt, and the strict checkpoint envelope
are different kinds of evidence.  Status pages and schedulers previously
combined them ad hoc, which allowed a retired ``abandoned_versions.jsonl`` or
an unbound v155 checkpoint to make the fresh target appear to be v168.  This
module keeps those authorities separate and exposes one read-only projection.

The archived v142 tag is a numeric high-water only.  Until either the reset
receipt validates or a strict v143+ tag exists, mutable runtime files belong to
the retired epoch and cannot reserve version numbers or describe an active
generation.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Literal, TypeAlias

from bot_namespace import (
    ARCHIVED_VERSION_HIGH_WATER,
    EVALUATION_EPOCH,
    FIRST_STRICT_POLICY_VERSION,
    parse_bot_version,
    strict_generation_identity,
)
from first_strict_control import CONTROL_ID as FIRST_STRICT_CONTROL_ID


RESET_COMMAND = (
    "python scripts/reset_national_tcp_policy_epoch.py --execute "
    "--acknowledge-runtime-checkout"
)
FIRST_STRICT_BOOTSTRAP_COMMAND = (
    "python scripts/official_certify.py bootstrap-first-strict "
    f"bots/national_v{FIRST_STRICT_POLICY_VERSION} "
    f"--control-id {FIRST_STRICT_CONTROL_ID} "
    "--acknowledge-one-time-first-strict-control --wait-if-busy"
)
FIRST_STRICT_BOOTSTRAP_RETRY_COMMAND = f"{FIRST_STRICT_BOOTSTRAP_COMMAND} --force"
FIRST_STRICT_FINALIZE_COMMAND = (
    "python scripts/official_certify.py finalize-first-strict "
    "--acknowledge-publish-first-strict"
)
FIRST_STRICT_CERTIFICATION_PROFILE = "first_strict_control_v1"
RUNTIME_RECONCILIATION_CLAIM_FILENAME = (
    "policy_epoch_reconciliation_claim.json"
)
RUNTIME_RECONCILIATION_COMMAND = (
    "python scripts/reconcile_national_policy_epoch.py --execute "
    "--acknowledge-runtime-checkout "
    "--quarantine-legacy-ledger-and-abandon-checkpoint"
)
RECORDED_ABANDON_FINALIZE_COMMAND = (
    "python scripts/reconcile_national_policy_epoch.py --execute "
    "--acknowledge-runtime-checkout "
    "--finalize-recorded-abandon-checkpoint"
)

EpochState: TypeAlias = Literal[
    "reset_required",
    "reset_evidence_requires_recovery",
    "version_authority_requires_recovery",
    "epoch_authority_unavailable",
    "runtime_reconciliation_in_progress",
    "publication_recovery_ready",
    "fresh_bootstrap_ready",
    "strict_published",
]
EpochOperatorAction: TypeAlias = Literal[
    "execute_policy_epoch_reset",
    "inspect_policy_epoch_reset_evidence",
    "inspect_strict_version_authority",
    "inspect_epoch_authority",
    "inspect_runtime_reconciliation_claim",
    "archive_incompatible_checkpoint",
    "operator_reconcile_checkpoint",
    "quarantine_legacy_ledger_and_abandon_checkpoint",
    "complete_runtime_reconciliation",
    "finalize_recorded_abandon_checkpoint",
    "run_first_strict_official_certification",
]
IgnoredCheckpointReason: TypeAlias = Literal[
    "checkpoint_unreadable_or_not_object",
    "checkpoint_not_bound_to_strict_epoch",
    "runtime_reconciliation_in_progress",
    "abandon_receipt_ledger_requires_reconciliation",
]


def epoch_stream_authority_digest(projection: dict[str, Any]) -> str | None:
    """Return the exact digest that owns one evolution SSE replay ring.

    The reset receipt alone is not enough: publishing a new strict bot changes
    both the version authority and active pool while retaining that receipt.
    Binding browser controllers and the backend replay ring to this one digest
    prevents a reconnect after publication from replaying the preceding
    generation's process-memory events.
    """

    if not isinstance(projection, dict):
        return None
    receipt_digest = projection.get("reset_receipt_digest")
    high_water = projection.get("version_authority_high_water")
    active_bots = projection.get("active_bots")
    state = projection.get("state")
    if (
        projection.get("evaluation_epoch") != EVALUATION_EPOCH
        or projection.get("initialized") is not True
        or projection.get("reset_receipt_valid") is not True
        or state not in {"fresh_bootstrap_ready", "strict_published"}
        or not isinstance(receipt_digest, str)
        or len(receipt_digest) != 64
        or any(char not in "0123456789abcdef" for char in receipt_digest)
        or not isinstance(high_water, int)
        or isinstance(high_water, bool)
        or high_water < 0
        or not isinstance(active_bots, list)
    ):
        return None
    canonical_bots: list[str] = []
    for name in active_bots:
        if not isinstance(name, str):
            return None
        version = parse_bot_version(name)
        if version is None or name != f"national_v{version}":
            return None
        canonical_bots.append(name)
    if len(set(canonical_bots)) != len(canonical_bots):
        return None
    payload = {
        "schema_version": 1,
        "kind": "national-tcp-evolution-stream-authority",
        "evaluation_epoch": EVALUATION_EPOCH,
        "epoch_state": state,
        "reset_receipt_digest": receipt_digest,
        "version_authority_high_water": high_water,
        "active_bots": sorted(canonical_bots),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def first_strict_operator_transition(
    checkpoint: dict[str, Any] | None,
    *,
    state: str = "bootstrap_required",
    reason: str | None = None,
    job_id: str | None = None,
    certificate_digest: str | None = None,
) -> dict[str, Any]:
    """Build the digest-bound operator handoff for the parked strict v143.

    Epoch authority owns the checkpoint identity and the initial manual
    bootstrap requirement.  The certification route may refine this record to
    ``bootstrap_running``, ``bootstrap_failed`` or ``ready_to_finalize`` only
    after reopening the exact authorized durable job and certificate.
    """

    state_contract = {
        "bootstrap_required": (
            "run_first_strict_official_certification",
            FIRST_STRICT_BOOTSTRAP_COMMAND,
            "authorized_bootstrap_job_not_started",
        ),
        "bootstrap_running": (
            "wait_for_first_strict_official_certification",
            None,
            "authorized_bootstrap_job_running",
        ),
        "bootstrap_failed": (
            "retry_first_strict_official_certification",
            FIRST_STRICT_BOOTSTRAP_RETRY_COMMAND,
            "authorized_bootstrap_job_failed",
        ),
        "ready_to_finalize": (
            "finalize_first_strict_publication",
            FIRST_STRICT_FINALIZE_COMMAND,
            "bootstrap_certificate_and_authorization_verified",
        ),
    }
    if state not in state_contract:
        raise ValueError(f"invalid first-strict operator transition: {state}")
    action, command, default_reason = state_contract[state]
    reason = str(reason or default_reason)
    def _hex64(value: object) -> bool:
        return bool(
            isinstance(value, str)
            and len(value) == 64
            and all(char in "0123456789abcdef" for char in value)
        )

    if state == "bootstrap_required" and (job_id is not None or certificate_digest is not None):
        raise ValueError("bootstrap_required transition cannot bind a job or certificate")
    if job_id is not None and not _hex64(job_id):
        raise ValueError("operator transition durable job id must be a hex digest")
    if state == "bootstrap_running" and not _hex64(job_id):
        raise ValueError("bootstrap_running transition requires a durable job id")
    if state != "ready_to_finalize" and certificate_digest is not None:
        raise ValueError("only ready_to_finalize may bind a certificate")
    if state == "ready_to_finalize" and (
        not _hex64(job_id)
        or not _hex64(certificate_digest)
    ):
        raise ValueError(
            "ready_to_finalize transition requires durable job and certificate digests"
        )
    checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
    audit_context = checkpoint.get("audit_context")
    parked = (
        audit_context.get("official_bootstrap_request")
        if isinstance(audit_context, dict)
        else None
    )
    parked = parked if isinstance(parked, dict) else {}
    payload: dict[str, Any] = {
        "schema_version": 1,
        "kind": "first-strict-official-operator-transition",
        "state": state,
        "action": str(action),
        "command": command,
        "reason": reason,
        "certification_profile": FIRST_STRICT_CERTIFICATION_PROFILE,
        "opponent_authority": "system_control",
        "strength_evidence_weight": 0,
        "strategy_evidence_weight": 0,
        "evaluation_epoch": EVALUATION_EPOCH,
        "workflow_run_id": checkpoint.get("workflow_run_id"),
        "candidate_version": checkpoint.get("next_v"),
        "source_v": checkpoint.get("source_v"),
        "checkpoint_stage": checkpoint.get("stage"),
        "checkpoint_revision": checkpoint.get("checkpoint_revision"),
        "candidate_hash": parked.get("candidate_hash"),
        "parked_request_digest": parked.get("request_digest"),
    }
    if job_id is not None:
        payload["job_id"] = job_id
    if certificate_digest is not None:
        payload["certificate_digest"] = certificate_digest
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    payload["transition_digest"] = hashlib.sha256(encoded).hexdigest()
    return payload


class PolicyEpochInitializationRequired(RuntimeError):
    """Raised when a mutating runtime is launched before the epoch reset.

    The attached projection is the same canonical state exposed by the status
    API.  Launchers must not manufacture a second interpretation of the reset
    receipt or version tags, and must not persist an event while the results
    directory still belongs to the retired epoch.
    """

    def __init__(self, operation: str, state: dict[str, Any]) -> None:
        self.operation = str(operation)
        self.state = dict(state)
        super().__init__(
            f"{self.operation} requires initialized {state.get('evaluation_epoch')}: "
            f"{state.get('state')}"
        )


def _canonical_object_digest(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _claim_payload_digest(payload: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in payload.items() if key != "claim_digest"}
    return _canonical_object_digest(unsigned)


_ABANDON_CLAIM_MAX_TREE_ENTRIES = 10_000
_ABANDON_CLAIM_MAX_TREE_BYTES = 64 * 1024 * 1024
_ABANDON_TRANSACTION_ROOT_CONTRACT = (
    "RESULTS_DIR/policy_epoch_abandon_transactions/<transaction_id>"
)
_ABANDON_QUARANTINE_LEAF = "candidate"
_ABANDON_LEDGER_PATH_CONTRACT = "RESULTS_DIR/abandoned_versions.jsonl"
_ABANDON_CHECKPOINT_KEYS = frozenset({
    "digest",
    "next_v",
    "source_v",
    "stage",
    "workflow_run_id",
    "checkpoint_revision",
})
_ABANDON_CANDIDATE_KEYS = frozenset({
    "present",
    "path",
    "manifest_digest",
    "entry_count",
    "total_bytes",
})
_ABANDON_QUARANTINE_KEYS = frozenset({"root_contract", "leaf"})
_ABANDON_GIT_STATE_KEYS = frozenset({
    "head",
    "tracked_worktree_clean",
    "candidate_tracked",
    "publication_refs",
})
_ABANDON_LEDGER_KEYS = frozenset({
    "path_contract",
    "prior_receipt_count",
    "prior_receipt_head_digest",
    "receipt_identity",
})
_ABANDON_RECEIPT_IDENTITY_KEYS = frozenset({
    "version",
    "source_v",
    "checkpoint_stage",
    "workflow_run_id",
    "checkpoint_revision",
    "reason",
})
_SCHEMA2_ABANDON_CLAIM_KEYS = frozenset({
    "schema_version",
    "kind",
    "evaluation_epoch",
    "git_head",
    "git_state",
    "checkout_role",
    "transaction_id",
    "checkpoint",
    "abandon_reason",
    "candidate",
    "quarantine",
    "ledger",
    "claim_digest",
})


def _is_hex_digest(value: object, *, lengths: tuple[int, ...] = (64,)) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) in lengths
        and re.fullmatch(r"[0-9a-f]+", value) is not None
    )


def schema2_abandon_receipt_identity(
    checkpoint: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    """Return the exact six fields that identify one abandon-ledger row."""

    return {
        "version": checkpoint.get("next_v"),
        "source_v": checkpoint.get("source_v"),
        "checkpoint_stage": checkpoint.get("stage"),
        "workflow_run_id": checkpoint.get("workflow_run_id"),
        "checkpoint_revision": checkpoint.get("checkpoint_revision"),
        "reason": str(reason),
    }


def schema2_abandon_quarantine_contract() -> dict[str, str]:
    """Return the stable, non-self-referential quarantine path contract."""

    return {
        "root_contract": _ABANDON_TRANSACTION_ROOT_CONTRACT,
        "leaf": _ABANDON_QUARANTINE_LEAF,
    }


def schema2_abandon_transaction_preimage(claim: dict[str, Any]) -> dict[str, Any]:
    """Build the complete transaction-id preimage from a schema-2 claim."""

    return {
        "checkpoint": claim.get("checkpoint"),
        "reason": claim.get("abandon_reason"),
        "candidate": claim.get("candidate"),
        "quarantine": claim.get("quarantine"),
        "ledger": claim.get("ledger"),
        "git_state": claim.get("git_state"),
    }


def validate_schema2_abandon_claim_structure(
    claim: dict[str, Any],
) -> dict[str, Any]:
    """Validate the exact self-contained schema-2 abandon claim envelope.

    A digest alone is not authenticity: an operator can accidentally (or an
    attacker can deliberately) re-sign an altered object.  This validator
    therefore reconstructs every canonical identity and rejects all unknown,
    omitted, unbounded, or path-bearing variants before callers inspect live
    filesystem state.
    """

    if not isinstance(claim, dict) or set(claim) != _SCHEMA2_ABANDON_CLAIM_KEYS:
        raise RuntimeError("recorded_abandon_claim_fields_invalid")
    unsigned = {key: value for key, value in claim.items() if key != "claim_digest"}
    if (
        claim.get("schema_version") != 2
        or claim.get("kind")
        != "national-policy-recorded-abandon-finalize-claim"
        or claim.get("evaluation_epoch") != EVALUATION_EPOCH
        or claim.get("checkout_role") != "autonomous_evolution_runtime"
        or not _is_hex_digest(claim.get("claim_digest"))
        or claim.get("claim_digest") != _claim_payload_digest(unsigned)
    ):
        raise RuntimeError("recorded_abandon_claim_envelope_invalid")

    checkpoint = claim.get("checkpoint")
    if not isinstance(checkpoint, dict) or set(checkpoint) != _ABANDON_CHECKPOINT_KEYS:
        raise RuntimeError("recorded_abandon_checkpoint_identity_invalid")
    next_v = checkpoint.get("next_v")
    source_v = checkpoint.get("source_v")
    revision = checkpoint.get("checkpoint_revision")
    stage = checkpoint.get("stage")
    workflow_run_id = checkpoint.get("workflow_run_id")
    if (
        not _is_hex_digest(checkpoint.get("digest"))
        or type(next_v) is not int
        or next_v < FIRST_STRICT_POLICY_VERSION
        or type(source_v) is not int
        or source_v < 0
        or type(revision) is not int
        or revision < 1
        or not isinstance(stage, str)
        or not stage
        or len(stage.encode("utf-8")) > 256
        or not isinstance(workflow_run_id, str)
        or not workflow_run_id
        or len(workflow_run_id.encode("utf-8")) > 1024
    ):
        raise RuntimeError("recorded_abandon_checkpoint_identity_invalid")

    reason = claim.get("abandon_reason")
    if (
        not isinstance(reason, str)
        or not reason
        or reason != reason.strip()
        or len(reason.encode("utf-8")) > 4 * 1024
    ):
        raise RuntimeError("recorded_abandon_reason_invalid")

    candidate = claim.get("candidate")
    if not isinstance(candidate, dict) or set(candidate) != _ABANDON_CANDIDATE_KEYS:
        raise RuntimeError("recorded_abandon_candidate_identity_invalid")
    present = candidate.get("present")
    entry_count = candidate.get("entry_count")
    total_bytes = candidate.get("total_bytes")
    if (
        type(present) is not bool
        or candidate.get("path") != f"bots/national_v{next_v}"
        or type(entry_count) is not int
        or not 0 <= entry_count <= _ABANDON_CLAIM_MAX_TREE_ENTRIES
        or type(total_bytes) is not int
        or not 0 <= total_bytes <= _ABANDON_CLAIM_MAX_TREE_BYTES
    ):
        raise RuntimeError("recorded_abandon_candidate_identity_invalid")
    if present:
        if not _is_hex_digest(candidate.get("manifest_digest")):
            raise RuntimeError("recorded_abandon_candidate_manifest_invalid")
    elif (
        candidate.get("manifest_digest") is not None
        or entry_count != 0
        or total_bytes != 0
    ):
        raise RuntimeError("recorded_abandon_absent_candidate_invalid")

    quarantine = claim.get("quarantine")
    if (
        not isinstance(quarantine, dict)
        or set(quarantine) != _ABANDON_QUARANTINE_KEYS
        or quarantine != schema2_abandon_quarantine_contract()
    ):
        raise RuntimeError("recorded_abandon_quarantine_contract_invalid")

    git_state = claim.get("git_state")
    if not isinstance(git_state, dict) or set(git_state) != _ABANDON_GIT_STATE_KEYS:
        raise RuntimeError("recorded_abandon_git_state_invalid")
    expected_refs = {
        f"national-bot-v{next_v}": False,
        f"national-high-water-v{next_v}": False,
    }
    if (
        not _is_hex_digest(git_state.get("head"), lengths=(40, 64))
        or claim.get("git_head") != git_state.get("head")
        or git_state.get("tracked_worktree_clean") is not True
        or git_state.get("candidate_tracked") is not False
        or git_state.get("publication_refs") != expected_refs
    ):
        raise RuntimeError("recorded_abandon_git_state_invalid")

    ledger = claim.get("ledger")
    if not isinstance(ledger, dict) or set(ledger) != _ABANDON_LEDGER_KEYS:
        raise RuntimeError("recorded_abandon_ledger_binding_invalid")
    prior_count = ledger.get("prior_receipt_count")
    prior_head = ledger.get("prior_receipt_head_digest")
    receipt_identity = ledger.get("receipt_identity")
    if (
        ledger.get("path_contract") != _ABANDON_LEDGER_PATH_CONTRACT
        or type(prior_count) is not int
        or not 0 <= prior_count <= 1_000_000
        or (prior_head is not None and not _is_hex_digest(prior_head))
        or (prior_count == 0) != (prior_head is None)
        or not isinstance(receipt_identity, dict)
        or set(receipt_identity) != _ABANDON_RECEIPT_IDENTITY_KEYS
        or receipt_identity != schema2_abandon_receipt_identity(checkpoint, reason)
    ):
        raise RuntimeError("recorded_abandon_ledger_binding_invalid")

    expected_transaction_id = _canonical_object_digest(
        schema2_abandon_transaction_preimage(claim)
    )
    if (
        not _is_hex_digest(claim.get("transaction_id"))
        or claim.get("transaction_id") != expected_transaction_id
    ):
        raise RuntimeError("recorded_abandon_transaction_id_invalid")
    return claim


def validate_schema2_abandon_ledger_history(
    claim: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    require_active_head: bool,
) -> dict[str, Any] | None:
    """Cross-bind a claim to its exact prior chain and six-field receipt."""

    validate_schema2_abandon_claim_structure(claim)
    if not isinstance(rows, list):
        raise RuntimeError("recorded_abandon_ledger_history_invalid")
    ledger = claim["ledger"]
    prior_count = ledger["prior_receipt_count"]
    prior_head = ledger["prior_receipt_head_digest"]
    if len(rows) < prior_count:
        raise RuntimeError("recorded_abandon_ledger_prefix_missing")
    observed_prior_head = (
        rows[prior_count - 1].get("receipt_digest") if prior_count else None
    )
    if observed_prior_head != prior_head:
        raise RuntimeError("recorded_abandon_ledger_prefix_changed")
    identity = ledger["receipt_identity"]
    matches = [
        row
        for row in rows
        if all(row.get(key) == value for key, value in identity.items())
    ]
    if len(matches) > 1:
        raise RuntimeError("recorded_abandon_receipt_not_unique")
    receipt = matches[0] if matches else None
    if receipt is not None:
        try:
            receipt_index = next(index for index, row in enumerate(rows) if row is receipt)
        except StopIteration as exc:  # pragma: no cover - defensive
            raise RuntimeError("recorded_abandon_receipt_history_invalid") from exc
        if (
            receipt_index != prior_count
            or receipt.get("previous_receipt_digest") != prior_head
        ):
            raise RuntimeError("recorded_abandon_receipt_history_invalid")
    if require_active_head:
        expected_count = prior_count + (1 if receipt is not None else 0)
        if len(rows) != expected_count:
            raise RuntimeError("recorded_abandon_active_ledger_advanced")
    return receipt


def validate_schema2_abandon_finalize_receipt(
    claim: dict[str, Any],
    receipt: dict[str, Any],
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Validate a completed transaction without pinning today's Git HEAD.

    Historical completion remains valid after later code commits and later
    legitimate abandon rows.  Its immutable claim, exact ledger prefix/row and
    finalize receipt stay bound; no live filesystem bytes are adopted.
    """

    validate_schema2_abandon_claim_structure(claim)
    abandon_receipt = validate_schema2_abandon_ledger_history(
        claim,
        rows,
        require_active_head=False,
    )
    required = {
        "schema_version",
        "kind",
        "evaluation_epoch",
        "mode",
        "claim_digest",
        "workflow_run_id",
        "abandon_receipt_digest",
        "checkpoint_cleared",
        "candidate_state",
        "candidate_manifest_digest",
        "receipt_digest",
    }
    unsigned = {
        key: value for key, value in receipt.items() if key != "receipt_digest"
    } if isinstance(receipt, dict) else {}
    expected_state = "quarantine" if claim["candidate"]["present"] else "absent"
    if (
        not isinstance(receipt, dict)
        or set(receipt) != required
        or receipt.get("schema_version") != 2
        or receipt.get("kind") != "national-policy-recorded-abandon-finalize"
        or receipt.get("evaluation_epoch") != EVALUATION_EPOCH
        or receipt.get("mode") != "execute"
        or receipt.get("claim_digest") != claim.get("claim_digest")
        or receipt.get("workflow_run_id")
        != claim["checkpoint"]["workflow_run_id"]
        or abandon_receipt is None
        or receipt.get("abandon_receipt_digest")
        != abandon_receipt.get("receipt_digest")
        or receipt.get("checkpoint_cleared") is not True
        or receipt.get("candidate_state") != expected_state
        or receipt.get("candidate_manifest_digest")
        != claim["candidate"]["manifest_digest"]
        or not _is_hex_digest(receipt.get("receipt_digest"))
        or receipt.get("receipt_digest") != _canonical_object_digest(unsigned)
    ):
        raise RuntimeError("recorded_abandon_finalize_receipt_invalid")
    return receipt


def _read_bounded_regular_json(path: Path, *, limit: int = 1024 * 1024) -> dict:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        raw = os.read(descriptor, limit + 1)
        opened_after = os.fstat(descriptor)
        live = os.lstat(path)
        if (
            len(raw) > limit
            or not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(opened_after.st_mode)
            or not stat.S_ISREG(live.st_mode)
            or opened.st_nlink != 1
            or opened_after.st_nlink != 1
            or live.st_nlink != 1
            or opened.st_size != len(raw)
            or (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
                opened.st_ctime_ns,
            ) != (
                opened_after.st_dev,
                opened_after.st_ino,
                opened_after.st_size,
                opened_after.st_mtime_ns,
                opened_after.st_ctime_ns,
            )
            or (
                opened_after.st_dev,
                opened_after.st_ino,
                opened_after.st_size,
                opened_after.st_mtime_ns,
                opened_after.st_ctime_ns,
            ) != (
                live.st_dev,
                live.st_ino,
                live.st_size,
                live.st_mtime_ns,
                live.st_ctime_ns,
            )
        ):
            raise RuntimeError("claim_json_unsafe")
    finally:
        os.close(descriptor)
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("claim_json_not_object")
    return value


def _schema2_candidate_tree_identity(path: Path) -> dict[str, Any]:
    root = Path(path)
    root_stat = os.lstat(root)
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise RuntimeError("recorded_abandon_candidate_root_unsafe")
    entries: list[dict[str, Any]] = []
    total_bytes = 0
    for child in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = child.relative_to(root).as_posix()
        if (
            len(entries) >= _ABANDON_CLAIM_MAX_TREE_ENTRIES
            or len(Path(relative).parts) > 32
        ):
            raise RuntimeError("recorded_abandon_candidate_tree_limit")
        metadata = os.lstat(child)
        if stat.S_ISLNK(metadata.st_mode):
            raise RuntimeError("recorded_abandon_candidate_symlink")
        if stat.S_ISDIR(metadata.st_mode):
            entries.append({"path": relative, "kind": "directory"})
            continue
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise RuntimeError("recorded_abandon_candidate_file_unsafe")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(child, flags)
        try:
            opened = os.fstat(descriptor)
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
                total_bytes += len(chunk)
                if total_bytes > _ABANDON_CLAIM_MAX_TREE_BYTES:
                    raise RuntimeError("recorded_abandon_candidate_tree_limit")
            opened_after = os.fstat(descriptor)
            live = os.lstat(child)
            raw = b"".join(chunks)
            if (
                opened.st_nlink != 1
                or opened_after.st_nlink != 1
                or live.st_nlink != 1
                or (opened.st_dev, opened.st_ino)
                != (opened_after.st_dev, opened_after.st_ino)
                or (opened_after.st_dev, opened_after.st_ino)
                != (live.st_dev, live.st_ino)
                or (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
                != (
                    opened_after.st_size,
                    opened_after.st_mtime_ns,
                    opened_after.st_ctime_ns,
                )
                or opened.st_size != len(raw)
            ):
                raise RuntimeError("recorded_abandon_candidate_changed_while_read")
        finally:
            os.close(descriptor)
        entries.append({
            "path": relative,
            "kind": "file",
            "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        })
    return {
        "manifest_digest": _canonical_object_digest(entries),
        "entry_count": len(entries),
        "total_bytes": total_bytes,
    }


def _assert_schema2_transaction_chain_safe(
    results_dir: Path,
    transaction_dir: Path,
) -> None:
    try:
        relative = transaction_dir.relative_to(results_dir)
    except ValueError as exc:
        raise RuntimeError("recorded_abandon_transaction_path_escape") from exc
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(results_dir, flags)
    try:
        for part in relative.parts:
            if part in {"", ".", ".."}:
                raise RuntimeError("recorded_abandon_transaction_path_invalid")
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
    except OSError as exc:
        raise RuntimeError("recorded_abandon_transaction_path_unsafe") from exc
    finally:
        os.close(descriptor)


def _validate_schema2_active_claim_state(
    claim: dict[str, Any],
    *,
    results_dir: Path,
    bots_dir: Path,
    infra: Any,
) -> None:
    """Read-only live recovery validation used by the canonical epoch view."""

    validate_schema2_abandon_claim_structure(claim)
    version = int(claim["checkpoint"]["next_v"])
    current_git_state = {
        "head": infra._git("rev-parse", "HEAD"),
        "tracked_worktree_clean": infra._git(
            "status", "--porcelain", "--untracked-files=no"
        ) == "",
        "candidate_tracked": bool(infra.git_dir_is_committed(version)),
        "publication_refs": {
            f"national-bot-v{version}": bool(
                infra.git_has_publication_ref(version)
            ),
            f"national-high-water-v{version}": bool(
                infra.git_has_publication_ref(version)
            ),
        },
    }
    if current_git_state != claim["git_state"]:
        raise RuntimeError("recorded_abandon_active_git_state_changed")

    if (
        os.path.lexists(bots_dir)
        and (
            stat.S_ISLNK(os.lstat(bots_dir).st_mode)
            or not stat.S_ISDIR(os.lstat(bots_dir).st_mode)
        )
    ):
        raise RuntimeError("recorded_abandon_bots_root_unsafe")
    candidate = bots_dir / f"national_v{version}"
    transaction_dir = (
        results_dir
        / "policy_epoch_abandon_transactions"
        / claim["transaction_id"]
    )
    quarantine = transaction_dir / _ABANDON_QUARANTINE_LEAF
    _assert_schema2_transaction_chain_safe(results_dir, transaction_dir)
    transaction_claim = transaction_dir / "claim.json"
    if (
        not os.path.lexists(transaction_claim)
        or _read_bounded_regular_json(transaction_claim) != claim
    ):
        raise RuntimeError("recorded_abandon_transaction_claim_mismatch")

    source_exists = os.path.lexists(candidate)
    quarantine_exists = os.path.lexists(quarantine)
    if source_exists and quarantine_exists:
        raise RuntimeError("recorded_abandon_source_quarantine_xor_invalid")
    expected = claim["candidate"]
    if expected["present"] is False:
        if source_exists or quarantine_exists:
            raise RuntimeError("recorded_abandon_unexpected_candidate")
        phase = "absent"
    else:
        if not source_exists and not quarantine_exists:
            raise RuntimeError("recorded_abandon_claimed_candidate_missing")
        observed = _schema2_candidate_tree_identity(
            candidate if source_exists else quarantine
        )
        if any(
            observed[field] != expected[field]
            for field in ("manifest_digest", "entry_count", "total_bytes")
        ):
            raise RuntimeError("recorded_abandon_candidate_preimage_changed")
        phase = "source" if source_exists else "quarantine"

    rows = infra.load_abandoned_version_receipts(
        path=results_dir / "abandoned_versions.jsonl",
        project_root=infra.PROJECT_ROOT,
    )
    abandon_receipt = validate_schema2_abandon_ledger_history(
        claim,
        rows,
        require_active_head=True,
    )
    checkpoint_path = Path(infra.PIPELINE_STATE_FILE)
    if os.path.lexists(checkpoint_path):
        checkpoint = infra.read_pipeline_checkpoint()
        if (
            not isinstance(checkpoint, dict)
            or _canonical_object_digest(checkpoint)
            != claim["checkpoint"]["digest"]
        ):
            raise RuntimeError("recorded_abandon_active_checkpoint_changed")
        if abandon_receipt is None and phase not in {"source", "absent"}:
            raise RuntimeError("recorded_abandon_phase_invalid_before_ledger")
    else:
        if abandon_receipt is None:
            raise RuntimeError("recorded_abandon_receipt_missing_after_checkpoint_clear")
        if expected["present"] is True and phase != "quarantine":
            raise RuntimeError("recorded_abandon_source_invalid_after_checkpoint_clear")

    finalize = transaction_dir / "receipt.json"
    if os.path.lexists(finalize):
        validate_schema2_abandon_finalize_receipt(
            claim,
            _read_bounded_regular_json(finalize),
            rows,
        )


def _runtime_reconciliation_claim_status(
    path: Path,
    *,
    results_dir: Path | None = None,
    bots_dir: Path | None = None,
    infra: Any = None,
) -> dict[str, Any]:
    """Safely classify the one launch-barrier claim without executing it."""

    status: dict[str, Any] = {
        "claimed": os.path.lexists(path),
        "valid": False,
        "kind": None,
        "claim_digest": None,
        "issues": [],
    }
    if not status["claimed"]:
        return status
    try:
        claim = _read_bounded_regular_json(path, limit=16 * 1024 * 1024)
        digest = claim.get("claim_digest")
        if (
            not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or digest != _claim_payload_digest(claim)
            or claim.get("schema_version") not in {1, 2}
            or claim.get("evaluation_epoch") != EVALUATION_EPOCH
            or claim.get("checkout_role") != "autonomous_evolution_runtime"
        ):
            raise RuntimeError("claim_envelope_invalid")
        raw_kind = claim.get("kind")
        if raw_kind == "national-policy-runtime-reconciliation-claim":
            required = {
                "schema_version", "kind", "evaluation_epoch", "git_head",
                "checkout_role", "action", "archive_root", "inputs",
                "terminal_abandon", "claim_digest",
            }
            inputs = claim.get("inputs")
            terminal = claim.get("terminal_abandon")
            if (
                set(claim) != required
                or claim.get("schema_version") != 1
                or claim.get("action")
                != "quarantine_legacy_ledger_and_abandon_checkpoint"
                or not isinstance(claim.get("archive_root"), str)
                or not isinstance(inputs, dict)
                or set(inputs) != {
                    "reset_receipt_digest", "published_high_water",
                    "legacy_ledger", "checkpoint", "candidate",
                    "runtime_control", "target_successor",
                    "allocation_receipt_eligible",
                    "allocation_receipt_rejection_issues",
                }
                or not isinstance(terminal, dict)
                or set(terminal) != {"reason", "infra_failure", "timestamp"}
            ):
                raise RuntimeError("legacy_reconciliation_claim_structure_invalid")
            kind = "legacy_quarantine"
        elif raw_kind == "national-policy-recorded-abandon-finalize-claim":
            schema = claim.get("schema_version")
            checkpoint = claim.get("checkpoint")
            candidate = claim.get("candidate")
            old_required = {
                "schema_version", "kind", "evaluation_epoch", "git_head",
                "checkout_role", "checkpoint", "abandon_receipt_digest",
                "abandon_reason", "candidate", "claim_digest",
            }
            old_valid = bool(
                schema == 1
                and set(claim) == old_required
                and isinstance(checkpoint, dict)
                and set(checkpoint) == {
                    "sha256", "next_v", "source_v", "stage",
                    "workflow_run_id", "checkpoint_revision",
                }
                and isinstance(candidate, dict)
                and set(candidate) in ({
                    "path", "manifest_digest", "entry_count", "total_bytes",
                }, {
                    "path", "state", "manifest_digest", "entry_count", "total_bytes",
                })
            )
            new_valid = False
            if schema == 2:
                validate_schema2_abandon_claim_structure(claim)
                if results_dir is None or bots_dir is None or infra is None:
                    raise RuntimeError("recorded_finalize_live_authority_unavailable")
                _validate_schema2_active_claim_state(
                    claim,
                    results_dir=Path(results_dir),
                    bots_dir=Path(bots_dir),
                    infra=infra,
                )
                new_valid = True
            if not old_valid and not new_valid:
                raise RuntimeError("recorded_finalize_claim_structure_invalid")
            kind = "recorded_abandon_finalize"
        else:
            raise RuntimeError("claim_kind_unsupported")
        status.update({
            "valid": True,
            "kind": kind,
            "claim_digest": digest,
        })
    except Exception as exc:
        status["issues"] = [f"{type(exc).__name__}:{str(exc)[:160]}"]
    return status


def policy_epoch_initialization(
    *, results_dir: str | Path | None = None, current_v: int | None = None
) -> dict[str, Any]:
    """Describe whether mutable runtime data belongs to the strict epoch."""

    import evolution_infra as infra
    from system_strict_bootstrap import (
        POLICY_EPOCH_RESET_RECEIPT_FILENAME,
        load_policy_epoch_reset_receipt,
    )
    from national_runtime_authority import strict_published_bot_names

    root = Path(results_dir) if results_dir is not None else Path(infra.RESULTS_DIR)
    authority_high_water = int(
        infra.find_current_v() if current_v is None else current_v
    )
    try:
        namespace_authority = infra.version_namespace_authority()
    except Exception:
        namespace_authority = None
    namespace_snapshot_matches = bool(
        namespace_authority is not None
        and int(getattr(namespace_authority, "high_water", -1))
        == authority_high_water
    )
    unpaired_completion_versions = sorted(
        version
        for version in (
            getattr(namespace_authority, "unpaired_completion_versions", ())
            if namespace_snapshot_matches else ()
        )
        if version > authority_high_water
    )
    unpaired_high_water_versions = sorted(
        version
        for version in (
            getattr(namespace_authority, "unpaired_high_water_versions", ())
            if namespace_snapshot_matches else ()
        )
        if version > authority_high_water
    )
    unpaired_active_versions = sorted({
        *unpaired_completion_versions,
        *unpaired_high_water_versions,
    })
    receipt_path = root / POLICY_EPOCH_RESET_RECEIPT_FILENAME
    reconciliation_claim_path = root / RUNTIME_RECONCILIATION_CLAIM_FILENAME
    receipt, receipt_errors = load_policy_epoch_reset_receipt(root)
    # A tag number is immutable version authority, but it is not publication
    # authority by itself.  Initialization through publication requires the
    # complete strict artifact, tag/tree binding, and signed full-v5
    # certificate resolved by ``strict_published_bot_names``.  This prevents a
    # stray/manual national-bot-v143+ tag from bypassing the one-time reset.
    observed_strict_bots = [
        name
        for name in strict_published_bot_names()
        if (version := parse_bot_version(str(name))) is not None
        and version <= authority_high_water
    ]
    # A strict artifact scan is not independently active-bot authority.  If
    # the paired completion/high-water namespace snapshot is unavailable or
    # disagrees with the first high-water read, fail closed at this projection
    # boundary so the scheduler, prompts and UI cannot inject an otherwise
    # eligible artifact while version authority is transiently ambiguous.
    strict_bots = observed_strict_bots if namespace_snapshot_matches else []
    strict_versions = sorted({
        version
        for name in strict_bots
        if (version := parse_bot_version(str(name))) is not None
        and version >= FIRST_STRICT_POLICY_VERSION
    })
    strict_published_bot_identities = [
        strict_generation_identity(version) for version in strict_versions
    ]
    strict_published = bool(strict_bots)
    # Numeric namespace authority and executable publication authority are
    # intentionally distinct.  The paired completion/high-water resolver owns
    # the former; the exact high-water strict artifact, immutable tag/tree and
    # signed certificate must independently prove the latter.  Also reject a
    # higher eligible completion artifact whose matching high-water effect has
    # not landed, otherwise a partial publication could make the scheduler
    # allocate the same label again.
    namespace_publication_proven = bool(
        namespace_snapshot_matches
        and (
            (
                authority_high_water < FIRST_STRICT_POLICY_VERSION
                and not strict_versions
            )
            or (
                authority_high_water >= FIRST_STRICT_POLICY_VERSION
                and strict_versions
                and strict_versions[-1] == authority_high_water
            )
        )
    )
    reset_valid = receipt is not None
    partial_publication_recovery = False
    if unpaired_active_versions and namespace_authority is not None:
        try:
            partial_publication_recovery = bool(
                infra.partial_publication_checkpoint_recovery_allowed(
                    infra.read_pipeline_checkpoint(),
                    namespace_authority=namespace_authority,
                )
            )
        except Exception:
            partial_publication_recovery = False
    epoch_initialized = bool(
        namespace_snapshot_matches
        and
        (strict_published or reset_valid)
        and (namespace_publication_proven or partial_publication_recovery)
        and (not unpaired_active_versions or partial_publication_recovery)
    )
    reconciliation = _runtime_reconciliation_claim_status(
        reconciliation_claim_path,
        results_dir=root,
        bots_dir=Path(infra.BOTS_DIR),
        infra=infra,
    )
    reconciliation_claimed = bool(reconciliation["claimed"])
    initialized = epoch_initialized and not reconciliation_claimed

    if reconciliation_claimed:
        state = "runtime_reconciliation_in_progress"
        if reconciliation.get("valid") is not True:
            operator_action = "inspect_runtime_reconciliation_claim"
            operator_command = None
        elif reconciliation.get("kind") == "recorded_abandon_finalize":
            operator_action = "finalize_recorded_abandon_checkpoint"
            operator_command = RECORDED_ABANDON_FINALIZE_COMMAND
        else:
            operator_action = "complete_runtime_reconciliation"
            operator_command = RUNTIME_RECONCILIATION_COMMAND
    elif partial_publication_recovery:
        state = "publication_recovery_ready"
        operator_action = None
        operator_command = None
    elif unpaired_active_versions or not namespace_publication_proven:
        state = "version_authority_requires_recovery"
        operator_action = "inspect_strict_version_authority"
        operator_command = None
    elif strict_published:
        state: EpochState = "strict_published"
        operator_action: EpochOperatorAction | None = None
        operator_command = None
    elif reset_valid:
        state = "fresh_bootstrap_ready"
        operator_action = None
        operator_command = None
    elif os.path.lexists(receipt_path):
        # A malformed/interrupted durable claim is deliberately no-clobber.
        # The reset script will refuse a second receipt, so claiming that the
        # normal command is actionable would be misleading and unsafe.
        state = "reset_evidence_requires_recovery"
        operator_action = "inspect_policy_epoch_reset_evidence"
        operator_command = None
    elif authority_high_water >= FIRST_STRICT_POLICY_VERSION:
        # The normal reset is pinned to the v142 high-water.  A later tag with
        # no eligible published artifact is therefore an inconsistent durable
        # claim, not a safe invitation to rerun the reset command.
        state = "version_authority_requires_recovery"
        operator_action = "inspect_strict_version_authority"
        operator_command = None
    else:
        state = "reset_required"
        operator_action = "execute_policy_epoch_reset"
        operator_command = RESET_COMMAND

    return {
        "evaluation_epoch": EVALUATION_EPOCH,
        "state": state,
        "initialized": initialized,
        "epoch_initialized": epoch_initialized,
        "strict_published": strict_published,
        "strict_published_bots": strict_bots,
        "strict_published_versions": strict_versions,
        "strict_published_bot_identities": strict_published_bot_identities,
        "namespace_publication_proven": namespace_publication_proven,
        "publication_recovery_ready": partial_publication_recovery,
        "unpaired_completion_versions": unpaired_completion_versions,
        "unpaired_high_water_versions": unpaired_high_water_versions,
        "reset_receipt_valid": reset_valid,
        "reset_receipt_digest": (
            str(receipt.get("receipt_digest")) if receipt is not None else None
        ),
        "reset_receipt_issues": list(receipt_errors),
        "version_authority_high_water": authority_high_water,
        "runtime_reconciliation_claimed": reconciliation_claimed,
        "runtime_reconciliation_claim_path": str(reconciliation_claim_path),
        "runtime_reconciliation_kind": reconciliation.get("kind"),
        "runtime_reconciliation_claim_digest": reconciliation.get("claim_digest"),
        "runtime_reconciliation_claim_valid": bool(reconciliation.get("valid")),
        "runtime_reconciliation_claim_issues": list(
            reconciliation.get("issues") or []
        ),
        "first_strict_version": FIRST_STRICT_POLICY_VERSION,
        "operator_action": operator_action,
        "operator_command": operator_command,
    }


def require_policy_epoch_initialized(operation: str) -> dict[str, Any]:
    """Return canonical initialization state or fail before a mutating launch.

    This function is deliberately read-only.  In particular it emits no
    structured event: before initialization, ``results/`` is retired evidence,
    so even a helpful launch-denied event would contaminate the old epoch.
    """

    state = policy_epoch_initialization()
    if not state["initialized"]:
        raise PolicyEpochInitializationRequired(operation, state)
    return state


def strict_epoch_projection(*, include_checkpoint: bool = True) -> dict[str, Any]:
    """Return the sole status/scheduling view of versions and checkpoint state.

    Published namespace identity and current-epoch allocation are intentionally
    separate: annotated completion/high-water tags own ``current_v``; a valid
    checkpoint-bound abandonment receipt may only reserve an allocation label.
    """

    import evolution_infra as infra
    from checkpoint_schema import (
        OPERATOR_ARCHIVE_RESET_COMMAND,
        checkpoint_epoch_errors,
        live_checkpoint_allocation_authority_errors,
        live_checkpoint_parent_authority_errors,
        live_policy_epoch_reset_receipt_errors,
    )

    current_v = int(infra.find_current_v())
    initialization = policy_epoch_initialization(current_v=current_v)
    published_versions = sorted({
        version
        for name in initialization.get("strict_published_bots", [])
        if (version := parse_bot_version(str(name))) is not None
        and version >= FIRST_STRICT_POLICY_VERSION
    })
    allocation_authority_error = None
    try:
        abandoned_authority = infra.abandoned_version_authority(
            initialization=initialization,
            published_high_water=current_v,
        )
        abandoned_floor = int(abandoned_authority["floor"])
        abandoned_head = abandoned_authority.get("head_digest")
    except Exception as exc:
        abandoned_floor = 0
        abandoned_head = None
        allocation_authority_error = f"{type(exc).__name__}: {exc}"
    allocation_floor = max(current_v, abandoned_floor)
    next_v = allocation_floor + 1

    # Discovery is strict and publication-bound; untracked candidate directory
    # names such as national_v155 never become active identities.
    active_bots = list(initialization.get("strict_published_bots", []))

    projection: dict[str, Any] = {
        **initialization,
        "current_v": current_v,
        "published_high_water": current_v,
        "allocation_floor": allocation_floor,
        "next_v": next_v,
        "next_v_authority": "published_tags_and_abandon_receipts",
        # Compatibility alias for older read-only consumers.  It is now the
        # published tag high-water and never scans/accepts bare commits.
        "max_committed_v": current_v,
        "abandoned_floor": abandoned_floor,
        "abandoned_receipt_floor": abandoned_floor,
        "abandoned_receipt_head_digest": abandoned_head,
        "allocation_authority_valid": allocation_authority_error is None,
        "allocation_authority_error": allocation_authority_error,
        "active_bots": active_bots,
        "active_bots_count": len(active_bots),
        "strict_published_versions": published_versions,
        "strict_published_bot_identities": [
            strict_generation_identity(version) for version in published_versions
        ],
        "strict_generation_count": len(published_versions),
        "active_generation": None,
        "ignored_checkpoint": None,
    }
    if initialization.get("runtime_reconciliation_claimed"):
        # A live claim is a launch barrier, not merely a UI annotation.  Even
        # previously published artifacts are withheld from the active pool
        # until the exact claim validates and completes; a forged claim must
        # never coexist with schedulable bots in this canonical projection.
        projection["active_bots"] = []
        projection["active_bots_count"] = 0
        claim_issues = list(
            initialization.get("runtime_reconciliation_claim_issues") or []
        )
        projection["ignored_checkpoint"] = {
            "next_v": None,
            "source_v": None,
            "stage": None,
            "reason": "runtime_reconciliation_in_progress",
            "issues": [
                "durable_runtime_reconciliation_claim_present",
                *claim_issues,
            ],
        }
        projection["operator_action"] = initialization.get("operator_action")
        projection["operator_command"] = initialization.get("operator_command")
        return projection
    if allocation_authority_error is not None:
        projection["ignored_checkpoint"] = {
            "next_v": None,
            "source_v": None,
            "stage": None,
            "reason": "abandon_receipt_ledger_requires_reconciliation",
            "issues": [allocation_authority_error],
        }
        projection["operator_action"] = (
            "quarantine_legacy_ledger_and_abandon_checkpoint"
        )
        projection["operator_command"] = OPERATOR_ARCHIVE_RESET_COMMAND
        return projection
    if not include_checkpoint:
        return projection

    checkpoint_path = Path(infra.PIPELINE_STATE_FILE)
    checkpoint_read_error = None
    checkpoint_path_existed_before = os.path.lexists(checkpoint_path)
    try:
        checkpoint = infra.read_pipeline_checkpoint()
    except Exception as exc:
        checkpoint = None
        checkpoint_read_error = f"{type(exc).__name__}: {exc}"
    checkpoint_path_exists_after = os.path.lexists(checkpoint_path)
    checkpoint_disappeared_during_read = bool(
        checkpoint_path_existed_before and not checkpoint_path_exists_after
    )
    checkpoint_claimed = bool(
        checkpoint_path_existed_before
        or checkpoint_path_exists_after
        or checkpoint is not None
    )
    if checkpoint_disappeared_during_read:
        projection["ignored_checkpoint"] = {
            "next_v": (
                checkpoint.get("next_v")
                if isinstance(checkpoint, dict)
                else None
            ),
            "source_v": (
                checkpoint.get("source_v")
                if isinstance(checkpoint, dict)
                else None
            ),
            "stage": (
                checkpoint.get("stage")
                if isinstance(checkpoint, dict)
                else None
            ),
            "reason": "checkpoint_unreadable_or_not_object",
            "issues": ["checkpoint_disappeared_during_read"],
        }
        projection["operator_action"] = "archive_incompatible_checkpoint"
        projection["operator_command"] = None
        return projection
    if checkpoint_claimed and (not isinstance(checkpoint, dict) or not checkpoint):
        unreadable_reason: IgnoredCheckpointReason = (
            "checkpoint_unreadable_or_not_object"
        )
        projection["ignored_checkpoint"] = {
            "next_v": None,
            "source_v": None,
            "stage": None,
            "reason": unreadable_reason,
            "issues": [
                checkpoint_read_error or "checkpoint_unreadable_or_not_object"
            ],
        }
        projection["operator_action"] = "archive_incompatible_checkpoint"
        projection["operator_command"] = None
        return projection
    if not isinstance(checkpoint, dict) or not checkpoint:
        return projection
    stage = checkpoint.get("stage")
    # A claimed checkpoint path is never equivalent to the scheduler's clean
    # no-checkpoint boundary.  In particular, a crash can leave a terminal
    # ``archived``/``abandoned`` row (or a partially-written object with no
    # stage/target) after the publication handoff was created.  Treating that
    # file as absent lets the Web launch barrier advertise
    # ``ready_to_prepare`` while the in-core checkpoint reader correctly stops
    # on the malformed identity.  Require explicit operator cleanup instead.
    if (
        stage in (None, "archived", "abandoned")
        or checkpoint.get("next_v") is None
    ):
        terminal_issues = []
        if stage in {"archived", "abandoned"}:
            terminal_issues.append(
                f"terminal_checkpoint_requires_cleanup:{stage}"
            )
        if stage is None:
            terminal_issues.append("checkpoint_stage_missing")
        if checkpoint.get("next_v") is None:
            terminal_issues.append("checkpoint_next_v_missing")
        projection["ignored_checkpoint"] = {
            "next_v": checkpoint.get("next_v"),
            "source_v": checkpoint.get("source_v"),
            "stage": stage,
            "reason": "checkpoint_not_bound_to_strict_epoch",
            "issues": terminal_issues,
        }
        projection["operator_action"] = "archive_incompatible_checkpoint"
        projection["operator_command"] = None
        return projection

    issues = checkpoint_epoch_errors(checkpoint)
    if not issues:
        issues.extend(
            live_checkpoint_parent_authority_errors(
                checkpoint,
                repo_root=infra.PROJECT_ROOT,
            )
        )
    if not issues:
        issues.extend(
            live_policy_epoch_reset_receipt_errors(
                checkpoint,
                project_root=infra.PROJECT_ROOT,
            )
        )
    publication_reconciliation = False
    recorded_abandon_finalize = None
    if not issues and stage == "publishing":
        try:
            publication_reconciliation = bool(
                infra._publication_checkpoint_reconciliation_allowed(
                    checkpoint,
                    {
                        "published_high_water": current_v,
                        "abandoned_receipt_floor": abandoned_floor,
                        "abandoned_receipt_head_digest": abandoned_head,
                    },
                )
            )
        except Exception:
            publication_reconciliation = False
    if not issues:
        live_allocation_issues = live_checkpoint_allocation_authority_errors(
            checkpoint,
            published_high_water=current_v,
            abandoned_receipt_floor=abandoned_floor,
            abandoned_receipt_head_digest=abandoned_head,
            allow_published_target_reconciliation=publication_reconciliation,
        )
        if live_allocation_issues:
            try:
                recorded_abandon_finalize = (
                    infra.recorded_abandon_receipt_for_checkpoint(checkpoint)
                )
            except Exception as exc:
                issues.append(
                    "recorded_abandon_receipt_validation_error:"
                    f"{type(exc).__name__}"
                )
            if recorded_abandon_finalize is None and not issues:
                issues.extend(live_allocation_issues)
    if issues:
        invalid_binding_reason: IgnoredCheckpointReason = (
            "checkpoint_not_bound_to_strict_epoch"
        )
        projection["ignored_checkpoint"] = {
            "next_v": checkpoint.get("next_v"),
            "source_v": checkpoint.get("source_v"),
            "stage": stage,
            "reason": invalid_binding_reason,
            "issues": list(dict.fromkeys(map(str, issues))),
        }
        # Before initialization, the central reset archives both the checkpoint
        # and candidate.  It is evidence for that operator action, never an
        # active generation or a version floor.
        if not initialization["initialized"]:
            return projection
        projection["operator_action"] = "operator_reconcile_checkpoint"
        projection["operator_command"] = OPERATOR_ARCHIVE_RESET_COMMAND
        return projection

    generation_attempt = int(checkpoint.get("generation_attempt") or 0)
    canonical_identity = strict_generation_identity(int(checkpoint["next_v"]))
    projection["active_generation"] = {
        **canonical_identity,
        "next_v": int(checkpoint["next_v"]),
        "source_v": checkpoint.get("source_v"),
        "parent2_v": checkpoint.get("parent2_v"),
        "stage": stage,
        "run_id": checkpoint.get("run_id")
        or f"{int(checkpoint['next_v'])}#{generation_attempt}",
        "workflow_run_id": checkpoint.get("workflow_run_id"),
        "checkpoint_revision": checkpoint.get("checkpoint_revision"),
        "attempt": {
            "generation": generation_attempt,
            "audit": int(checkpoint.get("audit_attempt") or 0),
            "precommit": int(checkpoint.get("precommit_attempt") or 0),
        },
    }
    projection["next_v"] = int(checkpoint["next_v"])
    projection["next_v_authority"] = (
        "recorded_abandon_checkpoint_finalize"
        if recorded_abandon_finalize is not None
        else (
            "publication_checkpoint_reconciliation"
            if publication_reconciliation
            else "active_checkpoint_epoch_binding"
        )
    )
    if recorded_abandon_finalize is not None:
        projection["active_generation"]["recovery_kind"] = (
            "recorded_abandon_checkpoint_finalize"
        )
        projection["active_generation"]["abandon_receipt_digest"] = (
            recorded_abandon_finalize.get("receipt_digest")
        )
        projection["operator_action"] = "finalize_recorded_abandon_checkpoint"
        projection["operator_command"] = RECORDED_ABANDON_FINALIZE_COMMAND
    if publication_reconciliation:
        projection["active_generation"]["recovery_kind"] = (
            "publication_reconciliation"
        )
    if (
        recorded_abandon_finalize is None
        and
        stage == "official_bootstrap_required"
        and int(checkpoint["next_v"]) == FIRST_STRICT_POLICY_VERSION
    ):
        projection["operator_action"] = "run_first_strict_official_certification"
        projection["operator_command"] = FIRST_STRICT_BOOTSTRAP_COMMAND
        projection["operator_transition"] = first_strict_operator_transition(
            checkpoint
        )
    return projection


def unpublished_candidate_versions() -> list[int]:
    """List on-disk strict-numbered directories only as non-authoritative debris."""

    import evolution_infra as infra

    active = set(strict_epoch_projection(include_checkpoint=False)["active_bots"])
    versions: list[int] = []
    root = Path(infra.BOTS_DIR)
    if not root.is_dir() or root.is_symlink():
        return versions
    for child in root.iterdir():
        version = parse_bot_version(child.name)
        if (
            version is not None
            and version >= FIRST_STRICT_POLICY_VERSION
            and child.name not in active
            and child.is_dir()
            and not child.is_symlink()
        ):
            versions.append(version)
    return sorted(set(versions))


__all__ = [
    "EpochOperatorAction",
    "EpochState",
    "FIRST_STRICT_BOOTSTRAP_COMMAND",
    "FIRST_STRICT_BOOTSTRAP_RETRY_COMMAND",
    "FIRST_STRICT_CERTIFICATION_PROFILE",
    "FIRST_STRICT_FINALIZE_COMMAND",
    "IgnoredCheckpointReason",
    "PolicyEpochInitializationRequired",
    "RESET_COMMAND",
    "RECORDED_ABANDON_FINALIZE_COMMAND",
    "RUNTIME_RECONCILIATION_COMMAND",
    "epoch_stream_authority_digest",
    "policy_epoch_initialization",
    "first_strict_operator_transition",
    "require_policy_epoch_initialized",
    "strict_epoch_projection",
    "unpublished_candidate_versions",
    "schema2_abandon_quarantine_contract",
    "schema2_abandon_receipt_identity",
    "schema2_abandon_transaction_preimage",
    "validate_schema2_abandon_claim_structure",
    "validate_schema2_abandon_finalize_receipt",
    "validate_schema2_abandon_ledger_history",
]
