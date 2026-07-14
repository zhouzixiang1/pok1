import hashlib
import json

import checkpoint_schema
from pipeline_state import (
    STAGE_GATE_ALLOWLIST,
    route_policy as _route_policy,
    validate_stage_transition,
)
from system_strict_bootstrap import build_fresh_bootstrap_receipt


def _digest(payload):
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _parent_identity(version):
    return {
        "version": version,
        "bot": f"national_v{version}",
        "role": "parent_source",
        "epoch": "national_tcp_policy_v1",
        "runtime_manifest_digest": "1" * 64,
        "epoch_receipt_digest": "2" * 64,
        "publication_identity_digest": "3" * 64,
        "certificate_digest": "4" * 64,
    }


def _strict(checkpoint):
    target = checkpoint["next_v"]
    source = checkpoint["source_v"]
    payload = {
        "schema_version": 1,
        "epoch": "national_tcp_policy_v1",
        "mode": "published_strict_parent",
        "next_v": target,
        "source_v": source,
        "parent2_v": None,
        "parent_versions": [source],
        "source_artifact_inherited": True,
        "parent_authority": "strict_published_parent_resolution",
        "published_parent_identities": [_parent_identity(source)],
        "protocol_bootstrap_receipt_digest": None,
        "policy_epoch_reset_receipt_digest": None,
    }
    return {
        **checkpoint,
        "checkpoint_schema_version": 1,
        "evaluation_epoch": "national_tcp_policy_v1",
        "epoch_binding": {**payload, "binding_digest": _digest(payload)},
    }


def _reset_receipt():
    archive_root = (
        "archive/evolution_epochs/national_native_v1/"
        "runtime_legacy_untrusted/20260714_000000_000000"
    )
    claim_payload = {
        "schema_version": 1,
        "kind": "national_tcp_policy_epoch_reset_claim",
        "epoch": "national_tcp_policy_v1",
        "created_at": "2026-07-14T00:00:00.000000",
        "git_head": "a" * 40,
        "archive_root": archive_root,
        "first_target_version": 143,
        "checkout_role": "autonomous_evolution_runtime",
        "one_time": True,
    }
    claim_digest = _digest(claim_payload)
    payload = {
        "schema_version": 2,
        "kind": "national_tcp_policy_epoch_reset",
        "epoch": "national_tcp_policy_v1",
        "created_at": "2026-07-14T00:00:00",
        "mode": "execute",
        "git_head": "a" * 40,
        "archive_root": archive_root,
        "execution_scope": {
            "checkout_role": "autonomous_evolution_runtime",
            "one_time": True,
            "prior_reset_evidence_required_empty": True,
            "claim_digest": claim_digest,
        },
        "archived_version_high_water": 142,
        "version_authority_high_water": 142,
        "first_target_version": 143,
        "source_code_inherited": False,
        "seed_bot": None,
        "active_namespace": {
            "bot": "national_v143",
            "protocol": "official-national-raw-tcp-v1",
            "policy_abi": "national-tcp-policy-runtime-v1",
        },
        "archived_runtime": [],
        "archived_bot_debris": [],
    }
    return {
        **payload,
        "receipt_digest": _digest(payload),
        "_test_claim": {**claim_payload, "claim_digest": claim_digest},
    }


def _strip_test_claim(receipt):
    value = dict(receipt)
    value.pop("_test_claim", None)
    return value


def _write_reset_authority(root, receipt):
    value = _strip_test_claim(receipt)
    claim = receipt["_test_claim"]
    live = root / checkpoint_schema.POLICY_EPOCH_RESET_RECEIPT_RELATIVE_PATH
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_text(json.dumps(value) + "\n", encoding="utf-8")
    archive = root / value["archive_root"]
    archive.mkdir(parents=True)
    (archive / "reset_claim.json").write_text(
        json.dumps(claim) + "\n", encoding="utf-8"
    )
    (archive / "reset_receipt.json").write_text(
        json.dumps(value) + "\n", encoding="utf-8"
    )
    return value


def _fresh(checkpoint, reset_receipt):
    bootstrap = build_fresh_bootstrap_receipt(
        active_bots=(),
        epoch_reset_receipt_digest=reset_receipt["receipt_digest"],
    )
    audit_context = {
        "protocol_bootstrap": bootstrap,
        "selection": {"strategy": "fresh_policy_bootstrap"},
    }
    binding = checkpoint_schema.build_checkpoint_epoch_binding(
        next_v=143,
        source_v=142,
        audit_context=audit_context,
    )
    return {
        **checkpoint,
        "checkpoint_schema_version": 1,
        "evaluation_epoch": "national_tcp_policy_v1",
        "epoch_binding": binding,
        "audit_context": audit_context,
    }


