from __future__ import annotations

import dataclasses
import copy
import hashlib
import inspect

import pytest

from bots.research_native_lab.common_contracts.evaluation import (
    ArtifactIdentity,
    BlockPlan,
    CandidateBundleManifest,
    DealSequenceCommitment,
    DecisionTelemetry,
    EvaluationStratum,
    ExecutionReceipt,
    FaultAttribution,
    FaultKind,
    FormalEvaluationPlan,
    InfrastructureAttributionReceipt,
    InfrastructureFailureDomain,
    LegPlan,
    MatchObservation,
    PairedBlock,
    PlannedStratumContract,
    ResourceProfile,
    ResourceReceipt,
    ReplayVerificationReceipt,
    RetryLedger,
    RetryLedgerEntry,
    TerminationKind,
    aggregate_blocks,
)
from bots.research_native_lab.common_contracts.deal_generator import (
    DEAL_GENERATOR_ALGORITHM_DIGEST,
)
from bots.research_native_lab.common_contracts.native_replay import (
    NATIVE_REPLAY_VERIFIER_DIGEST,
    verify_native_replay,
)
from bots.research_native_lab.common_contracts.tests.test_native_replay import (
    COMMITMENT as NATIVE_REPLAY_COMMITMENT,
    RAW_WIRE as NATIVE_REPLAY_RAW_WIRE,
    _captured_payload,
    _development_binding,
    _json_bytes,
)


