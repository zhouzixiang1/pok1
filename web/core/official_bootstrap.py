"""One-time formal bootstrap for the first strict national TCP policy bot.

This authority is deliberately independent of every archived bot and historic
certificate.  It materializes the repository-owned ``first_strict_control``
from the current typed-policy runtime and admits it only as the opponent in the
first v143 full official-EXE suite.  It is never a normal official opponent,
never enters ratings, and can be consumed only once by a successful signed
``official-full-v5`` verdict.

The old v141 signed-ledger-root implementation is archived.  Nothing in this
module resolves, parses, imports, seals, or executes that artifact.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Iterable

from bot_artifact import canonical_digest, hash_path
from bot_namespace import (
    ARCHIVED_VERSION_HIGH_WATER,
    EVALUATION_EPOCH,
    FIRST_STRICT_POLICY_VERSION,
    bot_name,
    parse_bot_version,
    resolve_national_bot_spec,
)
from first_strict_control import (
    CONTROL_AUTHORITY,
    CONTROL_ID,
    build_control_receipt,
    control_identity,
    materialize_control,
    validate_control_receipt,
)
from official_verdict_ledger import ledger_integrity


ROOT = Path(__file__).resolve().parents[2]
BOTS_DIR = ROOT / "bots"
BOOTSTRAP_CONTROL_POLICY_PATH = (
    ROOT / "web" / "core" / "official_bootstrap_control.json"
)

BOOTSTRAP_POLICY_SCHEMA_VERSION = 1
BOOTSTRAP_POLICY_KIND = "official-first-strict-control-bootstrap-policy"
BOOTSTRAP_POLICY_ID = "official-first-strict-control-bootstrap-v1"
DEFAULT_BOOTSTRAP_CONTROL_ID = CONTROL_ID
FULL_V5_POLICY_ID = "official-full-v5"

CONTROL_SELECTION_KIND = "official-first-strict-control-selection"
CONTROL_RECEIPT_KIND = "official-first-strict-control-authorization-receipt"
CONTROL_RECEIPT_SCHEMA_VERSION = 1
PARKED_REQUEST_KIND = "official-first-strict-control-parked-request"
PARKED_REQUEST_SCHEMA_VERSION = 1
OPERATOR_AUTHORIZATION_KIND = "official-first-strict-control-operator-authorization"
OPERATOR_AUTHORIZATION_SCHEMA_VERSION = 1

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_PARKED_FACT_FIELDS = (
    "candidate_path",
    "candidate_label",
    "candidate_version",
    "candidate_hash",
    "source_v",
    "workflow_run_id",
    "checkpoint_contract_digest",
    "evaluation_contract_version",
    "evaluation_contract_hash",
    "protocol_bootstrap_receipt",
    "protocol_bootstrap_receipt_digest",
    "first_strict_control_receipt",
    "first_strict_control_receipt_digest",
    "active_bots",
    "strict_published_bots",
    "bootstrap_control_id",
    "bootstrap_policy_digest",
)


class BootstrapControlConfigurationError(ValueError):
    """The checked-in first-strict formal bootstrap policy is malformed."""


def _digest_bound(
    payload: dict[str, Any], *, field: str = "receipt_digest"
) -> dict[str, Any]:
    return {**payload, field: canonical_digest(payload)}


def _unique(issues: Iterable[object]) -> list[str]:
    return list(dict.fromkeys(str(item) for item in issues if str(item)))


def _read_policy(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise BootstrapControlConfigurationError(
            "first strict bootstrap policy is missing or non-regular"
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise BootstrapControlConfigurationError(
            f"first strict bootstrap policy unreadable:{type(exc).__name__}"
        ) from exc
    if not isinstance(value, dict):
        raise BootstrapControlConfigurationError(
            "first strict bootstrap policy must be an object"
        )
    return value


def load_first_strict_bootstrap_policy(
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Load the exact current-epoch policy; archived roots are not consulted."""

    target = Path(path) if path is not None else BOOTSTRAP_CONTROL_POLICY_PATH
    value = _read_policy(target)
    if value.get("schema_version") != BOOTSTRAP_POLICY_SCHEMA_VERSION:
        raise BootstrapControlConfigurationError("bootstrap policy schema mismatch")
    if value.get("kind") != BOOTSTRAP_POLICY_KIND:
        raise BootstrapControlConfigurationError("bootstrap policy kind mismatch")
    if value.get("policy_id") != BOOTSTRAP_POLICY_ID:
        raise BootstrapControlConfigurationError("bootstrap policy id mismatch")
    if value.get("epoch") != EVALUATION_EPOCH:
        raise BootstrapControlConfigurationError("bootstrap policy epoch mismatch")
    if value.get("candidate") != {
        "label": bot_name(FIRST_STRICT_POLICY_VERSION),
        "version": FIRST_STRICT_POLICY_VERSION,
        "source_version_authority": ARCHIVED_VERSION_HIGH_WATER,
    }:
        raise BootstrapControlConfigurationError("bootstrap candidate contract mismatch")
    if value.get("control") != {
        "control_id": CONTROL_ID,
        "authority": CONTROL_AUTHORITY,
        "normal_official_opponent": False,
        "strength_admitted": False,
        "rating_eligible": False,
    }:
        raise BootstrapControlConfigurationError("bootstrap control contract mismatch")
    if value.get("formal_suite") != {
        "policy_id": FULL_V5_POLICY_ID,
        "self_play_rounds": 5,
        "opponent_rounds": 3,
        "hands_per_round": 70,
    }:
        raise BootstrapControlConfigurationError("bootstrap formal suite mismatch")
    if value.get("authorization") != {
        "requires_empty_active_policy_pool": True,
        "requires_empty_strict_publication_pool": True,
        "max_successful_consumptions": 1,
        "operator_acknowledgement_required": True,
    }:
        raise BootstrapControlConfigurationError("bootstrap authorization mismatch")
    if value.get("historical_v141_root") != {
        "status": "retired_validation_history_only",
        "executable": False,
        "selectable": False,
    }:
        raise BootstrapControlConfigurationError("historical root retirement mismatch")

    # This also proves the control is composed from the current system runtime.
    identity = control_identity(materialize_control())
    if identity.get("control_id") != CONTROL_ID:
        raise BootstrapControlConfigurationError("bootstrap control identity mismatch")
    return value


