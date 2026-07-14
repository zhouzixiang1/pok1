from __future__ import annotations

import copy
import hashlib
import inspect
import math
from dataclasses import asdict, fields, replace
from types import SimpleNamespace

import pytest

from bots.research_native_lab.common_contracts import seeds
from bots.research_native_lab.common_contracts.deal_generator import (
    DEAL_GENERATOR_ALGORITHM_DIGEST,
)
from bots.research_native_lab.common_contracts.evaluation import (
    ArtifactIdentity,
    FormalEvaluationPlan,
    ResourceProfile,
)
from bots.research_native_lab.common_contracts.matrix import (
    ABLATION_PAIRED_BLOCKS,
    CHECKPOINT_IDS,
    COMPARISON_MODES,
    DIRECT_PAIRED_BLOCKS,
    EXTERNAL_PAIRED_BLOCKS,
    FORMAL_BUDGETS_MS,
    HELDOUT_SLOT_IDS,
    ROUTE_IDS,
    AblationRegistration,
    CheckpointFreezeReceipt,
    CompleteFormalMatrix,
    FormalCellResult,
    FormalEvaluationPlanBridge,
    HeldoutReveal,
    LegacyRouteArtifacts,
    MainArtifact,
    MandatoryAblationRegistry,
    MatrixSharedContracts,
    NotApplicableAblationReceipt,
    RouteArtifacts,
    SupervisorObservationBinding,
    build_complete_formal_matrix,
    build_diagnostic_evaluation_plan_bridge,
    build_formal_evaluation_plan_bridge,
    build_formal_matrix_result_ledger,
    build_legacy_diagnostic_matrix,
    create_heldout_precommitment,
    formal_attempt_journal_genesis_digest,
    formal_attempt_journal_scope_digest,
    materialize_diagnostic_matrix_projection,
    materialize_formal_matrix,
    validate_attempt_journal_leg_policy,
)