def _h(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _artifact(label: str, key: str, *, action_set: str = "shared-actions") -> ArtifactIdentity:
    return ArtifactIdentity(
        display_label=label,
        sealed_tree_manifest_digest=_h(f"{key}/tree"),
        launch_contract_digest=_h(f"{key}/launch"),
        launch_command_digest=_h(f"{key}/command"),
        base_environment_digest=_h(f"{key}/base-environment"),
        model_digest=_h(f"{key}/model"),
        config_digest=_h(f"{key}/config"),
        action_set_digest=_h(action_set),
        dependency_digest=_h(f"{key}/dependencies"),
        runtime_digest=_h(f"{key}/runtime"),
    )


def _profile(*, budget_ms: int = 5_000) -> ResourceProfile:
    return ResourceProfile(
        cpu_affinity_by_connection=((0, 1), (2, 3)),
        cpu_threads_per_connection=2,
        cpu_quota_us=200_000,
        cpu_period_us=100_000,
        max_tasks_per_connection=8,
        thread_environment_digest=_h("frozen-env"),
        ram_limit_bytes_per_connection=512 * 1024 * 1024,
        swap_limit_bytes_per_connection=0,
        gpu_devices_by_connection=((), ()),
        vram_limit_bytes_per_connection=0,
        decision_budget_ms=budget_ms,
        platform_action_timeout_ms=60_000,
        match_wall_timeout_ms=5_000_000,
        action_send_delay_ms=0,
        enforcer_digest=_h("resource-verifier"),
        cgroup_controllers_digest=_h("cpuset+cpu+memory+pids"),
    )


def _plan(
    *,
    count: int = 2,
    budget_ms: int = 5_000,
    action_a: str = "shared-actions",
    action_b: str = "shared-actions",
    mode: str = "controlled",
    split: str = "direct-h2h",
) -> FormalEvaluationPlan:
    a = _artifact("A", "artifact-a", action_set=action_a)
    b = _artifact("B", "artifact-b", action_set=action_b)
    pair = tuple(sorted((a.identity_digest(), b.identity_digest())))
    profile = _profile(budget_ms=budget_ms)
    generator = DEAL_GENERATOR_ALGORITHM_DIGEST
    stratum = EvaluationStratum(
        identity_pair=pair,
        split=split,
        opponent_family_manifest_digest=_h("direct-A-B-family"),
        rules_digest=_h("national-rules-v1"),
        harness_digest=_h("native-harness-v1"),
        deal_generator_digest=generator,
        resource_profile_digest=profile.digest(),
        time_budget_ms=budget_ms,
        comparison_mode=mode,
        hypothesis_digest=_h("two-sided-zero-margin"),
        multiplicity_family_digest=_h("candidate-pair-x-budget"),
    )
    blocks = []
    for block_index in range(count):
        deal = DealSequenceCommitment.from_root(10_000 + block_index)
        blocks.append(
            BlockPlan.create(
                stratum_digest=stratum.digest(),
                identity_pair=pair,
                block_index=block_index,
                deal_sequence=deal,
                policy_seed_by_identity=(
                    (pair[0], 2_000_000 + block_index * 2),
                    (pair[1], 2_000_001 + block_index * 2),
                ),
            )
        )
    bundle = CandidateBundleManifest(
        scope="development_pair",
        research_candidate_identity_digests=pair,
        opponent_identity_digests=(),
        common_contract_tree_digest=_h("common-contract-tree"),
        opponent_universe_digest=_h("frozen-opponent-universe"),
        resource_profile_digests=(profile.digest(),),
        evaluation_harness_digest=stratum.harness_digest,
        rules_digest=stratum.rules_digest,
        evaluation_contract_digest=_h("evaluation-v1-json"),
        final_randomness_contract_digest=_h("final-randomness-v1-json"),
        planned_strata=(
            PlannedStratumContract(
                stratum=stratum,
                paired_block_count=count,
                stopping_rule_digest=_h("exact-complete-block-list"),
                retry_policy_digest=_h("infra-only-first-clean-max2"),
                analysis_code_digest=_h("evaluation.py"),
                bootstrap_samples=10_000,
                max_infrastructure_retries_per_leg=2,
            ),
        ),
        replay_verifier_digest=_h("replay-verifier"),
        oracle_fixture_digest=_h("official-oracle-fixtures"),
        infrastructure_monitor_digest=_h("independent-infrastructure-monitor"),
    )
    return FormalEvaluationPlan(
        artifacts=(a, b),
        candidate_bundle=bundle,
        stratum=stratum,
        resource_profile=profile,
        blocks=tuple(blocks),
        candidate_freeze_receipt_digest=_h("candidate-freeze-proof"),
        randomness_receipt_digest=_h("verified-future-randomness"),
        stopping_rule_digest=_h("exact-complete-block-list"),
        retry_policy_digest=_h("infra-only-first-clean-max2"),
        analysis_code_digest=_h("evaluation.py"),
        replay_verifier_digest=_h("replay-verifier"),
        oracle_fixture_digest=_h("official-oracle-fixtures"),
        infrastructure_monitor_digest=_h("independent-infrastructure-monitor"),
        bootstrap_seed=777,
        bootstrap_samples=10_000,
        max_infrastructure_retries_per_leg=2,
    )


def _termination_for_fault(kind: FaultKind | None) -> TerminationKind:
    return {
        None: TerminationKind.NORMAL,
        FaultKind.CRASH: TerminationKind.CRASH,
        FaultKind.TIMEOUT: TerminationKind.TIMEOUT,
        FaultKind.ILLEGAL_ACTION: TerminationKind.NORMAL,
        FaultKind.RESOURCE_OVERRUN: TerminationKind.RESOURCE,
        FaultKind.PROTOCOL: TerminationKind.PROTOCOL,
        FaultKind.INFRASTRUCTURE: TerminationKind.INFRASTRUCTURE,
    }[kind]


def _observation(
    plan: FormalEvaluationPlan,
    block_index: int,
    leg_index: int,
    *,
    net_a: int = 1,
    fault_kind: FaultKind | None = None,
    fault_connection: int = 0,
    retry_of: str | None = None,
    attempt: int = 0,
    run_salt: str | None = None,
    actual_seed_override: dict[int, int] | None = None,
) -> MatchObservation:
    block = plan.blocks[block_index]
    leg = LegPlan.from_plan(plan, block, leg_index)
    executions = []
    for connection, identity_digest in enumerate(leg.connection_to_identity):
        artifact = plan.artifact(identity_digest)
        run_component = run_salt if run_salt is not None else f"{block_index}/{leg_index}/{attempt}"
        termination = _termination_for_fault(fault_kind if connection == fault_connection else None)
        actual_policy_seed = (actual_seed_override or {}).get(
            connection, block.policy_seed(identity_digest)
        )
        executions.append(
            ExecutionReceipt(
                leg_plan_digest=leg.digest(),
                identity_digest=identity_digest,
                connection_index=connection,
                launch_contract_digest=artifact.launch_contract_digest,
                launch_command_digest=artifact.launch_command_digest,
                base_environment_digest=artifact.base_environment_digest,
                thread_environment_digest=plan.resource_profile.thread_environment_digest,
                launch_environment_digest=_h(
                    f"effective-env/{identity_digest}/{actual_policy_seed}"
                ),
                actual_policy_seed=actual_policy_seed,
                run_id=_h(f"run/{run_component}/{connection}"),
                process_tree_id=f"process-tree-{run_component}-{connection}",
                cgroup_path=f"/sys/fs/cgroup/pok/{run_component}/{connection}",
                issuer_digest=plan.stratum.harness_digest,
                verifier_digest=plan.stratum.harness_digest,
                raw_evidence_digest=_h(f"exec-raw/{run_component}/{connection}"),
                termination_kind=termination,
                termination_evidence_digest=_h(
                    f"termination/{run_component}/{connection}/{termination.value}"
                ),
                exit_code=0 if termination is TerminationKind.NORMAL else -9,
            )
        )
    executions = tuple(executions)

    resources = []
    match_start_epoch_ms = (
        1_800_000_000_000
        + block_index * 100_000
        + leg_index * 20_000
        + attempt * 7_000
    )
    for connection, execution in enumerate(executions):
        overrun = fault_kind is FaultKind.RESOURCE_OVERRUN and connection == fault_connection
        resources.append(
            ResourceReceipt(
                execution_receipt_digest=execution.digest(),
                profile_digest=plan.resource_profile.digest(),
                connection_index=connection,
                identity_digest=execution.identity_digest,
                cgroup_path=execution.cgroup_path,
                cgroup_inode=int(_h(f"cgroup-inode/{run_component}/{connection}")[:12], 16) + 1,
                controllers_digest=_h("cpuset+cpu+memory+pids"),
                enforcer_digest=plan.resource_profile.enforcer_digest,
                cpu_affinity=plan.resource_profile.cpu_affinity_by_connection[connection],
                cpu_quota_us=plan.resource_profile.cpu_quota_us,
                cpu_period_us=plan.resource_profile.cpu_period_us,
                max_tasks_limit=plan.resource_profile.max_tasks_per_connection,
                memory_limit_bytes=plan.resource_profile.ram_limit_bytes_per_connection,
                swap_limit_bytes=0,
                gpu_devices=(),
                vram_limit_bytes=0,
                observed_max_tasks=2,
                observed_peak_rss_bytes=(
                    plan.resource_profile.ram_limit_bytes_per_connection + 1
                    if overrun
                    else 64 * 1024 * 1024
                ),
                observed_peak_swap_bytes=0,
                observed_peak_vram_bytes=0,
                oom_kill_count=int(overrun),
                pids_limit_hit_count=0,
                deadline_kill_count=int(
                    fault_kind is FaultKind.TIMEOUT and connection == fault_connection
                ),
                cpu_throttled_usec=0,
                started_epoch_ms=match_start_epoch_ms,
                finished_epoch_ms=match_start_epoch_ms + 5_000,
                raw_evidence_digest=_h(f"resource-raw/{run_component}/{connection}"),
                verifier_digest=_h("resource-verifier"),
                thermal_event=False,
                host_preemption_event=False,
            )
        )
    resources = tuple(resources)

    event = _h(f"event/{block_index}/{leg_index}/{attempt}/{fault_kind}")
    terminal_fault = None
    if fault_kind is not None:
        owner = (
            "infrastructure"
            if fault_kind is FaultKind.INFRASTRUCTURE
            else leg.connection_to_identity[fault_connection]
        )
        if fault_kind is FaultKind.RESOURCE_OVERRUN:
            evidence = resources[fault_connection].raw_evidence_digest
        elif fault_kind in {FaultKind.CRASH, FaultKind.PROTOCOL}:
            evidence = executions[fault_connection].termination_evidence_digest
        else:
            evidence = event
        terminal_fault = FaultAttribution(
            owner=owner,
            kind=fault_kind,
            evidence_digest=evidence,
            incident_digest=_h(f"incident/{block_index}/{leg_index}/{attempt}"),
            hand_number=10,
            decision_index=3,
        )

    hands_started = 70 if fault_kind is None else 10
    hands_played = 70 if fault_kind is None else 9
    a_digest = next(item.identity_digest() for item in plan.artifacts if item.display_label == "A")
    net_connection0 = net_a if leg.connection_to_identity[0] == a_digest else -net_a
    decision_wait_ns = (tuple([1_000_000] * 10), tuple([1_000_000] * 10))
    decision_trace_digests = tuple(
        _h(f"telemetry/{run_component}/{connection}")
        for connection in range(2)
    )
    telemetry = tuple(
        DecisionTelemetry(
            decisions=10,
            p50_latency_ms=1.0,
            p95_latency_ms=1.0,
            p99_latency_ms=1.0,
            search_nodes=100,
            fallback_decisions=0,
            trace_digest=decision_trace_digests[connection],
        )
        for connection in range(2)
    )
    timeout_counts = (
        (1 if fault_kind is FaultKind.TIMEOUT and fault_connection == 0 else 0),
        (1 if fault_kind is FaultKind.TIMEOUT and fault_connection == 1 else 0),
    )
    illegal_counts = (
        (1 if fault_kind is FaultKind.ILLEGAL_ACTION and fault_connection == 0 else 0),
        (1 if fault_kind is FaultKind.ILLEGAL_ACTION and fault_connection == 1 else 0),
    )
    replay_receipt = ReplayVerificationReceipt(
        leg_plan_digest=leg.digest(),
        execution_binding_digest=_h(f"execution-binding/{run_component}"),
        execution_binding_authority="development_diagnostic_only",
        connection_identity_digests=leg.connection_to_identity,
        run_ids_by_connection=tuple(item.run_id for item in executions),
        process_tree_ids_by_connection=tuple(
            item.process_tree_id for item in executions
        ),
        cgroup_paths_by_connection=tuple(item.cgroup_path for item in executions),
        resource_profile_digest=plan.resource_profile.digest(),
        decision_budget_ms=plan.resource_profile.decision_budget_ms,
        platform_action_timeout_ms=plan.resource_profile.platform_action_timeout_ms,
        action_send_delay_ms=plan.resource_profile.action_send_delay_ms,
        verifier_digest=plan.replay_verifier_digest,
        issuer_digest=plan.stratum.harness_digest,
        rules_digest=plan.stratum.rules_digest,
        oracle_fixture_digest=plan.oracle_fixture_digest,
        raw_wire_digest=_h(f"wire/{run_component}"),
        wire_semantics_verified=False,
        wire_semantic_binding_digest=_h(f"wire-semantics/{run_component}"),
        raw_replay_digest=_h(f"replay/{run_component}"),
        match_trace_digest=_h(f"replay/{run_component}"),
        verification_evidence_digest=_h(f"replay-verification/{run_component}"),
        actual_dealt_prefix_digests=block.deal_sequence.hand_deal_digests[:hands_started],
        verified_event_digests=(event,),
        hands_started=hands_started,
        hands_played=hands_played,
        settlement_count=hands_played,
        net_chips_connection0=net_connection0,
        timeout_count_by_connection=timeout_counts,
        illegal_action_count_by_connection=illegal_counts,
        decision_wait_ns_by_connection=decision_wait_ns,
        search_nodes_by_connection=(100, 100),
        fallback_decisions_by_connection=(0, 0),
        decision_trace_digest_by_connection=decision_trace_digests,
        telemetry_complete_by_connection=(True, True),
        adjudicated_fault=terminal_fault,
        result_finalized_epoch_ms=(
            match_start_epoch_ms + 4_000 if hands_played == 70 else None
        ),
        hand70_evidence_digest=(_h(f"hand70/{run_component}") if hands_played == 70 else None),
    )
    return MatchObservation(
        leg_plan=leg,
        execution_receipts=executions,
        resource_receipts=resources,
        replay_receipt=replay_receipt,
        actual_dealt_prefix_digests=block.deal_sequence.hand_deal_digests[:hands_started],
        actual_replay_digest=_h(f"replay/{run_component}"),
        match_trace_digest=_h(f"replay/{run_component}"),
        verified_event_digests=(event,),
        hands_started=hands_started,
        hands_played=hands_played,
        net_chips_connection0=net_connection0,
        match_wall_elapsed_ms=5_000,
        telemetry_by_connection=telemetry,
        timeout_count_by_connection=timeout_counts,
        illegal_action_count_by_connection=illegal_counts,
        terminal_fault=terminal_fault,
        retry_of_observation_digest=retry_of,
    )


def _paired(plan: FormalEvaluationPlan, block_index: int, *, net_a: int = 1) -> PairedBlock:
    return PairedBlock(
        _observation(plan, block_index, 0, net_a=net_a),
        _observation(plan, block_index, 1, net_a=net_a),
    )


def test_display_label_is_not_content_identity() -> None:
    original = _artifact("A", "same")
    alias = dataclasses.replace(original, display_label="renamed")
    assert original.identity_digest() == alias.identity_digest()
    plan = _plan(count=1)
    a, b = plan.artifacts
    with pytest.raises(ValueError, match="masquerade"):
        dataclasses.replace(
            plan,
            artifacts=(dataclasses.replace(a, display_label=b.identity_digest()), b),
        )


def test_aggregate_evidence_digest_ignores_display_rename() -> None:
    plan = _plan(count=1)
    paired = _paired(plan, 0)
    original = aggregate_blocks((paired,), "A", plan=plan)
    renamed_artifacts = tuple(
        dataclasses.replace(item, display_label="renamed-A")
        if item.display_label == "A"
        else item
        for item in plan.artifacts
    )
    renamed_plan = dataclasses.replace(plan, artifacts=renamed_artifacts)
    renamed = aggregate_blocks((paired,), "renamed-A", plan=renamed_plan)
    assert original.formal_plan_digest == renamed.formal_plan_digest
    assert original.digest() == renamed.digest()


def test_resource_profile_separates_54s_compute_and_exact_60s_platform_deadline() -> None:
    with pytest.raises(ValueError, match="54"):
        _profile(budget_ms=54_001)
    with pytest.raises(ValueError, match="exactly 60"):
        dataclasses.replace(_profile(), platform_action_timeout_ms=59_999)
    with pytest.raises(ValueError, match="sequentially"):
        dataclasses.replace(_profile(), concurrent_matches=True)
    with pytest.raises(ValueError, match="disjoint"):
        dataclasses.replace(_profile(), cpu_affinity_by_connection=((0, 1), (1, 2)))


def test_formal_strength_authority_enforces_preregistered_sample_floors() -> None:
    with pytest.raises(ValueError, match="at least 400"):
        dataclasses.replace(_plan(count=1), result_authority="formal_strength")
    with pytest.raises(ValueError, match="legacy candidate-bundle"):
        dataclasses.replace(_plan(count=400), result_authority="formal_strength")
    with pytest.raises(ValueError, match="unregistered time budget"):
        dataclasses.replace(
            _plan(count=400, budget_ms=1_234), result_authority="formal_strength"
        )


def test_replay_receipt_is_bound_to_exact_leg_and_cannot_promote_dev_capture() -> None:
    plan = _plan(count=1)
    leg = LegPlan.from_plan(plan, plan.blocks[0], 0)
    binding = _development_binding(
        leg_plan_digest=leg.digest(),
        connection_identity_digests=leg.connection_to_identity,
        run_ids_by_connection=(_h("replay-run-0"), _h("replay-run-1")),
        process_tree_ids_by_connection=("replay-process-0", "replay-process-1"),
        cgroup_paths_by_connection=(
            "/sys/fs/cgroup/pok/replay/0",
            "/sys/fs/cgroup/pok/replay/1",
        ),
        resource_profile_digest=plan.resource_profile.digest(),
        decision_budget_ms=plan.resource_profile.decision_budget_ms,
        platform_action_timeout_ms=plan.resource_profile.platform_action_timeout_ms,
        action_send_delay_ms=plan.resource_profile.action_send_delay_ms,
        raw_wire=NATIVE_REPLAY_RAW_WIRE,
    )
    raw = _json_bytes(_captured_payload(binding=binding))
    verified = verify_native_replay(
        raw,
        NATIVE_REPLAY_COMMITMENT,
        execution_binding=binding,
        raw_wire=NATIVE_REPLAY_RAW_WIRE,
    )
    receipt = ReplayVerificationReceipt.from_verified_native_replay(
        leg_plan=leg,
        verified_replay=verified,
        issuer_digest=plan.stratum.harness_digest,
        rules_digest=plan.stratum.rules_digest,
        oracle_fixture_digest=plan.oracle_fixture_digest,
        adjudicated_fault=None,
    )
    assert receipt.verifier_digest == NATIVE_REPLAY_VERIFIER_DIGEST
    assert receipt.raw_replay_digest == verified.raw_replay_digest
    assert receipt.raw_wire_digest == hashlib.sha256(NATIVE_REPLAY_RAW_WIRE).hexdigest()
    assert receipt.result_finalized_epoch_ms == verified.result_finalized_epoch_ms
    assert receipt.connection_identity_digests == leg.connection_to_identity
    assert receipt.hands_played == 70
    with pytest.raises(ValueError, match="enforcer-issued"):
        receipt._assert_formal_verifier_authority()
    with pytest.raises(ValueError, match="development replay receipt"):
        dataclasses.replace(
            receipt,
            supervisor_contract_digest=_h("forged-supervisor-contract"),
            attestation_digest=None,
        )
    with pytest.raises(ValueError, match="lacks signed supervisor fields"):
        dataclasses.replace(
            receipt,
            execution_binding_authority="formal_enforcer_bound",
            attestation_digest=None,
        )

    copied = dataclasses.replace(receipt)
    with pytest.raises(ValueError, match="pinned native replay verifier"):
        copied._assert_formal_verifier_authority()

    shallow = copy.copy(receipt)
    with pytest.raises(ValueError, match="copied/altered"):
        shallow._assert_formal_verifier_authority()

    copied_verified = copy.copy(verified)
    with pytest.raises(TypeError, match="copied or forged"):
        ReplayVerificationReceipt.from_verified_native_replay(
            leg_plan=leg,
            verified_replay=copied_verified,
            issuer_digest=plan.stratum.harness_digest,
            rules_digest=plan.stratum.rules_digest,
            oracle_fixture_digest=plan.oracle_fixture_digest,
            adjudicated_fault=None,
        )

    other_leg = dataclasses.replace(
        leg,
        leg_index=1,
        connection_to_identity=tuple(reversed(leg.connection_to_identity)),
    )
    with pytest.raises(ValueError, match="different LegPlan"):
        ReplayVerificationReceipt.from_verified_native_replay(
            leg_plan=other_leg,
            verified_replay=verified,
            issuer_digest=plan.stratum.harness_digest,
            rules_digest=plan.stratum.rules_digest,
            oracle_fixture_digest=plan.oracle_fixture_digest,
            adjudicated_fault=None,
        )


def test_happy_path_aggregates_exact_preregistered_blocks_and_binds_evidence() -> None:
    plan = _plan(count=3)
    blocks = [_paired(plan, index) for index in range(3)]
    result = aggregate_blocks(reversed(blocks), "A", plan=plan)
    assert result.wins == 6 and result.draws == 0 and result.losses == 0
    assert result.match_score == 1.0 and result.paired_blocks == 3
    assert result.formal_plan_digest == plan.digest()
    assert result.stratum_digest == plan.stratum.digest()
    assert result.candidate_identity_digest == next(
        item.identity_digest() for item in plan.artifacts if item.display_label == "A"
    )
    assert result.bootstrap_ci95_low == result.bootstrap_ci95_high == 1.0
    assert result.ci95_low < 1.0 and result.digest()


def test_aggregate_rejects_duplicate_missing_extra_and_cross_stratum_blocks() -> None:
    plan = _plan(count=2)
    first = _paired(plan, 0)
    second = _paired(plan, 1)
    with pytest.raises(ValueError, match="duplicate"):
        aggregate_blocks((first, first), "A", plan=plan)
    with pytest.raises(ValueError, match="preregistration"):
        aggregate_blocks((first,), "A", plan=plan)
    other_plan = _plan(count=2, budget_ms=20_000)
    with pytest.raises(ValueError, match="preregistration|different formal plan"):
        aggregate_blocks((first, _paired(other_plan, 1)), "A", plan=plan)
    # The valid set remains accepted and input order is canonicalized.
    assert aggregate_blocks((second, first), "A", plan=plan).digest() == aggregate_blocks(
        (first, second), "A", plan=plan
    ).digest()


def test_candidate_bundle_binds_exact_pre_beacon_stratum_parameters() -> None:
    plan = _plan(count=2)
    with pytest.raises(ValueError, match="timestamped stratum contract"):
        dataclasses.replace(plan, stopping_rule_digest=_h("post-beacon-new-stopping-rule"))
    with pytest.raises(ValueError, match="timestamped stratum contract"):
        dataclasses.replace(plan, bootstrap_samples=20_000)
    with pytest.raises(ValueError, match="timestamped stratum contract"):
        dataclasses.replace(plan, blocks=(plan.blocks[0],))

    third = _artifact("C", "artifact-c").identity_digest()
    opponent = _artifact("pool", "pool-opponent").identity_digest()
    formal_bundle = CandidateBundleManifest(
        scope="formal_three_candidate_matrix",
        research_candidate_identity_digests=(
            *plan.candidate_bundle.research_candidate_identity_digests,
            third,
        ),
        opponent_identity_digests=(opponent,),
        common_contract_tree_digest=plan.candidate_bundle.common_contract_tree_digest,
        opponent_universe_digest=plan.candidate_bundle.opponent_universe_digest,
        resource_profile_digests=plan.candidate_bundle.resource_profile_digests,
        evaluation_harness_digest=plan.candidate_bundle.evaluation_harness_digest,
        rules_digest=plan.candidate_bundle.rules_digest,
        evaluation_contract_digest=plan.candidate_bundle.evaluation_contract_digest,
        final_randomness_contract_digest=plan.candidate_bundle.final_randomness_contract_digest,
        planned_strata=plan.candidate_bundle.planned_strata,
        replay_verifier_digest=plan.candidate_bundle.replay_verifier_digest,
        oracle_fixture_digest=plan.candidate_bundle.oracle_fixture_digest,
        infrastructure_monitor_digest=plan.candidate_bundle.infrastructure_monitor_digest,
    )
    with pytest.raises(ValueError, match="complete formal matrix gate"):
        formal_bundle._assert_formal_matrix()
    copied = dataclasses.replace(formal_bundle, opponent_universe_digest=_h("changed-universe"))
    with pytest.raises(ValueError, match="complete formal matrix gate"):
        copied._assert_formal_matrix()


def test_plan_rejects_controlled_action_drift_and_overlapping_70_hand_seed_windows() -> None:
    with pytest.raises(ValueError, match="action set"):
        _plan(action_b="different-actions")
    assert _plan(action_b="different-actions", mode="best-of-route")

    plan = _plan(count=2)
    with pytest.raises(ValueError, match="root derivation"):
        dataclasses.replace(
            plan.blocks[1].deal_sequence,
            hand_seeds=plan.blocks[0].deal_sequence.hand_seeds,
        )

    repeated_root_deal = plan.blocks[0].deal_sequence
    repeated_root_block = BlockPlan.create(
        stratum_digest=plan.stratum.digest(),
        identity_pair=plan.stratum.identity_pair,
        block_index=1,
        deal_sequence=repeated_root_deal,
        policy_seed_by_identity=plan.blocks[1].policy_seed_by_identity,
    )
    with pytest.raises(ValueError, match="root seed repeats"):
        dataclasses.replace(plan, blocks=(plan.blocks[0], repeated_root_block))

    repeated_policy_block = BlockPlan.create(
        stratum_digest=plan.stratum.digest(),
        identity_pair=plan.stratum.identity_pair,
        block_index=1,
        deal_sequence=plan.blocks[1].deal_sequence,
        policy_seed_by_identity=plan.blocks[0].policy_seed_by_identity,
    )
    with pytest.raises(ValueError, match="policy RNG stream repeats"):
        dataclasses.replace(plan, blocks=(plan.blocks[0], repeated_policy_block))


def test_actual_deals_policy_seed_and_per_bot_receipts_are_verified_against_plan() -> None:
    plan = _plan(count=1)
    observation = _observation(plan, 0, 0)
    with pytest.raises(ValueError, match="replay-verifier receipt"):
        dataclasses.replace(
            observation,
            actual_dealt_prefix_digests=(_h("wrong-deal"),)
            + observation.actual_dealt_prefix_digests[1:],
        )

    bad_seed = _observation(plan, 0, 0, actual_seed_override={0: 123})
    with pytest.raises(ValueError, match="policy RNG seed"):
        bad_seed.verify_against_plan(plan)

    unplanned = _observation(plan, 0, 0)
    unplanned_execs = list(unplanned.execution_receipts)
    unplanned_execs[0] = dataclasses.replace(
        unplanned_execs[0], launch_command_digest=_h("unplanned-command")
    )
    unplanned_resources = list(unplanned.resource_receipts)
    unplanned_resources[0] = dataclasses.replace(
        unplanned_resources[0],
        execution_receipt_digest=unplanned_execs[0].digest(),
    )
    unplanned = dataclasses.replace(
        unplanned,
        execution_receipts=tuple(unplanned_execs),
        resource_receipts=tuple(unplanned_resources),
    )
    with pytest.raises(ValueError, match="command differs"):
        unplanned.verify_against_plan(plan)

    with pytest.raises(ValueError, match="reused"):
        PairedBlock(
            _observation(plan, 0, 0, run_salt="shared-runs"),
            _observation(plan, 0, 1, run_salt="shared-runs"),
        )


def test_fault_evidence_is_bijective_and_candidate_fault_is_a_loss() -> None:
    plan = _plan(count=1)
    timeout = _observation(plan, 0, 0, fault_kind=FaultKind.TIMEOUT, fault_connection=0)
    timeout.verify_against_plan(plan)
    failed_identity = timeout.leg_plan.connection_to_identity[0]
    other_identity = timeout.leg_plan.connection_to_identity[1]
    assert timeout.candidate_score(failed_identity) == 0.0
    assert timeout.candidate_score(other_identity) == 1.0

    with pytest.raises(ValueError, match="replay-verifier receipt"):
        dataclasses.replace(timeout, timeout_count_by_connection=(0, 0))

    resource_fault = _observation(
        plan, 0, 0, fault_kind=FaultKind.RESOURCE_OVERRUN, fault_connection=0
    )
    resource_fault.verify_against_plan(plan)
    clean_resources = list(resource_fault.resource_receipts)
    clean_resources[0] = dataclasses.replace(
        clean_resources[0],
        observed_peak_rss_bytes=1,
        oom_kill_count=0,
    )
    forged = dataclasses.replace(resource_fault, resource_receipts=tuple(clean_resources))
    with pytest.raises(ValueError, match="requires measured"):
        forged.verify_against_plan(plan)

    clean = _observation(plan, 0, 0)
    post_result_timeout = FaultAttribution(
        owner=clean.leg_plan.connection_to_identity[0],
        kind=FaultKind.TIMEOUT,
        evidence_digest=clean.verified_event_digests[0],
        incident_digest=_h("post-result-timeout"),
        hand_number=70,
        decision_index=3,
    )
    with pytest.raises(ValueError, match="replay-verifier receipt"):
        dataclasses.replace(
            clean,
            terminal_fault=post_result_timeout,
            timeout_count_by_connection=(1, 0),
        )
    with pytest.raises(ValueError, match="normal execution"):
        bad_executions = list(clean.execution_receipts)
        bad_executions[0] = dataclasses.replace(bad_executions[0], exit_code=-9)
        dataclasses.replace(clean, execution_receipts=tuple(bad_executions))

    crash = _observation(plan, 0, 0, fault_kind=FaultKind.CRASH)
    with pytest.raises(ValueError, match="replay-verifier receipt"):
        dataclasses.replace(
            crash,
            terminal_fault=dataclasses.replace(crash.terminal_fault, hand_number=70),
        )
    with pytest.raises(ValueError, match="settlements must equal"):
        dataclasses.replace(
            crash.replay_receipt,
            settlement_count=0,
            attestation_digest=None,
        )
    with pytest.raises(ValueError, match="incomplete match cannot contain hand-70"):
        dataclasses.replace(
            crash.replay_receipt,
            hand70_evidence_digest=_h("impossible-hand70"),
            attestation_digest=None,
        )


def test_infrastructure_retry_requires_retained_exact_lineage_and_first_clean_result() -> None:
    plan = _plan(count=1)
    original = _observation(plan, 0, 0, fault_kind=FaultKind.INFRASTRUCTURE, attempt=0)
    retry = _observation(
        plan,
        0,
        0,
        retry_of=original.observation_digest(),
        attempt=1,
    )
    entry = RetryLedgerEntry(
        original=original,
        retry=retry,
        infrastructure_attribution=InfrastructureAttributionReceipt.create(
            original_observation_digest=original.observation_digest(),
            incident_digest=original.terminal_fault.incident_digest,
            monitor_digest=plan.infrastructure_monitor_digest,
            raw_monitor_evidence_digest=_h("independent-host-audit"),
            verifier_digest=plan.infrastructure_monitor_digest,
            failure_domain=InfrastructureFailureDomain.HARNESS,
            fault_epoch_ms=original.resource_receipts[0].started_epoch_ms + 2_000,
            affected_run_ids=(original.execution_receipts[0].run_id,),
            result_was_unavailable=True,
        ),
    )
    ledger = RetryLedger((entry,))
    paired = PairedBlock(retry, _observation(plan, 0, 1))
    result = aggregate_blocks((paired,), "A", plan=plan, retry_ledger=ledger)
    assert result.paired_blocks == 1 and result.retry_ledger_digest == ledger.digest()

    class _FormalPlanStub:
        result_authority = "formal_strength"

    with pytest.raises(ValueError, match="external signed"):
        entry.infrastructure_attribution.verify(original, _FormalPlanStub())  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="retained ledger"):
        aggregate_blocks((paired,), "A", plan=plan)
    with pytest.raises(ValueError, match="unused branch"):
        aggregate_blocks((_paired(plan, 0),), "A", plan=plan, retry_ledger=ledger)

    thermal_attribution = InfrastructureAttributionReceipt.create(
        original_observation_digest=original.observation_digest(),
        incident_digest=original.terminal_fault.incident_digest,
        monitor_digest=plan.infrastructure_monitor_digest,
        raw_monitor_evidence_digest=_h("thermal-monitor-without-resource-marker"),
        verifier_digest=plan.infrastructure_monitor_digest,
        failure_domain=InfrastructureFailureDomain.THERMAL,
        fault_epoch_ms=original.resource_receipts[0].started_epoch_ms + 2_000,
        affected_run_ids=(original.execution_receipts[0].run_id,),
        result_was_unavailable=True,
    )
    with pytest.raises(ValueError, match="lacks a thermal resource event"):
        thermal_attribution.verify(original, plan)

    candidate_failure = _observation(plan, 0, 0, fault_kind=FaultKind.CRASH)
    candidate_retry = _observation(
        plan,
        0,
        0,
        retry_of=candidate_failure.observation_digest(),
        attempt=2,
    )
    with pytest.raises(ValueError, match="only an independently attributed infrastructure"):
        RetryLedgerEntry(
            original=candidate_failure,
            retry=candidate_retry,
            infrastructure_attribution=entry.infrastructure_attribution,
        )

    changed_launch_retry = _observation(
        plan,
        0,
        0,
        retry_of=original.observation_digest(),
        attempt=2,
    )
    changed_execs = list(changed_launch_retry.execution_receipts)
    changed_execs[0] = dataclasses.replace(
        changed_execs[0], launch_command_digest=_h("changed-command")
    )
    changed_resources = list(changed_launch_retry.resource_receipts)
    changed_resources[0] = dataclasses.replace(
        changed_resources[0],
        execution_receipt_digest=changed_execs[0].digest(),
    )
    changed_launch_retry = dataclasses.replace(
        changed_launch_retry,
        execution_receipts=tuple(changed_execs),
        resource_receipts=tuple(changed_resources),
    )
    with pytest.raises(ValueError, match="changed the immutable leg or launch inputs"):
        RetryLedgerEntry(
            original=original,
            retry=changed_launch_retry,
            infrastructure_attribution=entry.infrastructure_attribution,
        )


