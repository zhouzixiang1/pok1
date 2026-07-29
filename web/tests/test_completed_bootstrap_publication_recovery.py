from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from bot_artifact import canonical_digest
from bot_namespace import (
    ARCHIVED_VERSION_HIGH_WATER,
    FIRST_STRICT_POLICY_VERSION,
    bot_name,
    bot_tag,
)
from scripts import recover_completed_first_strict_publication as recovery


# Branch-portable first-strict publication identity.  Production pins the
# completed-publication checkpoint/intent to next_v=FIRST_STRICT_POLICY_VERSION
# / source_v=ARCHIVED_VERSION_HIGH_WATER and the candidate to bot_name of that
# version (national_v143 on main, national_cloud_v1 on cloud).  Express every
# fixture value through these so the same snapshot exercises both floors.
TARGET_V = FIRST_STRICT_POLICY_VERSION
SOURCE_V = ARCHIVED_VERSION_HIGH_WATER
CANDIDATE_LABEL = bot_name(TARGET_V)
WORKFLOW_RUN_ID = f"generation:{TARGET_V}:workflow-v68"


def _snapshot(tmp_path: Path) -> dict:
    candidate = tmp_path / "bots" / CANDIDATE_LABEL
    candidate.mkdir(parents=True)
    parked_payload = {
        "schema_version": 1,
        "kind": "official-first-strict-control-parked-request",
        "candidate_path": str(candidate.resolve()),
        "candidate_label": CANDIDATE_LABEL,
        "candidate_version": TARGET_V,
        "candidate_hash": "a" * 64,
        "source_v": SOURCE_V,
        "workflow_run_id": WORKFLOW_RUN_ID,
        "checkpoint_contract_digest": "b" * 64,
        "evaluation_contract_version": 42,
        "evaluation_contract_hash": "c" * 64,
        "protocol_bootstrap_receipt": {"receipt_digest": "d" * 64},
        "protocol_bootstrap_receipt_digest": "d" * 64,
        "first_strict_control_receipt": {"receipt_digest": "e" * 64},
        "first_strict_control_receipt_digest": "e" * 64,
        "active_bots": [],
        "strict_published_bots": [],
        "bootstrap_control_id": "first_strict_control_v1",
        "bootstrap_policy_digest": "f" * 64,
    }
    parked = {
        **parked_payload,
        "request_digest": canonical_digest(parked_payload),
    }
    ledger_entry = {
        "entry_digest": "1" * 64,
        "certificate_digest": "2" * 64,
    }
    status = {
        "status": "official-certified",
        "mode": "full",
        "policy_id": "official-full-v5",
        "certificate_digest": "2" * 64,
        "certification_identity": {
            "candidate_hash": "a" * 64,
            "spec": {
                "candidate": str(candidate.resolve()),
                "bootstrap_control_id": "first_strict_control_v1",
            },
        },
        "official_verdict_ledger_entry": ledger_entry,
    }
    checkpoint = {
        "next_v": TARGET_V,
        "source_v": SOURCE_V,
        "stage": "verified",
        "workflow_run_id": WORKFLOW_RUN_ID,
        "checkpoint_revision": 24,
        "publication_intent": None,
        "official_job": None,
        "audit_context": {"official_bootstrap_request": parked},
        "gate_results": {
            "official_full": {
                "passed": True,
                "status": deepcopy(status),
                "certificate_digest": "2" * 64,
                "certification_identity": deepcopy(
                    status["certification_identity"]
                ),
                "completed_bootstrap_authorization": {
                    "valid": True,
                    "candidate_hash": "a" * 64,
                    "certificate_digest": "2" * 64,
                    "ledger_entry_digest": "1" * 64,
                },
            }
        },
    }
    return {
        "checkpoint": checkpoint,
        "status": status,
        "candidate": candidate,
        "candidate_hash": "a" * 64,
        "certificate_digest": "2" * 64,
        "ledger_entry_digest": "1" * 64,
        "authorization": {
            "valid": True,
            "candidate_hash": "a" * 64,
            "certificate_digest": "2" * 64,
            "ledger_entry_digest": "1" * 64,
        },
        "gate_ledger": {
            "ok": True,
            "missing_gates": [],
            "failed_gates": [],
            "current_code_fingerprint": "a" * 64,
        },
        "active_bots": [],
        "strict_bots": [],
        "completion_tags": [],
        "completed": False,
        "official_jobs_active": False,
    }