def _h(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _artifact(label: str, key: str, *, action_set: str) -> ArtifactIdentity:
    return ArtifactIdentity(
        display_label=label,
        sealed_tree_manifest_digest=_h(f"{key}/tree"),
        launch_contract_digest=_h(f"{key}/launch-contract"),
        launch_command_digest=_h(f"{key}/launch-command"),
        base_environment_digest=_h(f"{key}/base-environment"),
        model_digest=_h(f"{key}/model"),
        config_digest=_h(f"{key}/config"),
        action_set_digest=_h(action_set),
        dependency_digest=_h(f"{key}/dependencies"),
        runtime_digest=_h(f"{key}/runtime"),
    )


def _profile(budget_ms: int) -> ResourceProfile:
    return ResourceProfile(
        cpu_affinity_by_connection=((0, 1), (2, 3)),
        cpu_threads_per_connection=2,
        cpu_quota_us=200_000,
        cpu_period_us=100_000,
        max_tasks_per_connection=8,
        thread_environment_digest=_h("formal-thread-environment"),
        ram_limit_bytes_per_connection=2 * 1024 * 1024 * 1024,
        swap_limit_bytes_per_connection=0,
        gpu_devices_by_connection=((), ()),
        vram_limit_bytes_per_connection=0,
        decision_budget_ms=budget_ms,
        platform_action_timeout_ms=60_000,
        match_wall_timeout_ms=6_000_000,
        action_send_delay_ms=0,
        enforcer_digest=_h("formal-resource-enforcer"),
        cgroup_controllers_digest=_h("cpuset+cpu+memory+pids"),
    )


def _contracts() -> MatrixSharedContracts:
    return MatrixSharedContracts(
        common_contract_tree_digest=_h("common-contract-tree"),
        rules_digest=_h("national-rules"),
        harness_digest=_h("native-evaluation-harness"),
        deal_generator_digest=DEAL_GENERATOR_ALGORITHM_DIGEST,
        evaluation_contract_digest=_h("evaluation-contract-v3"),
        final_randomness_contract_digest=_h("final-randomness-contract-v1"),
        stopping_rule_digest=_h("fixed-complete-block-set-no-early-stop"),
        retry_policy_digest=_h("infrastructure-only-same-leg-max-two"),
        analysis_code_digest=_h("paired-bootstrap-sign-flip-holm-code"),
        replay_verifier_digest=_h("native-replay-verifier"),
        oracle_fixture_digest=_h("national-oracle-fixtures"),
        infrastructure_monitor_digest=_h("independent-infrastructure-monitor"),
        heldout_commitment_contract_digest=_h("heldout-salted-commitment-v1"),
        ablation_registry_contract_digest=_h("mandatory-ablation-registry-v1"),
        result_ledger_contract_digest=_h("formal-matrix-result-ledger-v1"),
    )


def _routes() -> tuple[RouteArtifacts, ...]:
    routes = []
    for route in ROUTE_IDS:
        main = []
        for checkpoint in CHECKPOINT_IDS:
            for mode in COMPARISON_MODES:
                action_set = "controlled-action-set" if mode == "controlled" else f"{route}/{checkpoint}/best-actions"
                key = f"{route}/{checkpoint}/{mode}"
                main.append(MainArtifact(checkpoint, mode, _artifact(key, key, action_set=action_set)))
        routes.append(RouteArtifacts(route, tuple(main)))
    return tuple(routes)


def _checkpoint_freezes(routes: tuple[RouteArtifacts, ...]) -> tuple[CheckpointFreezeReceipt, ...]:
    result = []
    for route in routes:
        for checkpoint in CHECKPOINT_IDS:
            budget = "equal-offline-envelope" if checkpoint == "equal-offline-compute" else f"{route.route_id}/best/full-compute"
            result.append(
                CheckpointFreezeReceipt(
                    route_id=route.route_id,
                    checkpoint=checkpoint,
                    controlled_identity_digest=route.identity_for(checkpoint, "controlled"),
                    best_of_route_identity_digest=route.identity_for(checkpoint, "best-of-route"),
                    offline_compute_budget_digest=_h(budget),
                    training_seed_manifest_digest=_h(f"{route.route_id}/{checkpoint}/training-seeds"),
                    training_data_manifest_digest=_h(f"{route.route_id}/{checkpoint}/training-data"),
                    training_resource_receipt_digest=_h(f"{route.route_id}/{checkpoint}/resources"),
                    validation_selection_digest=_h(f"{route.route_id}/{checkpoint}/validation-selection"),
                )
            )
    return tuple(result)


_NA_KEYS = {
    ("A1", "no-cross-hand-model"),
    ("A1", "no-70-hand-controller"),
    ("A2", "no-cross-hand-model"),
    ("A2", "no-70-hand-controller"),
}


def _registry() -> MandatoryAblationRegistry:
    # Use the public route-specific constants indirectly through the exact
    # validation surface: enumerate the IDs accepted for each route.
    from bots.research_native_lab.common_contracts.matrix import (
        COMMON_ABLATION_IDS,
        ROUTE_SPECIFIC_ABLATION_IDS,
    )

    entries = []
    for route in ROUTE_IDS:
        for ablation in COMMON_ABLATION_IDS + ROUTE_SPECIFIC_ABLATION_IDS[route]:
            key = (route, ablation)
            if key in _NA_KEYS:
                entries.append(
                    AblationRegistration(
                        route,
                        ablation,
                        "not-applicable",
                        "route contract contains no such component; explicit N/A retained",
                    )
                )
            else:
                entries.append(
                    AblationRegistration(
                        route,
                        ablation,
                        "materialized",
                        "component toggle or declared variant is independently frozen",
                        ablated_artifact=_artifact(
                            f"{route}-{ablation}",
                            f"{route}/ablation/{ablation}",
                            action_set=f"{route}/ablation/{ablation}/actions",
                        ),
                    )
                )
    return MandatoryAblationRegistry(tuple(entries))


def _fixture_parts() -> dict[str, object]:
    routes = _routes()
    registry = _registry()
    current_pool = (
        _artifact("pool-1", "opponents/pool-1", action_set="opponent-actions"),
        _artifact("pool-2", "opponents/pool-2", action_set="opponent-actions"),
    )
    anchor = _artifact("anchor", "opponents/anchor", action_set="opponent-actions")
    nemesis = (_artifact("nemesis", "opponents/nemesis", action_set="opponent-actions"),)
    train = (_artifact("train", "opponents/train", action_set="opponent-actions"),)
    dev = (_artifact("dev", "opponents/dev", action_set="opponent-actions"),)
    validation = (
        _artifact("validation", "opponents/validation", action_set="opponent-actions"),
    )
    known = [identity for route in routes for identity in route.identity_digests]
    known.extend(
        entry.ablated_artifact.identity_digest()
        for entry in registry.materialized
        if entry.ablated_artifact is not None
    )
    known.extend(item.identity_digest() for item in current_pool + (anchor,) + nemesis + train + dev + validation)
    heldout = HeldoutReveal(
        salt_hex="a5" * 32,
        universe=tuple(
            _artifact(f"heldout-{index}", f"opponents/heldout-{index}", action_set="opponent-actions")
            for index in range(6)
        ),
    )
    return {
        "routes": routes,
        "checkpoint_freezes": _checkpoint_freezes(routes),
        "ablation_registry": registry,
        "current_pool": current_pool,
        "stable_anchor": anchor,
        "nemesis": nemesis,
        "train_opponents": train,
        "dev_opponents": dev,
        "validation_opponents": validation,
        "heldout_reveal": heldout,
        "heldout_precommitment": create_heldout_precommitment(heldout, known),
        "resource_profiles": {budget: _profile(budget) for budget in FORMAL_BUDGETS_MS},
        "contracts": _contracts(),
    }


def _matrix(**overrides: object) -> CompleteFormalMatrix:
    values = _fixture_parts()
    values.pop("heldout_reveal")
    values.update(overrides)
    return build_complete_formal_matrix(**values)  # type: ignore[arg-type]


def _matrix_and_reveal() -> tuple[CompleteFormalMatrix, HeldoutReveal]:
    values = _fixture_parts()
    reveal = values.pop("heldout_reveal")
    return build_complete_formal_matrix(**values), reveal  # type: ignore[arg-type,return-value]


def _resolved_artifacts(
    matrix: CompleteFormalMatrix,
    projection,
    materialized,
) -> tuple[ArtifactIdentity, ArtifactIdentity]:
    artifacts = [
        item.artifact
        for route in matrix.routes
        for item in route.main_artifacts
    ]
    artifacts.extend(
        item.ablated_artifact
        for item in matrix.ablation_registry.materialized
        if item.ablated_artifact is not None
    )
    artifacts.extend(
        artifact
        for family in matrix.fixed_families
        for artifact in family.members
    )
    artifacts.extend(projection.selected_heldout_artifacts)
    by_digest = {item.identity_digest(): item for item in artifacts}
    return (
        by_digest[materialized.receipt.resolved_focal_identity_digest],
        by_digest[materialized.receipt.resolved_counterparty_identity_digest],
    )


def _authorized_entropy(
    matrix: CompleteFormalMatrix,
    *,
    randomness: str = "11" * 32,
) -> tuple[seeds.FinalEvaluationPlan, seeds.VerifiedBeacon]:
    not_before = 1_800_003_600
    round_number = (
        math.ceil(
            (not_before - seeds.DRAND_GENESIS_TIME) / seeds.DRAND_PERIOD_SEC
        )
        + 1
    )
    plan = seeds.FinalEvaluationPlan(
        candidate_bundle_digest=matrix.digest(),
        freeze_attested_epoch=1_800_000_000,
        freeze_receipt_digest="01" * 32,
        freeze_bitcoin_height=900_000,
        freeze_bitcoin_block_hash="02" * 32,
        beacon_not_before_epoch=not_before,
        beacon_round=round_number,
    )
    object.__setattr__(plan, "_token", seeds._FINAL_PLAN_TOKEN)
    beacon = seeds.VerifiedBeacon(
        chain_hash=seeds.DRAND_CHAIN_HASH,
        round=round_number,
        randomness=randomness,
        signature="03" * 96,
        previous_signature="04" * 96,
        receipt_digest="55" * 32,
    )
    object.__setattr__(beacon, "_token", seeds._VERIFIED_BEACON_TOKEN)
    return plan, beacon


def test_formal_matrix_has_twelve_main_artifacts_and_exact_cross_product() -> None:
    matrix = _matrix()
    matrix.assert_complete_registration()
    with pytest.raises(ValueError, match="formal checkpoint freeze is unavailable"):
        matrix.assert_formal_authority()
    assert len(matrix.all_main_identity_digests) == 12
    assert len(set(matrix.all_main_identity_digests)) == 12
    assert all(len(route.main_artifacts) == 4 for route in matrix.routes)

    direct = [item for item in matrix.planned_templates if item.key.kind == "direct-h2h"]
    fixed = [item for item in matrix.planned_templates if item.key.kind == "fixed-opponent"]
    heldout = [item for item in matrix.planned_templates if item.key.kind == "heldout-slot"]
    ablations = [item for item in matrix.planned_templates if item.key.kind == "ablation"]
    assert len(direct) == 3 * 4 * 4 == 48
    assert len(fixed) == 3 * 4 * 4 * 4 == 192
    assert len(heldout) == 3 * 4 * 4 * 4 == 192
    assert len(ablations) == len(matrix.ablation_registry.materialized) * 4
    assert len(matrix.planned_templates) == 432 + len(ablations)
    assert {item.paired_block_count for item in direct} == {DIRECT_PAIRED_BLOCKS}
    assert {item.paired_block_count for item in fixed + heldout} == {EXTERNAL_PAIRED_BLOCKS}
    assert {item.paired_block_count for item in ablations} == {ABLATION_PAIRED_BLOCKS}


def test_checkpoint_and_mode_are_both_full_formal_dimensions() -> None:
    matrix = _matrix()
    main = [item for item in matrix.planned_templates if item.key.kind != "ablation"]
    assert {item.key.checkpoint for item in main} == set(CHECKPOINT_IDS)
    assert {item.key.comparison_mode for item in main} == set(COMPARISON_MODES)
    controlled = [
        route.artifact_for(checkpoint, "controlled")
        for route in matrix.routes
        for checkpoint in CHECKPOINT_IDS
    ]
    assert len({item.action_set_digest for item in controlled}) == 1
    equal_receipts = [
        item for item in matrix.checkpoint_freezes if item.checkpoint == "equal-offline-compute"
    ]
    assert len({item.offline_compute_budget_digest for item in equal_receipts}) == 1


def test_ablation_registry_requires_every_key_or_explicit_na() -> None:
    registry = _registry()
    assert len(registry.not_applicable) == len(_NA_KEYS)
    assert {item.key for item in registry.not_applicable} == _NA_KEYS
    with pytest.raises(ValueError, match="explicitly cover every mandatory"):
        MandatoryAblationRegistry(registry.entries[:-1])
    registration = registry.not_applicable[0]
    with pytest.raises(ValueError, match="must not smuggle"):
        replace(
            registration,
            ablated_artifact=_artifact("bad", "bad/na", action_set="bad"),
        )


def test_heldout_prebeacon_root_contains_only_salted_commitment() -> None:
    matrix, reveal = _matrix_and_reveal()
    heldout_payload = asdict(matrix.heldout_precommitment)
    assert not any("salt_hex" in key or "universe" == key or "selected" in key for key in heldout_payload)
    frozen_root = matrix.digest()
    plan, beacon = _authorized_entropy(matrix)
    projection = materialize_diagnostic_matrix_projection(matrix, plan, beacon, reveal)
    assert projection.result_authority == "development_diagnostic_only"
    assert projection.complete_matrix_root_digest == frozen_root
    assert matrix.digest() == frozen_root
    assert len(projection.selected_heldout_artifacts) == 4
    assert len(projection.strata) == len(matrix.planned_templates)
    assert tuple(inspect.signature(materialize_formal_matrix).parameters) == (
        "matrix",
        "final_plan",
        "beacon",
        "heldout_reveal",
    )
    with pytest.raises(ValueError, match="formal checkpoint freeze is unavailable"):
        materialize_formal_matrix(matrix, plan, beacon, reveal)


def test_heldout_opening_is_salted_and_disjoint_from_all_known_splits() -> None:
    matrix, reveal = _matrix_and_reveal()
    plan, beacon = _authorized_entropy(matrix)
    tampered = replace(reveal, salt_hex="b6" * 32)
    with pytest.raises(ValueError, match="does not open"):
        materialize_diagnostic_matrix_projection(matrix, plan, beacon, tampered)

    overlap = replace(
        reveal,
        universe=(
            matrix.routes[0].main_artifacts[0].artifact,
        )
        + reveal.universe[1:],
    )
    with pytest.raises(ValueError, match="overlap train/dev/validation/fixed/research"):
        materialize_diagnostic_matrix_projection(matrix, plan, beacon, overlap)


def test_common_seed_cohort_excludes_candidate_checkpoint_and_mode() -> None:
    matrix = _matrix()
    opponent = matrix.fixed_opponent_identity_digests[0]
    cells = [
        item
        for item in matrix.planned_templates
        if item.key.kind == "fixed-opponent"
        and item.key.opponent_identity_digest == opponent
        and item.key.budget_ms == 5_000
    ]
    assert len(cells) == 12
    assert len({item.seed_cohort_digest for item in cells}) == 1
    cohort_field_names = {item.name for item in fields(seeds.FormalSeedCohort)}
    assert not cohort_field_names & {
        "candidate",
        "candidate_identity_digest",
        "focal_route",
        "checkpoint",
        "comparison_mode",
        "artifact_identity_digest",
    }

    direct = [
        item
        for item in matrix.planned_templates
        if item.key.kind == "direct-h2h"
        and (item.key.focal_route, item.key.peer_route) == ("A1", "A2")
        and item.key.budget_ms == 20_000
    ]
    assert len(direct) == 4
    assert len({item.seed_cohort_digest for item in direct}) == 1


def test_future_entropy_uses_same_decks_but_identity_specific_policy_rng() -> None:
    matrix = _matrix()
    plan, beacon = _authorized_entropy(matrix)
    opponent = matrix.fixed_opponent_identity_digests[0]
    cells = [
        item
        for item in matrix.planned_templates
        if item.key.kind == "fixed-opponent"
        and item.key.opponent_identity_digest == opponent
        and item.key.budget_ms == 250
    ]
    left, right = cells[0], cells[-1]
    assert left.seed_cohort_digest == right.seed_cohort_digest
    left_decks = plan.derive_formal_deck_root_pool(beacon, left.seed_cohort_digest)
    right_decks = plan.derive_formal_deck_root_pool(beacon, right.seed_cohort_digest)
    assert left_decks == right_decks
    left_policy = plan.derive_formal_policy_seeds(
        beacon, left.seed_cohort_digest, left.focal_identity_digest, 100
    )
    right_policy = plan.derive_formal_policy_seeds(
        beacon, right.seed_cohort_digest, right.focal_identity_digest, 100
    )
    assert left_policy != right_policy


def test_projection_derives_analysis_rng_from_beacon_not_caller() -> None:
    matrix, reveal = _matrix_and_reveal()
    plan, beacon = _authorized_entropy(matrix)
    projection = materialize_diagnostic_matrix_projection(matrix, plan, beacon, reveal)
    item = projection.strata[0]
    assert item.receipt.bootstrap_seed == plan.derive_formal_analysis_seed(
        beacon,
        seed_cohort_digest=item.template.seed_cohort_digest,
        hypothesis_digest=item.template.hypothesis_id,
        analysis_domain="bootstrap",
    )
    assert item.receipt.bootstrap_seed != item.receipt.sign_flip_seed
    _, different_beacon = _authorized_entropy(matrix, randomness="22" * 32)
    changed = materialize_diagnostic_matrix_projection(matrix, plan, different_beacon, reveal)
    assert changed.strata[0].receipt.bootstrap_seed != item.receipt.bootstrap_seed


def test_matrix_issues_complete_evaluation_plan_bridge_without_legacy_bundle_gap() -> None:
    matrix, reveal = _matrix_and_reveal()
    plan, beacon = _authorized_entropy(matrix)
    projection = materialize_diagnostic_matrix_projection(matrix, plan, beacon, reveal)
    materialized = projection.strata[0]
    bridge = build_diagnostic_evaluation_plan_bridge(
        matrix, projection, materialized, plan, beacon
    )
    bridge.assert_diagnostic_for(matrix, projection, materialized, plan, beacon)
    payload = bridge.sealed_payload()
    assert payload["complete_matrix_root_digest"] == matrix.digest()
    assert payload["projection_digest"] == projection.digest()
    assert payload["seed_cohort_digest"] == materialized.template.seed_cohort_digest
    assert payload["candidate_bundle_digest"] != matrix.digest()
    assert payload["paired_block_count"] == materialized.template.paired_block_count
    assert payload["ordered_artifact_identity_digests"] == (
        materialized.receipt.resolved_focal_identity_digest,
        materialized.receipt.resolved_counterparty_identity_digest,
    )
    assert payload["bootstrap_seed"] == materialized.receipt.bootstrap_seed
    assert payload["sign_flip_seed"] == materialized.receipt.sign_flip_seed

    with pytest.raises(ValueError, match="formal checkpoint freeze is unavailable"):
        build_formal_evaluation_plan_bridge(
            matrix, projection, materialized, plan, beacon
        )


def test_evaluation_plan_bridge_copy_and_caller_construction_lose_authority() -> None:
    matrix, reveal = _matrix_and_reveal()
    plan, beacon = _authorized_entropy(matrix)
    projection = materialize_diagnostic_matrix_projection(matrix, plan, beacon, reveal)
    materialized = projection.strata[0]
    bridge = build_diagnostic_evaluation_plan_bridge(
        matrix, projection, materialized, plan, beacon
    )
    for clone in (replace(bridge), copy.copy(bridge), copy.deepcopy(bridge)):
        with pytest.raises(ValueError, match="copied, forged, altered"):
            clone.assert_diagnostic_for(
                matrix, projection, materialized, plan, beacon
            )

    fields_by_name = {
        item.name: getattr(bridge, item.name)
        for item in fields(FormalEvaluationPlanBridge)
        if item.init
    }
    forged = FormalEvaluationPlanBridge(**fields_by_name)
    with pytest.raises(ValueError, match="copied, forged, altered"):
        forged.digest()


def test_diagnostic_bridge_builds_exact_evaluation_plan_and_rejects_substitution() -> None:
    matrix, reveal = _matrix_and_reveal()
    entropy_plan, beacon = _authorized_entropy(matrix)
    projection = materialize_diagnostic_matrix_projection(
        matrix, entropy_plan, beacon, reveal
    )
    materialized = projection.strata[0]
    artifacts = _resolved_artifacts(matrix, projection, materialized)
    profile = next(
        item.profile
        for item in matrix.budget_profiles
        if item.profile.digest() == materialized.stratum.resource_profile_digest
    )
    bridge = build_diagnostic_evaluation_plan_bridge(
        matrix, projection, materialized, entropy_plan, beacon
    )
    evaluation_plan = FormalEvaluationPlan.from_matrix_bridge(
        matrix_bridge=bridge,
        complete_matrix=matrix,
        matrix_projection=projection,
        materialized_stratum=materialized,
        final_entropy_plan=entropy_plan,
        verified_beacon=beacon,
        artifacts=artifacts,
        resource_profile=profile,
    )
    assert evaluation_plan.result_authority == "development_diagnostic_only"
    assert evaluation_plan.complete_matrix_root_digest == matrix.digest()
    assert evaluation_plan.matrix_projection_digest == projection.digest()
    assert evaluation_plan.matrix_template_digest == materialized.template.digest()
    assert evaluation_plan.formal_seed_cohort_digest == (
        materialized.template.seed_cohort_digest
    )
    assert evaluation_plan.matrix_plan_bridge_digest == bridge.digest()
    assert len(evaluation_plan.blocks) == materialized.template.paired_block_count
    assert evaluation_plan.bootstrap_seed == materialized.receipt.bootstrap_seed
    assert evaluation_plan.sign_flip_seed == materialized.receipt.sign_flip_seed

    wrong_artifacts = (matrix.routes[-1].main_artifacts[-1].artifact, artifacts[1])
    with pytest.raises(ValueError, match="artifacts differ"):
        FormalEvaluationPlan.from_matrix_bridge(
            matrix_bridge=bridge,
            complete_matrix=matrix,
            matrix_projection=projection,
            materialized_stratum=materialized,
            final_entropy_plan=entropy_plan,
            verified_beacon=beacon,
            artifacts=wrong_artifacts,
            resource_profile=profile,
        )
    wrong_profile = next(
        item.profile
        for item in matrix.budget_profiles
        if item.profile.digest() != profile.digest()
    )
    with pytest.raises(ValueError, match="resource profile differs"):
        FormalEvaluationPlan.from_matrix_bridge(
            matrix_bridge=bridge,
            complete_matrix=matrix,
            matrix_projection=projection,
            materialized_stratum=materialized,
            final_entropy_plan=entropy_plan,
            verified_beacon=beacon,
            artifacts=artifacts,
            resource_profile=wrong_profile,
        )
    with pytest.raises(ValueError, match="copied, forged, altered"):
        FormalEvaluationPlan.from_matrix_bridge(
            matrix_bridge=replace(bridge),
            complete_matrix=matrix,
            matrix_projection=projection,
            materialized_stratum=materialized,
            final_entropy_plan=entropy_plan,
            verified_beacon=beacon,
            artifacts=artifacts,
            resource_profile=profile,
        )

    altered = build_diagnostic_evaluation_plan_bridge(
        matrix, projection, materialized, entropy_plan, beacon
    )
    object.__setattr__(altered, "seed_cohort_digest", _h("wrong-cohort"))
    with pytest.raises(ValueError, match="copied, forged, altered"):
        FormalEvaluationPlan.from_matrix_bridge(
            matrix_bridge=altered,
            complete_matrix=matrix,
            matrix_projection=projection,
            materialized_stratum=materialized,
            final_entropy_plan=entropy_plan,
            verified_beacon=beacon,
            artifacts=artifacts,
            resource_profile=profile,
        )


def test_projection_and_selection_copy_paths_strip_materialization_authority() -> None:
    matrix, reveal = _matrix_and_reveal()
    plan, beacon = _authorized_entropy(matrix)
    projection = materialize_diagnostic_matrix_projection(matrix, plan, beacon, reveal)
    for clone in (replace(projection), copy.copy(projection), copy.deepcopy(projection)):
        with pytest.raises(ValueError, match="materialization authority"):
            clone.assert_diagnostic_for(matrix, plan, beacon)
    for selection in (
        replace(projection.selection_receipt),
        copy.copy(projection.selection_receipt),
        copy.deepcopy(projection.selection_receipt),
    ):
        with pytest.raises(ValueError, match="future-beacon authority"):
            selection.assert_diagnostic_for(matrix, plan, beacon)


def test_matrix_is_the_unique_prebeacon_root_and_copy_loses_authority() -> None:
    matrix = _matrix()
    token_field = next(item for item in fields(CompleteFormalMatrix) if item.name == "_formal_token")
    assert token_field.init is False
    clones = (replace(matrix), copy.copy(matrix), copy.deepcopy(matrix))
    for clone in clones:
        assert clone.digest() == matrix.digest()
        with pytest.raises(ValueError, match="preregistration gate authority"):
            clone.assert_complete_registration()

    direct = CompleteFormalMatrix(
        routes=matrix.routes,
        checkpoint_freezes=matrix.checkpoint_freezes,
        ablation_registry=matrix.ablation_registry,
        fixed_families=matrix.fixed_families,
        opponent_splits=matrix.opponent_splits,
        heldout_precommitment=matrix.heldout_precommitment,
        budget_profiles=matrix.budget_profiles,
        contracts=matrix.contracts,
        planned_templates=matrix.planned_templates,
        pairwise_hypotheses=matrix.pairwise_hypotheses,
        holm_family=matrix.holm_family,
    )
    with pytest.raises(ValueError, match="preregistration gate authority"):
        direct.assert_complete_registration()


def test_checkpoint_claim_fields_and_self_hash_cannot_grant_formal_authority() -> None:
    receipt = _matrix().checkpoint_freezes[0]
    receipt._assert_formal_authority  # explicit public audit surface
    with pytest.raises(ValueError, match="formal checkpoint freeze is unavailable"):
        receipt._assert_formal_authority()
    claimed = replace(
        receipt,
        authority_kind="fixed-external-checkpoint-freezer-v1",
        authority_receipt_digest=_h("caller-filled-authority-receipt"),
    )
    assert claimed.digest() != receipt.digest()
    with pytest.raises(ValueError, match="formal checkpoint freeze is unavailable"):
        claimed._assert_formal_authority()


def test_formal_builder_rejects_unequal_equal_compute_and_template_deletion() -> None:
    values = _fixture_parts()
    values.pop("heldout_reveal")
    receipts = list(values["checkpoint_freezes"])
    receipts[0] = replace(receipts[0], offline_compute_budget_digest=_h("unfair-extra-compute"))
    values["checkpoint_freezes"] = tuple(receipts)
    with pytest.raises(ValueError, match="share one compute budget"):
        build_complete_formal_matrix(**values)  # type: ignore[arg-type]

    matrix = _matrix()
    with pytest.raises(ValueError, match="exact complete formal matrix"):
        replace(matrix, planned_templates=matrix.planned_templates[:-1])


def _forged_cell_result(projection, item) -> FormalCellResult:
    count = item.template.paired_block_count
    prefix = item.template.digest()
    observation_digest = _h(f"{prefix}/observation")
    launch_digest = _h(f"{prefix}/supervisor-launch")
    leg_digest = _h(f"{prefix}/supervisor-leg")
    consumption_key = _h(f"{prefix}/supervisor-consumption")
    capture_digest = _h(f"{prefix}/supervisor-capture")
    cleanup_digest = _h(f"{prefix}/supervisor-cleanup")
    wire_semantic_digest = _h(f"{prefix}/supervisor-wire-semantic")
    replay_digest = _h(f"{prefix}/supervisor-replay")
    return FormalCellResult(
        projection_digest=projection.digest(),
        planned_template_digest=prefix,
        evaluation_stratum_digest=item.stratum.digest(),
        seed_cohort_digest=item.template.seed_cohort_digest,
        focal_identity_digest=item.receipt.resolved_focal_identity_digest,
        counterparty_identity_digest=item.receipt.resolved_counterparty_identity_digest,
        formal_plan_digest=_h(f"{prefix}/forged-plan"),
        aggregate_result_digest=_h(f"{prefix}/forged-aggregate"),
        deck_sequence_commitment_digests=tuple(
            _h(f"{item.template.seed_cohort_digest}/deck/{index}")
            for index in range(count)
        ),
        block_plan_digests=tuple(_h(f"{prefix}/block/{index}") for index in range(count)),
        paired_evidence_receipt_digests=tuple(
            _h(f"{prefix}/evidence/{index}") for index in range(count)
        ),
        observation_digests=(observation_digest,),
        execution_receipt_digests=(
            _h(f"{prefix}/execution/0"),
            _h(f"{prefix}/execution/1"),
        ),
        resource_receipt_digests=(
            _h(f"{prefix}/resource/0"),
            _h(f"{prefix}/resource/1"),
        ),
        raw_evidence_digests=(_h(f"{prefix}/raw"),),
        supervisor_contract_digest=_h("fixed-supervisor-contract"),
        supervisor_launch_authorization_digests=(
            launch_digest,
        ),
        supervisor_leg_receipt_digests=(leg_digest,),
        supervisor_receipt_consumption_keys=(
            consumption_key,
        ),
        supervisor_control_session_digests=(
            _h(f"{prefix}/supervisor-control"),
        ),
        supervisor_capture_session_digests=(
            capture_digest,
        ),
        supervisor_socket_identity_digests=(
            _h(f"{prefix}/supervisor-socket/0"),
            _h(f"{prefix}/supervisor-socket/1"),
        ),
        supervisor_wire_semantic_digests=(
            wire_semantic_digest,
        ),
        supervisor_replay_digests=(replay_digest,),
        supervisor_decision_trace_digests=(
            _h(f"{prefix}/supervisor-decision-trace"),
        ),
        supervisor_cleanup_receipt_digests=(
            cleanup_digest,
        ),
        supervisor_observation_bindings=(
            SupervisorObservationBinding(
                observation_digest=observation_digest,
                leg_plan_digest=_h(f"{prefix}/leg-plan"),
                supervisor_readiness_attestation_digest=_h(
                    f"{prefix}/supervisor-readiness"
                ),
                supervisor_attempt_journal_scope_digest=_h(
                    f"{prefix}/attempt-journal-scope"
                ),
                supervisor_attempt_sequence=1,
                supervisor_previous_attempt_entry_digest="0" * 64,
                supervisor_leg_run_id=_h(f"{prefix}/supervisor-leg-run"),
                supervisor_launch_authorization_digest=launch_digest,
                supervisor_leg_receipt_digest=leg_digest,
                supervisor_receipt_consumption_key=consumption_key,
                supervisor_consumption_ledger_entry_digest=_h(
                    f"{prefix}/supervisor-consumption-ledger-entry"
                ),
                supervisor_consumption_ledger_entry_inode=(
                    int(prefix[:12], 16) + 10
                ),
                supervisor_consumption_ledger_entry_path=(
                    f"/var/lib/pok-formal/receipt-ledger-v1/{consumption_key}.json"
                ),
                supervisor_capture_session_digest=capture_digest,
                supervisor_cleanup_receipt_digest=cleanup_digest,
                raw_wire_digest=_h(f"{prefix}/raw-wire"),
                supervisor_wire_semantic_digest=wire_semantic_digest,
                supervisor_replay_digest=replay_digest,
                replay_verification_digest=_h(
                    f"{prefix}/replay-verification"
                ),
            ),
        ),
        supervisor_retry_observation_pairs=(),
        run_ids=(_h(f"{prefix}/run/0"), _h(f"{prefix}/run/1")),
        process_tree_ids=(f"process-{prefix}-0", f"process-{prefix}-1"),
        cgroup_paths=(
            f"/sys/fs/cgroup/pok/{prefix}/0",
            f"/sys/fs/cgroup/pok/{prefix}/1",
        ),
        cgroup_inodes=(
            int(prefix[:12], 16) + 1,
            int(prefix[:12], 16) + 2,
        ),
        focal_score_by_paired_block=(1.0,) * count,
    )


def test_formal_cell_result_rejects_caller_scores_and_random_digests() -> None:
    matrix, reveal = _matrix_and_reveal()
    plan, beacon = _authorized_entropy(matrix)
    projection = materialize_diagnostic_matrix_projection(matrix, plan, beacon, reveal)
    forged = _forged_cell_result(projection, projection.strata[0])
    with pytest.raises(ValueError, match="typed verified paired-block evidence"):
        forged.digest()
    na = tuple(
        NotApplicableAblationReceipt.from_registration(item)
        for item in matrix.ablation_registry.not_applicable
    )
    with pytest.raises(ValueError, match="formal checkpoint freeze is unavailable"):
        build_formal_matrix_result_ledger(
            matrix, projection, (forged,), na, None
        )


def test_formal_cell_factory_accepts_typed_evidence_not_raw_score_vectors() -> None:
    parameters = tuple(
        inspect.signature(FormalCellResult.from_verified_paired_blocks).parameters
    )
    assert parameters == (
        "projection",
        "materialized",
        "matrix",
        "final_entropy_plan",
        "verified_beacon",
        "plan_bridge",
        "plan",
        "paired_blocks",
        "retry_ledger",
    )
    assert not any("score" in parameter or "digest" in parameter for parameter in parameters)


def test_attempt_journal_scope_is_matrix_bound_and_uses_signed_zero_genesis() -> None:
    matrix, reveal = _matrix_and_reveal()
    plan, beacon = _authorized_entropy(matrix)
    projection = materialize_diagnostic_matrix_projection(
        matrix, plan, beacon, reveal
    )
    scope = formal_attempt_journal_scope_digest(matrix, projection)
    assert formal_attempt_journal_genesis_digest(scope) == "0" * 64
    _, different_beacon = _authorized_entropy(matrix, randomness="44" * 32)
    different_projection = materialize_diagnostic_matrix_projection(
        matrix, plan, different_beacon, reveal
    )
    assert scope != formal_attempt_journal_scope_digest(
        matrix,
        different_projection,
    )


def _policy_binding(leg: str, label: str) -> SimpleNamespace:
    return SimpleNamespace(
        leg_plan_digest=leg,
        observation_digest=_h(f"{label}/observation"),
        replay_verification_digest=_h(f"{label}/replay-verification"),
    )


def _policy_row(
    leg: str,
    sequence: int,
    state: str,
    *,
    binding: SimpleNamespace | None = None,
    replay_without_verification: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        leg_plan_digest=leg,
        attempt_sequence=sequence,
        terminal_state=state,
        replay_digest=(
            _h(f"row/{sequence}/unverified-replay")
            if replay_without_verification
            else (_h(f"row/{sequence}/replay") if binding is not None else None)
        ),
        replay_verification_digest=(
            binding.replay_verification_digest if binding is not None else None
        ),
    )


def test_attempt_leg_policy_rejects_unobserved_failure_before_success() -> None:
    leg = _h("attempt-policy/leg")
    completed = _policy_binding(leg, "attempt-policy/completed")
    with pytest.raises(ValueError, match="pre-replay failure lacks independent"):
        validate_attempt_journal_leg_policy(
            entries=(
                _policy_row(leg, 1, "launch_failed"),
                _policy_row(leg, 2, "completed", binding=completed),
            ),
            bindings=(completed,),
            max_retries_by_leg={leg: 2},
            retry_observation_pairs=(),
        )


def test_attempt_leg_policy_rejects_three_failures_before_success() -> None:
    leg = _h("attempt-policy/over-cap-leg")
    completed = _policy_binding(leg, "attempt-policy/over-cap-completed")
    with pytest.raises(ValueError, match="retry cap"):
        validate_attempt_journal_leg_policy(
            entries=(
                _policy_row(leg, 1, "launch_failed"),
                _policy_row(leg, 2, "capture_failed"),
                _policy_row(leg, 3, "infrastructure_failed"),
                _policy_row(leg, 4, "completed", binding=completed),
            ),
            bindings=(completed,),
            max_retries_by_leg={leg: 2},
            retry_observation_pairs=(),
        )


def test_attempt_leg_policy_rejects_aborted_scope() -> None:
    leg = _h("attempt-policy/aborted-leg")
    completed = _policy_binding(leg, "attempt-policy/aborted-completed")
    with pytest.raises(ValueError, match="aborted"):
        validate_attempt_journal_leg_policy(
            entries=(
                _policy_row(leg, 1, "aborted"),
                _policy_row(leg, 2, "completed", binding=completed),
            ),
            bindings=(completed,),
            max_retries_by_leg={leg: 2},
            retry_observation_pairs=(),
        )


def test_attempt_leg_policy_rejects_foreign_leg() -> None:
    retained_leg = _h("attempt-policy/retained-leg")
    foreign_leg = _h("attempt-policy/foreign-leg")
    completed = _policy_binding(retained_leg, "attempt-policy/foreign-completed")
    with pytest.raises(ValueError, match="journal Leg set"):
        validate_attempt_journal_leg_policy(
            entries=(
                _policy_row(foreign_leg, 1, "completed", binding=completed),
            ),
            bindings=(completed,),
            max_retries_by_leg={retained_leg: 2},
            retry_observation_pairs=(),
        )


def test_attempt_leg_policy_rejects_retry_after_completed_result() -> None:
    leg = _h("attempt-policy/post-success-leg")
    completed = _policy_binding(leg, "attempt-policy/post-success-completed")
    with pytest.raises(ValueError, match="end in exactly one completed"):
        validate_attempt_journal_leg_policy(
            entries=(
                _policy_row(leg, 1, "completed", binding=completed),
                _policy_row(leg, 2, "launch_failed"),
            ),
            bindings=(completed,),
            max_retries_by_leg={leg: 2},
            retry_observation_pairs=(),
        )


def test_attempt_leg_policy_requires_verified_retry_edge_for_failed_replay() -> None:
    leg = _h("attempt-policy/observed-retry-leg")
    failed = _policy_binding(leg, "attempt-policy/observed-failure")
    completed = _policy_binding(leg, "attempt-policy/observed-success")
    rows = (
        _policy_row(leg, 1, "infrastructure_failed", binding=failed),
        _policy_row(leg, 2, "completed", binding=completed),
    )
    validate_attempt_journal_leg_policy(
        entries=rows,
        bindings=(failed, completed),
        max_retries_by_leg={leg: 2},
        retry_observation_pairs=(
            (failed.observation_digest, completed.observation_digest),
        ),
    )
    with pytest.raises(ValueError, match="RetryLedger chain"):
        validate_attempt_journal_leg_policy(
            entries=rows,
            bindings=(failed, completed),
            max_retries_by_leg={leg: 2},
            retry_observation_pairs=(),
        )


def test_attempt_leg_policy_rejects_unverified_replay_on_failed_attempt() -> None:
    leg = _h("attempt-policy/unverified-replay-leg")
    completed = _policy_binding(leg, "attempt-policy/unverified-replay-success")
    with pytest.raises(ValueError, match="complete observation evidence"):
        validate_attempt_journal_leg_policy(
            entries=(
                _policy_row(
                    leg,
                    1,
                    "cleanup_failed",
                    replay_without_verification=True,
                ),
                _policy_row(leg, 2, "completed", binding=completed),
            ),
            bindings=(completed,),
            max_retries_by_leg={leg: 2},
            retry_observation_pairs=(),
        )


def test_pairwise_hypotheses_cover_canonical_claims_without_reverse_duplicates() -> None:
    matrix = _matrix()
    assert len(matrix.pairwise_hypotheses) == len(matrix.planned_templates)
    assert set(matrix.holm_family.hypothesis_ids) == {
        item.hypothesis_id for item in matrix.pairwise_hypotheses
    }
    external = [
        item
        for item in matrix.pairwise_hypotheses
        if item.kind == "external-paired-difference"
    ]
    assert external
    assert all(item.right_template_digest is not None for item in external)
    assert all(
        ROUTE_IDS.index(item.left_route) < ROUTE_IDS.index(item.right_route)
        for item in external
        if item.right_route is not None
    )
    oriented = {
        (
            item.left_route,
            item.right_route,
            item.left_template_digest,
            item.right_template_digest,
        )
        for item in external
    }
    assert not any(
        (right, left, right_template, left_template) in oriented
        for left, right, left_template, right_template in oriented
    )


def test_holm_family_is_complete_and_deterministic() -> None:
    family = _matrix().holm_family
    raw_forward = {hypothesis: 0.01 for hypothesis in family.hypothesis_ids}
    raw_reverse = dict(reversed(tuple(raw_forward.items())))
    assert family.adjust(raw_forward) == family.adjust(raw_reverse)
    missing = dict(raw_forward)
    missing.pop(next(iter(missing)))
    with pytest.raises(ValueError, match="exactly cover"):
        family.adjust(missing)


def test_legacy_six_artifact_entry_is_diagnostic_only() -> None:
    legacy = build_legacy_diagnostic_matrix(
        tuple(
            LegacyRouteArtifacts(
                route,
                _artifact(f"{route}-best", f"legacy/{route}/best", action_set="best"),
                _artifact(
                    f"{route}-controlled",
                    f"legacy/{route}/controlled",
                    action_set="controlled",
                ),
            )
            for route in ROUTE_IDS
        ),
        reason="read a historical six-artifact result",
    )
    assert legacy.result_authority == "development_diagnostic_only"
    with pytest.raises(ValueError, match="diagnostic only"):
        legacy.assert_formal_authority()