def test_formal_infrastructure_factory_accepts_only_external_capabilities_not_incident_claims() -> None:
    parameters = tuple(
        inspect.signature(
            InfrastructureAttributionReceipt.from_authorized_supervisor_failure
        ).parameters
    )
    assert parameters == (
        "plan",
        "original_observation",
        "authorized_supervisor_leg",
        "authorized_attempt_journal",
    )
    assert not {
        "incident_digest",
        "failure_domain",
        "fault_epoch_ms",
        "affected_run_ids",
        "raw_monitor_evidence_digest",
    } & set(parameters)

    plan = _plan(count=1)
    observation = _observation(
        plan, 0, 0, fault_kind=FaultKind.INFRASTRUCTURE
    )
    with pytest.raises(ValueError, match="diagnostic plan"):
        InfrastructureAttributionReceipt.from_authorized_supervisor_failure(
            plan=plan,
            original_observation=observation,
            authorized_supervisor_leg=object(),
            authorized_attempt_journal=object(),
        )

    # Merely changing the public authority label on a typed plan never creates
    # its matrix/future-entropy process-local capability.
    forged_plan = copy.copy(plan)
    object.__setattr__(forged_plan, "result_authority", "formal_strength")
    with pytest.raises(ValueError, match="copied, forged, or altered"):
        InfrastructureAttributionReceipt.from_authorized_supervisor_failure(
            plan=forged_plan,
            original_observation=observation,
            authorized_supervisor_leg=object(),
            authorized_attempt_journal=object(),
        )

    diagnostic = InfrastructureAttributionReceipt.create(
        original_observation_digest=observation.observation_digest(),
        incident_digest=observation.terminal_fault.incident_digest,
        monitor_digest=plan.infrastructure_monitor_digest,
        raw_monitor_evidence_digest=_h("diagnostic-monitor-only"),
        verifier_digest=plan.infrastructure_monitor_digest,
        failure_domain=InfrastructureFailureDomain.HARNESS,
        fault_epoch_ms=observation.resource_receipts[0].finished_epoch_ms,
        affected_run_ids=(observation.execution_receipts[0].run_id,),
        result_was_unavailable=True,
    )
    with pytest.raises(ValueError, match="typed supervisor/journal binding"):
        dataclasses.replace(
            diagnostic,
            authority="formal_signed_supervisor_and_closed_journal",
            attestation_digest=diagnostic.attestation_digest,
        )


