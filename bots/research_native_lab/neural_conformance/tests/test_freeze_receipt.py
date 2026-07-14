"""Tests for the append-only freeze receipt and Git-pinned label authorization."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from bots.research_native_lab.common_contracts.actions import Action, ActionKind
from bots.research_native_lab.common_contracts.national_state import NationalGameState
from bots.research_native_lab.neural_conformance.freeze_receipt import (
    GIT_RECEIPT_PIN_SCHEMA,
    PRELABEL_FREEZE_RECEIPT_SCHEMA,
    GitReceiptPin,
    PreLabelFreezeReceipt,
    freeze_prelabel_receipt,
    read_freeze_receipt,
    verify_git_pinned_receipt,
    verify_label_authorization,
)
from bots.research_native_lab.neural_conformance.prelabel import ARTIFACT_KINDS
from bots.research_native_lab.neural_conformance.public_family import (
    public_family_id,
    public_family_payload,
)
from bots.research_native_lab.neural_conformance.split import (
    SplitAuthority,
    build_leakage_closed_split,
    freeze_split_authority,
)


SALT_A = "route-a1-freeze-test-salt"


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _registry() -> dict[str, dict[str, object]]:
    states = [
        NationalGameState.new_hand(1, small_blind=0),
        NationalGameState.new_hand(1, small_blind=0).apply_action(
            Action(ActionKind.CALL)
        ),
    ]
    states.extend(
        NationalGameState.new_hand(1, small_blind=0).apply_action(
            Action(ActionKind.RAISE, amount)
        )
        for amount in range(200, 1000, 100)
    )
    return {
        public_family_id(state): public_family_payload(state) for state in states
    }


def _artifact_bytes(route: str, token: str, kind: str, name: str, family_id: str | None = None) -> bytes:
    def d(field: str) -> str:
        return hashlib.sha256(
            f"{route}|{token}|{kind}|{name}|{field}".encode("ascii")
        ).hexdigest()

    facts = {
        "route-contract": {"contract_sha256": d("contract"), "contract_byte_count": 42},
        "generator-code": {"source_closure_sha256": d("source"), "source_file_count": 2},
        "generator-config": {"config_sha256": d("config")},
        "seed-root": {"seed_root_sha256": d("seed"), "seed_count": 50},
        "source-checkpoint": {"checkpoint_sha256": d("ckpt"), "checkpoint_byte_count": 99},
        "pbs-input": {
            "public_family_id": family_id,
            "public_input_sha256": d("pub"),
            "range_payload_sha256": d("range"),
            "legal_mask_sha256": d("mask"),
            "semantics_sha256": d("sem"),
        },
        "trajectory": {"trajectory_sha256": d("traj"), "deal_key_sha256": d("deal"), "decision_count": 3},
        "rollout": {"rollout_plan_sha256": d("plan"), "seed_block_sha256": d("block"), "rollout_count": 10},
        "source-copy": {"origin_sha256": d("origin"), "copy_ordinal": 0},
    }[kind]
    return json.dumps(
        {"kind": kind, "payload": facts, "schema": "neural-prelabel-artifact-content-v1"},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _make_journal(
    artifact_root: Path,
    registry: dict[str, dict[str, object]],
    *,
    route: str = "route-a1:m5b",
    token: str = "a",
):
    from bots.research_native_lab.neural_conformance.prelabel import (
        PreLabelArtifact,
        PreLabelGeneratorJournal,
        make_generator_event,
    )

    artifacts: list[PreLabelArtifact] = []

    def add(kind: str, name: str, *, family_id: str | None = None) -> PreLabelArtifact:
        relative = f"{token}/{kind}/{name}.bin"
        path = artifact_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_artifact_bytes(route, token, kind, name, family_id))
        artifact = PreLabelArtifact.from_file(
            artifact_root, route_domain=route, kind=kind, relative_path=relative
        )
        artifacts.append(artifact)
        return artifact

    common = {
        "route_contract_artifact_id": add("route-contract", "contract").instance_id,
        "generator_code_artifact_id": add("generator-code", "code").instance_id,
        "generator_config_artifact_id": add("generator-config", "config").instance_id,
        "seed_root_artifact_id": add("seed-root", "seed").instance_id,
        "source_checkpoint_artifact_id": add("source-checkpoint", "ckpt").instance_id,
    }
    families = sorted(registry)
    events = []
    for index, family_id in enumerate(families):
        trajectory = add("trajectory", f"traj-{index}")
        references = {
            **common,
            "pbs_artifact_id": add("pbs-input", f"pbs-{index}", family_id=family_id).instance_id,
            "trajectory_artifact_id": trajectory.instance_id,
            "rollout_artifact_id": add("rollout", f"rollout-{index}").instance_id,
            "source_copy_artifact_id": add("source-copy", f"copy-{index}").instance_id,
        }
        by_id = {a.instance_id: a for a in artifacts}
        events.append(
            make_generator_event(
                route_domain=route,
                event_index=index,
                public_family_id=family_id,
                artifact_ids=references,
                artifacts=by_id,
                augmentation_kind="base",
                augmentation_parent_sample_id=None,
                decision_index=index,
                seed_counter=index,
            )
        )
    return PreLabelGeneratorJournal(
        route_domain=route,
        artifacts=tuple(artifacts),
        events=tuple(events),
    ).validated(artifact_root, registry)


def _authority(journal, artifact_root, registry, *, salt=SALT_A):
    contract_artifact = next(a for a in journal.artifacts if a.kind == "route-contract")
    contract_facts = contract_artifact.verify_file(artifact_root)
    return freeze_split_authority(
        journal,
        artifact_root,
        registry,
        route_domain=journal.route_domain,
        route_contract_sha256=contract_facts["contract_sha256"],
        route_salt=salt,
        train_basis_points=7000,
        validation_basis_points=1500,
        test_basis_points=1500,
        minimum_samples_per_split=1,
        minimum_components_per_split=1,
        minimum_families_per_split=1,
    )


def test_freeze_writes_no_clobber_receipt_and_roundtrip(tmp_path: Path) -> None:
    registry = _registry()
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    journal = _make_journal(artifact_root, registry)
    authority = _authority(journal, artifact_root, registry)
    store_root = tmp_path / "receipts"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    label_paths = ["labels/train.parquet", "labels/validation.parquet", "labels/test.parquet"]

    receipt, receipt_path = freeze_prelabel_receipt(
        store_root,
        workspace,
        journal,
        artifact_root,
        registry,
        authority=authority,
        route_salt=SALT_A,
        round_index=0,
        labels_absent_relative_paths=label_paths,
        expected_previous_receipt_sha256=None,
    )
    assert receipt_path.exists()
    assert not (receipt_path.stat().st_mode & 0o200)
    reread = read_freeze_receipt(receipt_path)
    assert reread.receipt_sha256 == receipt.receipt_sha256


def test_freeze_refuses_overwrite_and_label_presence(tmp_path: Path) -> None:
    registry = _registry()
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    journal = _make_journal(artifact_root, registry)
    authority = _authority(journal, artifact_root, registry)
    store_root = tmp_path / "receipts"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    label_paths = ["labels/train.parquet"]

    freeze_prelabel_receipt(
        store_root, workspace, journal, artifact_root, registry,
        authority=authority, route_salt=SALT_A, round_index=0,
        labels_absent_relative_paths=label_paths,
        expected_previous_receipt_sha256=None,
    )
    # Second freeze of same route/round must fail.
    with pytest.raises(ValueError, match="already exists"):
        freeze_prelabel_receipt(
            store_root, workspace, journal, artifact_root, registry,
            authority=authority, route_salt=SALT_A, round_index=0,
            labels_absent_relative_paths=label_paths,
            expected_previous_receipt_sha256=None,
        )
    # Freeze with label already present must fail.
    (workspace / "labels").mkdir(parents=True)
    (workspace / "labels" / "train.parquet").write_bytes(b"data")
    with pytest.raises(ValueError, match="already exists before"):
        freeze_prelabel_receipt(
            store_root, workspace, journal, artifact_root, registry,
            authority=authority, route_salt=SALT_A, round_index=1,
            labels_absent_relative_paths=label_paths,
            expected_previous_receipt_sha256=None,
        )


def test_freeze_chain_requires_exact_predecessor(tmp_path: Path) -> None:
    registry = _registry()
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    journal = _make_journal(artifact_root, registry)
    authority = _authority(journal, artifact_root, registry)
    store_root = tmp_path / "receipts"
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    r0, _ = freeze_prelabel_receipt(
        store_root, workspace, journal, artifact_root, registry,
        authority=authority, route_salt=SALT_A, round_index=0,
        labels_absent_relative_paths=["labels/a.parquet"],
        expected_previous_receipt_sha256=None,
    )
    # Round 1 with wrong predecessor hash must fail.
    with pytest.raises(ValueError, match="predecessor differs"):
        freeze_prelabel_receipt(
            store_root, workspace, journal, artifact_root, registry,
            authority=authority, route_salt=SALT_A, round_index=1,
            labels_absent_relative_paths=["labels/b.parquet"],
            expected_previous_receipt_sha256="0" * 64,
        )
    # Round 1 with correct predecessor must succeed.
    freeze_prelabel_receipt(
        store_root, workspace, journal, artifact_root, registry,
        authority=authority, route_salt=SALT_A, round_index=1,
        labels_absent_relative_paths=["labels/b.parquet"],
        expected_previous_receipt_sha256=r0.receipt_sha256,
    )


def _init_git_repo(repo_root: Path) -> None:
    subprocess.run(["git", "init", str(repo_root)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo_root), "config", "user.email", "test@test.com"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_root), "config", "user.name", "Test"],
        check=True, capture_output=True,
    )


def test_git_pinned_receipt_roundtrip(tmp_path: Path) -> None:
    registry = _registry()
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    journal = _make_journal(artifact_root, registry)
    authority = _authority(journal, artifact_root, registry)

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    store_root = repo / "receipts"
    store_root.mkdir()
    workspace = repo / "workspace"
    workspace.mkdir()
    label_paths = ["labels/train.parquet"]

    receipt, receipt_path = freeze_prelabel_receipt(
        store_root, workspace, journal, artifact_root, registry,
        authority=authority, route_salt=SALT_A, round_index=0,
        labels_absent_relative_paths=label_paths,
        expected_previous_receipt_sha256=None,
    )
    relative_receipt = str(receipt_path.relative_to(repo))
    subprocess.run(
        ["git", "-C", str(repo), "add", relative_receipt],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "freeze receipt"],
        check=True, capture_output=True,
    )
    commit_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True,
    ).stdout.decode().strip()

    pin = verify_git_pinned_receipt(
        repo,
        commit_sha=commit_sha,
        receipt_relative_path=relative_receipt,
        expected_receipt_sha256=receipt.receipt_sha256,
        expected_route_domain="route-a1:m5b",
        expected_round_index=0,
    )
    assert pin.commit_sha == commit_sha

    verify_label_authorization(
        pin,
        receipt,
        authority,
        journal,
        artifact_root,
        registry,
        workspace,
        route_salt=SALT_A,
    )


def test_git_pin_rejects_non_ancestor_and_wrong_receipt(tmp_path: Path) -> None:
    registry = _registry()
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    journal = _make_journal(artifact_root, registry)
    authority = _authority(journal, artifact_root, registry)

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    store_root = repo / "receipts"
    store_root.mkdir()
    workspace = repo / "workspace"
    workspace.mkdir()

    receipt, receipt_path = freeze_prelabel_receipt(
        store_root, workspace, journal, artifact_root, registry,
        authority=authority, route_salt=SALT_A, round_index=0,
        labels_absent_relative_paths=["labels/a.parquet"],
        expected_previous_receipt_sha256=None,
    )
    relative = str(receipt_path.relative_to(repo))
    subprocess.run(["git", "-C", str(repo), "add", relative], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "freeze"], check=True, capture_output=True)
    commit_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True
    ).stdout.decode().strip()

    with pytest.raises(ValueError, match="differs from expected"):
        verify_git_pinned_receipt(
            repo,
            commit_sha=commit_sha,
            receipt_relative_path=relative,
            expected_receipt_sha256="0" * 64,
            expected_route_domain="route-a1:m5b",
            expected_round_index=0,
        )
