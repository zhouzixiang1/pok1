from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest

from bots.research_native_lab.common_contracts.actions import Action, ActionKind
from bots.research_native_lab.common_contracts.national_state import NationalGameState
from bots.research_native_lab.neural_conformance.prelabel import (
    ARTIFACT_KINDS,
    PreLabelArtifact,
    PreLabelGeneratorJournal,
    make_generator_event,
)
from bots.research_native_lab.neural_conformance.public_family import (
    public_family_id,
    public_family_payload,
)
from bots.research_native_lab.neural_conformance.split import (
    SplitAuthority,
    build_leakage_closed_split,
    content_id,
    freeze_split_authority,
    records_from_journal,
    verify_cross_route_independence,
    verify_leakage_closed_split,
)


SALT_A = "route-a1-test-salt-5"
SALT_B = "route-b-test-salt-0"


def _id(namespace: str, value: object) -> str:
    return content_id(namespace, value)


def _registry() -> dict[str, dict[str, object]]:
    root = NationalGameState.new_hand(1, small_blind=0)
    called = root.apply_action(Action(ActionKind.CALL))
    flop = called.apply_action(Action(ActionKind.CHECK)).apply_chance((8, 13, 18))
    checked = flop.apply_action(Action(ActionKind.CHECK))
    turn = checked.apply_action(Action(ActionKind.CALL)).apply_chance((24,))
    states = [root, called, flop, checked, turn]
    states.extend(
        root.apply_action(Action(ActionKind.RAISE, amount))
        for amount in range(200, 2200, 100)
    )
    return {
        public_family_id(state): public_family_payload(state) for state in states
    }