def test_completed_result_cannot_be_relabelled_as_infrastructure_and_retried() -> None:
    plan = _plan(count=1)
    clean = _observation(plan, 0, 0)
    fake_fault = FaultAttribution(
        owner="infrastructure",
        kind=FaultKind.INFRASTRUCTURE,
        evidence_digest=clean.verified_event_digests[0],
        incident_digest=_h("post-result-incident"),
        hand_number=70,
        decision_index=None,
    )
    with pytest.raises(ValueError, match="replay-verifier receipt"):
        dataclasses.replace(clean, terminal_fault=fake_fault)


def test_retry_lineage_execution_and_cgroup_ids_are_globally_unique() -> None:
    plan = _plan(count=2)
    originals = [
        _observation(
            plan,
            block_index,
            0,
            fault_kind=FaultKind.INFRASTRUCTURE,
            run_salt="reused-original-runtime",
        )
        for block_index in range(2)
    ]
    retries = [
        _observation(
            plan,
            block_index,
            0,
            retry_of=originals[block_index].observation_digest(),
            attempt=1,
        )
        for block_index in range(2)
    ]
    entries = []
    for block_index, (original, retry) in enumerate(zip(originals, retries, strict=True)):
        entries.append(
            RetryLedgerEntry(
                original=original,
                retry=retry,
                infrastructure_attribution=InfrastructureAttributionReceipt.create(
                    original_observation_digest=original.observation_digest(),
                    incident_digest=original.terminal_fault.incident_digest,
                    monitor_digest=plan.infrastructure_monitor_digest,
                    raw_monitor_evidence_digest=_h(f"monitor/{block_index}"),
                    verifier_digest=plan.infrastructure_monitor_digest,
                    failure_domain=InfrastructureFailureDomain.HARNESS,
                    fault_epoch_ms=original.resource_receipts[0].started_epoch_ms + 2_000,
                    affected_run_ids=(original.execution_receipts[0].run_id,),
                    result_was_unavailable=True,
                ),
            )
        )
    paired = tuple(
        PairedBlock(retries[index], _observation(plan, index, 1)) for index in range(2)
    )
    with pytest.raises(ValueError, match="reused an execution receipt/run|reused a process tree"):
        aggregate_blocks(paired, "A", plan=plan, retry_ledger=RetryLedger(tuple(entries)))


