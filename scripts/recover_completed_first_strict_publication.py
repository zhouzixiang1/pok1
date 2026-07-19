#!/usr/bin/env python3
"""One-shot publication recovery for a completed first-strict certificate.

This command exists for one narrow state transition: the signed first-strict
certificate was durably recorded and the checkpoint moved from
``official_bootstrap_required`` to ``verified``, but the legacy completed
authorization validator still expected the pre-transition stage.  It does not
rerun certification, rewrite the parked request, recompute an authorization,
or perform Git effects itself.

Run a dry-run first.  Review its content-bound claim, then repeat with
``--execute --acknowledge-runtime-checkout --claim-digest ...``.  The command
persists a publication-recovery receipt and the ordinary immutable publication
intent in one checkpoint CAS, then delegates to ``commit_bot``'s existing
``publishing`` recovery branch.
"""

from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "web" / "core"
for entry in list(sys.path):
    try:
        resolved = Path(entry or ".").resolve()
    except OSError:
        continue
    if resolved in {CORE, ROOT}:
        sys.path.remove(entry)
sys.path.insert(0, str(CORE))
sys.path.insert(1, str(ROOT))

from bot_artifact import canonical_digest  # noqa: E402
from bot_namespace import (  # noqa: E402
    ARCHIVED_VERSION_HIGH_WATER,
    FIRST_STRICT_POLICY_VERSION,
    bot_name,
)


CLAIM_SCHEMA_VERSION = 1
CLAIM_KIND = "completed-first-strict-publication-recovery-claim"
TERMINAL_RECEIPT_SCHEMA_VERSION = 1
TERMINAL_RECEIPT_KIND = "completed-first-strict-publication-recovery-receipt"
CHECKPOINT_RECEIPT_KEY = "completed_first_strict_publication_recovery"
CLAIM_DIRNAME = "completed_first_strict_publication_recovery"
RECOVERY_SCRIPT = "scripts/recover_completed_first_strict_publication.py"
RECOVERY_TEST = "web/tests/test_completed_bootstrap_publication_recovery.py"
EXPECTED_CHANGED_PATHS = frozenset({RECOVERY_SCRIPT, RECOVERY_TEST})
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class CompletedFirstStrictPublicationRecoveryError(RuntimeError):
    def __init__(self, issues: list[str] | tuple[str, ...]):
        self.issues = tuple(dict.fromkeys(str(item) for item in issues if str(item)))
        super().__init__("; ".join(self.issues) or "publication recovery invalid")


def _unique(items: list[Any]) -> list[str]:
    return list(dict.fromkeys(str(item) for item in items if str(item)))


def receipt_strategy_tag(claim_digest: str, base_strategy: str = "") -> str:
    if not _HEX64.fullmatch(str(claim_digest or "")):
        raise CompletedFirstStrictPublicationRecoveryError([
            "first_strict_publication_recovery_claim_digest_invalid"
        ])
    base = str(base_strategy or "fresh_policy_bootstrap").strip()
    return f"{base}|publication-recovery:{claim_digest}"


# Compatibility name used by the focused regression.  The value is carried by
# strategy_tag (and therefore commit/tag/intent), never by rating authority.
def receipt_commit_line(claim_digest: str) -> str:
    return receipt_strategy_tag(claim_digest)


def _parked_request_issues(parked: Any) -> list[str]:
    if not isinstance(parked, dict):
        return ["first_strict_publication_recovery_parked_request_missing"]
    issues: list[str] = []
    unsigned = {
        key: value for key, value in parked.items() if key != "request_digest"
    }
    if parked.get("schema_version") != 1:
        issues.append("first_strict_publication_recovery_parked_schema_mismatch")
    if parked.get("kind") != "official-first-strict-control-parked-request":
        issues.append("first_strict_publication_recovery_parked_kind_mismatch")
    if parked.get("request_digest") != canonical_digest(unsigned):
        issues.append("first_strict_publication_recovery_parked_digest_mismatch")
    expected = {
        "candidate_label": bot_name(FIRST_STRICT_POLICY_VERSION),
        "candidate_version": FIRST_STRICT_POLICY_VERSION,
        "source_v": ARCHIVED_VERSION_HIGH_WATER,
        "active_bots": [],
        "strict_published_bots": [],
        "bootstrap_control_id": "first_strict_control_v1",
    }
    for field, value in expected.items():
        if parked.get(field) != value:
            issues.append(
                f"first_strict_publication_recovery_parked_{field}_mismatch"
            )
    for field in (
        "candidate_hash",
        "checkpoint_contract_digest",
        "evaluation_contract_hash",
        "protocol_bootstrap_receipt_digest",
        "first_strict_control_receipt_digest",
        "bootstrap_policy_digest",
    ):
        if not _HEX64.fullmatch(str(parked.get(field) or "")):
            issues.append(
                f"first_strict_publication_recovery_parked_{field}_invalid"
            )
    if parked.get("evaluation_contract_version") != 42:
        issues.append(
            "first_strict_publication_recovery_parked_contract_version_mismatch"
        )
    return _unique(issues)