def _artifact(
    root: Path,
    *,
    route: str,
    token: str,
    kind: str,
    name: str,
    public_family_id: str | None = None,
    semantic_override: bytes | None = None,
) -> PreLabelArtifact:
    relative = f"{token}/{kind}/{name}.bin"
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    def digest(field: str) -> str:
        return hashlib.sha256(
            f"{route}|{token}|{kind}|{name}|{field}".encode("ascii")
        ).hexdigest()

    facts = {
        "route-contract": {
            "contract_sha256": digest("contract"),
            "contract_byte_count": 123,
        },
        "generator-code": {
            "source_closure_sha256": digest("source-closure"),
            "source_file_count": 3,
        },
        "generator-config": {"config_sha256": digest("config")},
        "seed-root": {"seed_root_sha256": digest("seed"), "seed_count": 100},
        "source-checkpoint": {
            "checkpoint_sha256": digest("checkpoint"),
            "checkpoint_byte_count": 456,
        },
        "pbs-input": {
            "public_family_id": public_family_id,
            "public_input_sha256": digest("public-input"),
            "range_payload_sha256": digest("ranges"),
            "legal_mask_sha256": digest("legal-mask"),
            "semantics_sha256": digest("semantics"),
        },
        "trajectory": {
            "trajectory_sha256": digest("trajectory"),
            "deal_key_sha256": digest("deal-key"),
            "decision_count": 7,
        },
        "rollout": {
            "rollout_plan_sha256": digest("rollout-plan"),
            "seed_block_sha256": digest("seed-block"),
            "rollout_count": 32,
        },
        "source-copy": {"origin_sha256": digest("origin"), "copy_ordinal": 0},
    }[kind]
    payload = semantic_override if semantic_override is not None else json.dumps(
        {
            "kind": kind,
            "payload": facts,
            "schema": "neural-prelabel-artifact-content-v1",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    path.write_bytes(payload)
    return PreLabelArtifact.from_file(
        root,
        route_domain=route,
        kind=kind,
        relative_path=relative,
    )


def _journal(
    root: Path,
    registry: dict[str, dict[str, object]],
    *,
    route: str = "route-a1:m5b",
    token: str = "a",
    semantic_overrides: dict[str, bytes] | None = None,
) -> PreLabelGeneratorJournal:
    semantic_overrides = {} if semantic_overrides is None else semantic_overrides
    override_used: set[str] = set()
    artifacts: list[PreLabelArtifact] = []

    def add(
        kind: str, name: str, *, public_family_id: str | None = None
    ) -> PreLabelArtifact:
        artifact = _artifact(
            root,
            route=route,
            token=token,
            kind=kind,
            name=name,
            public_family_id=public_family_id,
            semantic_override=(
                semantic_overrides.get(kind)
                if kind not in override_used
                else None
            ),
        )
        override_used.add(kind)
        artifacts.append(artifact)
        return artifact

    common = {
        "route_contract_artifact_id": add(
            "route-contract", "contract"
        ).instance_id,
        "generator_code_artifact_id": add("generator-code", "code").instance_id,
        "generator_config_artifact_id": add("generator-config", "config").instance_id,
        "seed_root_artifact_id": add("seed-root", "seed").instance_id,
        "source_checkpoint_artifact_id": add(
            "source-checkpoint", "checkpoint"
        ).instance_id,
    }
    families = sorted(registry)
    events = []
    shared_trajectory = add("trajectory", "shared-trajectory-0-2")
    for index, family_id in enumerate(families):
        trajectory = (
            shared_trajectory
            if index in (0, 2)
            else add("trajectory", f"trajectory-{index}")
        )
        references = {
            **common,
            "pbs_artifact_id": add(
                "pbs-input", f"pbs-{index}", public_family_id=family_id
            ).instance_id,
            "trajectory_artifact_id": trajectory.instance_id,
            "rollout_artifact_id": add("rollout", f"rollout-{index}").instance_id,
            "source_copy_artifact_id": add(
                "source-copy", f"source-copy-{index}"
            ).instance_id,
        }
        by_id = {artifact.instance_id: artifact for artifact in artifacts}
        derived = index == 4
        events.append(
            make_generator_event(
                route_domain=route,
                event_index=index,
                public_family_id=family_id,
                artifact_ids=references,
                artifacts=by_id,
                augmentation_kind="derived" if derived else "base",
                augmentation_parent_sample_id=(events[3].sample_id if derived else None),
                decision_index=index,
                seed_counter=index,
            )
        )
    journal = PreLabelGeneratorJournal(
        route_domain=route,
        artifacts=tuple(artifacts),
        events=tuple(events),
    )
    return journal.validated(root, registry)


def _authority(
    journal: PreLabelGeneratorJournal,
    root: Path,
    registry: dict[str, dict[str, object]],
    *,
    salt: str = SALT_A,
) -> SplitAuthority:
    return freeze_split_authority(
        journal,
        root,
        registry,
        route_domain=journal.route_domain,
        route_contract_sha256=next(
            artifact.evidence_sha256s[0]
            for artifact in journal.artifacts
            if artifact.kind == "route-contract"
        ),
        route_salt=salt,
        train_basis_points=7000,
        validation_basis_points=1500,
        test_basis_points=1500,
        minimum_samples_per_split=1,
        minimum_components_per_split=1,
        minimum_families_per_split=1,
    )


def _build(
    journal: PreLabelGeneratorJournal,
    root: Path,
    registry: dict[str, dict[str, object]],
    *,
    authority: SplitAuthority | None = None,
    salt: str = SALT_A,
):
    authority = _authority(journal, root, registry, salt=salt) if authority is None else authority
    return build_leakage_closed_split(
        journal,
        root,
        registry,
        authority=authority,
        route_salt=salt,
    )


def test_typed_journal_replays_complete_relations_and_split(tmp_path: Path) -> None:
    registry = _registry()
    journal = _journal(tmp_path, registry)
    records = records_from_journal(journal, tmp_path, registry)
    authority = _authority(journal, tmp_path, registry)
    manifest = _build(journal, tmp_path, registry, authority=authority)
    rows = {row["sample_id"]: row for row in manifest["samples"]}

    assert rows[journal.events[0].sample_id]["component_id"] == rows[
        journal.events[2].sample_id
    ]["component_id"]
    # A shared source checkpoint is metadata, not a global union edge.
    by_id = {record.sample_id: record for record in records}
    first = by_id[journal.events[1].sample_id]
    second = by_id[journal.events[3].sample_id]
    assert first.source_checkpoint_id == second.source_checkpoint_id
    assert rows[first.sample_id]["component_id"] != rows[
        second.sample_id
    ]["component_id"]
    assert all(
        manifest["split_stats"][name]["component_count"] > 0
        for name in ("train", "validation", "test")
    )
    verify_leakage_closed_split(
        manifest,
        tmp_path,
        registry,
        route_salt=SALT_A,
        authority=authority,
        expected_authority_sha256=authority.digest,
    )


def test_arbitrary_ids_missing_relation_and_changed_bytes_fail_closed(
    tmp_path: Path,
) -> None:
    registry = _registry()
    journal = _journal(tmp_path, registry)
    artifacts = journal.artifact_map()
    forged = replace(journal.events[0], sample_id="0" * 64)
    with pytest.raises(ValueError, match="not replay-derived"):
        forged.validated(artifacts)
    missing_relation = replace(journal.events[0], trajectory_artifact_id="0" * 64)
    with pytest.raises(ValueError, match="absent from artifact registry"):
        missing_relation.validated(artifacts)

    authority = _authority(journal, tmp_path, registry)
    manifest = _build(journal, tmp_path, registry, authority=authority)
    artifact = journal.artifacts[0]
    (tmp_path / artifact.relative_path).write_bytes(b"post-freeze mutation\n")
    with pytest.raises(ValueError, match="bytes differ|byte_count"):
        verify_leakage_closed_split(
            manifest,
            tmp_path,
            registry,
            route_salt=SALT_A,
            authority=authority,
            expected_authority_sha256=authority.digest,
        )


def test_manifest_cannot_replace_external_authority(tmp_path: Path) -> None:
    registry = _registry()
    journal = _journal(tmp_path, registry)
    authority = _authority(journal, tmp_path, registry)
    original = _build(journal, tmp_path, registry, authority=authority)

    tampered = copy.deepcopy(original)
    tampered["samples"][0]["split"] = (
        "train" if tampered["samples"][0]["split"] != "train" else "test"
    )
    with pytest.raises(ValueError, match="recomputation"):
        verify_leakage_closed_split(
            tampered,
            tmp_path,
            registry,
            route_salt=SALT_A,
            authority=authority,
            expected_authority_sha256=authority.digest,
        )
    with pytest.raises(ValueError, match="pinned digest"):
        verify_leakage_closed_split(
            original,
            tmp_path,
            registry,
            route_salt=SALT_A,
            authority=authority,
            expected_authority_sha256="0" * 64,
        )


@pytest.mark.parametrize("reused_kind", sorted(ARTIFACT_KINDS))
def test_cross_route_rejects_one_route_owned_semantic_asset_overlap(
    tmp_path: Path,
    reused_kind: str,
) -> None:
    registry = _registry()
    a_root = tmp_path / "a-root"
    b_root = tmp_path / "b-root"
    a_root.mkdir()
    b_root.mkdir()
    a = _journal(a_root, registry, route="route-a1:m5b", token="a")
    unique_b = _journal(b_root, registry, route="route-b:m5", token="b")
    a_authority = _authority(a, a_root, registry, salt=SALT_A)
    unique_b_authority = _authority(unique_b, b_root, registry, salt=SALT_B)
    verify_cross_route_independence(
        a_authority,
        unique_b_authority,
        a,
        unique_b,
        a_root,
        b_root,
        registry,
        registry,
    )

    # Rebuild B under a fresh root with exactly one A semantic byte sequence.
    copied = next(artifact for artifact in a.artifacts if artifact.kind == reused_kind)
    copied_bytes = (a_root / copied.relative_path).read_bytes()
    reused_root = tmp_path / f"b-reused-{reused_kind}"
    reused_root.mkdir()
    reused_b = _journal(
        reused_root,
        registry,
        route="route-b:m5",
        token=f"b-{reused_kind}",
        semantic_overrides={reused_kind: copied_bytes},
    )
    reused_authority = _authority(reused_b, reused_root, registry, salt=SALT_B)
    expected_error = (
        "route_contract_sha256"
        if reused_kind == "route-contract"
        else f"{reused_kind} artifact content"
    )
    with pytest.raises(ValueError, match=expected_error):
        verify_cross_route_independence(
            a_authority,
            reused_authority,
            a,
            reused_b,
            a_root,
            reused_root,
            registry,
            registry,
        )


def test_public_family_overlap_is_explicitly_route_neutral(tmp_path: Path) -> None:
    registry = _registry()
    a_root = tmp_path / "a"
    b_root = tmp_path / "b"
    a_root.mkdir()
    b_root.mkdir()
    a = _journal(a_root, registry, route="route-a1:m5b", token="a")
    b = _journal(b_root, registry, route="route-b:m5", token="b")
    verify_cross_route_independence(
        _authority(a, a_root, registry, salt=SALT_A),
        _authority(b, b_root, registry, salt=SALT_B),
        a,
        b,
        a_root,
        b_root,
        registry,
        registry,
    )
    assert {event.public_family_id for event in a.events} == {
        event.public_family_id for event in b.events
    }