def test_replay_raw_evidence_is_unique_across_formal_observations() -> None:
    plan = _plan(count=2)
    first = _paired(plan, 0)
    second_first = _observation(plan, 1, 0)
    source = first.first.replay_receipt
    reused_receipt = dataclasses.replace(
        second_first.replay_receipt,
        raw_wire_digest=source.raw_wire_digest,
        raw_replay_digest=source.raw_replay_digest,
        match_trace_digest=source.match_trace_digest,
        verification_evidence_digest=source.verification_evidence_digest,
        hand70_evidence_digest=source.hand70_evidence_digest,
        attestation_digest=None,
    )
    second_first = dataclasses.replace(
        second_first,
        replay_receipt=reused_receipt,
        actual_replay_digest=reused_receipt.raw_replay_digest,
        match_trace_digest=reused_receipt.match_trace_digest,
    )
    second = PairedBlock(second_first, _observation(plan, 1, 1))
    with pytest.raises(ValueError, match="reused raw (wire|replay)"):
        aggregate_blocks((first, second), "A", plan=plan)


def test_canonical_raw_replay_and_match_trace_alias_is_domain_legal() -> None:
    plan = _plan(count=1)
    paired = _paired(plan, 0)
    receipt = dataclasses.replace(
        paired.first.replay_receipt,
        raw_replay_digest=paired.first.replay_receipt.match_trace_digest,
        attestation_digest=None,
    )
    first = dataclasses.replace(
        paired.first,
        replay_receipt=receipt,
        actual_replay_digest=receipt.raw_replay_digest,
        match_trace_digest=receipt.match_trace_digest,
    )
    result = aggregate_blocks(
        (PairedBlock(first, paired.swapped),),
        "A",
        plan=plan,
    )
    assert result.paired_blocks == 1