def _issues(snapshot: dict) -> list[str]:
    return recovery.publication_recovery_snapshot_issues(**snapshot)


def _publication_claim() -> dict:
    payload = {
        "schema_version": recovery.CLAIM_SCHEMA_VERSION,
        "kind": recovery.CLAIM_KIND,
        "evaluation_epoch": "national_tcp_policy_v1",
        "checkpoint": {
            "digest": "d" * 64,
            "workflow_run_id": WORKFLOW_RUN_ID,
            "checkpoint_revision": 24,
            "stage": "verified",
            "next_v": TARGET_V,
            "source_v": SOURCE_V,
        },
        "git": {
            "baseline_head": "4" * 40,
            "current_head": "5" * 40,
            "origin_main": "5" * 40,
            "changed_paths": sorted(recovery.EXPECTED_CHANGED_PATHS),
            "self_blob_path": recovery.RECOVERY_SCRIPT,
        },
        "evaluation_contract": {
            "version": 42,
            "baseline_hash": "6" * 64,
            "current_hash": "6" * 64,
            "evaluate_head_drift": {
                "allowed": True,
                "contract_paths": [],
                "external_paths": sorted(recovery.EXPECTED_CHANGED_PATHS),
            },
        },
        "candidate": {},
        "parked_request_digest": "7" * 64,
        "certificate": {
            "candidate_hash": "8" * 64,
            "certificate_digest": "9" * 64,
            "ledger_entry_digest": "a" * 64,
            "official_status_digest": "b" * 64,
            "authorization": {"valid": True},
        },
        "final_gate_ledger_digest": "c" * 64,
        "pool": {"active_bots": [], "strict_published_bots": []},
        "operator_boundary": {
            "runtime_checkout": "/tmp/.evolution_pok",
            "runtime_stopped": True,
            "ordinary_commit_route_authorized": False,
            "recovery_command_only": True,
        },
        "disposition": (
            "publication_only_preserve_signed_certificate_no_recertification"
        ),
    }
    return {**payload, "claim_digest": canonical_digest(payload)}


def test_verified_completed_certificate_snapshot_is_exact(tmp_path):
    assert _issues(_snapshot(tmp_path)) == []


def test_recovery_additions_are_not_verified_evaluation_contract_inputs():
    from evaluation_contract import build_evaluation_contract, is_contract_path

    contract = build_evaluation_contract(
        recovery.ROOT,
        candidate_v=TARGET_V,
        source_v=SOURCE_V,
        checkpoint={"stage": "verified"},
        stage="verified",
    )

    assert recovery.EXPECTED_CHANGED_PATHS == {
        recovery.RECOVERY_SCRIPT,
        recovery.RECOVERY_TEST,
    }
    assert all(
        not is_contract_path(path, contract)
        for path in recovery.EXPECTED_CHANGED_PATHS
    )


def test_index_flags_reject_assume_unchanged_and_skip_worktree():
    assert recovery.index_flag_issues(b"H normal.py\0") == []
    issues = recovery.index_flag_issues(
        b"h assume-unchanged.py\0S skip-worktree.py\0"
    )
    assert len(issues) == 2
    assert "assume-unchanged.py" in issues[0]
    assert "skip-worktree.py" in issues[1]


def test_exact_publication_ref_states_accept_only_reviewed_or_sole_child():
    reviewed = "1" * 40
    publication = "2" * 40
    assert recovery.publication_ref_state_issues(
        reviewed_head=reviewed,
        head=reviewed,
        origin=reviewed,
        publication_commit="",
        publication_parent="",
    ) == []
    assert recovery.publication_ref_state_issues(
        reviewed_head=reviewed,
        head=publication,
        origin=reviewed,
        publication_commit=publication,
        publication_parent=reviewed,
    ) == []
    assert recovery.publication_ref_state_issues(
        reviewed_head=reviewed,
        head=publication,
        origin=publication,
        publication_commit=publication,
        publication_parent=reviewed,
    ) == []
    assert recovery.publication_ref_state_issues(
        reviewed_head=reviewed,
        head="3" * 40,
        origin=reviewed,
        publication_commit=publication,
        publication_parent=reviewed,
    )
    assert recovery.publication_ref_state_issues(
        reviewed_head=reviewed,
        head=publication,
        origin=reviewed,
        publication_commit=publication,
        publication_parent="0" * 40,
    )