def publication_recovery_snapshot_issues(
    *,
    checkpoint: dict[str, Any],
    status: dict[str, Any],
    candidate: Path,
    candidate_hash: str,
    certificate_digest: str,
    ledger_entry_digest: str,
    authorization: dict[str, Any],
    gate_ledger: dict[str, Any],
    active_bots: list[str],
    strict_bots: list[str],
    completion_tags: list[str],
    completed: bool,
    official_jobs_active: bool,
) -> list[str]:
    """Validate the complete non-Git snapshot before building a claim."""

    issues: list[str] = []
    checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
    status = status if isinstance(status, dict) else {}
    candidate = Path(candidate).resolve()
    if checkpoint.get("stage") != "verified":
        issues.append("first_strict_publication_recovery_checkpoint_stage_mismatch")
    if (
        checkpoint.get("next_v") != FIRST_STRICT_POLICY_VERSION
        or checkpoint.get("source_v") != ARCHIVED_VERSION_HIGH_WATER
        or not str(checkpoint.get("workflow_run_id") or "").strip()
        or int(checkpoint.get("checkpoint_revision") or 0) < 1
    ):
        issues.append("first_strict_publication_recovery_checkpoint_identity_mismatch")
    if checkpoint.get("publication_intent") is not None:
        issues.append("first_strict_publication_recovery_publication_intent_present")
    if checkpoint.get("official_job") is not None:
        issues.append("first_strict_publication_recovery_official_job_attached")

    audit = checkpoint.get("audit_context")
    audit = audit if isinstance(audit, dict) else {}
    parked = audit.get("official_bootstrap_request")
    issues.extend(_parked_request_issues(parked))
    if isinstance(parked, dict):
        if parked.get("workflow_run_id") != checkpoint.get("workflow_run_id"):
            issues.append(
                "first_strict_publication_recovery_parked_workflow_mismatch"
            )
        if Path(str(parked.get("candidate_path") or "")).resolve() != candidate:
            issues.append(
                "first_strict_publication_recovery_parked_candidate_path_mismatch"
            )
        if parked.get("candidate_hash") != candidate_hash:
            issues.append(
                "first_strict_publication_recovery_parked_candidate_hash_mismatch"
            )

    official_gate = (checkpoint.get("gate_results") or {}).get("official_full")
    official_gate = official_gate if isinstance(official_gate, dict) else {}
    if official_gate.get("passed") is not True:
        issues.append("first_strict_publication_recovery_official_gate_not_passed")
    if official_gate.get("status") != status:
        issues.append("first_strict_publication_recovery_gate_status_mismatch")
    if official_gate.get("certificate_digest") != certificate_digest:
        issues.append("first_strict_publication_recovery_gate_certificate_mismatch")
    if official_gate.get("certification_identity") != status.get(
        "certification_identity"
    ):
        issues.append("first_strict_publication_recovery_gate_identity_mismatch")
    recorded_authorization = official_gate.get(
        "completed_bootstrap_authorization"
    )
    if not isinstance(recorded_authorization, dict) or (
        recorded_authorization.get("valid") is not True
    ):
        issues.append(
            "first_strict_publication_recovery_recorded_authorization_missing"
        )
    for observed, label in (
        (authorization, "live"),
        (recorded_authorization, "recorded"),
    ):
        observed = observed if isinstance(observed, dict) else {}
        if observed.get("valid") is not True:
            issues.append(
                f"first_strict_publication_recovery_{label}_authorization_invalid"
            )
        if observed.get("candidate_hash") != candidate_hash:
            issues.append(
                f"first_strict_publication_recovery_{label}_candidate_hash_mismatch"
            )
        if observed.get("certificate_digest") != certificate_digest:
            issues.append(
                f"first_strict_publication_recovery_{label}_certificate_mismatch"
            )
        if observed.get("ledger_entry_digest") != ledger_entry_digest:
            issues.append(
                f"first_strict_publication_recovery_{label}_ledger_mismatch"
            )

    identity = status.get("certification_identity")
    identity = identity if isinstance(identity, dict) else {}
    spec = identity.get("spec") if isinstance(identity.get("spec"), dict) else {}
    if (
        status.get("status") != "official-certified"
        or status.get("mode") != "full"
        or status.get("policy_id") != "official-full-v5"
        or status.get("certificate_digest") != certificate_digest
        or identity.get("candidate_hash") != candidate_hash
        or Path(str(spec.get("candidate") or "")).resolve() != candidate
        or spec.get("bootstrap_control_id") != "first_strict_control_v1"
    ):
        issues.append("first_strict_publication_recovery_certificate_identity_invalid")
    ledger_entry = status.get("official_verdict_ledger_entry")
    ledger_entry = ledger_entry if isinstance(ledger_entry, dict) else {}
    if (
        ledger_entry.get("entry_digest") != ledger_entry_digest
        or ledger_entry.get("certificate_digest") != certificate_digest
    ):
        issues.append("first_strict_publication_recovery_status_ledger_mismatch")

    if (
        gate_ledger.get("ok") is not True
        or gate_ledger.get("missing_gates")
        or gate_ledger.get("failed_gates")
    ):
        issues.append("first_strict_publication_recovery_final_gate_ledger_invalid")
    if gate_ledger.get("current_code_fingerprint") != candidate_hash:
        issues.append("first_strict_publication_recovery_gate_candidate_mismatch")
    if active_bots:
        issues.append("first_strict_publication_recovery_active_pool_not_empty")
    if strict_bots:
        issues.append("first_strict_publication_recovery_strict_pool_not_empty")
    if completion_tags:
        issues.append("first_strict_publication_recovery_completion_tag_present")
    if completed:
        issues.append("first_strict_publication_recovery_completed_present")
    if official_jobs_active:
        issues.append("first_strict_publication_recovery_official_job_active")
    return _unique(issues)


