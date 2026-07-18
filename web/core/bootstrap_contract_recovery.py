"""Operator-only recovery authority for a parked first-strict bootstrap.

This owner exists for one narrow crash-safe case: an unpublished v143 is
parked at ``official_bootstrap_required``, its explicit bootstrap job ended
inconclusively before any round, and a reviewed descendant HEAD changes the
official evaluation contract.  The checkpoint is never rewritten under the
new contract.  Instead an external, content-bound claim freezes the old
checkpoint/job/verdict identities and the canonical abandon transaction
consumes that claim.

The ordinary ``abandon_generation`` tool has no access to this authority.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import Any, Iterator

from bot_artifact import canonical_digest, hash_path
from bot_namespace import (
    ARCHIVED_VERSION_HIGH_WATER,
    EVALUATION_EPOCH,
    FIRST_STRICT_POLICY_VERSION,
    bot_name,
)


CLAIM_SCHEMA_VERSION = 2
CLAIM_KIND = "official-bootstrap-contract-change-abandon-claim"
CLAIM_DIRNAME = "official_bootstrap_contract_change_abandon"
ABANDON_REASON_PREFIX = "official_bootstrap_contract_change:"
PARKED_EVALUATION_CONTRACT_VERSION = 40
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_STRICT_FILES = frozenset({
    "national_bot.py",
    "policy.py",
    "precompute.py",
    "national_runtime_manifest.json",
    "policy_epoch_receipt.json",
})
_CLAIM_FIELDS = frozenset({
    "schema_version",
    "kind",
    "evaluation_epoch",
    "old_checkpoint",
    "git_contract_migration",
    "candidate",
    "parked_request_digest",
    "terminal_job",
    "first_strict_execution_success",
    "disposition",
    "claim_digest",
})
_FIRST_STRICT_SUCCESS_FIELDS = frozenset({
    "scope",
    "expected_receipts",
    "terminal_receipt",
    "proof_digest",
})


class BootstrapContractRecoveryError(RuntimeError):
    def __init__(self, issues: list[str]):
        self.issues = list(dict.fromkeys(str(item) for item in issues if str(item)))
        super().__init__("; ".join(self.issues[:12]))


def _read_succeeded_first_strict_execution(
    scope: Any,
    *,
    expected_receipts: Any,
    expected_terminal_receipt: Any,
) -> dict[str, Any]:
    """Keep the journal dependency lazy for the operator-only owner."""

    from first_strict_execution_journal import (
        read_succeeded_control_execution,
    )

    return read_succeeded_control_execution(
        scope,
        expected_receipts=expected_receipts,
        expected_terminal_receipt=expected_terminal_receipt,
    )


def validate_first_strict_execution_success(
    proof: Any,
) -> dict[str, Any]:
    """Reopen the immutable eight-sample authority frozen by a claim."""

    issues: list[str] = []
    if not isinstance(proof, dict) or set(proof) != _FIRST_STRICT_SUCCESS_FIELDS:
        raise BootstrapContractRecoveryError([
            "bootstrap_contract_first_strict_success_fields_invalid"
        ])
    scope = proof.get("scope")
    expected_receipts = proof.get("expected_receipts")
    terminal_receipt = proof.get("terminal_receipt")
    payload = {
        "scope": scope,
        "expected_receipts": expected_receipts,
        "terminal_receipt": terminal_receipt,
    }
    if (
        not isinstance(scope, dict)
        or not isinstance(expected_receipts, list)
        or len(expected_receipts) != 8
        or any(not isinstance(item, dict) for item in expected_receipts)
        or not isinstance(terminal_receipt, dict)
        or terminal_receipt.get("outcome") != "succeeded"
        or terminal_receipt.get("scope_digest") != canonical_digest(scope)
        or proof.get("proof_digest") != canonical_digest(payload)
    ):
        issues.append("bootstrap_contract_first_strict_success_shape_invalid")
    if not issues:
        try:
            observed = _read_succeeded_first_strict_execution(
                scope,
                expected_receipts=expected_receipts,
                expected_terminal_receipt=terminal_receipt,
            )
            if observed != terminal_receipt:
                issues.append(
                    "bootstrap_contract_first_strict_success_terminal_changed"
                )
        except Exception as exc:
            issues.append(
                "bootstrap_contract_first_strict_success_unverifiable:"
                f"{type(exc).__name__}"
            )
    if issues:
        raise BootstrapContractRecoveryError(issues)
    return proof


def _first_strict_execution_success_from_checkpoint(
    checkpoint: dict[str, Any],
) -> dict[str, Any]:
    """Extract and revalidate the exact successful precommit journal proof."""

    audit_context = checkpoint.get("audit_context")
    audit_context = audit_context if isinstance(audit_context, dict) else {}
    gate_results = checkpoint.get("gate_results")
    gate_results = gate_results if isinstance(gate_results, dict) else {}
    precommit = gate_results.get("precommit_eval")
    precommit = precommit if isinstance(precommit, dict) else {}
    national = precommit.get("national")
    national = national if isinstance(national, dict) else {}
    scope = precommit.get("control_execution_scope")
    audit_scope = audit_context.get("first_strict_control_execution_scope")
    matchups = national.get("matchups")
    matchups_valid = (
        isinstance(matchups, list)
        and bool(matchups)
        and all(
            isinstance(matchup, dict)
            and isinstance(matchup.get("repeats"), list)
            and all(
                isinstance(repeat, dict)
                for repeat in matchup.get("repeats")
            )
            for matchup in matchups
        )
    )
    receipts = (
        [
            repeat.get("execution_receipt")
            for matchup in (matchups or [])
            for repeat in (matchup.get("repeats") or [])
        ]
        if matchups_valid
        else []
    )
    terminal = precommit.get("first_strict_execution_terminal_receipt")
    if (
        precommit.get("passed") is not True
        or not matchups_valid
        or not isinstance(scope, dict)
        or scope != audit_scope
        or scope.get("workflow_run_id") != checkpoint.get("workflow_run_id")
        or scope.get("candidate_version") != checkpoint.get("next_v")
        or type(scope.get("checkpoint_revision")) is not int
        or int(scope["checkpoint_revision"])
        > int(checkpoint.get("checkpoint_revision") or 0)
        or len(receipts) != 8
        or any(not isinstance(item, dict) for item in receipts)
        or not isinstance(terminal, dict)
    ):
        raise BootstrapContractRecoveryError([
            "bootstrap_contract_first_strict_success_checkpoint_invalid"
        ])
    payload = {
        "scope": scope,
        "expected_receipts": receipts,
        "terminal_receipt": terminal,
    }
    return validate_first_strict_execution_success({
        **payload,
        "proof_digest": canonical_digest(payload),
    })


def _bootstrap_contract_chain_issues(
    parked: dict[str, Any],
    authorization: dict[str, Any],
    bootstrap_receipt: dict[str, Any],
    candidate_binding: dict[str, Any],
    control_receipt: dict[str, Any],
    *,
    expected_evaluation_contract_version: int,
    expected_evaluation_contract_hash: str,
    expected_checkpoint_contract_digest: str,
    expected_protocol_bootstrap_receipt_digest: str,
    expected_first_strict_control_receipt_digest: str,
    expected_protocol_bootstrap_receipt: dict[str, Any],
    expected_first_strict_control_receipt: dict[str, Any],
) -> list[str]:
    issues: list[str] = []
    if expected_evaluation_contract_version != PARKED_EVALUATION_CONTRACT_VERSION:
        issues.append("bootstrap_contract_evaluation_contract_chain_mismatch")
    if (
        parked.get("evaluation_contract_version")
        != expected_evaluation_contract_version
        or authorization.get("evaluation_contract_version")
        != expected_evaluation_contract_version
        or parked.get("evaluation_contract_hash")
        != expected_evaluation_contract_hash
        or authorization.get("evaluation_contract_hash")
        != expected_evaluation_contract_hash
    ):
        issues.append("bootstrap_contract_evaluation_contract_chain_mismatch")
    if (
        parked.get("checkpoint_contract_digest")
        != expected_checkpoint_contract_digest
        or authorization.get("checkpoint_contract_digest")
        != expected_checkpoint_contract_digest
    ):
        issues.append("bootstrap_contract_checkpoint_contract_chain_mismatch")
    if (
        authorization.get("protocol_bootstrap_receipt_digest")
        != expected_protocol_bootstrap_receipt_digest
        or parked.get("protocol_bootstrap_receipt_digest")
        != expected_protocol_bootstrap_receipt_digest
        or authorization.get("first_strict_control_receipt_digest")
        != expected_first_strict_control_receipt_digest
        or parked.get("first_strict_control_receipt_digest")
        != expected_first_strict_control_receipt_digest
        or authorization.get("first_strict_control_receipt_digest")
        != control_receipt.get("receipt_digest")
    ):
        issues.append("bootstrap_contract_control_receipt_chain_mismatch")
    parked_protocol = parked.get("protocol_bootstrap_receipt")
    parked_protocol = (
        parked_protocol if isinstance(parked_protocol, dict) else {}
    )
    parked_control = parked.get("first_strict_control_receipt")
    parked_control = parked_control if isinstance(parked_control, dict) else {}
    if (
        parked_protocol != expected_protocol_bootstrap_receipt
        or parked_protocol.get("receipt_digest")
        != expected_protocol_bootstrap_receipt_digest
    ):
        issues.append("bootstrap_contract_embedded_protocol_receipt_mismatch")
    if (
        parked_control != expected_first_strict_control_receipt
        or parked_control != control_receipt
        or parked_control.get("receipt_digest")
        != expected_first_strict_control_receipt_digest
    ):
        issues.append("bootstrap_contract_embedded_control_receipt_mismatch")
    policy = bootstrap_receipt.get("bootstrap_policy")
    policy = policy if isinstance(policy, dict) else {}
    if (
        authorization.get("bootstrap_control_receipt_digest")
        != bootstrap_receipt.get("receipt_digest")
        or authorization.get("candidate_binding_digest")
        != candidate_binding.get("candidate_binding_digest")
        or parked.get("bootstrap_policy_digest")
        != policy.get("contract_digest")
    ):
        issues.append("bootstrap_contract_embedded_binding_chain_mismatch")
    return issues


def _git(root: Path, *args: str, binary: bool = False) -> bytes | str:
    proc = subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True,
        text=not binary, timeout=30, check=False,
    )
    if proc.returncode != 0:
        stderr = proc.stderr if isinstance(proc.stderr, str) else proc.stderr.decode(errors="replace")
        raise RuntimeError(stderr.strip() or f"git {' '.join(args)} failed")
    return proc.stdout


def _git_absence(root: Path, *args: str) -> bool:
    proc = subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, timeout=30, check=False,
    )
    if proc.returncode == 1:
        return True
    if proc.returncode == 0:
        return False
    raise RuntimeError(f"git {' '.join(args)} returned {proc.returncode}")


def _full_commit(root: Path, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("empty Git revision")
    resolved = str(_git(root, "rev-parse", "--verify", f"{value}^{{commit}}" )).strip()
    if not _HEX40.fullmatch(resolved):
        raise RuntimeError("Git revision did not resolve to one full commit")
    return resolved


def _checkpoint_digest(checkpoint: dict[str, Any]) -> str:
    return canonical_digest(checkpoint)


def _contract_hash_at_head(
    root: Path,
    head: str,
    contract: dict[str, Any],
) -> str:
    """Reproduce an old contract hash from Git plus the live untracked Bot.

    ``evaluation_contract_hash`` reads tracked files plus untracked files below
    active Bot prefixes.  A parked candidate is intentionally untracked, so an
    old-tree proof must compose Git blobs with that exact live tree.
    """

    from evaluation_contract import is_contract_path

    names_raw = _git(root, "ls-tree", "-r", "-z", "--name-only", head, binary=True)
    assert isinstance(names_raw, bytes)
    tracked = {
        item.decode("utf-8", errors="replace")
        for item in names_raw.split(b"\0") if item
    }
    files = {name for name in tracked if is_contract_path(name, contract)}
    for prefix in contract.get("path_prefixes") or []:
        if not str(prefix).startswith("bots/national_v"):
            continue
        base = root / str(prefix).rstrip("/")
        if not base.exists():
            continue
        for current, dirnames, filenames in os.walk(base):
            dirnames[:] = [name for name in dirnames if name not in {
                "__pycache__", ".pytest_cache", ".mypy_cache",
            }]
            for filename in filenames:
                if filename.endswith((".pyc", ".pyo")):
                    continue
                relative = (Path(current) / filename).relative_to(root).as_posix()
                if is_contract_path(relative, contract):
                    files.add(relative)
    digest = hashlib.sha256()
    digest.update(f"contract-v{contract.get('version')}\n".encode())
    for relative in sorted(files):
        digest.update(relative.encode("utf-8", errors="replace") + b"\0")
        if relative in tracked:
            payload = _git(root, "show", f"{head}:{relative}", binary=True)
            assert isinstance(payload, bytes)
        else:
            payload = (root / relative).read_bytes()
        digest.update(payload)
        digest.update(b"\0")
    return digest.hexdigest()


def _safe_candidate(root: Path, version: int, expected_hash: str) -> dict[str, Any]:
    candidate = root / "bots" / bot_name(version)
    metadata = os.lstat(candidate)
    issues: list[str] = []
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        issues.append("bootstrap_contract_candidate_not_regular_directory")
    entries = {item.name for item in candidate.iterdir()}
    if entries != _STRICT_FILES:
        issues.append("bootstrap_contract_candidate_not_exact_five_files")
    for item in candidate.iterdir():
        item_meta = os.lstat(item)
        if not stat.S_ISREG(item_meta.st_mode) or stat.S_ISLNK(item_meta.st_mode):
            issues.append(f"bootstrap_contract_candidate_entry_unsafe:{item.name}")
    observed_hash = hash_path(candidate)
    if observed_hash != expected_hash:
        issues.append("bootstrap_contract_candidate_hash_mismatch")
    relative = f"bots/{bot_name(version)}"
    if not _git_absence(root, "ls-files", "--error-unmatch", relative):
        issues.append("bootstrap_contract_candidate_tracked")
    for tag in (f"national-bot-v{version}", f"national-high-water-v{version}"):
        if not _git_absence(root, "show-ref", "--verify", "--quiet", f"refs/tags/{tag}"):
            issues.append(f"bootstrap_contract_candidate_tag_present:{tag}")
    if os.path.lexists(candidate / ".completed"):
        issues.append("bootstrap_contract_candidate_completed")
    if issues:
        raise BootstrapContractRecoveryError(issues)
    return {
        "path": relative,
        "artifact_hash": observed_hash,
        "files": sorted(entries),
    }


def _terminal_job_facts(
    root: Path,
    *,
    job_id: str,
    candidate: Path,
    candidate_hash: str,
    workflow_run_id: str,
    parked_request: dict[str, Any],
    expected_evaluation_contract_version: int,
    expected_evaluation_contract_hash: str,
    expected_checkpoint_contract_digest: str,
    expected_protocol_bootstrap_receipt_digest: str,
    expected_first_strict_control_receipt_digest: str,
    expected_protocol_bootstrap_receipt: dict[str, Any],
    expected_first_strict_control_receipt: dict[str, Any],
) -> dict[str, Any]:
    from official_bootstrap import (
        CONTROL_ID,
        _parked_request_issues,
        _validated_ledger_entries,
        first_strict_control_consumption,
    )
    from official_certification import official_compliance_verdict
    from official_certification_job import (
        _job_lock,
        _public_state,
        _read_json,
        _result_payload,
        _validate_request,
        job_root,
    )

    issues: list[str] = []
    if not _HEX64.fullmatch(job_id):
        raise BootstrapContractRecoveryError(["bootstrap_contract_job_id_invalid"])
    issues.extend(_parked_request_issues(parked_request, None))
    if parked_request.get("workflow_run_id") != workflow_run_id:
        issues.append("bootstrap_contract_parked_workflow_mismatch")
    if parked_request.get("candidate_hash") != candidate_hash:
        issues.append("bootstrap_contract_parked_candidate_hash_mismatch")
    if parked_request.get("bootstrap_control_id") != CONTROL_ID:
        issues.append("bootstrap_contract_parked_control_mismatch")
    directory = job_root() / job_id
    if not directory.is_dir() or directory.is_symlink():
        issues.append("bootstrap_contract_terminal_job_missing")
        raise BootstrapContractRecoveryError(issues)
    with _job_lock(directory):
        request = _read_json(directory / "request.json") or {}
        state = _read_json(directory / "state.json") or {}
        issues.extend(_validate_request(request))
        try:
            public = _public_state(directory, state)
            result = _result_payload(directory, state) or {}
        except Exception as exc:
            issues.append(f"bootstrap_contract_job_result_invalid:{type(exc).__name__}")
            public, result = {}, {}
    status = result.get("status") if isinstance(result.get("status"), dict) else {}
    progress = public.get("progress") if isinstance(public.get("progress"), dict) else {}
    spec = request.get("spec") if isinstance(request.get("spec"), dict) else {}
    identity = request.get("identity") if isinstance(request.get("identity"), dict) else {}
    selection = request.get("opponent_selection")
    selection = selection if isinstance(selection, dict) else {}
    if request.get("job_id") != job_id or state.get("job_id") != job_id:
        issues.append("bootstrap_contract_job_identity_mismatch")
    if public.get("pending") is not False or public.get("state") != "completed":
        issues.append("bootstrap_contract_job_not_terminal_completed")
    if progress.get("rounds_requested") != 8 or progress.get("rounds_completed") != 0:
        issues.append("bootstrap_contract_job_progress_not_zero_of_eight")
    if status.get("status") != "official-inconclusive":
        issues.append("bootstrap_contract_job_not_inconclusive")
    summary = status.get("summary") if isinstance(status.get("summary"), dict) else {}
    if summary.get("rounds_run") != 0:
        issues.append("bootstrap_contract_job_rounds_run_nonzero")
    verdict = official_compliance_verdict(status)
    if not verdict.get("inconclusive") or verdict.get("blocking") or verdict.get("violation"):
        issues.append("bootstrap_contract_job_candidate_violation_or_authority")
    opponent = selection.get("opponent")
    opponent = opponent if isinstance(opponent, dict) else {}
    if (
        spec.get("bootstrap_control_id") != CONTROL_ID
        or int(spec.get("self_play_rounds", -1)) != 5
        or int(spec.get("opponent_rounds", -1)) != 3
        or int(spec.get("target_hands", -1)) != 70
        or Path(str(spec.get("candidate") or "")).resolve() != candidate.resolve()
    ):
        issues.append("bootstrap_contract_job_spec_mismatch")
    if identity.get("candidate_hash") != candidate_hash:
        issues.append("bootstrap_contract_job_candidate_hash_mismatch")
    # Read-only validation is deliberate.  The general selector validator may
    # materialize a missing control cache; a command advertised as dry-run
    # cannot create that cache.  This path instead requires the exact control
    # already named by the frozen request and validates it in place.
    binding = selection.get("candidate_binding")
    binding = binding if isinstance(binding, dict) else {}
    if (
        binding.get("candidate_hash") != candidate_hash
        or binding.get("candidate_version") != FIRST_STRICT_POLICY_VERSION
        or binding.get("candidate_binding_digest") != canonical_digest({
            key: value for key, value in binding.items()
            if key != "candidate_binding_digest"
        })
    ):
        issues.append("bootstrap_contract_selection_candidate_binding_invalid")
    bootstrap_receipt = selection.get("bootstrap_control_receipt")
    bootstrap_receipt = bootstrap_receipt if isinstance(bootstrap_receipt, dict) else {}
    if bootstrap_receipt.get("receipt_digest") != canonical_digest({
        key: value for key, value in bootstrap_receipt.items()
        if key != "receipt_digest"
    }):
        issues.append("bootstrap_contract_selection_receipt_digest_invalid")
    control_receipt = bootstrap_receipt.get("first_strict_control_receipt")
    control_receipt = control_receipt if isinstance(control_receipt, dict) else {}
    if control_receipt.get("receipt_digest") != canonical_digest({
        key: value for key, value in control_receipt.items()
        if key != "receipt_digest"
    }):
        issues.append("bootstrap_contract_control_receipt_digest_invalid")
    control = control_receipt.get("control")
    control = control if isinstance(control, dict) else {}
    try:
        from first_strict_control import control_identity

        control_path = Path(str(control.get("path") or ""))
        if not control_path.is_absolute() or not control_path.exists():
            raise RuntimeError("materialized control missing")
        if control_identity(control_path) != control:
            issues.append("bootstrap_contract_materialized_control_identity_mismatch")
    except Exception as exc:
        issues.append(
            f"bootstrap_contract_materialized_control_invalid:{type(exc).__name__}"
        )
    if (
        bootstrap_receipt.get("candidate_binding") != binding
        or bootstrap_receipt.get("first_strict_control_receipt") != control_receipt
        or selection.get("candidate") != binding.get("candidate")
        or opponent.get("eligibility_receipt") != bootstrap_receipt
    ):
        issues.append("bootstrap_contract_selection_receipt_binding_mismatch")
    try:
        spec_opponent = Path(str(spec.get("opponent") or "")).resolve()
        selected_opponent = Path(str(opponent.get("path") or "")).resolve()
        control_opponent = Path(str(control.get("path") or "")).resolve()
    except Exception:
        spec_opponent = selected_opponent = control_opponent = Path(".")
    if (
        spec_opponent != selected_opponent
        or selected_opponent != control_opponent
        or identity.get("opponent_hash") != opponent.get("artifact_hash")
        or opponent.get("artifact_hash") != control.get("artifact_hash")
        or opponent.get("eligible") is not True
        or opponent.get("normal_official_opponent") is not False
        or opponent.get("strength_admitted") is not False
        or opponent.get("rating_eligible") is not False
    ):
        issues.append("bootstrap_contract_control_opponent_binding_mismatch")
    authorization = (
        selection.get("operator_bootstrap_authorization")
        if isinstance(selection, dict) else None
    )
    if not isinstance(authorization, dict) or authorization.get(
        "authorization_digest"
    ) != canonical_digest({
        key: value for key, value in (authorization or {}).items()
        if key != "authorization_digest"
    }):
        issues.append("bootstrap_contract_operator_authorization_invalid")
    elif (
        authorization.get("parked_request_digest") != parked_request.get("request_digest")
        or authorization.get("workflow_run_id") != workflow_run_id
        or authorization.get("candidate_hash") != candidate_hash
        or Path(str(authorization.get("candidate_path") or "")).resolve()
        != candidate.resolve()
        or authorization.get("candidate_version")
        != FIRST_STRICT_POLICY_VERSION
        or authorization.get("bootstrap_control_id") != CONTROL_ID
        or authorization.get("active_bots") != []
        or authorization.get("strict_published_bots") != []
        or authorization.get("normal_official_opponent") is not False
        or authorization.get("strength_admitted") is not False
        or authorization.get("rating_eligible") is not False
    ):
        issues.append("bootstrap_contract_operator_authorization_mismatch")
    if (
        Path(str(parked_request.get("candidate_path") or "")).resolve()
        != candidate.resolve()
        or parked_request.get("candidate_version")
        != FIRST_STRICT_POLICY_VERSION
        or parked_request.get("source_v") != ARCHIVED_VERSION_HIGH_WATER
        or parked_request.get("active_bots") != []
        or parked_request.get("strict_published_bots") != []
    ):
        issues.append("bootstrap_contract_parked_authority_mismatch")
    issues.extend(_bootstrap_contract_chain_issues(
        parked_request,
        authorization if isinstance(authorization, dict) else {},
        bootstrap_receipt,
        binding,
        control_receipt,
        expected_evaluation_contract_version=(
            expected_evaluation_contract_version
        ),
        expected_evaluation_contract_hash=expected_evaluation_contract_hash,
        expected_checkpoint_contract_digest=expected_checkpoint_contract_digest,
        expected_protocol_bootstrap_receipt_digest=(
            expected_protocol_bootstrap_receipt_digest
        ),
        expected_first_strict_control_receipt_digest=(
            expected_first_strict_control_receipt_digest
        ),
        expected_protocol_bootstrap_receipt=(
            expected_protocol_bootstrap_receipt
        ),
        expected_first_strict_control_receipt=(
            expected_first_strict_control_receipt
        ),
    ))
    entries, ledger_issues = _validated_ledger_entries()
    issues.extend(ledger_issues)
    deterministic = status.get("official_deterministic_status_receipt") or {}
    envelope = status.get("official_job_envelope") or {}
    try:
        from official_job_envelope import job_envelope_issues

        issues.extend(job_envelope_issues(
            envelope,
            expected_job_id=job_id,
            expected_request_digest=request.get("request_digest"),
            expected_attempt=int(state.get("attempt", 0) or 0),
            expected_candidate_hash=candidate_hash,
            expected_opponent_hash=identity.get("opponent_hash"),
        ))
    except Exception as exc:
        issues.append(
            f"bootstrap_contract_job_envelope_validation_error:{type(exc).__name__}"
        )
    matching = [
        entry for entry in entries
        if entry.get("candidate_hash") == candidate_hash
        and entry.get("outcome") == "official-inconclusive"
        and entry.get("deterministic_status_receipt_digest") == deterministic.get("receipt_digest")
        and entry.get("job_envelope_digest") == envelope.get("envelope_digest")
    ]
    if len(matching) != 1:
        issues.append("bootstrap_contract_non_authoritative_ledger_entry_not_unique")
        ledger_entry = {}
    else:
        ledger_entry = matching[0]
        if status.get("official_verdict_ledger_entry") != ledger_entry:
            issues.append("bootstrap_contract_status_ledger_entry_mismatch")
        if (
            ledger_entry.get("authoritative") is not False
            or ledger_entry.get("blocking") is not False
            or ledger_entry.get("classification") != "harness"
            or ledger_entry.get("certificate_digest") not in {None, ""}
        ):
            issues.append("bootstrap_contract_ledger_entry_has_authority")
    consumption = first_strict_control_consumption(CONTROL_ID)
    if (
        consumption.get("valid") is not True
        or consumption.get("successful_count") != 0
        or consumption.get("max_successful_consumptions") != 1
    ):
        issues.append("bootstrap_contract_control_consumption_not_zero_of_one")
    if issues:
        raise BootstrapContractRecoveryError(issues)
    return {
        "job_id": job_id,
        "request_digest": request["request_digest"],
        "state_revision": state.get("revision"),
        "result_digest": result["result_digest"],
        "status_digest": canonical_digest(status),
        "rounds_requested": 8,
        "rounds_completed": 0,
        "rounds_run": 0,
        "ledger_entry_digest": ledger_entry["entry_digest"],
        "ledger_sequence": ledger_entry["sequence"],
        "deterministic_status_receipt_digest": deterministic.get("receipt_digest"),
        "job_envelope_digest": envelope.get("envelope_digest"),
        "evidence_sha256": (
            status.get("official_deterministic_status_receipt") or {}
        ).get("evidence_sha256"),
        "evidence_archive_sha256": (
            status.get("official_evidence_archive") or {}
        ).get("archive_sha256"),
        "control_consumption": consumption,
    }


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
    expected_terminal_job_id: str,
) -> dict[str, Any]:
    """Build the exact dry-run claim or raise without mutating state."""

    root = Path(root).resolve()
    issues: list[str] = []
    if root.name != ".evolution_pok":
        issues.append("bootstrap_contract_requires_runtime_checkout")
    if checkpoint.get("stage") != "official_bootstrap_required":
        issues.append("bootstrap_contract_stage_not_parked")
    if (
        checkpoint.get("next_v") != FIRST_STRICT_POLICY_VERSION
        or checkpoint.get("source_v") != ARCHIVED_VERSION_HIGH_WATER
        or checkpoint.get("workflow_run_id") != expected_workflow_run_id
        or checkpoint.get("checkpoint_revision") != expected_checkpoint_revision
    ):
        issues.append("bootstrap_contract_checkpoint_identity_mismatch")
    if checkpoint.get("publication_intent") is not None:
        issues.append("bootstrap_contract_publication_intent_present")
    if checkpoint.get("official_job") is not None:
        issues.append("bootstrap_contract_attached_official_job_present")
    try:
        from checkpoint_schema import strict_checkpoint_event_identity
        strict_checkpoint_event_identity(
            checkpoint,
            expected_gen=FIRST_STRICT_POLICY_VERSION,
            project_root=root,
        )
    except Exception as exc:
        issues.append(f"bootstrap_contract_checkpoint_invalid:{type(exc).__name__}")
    baseline = checkpoint.get("repo_baseline") if isinstance(
        checkpoint.get("repo_baseline"), dict
    ) else {}
    old_contract = baseline.get("evaluation_contract") if isinstance(
        baseline.get("evaluation_contract"), dict
    ) else {}
    if (
        old_contract.get("version") != PARKED_EVALUATION_CONTRACT_VERSION
        or old_contract.get("stage") != "official_bootstrap_required"
        or not _HEX64.fullmatch(str(old_contract.get("hash") or ""))
        or baseline.get("error")
        or baseline.get("truncated") is True
    ):
        issues.append("bootstrap_contract_baseline_contract_invalid")
    try:
        full_expected_baseline = _full_commit(root, expected_baseline_head)
        full_baseline = _full_commit(root, str(baseline.get("head") or ""))
    except Exception as exc:
        issues.append(f"bootstrap_contract_baseline_head_invalid:{type(exc).__name__}")
        full_expected_baseline = expected_baseline_head
        full_baseline = ""
    if not _HEX40.fullmatch(expected_baseline_head) or full_baseline != full_expected_baseline:
        issues.append("bootstrap_contract_baseline_head_mismatch")
    if old_contract.get("hash") != expected_baseline_contract_hash:
        issues.append("bootstrap_contract_baseline_hash_mismatch")
    current_head = str(_git(root, "rev-parse", "HEAD")).strip()
    origin_head = str(_git(root, "rev-parse", "origin/main")).strip()
    current_branch = str(_git(root, "rev-parse", "--abbrev-ref", "HEAD")).strip()
    if current_branch != "main":
        issues.append("bootstrap_contract_runtime_branch_not_main")
    if (
        not _HEX40.fullmatch(expected_current_head)
        or current_head != expected_current_head
        or current_head != origin_head
    ):
        issues.append("bootstrap_contract_current_head_mismatch")
    if str(_git(root, "status", "--porcelain", "--untracked-files=no")).strip():
        issues.append("bootstrap_contract_tracked_worktree_dirty")
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", full_expected_baseline, current_head],
        cwd=str(root), capture_output=True, timeout=30, check=False,
    )
    if ancestor.returncode != 0:
        issues.append("bootstrap_contract_current_head_not_descendant")
    try:
        old_hash = _contract_hash_at_head(root, full_expected_baseline, old_contract)
        if old_hash != expected_baseline_contract_hash:
            issues.append("bootstrap_contract_baseline_content_hash_invalid")
    except Exception as exc:
        issues.append(f"bootstrap_contract_baseline_content_unavailable:{type(exc).__name__}")
        old_hash = ""
    from evaluation_contract import (
        build_evaluation_contract,
        classify_contract_paths,
    )
    from evolution_scope import changed_paths_between_heads
    changed_paths = changed_paths_between_heads(root, full_expected_baseline, current_head)
    if changed_paths is None:
        issues.append("bootstrap_contract_changed_paths_unavailable")
        changed_paths = []
    new_contract = build_evaluation_contract(
        root,
        candidate_v=FIRST_STRICT_POLICY_VERSION,
        source_v=ARCHIVED_VERSION_HIGH_WATER,
        checkpoint=checkpoint,
        stage="official_bootstrap_required",
        include_hash=True,
    )
    old_scope = classify_contract_paths(changed_paths, old_contract)
    new_scope = classify_contract_paths(changed_paths, new_contract)
    contract_paths = sorted(set(old_scope["contract_paths"]) | set(new_scope["contract_paths"]))
    if not contract_paths or new_contract.get("hash") == expected_baseline_contract_hash:
        issues.append("bootstrap_contract_evaluation_contract_unchanged")
    try:
        candidate_facts = _safe_candidate(
            root, FIRST_STRICT_POLICY_VERSION, expected_candidate_hash,
        )
    except BootstrapContractRecoveryError as exc:
        issues.extend(exc.issues)
        candidate_facts = {}
    parked = ((checkpoint.get("audit_context") or {}).get("official_bootstrap_request"))
    try:
        from official_bootstrap import _checkpoint_gate_contract_projection

        checkpoint_contract_digest = canonical_digest(
            _checkpoint_gate_contract_projection(checkpoint)
        )
    except Exception as exc:
        issues.append(
            f"bootstrap_contract_checkpoint_projection_unavailable:{type(exc).__name__}"
        )
        checkpoint_contract_digest = ""
    audit_context = checkpoint.get("audit_context")
    audit_context = audit_context if isinstance(audit_context, dict) else {}
    protocol_bootstrap = audit_context.get("protocol_bootstrap")
    protocol_bootstrap = (
        protocol_bootstrap if isinstance(protocol_bootstrap, dict) else {}
    )
    quality_gate = (checkpoint.get("gate_results") or {}).get("quality")
    quality_gate = quality_gate if isinstance(quality_gate, dict) else {}
    checkpoint_control_receipt = quality_gate.get(
        "first_strict_control_receipt"
    )
    checkpoint_control_receipt = (
        checkpoint_control_receipt
        if isinstance(checkpoint_control_receipt, dict)
        else {}
    )
    try:
        job_facts = _terminal_job_facts(
            root,
            job_id=expected_terminal_job_id,
            candidate=root / "bots" / bot_name(FIRST_STRICT_POLICY_VERSION),
            candidate_hash=expected_candidate_hash,
            workflow_run_id=expected_workflow_run_id,
            parked_request=parked,
            expected_evaluation_contract_version=int(
                old_contract.get("version", 0) or 0
            ),
            expected_evaluation_contract_hash=expected_baseline_contract_hash,
            expected_checkpoint_contract_digest=checkpoint_contract_digest,
            expected_protocol_bootstrap_receipt_digest=str(
                protocol_bootstrap.get("receipt_digest") or ""
            ),
            expected_first_strict_control_receipt_digest=str(
                checkpoint_control_receipt.get("receipt_digest") or ""
            ),
            expected_protocol_bootstrap_receipt=protocol_bootstrap,
            expected_first_strict_control_receipt=(
                checkpoint_control_receipt
            ),
        )
    except BootstrapContractRecoveryError as exc:
        issues.extend(exc.issues)
        job_facts = {}
    try:
        from official_certification import official_full_certified, status_payload
        status = status_payload(root / "bots" / bot_name(FIRST_STRICT_POLICY_VERSION))
        if official_full_certified(
            status, root / "bots" / bot_name(FIRST_STRICT_POLICY_VERSION)
        ):
            issues.append("bootstrap_contract_valid_certificate_present")
    except Exception as exc:
        issues.append(f"bootstrap_contract_certificate_status_unavailable:{type(exc).__name__}")
    try:
        from official_certification_job import job_snapshot
        snapshot = job_snapshot()
        if snapshot.get("pending") or snapshot.get("running"):
            issues.append("bootstrap_contract_official_job_active")
    except Exception as exc:
        issues.append(f"bootstrap_contract_job_snapshot_unavailable:{type(exc).__name__}")
    try:
        from evolution_core import get_active_bots
        from national_runtime_authority import strict_published_bot_names

        if list(get_active_bots()) or list(strict_published_bot_names()):
            issues.append("bootstrap_contract_first_strict_pool_not_empty")
    except Exception as exc:
        issues.append(f"bootstrap_contract_pool_authority_unavailable:{type(exc).__name__}")
    try:
        first_strict_execution_success = (
            _first_strict_execution_success_from_checkpoint(checkpoint)
        )
    except BootstrapContractRecoveryError as exc:
        issues.extend(exc.issues)
        first_strict_execution_success = {}
    if issues:
        raise BootstrapContractRecoveryError(issues)
    payload = {
        "schema_version": CLAIM_SCHEMA_VERSION,
        "kind": CLAIM_KIND,
        "evaluation_epoch": EVALUATION_EPOCH,
        "old_checkpoint": {
            "digest": _checkpoint_digest(checkpoint),
            "workflow_run_id": expected_workflow_run_id,
            "next_v": FIRST_STRICT_POLICY_VERSION,
            "source_v": ARCHIVED_VERSION_HIGH_WATER,
            "stage": "official_bootstrap_required",
            "checkpoint_revision": expected_checkpoint_revision,
        },
        "git_contract_migration": {
            "baseline_head": full_expected_baseline,
            "baseline_contract_hash": old_hash,
            "current_head": current_head,
            "current_contract_hash": new_contract["hash"],
            "changed_paths": sorted(changed_paths),
            "contract_paths": contract_paths,
        },
        "candidate": candidate_facts,
        "parked_request_digest": parked["request_digest"],
        "terminal_job": job_facts,
        "first_strict_execution_success": first_strict_execution_success,
        "disposition": "canonical_abandon_and_quarantine_without_evidence_migration",
    }
    return {**payload, "claim_digest": canonical_digest(payload)}


def claim_path(root: str | Path, claim_digest: str) -> Path:
    if not _HEX64.fullmatch(str(claim_digest or "")):
        raise BootstrapContractRecoveryError(["bootstrap_contract_claim_digest_invalid"])
    return (
        Path(root) / "web" / "core" / "results" / CLAIM_DIRNAME
        / f"{claim_digest}.json"
    )


def _read_regular_exact(path: Path, *, max_bytes: int = 4 * 1024 * 1024) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        live = os.lstat(path)
        identity = (
            opened.st_dev, opened.st_ino, opened.st_size,
            opened.st_mtime_ns, opened.st_ctime_ns, opened.st_nlink,
        )
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(live.st_mode)
            or stat.S_ISLNK(live.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (live.st_dev, live.st_ino)
            or opened.st_size > max_bytes
        ):
            raise OSError("bootstrap contract claim path is unsafe")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        live_after = os.lstat(path)
        after_identity = (
            after.st_dev, after.st_ino, after.st_size,
            after.st_mtime_ns, after.st_ctime_ns, after.st_nlink,
        )
        if (
            len(raw) > max_bytes
            or after_identity != identity
            or (live_after.st_dev, live_after.st_ino)
            != (opened.st_dev, opened.st_ino)
            or live_after.st_nlink != 1
        ):
            raise OSError("bootstrap contract claim changed during read")
        return raw
    finally:
        os.close(descriptor)


def _read_regular_exact_at(
    directory_fd: int,
    name: str,
    *,
    max_bytes: int = 4 * 1024 * 1024,
) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, dir_fd=directory_fd)
    try:
        opened = os.fstat(descriptor)
        live = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        identity = (
            opened.st_dev, opened.st_ino, opened.st_size,
            opened.st_mtime_ns, opened.st_ctime_ns, opened.st_nlink,
        )
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(live.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (live.st_dev, live.st_ino)
            or opened.st_size > max_bytes
        ):
            raise OSError("bootstrap contract claim path is unsafe")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        live_after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            len(raw) > max_bytes
            or (
                after.st_dev, after.st_ino, after.st_size,
                after.st_mtime_ns, after.st_ctime_ns, after.st_nlink,
            ) != identity
            or (live_after.st_dev, live_after.st_ino)
            != (opened.st_dev, opened.st_ino)
            or live_after.st_nlink != 1
        ):
            raise OSError("bootstrap contract claim changed during read")
        return raw
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, raw: bytes) -> None:
    view = memoryview(raw)
    offset = 0
    while offset < len(view):
        written = os.write(descriptor, view[offset:])
        if written <= 0:
            raise OSError("bootstrap contract claim write made no progress")
        offset += int(written)


def _validated_claim_directory(root: str | Path, *, create: bool) -> Path:
    root = Path(root).resolve()
    results = root / "web" / "core" / "results"
    for parent in (results.parent.parent, results.parent, results):
        metadata = os.lstat(parent)
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise BootstrapContractRecoveryError([
                "bootstrap_contract_claim_parent_unsafe"
            ])
    directory = results / CLAIM_DIRNAME
    created = False
    if create:
        try:
            os.mkdir(directory, 0o700)
            created = True
        except FileExistsError:
            pass
    metadata = os.lstat(directory)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or directory.parent.resolve(strict=True) != results.resolve(strict=True)
    ):
        raise BootstrapContractRecoveryError([
            "bootstrap_contract_claim_directory_unsafe"
        ])
    if created:
        descriptor = os.open(
            results, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return directory


@contextmanager
def _claim_directory_fd(root: str | Path, *, create: bool) -> Iterator[tuple[Path, int]]:
    directory = _validated_claim_directory(root, create=create)
    descriptor = os.open(
        directory,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode):
            raise BootstrapContractRecoveryError([
                "bootstrap_contract_claim_directory_unsafe"
            ])
        yield directory, descriptor
        after = os.fstat(descriptor)
        if (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino):
            raise BootstrapContractRecoveryError([
                "bootstrap_contract_claim_directory_changed"
            ])
    finally:
        os.close(descriptor)


def _validate_claim_envelope(
    claim: Any,
    expected_digest: str,
) -> dict[str, Any]:
    """Validate the external envelope and dynamically reopen its journal proof."""

    if (
        not isinstance(claim, dict)
        or set(claim) != _CLAIM_FIELDS
        or claim.get("schema_version") != CLAIM_SCHEMA_VERSION
        or claim.get("kind") != CLAIM_KIND
        or claim.get("evaluation_epoch") != EVALUATION_EPOCH
        or claim.get("claim_digest") != expected_digest
        or canonical_digest({
            key: value for key, value in claim.items()
            if key != "claim_digest"
        }) != expected_digest
    ):
        raise BootstrapContractRecoveryError([
            "bootstrap_contract_claim_invalid"
        ])
    success = validate_first_strict_execution_success(
        claim.get("first_strict_execution_success")
    )
    scope = success["scope"]
    old = claim.get("old_checkpoint")
    candidate = claim.get("candidate")
    migration = claim.get("git_contract_migration")
    if (
        not isinstance(old, dict)
        or set(old) != {
            "digest",
            "workflow_run_id",
            "next_v",
            "source_v",
            "stage",
            "checkpoint_revision",
        }
        or not isinstance(candidate, dict)
        or set(candidate) != {"path", "artifact_hash", "files"}
        or not isinstance(migration, dict)
        or set(migration) != {
            "baseline_head",
            "baseline_contract_hash",
            "current_head",
            "current_contract_hash",
            "changed_paths",
            "contract_paths",
        }
        or old.get("next_v") != FIRST_STRICT_POLICY_VERSION
        or old.get("source_v") != ARCHIVED_VERSION_HIGH_WATER
        or old.get("stage") != "official_bootstrap_required"
        or not _HEX64.fullmatch(str(old.get("digest") or ""))
        or not isinstance(old.get("workflow_run_id"), str)
        or not old.get("workflow_run_id")
        or type(old.get("checkpoint_revision")) is not int
        or int(old["checkpoint_revision"]) < 1
        or scope.get("workflow_run_id") != old.get("workflow_run_id")
        or scope.get("candidate_version") != old.get("next_v")
        or scope.get("candidate_label") != bot_name(old.get("next_v"))
        or type(scope.get("checkpoint_revision")) is not int
        or not 1 <= int(scope["checkpoint_revision"]) <= int(
            old["checkpoint_revision"]
        )
        or candidate.get("path") != f"bots/{bot_name(old.get('next_v'))}"
        or candidate.get("artifact_hash")
        != scope.get("candidate_artifact_hash")
        or candidate.get("files") != sorted(_STRICT_FILES)
        or not _HEX40.fullmatch(str(migration.get("current_head") or ""))
        or claim.get("disposition")
        != "canonical_abandon_and_quarantine_without_evidence_migration"
    ):
        raise BootstrapContractRecoveryError([
            "bootstrap_contract_claim_crossbinding_invalid"
        ])
    return claim


def validate_canonical_abandon_external_binding(
    root: str | Path,
    canonical_claim: dict[str, Any],
) -> dict[str, Any] | None:
    """Reopen a private claim indirectly bound by a canonical schema-2 reason."""

    reason = str(canonical_claim.get("abandon_reason") or "")
    if not reason.startswith(ABANDON_REASON_PREFIX):
        return None
    claim_digest = reason.removeprefix(ABANDON_REASON_PREFIX)
    if not _HEX64.fullmatch(claim_digest):
        raise BootstrapContractRecoveryError([
            "bootstrap_contract_canonical_reason_digest_invalid"
        ])
    external = load_claim(root, claim_digest)
    old = external["old_checkpoint"]
    checkpoint = canonical_claim.get("checkpoint") or {}
    migration = external["git_contract_migration"]
    git_state = canonical_claim.get("git_state") or {}
    candidate = canonical_claim.get("candidate") or {}
    transaction_id = str(canonical_claim.get("transaction_id") or "")
    if (
        canonical_claim.get("schema_version") != 2
        or abandon_reason(claim_digest) != reason
        or checkpoint.get("digest") != old.get("digest")
        or checkpoint.get("workflow_run_id") != old.get("workflow_run_id")
        or checkpoint.get("checkpoint_revision")
        != old.get("checkpoint_revision")
        or checkpoint.get("next_v") != old.get("next_v")
        or checkpoint.get("source_v") != old.get("source_v")
        or checkpoint.get("stage") != old.get("stage")
        or migration.get("current_head") != canonical_claim.get("git_head")
        or migration.get("current_head") != git_state.get("head")
        or (external.get("candidate") or {}).get("path")
        != candidate.get("path")
        or candidate.get("present") is not True
        or not _HEX64.fullmatch(transaction_id)
    ):
        raise BootstrapContractRecoveryError([
            "bootstrap_contract_canonical_crossbinding_invalid"
        ])
    source = Path(root) / str(candidate["path"])
    quarantine = (
        Path(root)
        / "web"
        / "core"
        / "results"
        / "policy_epoch_abandon_transactions"
        / transaction_id
        / "candidate"
    )
    observed = (
        quarantine
        if os.path.lexists(quarantine)
        else source
        if os.path.lexists(source)
        else None
    )
    try:
        observed_hash = hash_path(observed) if observed is not None else None
    except Exception as exc:
        raise BootstrapContractRecoveryError([
            "bootstrap_contract_canonical_candidate_unverifiable:"
            f"{type(exc).__name__}"
        ]) from exc
    if observed_hash != (external.get("candidate") or {}).get(
        "artifact_hash"
    ):
        raise BootstrapContractRecoveryError([
            "bootstrap_contract_canonical_candidate_hash_mismatch"
        ])
    return external


def publish_claim(root: str | Path, claim: dict[str, Any]) -> Path:
    """Durably publish one immutable external authority receipt."""

    digest = str(claim.get("claim_digest") or "")
    _validate_claim_envelope(claim, digest)
    path = claim_path(root, digest)
    raw = (json.dumps(claim, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    with _claim_directory_fd(root, create=True) as (_directory, directory_fd):
        try:
            existing = _read_regular_exact_at(directory_fd, path.name)
        except FileNotFoundError:
            existing = None
        except OSError as exc:
            raise BootstrapContractRecoveryError([
                f"bootstrap_contract_claim_path_unsafe:{type(exc).__name__}"
            ]) from exc
        if existing is not None:
            if existing != raw:
                raise BootstrapContractRecoveryError([
                    "bootstrap_contract_claim_path_conflict"
                ])
            return path
        descriptor = os.open(
            path.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        try:
            _write_all(descriptor, raw)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(directory_fd)
    return path


def load_claim(root: str | Path, claim_digest: str) -> dict[str, Any]:
    path = claim_path(root, claim_digest)
    try:
        with _claim_directory_fd(root, create=False) as (_directory, directory_fd):
            raw = _read_regular_exact_at(directory_fd, path.name)
        claim = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise BootstrapContractRecoveryError([
            f"bootstrap_contract_claim_path_unsafe:{type(exc).__name__}"
        ]) from exc
    return _validate_claim_envelope(claim, claim_digest)


def validate_claim_for_checkpoint(
    root: str | Path,
    checkpoint: dict[str, Any],
    claim_digest: str,
) -> dict[str, Any]:
    """Rebuild the live proof and require byte-identical claim authority."""

    claim = load_claim(root, claim_digest)
    identity = claim["old_checkpoint"]
    rebuilt = build_claim(
        root,
        checkpoint=checkpoint,
        expected_baseline_head=claim["git_contract_migration"]["baseline_head"],
        expected_baseline_contract_hash=claim["git_contract_migration"]["baseline_contract_hash"],
        expected_current_head=claim["git_contract_migration"]["current_head"],
        expected_workflow_run_id=identity["workflow_run_id"],
        expected_checkpoint_revision=identity["checkpoint_revision"],
        expected_candidate_hash=claim["candidate"]["artifact_hash"],
        expected_terminal_job_id=claim["terminal_job"]["job_id"],
    )
    if rebuilt != claim:
        raise BootstrapContractRecoveryError(["bootstrap_contract_live_claim_drift"])
    return claim


def abandon_reason(claim_digest: str) -> str:
    if not _HEX64.fullmatch(str(claim_digest or "")):
        raise BootstrapContractRecoveryError(["bootstrap_contract_claim_digest_invalid"])
    return f"official_bootstrap_contract_change:{claim_digest}"


def _historical_terminal_job_matches(
    claim: dict[str, Any],
    directory: Path,
) -> bool:
    """Reopen immutable job/result/verdict bytes without a live old candidate."""

    from official_bootstrap import _validated_ledger_entries
    from official_certification_job import (
        _job_lock,
        _public_state,
        _read_json,
        _result_payload,
        _validate_request,
    )

    expected = claim.get("terminal_job") or {}
    if directory.name != expected.get("job_id"):
        return False
    try:
        with _job_lock(directory):
            request = _read_json(directory / "request.json") or {}
            state = _read_json(directory / "state.json") or {}
            if _validate_request(request):
                return False
            public = _public_state(directory, state)
            result = _result_payload(directory, state) or {}
        status = result.get("status") if isinstance(result.get("status"), dict) else {}
        progress = public.get("progress") if isinstance(public.get("progress"), dict) else {}
        if (
            public.get("state") != "completed"
            or public.get("pending") is not False
            or request.get("request_digest") != expected.get("request_digest")
            or state.get("revision") != expected.get("state_revision")
            or result.get("result_digest") != expected.get("result_digest")
            or canonical_digest(status) != expected.get("status_digest")
            or progress.get("rounds_requested") != 8
            or progress.get("rounds_completed") != 0
            or (status.get("summary") or {}).get("rounds_run") != 0
        ):
            return False
        entries, issues = _validated_ledger_entries()
        if issues:
            return False
        matches = [
            entry for entry in entries
            if entry.get("entry_digest") == expected.get("ledger_entry_digest")
        ]
        if len(matches) != 1 or status.get("official_verdict_ledger_entry") != matches[0]:
            return False
        entry = matches[0]
        return bool(
            entry.get("sequence") == expected.get("ledger_sequence")
            and entry.get("outcome") == "official-inconclusive"
            and entry.get("classification") == "harness"
            and entry.get("authoritative") is False
            and entry.get("blocking") is False
            and entry.get("certificate_digest") in {None, ""}
        )
    except Exception:
        return False


def _finalized_canonical_abandon(
    root: Path,
    claim: dict[str, Any],
) -> dict[str, Any] | None:
    """Validate the canonical transaction that consumed the external proof."""

    from epoch_authority import validate_abandon_finalize_receipt
    from evolution_infra import load_abandoned_version_receipts

    results = root / "web" / "core" / "results"
    transactions = results / "policy_epoch_abandon_transactions"
    expected_reason = abandon_reason(claim["claim_digest"])
    expected_checkpoint = claim.get("old_checkpoint") or {}
    try:
        rows = load_abandoned_version_receipts(
            path=results / "abandoned_versions.jsonl",
            project_root=root,
        )
        matches: list[tuple[dict[str, Any], dict[str, Any], Path]] = []
        for directory in transactions.iterdir():
            if directory.is_symlink() or not directory.is_dir():
                continue
            try:
                canonical_claim = json.loads(
                    _read_regular_exact(directory / "claim.json").decode("utf-8")
                )
                receipt = json.loads(
                    _read_regular_exact(directory / "receipt.json").decode("utf-8")
                )
            except Exception:
                continue
            checkpoint = canonical_claim.get("checkpoint") or {}
            if (
                canonical_claim.get("abandon_reason") == expected_reason
                and checkpoint.get("workflow_run_id")
                == expected_checkpoint.get("workflow_run_id")
                and checkpoint.get("digest") == expected_checkpoint.get("digest")
                and checkpoint.get("checkpoint_revision")
                == expected_checkpoint.get("checkpoint_revision")
            ):
                if directory.name != canonical_claim.get("transaction_id"):
                    continue
                if validate_canonical_abandon_external_binding(
                    root,
                    canonical_claim,
                ) != claim:
                    continue
                validate_abandon_finalize_receipt(canonical_claim, receipt, rows)
                matches.append((canonical_claim, receipt, directory))
        if len(matches) != 1:
            return None
        canonical_claim, _receipt, directory = matches[0]
        quarantine = directory / "candidate"
        candidate = canonical_claim.get("candidate") or {}
        if candidate.get("present") is not True or not quarantine.is_dir() or quarantine.is_symlink():
            return None
        if hash_path(quarantine) != (claim.get("candidate") or {}).get("artifact_hash"):
            return None
        return {
            "transaction_id": directory.name,
            "finalize_receipt_digest": _receipt.get("receipt_digest"),
            "abandon_receipt_digest": _receipt.get("abandon_receipt_digest"),
            "candidate_state": _receipt.get("candidate_state"),
        }
    except Exception:
        return None


def _finalized_canonical_abandon_matches(
    root: Path,
    claim: dict[str, Any],
) -> bool:
    return _finalized_canonical_abandon(root, claim) is not None


def finalized_claim_result(
    root: str | Path,
    claim_digest: str,
) -> dict[str, Any] | None:
    """Return the exact completed terminal result after checkpoint clearance."""

    root = Path(root).resolve()
    try:
        claim = load_claim(root, claim_digest)
        from official_certification_job import job_root

        directory = job_root() / str((claim.get("terminal_job") or {}).get("job_id") or "")
        if not _historical_terminal_job_matches(claim, directory):
            return None
        terminal = _finalized_canonical_abandon(root, claim)
        if terminal is None:
            return None
        return {
            "status": "already_abandoned",
            "claim_digest": claim_digest,
            "old_workflow_run_id": (claim.get("old_checkpoint") or {}).get(
                "workflow_run_id"
            ),
            **terminal,
        }
    except Exception:
        return None


def incomplete_claim_resume_identity(
    root: str | Path,
    claim_digest: str,
) -> dict[str, Any] | None:
    """Reopen the checkpoint-cleared, finalize-receipt-missing crash prefix."""

    root = Path(root).resolve()
    try:
        claim = load_claim(root, claim_digest)
        from official_certification_job import job_root
        from tool_bot_management import _load_live_abandon_claim

        job_id = str((claim.get("terminal_job") or {}).get("job_id") or "")
        if not _historical_terminal_job_matches(claim, job_root() / job_id):
            return None
        canonical = _load_live_abandon_claim()
        if not isinstance(canonical, dict):
            return None
        expected = claim.get("old_checkpoint") or {}
        observed = canonical.get("checkpoint") or {}
        if (
            canonical.get("abandon_reason") != abandon_reason(claim_digest)
            or observed.get("workflow_run_id") != expected.get("workflow_run_id")
            or observed.get("next_v") != expected.get("next_v")
            or observed.get("source_v") != expected.get("source_v")
            or observed.get("stage") != expected.get("stage")
            or observed.get("checkpoint_revision")
            != expected.get("checkpoint_revision")
            or observed.get("digest") != expected.get("digest")
        ):
            return None
        return {
            "workflow_run_id": observed["workflow_run_id"],
            "next_v": observed["next_v"],
            "source_v": observed["source_v"],
            "stage": observed["stage"],
            "checkpoint_revision": observed["checkpoint_revision"],
        }
    except Exception:
        return None


def is_finalized_historical_bootstrap_job(
    root: str | Path,
    *,
    current_workflow_run_id: str,
    job_directory: str | Path,
) -> bool:
    """Return true only for an exact old job consumed by canonical abandon.

    This prevents a new v143 workflow from treating the immutable old 0/8 job
    (which has the same candidate path) as a live ambiguous authorization.  A
    changed request/result/verdict/claim/transaction remains related-invalid.
    """

    root = Path(root).resolve()
    directory = Path(job_directory)
    if directory.is_symlink() or not directory.is_dir() or not _HEX64.fullmatch(directory.name):
        return False
    claims = root / "web" / "core" / "results" / CLAIM_DIRNAME
    try:
        if claims.is_symlink() or not claims.is_dir():
            return False
        candidates = sorted(claims.glob(f"*.json"))
    except OSError:
        return False
    matches = []
    for path in candidates:
        digest = path.stem
        if not _HEX64.fullmatch(digest):
            continue
        try:
            claim = load_claim(root, digest)
        except Exception:
            continue
        old = claim.get("old_checkpoint") or {}
        if (
            old.get("workflow_run_id") == current_workflow_run_id
            or (claim.get("terminal_job") or {}).get("job_id") != directory.name
        ):
            continue
        if (
            _historical_terminal_job_matches(claim, directory)
            and _finalized_canonical_abandon_matches(root, claim)
        ):
            matches.append(digest)
    return len(matches) == 1
