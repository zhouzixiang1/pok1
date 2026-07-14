from __future__ import annotations

import inspect
import math
from dataclasses import fields, replace

import pytest

from bots.research_native_lab.common_contracts import seeds


def _entropy() -> tuple[seeds.FinalEvaluationPlan, seeds.VerifiedBeacon]:
    not_before = 1_800_003_600
    round_number = (
        math.ceil(
            (not_before - seeds.DRAND_GENESIS_TIME) / seeds.DRAND_PERIOD_SEC
        )
        + 1
    )
    plan = seeds.FinalEvaluationPlan(
        candidate_bundle_digest="ab" * 32,
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
        randomness="11" * 32,
        signature="03" * 96,
        previous_signature="04" * 96,
        receipt_digest="05" * 32,
    )
    object.__setattr__(beacon, "_token", seeds._VERIFIED_BEACON_TOKEN)
    return plan, beacon


def test_seed_cohort_schema_cannot_name_focal_variant() -> None:
    names = {item.name for item in fields(seeds.FormalSeedCohort)}
    assert names == {
        "comparison_domain",
        "budget_ms",
        "counterparty_scope_digest",
        "paired_block_count",
        "common_contract_digest",
    }
    forbidden_fragments = ("candidate", "artifact", "checkpoint", "comparison_mode", "focal")
    assert not any(fragment in name for name in names for fragment in forbidden_fragments)


def test_same_candidate_neutral_cohort_produces_one_deck_stream() -> None:
    plan, beacon = _entropy()
    cohort = seeds.FormalSeedCohort(
        comparison_domain="external-opponent",
        budget_ms=5_000,
        counterparty_scope_digest="10" * 32,
        paired_block_count=100,
        common_contract_digest="20" * 32,
    )
    copied = replace(cohort)
    assert copied.digest() == cohort.digest()
    first = plan.derive_formal_deck_root_pool(beacon, cohort.digest())
    second = plan.derive_formal_deck_root_pool(beacon, copied.digest())
    assert first == second
    assert len(first) == seeds.FORMAL_DECK_ROOT_POOL_SIZE


def test_policy_stream_follows_identity_within_shared_cohort() -> None:
    plan, beacon = _entropy()
    cohort = seeds.FormalSeedCohort(
        comparison_domain="direct-h2h",
        budget_ms=20_000,
        counterparty_scope_digest="30" * 32,
        paired_block_count=400,
        common_contract_digest="40" * 32,
    )
    left = plan.derive_formal_policy_seeds(beacon, cohort.digest(), "51" * 32, 400)
    right = plan.derive_formal_policy_seeds(beacon, cohort.digest(), "52" * 32, 400)
    assert left != right
    assert len(set(left)) == len(left) == 400
    assert len(set(right)) == len(right) == 400


def test_analysis_rng_has_no_caller_seed_and_is_domain_separated() -> None:
    plan, beacon = _entropy()
    parameters = tuple(inspect.signature(plan.derive_formal_analysis_seed).parameters)
    assert parameters == (
        "beacon",
        "seed_cohort_digest",
        "hypothesis_digest",
        "analysis_domain",
    )
    kwargs = {
        "seed_cohort_digest": "61" * 32,
        "hypothesis_digest": "62" * 32,
    }
    bootstrap = plan.derive_formal_analysis_seed(
        beacon, analysis_domain="bootstrap", **kwargs
    )
    sign_flip = plan.derive_formal_analysis_seed(
        beacon, analysis_domain="sign-flip", **kwargs
    )
    assert bootstrap != sign_flip
    assert bootstrap == plan.derive_formal_analysis_seed(
        beacon, analysis_domain="bootstrap", **kwargs
    )
    with pytest.raises(ValueError, match="bootstrap or sign-flip"):
        plan.derive_formal_analysis_seed(
            beacon, analysis_domain="user-selected", **kwargs
        )


def test_v1_wire_field_is_explicitly_the_complete_matrix_root() -> None:
    plan, _ = _entropy()
    assert plan.complete_formal_matrix_root_digest == plan.candidate_bundle_digest
    assert plan.to_dict()["root_semantics"] == (
        "candidate_bundle_digest_is_complete_formal_matrix_root"
    )


def test_caller_rpc_freeze_and_known_drand_never_gain_formal_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, beacon = _entropy()
    assert plan.freeze_authority_kind == seeds.FREEZE_AUTHORITY_DIAGNOSTIC
    assert plan.entropy_target_bitcoin_height == (
        plan.freeze_bitcoin_height + seeds.FUTURE_BITCOIN_BLOCK_OFFSET
    )
    assert beacon.formal_entropy_mix_digest is None

    # A deployment flag alone cannot promote the caller-owned local-RPC path.
    monkeypatch.setattr(seeds, "FORMAL_FUTURE_ENTROPY_AVAILABLE", True)
    with pytest.raises(ValueError, match="formal future entropy is unavailable"):
        plan._assert_formal()


def test_direct_plan_rejects_a_drand_round_known_before_the_frozen_delay() -> None:
    with pytest.raises(ValueError, match="beacon round differs"):
        seeds.FinalEvaluationPlan(
            candidate_bundle_digest="ab" * 32,
            freeze_attested_epoch=1_800_000_000,
            freeze_receipt_digest="01" * 32,
            freeze_bitcoin_height=900_000,
            freeze_bitcoin_block_hash="02" * 32,
            beacon_not_before_epoch=1_800_003_600,
            beacon_round=123,
        )


def test_formal_beacon_requires_future_block_chainwork_and_witness_mix() -> None:
    plan, beacon = _entropy()
    object.__setattr__(plan, "freeze_authority_kind", seeds.FREEZE_AUTHORITY_EXTERNAL)
    object.__setattr__(plan, "_formal_authority_guard", lambda candidate: candidate is plan)
    with pytest.raises(ValueError, match="formal future entropy is unavailable"):
        beacon._assert_formal_for(plan)