def validate_completed_at_parked_authority(
    status: dict[str, Any],
    candidate: str | Path,
    checkpoint: dict[str, Any],
) -> dict[str, Any]:
    """Revalidate the signed result against the exact pre-transition stage.

    Only an in-memory copy is changed.  The durable checkpoint remains
    ``verified`` and its parked request is never regenerated or replaced.
    """

    normalized = deepcopy(checkpoint)
    normalized["stage"] = "official_bootstrap_required"
    from official_bootstrap import (
        validate_completed_operator_bootstrap_authorization,
    )

    return validate_completed_operator_bootstrap_authorization(
        status,
        candidate,
        checkpoint=normalized,
    )


def publishing_recovery_issues(
    checkpoint: dict[str, Any],
    claim_digest: str,
) -> list[str]:
    issues: list[str] = []
    if not _HEX64.fullmatch(str(claim_digest or "")):
        issues.append("first_strict_publication_recovery_claim_digest_invalid")
    if checkpoint.get("stage") != "publishing":
        issues.append("first_strict_publication_recovery_not_publishing")
    if (
        checkpoint.get("next_v") != FIRST_STRICT_POLICY_VERSION
        or checkpoint.get("source_v") != ARCHIVED_VERSION_HIGH_WATER
    ):
        issues.append("first_strict_publication_recovery_checkpoint_identity_mismatch")
    audit = checkpoint.get("audit_context")
    audit = audit if isinstance(audit, dict) else {}
    receipt = audit.get(CHECKPOINT_RECEIPT_KEY)
    if not isinstance(receipt, dict):
        issues.append("first_strict_publication_recovery_receipt_missing")
        receipt = {}
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_digest"}
    if receipt and receipt.get("receipt_digest") != canonical_digest(unsigned):
        issues.append("first_strict_publication_recovery_receipt_digest_mismatch")
    if receipt and (
        receipt.get("schema_version") != TERMINAL_RECEIPT_SCHEMA_VERSION
        or receipt.get("kind") != TERMINAL_RECEIPT_KIND
        or receipt.get("claim_digest") != claim_digest
    ):
        issues.append("first_strict_publication_recovery_receipt_identity_mismatch")
    intent = checkpoint.get("publication_intent")
    if not isinstance(intent, dict):
        issues.append("first_strict_publication_recovery_intent_missing")
        intent = {}
    if intent and (
        intent.get("version") != FIRST_STRICT_POLICY_VERSION
        or intent.get("source_v") != ARCHIVED_VERSION_HIGH_WATER
        or intent.get("workflow_run_id") != checkpoint.get("workflow_run_id")
        or intent.get("publication_id") != receipt.get("publication_id")
    ):
        issues.append("first_strict_publication_recovery_intent_identity_mismatch")
    if intent and f"publication-recovery:{claim_digest}" not in str(
        intent.get("strategy_tag") or ""
    ):
        issues.append("first_strict_publication_recovery_intent_receipt_mismatch")
    return _unique(issues)


def _git(root: Path, *args: str, binary: bool = False) -> bytes | str:
    process = subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=not binary,
        timeout=30,
        check=False,
    )
    if process.returncode != 0:
        stderr = process.stderr
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        raise RuntimeError(str(stderr).strip() or f"git {' '.join(args)} failed")
    return process.stdout


def _full_commit(root: Path, revision: str) -> str:
    value = str(_git(root, "rev-parse", "--verify", f"{revision}^{{commit}}" )).strip()
    if not _HEX40.fullmatch(value):
        raise RuntimeError("Git revision is not one full commit")
    return value


