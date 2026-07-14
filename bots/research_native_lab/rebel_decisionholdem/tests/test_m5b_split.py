from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from ...common_contracts.actions import Action, ActionKind
from ...common_contracts.national_state import NationalGameState
from ..rebel_like.m5b_data import (
    ROUTE_DOMAIN_SALT,
    PreLabelPlan,
    TestOnceSeal as OnceSeal,
    _digest,
    build_split_manifest,
    consume_test_once_seal,
    prelabel_plan_digest,
    public_family_payload,
    canonical_test_once_receipt_path,
    verify_split_manifest,
    write_test_once_seal,
)


def _plan(
    index: int,
    *,
    family: str,
    trajectory: str | None = None,
    source_checkpoint: str | None = None,
) -> PreLabelPlan:
    trajectory = trajectory or _digest({"trajectory": index})
    rollout = _digest({"rollout": index})
    source_copy = _digest({"source_copy": index})
    source_checkpoint = source_checkpoint or _digest({"checkpoint": 0})
    pbs = _digest({"pbs": index})
    payload = {
        "schema": "route-a1-m5b-prelabel-sample-id-v1",
        "route_domain_salt": ROUTE_DOMAIN_SALT,
        "pbs_state_id": pbs,
        "public_family_id": family,
        "trajectory_id": trajectory,
        "rollout_group_id": rollout,
        "source_copy_group_id": source_copy,
        "source_checkpoint_digest": source_checkpoint,
        "decision_index": index,
        "seed_group": index,
    }
    return PreLabelPlan(
        sample_id=_digest(payload),
        pbs_state_id=pbs,
        public_family_id=family,
        trajectory_id=trajectory,
        rollout_group_id=rollout,
        augmentation_parent_sample_id=None,
        source_copy_group_id=source_copy,
        source_checkpoint_digest=source_checkpoint,
        decision_index=index,
        seed_group=index,
    )


def _three_way_plans() -> list[PreLabelPlan]:
    plans: list[PreLabelPlan] = []
    found: set[str] = set()
    index = 0
    while found != {"train", "validation", "test"}:
        family = _digest({"family": index})
        plan = _plan(index, family=family)
        candidate = plans + [plan]
        try:
            manifest = build_split_manifest(
                candidate,
                expected_prelabel_plan_digest=prelabel_plan_digest(candidate),
                split_seed=2026071425,
                basis_points={"train": 7000, "validation": 1500, "test": 1500},
            )
        except ValueError as exc:
            if "three split" not in str(exc):
                raise
        else:
            found = set(manifest["sample_splits"].values())
        plans = candidate
        index += 1
        assert index < 200
    return plans


def _build(plans, *, previous=None):
    return build_split_manifest(
        plans,
        expected_prelabel_plan_digest=prelabel_plan_digest(plans),
        split_seed=2026071425,
        basis_points={"train": 7000, "validation": 1500, "test": 1500},
        previous_manifest=previous,
    )


def test_split_is_externally_bound_and_three_way_disjoint() -> None:
    plans = _three_way_plans()
    manifest = _build(plans)
    assert set(manifest["sample_splits"].values()) == {
        "train",
        "validation",
        "test",
    }
    assert len(set(manifest["split_component_digests"].values())) == 3
    assert manifest["outcomes_used_for_split"] is False
    verify_split_manifest(
        manifest,
        plans,
        expected_prelabel_plan_digest=prelabel_plan_digest(plans),
        split_seed=2026071425,
        basis_points={"train": 7000, "validation": 1500, "test": 1500},
    )
    with pytest.raises(ValueError, match="external commitment"):
        build_split_manifest(
            plans[:-1],
            expected_prelabel_plan_digest=prelabel_plan_digest(plans),
            split_seed=2026071425,
            basis_points={"train": 7000, "validation": 1500, "test": 1500},
        )