def test_formal_observations_cannot_run_concurrently() -> None:
    plan = _plan(count=2)
    first = _paired(plan, 0)
    overlapping = _observation(plan, 1, 0)
    source_start = first.first.resource_receipts[0].started_epoch_ms
    changed_resources = tuple(
        dataclasses.replace(
            resource,
            started_epoch_ms=source_start,
            finished_epoch_ms=source_start + 5_000,
        )
        for resource in overlapping.resource_receipts
    )
    changed_replay = dataclasses.replace(
        overlapping.replay_receipt,
        result_finalized_epoch_ms=source_start + 4_000,
        attestation_digest=None,
    )
    overlapping = dataclasses.replace(
        overlapping,
        resource_receipts=changed_resources,
        replay_receipt=changed_replay,
    )
    second = PairedBlock(overlapping, _observation(plan, 1, 1))
    with pytest.raises(ValueError, match="overlap despite concurrent_matches=1"):
        aggregate_blocks((first, second), "A", plan=plan)


def test_arbitrary_retry_digest_and_unresolved_infrastructure_fail_closed() -> None:
    plan = _plan(count=1)
    forged_retry = _observation(plan, 0, 0, retry_of=_h("invented-original"))
    with pytest.raises(ValueError, match="retained ledger"):
        aggregate_blocks(
            (PairedBlock(forged_retry, _observation(plan, 0, 1)),),
            "A",
            plan=plan,
        )

    infrastructure = _observation(plan, 0, 0, fault_kind=FaultKind.INFRASTRUCTURE)
    with pytest.raises(ValueError, match="unresolved infrastructure"):
        aggregate_blocks(
            (PairedBlock(infrastructure, _observation(plan, 0, 1)),),
            "A",
            plan=plan,
        )


def test_strict_numeric_and_telemetry_validation() -> None:
    with pytest.raises(ValueError):
        dataclasses.replace(_profile(), cpu_threads_per_connection=True)
    with pytest.raises(ValueError, match="ordered"):
        DecisionTelemetry(
            decisions=2,
            p50_latency_ms=3.0,
            p95_latency_ms=2.0,
            p99_latency_ms=4.0,
            search_nodes=1,
            fallback_decisions=0,
            trace_digest=_h("bad-telemetry"),
        )

    observation = _observation(_plan(count=1), 0, 0)
    forged = dataclasses.replace(
        observation.telemetry_by_connection[0],
        search_nodes=observation.telemetry_by_connection[0].search_nodes + 1,
    )
    with pytest.raises(ValueError, match="replay-derived timing/search"):
        dataclasses.replace(
            observation,
            telemetry_by_connection=(forged, observation.telemetry_by_connection[1]),
        )