def test_stopped_operator_boundary_does_not_require_head_equals_origin(
    tmp_path,
    monkeypatch,
):
    from scripts import reconcile_national_policy_epoch as reconcile

    root = tmp_path / ".evolution_pok"
    root.mkdir()
    monkeypatch.setattr(recovery, "ROOT", root)

    def git(_root, *args, binary=False):
        if args == ("rev-parse", "--show-toplevel"):
            return str(root)
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return "main"
        if args == ("status", "--porcelain", "--untracked-files=no"):
            return ""
        if args == ("ls-files", "-v", "-z") and binary:
            return b"H tracked\0"
        raise AssertionError(f"operator boundary must not compare refs: {args}")

    monkeypatch.setattr(recovery, "_git", git)
    monkeypatch.setattr(reconcile, "_runtime_process_errors", lambda: [])

    assert recovery.operator_runtime_issues() == []


def test_claim_publication_hides_partial_write_until_complete(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / ".evolution_pok"
    (root / "web" / "core" / "results").mkdir(parents=True)
    claim = _publication_claim()
    target = recovery._claim_path(root, claim["claim_digest"])
    original_write = recovery.os.write
    calls = []

    def interrupted_write(descriptor, payload):
        calls.append(True)
        original_write(descriptor, payload[: min(13, len(payload))])
        raise OSError("simulated kill before claim publication")

    monkeypatch.setattr(recovery.os, "write", interrupted_write)
    with pytest.raises(OSError, match="simulated kill"):
        recovery.publish_claim(root, claim)
    assert not target.exists()
    assert not list(target.parent.glob("*.tmp"))

    monkeypatch.setattr(recovery.os, "write", original_write)
    assert recovery.publish_claim(root, claim) == target
    assert recovery.load_claim(root, claim["claim_digest"]) == claim


def test_claim_publication_recovers_crash_after_atomic_rename(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / ".evolution_pok"
    (root / "web" / "core" / "results").mkdir(parents=True)
    claim = _publication_claim()
    target = recovery._claim_path(root, claim["claim_digest"])
    real_rename = recovery._rename_noreplace
    calls = []

    def renamed_then_killed(directory_fd, source_name, destination_name):
        assert isinstance(directory_fd, int)
        assert source_name.endswith(".tmp")
        assert destination_name == target.name
        real_rename(directory_fd, source_name, destination_name)
        calls.append(True)
        raise RuntimeError("simulated kill after atomic rename")

    monkeypatch.setattr(recovery, "_rename_noreplace", renamed_then_killed)
    with pytest.raises(RuntimeError, match="simulated kill"):
        recovery.publish_claim(root, claim)
    assert calls == [True]
    assert target.is_file()
    assert recovery.load_claim(root, claim["claim_digest"]) == claim

    monkeypatch.setattr(recovery, "_rename_noreplace", real_rename)
    assert recovery.publish_claim(root, claim) == target


def test_publishing_live_recheck_accepts_portable_status_and_own_sentinel(
    tmp_path,
    monkeypatch,
):
    import bot_artifact
    import evolution_infra
    import national_runtime_authority
    import official_certification
    import official_certification_job
    import publication_transaction
    import tool_commit

    root = tmp_path / ".evolution_pok"
    candidate = root / "bots" / CANDIDATE_LABEL
    candidate.mkdir(parents=True)
    frozen_status = {"shape": "raw", "certificate_digest": "9" * 64}
    portable_status = {"shape": "portable", "certificate_digest": "9" * 64}
    claim = _publication_claim()
    claim["certificate"]["official_status_digest"] = canonical_digest(
        frozen_status
    )
    checkpoint = {
        "gate_results": {"official_full": {"status": frozen_status}},
        "publication_intent": {
            "remote_publication_required": True,
            "remote_publication_enabled": True,
        },
    }
    monkeypatch.setattr(bot_artifact, "hash_path", lambda _path: "8" * 64)
    monkeypatch.setattr(
        evolution_infra,
        "get_active_bots_read_only",
        lambda: (CANDIDATE_LABEL,),
    )
    monkeypatch.setattr(
        national_runtime_authority,
        "build_pending_local_publication_proof",
        lambda _candidate: {"proof": "pending"},
    )
    monkeypatch.setattr(
        national_runtime_authority,
        "strict_published_bot_names",
        lambda: (CANDIDATE_LABEL,),
    )
    monkeypatch.setattr(
        official_certification,
        "read_status",
        lambda _candidate: portable_status,
    )
    monkeypatch.setattr(
        official_certification,
        "official_full_certified",
        lambda *_a, **kwargs: kwargs.get("require_published") is True,
    )
    monkeypatch.setattr(
        official_certification_job,
        "job_snapshot",
        lambda: {"pending": [], "running": []},
    )
    monkeypatch.setattr(
        publication_transaction,
        "publication_gate_ledger_digest",
        lambda _gate: "c" * 64,
    )
    monkeypatch.setattr(
        tool_commit,
        "validate_commit_gate_ledger",
        lambda *_a, **_k: {
            "missing_gates": [],
            "failed_gates": [],
        },
    )

    class Probe:
        returncode = 0

    monkeypatch.setattr(recovery.subprocess, "run", lambda *_a, **_k: Probe())

    assert recovery.publishing_live_claim_issues(
        root, checkpoint, claim
    ) == []


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (
            lambda value: value["checkpoint"].update(stage="reviewed"),
            "first_strict_publication_recovery_checkpoint_stage_mismatch",
        ),
        (
            lambda value: value["checkpoint"]["audit_context"].pop(
                "official_bootstrap_request"
            ),
            "first_strict_publication_recovery_parked_request_missing",
        ),
        (
            lambda value: value["checkpoint"]["gate_results"][
                "official_full"
            ].update(passed=False),
            "first_strict_publication_recovery_official_gate_not_passed",
        ),
        (
            lambda value: value["checkpoint"]["gate_results"][
                "official_full"
            ].update(certificate_digest="9" * 64),
            "first_strict_publication_recovery_gate_certificate_mismatch",
        ),
        (
            lambda value: value["checkpoint"]["gate_results"][
                "official_full"
            ].update(status={}),
            "first_strict_publication_recovery_gate_status_mismatch",
        ),
        (
            lambda value: value.update(active_bots=[bot_name(FIRST_STRICT_POLICY_VERSION)]),
            "first_strict_publication_recovery_active_pool_not_empty",
        ),
        (
            lambda value: value.update(strict_bots=[bot_name(FIRST_STRICT_POLICY_VERSION)]),
            "first_strict_publication_recovery_strict_pool_not_empty",
        ),
        (
            lambda value: value.update(
                completion_tags=[bot_tag(FIRST_STRICT_POLICY_VERSION)]
            ),
            "first_strict_publication_recovery_completion_tag_present",
        ),
    ],
)
def test_verified_recovery_snapshot_fails_closed(
    tmp_path,
    mutation,
    expected,
):
    snapshot = _snapshot(tmp_path)
    mutation(snapshot)
    assert expected in _issues(snapshot)