def _self_blob_issues(root: Path, head: str) -> list[str]:
    issues: list[str] = []
    try:
        blob = _git(root, "show", f"{head}:{RECOVERY_SCRIPT}", binary=True)
        if not isinstance(blob, bytes) or blob != Path(__file__).read_bytes():
            issues.append("first_strict_publication_recovery_self_blob_mismatch")
    except Exception as exc:
        issues.append(
            "first_strict_publication_recovery_self_blob_unavailable:"
            f"{type(exc).__name__}"
        )
    return issues


def _claim_path(root: Path, claim_digest: str) -> Path:
    return root / "web" / "core" / "results" / CLAIM_DIRNAME / f"{claim_digest}.json"


def _read_regular_json(path: Path) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        live = os.lstat(path)
        identity = (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
            opened.st_nlink,
        )
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(live.st_mode)
            or stat.S_ISLNK(live.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (live.st_dev, live.st_ino)
            or opened.st_size > 4 * 1024 * 1024
        ):
            raise RuntimeError("recovery claim path is unsafe")
        chunks: list[bytes] = []
        remaining = 4 * 1024 * 1024 + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        live_after = os.lstat(path)
        if (
            len(raw) > 4 * 1024 * 1024
            or (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
                after.st_nlink,
            ) != identity
            or (live_after.st_dev, live_after.st_ino)
            != (opened.st_dev, opened.st_ino)
            or live_after.st_nlink != 1
        ):
            raise RuntimeError("recovery claim changed during read")
    finally:
        os.close(descriptor)
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("recovery claim is not an object")
    return value