def _policy_identity() -> dict[str, Any]:
    policy = load_first_strict_bootstrap_policy()
    return {
        "path": str(BOOTSTRAP_CONTROL_POLICY_PATH),
        "file_sha256": hashlib.sha256(
            BOOTSTRAP_CONTROL_POLICY_PATH.read_bytes()
        ).hexdigest(),
        "contract_digest": canonical_digest(policy),
        "policy_id": BOOTSTRAP_POLICY_ID,
        "epoch": EVALUATION_EPOCH,
    }


def _completion_tag_exists(version: int) -> bool:
    result = subprocess.run(
        [
            "git",
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/tags/national-bot-v{int(version)}",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return result.returncode == 0


def _candidate_binding(
    candidate_path: str | Path,
    *,
    allow_published: bool = False,
) -> tuple[dict[str, Any] | None, list[str]]:
    path = Path(candidate_path).expanduser().resolve()
    issues: list[str] = []
    version = parse_bot_version(path.name)
    if version != FIRST_STRICT_POLICY_VERSION:
        issues.append("official_bootstrap_candidate_must_be_first_strict_v143")
    expected_path = (BOTS_DIR / bot_name(FIRST_STRICT_POLICY_VERSION)).resolve()
    if path != expected_path:
        issues.append("official_bootstrap_candidate_path_mismatch")
    if path.is_symlink() or not path.is_dir():
        issues.append("official_bootstrap_candidate_directory_invalid")
    if (path / ".completed").exists() and not allow_published:
        issues.append("official_bootstrap_candidate_already_completed")
    if _completion_tag_exists(FIRST_STRICT_POLICY_VERSION) and not allow_published:
        issues.append("official_bootstrap_candidate_already_tagged")

    if path.is_dir():
        try:
            spec = resolve_national_bot_spec(
                path,
                role="candidate",
                repo_root=ROOT,
                require_completion=False,
                require_certificate=False,
            )
            issues.extend(
                f"official_bootstrap_candidate_spec:{item}"
                for item in spec.issues
            )
        except Exception as exc:
            issues.append(
                "official_bootstrap_candidate_spec_error:"
                f"{type(exc).__name__}:{str(exc)[:160]}"
            )
        try:
            from national_runtime_authority import (
                current_system_native_runtime_errors,
            )

            issues.extend(
                f"official_bootstrap_candidate_runtime:{item}"
                for item in current_system_native_runtime_errors(path)
            )
        except Exception as exc:
            issues.append(
                "official_bootstrap_candidate_runtime_error:"
                f"{type(exc).__name__}:{str(exc)[:160]}"
            )
    try:
        artifact_hash = hash_path(path)
    except Exception as exc:
        artifact_hash = ""
        issues.append(
            f"official_bootstrap_candidate_hash_error:{type(exc).__name__}"
        )
    if issues:
        return None, _unique(issues)
    payload = {
        "schema_version": 1,
        "kind": "official-first-strict-candidate-binding",
        "epoch": EVALUATION_EPOCH,
        "candidate": str(path),
        "candidate_label": path.name,
        "candidate_version": FIRST_STRICT_POLICY_VERSION,
        "candidate_hash": artifact_hash,
        "source_artifact_inherited": False,
    }
    return _digest_bound(payload, field="candidate_binding_digest"), []


def _validated_ledger_entries() -> tuple[list[dict[str, Any]], list[str]]:
    try:
        from official_verdict_ledger import _locked_ledger, _read_validated

        with _locked_ledger() as path:
            entries, issues = _read_validated(path)
        return list(entries), list(issues)
    except Exception as exc:
        return [], [
            "official_verdict_ledger_read_error:"
            f"{type(exc).__name__}:{str(exc)[:180]}"
        ]


def _consumption_report(
    control_id: str,
    receipt_digest: str,
    entries: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    matched: list[dict[str, Any]] = []
    successful: list[dict[str, Any]] = []
    issues: list[str] = []
    for entry in entries:
        if str(entry.get("bootstrap_control_id") or "") != control_id:
            continue
        matched.append(entry)
        observed = str(entry.get("bootstrap_control_receipt_digest") or "")
        if observed != receipt_digest:
            issues.append(
                "official_bootstrap_control_consumption_receipt_mismatch:"
                f"{entry.get('entry_digest', '')}"
            )
            continue
        if (
            entry.get("outcome") == "official-certified"
            and entry.get("policy_id") == FULL_V5_POLICY_ID
            and entry.get("mode") == "full"
            and entry.get("authoritative") is True
            and entry.get("blocking") is False
            and entry.get("classification") == "pass"
        ):
            successful.append(entry)
    if len(successful) > 1:
        issues.append(
            "official_bootstrap_control_successful_consumption_count_exceeded"
        )
    return {
        "bootstrap_control_id": control_id,
        "bootstrap_control_receipt_digest": receipt_digest,
        "consumed": bool(successful),
        "successful_count": len(successful),
        "max_successful_consumptions": 1,
        "matched_entry_digests": [item.get("entry_digest") for item in matched],
        "successful_entry_digests": [
            item.get("entry_digest") for item in successful
        ],
        "issues": _unique(issues),
    }


def first_strict_control_consumption(
    control_id: str = DEFAULT_BOOTSTRAP_CONTROL_ID,
) -> dict[str, Any]:
    if control_id != CONTROL_ID:
        return {
            "valid": False,
            "reason": "official_bootstrap_control_unknown",
            "consumed": False,
        }
    try:
        load_first_strict_bootstrap_policy()
    except Exception as exc:
        return {
            "valid": False,
            "reason": f"official_bootstrap_policy_invalid:{type(exc).__name__}",
            "consumed": False,
        }
    health = ledger_integrity()
    if not health.get("valid"):
        return {
            "valid": False,
            "reason": "official_bootstrap_signed_ledger_invalid",
            "ledger_issues": list(health.get("issues") or []),
            "consumed": False,
        }
    entries, issues = _validated_ledger_entries()
    if issues:
        return {
            "valid": False,
            "reason": "official_bootstrap_signed_ledger_invalid",
            "ledger_issues": issues,
            "consumed": False,
        }
    matching = [
        item
        for item in entries
        if item.get("bootstrap_control_id") == control_id
    ]
    receipt_digests = {
        str(item.get("bootstrap_control_receipt_digest") or "")
        for item in matching
    }
    successful = [
        item
        for item in matching
        if item.get("outcome") == "official-certified"
        and item.get("policy_id") == FULL_V5_POLICY_ID
        and item.get("mode") == "full"
        and item.get("authoritative") is True
        and item.get("blocking") is False
        and item.get("classification") == "pass"
    ]
    integrity_issues: list[str] = []
    if len(receipt_digests) > 1:
        integrity_issues.append(
            "official_bootstrap_control_multiple_consumption_receipts"
        )
    if len(successful) > 1:
        integrity_issues.append(
            "official_bootstrap_control_successful_consumption_count_exceeded"
        )
    return {
        "valid": not integrity_issues,
        "reason": "ok" if not integrity_issues else integrity_issues[0],
        "bootstrap_control_id": control_id,
        "consumed": bool(successful),
        "successful_count": len(successful),
        "max_successful_consumptions": 1,
        "successful_entry_digests": [
            item.get("entry_digest") for item in successful
        ],
        "issues": integrity_issues,
    }


def _checkpoint_gate_contract_projection(
    checkpoint: dict[str, Any],
) -> dict[str, Any]:
    audit = checkpoint.get("audit_context") or {}
    audit = audit if isinstance(audit, dict) else {}
    gates = checkpoint.get("gate_results") or {}
    gates = gates if isinstance(gates, dict) else {}
    return {
        "next_v": checkpoint.get("next_v"),
        "source_v": checkpoint.get("source_v"),
        "workflow_run_id": checkpoint.get("workflow_run_id"),
        "workflow_profile_id": checkpoint.get("workflow_profile_id"),
        "national_execution_mode": checkpoint.get("national_execution_mode"),
        "runtime_contract_ledger": checkpoint.get("runtime_contract_ledger"),
        "master_plan_runtime_contract_ledger": (
            (checkpoint.get("master_plan") or {}).get("runtime_contract_ledger")
            if isinstance(checkpoint.get("master_plan"), dict)
            else None
        ),
        "protocol_bootstrap": audit.get("protocol_bootstrap"),
        "protocol_bootstrap_prepare": audit.get("protocol_bootstrap_prepare"),
        "prepared_artifact_contract": audit.get("prepared_artifact_contract"),
        "precommit_eval_plan": audit.get("precommit_eval_plan"),
        "quality_gate": gates.get("quality"),
        "review_gate": gates.get("review"),
        "critic_gate": gates.get("critic"),
        "precommit_gate": gates.get("precommit_eval"),
    }


def _current_pipeline_checkpoint() -> dict[str, Any] | None:
    try:
        from evolution_infra import read_pipeline_checkpoint

        value = read_pipeline_checkpoint()
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def _current_operator_bootstrap_facts(
    candidate_path: str | Path,
    control_id: str,
    *,
    checkpoint: dict[str, Any] | None,
    expected_stage: str,
    expected_candidate_hash: str | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Build a live proof that only v143, an empty pool, and current bytes exist."""

    issues: list[str] = []
    if control_id != CONTROL_ID:
        return None, ["official_bootstrap_control_unknown"]
    try:
        policy_identity = _policy_identity()
    except Exception as exc:
        return None, [
            f"official_bootstrap_policy_invalid:{type(exc).__name__}:{str(exc)[:180]}"
        ]
    ckpt = checkpoint if isinstance(checkpoint, dict) else None
    if ckpt is None:
        return None, ["official_bootstrap_checkpoint_missing"]
    if ckpt.get("stage") != expected_stage:
        issues.append(
            "official_bootstrap_checkpoint_stage_mismatch:"
            f"expected={expected_stage}:actual={ckpt.get('stage')}"
        )
    if ckpt.get("next_v") != FIRST_STRICT_POLICY_VERSION:
        issues.append("official_bootstrap_checkpoint_candidate_version_mismatch")
    if ckpt.get("source_v") != ARCHIVED_VERSION_HIGH_WATER:
        issues.append("official_bootstrap_checkpoint_source_version_mismatch")

    binding, binding_issues = _candidate_binding(candidate_path)
    issues.extend(binding_issues)
    if binding is None:
        return None, _unique(issues)
    candidate_hash = str(binding.get("candidate_hash") or "")
    if expected_candidate_hash and candidate_hash != str(expected_candidate_hash):
        issues.append("official_bootstrap_candidate_hash_mismatch")

    try:
        from evolution_infra import get_active_bots_read_only
        from national_runtime_authority import strict_published_bot_names

        active_bots = list(get_active_bots_read_only())
        strict_bots = list(strict_published_bot_names())
    except Exception as exc:
        active_bots = []
        strict_bots = []
        issues.append(
            "official_bootstrap_pool_discovery_error:"
            f"{type(exc).__name__}:{str(exc)[:160]}"
        )
    if active_bots:
        issues.append("official_bootstrap_active_policy_pool_not_empty")
    if strict_bots:
        issues.append("official_bootstrap_strict_publication_pool_not_empty")

    try:
        control_receipt = build_control_receipt(ckpt, active_bots=active_bots)
        issues.extend(
            f"official_bootstrap_control:{item}"
            for item in validate_control_receipt(
                control_receipt,
                checkpoint=ckpt,
                candidate_version=FIRST_STRICT_POLICY_VERSION,
                source_version=ARCHIVED_VERSION_HIGH_WATER,
                active_bots=active_bots,
            )
        )
    except Exception as exc:
        control_receipt = None
        issues.append(
            "official_bootstrap_control_receipt_error:"
            f"{type(exc).__name__}:{str(exc)[:180]}"
        )

    try:
        from tool_commit import validate_commit_gate_ledger

        gate_ledger = validate_commit_gate_ledger(
            FIRST_STRICT_POLICY_VERSION,
            ARCHIVED_VERSION_HIGH_WATER,
            ckpt,
            bot_dir=Path(candidate_path).expanduser().resolve(),
        )
        if gate_ledger.get("ok") is not True:
            issues.append("official_bootstrap_parked_gate_ledger_invalid")
        if gate_ledger.get("current_code_fingerprint") != candidate_hash:
            issues.append("official_bootstrap_gate_candidate_hash_mismatch")
    except Exception as exc:
        gate_ledger = {}
        issues.append(
            "official_bootstrap_parked_gate_validation_error:"
            f"{type(exc).__name__}:{str(exc)[:160]}"
        )

    contract_projection = _checkpoint_gate_contract_projection(ckpt)
    try:
        from evaluation_contract import build_evaluation_contract

        evaluation_contract = build_evaluation_contract(
            ROOT,
            candidate_v=FIRST_STRICT_POLICY_VERSION,
            source_v=ARCHIVED_VERSION_HIGH_WATER,
            checkpoint=ckpt,
            stage=expected_stage,
            include_hash=True,
        )
        evaluation_hash = str(evaluation_contract.get("hash") or "")
        if not _HEX64.fullmatch(evaluation_hash):
            issues.append("official_bootstrap_evaluation_contract_hash_invalid")
    except Exception as exc:
        evaluation_contract = {}
        evaluation_hash = ""
        issues.append(
            "official_bootstrap_evaluation_contract_error:"
            f"{type(exc).__name__}:{str(exc)[:160]}"
        )

    protocol_receipt = (
        (ckpt.get("audit_context") or {}).get("protocol_bootstrap")
        if isinstance(ckpt.get("audit_context"), dict)
        else None
    )
    facts = {
        "candidate_path": str(Path(candidate_path).expanduser().resolve()),
        "candidate_label": bot_name(FIRST_STRICT_POLICY_VERSION),
        "candidate_version": FIRST_STRICT_POLICY_VERSION,
        "candidate_hash": candidate_hash,
        "source_v": ARCHIVED_VERSION_HIGH_WATER,
        "workflow_run_id": str(ckpt.get("workflow_run_id") or ""),
        "checkpoint_contract_digest": canonical_digest(contract_projection),
        "evaluation_contract_version": evaluation_contract.get("version"),
        "evaluation_contract_hash": evaluation_hash,
        "protocol_bootstrap_receipt": deepcopy(protocol_receipt),
        "protocol_bootstrap_receipt_digest": str(
            (protocol_receipt or {}).get("receipt_digest") or ""
        ),
        "first_strict_control_receipt": deepcopy(control_receipt),
        "first_strict_control_receipt_digest": str(
            (control_receipt or {}).get("receipt_digest") or ""
        ),
        "active_bots": active_bots,
        "strict_published_bots": strict_bots,
        "bootstrap_control_id": CONTROL_ID,
        "bootstrap_policy_digest": policy_identity["contract_digest"],
    }
    return facts, _unique(issues)


def build_operator_bootstrap_parked_request(
    candidate_path: str | Path,
    checkpoint: dict[str, Any],
    *,
    control_id: str = DEFAULT_BOOTSTRAP_CONTROL_ID,
    candidate_hash: str | None = None,
) -> dict[str, Any]:
    facts, issues = _current_operator_bootstrap_facts(
        candidate_path,
        control_id,
        checkpoint=checkpoint,
        expected_stage="verified",
        expected_candidate_hash=candidate_hash,
    )
    if facts is None or issues:
        return {"valid": False, "issues": issues, "request": None}
    request = _digest_bound(
        {
            "schema_version": PARKED_REQUEST_SCHEMA_VERSION,
            "kind": PARKED_REQUEST_KIND,
            **facts,
        },
        field="request_digest",
    )
    return {"valid": True, "issues": [], "request": request}


def _parked_request_issues(
    parked: Any,
    facts: dict[str, Any] | None,
) -> list[str]:
    if not isinstance(parked, dict):
        return ["official_bootstrap_parked_request_missing"]
    issues: list[str] = []
    unsigned = {key: value for key, value in parked.items() if key != "request_digest"}
    if parked.get("schema_version") != PARKED_REQUEST_SCHEMA_VERSION:
        issues.append("official_bootstrap_parked_request_schema_mismatch")
    if parked.get("kind") != PARKED_REQUEST_KIND:
        issues.append("official_bootstrap_parked_request_kind_mismatch")
    if parked.get("request_digest") != canonical_digest(unsigned):
        issues.append("official_bootstrap_parked_request_digest_mismatch")
    if isinstance(facts, dict):
        for field in _PARKED_FACT_FIELDS:
            if parked.get(field) != facts.get(field):
                issues.append(
                    f"official_bootstrap_parked_request_{field}_mismatch"
                )
    return _unique(issues)


def _authorization_receipt(
    candidate_binding: dict[str, Any],
    control_receipt: dict[str, Any],
) -> dict[str, Any]:
    control = control_receipt.get("control") or {}
    payload = {
        "schema_version": CONTROL_RECEIPT_SCHEMA_VERSION,
        "kind": CONTROL_RECEIPT_KIND,
        "role": "formal_first_strict_bootstrap_control",
        "bootstrap_control_id": CONTROL_ID,
        "bootstrap_policy": _policy_identity(),
        "policy_id": FULL_V5_POLICY_ID,
        "epoch": EVALUATION_EPOCH,
        "candidate_binding": deepcopy(candidate_binding),
        "first_strict_control_receipt": deepcopy(control_receipt),
        "first_strict_control_receipt_digest": control_receipt.get(
            "receipt_digest"
        ),
        "control_identity_digest": control.get("identity_digest"),
        "control_artifact_hash": control.get("artifact_hash"),
        "formal_suite": {
            "self_play_rounds": 5,
            "opponent_rounds": 3,
            "hands_per_round": 70,
        },
        "max_successful_consumptions": 1,
        "normal_official_opponent": False,
        "strength_admitted": False,
        "rating_eligible": False,
    }
    return _digest_bound(payload)


def _expected_selection(
    candidate_binding: dict[str, Any],
    control_receipt: dict[str, Any],
    entries: list[dict[str, Any]],
) -> dict[str, Any]:
    identity = control_identity(materialize_control())
    receipt = _authorization_receipt(candidate_binding, control_receipt)
    consumption = _consumption_report(
        CONTROL_ID,
        str(receipt["receipt_digest"]),
        entries,
    )
    return {
        "selected": True,
        "eligible": True,
        "reason": "first_strict_control_bootstrap",
        "kind": CONTROL_SELECTION_KIND,
        "bootstrap_control_id": CONTROL_ID,
        "candidate": candidate_binding["candidate"],
        "candidate_binding": deepcopy(candidate_binding),
        "opponent": {
            "bot": CONTROL_ID,
            "path": identity["path"],
            "artifact_hash": identity["artifact_hash"],
            "eligible": True,
            "reason": "first_strict_control_bootstrap",
            "authority": CONTROL_AUTHORITY,
            "eligibility_receipt": deepcopy(receipt),
            "normal_official_opponent": False,
            "strength_admitted": False,
            "rating_eligible": False,
        },
        "bootstrap_control_receipt": receipt,
        "consumption": consumption,
    }


def _selection_projection(selection: Any) -> dict[str, Any]:
    value = selection if isinstance(selection, dict) else {}
    opponent = value.get("opponent") if isinstance(value.get("opponent"), dict) else {}
    return {
        "selected": value.get("selected") is True,
        "eligible": value.get("eligible") is True,
        "reason": value.get("reason"),
        "kind": value.get("kind"),
        "bootstrap_control_id": value.get("bootstrap_control_id"),
        "candidate": value.get("candidate"),
        "candidate_binding": value.get("candidate_binding"),
        "bootstrap_control_receipt": value.get("bootstrap_control_receipt"),
        "opponent": {
            key: opponent.get(key)
            for key in (
                "bot",
                "path",
                "artifact_hash",
                "eligible",
                "reason",
                "authority",
                "eligibility_receipt",
                "normal_official_opponent",
                "strength_admitted",
                "rating_eligible",
            )
        },
    }


def _bound_mapping_issues(
    value: Any,
    *,
    digest_field: str,
    label: str,
) -> list[str]:
    if not isinstance(value, dict):
        return [f"{label}_missing"]
    unsigned = {key: item for key, item in value.items() if key != digest_field}
    if value.get(digest_field) != canonical_digest(unsigned):
        return [f"{label}_digest_mismatch"]
    return []


def _published_selection_validation(
    selection: Any,
    binding: dict[str, Any],
    validated_entries: list[dict[str, Any]],
    *,
    allow_consumed: bool,
) -> tuple[list[str], dict[str, Any] | None, dict[str, Any]]:
    """Validate a signed first-strict selection after checkout relocation.

    The portable attestation legitimately retains absolute paths from the
    autonomous checkout that produced it.  Those path strings stay covered by
    the certificate signature and their original receipt digests; semantic
    comparison replaces only checkout-local paths with current paths, then
    revalidates every content identity before consulting the consumption
    ledger bound to the original authorization receipt.
    """

    if not isinstance(selection, dict):
        return ["official_bootstrap_control_selection_missing"], None, {}
    issues: list[str] = []
    supplied_binding = selection.get("candidate_binding")
    issues.extend(_bound_mapping_issues(
        supplied_binding,
        digest_field="candidate_binding_digest",
        label="official_bootstrap_candidate_binding",
    ))
    if isinstance(supplied_binding, dict):
        supplied_semantic = {
            key: value
            for key, value in supplied_binding.items()
            if key not in {"candidate", "candidate_binding_digest"}
        }
        expected_semantic = {
            key: value
            for key, value in binding.items()
            if key not in {"candidate", "candidate_binding_digest"}
        }
        if supplied_semantic != expected_semantic:
            issues.append("official_bootstrap_candidate_binding_content_mismatch")
        if Path(str(supplied_binding.get("candidate") or "")).name != bot_name(
            FIRST_STRICT_POLICY_VERSION
        ):
            issues.append("official_bootstrap_candidate_binding_label_mismatch")
        if selection.get("candidate") != supplied_binding.get("candidate"):
            issues.append("official_bootstrap_selection_candidate_binding_mismatch")

    supplied_receipt = (
        selection.get("bootstrap_control_receipt")
        if isinstance(selection.get("bootstrap_control_receipt"), dict)
        else {}
    )
    control_receipt = supplied_receipt.get("first_strict_control_receipt")
    issues.extend(_bound_mapping_issues(
        control_receipt,
        digest_field="receipt_digest",
        label="official_bootstrap_first_strict_control_receipt",
    ))
    supplied_control = (
        control_receipt.get("control")
        if isinstance(control_receipt, dict)
        and isinstance(control_receipt.get("control"), dict)
        else {}
    )
    issues.extend(_bound_mapping_issues(
        supplied_control,
        digest_field="identity_digest",
        label="official_bootstrap_control_identity",
    ))
    try:
        current_control = control_identity(materialize_control())
    except Exception as exc:
        current_control = {}
        issues.append(
            f"official_bootstrap_control_identity_error:{type(exc).__name__}"
        )
    if supplied_control and current_control:
        supplied_control_semantic = {
            key: value
            for key, value in supplied_control.items()
            if key not in {"path", "identity_digest"}
        }
        current_control_semantic = {
            key: value
            for key, value in current_control.items()
            if key not in {"path", "identity_digest"}
        }
        if supplied_control_semantic != current_control_semantic:
            issues.append("official_bootstrap_control_identity_content_mismatch")
        if Path(str(supplied_control.get("path") or "")).name != Path(
            str(current_control.get("path") or "")
        ).name:
            issues.append("official_bootstrap_control_identity_label_mismatch")

    normalized_control_receipt = deepcopy(control_receipt) if isinstance(
        control_receipt, dict
    ) else {}
    if normalized_control_receipt and current_control:
        normalized_control_receipt["control"] = deepcopy(current_control)
        normalized_control_receipt["receipt_digest"] = canonical_digest({
            key: value
            for key, value in normalized_control_receipt.items()
            if key != "receipt_digest"
        })
        issues.extend(
            f"official_bootstrap_control:{item}"
            for item in validate_control_receipt(
                normalized_control_receipt,
                candidate_version=FIRST_STRICT_POLICY_VERSION,
                source_version=ARCHIVED_VERSION_HIGH_WATER,
                active_bots=[],
                force_protocol_refresh=False,
            )
        )

    issues.extend(_bound_mapping_issues(
        supplied_receipt,
        digest_field="receipt_digest",
        label="official_bootstrap_control_authorization_receipt",
    ))
    if isinstance(supplied_receipt, dict):
        opponent = selection.get("opponent")
        opponent = opponent if isinstance(opponent, dict) else {}
        if opponent.get("eligibility_receipt") != supplied_receipt:
            issues.append("official_bootstrap_opponent_authorization_receipt_mismatch")
        if supplied_receipt.get("candidate_binding") != supplied_binding:
            issues.append("official_bootstrap_authorization_candidate_binding_mismatch")
        if supplied_receipt.get("first_strict_control_receipt") != control_receipt:
            issues.append("official_bootstrap_authorization_control_receipt_mismatch")
        if supplied_receipt.get("first_strict_control_receipt_digest") != (
            control_receipt.get("receipt_digest")
            if isinstance(control_receipt, dict)
            else None
        ):
            issues.append("official_bootstrap_authorization_control_digest_mismatch")
        if supplied_receipt.get("control_identity_digest") != supplied_control.get(
            "identity_digest"
        ):
            issues.append("official_bootstrap_authorization_identity_digest_mismatch")

    try:
        current_policy = _policy_identity()
    except Exception as exc:
        current_policy = {}
        issues.append(f"official_bootstrap_policy_identity_error:{type(exc).__name__}")
    supplied_policy = (
        supplied_receipt.get("bootstrap_policy")
        if isinstance(supplied_receipt, dict)
        and isinstance(supplied_receipt.get("bootstrap_policy"), dict)
        else {}
    )
    if supplied_policy and current_policy:
        if {
            key: value for key, value in supplied_policy.items() if key != "path"
        } != {
            key: value for key, value in current_policy.items() if key != "path"
        }:
            issues.append("official_bootstrap_policy_identity_content_mismatch")
        if Path(str(supplied_policy.get("path") or "")).name != Path(
            str(current_policy.get("path") or "")
        ).name:
            issues.append("official_bootstrap_policy_identity_label_mismatch")

    normalized_receipt = deepcopy(supplied_receipt)
    if normalized_receipt and current_policy and normalized_control_receipt:
        normalized_receipt["bootstrap_policy"] = deepcopy(current_policy)
        normalized_receipt["candidate_binding"] = deepcopy(binding)
        normalized_receipt["first_strict_control_receipt"] = deepcopy(
            normalized_control_receipt
        )
        normalized_receipt["first_strict_control_receipt_digest"] = (
            normalized_control_receipt.get("receipt_digest")
        )
        normalized_receipt["control_identity_digest"] = current_control.get(
            "identity_digest"
        )
        normalized_receipt["receipt_digest"] = canonical_digest({
            key: value
            for key, value in normalized_receipt.items()
            if key != "receipt_digest"
        })
        expected_receipt = _authorization_receipt(
            binding,
            normalized_control_receipt,
        )
        if normalized_receipt != expected_receipt:
            issues.append("official_bootstrap_authorization_receipt_content_mismatch")

    expected = None
    if binding and normalized_control_receipt and normalized_receipt and current_control:
        expected = _expected_selection(
            binding,
            normalized_control_receipt,
            validated_entries,
        )
        normalized_selection = deepcopy(selection)
        normalized_selection["candidate"] = binding["candidate"]
        normalized_selection["candidate_binding"] = deepcopy(binding)
        normalized_selection["bootstrap_control_receipt"] = deepcopy(
            normalized_receipt
        )
        normalized_opponent = normalized_selection.get("opponent")
        normalized_opponent = (
            normalized_opponent if isinstance(normalized_opponent, dict) else {}
        )
        normalized_opponent["path"] = current_control["path"]
        normalized_opponent["eligibility_receipt"] = deepcopy(normalized_receipt)
        normalized_selection["opponent"] = normalized_opponent
        if _selection_projection(normalized_selection) != _selection_projection(expected):
            issues.append("official_bootstrap_control_selection_receipt_mismatch")

    original_receipt_digest = str(supplied_receipt.get("receipt_digest") or "")
    consumption = _consumption_report(
        CONTROL_ID,
        original_receipt_digest,
        validated_entries,
    )
    issues.extend(consumption.get("issues") or [])
    if consumption.get("consumed") and not allow_consumed:
        issues.append("official_bootstrap_control_already_consumed")
    return _unique(issues), expected, consumption


def validate_first_strict_control_selection_from_entries(
    selection: Any,
    control_id: str,
    candidate_path: str | Path,
    validated_entries: list[dict[str, Any]],
    *,
    allow_consumed: bool = False,
    allow_published: bool = False,
) -> dict[str, Any]:
    issues: list[str] = []
    if control_id != CONTROL_ID:
        return {
            "valid": False,
            "reason": "official_bootstrap_control_unknown",
            "issues": ["official_bootstrap_control_unknown"],
        }
    try:
        load_first_strict_bootstrap_policy()
    except Exception as exc:
        return {
            "valid": False,
            "reason": f"official_bootstrap_policy_invalid:{type(exc).__name__}",
            "issues": [f"official_bootstrap_policy_invalid:{type(exc).__name__}"],
        }
    if not isinstance(validated_entries, list) or any(
        not isinstance(item, dict) for item in validated_entries
    ):
        return {
            "valid": False,
            "reason": "official_bootstrap_locked_ledger_view_invalid",
            "issues": ["official_bootstrap_locked_ledger_view_invalid"],
        }
    binding, binding_issues = _candidate_binding(
        candidate_path,
        allow_published=allow_published or allow_consumed,
    )
    issues.extend(binding_issues)
    supplied_receipt = (
        selection.get("bootstrap_control_receipt")
        if isinstance(selection, dict)
        and isinstance(selection.get("bootstrap_control_receipt"), dict)
        else {}
    )
    control_receipt = supplied_receipt.get("first_strict_control_receipt")
    expected: dict[str, Any] | None = None
    consumption: dict[str, Any] = {}
    if allow_published and binding is not None:
        portable_issues, expected, consumption = _published_selection_validation(
            selection,
            binding,
            validated_entries,
            allow_consumed=allow_consumed,
        )
        issues.extend(portable_issues)
    else:
        issues.extend(
            f"official_bootstrap_control:{item}"
            for item in validate_control_receipt(
                control_receipt,
                candidate_version=FIRST_STRICT_POLICY_VERSION,
                source_version=ARCHIVED_VERSION_HIGH_WATER,
                active_bots=[],
                force_protocol_refresh=False,
            )
        )
    if not allow_published and binding is not None and isinstance(control_receipt, dict):
        expected = _expected_selection(binding, control_receipt, validated_entries)
        if _selection_projection(selection) != _selection_projection(expected):
            issues.append("official_bootstrap_control_selection_receipt_mismatch")
        consumption = expected["consumption"]
        issues.extend(consumption.get("issues") or [])
        if consumption.get("consumed") and not allow_consumed:
            issues.append("official_bootstrap_control_already_consumed")
    elif not allow_published:
        expected = None
        consumption = {}
    issues = _unique(issues)
    return {
        "valid": not issues,
        "reason": "ok" if not issues else issues[0],
        "issues": issues,
        "expected_selection": expected,
        "consumption": consumption,
    }


def validate_first_strict_control_selection(
    selection: Any,
    control_id: str,
    candidate_path: str | Path,
    *,
    allow_consumed: bool = False,
    allow_published: bool = False,
) -> dict[str, Any]:
    health = ledger_integrity()
    if not health.get("valid"):
        return {
            "valid": False,
            "reason": "official_bootstrap_signed_ledger_invalid",
            "issues": [
                "official_bootstrap_signed_ledger_invalid",
                *list(health.get("issues") or []),
            ],
        }
    entries, issues = _validated_ledger_entries()
    if issues:
        return {
            "valid": False,
            "reason": "official_bootstrap_signed_ledger_invalid",
            "issues": ["official_bootstrap_signed_ledger_invalid", *issues],
        }
    return validate_first_strict_control_selection_from_entries(
        selection,
        control_id,
        candidate_path,
        entries,
        allow_consumed=allow_consumed,
        allow_published=allow_published,
    )


def select_first_strict_control(
    control_id: str,
    candidate_path: str | Path,
    *,
    checkpoint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Select only the current system control for the live parked v143 run."""

    if control_id != CONTROL_ID:
        return {
            "selected": False,
            "eligible": False,
            "bootstrap_control_id": control_id,
            "reason": "official_bootstrap_control_unknown",
            "issues": ["official_bootstrap_control_unknown"],
        }
    ckpt = checkpoint if isinstance(checkpoint, dict) else _current_pipeline_checkpoint()
    parked = (
        ((ckpt or {}).get("audit_context") or {}).get("official_bootstrap_request")
        if isinstance((ckpt or {}).get("audit_context"), dict)
        else None
    )
    facts, fact_issues = _current_operator_bootstrap_facts(
        candidate_path,
        control_id,
        checkpoint=ckpt,
        expected_stage="official_bootstrap_required",
        expected_candidate_hash=str((parked or {}).get("candidate_hash") or ""),
    )
    issues = [*_parked_request_issues(parked, facts), *fact_issues]
    if facts is None or issues:
        return {
            "selected": False,
            "eligible": False,
            "bootstrap_control_id": control_id,
            "reason": issues[0] if issues else "official_bootstrap_authority_invalid",
            "issues": _unique(issues),
        }
    binding, binding_issues = _candidate_binding(candidate_path)
    if binding is None or binding_issues:
        return {
            "selected": False,
            "eligible": False,
            "bootstrap_control_id": control_id,
            "reason": binding_issues[0],
            "issues": binding_issues,
        }
    entries, ledger_issues = _validated_ledger_entries()
    if ledger_issues:
        return {
            "selected": False,
            "eligible": False,
            "bootstrap_control_id": control_id,
            "reason": "official_bootstrap_signed_ledger_invalid",
            "issues": ledger_issues,
        }
    selection = _expected_selection(
        binding,
        facts["first_strict_control_receipt"],
        entries,
    )
    if selection["consumption"].get("issues"):
        return {
            "selected": False,
            "eligible": False,
            "bootstrap_control_id": control_id,
            "reason": "official_bootstrap_control_consumption_invalid",
            "consumption": selection["consumption"],
        }
    if selection["consumption"].get("consumed"):
        return {
            "selected": False,
            "eligible": False,
            "bootstrap_control_id": control_id,
            "reason": "official_bootstrap_control_already_consumed",
            "consumption": selection["consumption"],
        }
    return selection


def _operator_authorization(
    selection: dict[str, Any],
    parked: dict[str, Any],
    facts: dict[str, Any],
) -> dict[str, Any]:
    receipt = selection.get("bootstrap_control_receipt") or {}
    binding = selection.get("candidate_binding") or {}
    payload = {
        "schema_version": OPERATOR_AUTHORIZATION_SCHEMA_VERSION,
        "kind": OPERATOR_AUTHORIZATION_KIND,
        "bootstrap_control_id": CONTROL_ID,
        "parked_request_digest": parked["request_digest"],
        "checkpoint_contract_digest": facts["checkpoint_contract_digest"],
        "evaluation_contract_version": facts["evaluation_contract_version"],
        "evaluation_contract_hash": facts["evaluation_contract_hash"],
        "workflow_run_id": facts["workflow_run_id"],
        "candidate_path": facts["candidate_path"],
        "candidate_version": facts["candidate_version"],
        "candidate_hash": facts["candidate_hash"],
        "protocol_bootstrap_receipt_digest": facts[
            "protocol_bootstrap_receipt_digest"
        ],
        "first_strict_control_receipt_digest": facts[
            "first_strict_control_receipt_digest"
        ],
        "bootstrap_control_receipt_digest": receipt.get("receipt_digest"),
        "candidate_binding_digest": binding.get("candidate_binding_digest"),
        "active_bots": [],
        "strict_published_bots": [],
        "normal_official_opponent": False,
        "strength_admitted": False,
        "rating_eligible": False,
    }
    return _digest_bound(payload, field="authorization_digest")


def authorize_operator_bootstrap_selection(
    selection: dict[str, Any],
    control_id: str,
    candidate_path: str | Path,
    *,
    checkpoint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ckpt = checkpoint if isinstance(checkpoint, dict) else _current_pipeline_checkpoint()
    parked = (
        ((ckpt or {}).get("audit_context") or {}).get("official_bootstrap_request")
        if isinstance((ckpt or {}).get("audit_context"), dict)
        else None
    )
    facts, fact_issues = _current_operator_bootstrap_facts(
        candidate_path,
        control_id,
        checkpoint=ckpt,
        expected_stage="official_bootstrap_required",
        expected_candidate_hash=str((parked or {}).get("candidate_hash") or ""),
    )
    issues = [*_parked_request_issues(parked, facts), *fact_issues]
    validation = validate_first_strict_control_selection(
        selection,
        control_id,
        candidate_path,
    )
    if validation.get("valid") is not True:
        issues.extend(validation.get("issues") or [validation.get("reason")])
    if facts is None or not isinstance(parked, dict) or issues:
        issues = _unique(issues)
        return {
            "valid": False,
            "reason": issues[0] if issues else "official_bootstrap_authorization_invalid",
            "issues": issues,
        }
    authorization = _operator_authorization(selection, parked, facts)
    return {
        "valid": True,
        "reason": "ok",
        "issues": [],
        "selection": {
            **selection,
            "operator_bootstrap_authorization": authorization,
        },
        "authorization": authorization,
    }


def validate_operator_bootstrap_authorized_selection(
    selection: dict[str, Any],
    control_id: str,
    candidate_path: str | Path,
    *,
    checkpoint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(selection, dict):
        return {
            "valid": False,
            "reason": "official_bootstrap_selection_missing",
            "issues": ["official_bootstrap_selection_missing"],
        }
    supplied = selection.get("operator_bootstrap_authorization")
    unsigned = {
        key: value
        for key, value in selection.items()
        if key != "operator_bootstrap_authorization"
    }
    current = authorize_operator_bootstrap_selection(
        unsigned,
        control_id,
        candidate_path,
        checkpoint=checkpoint,
    )
    if current.get("valid") is not True:
        return current
    if supplied != current.get("authorization"):
        return {
            "valid": False,
            "reason": "official_bootstrap_authorization_drift",
            "issues": ["official_bootstrap_authorization_drift"],
            "expected_authorization": current.get("authorization"),
        }
    return current


def validate_completed_operator_bootstrap_authorization(
    status: dict[str, Any],
    candidate_path: str | Path,
    *,
    checkpoint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Rebind a successful signed verdict to the parked empty-pool request."""

    if not isinstance(status, dict):
        return {
            "valid": False,
            "reason": "official_bootstrap_completed_status_missing",
            "issues": ["official_bootstrap_completed_status_missing"],
        }
    issues: list[str] = []
    candidate = Path(candidate_path).expanduser().resolve()
    identity = status.get("certification_identity") or {}
    spec = identity.get("spec") if isinstance(identity, dict) else {}
    control_id = str((spec or {}).get("bootstrap_control_id") or "")
    if control_id != CONTROL_ID:
        issues.append("official_bootstrap_completed_control_id_mismatch")
    if status.get("status") != "official-certified":
        issues.append("official_bootstrap_completed_status_not_certified")
    if status.get("mode") != "full" or status.get("policy_id") != FULL_V5_POLICY_ID:
        issues.append("official_bootstrap_completed_policy_mismatch")
    if Path(str((spec or {}).get("candidate") or "")).resolve() != candidate:
        issues.append("official_bootstrap_completed_candidate_path_mismatch")

    raw_selection = status.get("opponent_selection")
    raw_envelope = status.get("official_job_envelope")
    envelope_selection = (
        raw_envelope.get("opponent_selection")
        if isinstance(raw_envelope, dict)
        else None
    )
    if not isinstance(raw_selection, dict) or not isinstance(
        envelope_selection, dict
    ):
        issues.append("official_bootstrap_completed_envelope_selection_mismatch")
    else:
        from official_certification import stable_official_opponent_selection

        # The durable envelope is already the exact request-side stable
        # receipt.  Normalize only the separately produced terminal status;
        # accepting extra envelope fields would weaken that original binding.
        if (
            stable_official_opponent_selection(raw_selection)
            != envelope_selection
        ):
            issues.append("official_bootstrap_completed_envelope_selection_mismatch")
    selection = raw_selection if isinstance(raw_selection, dict) else {}

    ckpt = checkpoint if isinstance(checkpoint, dict) else _current_pipeline_checkpoint()
    parked = (
        ((ckpt or {}).get("audit_context") or {}).get("official_bootstrap_request")
        if isinstance((ckpt or {}).get("audit_context"), dict)
        else None
    )
    facts, fact_issues = _current_operator_bootstrap_facts(
        candidate,
        control_id,
        checkpoint=ckpt,
        expected_stage="official_bootstrap_required",
        expected_candidate_hash=str(identity.get("candidate_hash") or ""),
    )
    issues.extend(fact_issues)
    issues.extend(_parked_request_issues(parked, facts))
    if isinstance(parked, dict) and isinstance(facts, dict):
        expected_authorization = _operator_authorization(selection, parked, facts)
        if selection.get("operator_bootstrap_authorization") != expected_authorization:
            issues.append("official_bootstrap_completed_authorization_drift")

    validation = validate_first_strict_control_selection(
        selection,
        control_id,
        candidate,
        allow_consumed=True,
        allow_published=True,
    )
    if validation.get("valid") is not True:
        issues.extend(validation.get("issues") or [validation.get("reason")])

    entries, ledger_issues = _validated_ledger_entries()
    issues.extend(ledger_issues)
    receipt = selection.get("bootstrap_control_receipt") or {}
    digest = str(receipt.get("receipt_digest") or "")
    successful = [
        item
        for item in entries
        if item.get("bootstrap_control_id") == control_id
        and item.get("bootstrap_control_receipt_digest") == digest
        and item.get("outcome") == "official-certified"
        and item.get("policy_id") == FULL_V5_POLICY_ID
        and item.get("mode") == "full"
        and item.get("authoritative") is True
        and item.get("blocking") is False
        and item.get("classification") == "pass"
    ]
    if len(successful) != 1:
        issues.append("official_bootstrap_completed_consumption_count_mismatch")
        ledger_entry = None
    else:
        ledger_entry = successful[0]
        if status.get("official_verdict_ledger_entry") != ledger_entry:
            issues.append("official_bootstrap_completed_status_ledger_entry_mismatch")
        if ledger_entry.get("certificate_digest") != status.get("certificate_digest"):
            issues.append("official_bootstrap_completed_certificate_digest_mismatch")

    issues = _unique(issues)
    return {
        "valid": not issues,
        "reason": "ok" if not issues else issues[0],
        "issues": issues,
        "bootstrap_control_id": control_id,
        "candidate_hash": identity.get("candidate_hash"),
        "certificate_digest": status.get("certificate_digest"),
        "ledger_entry_digest": (
            ledger_entry.get("entry_digest") if isinstance(ledger_entry, dict) else None
        ),
    }


__all__ = [
    "BOOTSTRAP_CONTROL_POLICY_PATH",
    "DEFAULT_BOOTSTRAP_CONTROL_ID",
    "BootstrapControlConfigurationError",
    "authorize_operator_bootstrap_selection",
    "build_operator_bootstrap_parked_request",
    "first_strict_control_consumption",
    "load_first_strict_bootstrap_policy",
    "select_first_strict_control",
    "validate_completed_operator_bootstrap_authorization",
    "validate_first_strict_control_selection",
    "validate_first_strict_control_selection_from_entries",
    "validate_operator_bootstrap_authorized_selection",
]