def test_publishing_recovery_requires_matching_receipt_and_intent(tmp_path):
    claim = _publication_claim()
    intent = {
        "version": TARGET_V,
        "source_v": SOURCE_V,
        "workflow_run_id": WORKFLOW_RUN_ID,
        "publication_id": "3" * 64,
        "strategy_tag": recovery.receipt_commit_line(claim["claim_digest"]),
        "candidate_artifact_hash": "8" * 64,
        "official_certificate_digest": "9" * 64,
        "official_status_digest": "b" * 64,
        "final_gate_ledger_digest": "c" * 64,
        "baseline_head": "5" * 40,
        "baseline_remote_main": "5" * 40,
        "baseline_remote_completion_refs": {},
        "prepublication_strict_bots": [],
        "origin_checkpoint_revision": 24,
        "origin_checkpoint_stage": "verified",
        "remote_publication_required": True,
        "remote_publication_enabled": True,
    }
    receipt = recovery._terminal_receipt(claim, intent)
    checkpoint = {
        "next_v": TARGET_V,
        "source_v": SOURCE_V,
        "stage": "publishing",
        "workflow_run_id": WORKFLOW_RUN_ID,
        "audit_context": {
            recovery.CHECKPOINT_RECEIPT_KEY: receipt,
        },
        "publication_intent": intent,
    }

    assert recovery.publishing_recovery_issues(
        checkpoint, claim["claim_digest"], claim=claim
    ) == []

    missing = deepcopy(checkpoint)
    missing["publication_intent"] = None
    assert (
        "first_strict_publication_recovery_intent_missing"
        in recovery.publishing_recovery_issues(
            missing, claim["claim_digest"]
        )
    )

    mismatched = deepcopy(checkpoint)
    mismatched["publication_intent"]["strategy_tag"] = "other"
    assert (
        "first_strict_publication_recovery_intent_receipt_mismatch"
        in recovery.publishing_recovery_issues(
            mismatched, claim["claim_digest"]
        )
    )