def publish_claim(root: Path, claim: dict[str, Any]) -> Path:
    digest = str(claim.get("claim_digest") or "")
    if digest != canonical_digest({
        key: value for key, value in claim.items() if key != "claim_digest"
    }):
        raise CompletedFirstStrictPublicationRecoveryError([
            "first_strict_publication_recovery_claim_digest_mismatch"
        ])
    path = _claim_path(root, digest)
    results = root / "web" / "core" / "results"
    for parent in (results.parent.parent, results.parent, results):
        metadata = os.lstat(parent)
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise CompletedFirstStrictPublicationRecoveryError([
                "first_strict_publication_recovery_claim_parent_unsafe"
            ])
    try:
        os.mkdir(path.parent, 0o700)
        created = True
    except FileExistsError:
        created = False
    directory_metadata = os.lstat(path.parent)
    if (
        not stat.S_ISDIR(directory_metadata.st_mode)
        or stat.S_ISLNK(directory_metadata.st_mode)
        or path.parent.parent.resolve(strict=True) != results.resolve(strict=True)
    ):
        raise CompletedFirstStrictPublicationRecoveryError([
            "first_strict_publication_recovery_claim_directory_unsafe"
        ])
    directory = os.open(
        path.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    if created:
        results_descriptor = os.open(
            results, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(results_descriptor)
        finally:
            os.close(results_descriptor)
    encoded = (json.dumps(
        claim, ensure_ascii=False, sort_keys=True, indent=2
    ) + "\n").encode("utf-8")
    try:
        try:
            descriptor = os.open(
                path.name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o400,
                dir_fd=directory,
            )
        except FileExistsError:
            if _read_regular_json(path) != claim:
                raise CompletedFirstStrictPublicationRecoveryError([
                    "first_strict_publication_recovery_existing_claim_mismatch"
                ])
            return path
        try:
            view = memoryview(encoded)
            offset = 0
            while offset < len(view):
                written = os.write(descriptor, view[offset:])
                if written <= 0:
                    raise OSError("recovery claim write made no progress")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(directory)
    finally:
        os.close(directory)
    return path


def build_claim(
    root: str | Path,
    *,
    checkpoint: dict[str, Any],
    expected_baseline_head: str,
    expected_baseline_contract_hash: str,
    expected_current_head: str,
    expected_workflow_run_id: str,
    expected_checkpoint_revision: int,
    expected_candidate_hash: str,
    expected_certificate_digest: str,
    expected_ledger_entry_digest: str,
) -> dict[str, Any]:
    """Build the exact dry-run recovery claim without mutating state."""

    root = Path(root).resolve()
    issues: list[str] = []
    if root.name != ".evolution_pok":
        issues.append("first_strict_publication_recovery_requires_runtime_checkout")
    if (
        checkpoint.get("workflow_run_id") != expected_workflow_run_id
        or checkpoint.get("checkpoint_revision") != expected_checkpoint_revision
    ):
        issues.append("first_strict_publication_recovery_expected_checkpoint_mismatch")

    baseline = checkpoint.get("repo_baseline")
    baseline = baseline if isinstance(baseline, dict) else {}
    old_contract = baseline.get("evaluation_contract")
    old_contract = old_contract if isinstance(old_contract, dict) else {}
    parked = ((checkpoint.get("audit_context") or {}).get(
        "official_bootstrap_request"
    ))
    if (
        old_contract.get("version") != 42
        or old_contract.get("stage") != "verified"
        or old_contract.get("hash") != expected_baseline_contract_hash
        or not isinstance(parked, dict)
        or parked.get("evaluation_contract_version") != 42
        or parked.get("evaluation_contract_hash")
        != expected_baseline_contract_hash
    ):
        issues.append("first_strict_publication_recovery_baseline_contract_mismatch")
    try:
        full_expected_baseline = _full_commit(root, expected_baseline_head)
        full_checkpoint_baseline = _full_commit(root, str(baseline.get("head") or ""))
        current_head = _full_commit(root, "HEAD")
        origin_head = _full_commit(root, "origin/main")
    except Exception as exc:
        issues.append(
            "first_strict_publication_recovery_git_identity_unavailable:"
            f"{type(exc).__name__}"
        )
        full_expected_baseline = expected_baseline_head
        full_checkpoint_baseline = ""
        current_head = ""
        origin_head = ""
    if (
        full_checkpoint_baseline != full_expected_baseline
        or current_head != expected_current_head
        or current_head != origin_head
        or str(_git(root, "rev-parse", "--abbrev-ref", "HEAD")).strip() != "main"
    ):
        issues.append("first_strict_publication_recovery_git_identity_mismatch")
    tracked_dirty = str(
        _git(root, "status", "--porcelain", "--untracked-files=no")
    ).strip()
    if tracked_dirty:
        issues.append("first_strict_publication_recovery_tracked_worktree_dirty")
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", full_expected_baseline, current_head],
        cwd=str(root),
        capture_output=True,
        timeout=30,
        check=False,
    )
    if ancestor.returncode != 0:
        issues.append("first_strict_publication_recovery_head_not_descendant")
    issues.extend(_self_blob_issues(root, current_head))

    try:
        from bootstrap_contract_recovery import (
            _contract_hash_at_head,
            _safe_candidate,
        )
        from evaluation_contract import (
            build_evaluation_contract,
            classify_contract_paths,
            evaluate_head_drift,
        )
        from evolution_scope import changed_paths_between_heads

        changed_paths = changed_paths_between_heads(
            root, full_expected_baseline, current_head
        )
        if changed_paths is None:
            raise RuntimeError("changed paths unavailable")
        if set(changed_paths) != EXPECTED_CHANGED_PATHS:
            issues.append("first_strict_publication_recovery_changed_paths_mismatch")
        old_hash = _contract_hash_at_head(
            root, full_expected_baseline, old_contract
        )
        current_contract = build_evaluation_contract(
            root,
            candidate_v=FIRST_STRICT_POLICY_VERSION,
            source_v=ARCHIVED_VERSION_HIGH_WATER,
            checkpoint=checkpoint,
            stage="verified",
            include_hash=True,
        )
        old_scope = classify_contract_paths(changed_paths, old_contract)
        current_scope = classify_contract_paths(changed_paths, current_contract)
        drift_allowed, drift = evaluate_head_drift(
            root,
            full_expected_baseline,
            current_head,
            candidate_v=FIRST_STRICT_POLICY_VERSION,
            source_v=ARCHIVED_VERSION_HIGH_WATER,
            checkpoint=checkpoint,
            stage="verified",
        )
        if (
            old_hash != expected_baseline_contract_hash
            or current_contract.get("hash") != expected_baseline_contract_hash
            or old_scope.get("contract_paths")
            or current_scope.get("contract_paths")
            or drift_allowed is not True
            or (drift.get("evaluation_contract_unchanged") is not True)
        ):
            issues.append("first_strict_publication_recovery_contract_drift")
        candidate_facts = _safe_candidate(
            root,
            FIRST_STRICT_POLICY_VERSION,
            expected_candidate_hash,
        )
    except Exception as exc:
        issues.append(
            "first_strict_publication_recovery_contract_or_candidate_error:"
            f"{type(exc).__name__}:{str(exc)[:180]}"
        )
        changed_paths = []
        old_hash = ""
        current_contract = {}
        drift = {}
        candidate_facts = {}

    candidate = root / "bots" / bot_name(FIRST_STRICT_POLICY_VERSION)
    try:
        from evolution_infra import get_active_bots_read_only, git_publish_status
        from national_runtime_authority import strict_published_bot_names
        from official_certification import official_full_certified, status_payload
        from official_certification_job import job_snapshot
        from tool_commit import validate_commit_gate_ledger

        status = status_payload(candidate)
        certified = official_full_certified(status, candidate)
        authorization = validate_completed_at_parked_authority(
            status, candidate, checkpoint
        )
        gate_ledger = validate_commit_gate_ledger(
            FIRST_STRICT_POLICY_VERSION,
            ARCHIVED_VERSION_HIGH_WATER,
            checkpoint,
            bot_dir=candidate,
        )
        active_bots = list(get_active_bots_read_only())
        strict_bots = list(strict_published_bot_names())
        jobs = job_snapshot()
        jobs_active = bool(jobs.get("pending") or jobs.get("running"))
        publish_state = git_publish_status()
        if (
            publish_state.get("ok") is not True
            or int(publish_state.get("ahead") or 0) != 0
            or int(publish_state.get("behind") or 0) != 0
        ):
            issues.append("first_strict_publication_recovery_publish_state_invalid")
        if not certified:
            issues.append("first_strict_publication_recovery_certificate_invalid")
    except Exception as exc:
        issues.append(
            "first_strict_publication_recovery_live_authority_error:"
            f"{type(exc).__name__}:{str(exc)[:180]}"
        )
        status = {}
        authorization = {}
        gate_ledger = {}
        active_bots = []
        strict_bots = []
        jobs_active = True

    status_ledger = status.get("official_verdict_ledger_entry")
    status_ledger = status_ledger if isinstance(status_ledger, dict) else {}
    completion_tags = []
    for tag in (
        f"national-bot-v{FIRST_STRICT_POLICY_VERSION}",
        f"national-high-water-v{FIRST_STRICT_POLICY_VERSION}",
    ):
        probe = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/tags/{tag}"],
            cwd=str(root),
            capture_output=True,
            timeout=30,
            check=False,
        )
        if probe.returncode == 0:
            completion_tags.append(tag)
        elif probe.returncode != 1:
            issues.append("first_strict_publication_recovery_tag_probe_failed")
    issues.extend(publication_recovery_snapshot_issues(
        checkpoint=checkpoint,
        status=status,
        candidate=candidate,
        candidate_hash=expected_candidate_hash,
        certificate_digest=expected_certificate_digest,
        ledger_entry_digest=expected_ledger_entry_digest,
        authorization=authorization,
        gate_ledger=gate_ledger,
        active_bots=active_bots,
        strict_bots=strict_bots,
        completion_tags=completion_tags,
        completed=os.path.lexists(candidate / ".completed"),
        official_jobs_active=jobs_active,
    ))
    if status_ledger.get("entry_digest") != expected_ledger_entry_digest:
        issues.append("first_strict_publication_recovery_expected_ledger_mismatch")
    if status.get("certificate_digest") != expected_certificate_digest:
        issues.append("first_strict_publication_recovery_expected_certificate_mismatch")
    if issues:
        raise CompletedFirstStrictPublicationRecoveryError(_unique(issues))

    payload = {
        "schema_version": CLAIM_SCHEMA_VERSION,
        "kind": CLAIM_KIND,
        "evaluation_epoch": "national_tcp_policy_v1",
        "checkpoint": {
            "digest": canonical_digest(checkpoint),
            "workflow_run_id": expected_workflow_run_id,
            "checkpoint_revision": expected_checkpoint_revision,
            "stage": "verified",
            "next_v": FIRST_STRICT_POLICY_VERSION,
            "source_v": ARCHIVED_VERSION_HIGH_WATER,
        },
        "git": {
            "baseline_head": full_expected_baseline,
            "current_head": current_head,
            "origin_main": origin_head,
            "changed_paths": sorted(changed_paths),
            "self_blob_path": RECOVERY_SCRIPT,
        },
        "evaluation_contract": {
            "version": old_contract["version"],
            "baseline_hash": old_hash,
            "current_hash": current_contract["hash"],
            "evaluate_head_drift": {
                "allowed": True,
                "contract_paths": drift.get("head_contract_paths") or [],
                "external_paths": drift.get("head_external_paths") or [],
            },
        },
        "candidate": candidate_facts,
        "parked_request_digest": parked["request_digest"],
        "certificate": {
            "candidate_hash": expected_candidate_hash,
            "certificate_digest": expected_certificate_digest,
            "ledger_entry_digest": expected_ledger_entry_digest,
            "official_status_digest": canonical_digest(status),
            "authorization": authorization,
        },
        "final_gate_ledger_digest": __import__(
            "publication_transaction"
        ).publication_gate_ledger_digest(gate_ledger),
        "pool": {"active_bots": [], "strict_published_bots": []},
        "disposition": (
            "publication_only_preserve_signed_certificate_no_recertification"
        ),
    }
    return {**payload, "claim_digest": canonical_digest(payload)}