def test_same_family_different_seed_unions_but_same_checkpoint_different_family_does_not() -> None:
    plans = _three_way_plans()
    base = _build(plans)
    original = plans[0]
    appended = _plan(
        1001,
        family=original.public_family_id,
        source_checkpoint=original.source_checkpoint_digest,
    )
    extension = _build(plans + [appended], previous=base)
    assert extension["sample_splits"][appended.sample_id] == base["sample_splits"][
        original.sample_id
    ]
    # A common generator checkpoint is metadata, not a global union edge.
    checkpoint_members = [
        plan for plan in plans if plan.source_checkpoint_digest == original.source_checkpoint_digest
    ]
    assert len({base["sample_splits"][plan.sample_id] for plan in checkpoint_members}) == 3


def test_cross_split_bridge_is_rejected_in_monotonic_extension() -> None:
    plans = _three_way_plans()
    base = _build(plans)
    by_split = {
        split: next(plan for plan in plans if base["sample_splits"][plan.sample_id] == split)
        for split in ("train", "test")
    }
    # The new train-family copy shares the existing test trajectory and would
    # conservatively lift the old train component to test.  Monotonic extension
    # must reject rather than silently migrate round-0 data.
    bridge = _plan(
        2001,
        family=by_split["train"].public_family_id,
        trajectory=by_split["test"].trajectory_id,
    )
    with pytest.raises(ValueError, match="migrate an existing sample"):
        _build(plans + [bridge], previous=base)


def test_public_family_is_route_neutral_and_rejects_terminal_outcome() -> None:
    state = NationalGameState.new_hand(1, small_blind=0)
    payload = public_family_payload(state)
    assert "route_domain_salt" not in payload
    terminal = state.apply_action(Action(ActionKind.FOLD))
    with pytest.raises(ValueError, match="terminal"):
        public_family_payload(terminal)


def test_durable_test_once_seal_survives_restart_and_concurrent_double_open(
    tmp_path,
) -> None:
    seal = OnceSeal(
        model_sha256=_digest("model"),
        threshold_sha256=_digest("threshold"),
        strongest_baseline_sha256=_digest("baseline"),
        split_manifest_sha256=_digest("split"),
    )
    seal_path = tmp_path / "test.seal.json"
    receipt_path = canonical_test_once_receipt_path(seal_path, seal)
    write_test_once_seal(seal_path, seal)
    write_test_once_seal(seal_path, seal)  # byte-identical idempotent reuse

    def consume():
        try:
            return consume_test_once_seal(seal_path, seal)
        except ValueError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: consume(), range(2)))
    assert sum(isinstance(outcome, dict) for outcome in outcomes) == 1
    assert sum("already" in outcome for outcome in outcomes if isinstance(outcome, str)) == 1
    assert receipt_path.is_file()
    # A fresh process would reconstruct the same immutable seal object; the
    # durable receipt still closes the gate.
    with pytest.raises(ValueError, match="already"):
        consume_test_once_seal(seal_path, seal)
    # A caller cannot create a second uniqueness domain by choosing another
    # receipt filename: the marker path is derived solely from seal authority.
    alias_seal_path = tmp_path / "same-authority-alias.seal.json"
    os.link(seal_path, alias_seal_path)
    assert canonical_test_once_receipt_path(alias_seal_path, seal) == receipt_path
    with pytest.raises(ValueError, match="already"):
        consume_test_once_seal(alias_seal_path, seal)
    changed = OnceSeal(
        model_sha256=_digest("different"),
        threshold_sha256=seal.threshold_sha256,
        strongest_baseline_sha256=seal.strongest_baseline_sha256,
        split_manifest_sha256=seal.split_manifest_sha256,
    )
    with pytest.raises(ValueError, match="collision"):
        write_test_once_seal(seal_path, changed)
    actual_directory = tmp_path / "actual-seal-directory"
    actual_directory.mkdir()
    alias_directory = tmp_path / "alias-seal-directory"
    os.symlink(actual_directory, alias_directory)
    with pytest.raises(ValueError, match="path contains a symlink"):
        write_test_once_seal(alias_directory / "escaped.seal.json", seal)
