from __future__ import annotations

import json
from pathlib import Path

from bots.research_native_lab.common_contracts.resource_enforcer import (
    FORMAL_GLOBAL_LOCK_PATH,
    RESOURCE_ENFORCER_DIGEST,
    TRUSTED_SUPERVISOR_ATTESTATION_PATH,
    TRUSTED_SUPERVISOR_ATTEMPT_JOURNAL_ROOT,
    TRUSTED_SUPERVISOR_CONTRACT_PATH,
    TRUSTED_SUPERVISOR_LEDGER_ROOT,
    TRUSTED_SUPERVISOR_PROTOCOL,
    current_enforcer_digest,
    probe_resource_enforcer,
    probe_trusted_supervisor,
    required_controllers_digest,
)


CONTRACT_PATH = Path(
    "bots/research_native_lab/common_contracts/contracts/resource_v1.json"
)


def _contract() -> dict[str, object]:
    payload = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_checked_in_resource_contract_binds_current_enforcer_and_controllers() -> None:
    payload = _contract()
    integrity = payload["integrity"]
    assert isinstance(integrity, dict)
    assert integrity == {
        "controllers_digest": required_controllers_digest(),
        "enforcer_digest": RESOURCE_ENFORCER_DIGEST,
        "same_uid_backend_authority": "development_diagnostic_only",
        "schema": "pok-resource-contract-v4",
    }
    assert current_enforcer_digest() == RESOURCE_ENFORCER_DIGEST


def test_formal_authority_is_fixed_external_and_currently_unavailable() -> None:
    payload = _contract()
    authority = payload["formal_authority_contract"]
    assert isinstance(authority, dict)
    assert authority["contract_path"] == str(TRUSTED_SUPERVISOR_CONTRACT_PATH)
    assert authority["supervisor_attestation_path"] == str(
        TRUSTED_SUPERVISOR_ATTESTATION_PATH
    )
    assert authority["fixed_global_lock_path"] == str(FORMAL_GLOBAL_LOCK_PATH)
    assert authority["consumption_ledger_root"] == str(TRUSTED_SUPERVISOR_LEDGER_ROOT)
    assert authority["attempt_journal_root"] == str(
        TRUSTED_SUPERVISOR_ATTEMPT_JOURNAL_ROOT
    )
    assert authority["protocol"] == TRUSTED_SUPERVISOR_PROTOCOL
    assert authority["formal_available"] is False
    assert authority["formal_result_authority"] == "unavailable"
    assert authority["local_same_uid_launcher_can_mint_formal_receipts"] is False
    assert authority["service_uid"] == 0
    assert len(authority["missing_components"]) >= 10

    live = probe_trusted_supervisor()
    assert live.formal_available is False
    assert live.contract_path == str(TRUSTED_SUPERVISOR_CONTRACT_PATH)
    assert live.reasons


def test_same_uid_probe_can_never_upgrade_itself_to_formal() -> None:
    payload = _contract()
    checked_in = payload["diagnostic_same_uid_cgroup_probe"]
    assert isinstance(checked_in, dict)
    assert checked_in["formal_available"] is False
    assert "diagnostic-only" in " ".join(checked_in["reasons"])

    live = probe_resource_enforcer(checked_in["delegated_root"])
    assert live.formal_available is False
    assert any("same-uid" in reason for reason in live.reasons)


def test_contract_requires_artifact_raw_decision_and_cleanup_proofs() -> None:
    payload = _contract()
    artifact = payload["artifact_execution_contract"]
    raw = payload["raw_evidence_contract"]
    decision = payload["decision_enforcement_contract"]
    cleanup = payload["cleanup_contract"]
    durability = payload["restart_durability_contract"]
    assert artifact["formal_materialization_authority"] == (
        "external_signed_privileged_supervisor_only"
    )
    assert artifact["local_python_materialization_is_formal"] is False
    assert artifact["actual_executable_hash_required"] is True
    assert artifact["readonly_cas_mount_attestation_required"] is True
    assert raw["typed_cross_link_required"] is True
    assert raw["signed_leg_receipt_binds_both_ordered_digests"] is True
    assert decision["candidate_fault_owner"] == "acting_connection_only"
    assert decision["infrastructure_fault_charged_to_candidate"] is False
    assert decision["no_opponent_time_pondering"] is True
    assert decision["decision_event_schema"] == (
        "pok-supervisor-decision-enforcement-event-v3"
    )
    assert decision["decision_identity_schema"] == (
        "pok-supervisor-decision-identity-v3"
    )
    assert decision["signed_leg_receipt_schema"] == (
        "pok-trusted-resource-supervisor-leg-receipt-v3"
    )
    assert "explicit_null" in decision["timeout_action_evidence"]
    assert "exact_peer_fold_close" in decision["tokenless_fault_action_evidence"]
    assert "client_token_ingress_only" in decision[
        "action_sent_monotonic_ns_semantics"
    ]
    assert decision["decision_close_boundary"].startswith("mandatory_server_to_peer")
    assert cleanup["durable_exclusive_receipt_required"] is True
    assert cleanup["receipt_directory_fsync_required"] is True
    assert cleanup["fail_closed_on_any_cleanup_error"] is True
    assert durability["in_process_sets_are_formal_authority"] is False
    assert durability["matrix_must_consume_authorized_attempt_journal_capability"] is True
    assert durability["receipt_key_replay_must_be_rejected_by_downstream_durable_ledger"] is True