def _terminal_receipt(claim: dict[str, Any], intent: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "schema_version": TERMINAL_RECEIPT_SCHEMA_VERSION,
        "kind": TERMINAL_RECEIPT_KIND,
        "claim_digest": claim["claim_digest"],
        "publication_id": intent["publication_id"],
        "workflow_run_id": claim["checkpoint"]["workflow_run_id"],
        "candidate_hash": claim["certificate"]["candidate_hash"],
        "certificate_digest": claim["certificate"]["certificate_digest"],
        "ledger_entry_digest": claim["certificate"]["ledger_entry_digest"],
        "baseline_head": claim["git"]["baseline_head"],
        "migration_head": claim["git"]["current_head"],
        "baseline_contract_hash": claim["evaluation_contract"]["baseline_hash"],
        "migration_contract_hash": claim["evaluation_contract"]["current_hash"],
    }
    return {**payload, "receipt_digest": canonical_digest(payload)}


async def _delegate_commit_bot(strategy: str) -> dict[str, Any]:
    from tool_commit import commit_bot

    raw = await commit_bot.handler({
        "version": FIRST_STRICT_POLICY_VERSION,
        "source_v": ARCHIVED_VERSION_HIGH_WATER,
        "strategy": strategy,
        "review_approved": True,
    })
    content = raw.get("content") if isinstance(raw, dict) else None
    first = content[0] if isinstance(content, list) and content else {}
    try:
        return json.loads(first.get("text") or "{}")
    except Exception as exc:
        return {
            "committed": False,
            "error": "publication-result-unreadable",
            "reason": f"{type(exc).__name__}: {str(exc)[:240]}",
        }