def route_policy(checkpoint):
    return _route_policy(checkpoint)


def test_verified_stage_retains_content_bound_official_gate():
    assert "official_full" in STAGE_GATE_ALLOWLIST["verified"]


def test_official_failed_routes_to_worker_repair():
    route = route_policy(_strict({
        "stage": "official_failed",
        "next_v": 144,
        "source_v": 143,
        "gate_results": {"official_full": {"passed": False}},
    }))

    assert route["next_tool"] == "execute_workers"
    assert route["intent"] == "official_rework"
    assert "Official EXE full certification" in route["directive"]
    assert validate_stage_transition("verified", "official_failed")[0] is True
    assert validate_stage_transition("official_failed", "repair_planned")[0] is True


def test_official_inconclusive_has_no_automatic_commit_retry():
    route = route_policy(_strict({
        "stage": "official_inconclusive",
        "next_v": 144,
        "source_v": 143,
        "gate_results": {"official_full": {"passed": False}},
    }))

    assert route["next_tool"] is None
    assert route["allowed_tools"] == []
    assert "Do not call commit_bot" in route["directive"]


def test_official_bootstrap_required_is_parked_for_manual_validated_commit():
    route = route_policy(_fresh({
        "stage": "official_bootstrap_required",
        "next_v": 143,
        "source_v": 142,
        "parent2_v": None,
        "gate_results": {"official_full": {"passed": False}},
    }, _strip_test_claim(_reset_receipt())))

    assert route["next_tool"] is None
    assert route["allowed_tools"] == ["commit_bot"]
    assert route["intent"] == "operator_bootstrap"
    assert "orchestrator must stop" in route["directive"]
    assert "never authorize or consume" in route["directive"]
    assert validate_stage_transition("verified", "official_bootstrap_required")[0] is True
    assert validate_stage_transition(
        "official_bootstrap_required", "verified"
    ) == (True, "official_bootstrap_certificate_validated")
    assert validate_stage_transition(
        "official_bootstrap_required", "timed_out"
    ) == (False, "operator_bootstrap_pause_is_durable")
    assert validate_stage_transition(
        "official_bootstrap_required", "infra_timed_out"
    ) == (False, "operator_bootstrap_pause_is_durable")
    assert validate_stage_transition(
        "official_bootstrap_required", "official_certifying"
    ) == (False, "operator_bootstrap_pause_is_durable")
    assert validate_stage_transition(
        "official_bootstrap_required", "workers_done"
    ) == (False, "operator_bootstrap_pause_is_durable")


def test_official_bootstrap_required_uses_commit_evaluation_contract():
    from evaluation_contract import COMMIT_STAGE_EXACT, _STAGE_EXACT

    assert _STAGE_EXACT["official_bootstrap_required"] == COMMIT_STAGE_EXACT


def test_official_inconclusive_recovery_is_blocked(tmp_path):
    from pipeline_recovery import checkpoint_recovery_diagnostics

    root = tmp_path
    (root / "bots" / "national_v144").mkdir(parents=True)
    checkpoint = _strict({
        "stage": "official_inconclusive",
        "next_v": 144,
        "source_v": 143,
        "repo_baseline": {"branch": "main", "head": "abc123"},
    })
    snapshot = {"ok": True, "branch": "main", "head": "abc123", "entries": []}

    diag = checkpoint_recovery_diagnostics(checkpoint, snapshot=snapshot, project_root=root)

    assert diag["active"] is True
    assert diag["recoverable"] is False
    assert "official_inconclusive_requires_infra_intervention" in diag["issues"]


def test_official_bootstrap_required_recovery_waits_for_operator(tmp_path):
    from pipeline_recovery import checkpoint_recovery_diagnostics

    root = tmp_path
    (root / "bots" / "national_v143").mkdir(parents=True)
    reset_receipt = _write_reset_authority(root, _reset_receipt())
    checkpoint = _fresh({
        "stage": "official_bootstrap_required",
        "next_v": 143,
        "source_v": 142,
        "parent2_v": None,
        "repo_baseline": {"branch": "main", "head": "abc123"},
    }, reset_receipt)
    snapshot = {"ok": True, "branch": "main", "head": "abc123", "entries": []}

    diag = checkpoint_recovery_diagnostics(
        checkpoint,
        snapshot=snapshot,
        project_root=root,
    )

    assert diag["active"] is True
    assert diag["recoverable"] is False
    assert "official_bootstrap_requires_operator_action" in diag["issues"]
    assert "automatic_first_strict_control_consumption_forbidden" in diag["warnings"]