def test_execute_claim_recovers_attestation_before_cas_crash(
    tmp_path,
    monkeypatch,
):
    import evolution_infra
    import bootstrap_contract_recovery
    import national_runtime_authority
    import official_certification
    import official_certification_job
    import publication_transaction
    import tool_commit

    root = tmp_path / ".evolution_pok"
    (root / "web" / "core" / "results").mkdir(parents=True)
    candidate = root / "bots" / CANDIDATE_LABEL
    candidate.mkdir(parents=True)
    checkpoint = {
        "next_v": TARGET_V,
        "source_v": SOURCE_V,
        "stage": "verified",
        "workflow_run_id": WORKFLOW_RUN_ID,
        "checkpoint_revision": 24,
        "master_plan": {"strategy": "test-strategy"},
    }
    status = {
        "policy_id": "official-full-v5",
        "certificate_digest": "9" * 64,
        "certification_identity": {"candidate_hash": "8" * 64},
    }
    claim = _publication_claim()
    claim_payload = {
        key: deepcopy(value)
        for key, value in claim.items()
        if key != "claim_digest"
    }
    claim_payload["checkpoint"]["digest"] = canonical_digest(checkpoint)
    claim_payload["certificate"]["official_status_digest"] = canonical_digest(
        status
    )
    claim = {
        **claim_payload,
        "claim_digest": canonical_digest(claim_payload),
    }
    publishing = {}
    cas_attempts = []
    delegated = []
    monkeypatch.setenv("POK_EVOLUTION_RUNTIME", "0")
    monkeypatch.setenv("POK_REQUIRE_EVOLUTION_PUSH", "0")
    monkeypatch.setenv("EVOLUTION_GIT_PUSH", "0")

    monkeypatch.setattr(
        official_certification,
        "read_status",
        lambda _candidate: deepcopy(status),
    )
    monkeypatch.setattr(
        official_certification,
        "official_full_certified",
        lambda *_a, **_k: True,
    )
    monkeypatch.setattr(
        recovery,
        "validate_completed_at_parked_authority",
        lambda *_a, **_k: {"valid": True},
    )
    monkeypatch.setattr(
        recovery,
        "publication_recovery_snapshot_issues",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(recovery, "operator_runtime_issues", lambda: [])
    monkeypatch.setattr(
        recovery,
        "publishing_exact_git_issues",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        recovery,
        "publishing_live_claim_issues",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        bootstrap_contract_recovery,
        "_safe_candidate",
        lambda *_a, **_k: deepcopy(claim["candidate"]),
    )
    monkeypatch.setattr(
        official_certification_job,
        "job_snapshot",
        lambda: {"pending": [], "running": []},
    )

    def attest(_status, _candidate):
        path = root / "official_certificates" / f"{CANDIDATE_LABEL}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("attestation\n", encoding="utf-8")
        return {
            "relative_path": f"official_certificates/{CANDIDATE_LABEL}.json",
            "attestation_digest": "e" * 64,
        }

    monkeypatch.setattr(
        official_certification,
        "publish_certificate_attestation",
        attest,
    )
    monkeypatch.setattr(
        tool_commit,
        "validate_commit_gate_ledger",
        lambda *_a, **_k: {"ok": True},
    )
    monkeypatch.setattr(
        tool_commit,
        "_official_certificate_projection",
        lambda _status: {
            "candidate_hash": "8" * 64,
            "certificate_digest": "9" * 64,
            "policy_id": "official-full-v5",
        },
    )
    monkeypatch.setattr(
        national_runtime_authority,
        "strict_published_bot_names",
        lambda: (),
    )
    monkeypatch.setattr(
        evolution_infra,
        "get_active_bots_read_only",
        lambda: (),
    )
    monkeypatch.setattr(
        evolution_infra,
        "evolution_git_push_enabled",
        lambda: True,
    )
    monkeypatch.setattr(
        evolution_infra,
        "evolution_git_push_required",
        lambda: True,
    )
    monkeypatch.setattr(
        evolution_infra,
        "remote_completion_ref_snapshot",
        lambda: {},
    )
    monkeypatch.setattr(
        evolution_infra,
        "_git",
        lambda *_a, **_k: "5" * 40,
    )
    monkeypatch.setattr(
        publication_transaction,
        "publication_gate_ledger_digest",
        lambda _ledger: "c" * 64,
    )

    def intent_builder(**kwargs):
        import os

        assert os.environ["POK_EVOLUTION_RUNTIME"] == "1"
        assert os.environ["POK_REQUIRE_EVOLUTION_PUSH"] == "1"
        assert os.environ["EVOLUTION_GIT_PUSH"] == "1"
        assert f"publication-recovery:{claim['claim_digest']}" in kwargs[
            "strategy_tag"
        ]
        return {
            "publication_id": "3" * 64,
            "version": TARGET_V,
            "source_v": SOURCE_V,
            "workflow_run_id": WORKFLOW_RUN_ID,
            "strategy_tag": kwargs["strategy_tag"],
            "candidate_artifact_hash": "8" * 64,
            "official_certificate_digest": "9" * 64,
            "official_status_digest": claim["certificate"][
                "official_status_digest"
            ],
            "final_gate_ledger_digest": "c" * 64,
            "baseline_head": "5" * 40,
            "baseline_remote_main": "5" * 40,
            "baseline_remote_completion_refs": {},
            "prepublication_strict_bots": [],
            "origin_checkpoint_revision": 24,
            "origin_checkpoint_stage": "verified",
            "remote_publication_required": True,
            "remote_publication_enabled": True,
        }

    monkeypatch.setattr(
        publication_transaction,
        "build_publication_intent",
        intent_builder,
    )

    def write_checkpoint(*_args, **kwargs):
        cas_attempts.append(kwargs)
        if len(cas_attempts) == 1:
            return False
        publishing.update({
            **checkpoint,
            "stage": "publishing",
            "publication_intent": kwargs["publication_intent"],
            "audit_context": kwargs["audit_context"],
        })
        return True

    monkeypatch.setattr(
        evolution_infra,
        "write_pipeline_checkpoint",
        write_checkpoint,
    )
    monkeypatch.setattr(
        evolution_infra,
        "read_pipeline_checkpoint",
        lambda: deepcopy(publishing or checkpoint),
    )
    monkeypatch.setattr(
        recovery,
        "_git",
        lambda *_a, **_k: b"H tracked\0",
    )

    async def delegate(strategy):
        delegated.append(strategy)
        return {"committed": True, "publication_id": "3" * 64}

    monkeypatch.setattr(recovery, "_delegate_commit_bot", delegate)

    with pytest.raises(
        recovery.CompletedFirstStrictPublicationRecoveryError,
        match="publishing_cas_failed",
    ):
        recovery.execute_claim(root, checkpoint, claim)

    result = recovery.execute_claim(root, checkpoint, claim)

    assert result["committed"] is True
    assert len(cas_attempts) == 2
    assert delegated == [cas_attempts[1]["publication_intent"]["strategy_tag"]]
    receipt = cas_attempts[1]["audit_context"][recovery.CHECKPOINT_RECEIPT_KEY]
    assert receipt["claim_digest"] == claim["claim_digest"]
    assert receipt["publication_id"] == "3" * 64
    assert recovery.load_claim(root, claim["claim_digest"]) == claim


def test_double_validation_uses_real_verified_checkpoint_persistence(
    tmp_path,
    monkeypatch,
):
    """Regression for parked validation -> persisted pass -> verified rebind."""

    import checkpoint_schema
    import evolution_infra
    import official_bootstrap
    import tool_commit

    state_path = tmp_path / "pipeline_state.json"
    parked = _snapshot(tmp_path)["checkpoint"]
    parked["stage"] = "official_bootstrap_required"
    parked["checkpoint_revision"] = 23
    parked.update({
        "checkpoint_schema_version": 2,
        "evaluation_epoch": "national_tcp_policy_v1",
        "epoch_binding": {},
        "repo_baseline": {},
        "workflow_profile_id": "national_tcp_policy_v1",
        "national_execution_mode": "native_tcp",
    })
    state_path.write_text(json.dumps(parked), encoding="utf-8")
    monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", state_path)
    monkeypatch.setattr(
        evolution_infra,
        "checkpoint_allocation_authority",
        lambda **_kwargs: {
            "published_high_water": SOURCE_V,
            "abandoned_receipt_floor": 0,
            "abandoned_receipt_head_digest": None,
            "allocation_floor": SOURCE_V,
        },
    )
    monkeypatch.setattr(
        evolution_infra,
        "_capture_repo_baseline",
        lambda stage, **_kwargs: {
            "head": "baseline",
            "evaluation_contract": {"stage": stage, "hash": "c" * 64},
        },
    )
    monkeypatch.setattr(checkpoint_schema, "checkpoint_epoch_errors", lambda *_a, **_k: [])
    monkeypatch.setattr(
        checkpoint_schema,
        "live_checkpoint_allocation_authority_errors",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        checkpoint_schema,
        "live_checkpoint_parent_authority_errors",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        tool_commit,
        "write_pipeline_checkpoint",
        evolution_infra.write_pipeline_checkpoint,
    )

    facts = {}

    def stage_bound_facts(_candidate, _control, *, checkpoint, expected_stage, **_kwargs):
        if checkpoint.get("stage") != expected_stage:
            return facts, [
                "official_bootstrap_checkpoint_stage_mismatch:"
                f"expected={expected_stage}:actual={checkpoint.get('stage')}"
            ]
        return facts, []

    monkeypatch.setattr(
        official_bootstrap,
        "_current_operator_bootstrap_facts",
        stage_bound_facts,
    )
    monkeypatch.setattr(official_bootstrap, "_parked_request_issues", lambda *_a: [])
    monkeypatch.setattr(
        official_bootstrap,
        "_operator_authorization",
        lambda *_a: {},
    )
    monkeypatch.setattr(
        official_bootstrap,
        "validate_first_strict_control_selection",
        lambda *_a, **_k: {"valid": True, "issues": []},
    )
    monkeypatch.setattr(
        official_bootstrap,
        "_validated_ledger_entries",
        lambda: ([], []),
    )

    assert tool_commit._record_official_full_pass_checkpoint(
        TARGET_V,
        SOURCE_V,
        parked,
        {
            "passed": True,
            "bootstrap_certificate": True,
            "status": {"status": "official-certified"},
        },
    ) is True
    verified = evolution_infra.read_pipeline_checkpoint()
    assert verified["stage"] == "verified"
    assert verified["checkpoint_revision"] == 24

    calls = []

    def completed_validator(_status, _candidate, *, checkpoint):
        calls.append(checkpoint["stage"])
        return {
            "valid": checkpoint["stage"] == "official_bootstrap_required",
            "issues": [] if checkpoint["stage"] == "official_bootstrap_required" else ["stage"],
        }

    monkeypatch.setattr(
        official_bootstrap,
        "validate_completed_operator_bootstrap_authorization",
        completed_validator,
    )
    result = recovery.validate_completed_at_parked_authority(
        {}, tmp_path / "bots" / CANDIDATE_LABEL, verified
    )

    assert result["valid"] is True
    assert calls == ["official_bootstrap_required"]
    assert evolution_infra.read_pipeline_checkpoint()["stage"] == "verified"