def _resume_publishing(
    checkpoint: dict[str, Any],
    claim_digest: str,
) -> dict[str, Any]:
    issues = publishing_recovery_issues(checkpoint, claim_digest)
    if issues:
        raise CompletedFirstStrictPublicationRecoveryError(issues)
    os.environ.setdefault("POK_EVOLUTION_RUNTIME", "1")
    os.environ.setdefault("POK_REQUIRE_EVOLUTION_PUSH", "1")
    os.environ.setdefault("EVOLUTION_GIT_PUSH", "1")
    os.environ["POK_OPERATOR_FIRST_STRICT_FINALIZE"] = str(os.getpid())
    strategy = str((checkpoint.get("publication_intent") or {}).get(
        "strategy_tag"
    ) or "")
    return asyncio.run(_delegate_commit_bot(strategy))


def execute_claim(root: Path, checkpoint: dict[str, Any], claim: dict[str, Any]) -> dict[str, Any]:
    from evolution_infra import (
        _git as infra_git,
        evolution_git_push_enabled,
        evolution_git_push_required,
        read_pipeline_checkpoint,
        remote_completion_ref_snapshot,
        write_pipeline_checkpoint,
    )
    from national_runtime_authority import strict_published_bot_names
    from official_certification import publish_certificate_attestation, status_payload
    from publication_transaction import (
        build_publication_intent,
        file_sha256,
        publication_gate_ledger_digest,
    )
    from tool_commit import _official_certificate_projection, validate_commit_gate_ledger

    claim_path = publish_claim(root, claim)
    candidate = root / "bots" / bot_name(FIRST_STRICT_POLICY_VERSION)
    status = status_payload(candidate)
    gate_ledger = validate_commit_gate_ledger(
        FIRST_STRICT_POLICY_VERSION,
        ARCHIVED_VERSION_HIGH_WATER,
        checkpoint,
        bot_dir=candidate,
    )
    strategy_base = (
        str(((checkpoint.get("master_plan") or {}).get("strategy") or ""))
        if isinstance(checkpoint.get("master_plan"), dict)
        else ""
    )
    strategy = receipt_strategy_tag(claim["claim_digest"], strategy_base)
    attestation = publish_certificate_attestation(status, candidate)
    relative = str(attestation.get("relative_path") or "")
    certificate_path = root / relative
    certificate = _official_certificate_projection(status)
    intent = build_publication_intent(
        checkpoint=checkpoint,
        candidate_artifact_hash=str(certificate.get("candidate_hash") or ""),
        certificate_digest=str(certificate.get("certificate_digest") or ""),
        certificate_policy_id=str(certificate.get("policy_id") or ""),
        official_status=status,
        certificate_relative_path=relative,
        certificate_file_sha256=file_sha256(certificate_path),
        certificate_attestation_digest=str(attestation.get("attestation_digest") or ""),
        final_gate_ledger_digest=publication_gate_ledger_digest(gate_ledger),
        strategy_tag=strategy,
        rating_info="",
        baseline_head=infra_git("rev-parse", "refs/heads/main").strip(),
        baseline_remote_main=infra_git(
            "rev-parse", "refs/remotes/origin/main", check=False
        ).strip(),
        baseline_remote_completion_refs=(
            remote_completion_ref_snapshot()
            if evolution_git_push_required() or evolution_git_push_enabled()
            else {}
        ),
        prepublication_strict_bots=strict_published_bot_names(),
        remote_publication_required=evolution_git_push_required(),
        remote_publication_enabled=evolution_git_push_enabled(),
    )
    receipt = _terminal_receipt(claim, intent)
    if not write_pipeline_checkpoint(
        FIRST_STRICT_POLICY_VERSION,
        ARCHIVED_VERSION_HIGH_WATER,
        "publishing",
        publication_intent=intent,
        audit_context={CHECKPOINT_RECEIPT_KEY: receipt},
        expected_checkpoint_revision=checkpoint.get("checkpoint_revision"),
        expected_checkpoint_stage="verified",
        expected_workflow_run_id=checkpoint.get("workflow_run_id"),
    ):
        raise CompletedFirstStrictPublicationRecoveryError([
            "first_strict_publication_recovery_publishing_cas_failed"
        ])
    publishing = read_pipeline_checkpoint() or {}
    issues = publishing_recovery_issues(publishing, claim["claim_digest"])
    if issues:
        raise CompletedFirstStrictPublicationRecoveryError(issues)
    os.environ.setdefault("POK_EVOLUTION_RUNTIME", "1")
    os.environ.setdefault("POK_REQUIRE_EVOLUTION_PUSH", "1")
    os.environ.setdefault("EVOLUTION_GIT_PUSH", "1")
    os.environ["POK_OPERATOR_FIRST_STRICT_FINALIZE"] = str(os.getpid())
    result = asyncio.run(_delegate_commit_bot(strategy))
    return {
        **result,
        "publication_recovery_claim_digest": claim["claim_digest"],
        "publication_recovery_claim_path": str(claim_path),
        "publication_recovery_receipt_digest": receipt["receipt_digest"],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--acknowledge-runtime-checkout", action="store_true")
    parser.add_argument("--claim-digest")
    parser.add_argument("--expected-baseline-head", required=True)
    parser.add_argument("--expected-baseline-contract-hash", required=True)
    parser.add_argument("--expected-current-head", required=True)
    parser.add_argument("--expected-workflow-run-id", required=True)
    parser.add_argument("--expected-checkpoint-revision", required=True, type=int)
    parser.add_argument("--expected-candidate-hash", required=True)
    parser.add_argument("--expected-certificate-digest", required=True)
    parser.add_argument("--expected-ledger-entry-digest", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.execute and not args.acknowledge_runtime_checkout:
        raise CompletedFirstStrictPublicationRecoveryError([
            "first_strict_publication_recovery_acknowledgement_required"
        ])
    if args.execute and not args.claim_digest:
        raise CompletedFirstStrictPublicationRecoveryError([
            "first_strict_publication_recovery_claim_acknowledgement_required"
        ])
    from evolution_infra import read_pipeline_checkpoint

    checkpoint = read_pipeline_checkpoint()
    if not isinstance(checkpoint, dict):
        raise CompletedFirstStrictPublicationRecoveryError([
            "first_strict_publication_recovery_checkpoint_missing"
        ])
    if checkpoint.get("stage") == "publishing":
        if not args.execute:
            receipt = ((checkpoint.get("audit_context") or {}).get(
                CHECKPOINT_RECEIPT_KEY
            ) or {})
            print(json.dumps({
                "mode": "dry_run",
                "mutates": False,
                "status": "publishing_recovery_available",
                "claim_digest": receipt.get("claim_digest"),
                "publication_id": receipt.get("publication_id"),
                "issues": publishing_recovery_issues(
                    checkpoint, str(receipt.get("claim_digest") or "")
                ),
            }, ensure_ascii=False, indent=2))
            return 0
        result = _resume_publishing(checkpoint, str(args.claim_digest))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("committed") is True else 1

    claim = build_claim(
        ROOT,
        checkpoint=checkpoint,
        expected_baseline_head=args.expected_baseline_head,
        expected_baseline_contract_hash=args.expected_baseline_contract_hash,
        expected_current_head=args.expected_current_head,
        expected_workflow_run_id=args.expected_workflow_run_id,
        expected_checkpoint_revision=args.expected_checkpoint_revision,
        expected_candidate_hash=args.expected_candidate_hash,
        expected_certificate_digest=args.expected_certificate_digest,
        expected_ledger_entry_digest=args.expected_ledger_entry_digest,
    )
    if not args.execute:
        print(json.dumps({
            **claim,
            "mode": "dry_run",
            "mutates": False,
            "next_step": (
                "repeat with --execute --acknowledge-runtime-checkout "
                f"--claim-digest {claim['claim_digest']} and identical expected values"
            ),
        }, ensure_ascii=False, indent=2))
        return 0
    if args.claim_digest != claim.get("claim_digest"):
        raise CompletedFirstStrictPublicationRecoveryError([
            "first_strict_publication_recovery_reviewed_claim_mismatch"
        ])
    result = execute_claim(ROOT, checkpoint, claim)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("committed") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
