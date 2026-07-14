"""Frozen native-TCP evaluation evidence and paired-cluster statistics.

The types in this module deliberately separate a preregistered plan from what
the launcher and replay verifier observed.  A formal aggregate is constructed
only when every planned block is present exactly once, both legs use the same
70-deal sequence, resources and policy seeds are bound to artifact identity,
and every infrastructure retry has a unique retained lineage.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import asdict, dataclass, field
from enum import Enum
from statistics import NormalDist
from typing import Iterable, Mapping, Sequence

from .constants import HANDS_PER_MATCH
from .deal_generator import (
    DEAL_GENERATOR_ALGORITHM_DIGEST,
    build_70_hand_commitment,
)


Digest = str
MAX_SEED = (1 << 256) - 1
PLATFORM_ACTION_TIMEOUT_MS = 60_000
HARD_COMPUTE_STOP_MS = 54_000
FORMAL_TIME_BUDGETS_MS = frozenset({250, 5_000, 20_000, 50_000})
FORMAL_DECK_ROOT_POOL_SIZE = 8_192
_FORMAL_STRENGTH_PLAN_TOKEN = object()
_FORMAL_MATRIX_TOKEN = object()


def outcome_score(net_chips: int) -> float:
    return 1.0 if net_chips > 0 else 0.0 if net_chips < 0 else 0.5


def _digest(value: str, name: str) -> Digest:
    try:
        decoded = bytes.fromhex(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be hexadecimal") from exc
    if len(decoded) != 32:
        raise ValueError(f"{name} must be a 32-byte digest")
    return value.lower()


def _payload_digest(payload: Mapping | Sequence) -> Digest:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _strict_int(value: int, name: str, *, minimum: int = 0, maximum: int | None = None) -> int:
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        suffix = f"..{maximum}" if maximum is not None else " or greater"
        raise ValueError(f"{name} must be an integer in {minimum}{suffix}")
    return value


def _seed(value: int, name: str) -> int:
    return _strict_int(value, name, maximum=MAX_SEED)


def _two_items(value: Sequence, name: str) -> tuple:
    materialized = tuple(value)
    if len(materialized) != 2:
        raise ValueError(f"{name} must contain exactly two connection slots")
    return materialized


def _nonempty_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


@dataclass(frozen=True, slots=True)
class ArtifactIdentity:
    """Content identity for one candidate; ``display_label`` is non-semantic."""

    display_label: str
    sealed_tree_manifest_digest: Digest
    launch_contract_digest: Digest
    launch_command_digest: Digest
    base_environment_digest: Digest
    model_digest: Digest
    config_digest: Digest
    action_set_digest: Digest
    dependency_digest: Digest
    runtime_digest: Digest

    def __post_init__(self) -> None:
        if not self.display_label:
            raise ValueError("artifact display label is required")
        for name in (
            "sealed_tree_manifest_digest",
            "launch_contract_digest",
            "launch_command_digest",
            "base_environment_digest",
            "model_digest",
            "config_digest",
            "action_set_digest",
            "dependency_digest",
            "runtime_digest",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), name))

    def identity_digest(self) -> Digest:
        # A presentation rename must not manufacture a new artifact identity.
        return _payload_digest(
            {
                "sealed_tree_manifest_digest": self.sealed_tree_manifest_digest,
                "launch_contract_digest": self.launch_contract_digest,
                "launch_command_digest": self.launch_command_digest,
                "base_environment_digest": self.base_environment_digest,
                "model_digest": self.model_digest,
                "config_digest": self.config_digest,
                "action_set_digest": self.action_set_digest,
                "dependency_digest": self.dependency_digest,
                "runtime_digest": self.runtime_digest,
            }
        )


@dataclass(frozen=True, slots=True)
class DealSequenceCommitment:
    """Commitment to all 70 deal seeds and the resulting complete deals."""

    generator_digest: Digest
    deck_root_seed: int
    hand_seeds: tuple[int, ...]
    hand_deal_digests: tuple[Digest, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "generator_digest", _digest(self.generator_digest, "deal generator"))
        if self.generator_digest != DEAL_GENERATOR_ALGORITHM_DIGEST:
            raise ValueError("deal commitment does not use the frozen common generator")
        _seed(self.deck_root_seed, "deck root seed")
        seeds = tuple(self.hand_seeds)
        deals = tuple(self.hand_deal_digests)
        if len(seeds) != HANDS_PER_MATCH or len(deals) != HANDS_PER_MATCH:
            raise ValueError(f"a deal commitment requires exactly {HANDS_PER_MATCH} hands")
        for index, value in enumerate(seeds):
            _seed(value, f"hand seed {index}")
        if len(set(seeds)) != len(seeds):
            raise ValueError("hand seeds overlap inside one 70-hand match")
        deals = tuple(_digest(value, f"hand deal {index}") for index, value in enumerate(deals))
        if len(set(deals)) != len(deals):
            raise ValueError("complete deal payloads repeat inside one 70-hand match")
        object.__setattr__(self, "hand_seeds", seeds)
        object.__setattr__(self, "hand_deal_digests", deals)
        expected = build_70_hand_commitment(self.deck_root_seed)
        if seeds != expected.hand_seeds or deals != expected.deck_digests:
            raise ValueError("deal commitment does not match its frozen root derivation")

    @classmethod
    def from_root(cls, deck_root_seed: int) -> "DealSequenceCommitment":
        expected = build_70_hand_commitment(deck_root_seed)
        return cls(
            generator_digest=DEAL_GENERATOR_ALGORITHM_DIGEST,
            deck_root_seed=deck_root_seed,
            hand_seeds=expected.hand_seeds,
            hand_deal_digests=expected.deck_digests,
        )

    def digest(self) -> Digest:
        return _payload_digest(asdict(self))


@dataclass(frozen=True, slots=True)
class ResourceProfile:
    """Exact per-connection resource envelope for one isolated formal match."""

    cpu_affinity_by_connection: tuple[tuple[int, ...], tuple[int, ...]]
    cpu_threads_per_connection: int
    cpu_quota_us: int
    cpu_period_us: int
    max_tasks_per_connection: int
    thread_environment_digest: Digest
    ram_limit_bytes_per_connection: int
    swap_limit_bytes_per_connection: int
    gpu_devices_by_connection: tuple[tuple[str, ...], tuple[str, ...]]
    vram_limit_bytes_per_connection: int
    decision_budget_ms: int
    platform_action_timeout_ms: int
    match_wall_timeout_ms: int
    action_send_delay_ms: int
    enforcer_digest: Digest
    cgroup_controllers_digest: Digest
    concurrent_matches: int = 1
    pondering_allowed: bool = False

    def __post_init__(self) -> None:
        cpu_slots = _two_items(self.cpu_affinity_by_connection, "CPU affinity")
        normalized_cpu: list[tuple[int, ...]] = []
        for index, slot in enumerate(cpu_slots):
            slot = tuple(slot)
            if not slot or len(set(slot)) != len(slot):
                raise ValueError(f"CPU slot {index} must be non-empty and unique")
            for cpu in slot:
                _strict_int(cpu, f"CPU slot {index}")
            normalized_cpu.append(slot)
        if set(normalized_cpu[0]) & set(normalized_cpu[1]):
            raise ValueError("formal connection CPU sets must be disjoint")
        object.__setattr__(self, "cpu_affinity_by_connection", tuple(normalized_cpu))

        _strict_int(self.cpu_threads_per_connection, "CPU threads", minimum=1)
        if any(self.cpu_threads_per_connection > len(slot) for slot in normalized_cpu):
            raise ValueError("CPU thread count exceeds a frozen affinity slot")
        for name in (
            "cpu_quota_us",
            "cpu_period_us",
            "max_tasks_per_connection",
            "ram_limit_bytes_per_connection",
            "match_wall_timeout_ms",
        ):
            _strict_int(getattr(self, name), name, minimum=1)
        if self.cpu_quota_us > self.cpu_period_us * self.cpu_threads_per_connection:
            raise ValueError("CPU quota exceeds the frozen per-bot core envelope")
        if self.max_tasks_per_connection < self.cpu_threads_per_connection:
            raise ValueError("task limit is smaller than the frozen worker thread count")
        _strict_int(self.swap_limit_bytes_per_connection, "swap limit")
        if self.swap_limit_bytes_per_connection != 0:
            raise ValueError("formal evaluation forbids swap as a capacity strategy")

        gpu_slots = _two_items(self.gpu_devices_by_connection, "GPU visibility")
        normalized_gpu = tuple(tuple(slot) for slot in gpu_slots)
        for index, slot in enumerate(normalized_gpu):
            if len(set(slot)) != len(slot) or any(not isinstance(device, str) or not device for device in slot):
                raise ValueError(f"GPU slot {index} contains invalid or duplicate devices")
        if set(normalized_gpu[0]) & set(normalized_gpu[1]):
            raise ValueError("formal connection GPU devices must be isolated")
        _strict_int(self.vram_limit_bytes_per_connection, "VRAM limit")
        if not any(normalized_gpu) and self.vram_limit_bytes_per_connection != 0:
            raise ValueError("CPU-only evaluation must have a zero VRAM limit")
        if any(normalized_gpu) and self.vram_limit_bytes_per_connection == 0:
            raise ValueError("GPU evaluation must declare an enforceable VRAM limit")
        object.__setattr__(self, "gpu_devices_by_connection", normalized_gpu)

        _strict_int(self.decision_budget_ms, "decision budget", minimum=1, maximum=HARD_COMPUTE_STOP_MS)
        _strict_int(self.platform_action_timeout_ms, "platform action timeout", minimum=1)
        if self.platform_action_timeout_ms != PLATFORM_ACTION_TIMEOUT_MS:
            raise ValueError("national formal platform timeout is exactly 60 seconds")
        if self.decision_budget_ms >= self.platform_action_timeout_ms:
            raise ValueError("compute budget must stop before the platform deadline")
        if self.match_wall_timeout_ms <= self.platform_action_timeout_ms:
            raise ValueError("match wall timeout cannot be a decision timeout")
        _strict_int(self.action_send_delay_ms, "action send delay")
        if type(self.concurrent_matches) is not int or self.concurrent_matches != 1:
            raise ValueError("formal matches run sequentially inside a resource profile")
        if type(self.pondering_allowed) is not bool or self.pondering_allowed:
            raise ValueError("formal comparison forbids opponent-time pondering")
        for name in ("thread_environment_digest", "enforcer_digest", "cgroup_controllers_digest"):
            object.__setattr__(self, name, _digest(getattr(self, name), name))

    def digest(self) -> Digest:
        return _payload_digest(asdict(self))


@dataclass(frozen=True, slots=True)
class EvaluationStratum:
    """One estimand; no aggregate may cross any field in this contract."""

    identity_pair: tuple[Digest, Digest]
    split: str
    opponent_family_manifest_digest: Digest
    rules_digest: Digest
    harness_digest: Digest
    deal_generator_digest: Digest
    resource_profile_digest: Digest
    time_budget_ms: int
    comparison_mode: str
    hypothesis_digest: Digest
    multiplicity_family_digest: Digest

    def __post_init__(self) -> None:
        pair = tuple(_digest(item, "stratum identity") for item in _two_items(self.identity_pair, "identity pair"))
        if pair[0] == pair[1]:
            raise ValueError("evaluation stratum requires two distinct content identities")
        if pair != tuple(sorted(pair)):
            raise ValueError("evaluation identity pair must use canonical digest ordering")
        object.__setattr__(self, "identity_pair", pair)
        if self.split not in {
            "train",
            "dev",
            "validation",
            "current-pool",
            "stable-anchor",
            "final-heldout",
            "nemesis-exploit",
            "direct-h2h",
            "ablation",
        }:
            raise ValueError("unknown evaluation split")
        if self.comparison_mode not in {"controlled", "best-of-route"}:
            raise ValueError("unknown comparison mode")
        _strict_int(self.time_budget_ms, "stratum time budget", minimum=1, maximum=HARD_COMPUTE_STOP_MS)
        for name in (
            "opponent_family_manifest_digest",
            "rules_digest",
            "harness_digest",
            "deal_generator_digest",
            "resource_profile_digest",
            "hypothesis_digest",
            "multiplicity_family_digest",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), name))

    def digest(self) -> Digest:
        return _payload_digest(asdict(self))


@dataclass(frozen=True, slots=True)
class BlockPlan:
    """Preregistered seeds and complete deal commitment for one paired block."""

    stratum_digest: Digest
    identity_pair: tuple[Digest, Digest]
    block_index: int
    deal_sequence: DealSequenceCommitment
    policy_seed_by_identity: tuple[tuple[Digest, int], tuple[Digest, int]]
    block_id: Digest

    def __post_init__(self) -> None:
        object.__setattr__(self, "stratum_digest", _digest(self.stratum_digest, "block stratum"))
        pair = tuple(_digest(item, "block identity") for item in _two_items(self.identity_pair, "block identity pair"))
        if pair != tuple(sorted(pair)) or pair[0] == pair[1]:
            raise ValueError("block identity pair must be two canonical distinct digests")
        object.__setattr__(self, "identity_pair", pair)
        _strict_int(self.block_index, "block index")
        seeds = tuple(self.policy_seed_by_identity)
        if len(seeds) != 2:
            raise ValueError("block requires two identity-bound policy seeds")
        normalized = tuple((_digest(identity, "policy-seed identity"), _seed(seed, "policy seed")) for identity, seed in seeds)
        if tuple(identity for identity, _ in normalized) != pair:
            raise ValueError("policy seeds must follow canonical artifact identity order")
        object.__setattr__(self, "policy_seed_by_identity", normalized)
        expected = self.expected_block_id()
        object.__setattr__(self, "block_id", _digest(self.block_id, "block ID"))
        if self.block_id != expected:
            raise ValueError("block ID is not the canonical commitment to its plan")

    @classmethod
    def create(
        cls,
        *,
        stratum_digest: Digest,
        identity_pair: tuple[Digest, Digest],
        block_index: int,
        deal_sequence: DealSequenceCommitment,
        policy_seed_by_identity: tuple[tuple[Digest, int], tuple[Digest, int]],
    ) -> "BlockPlan":
        normalized_stratum = _digest(stratum_digest, "block stratum")
        pair = tuple(_digest(item, "block identity") for item in _two_items(identity_pair, "block identity pair"))
        if pair != tuple(sorted(pair)) or pair[0] == pair[1]:
            raise ValueError("block identity pair must be two canonical distinct digests")
        _strict_int(block_index, "block index")
        normalized = tuple(
            (_digest(identity, "policy-seed identity"), _seed(seed, "policy seed"))
            for identity, seed in policy_seed_by_identity
        )
        block_id = _payload_digest(
            {
                "stratum_digest": normalized_stratum,
                "identity_pair": pair,
                "block_index": block_index,
                "deal_sequence_digest": deal_sequence.digest(),
                "policy_seed_by_identity": normalized,
            }
        )
        return cls(
            stratum_digest=normalized_stratum,
            identity_pair=pair,
            block_index=block_index,
            deal_sequence=deal_sequence,
            policy_seed_by_identity=normalized,  # type: ignore[arg-type]
            block_id=block_id,
        )

    def expected_block_id(self) -> Digest:
        return _payload_digest(
            {
                "stratum_digest": self.stratum_digest,
                "identity_pair": self.identity_pair,
                "block_index": self.block_index,
                "deal_sequence_digest": self.deal_sequence.digest(),
                "policy_seed_by_identity": self.policy_seed_by_identity,
            }
        )

    def policy_seed(self, identity_digest: Digest) -> int:
        identity_digest = _digest(identity_digest, "policy identity")
        for identity, seed in self.policy_seed_by_identity:
            if identity == identity_digest:
                return seed
        raise KeyError(identity_digest)

    def digest(self) -> Digest:
        return _payload_digest(
            {
                "stratum_digest": self.stratum_digest,
                "identity_pair": self.identity_pair,
                "block_index": self.block_index,
                "deal_sequence_digest": self.deal_sequence.digest(),
                "policy_seed_by_identity": self.policy_seed_by_identity,
                "block_id": self.block_id,
            }
        )


@dataclass(frozen=True, slots=True)
class PlannedStratumContract:
    """Everything chosen for one stratum before future entropy is revealed."""

    stratum: EvaluationStratum
    paired_block_count: int
    stopping_rule_digest: Digest
    retry_policy_digest: Digest
    analysis_code_digest: Digest
    bootstrap_samples: int
    max_infrastructure_retries_per_leg: int

    def __post_init__(self) -> None:
        _strict_int(
            self.paired_block_count,
            "planned paired blocks",
            minimum=1,
            maximum=FORMAL_DECK_ROOT_POOL_SIZE,
        )
        _strict_int(self.bootstrap_samples, "planned bootstrap samples", minimum=10_000)
        _strict_int(
            self.max_infrastructure_retries_per_leg,
            "planned maximum infrastructure retries",
            minimum=0,
        )
        for name in ("stopping_rule_digest", "retry_policy_digest", "analysis_code_digest"):
            object.__setattr__(self, name, _digest(getattr(self, name), name))

    def digest(self) -> Digest:
        return _payload_digest(
            {
                "stratum_digest": self.stratum.digest(),
                "paired_block_count": self.paired_block_count,
                "stopping_rule_digest": self.stopping_rule_digest,
                "retry_policy_digest": self.retry_policy_digest,
                "analysis_code_digest": self.analysis_code_digest,
                "bootstrap_samples": self.bootstrap_samples,
                "max_infrastructure_retries_per_leg": self.max_infrastructure_retries_per_leg,
            }
        )


@dataclass(frozen=True, slots=True)
class CandidateBundleManifest:
    """Pre-beacon manifest externally timestamped before final selection."""

    scope: str
    research_candidate_identity_digests: tuple[Digest, ...]
    opponent_identity_digests: tuple[Digest, ...]
    common_contract_tree_digest: Digest
    opponent_universe_digest: Digest
    resource_profile_digests: tuple[Digest, ...]
    evaluation_harness_digest: Digest
    rules_digest: Digest
    evaluation_contract_digest: Digest
    final_randomness_contract_digest: Digest
    planned_strata: tuple[PlannedStratumContract, ...]
    replay_verifier_digest: Digest
    oracle_fixture_digest: Digest
    infrastructure_monitor_digest: Digest
    _formal_matrix_token: object = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.scope not in {"development_pair", "formal_three_candidate_matrix"}:
            raise ValueError("unknown candidate bundle scope")
        candidates = tuple(sorted(_digest(value, "bundle candidate") for value in self.research_candidate_identity_digests))
        expected = 3 if self.scope == "formal_three_candidate_matrix" else 2
        if len(candidates) != expected or len(set(candidates)) != len(candidates):
            raise ValueError(f"candidate bundle requires exactly {expected} unique research identities")
        opponents = tuple(sorted(_digest(value, "bundle opponent") for value in self.opponent_identity_digests))
        if len(set(opponents)) != len(opponents) or set(opponents) & set(candidates):
            raise ValueError("candidate and opponent identities must be disjoint and unique")
        if self.scope == "formal_three_candidate_matrix" and not opponents:
            raise ValueError("formal candidate bundle requires a frozen opponent universe")
        object.__setattr__(self, "research_candidate_identity_digests", candidates)
        object.__setattr__(self, "opponent_identity_digests", opponents)
        resources = tuple(sorted(_digest(value, "bundle resource profile") for value in self.resource_profile_digests))
        strata = tuple(sorted(self.planned_strata, key=lambda item: item.stratum.digest()))
        if not resources or len(set(resources)) != len(resources):
            raise ValueError("candidate bundle resource profiles must be non-empty and unique")
        stratum_digests = [item.stratum.digest() for item in strata]
        if not strata or len(set(stratum_digests)) != len(stratum_digests):
            raise ValueError("candidate bundle strata must be non-empty and unique")
        object.__setattr__(self, "resource_profile_digests", resources)
        object.__setattr__(self, "planned_strata", strata)
        for name in (
            "common_contract_tree_digest",
            "opponent_universe_digest",
            "evaluation_harness_digest",
            "rules_digest",
            "evaluation_contract_digest",
            "final_randomness_contract_digest",
            "replay_verifier_digest",
            "oracle_fixture_digest",
            "infrastructure_monitor_digest",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), name))

    def digest(self) -> Digest:
        return _payload_digest(
            {
                "scope": self.scope,
                "research_candidate_identity_digests": self.research_candidate_identity_digests,
                "opponent_identity_digests": self.opponent_identity_digests,
                "common_contract_tree_digest": self.common_contract_tree_digest,
                "opponent_universe_digest": self.opponent_universe_digest,
                "resource_profile_digests": self.resource_profile_digests,
                "evaluation_harness_digest": self.evaluation_harness_digest,
                "rules_digest": self.rules_digest,
                "evaluation_contract_digest": self.evaluation_contract_digest,
                "final_randomness_contract_digest": self.final_randomness_contract_digest,
                "planned_stratum_contract_digests": tuple(item.digest() for item in self.planned_strata),
                "replay_verifier_digest": self.replay_verifier_digest,
                "oracle_fixture_digest": self.oracle_fixture_digest,
                "infrastructure_monitor_digest": self.infrastructure_monitor_digest,
            }
        )

    def planned_contract(self, stratum_digest: Digest) -> PlannedStratumContract:
        stratum_digest = _digest(stratum_digest, "planned stratum")
        matches = [item for item in self.planned_strata if item.stratum.digest() == stratum_digest]
        if len(matches) != 1:
            raise KeyError(stratum_digest)
        return matches[0]

    def _assert_formal_matrix(self) -> None:
        if self.scope != "formal_three_candidate_matrix" or self._formal_matrix_token is not _FORMAL_MATRIX_TOKEN:
            raise ValueError("candidate bundle has not passed the complete formal matrix gate")


@dataclass(frozen=True, slots=True)
class FormalEvaluationPlan:
    """Exact finite formal sample set and frozen analysis/retry policy."""

    artifacts: tuple[ArtifactIdentity, ArtifactIdentity]
    candidate_bundle: CandidateBundleManifest | None
    stratum: EvaluationStratum
    resource_profile: ResourceProfile
    blocks: tuple[BlockPlan, ...]
    candidate_freeze_receipt_digest: Digest
    randomness_receipt_digest: Digest
    stopping_rule_digest: Digest
    retry_policy_digest: Digest
    analysis_code_digest: Digest
    replay_verifier_digest: Digest
    oracle_fixture_digest: Digest
    infrastructure_monitor_digest: Digest
    bootstrap_seed: int
    bootstrap_samples: int = 10_000
    max_infrastructure_retries_per_leg: int = 2
    result_authority: str = "development_diagnostic_only"
    complete_matrix_root_digest: Digest | None = None
    matrix_projection_digest: Digest | None = None
    matrix_template_digest: Digest | None = None
    formal_seed_cohort_digest: Digest | None = None
    matrix_candidate_bundle_digest: Digest | None = None
    matrix_plan_bridge_digest: Digest | None = None
    sign_flip_seed: int | None = None
    _formal_token: object = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not hasattr(self, "_formal_token"):
            object.__setattr__(self, "_formal_token", None)
        if self.result_authority not in {
            "development_diagnostic_only",
            "formal_strength",
        }:
            raise ValueError("unknown evaluation result authority")
        artifacts = _two_items(self.artifacts, "formal artifacts")
        object.__setattr__(self, "artifacts", artifacts)
        if len({artifact.display_label for artifact in artifacts}) != 2:
            raise ValueError("formal artifact display labels must be unique within a plan")
        digests = tuple(sorted(artifact.identity_digest() for artifact in artifacts))
        if {artifact.display_label for artifact in artifacts} & set(digests):
            raise ValueError("artifact display labels cannot masquerade as content digests")
        if digests[0] == digests[1]:
            raise ValueError("display aliases cannot create two formal artifacts")
        if digests != self.stratum.identity_pair:
            raise ValueError("formal artifacts do not match the stratum")
        planned: PlannedStratumContract | None = None
        if self.candidate_bundle is not None:
            research = set(
                self.candidate_bundle.research_candidate_identity_digests
            )
            opponents = set(self.candidate_bundle.opponent_identity_digests)
            if self.stratum.split == "direct-h2h":
                if not set(digests) <= research:
                    raise ValueError(
                        "direct H2H participants are not both frozen research candidates"
                    )
            elif (
                len(set(digests) & research) != 1
                or len(set(digests) & opponents) != 1
            ):
                raise ValueError(
                    "external stratum must pair one research candidate with one frozen opponent"
                )
            try:
                planned = self.candidate_bundle.planned_contract(
                    self.stratum.digest()
                )
            except KeyError as exc:
                raise ValueError(
                    "formal stratum is absent from the timestamped evaluation matrix"
                ) from exc
            if (
                self.resource_profile.digest()
                not in self.candidate_bundle.resource_profile_digests
            ):
                raise ValueError(
                    "formal resource profile is absent from the timestamped bundle"
                )
            if (
                self.stratum.harness_digest
                != self.candidate_bundle.evaluation_harness_digest
            ):
                raise ValueError(
                    "formal harness differs from the timestamped bundle"
                )
            if self.stratum.rules_digest != self.candidate_bundle.rules_digest:
                raise ValueError("formal rules differ from the timestamped bundle")
        else:
            matrix_fields = (
                "complete_matrix_root_digest",
                "matrix_projection_digest",
                "matrix_template_digest",
                "formal_seed_cohort_digest",
                "matrix_candidate_bundle_digest",
                "matrix_plan_bridge_digest",
            )
            missing = [name for name in matrix_fields if getattr(self, name) is None]
            if missing:
                raise ValueError(
                    "matrix-backed evaluation plan lacks authority fields: "
                    + ", ".join(missing)
                )
            for name in matrix_fields:
                object.__setattr__(
                    self,
                    name,
                    _digest(getattr(self, name), name.replace("_", " ")),
                )
            if self.sign_flip_seed is None:
                raise ValueError("matrix-backed evaluation plan lacks sign-flip seed")
            _seed(self.sign_flip_seed, "sign-flip seed")
            if self.sign_flip_seed == self.bootstrap_seed:
                raise ValueError("bootstrap and sign-flip seeds are not domain-separated")
        if self.stratum.resource_profile_digest != self.resource_profile.digest():
            raise ValueError("formal resource profile does not match the stratum")
        if self.stratum.time_budget_ms != self.resource_profile.decision_budget_ms:
            raise ValueError("formal resource and stratum budgets differ")
        if self.stratum.comparison_mode == "controlled":
            if artifacts[0].action_set_digest != artifacts[1].action_set_digest:
                raise ValueError("controlled comparison changed the action set")

        blocks = tuple(self.blocks)
        if not blocks:
            raise ValueError("formal evaluation plan requires at least one block")
        if len(blocks) > FORMAL_DECK_ROOT_POOL_SIZE:
            raise ValueError("evaluation plan exceeds the frozen deck-root pool")
        if self.stratum.deal_generator_digest != blocks[0].deal_sequence.generator_digest:
            raise ValueError("formal deal generator does not match the stratum")
        if tuple(block.block_index for block in blocks) != tuple(range(len(blocks))):
            raise ValueError("formal block indices must be an exact ordered prefix from zero")
        if len({block.block_id for block in blocks}) != len(blocks):
            raise ValueError("formal block IDs must be unique")
        seen_hand_seeds: set[int] = set()
        seen_hand_deals: set[Digest] = set()
        seen_deck_roots: set[int] = set()
        seen_policy_streams: set[tuple[Digest, int]] = set()
        for block in blocks:
            if block.stratum_digest != self.stratum.digest() or block.identity_pair != self.stratum.identity_pair:
                raise ValueError("formal block escaped its stratum")
            if block.deal_sequence.generator_digest != self.stratum.deal_generator_digest:
                raise ValueError("formal block changed the deal generator")
            if block.deal_sequence.deck_root_seed in seen_deck_roots:
                raise ValueError("deck root seed repeats across formal blocks")
            seen_deck_roots.add(block.deal_sequence.deck_root_seed)
            overlap = seen_hand_seeds & set(block.deal_sequence.hand_seeds)
            if overlap:
                raise ValueError("70-hand seed windows overlap across formal blocks")
            seen_hand_seeds.update(block.deal_sequence.hand_seeds)
            deal_overlap = seen_hand_deals & set(block.deal_sequence.hand_deal_digests)
            if deal_overlap:
                raise ValueError("complete deals repeat across formal blocks")
            seen_hand_deals.update(block.deal_sequence.hand_deal_digests)
            for stream in block.policy_seed_by_identity:
                if stream in seen_policy_streams:
                    raise ValueError("artifact policy RNG stream repeats across formal blocks")
                seen_policy_streams.add(stream)
        object.__setattr__(self, "blocks", blocks)

        for name in (
            "candidate_freeze_receipt_digest",
            "randomness_receipt_digest",
            "stopping_rule_digest",
            "retry_policy_digest",
            "analysis_code_digest",
            "replay_verifier_digest",
            "oracle_fixture_digest",
            "infrastructure_monitor_digest",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), name))
        if self.candidate_bundle is not None:
            if (
                self.replay_verifier_digest
                != self.candidate_bundle.replay_verifier_digest
            ):
                raise ValueError(
                    "formal replay verifier differs from the timestamped bundle"
                )
            if (
                self.oracle_fixture_digest
                != self.candidate_bundle.oracle_fixture_digest
            ):
                raise ValueError(
                    "formal oracle fixtures differ from the timestamped bundle"
                )
            if (
                self.infrastructure_monitor_digest
                != self.candidate_bundle.infrastructure_monitor_digest
            ):
                raise ValueError(
                    "formal infrastructure monitor differs from the timestamped bundle"
                )
        _seed(self.bootstrap_seed, "bootstrap seed")
        _strict_int(self.bootstrap_samples, "bootstrap samples", minimum=10_000)
        _strict_int(
            self.max_infrastructure_retries_per_leg,
            "maximum infrastructure retries",
            minimum=0,
        )
        if planned is not None and (
            len(self.blocks) != planned.paired_block_count
            or self.stopping_rule_digest != planned.stopping_rule_digest
            or self.retry_policy_digest != planned.retry_policy_digest
            or self.analysis_code_digest != planned.analysis_code_digest
            or self.bootstrap_samples != planned.bootstrap_samples
            or self.max_infrastructure_retries_per_leg
            != planned.max_infrastructure_retries_per_leg
        ):
            raise ValueError(
                "evaluation plan parameters differ from the timestamped stratum contract"
            )
        if self.result_authority == "formal_strength":
            if self.stratum.time_budget_ms not in FORMAL_TIME_BUDGETS_MS:
                raise ValueError("formal strength plan uses an unregistered time budget")
            minimum_blocks = 400 if self.stratum.split == "direct-h2h" else 100
            if len(self.blocks) < minimum_blocks:
                raise ValueError(
                    f"formal {self.stratum.split} plans require at least {minimum_blocks} paired blocks"
                )
            if self.candidate_bundle is not None:
                raise ValueError(
                    "legacy candidate-bundle manifests cannot authorize the complete formal matrix"
                )
            if self._formal_token is not _FORMAL_STRENGTH_PLAN_TOKEN:
                raise ValueError("formal strength plan lacks verified freeze and future entropy")

    def _unchecked_digest(self) -> Digest:
        return _payload_digest(
            {
                "artifact_identity_digests": tuple(sorted(item.identity_digest() for item in self.artifacts)),
                "candidate_bundle_digest": (
                    None
                    if self.candidate_bundle is None
                    else self.candidate_bundle.digest()
                ),
                "stratum_digest": self.stratum.digest(),
                "resource_profile_digest": self.resource_profile.digest(),
                "block_digests": tuple(block.digest() for block in self.blocks),
                "candidate_freeze_receipt_digest": self.candidate_freeze_receipt_digest,
                "randomness_receipt_digest": self.randomness_receipt_digest,
                "stopping_rule_digest": self.stopping_rule_digest,
                "retry_policy_digest": self.retry_policy_digest,
                "analysis_code_digest": self.analysis_code_digest,
                "replay_verifier_digest": self.replay_verifier_digest,
                "oracle_fixture_digest": self.oracle_fixture_digest,
                "infrastructure_monitor_digest": self.infrastructure_monitor_digest,
                "bootstrap_seed": self.bootstrap_seed,
                "bootstrap_samples": self.bootstrap_samples,
                "max_infrastructure_retries_per_leg": self.max_infrastructure_retries_per_leg,
                "result_authority": self.result_authority,
                "complete_matrix_root_digest": self.complete_matrix_root_digest,
                "matrix_projection_digest": self.matrix_projection_digest,
                "matrix_template_digest": self.matrix_template_digest,
                "formal_seed_cohort_digest": self.formal_seed_cohort_digest,
                "matrix_candidate_bundle_digest": self.matrix_candidate_bundle_digest,
                "matrix_plan_bridge_digest": self.matrix_plan_bridge_digest,
                "sign_flip_seed": self.sign_flip_seed,
            }
        )

    def _assert_formal_authority(self) -> None:
        guard = self._formal_token
        if not callable(guard) or guard(self) is not True:
            raise ValueError("formal evaluation plan was copied, forged, or altered")

    def digest(self) -> Digest:
        if self.result_authority == "formal_strength":
            self._assert_formal_authority()
        return self._unchecked_digest()

    @classmethod
    def from_verified_final_entropy(
        cls,
        *,
        final_entropy_plan: object,
        verified_beacon: object,
        **kwargs,
    ) -> "FormalEvaluationPlan":
        """Deprecated legacy path retained only to fail closed.

        The old exactly-three-candidate bundle cannot represent the complete
        checkpoint/mode/ablation matrix and its digest is not the frozen matrix
        root.  Formal callers must consume a matrix-issued bridge.
        """

        raise ValueError(
            "formal evaluation requires FormalEvaluationPlan.from_matrix_bridge; "
            "legacy CandidateBundleManifest entropy issuance is disabled"
        )

    @classmethod
    def from_matrix_bridge(
        cls,
        *,
        matrix_bridge: object,
        complete_matrix: object,
        matrix_projection: object,
        materialized_stratum: object,
        final_entropy_plan: object,
        verified_beacon: object,
        artifacts: Sequence[ArtifactIdentity],
        resource_profile: ResourceProfile,
    ) -> "FormalEvaluationPlan":
        """Construct one exact cell plan from the complete matrix authority."""

        from .matrix import (
            CompleteFormalMatrix,
            FormalEvaluationPlanBridge,
            FormalMatrixProjection,
            MaterializedStratum,
        )
        from .seeds import FinalEvaluationPlan as EntropyPlan, VerifiedBeacon

        if not isinstance(matrix_bridge, FormalEvaluationPlanBridge):
            raise ValueError("evaluation plan requires a typed matrix bridge")
        if not isinstance(complete_matrix, CompleteFormalMatrix):
            raise ValueError("evaluation plan requires a typed complete matrix")
        if not isinstance(matrix_projection, FormalMatrixProjection):
            raise ValueError("evaluation plan requires a typed matrix projection")
        if not isinstance(materialized_stratum, MaterializedStratum):
            raise ValueError("evaluation plan requires a materialized matrix stratum")
        if not isinstance(final_entropy_plan, EntropyPlan) or not isinstance(
            verified_beacon, VerifiedBeacon
        ):
            raise ValueError("evaluation plan requires typed future entropy")
        if not isinstance(resource_profile, ResourceProfile):
            raise ValueError("evaluation plan requires a typed resource profile")
        authority = matrix_bridge.result_authority
        if authority == "formal_strength":
            matrix_bridge.assert_for(
                complete_matrix,
                matrix_projection,
                materialized_stratum,
                final_entropy_plan,
                verified_beacon,
            )
        elif authority == "development_diagnostic_only":
            matrix_bridge.assert_diagnostic_for(
                complete_matrix,
                matrix_projection,
                materialized_stratum,
                final_entropy_plan,
                verified_beacon,
            )
        else:
            raise ValueError("matrix bridge has unknown result authority")
        bridge = matrix_bridge.sealed_payload()
        artifact_pair = _two_items(tuple(artifacts), "matrix plan artifacts")
        if any(not isinstance(item, ArtifactIdentity) for item in artifact_pair):
            raise ValueError("matrix plan artifacts must be typed identities")
        if tuple(item.identity_digest() for item in artifact_pair) != tuple(
            bridge["ordered_artifact_identity_digests"]
        ):
            raise ValueError("matrix plan artifacts differ from the resolved cell pair")
        if resource_profile.digest() != bridge["resource_profile_digest"]:
            raise ValueError("matrix plan resource profile differs from the frozen cell")
        if materialized_stratum.stratum.digest() != bridge[
            "evaluation_stratum_digest"
        ]:
            raise ValueError("matrix bridge changed the materialized stratum")
        block_count = bridge["paired_block_count"]
        if type(block_count) is not int:
            raise ValueError("matrix bridge block count is not an integer")
        cohort_digest = _digest(
            bridge["seed_cohort_digest"],  # type: ignore[arg-type]
            "formal seed cohort",
        )
        deck_roots = final_entropy_plan.derive_formal_deck_root_pool(
            verified_beacon,
            cohort_digest,
        )[:block_count]
        identity_pair = materialized_stratum.stratum.identity_pair
        policy_streams = {
            identity: final_entropy_plan.derive_formal_policy_seeds(
                verified_beacon,
                cohort_digest,
                identity,
                block_count,
            )
            for identity in identity_pair
        }
        blocks = tuple(
            BlockPlan.create(
                stratum_digest=materialized_stratum.stratum.digest(),
                identity_pair=identity_pair,
                block_index=index,
                deal_sequence=DealSequenceCommitment.from_root(deck_roots[index]),
                policy_seed_by_identity=(
                    (identity_pair[0], policy_streams[identity_pair[0]][index]),
                    (identity_pair[1], policy_streams[identity_pair[1]][index]),
                ),
            )
            for index in range(block_count)
        )
        constructor = {
            "artifacts": artifact_pair,
            "candidate_bundle": None,
            "stratum": materialized_stratum.stratum,
            "resource_profile": resource_profile,
            "blocks": blocks,
            "candidate_freeze_receipt_digest": bridge["freeze_receipt_digest"],
            "randomness_receipt_digest": bridge["beacon_receipt_digest"],
            "stopping_rule_digest": bridge["stopping_rule_digest"],
            "retry_policy_digest": bridge["retry_policy_digest"],
            "analysis_code_digest": bridge["analysis_code_digest"],
            "replay_verifier_digest": bridge["replay_verifier_digest"],
            "oracle_fixture_digest": bridge["oracle_fixture_digest"],
            "infrastructure_monitor_digest": bridge[
                "infrastructure_monitor_digest"
            ],
            "bootstrap_seed": bridge["bootstrap_seed"],
            "bootstrap_samples": materialized_stratum.template.bootstrap_samples,
            "max_infrastructure_retries_per_leg": (
                materialized_stratum.template.max_infrastructure_retries_per_leg
            ),
            "result_authority": authority,
            "complete_matrix_root_digest": bridge[
                "complete_matrix_root_digest"
            ],
            "matrix_projection_digest": bridge["projection_digest"],
            "matrix_template_digest": bridge["planned_template_digest"],
            "formal_seed_cohort_digest": cohort_digest,
            "matrix_candidate_bundle_digest": bridge[
                "candidate_bundle_digest"
            ],
            "matrix_plan_bridge_digest": bridge["bridge_digest"],
            "sign_flip_seed": bridge["sign_flip_seed"],
        }
        if authority == "formal_strength":
            plan = object.__new__(cls)
            object.__setattr__(plan, "_formal_token", _FORMAL_STRENGTH_PLAN_TOKEN)
            cls.__init__(plan, **constructor)  # type: ignore[arg-type]
        else:
            plan = cls(**constructor)  # type: ignore[arg-type]
        sealed_digest = plan._unchecked_digest()
        sealed_bridge_digest = matrix_bridge.digest()

        def issued_instance(
            candidate: object,
            owner: object = plan,
            sealed: Digest = sealed_digest,
            bridge_owner: object = matrix_bridge,
            bridge_digest: Digest = sealed_bridge_digest,
        ) -> bool:
            return (
                candidate is owner
                and isinstance(candidate, FormalEvaluationPlan)
                and candidate._unchecked_digest() == sealed
                and bridge_owner is matrix_bridge
                and matrix_bridge.digest() == bridge_digest
            )

        if authority == "formal_strength":
            object.__setattr__(plan, "_formal_token", issued_instance)
            plan._assert_formal_authority()
        return plan

    def artifact(self, identity_digest: Digest) -> ArtifactIdentity:
        identity_digest = _digest(identity_digest, "artifact identity")
        for artifact in self.artifacts:
            if artifact.identity_digest() == identity_digest:
                return artifact
        raise KeyError(identity_digest)

    def block(self, block_id: Digest) -> BlockPlan:
        block_id = _digest(block_id, "block ID")
        for block in self.blocks:
            if block.block_id == block_id:
                return block
        raise KeyError(block_id)


@dataclass(frozen=True, slots=True)
class LegPlan:
    formal_plan_digest: Digest
    stratum_digest: Digest
    block_id: Digest
    block_plan_digest: Digest
    leg_index: int
    connection_to_identity: tuple[Digest, Digest]
    deal_sequence_digest: Digest

    def __post_init__(self) -> None:
        for name in (
            "formal_plan_digest",
            "stratum_digest",
            "block_id",
            "block_plan_digest",
            "deal_sequence_digest",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), name))
        _strict_int(self.leg_index, "leg index", maximum=1)
        mapping = tuple(_digest(item, "connection identity") for item in _two_items(self.connection_to_identity, "connection mapping"))
        if mapping[0] == mapping[1]:
            raise ValueError("a leg requires two distinct connection identities")
        object.__setattr__(self, "connection_to_identity", mapping)

    @classmethod
    def from_plan(cls, plan: FormalEvaluationPlan, block: BlockPlan, leg_index: int) -> "LegPlan":
        _strict_int(leg_index, "leg index", maximum=1)
        pair = plan.stratum.identity_pair
        mapping = pair if leg_index == 0 else (pair[1], pair[0])
        return cls(
            formal_plan_digest=plan.digest(),
            stratum_digest=plan.stratum.digest(),
            block_id=block.block_id,
            block_plan_digest=block.digest(),
            leg_index=leg_index,
            connection_to_identity=mapping,
            deal_sequence_digest=block.deal_sequence.digest(),
        )

    def digest(self) -> Digest:
        return _payload_digest(asdict(self))


class TerminationKind(str, Enum):
    NORMAL = "normal"
    CRASH = "crash"
    TIMEOUT = "timeout"
    RESOURCE = "resource"
    PROTOCOL = "protocol"
    INFRASTRUCTURE = "infrastructure"


@dataclass(frozen=True, slots=True)
class ExecutionReceipt:
    """Launcher evidence for one identity in one immutable leg."""

    leg_plan_digest: Digest
    identity_digest: Digest
    connection_index: int
    launch_contract_digest: Digest
    launch_command_digest: Digest
    base_environment_digest: Digest
    thread_environment_digest: Digest
    launch_environment_digest: Digest
    actual_policy_seed: int
    run_id: Digest
    process_tree_id: str
    cgroup_path: str
    issuer_digest: Digest
    verifier_digest: Digest
    raw_evidence_digest: Digest
    termination_kind: TerminationKind
    termination_evidence_digest: Digest
    exit_code: int | None
    _enforcer_guard: object = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not hasattr(self, "_enforcer_guard"):
            object.__setattr__(self, "_enforcer_guard", None)
        for name in (
            "leg_plan_digest",
            "identity_digest",
            "launch_contract_digest",
            "launch_command_digest",
            "base_environment_digest",
            "thread_environment_digest",
            "launch_environment_digest",
            "run_id",
            "issuer_digest",
            "verifier_digest",
            "raw_evidence_digest",
            "termination_evidence_digest",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), name))
        _strict_int(self.connection_index, "connection index", maximum=1)
        _seed(self.actual_policy_seed, "actual policy seed")
        if not self.process_tree_id or not self.cgroup_path.startswith("/sys/fs/cgroup/"):
            raise ValueError("execution receipt requires process-tree and cgroup identity")
        if not isinstance(self.termination_kind, TerminationKind):
            raise ValueError("unknown execution termination kind")
        if self.exit_code is not None and type(self.exit_code) is not int:
            raise ValueError("exit code must be an integer or null")
        if self.termination_kind is TerminationKind.NORMAL and self.exit_code != 0:
            raise ValueError("a normal execution must have exit code zero")
        if self.termination_kind is not TerminationKind.NORMAL and self.exit_code == 0:
            raise ValueError("an abnormal execution cannot have exit code zero")

    def digest(self) -> Digest:
        payload = asdict(self)
        payload.pop("_enforcer_guard")
        return _payload_digest(payload)

    def _assert_formal_enforcer_authority(self) -> None:
        guard = self._enforcer_guard
        if not callable(guard) or guard(self) is not True:
            raise ValueError(
                "execution receipt was not issued by the frozen resource enforcer "
                "or was copied/altered"
            )


@dataclass(frozen=True, slots=True)
class ResourceReceipt:
    """cgroup/accelerator measurements bound to one execution receipt."""

    execution_receipt_digest: Digest
    profile_digest: Digest
    connection_index: int
    identity_digest: Digest
    cgroup_path: str
    cgroup_inode: int
    controllers_digest: Digest
    enforcer_digest: Digest
    cpu_affinity: tuple[int, ...]
    cpu_quota_us: int
    cpu_period_us: int
    max_tasks_limit: int
    memory_limit_bytes: int
    swap_limit_bytes: int
    gpu_devices: tuple[str, ...]
    vram_limit_bytes: int
    observed_max_tasks: int
    observed_peak_rss_bytes: int
    observed_peak_swap_bytes: int
    observed_peak_vram_bytes: int
    oom_kill_count: int
    pids_limit_hit_count: int
    deadline_kill_count: int
    cpu_throttled_usec: int
    started_epoch_ms: int
    finished_epoch_ms: int
    raw_evidence_digest: Digest
    verifier_digest: Digest
    cgroup_v2: bool = True
    thermal_event: bool = False
    host_preemption_event: bool = False
    _enforcer_guard: object = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not hasattr(self, "_enforcer_guard"):
            object.__setattr__(self, "_enforcer_guard", None)
        for name in (
            "execution_receipt_digest",
            "profile_digest",
            "identity_digest",
            "controllers_digest",
            "enforcer_digest",
            "raw_evidence_digest",
            "verifier_digest",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), name))
        _strict_int(self.connection_index, "resource connection index", maximum=1)
        if not self.cgroup_path.startswith("/sys/fs/cgroup/"):
            raise ValueError("resource receipt requires an absolute cgroup v2 path")
        _strict_int(self.cgroup_inode, "cgroup inode", minimum=1)
        affinity = tuple(self.cpu_affinity)
        if not affinity or len(set(affinity)) != len(affinity):
            raise ValueError("resource receipt CPU affinity must be non-empty and unique")
        for cpu in affinity:
            _strict_int(cpu, "resource CPU")
        object.__setattr__(self, "cpu_affinity", affinity)
        object.__setattr__(self, "gpu_devices", tuple(self.gpu_devices))
        for name in (
            "cpu_quota_us",
            "cpu_period_us",
            "max_tasks_limit",
            "memory_limit_bytes",
        ):
            _strict_int(getattr(self, name), name, minimum=1)
        for name in (
            "swap_limit_bytes",
            "vram_limit_bytes",
            "observed_max_tasks",
            "observed_peak_rss_bytes",
            "observed_peak_swap_bytes",
            "observed_peak_vram_bytes",
            "oom_kill_count",
            "pids_limit_hit_count",
            "deadline_kill_count",
            "cpu_throttled_usec",
            "started_epoch_ms",
            "finished_epoch_ms",
        ):
            _strict_int(getattr(self, name), name)
        if self.finished_epoch_ms <= self.started_epoch_ms:
            raise ValueError("resource receipt requires a positive time interval")
        if type(self.cgroup_v2) is not bool or not self.cgroup_v2:
            raise ValueError("formal resource receipts require cgroup v2")
        if type(self.thermal_event) is not bool or type(self.host_preemption_event) is not bool:
            raise ValueError("thermal/preemption markers must be boolean")

    def digest(self) -> Digest:
        payload = asdict(self)
        payload.pop("_enforcer_guard")
        return _payload_digest(payload)

    def _assert_formal_enforcer_authority(self) -> None:
        guard = self._enforcer_guard
        if not callable(guard) or guard(self) is not True:
            raise ValueError(
                "resource receipt was not issued by the frozen resource enforcer "
                "or was copied/altered"
            )

    def has_resource_overrun(self) -> bool:
        return (
            self.observed_max_tasks > self.max_tasks_limit
            or self.observed_peak_rss_bytes > self.memory_limit_bytes
            or self.observed_peak_swap_bytes > self.swap_limit_bytes
            or self.observed_peak_vram_bytes > self.vram_limit_bytes
            or self.oom_kill_count > 0
            or self.pids_limit_hit_count > 0
        )

    def verify(
        self,
        *,
        execution: ExecutionReceipt,
        profile: ResourceProfile,
    ) -> None:
        if self.execution_receipt_digest != execution.digest():
            raise ValueError("resource receipt is not bound to this execution")
        if self.profile_digest != profile.digest():
            raise ValueError("resource receipt profile digest mismatch")
        index = execution.connection_index
        if self.connection_index != index or self.identity_digest != execution.identity_digest:
            raise ValueError("resource receipt connection/identity mismatch")
        if self.cgroup_path != execution.cgroup_path:
            raise ValueError("resource receipt changed the execution cgroup path")
        if self.enforcer_digest != profile.enforcer_digest:
            raise ValueError("resource receipt was not verified by the frozen enforcer")
        if self.verifier_digest != profile.enforcer_digest:
            raise ValueError("resource receipt verifier differs from the frozen enforcer")
        if self.controllers_digest != profile.cgroup_controllers_digest:
            raise ValueError("resource receipt controller snapshot differs from the profile")
        if self.cpu_affinity != profile.cpu_affinity_by_connection[index]:
            raise ValueError("resource CPU affinity differs from the frozen slot")
        if self.gpu_devices != profile.gpu_devices_by_connection[index]:
            raise ValueError("resource GPU visibility differs from the frozen slot")
        expected = {
            "cpu_quota_us": profile.cpu_quota_us,
            "cpu_period_us": profile.cpu_period_us,
            "max_tasks_limit": profile.max_tasks_per_connection,
            "memory_limit_bytes": profile.ram_limit_bytes_per_connection,
            "swap_limit_bytes": profile.swap_limit_bytes_per_connection,
            "vram_limit_bytes": profile.vram_limit_bytes_per_connection,
        }
        for name, value in expected.items():
            if getattr(self, name) != value:
                raise ValueError(f"resource receipt changed frozen {name}")


@dataclass(frozen=True, slots=True)
class DecisionTelemetry:
    decisions: int
    p50_latency_ms: float | None
    p95_latency_ms: float | None
    p99_latency_ms: float | None
    search_nodes: int
    fallback_decisions: int
    trace_digest: Digest

    def __post_init__(self) -> None:
        _strict_int(self.decisions, "decision count")
        _strict_int(self.search_nodes, "search nodes")
        _strict_int(self.fallback_decisions, "fallback decisions")
        if self.fallback_decisions > self.decisions:
            raise ValueError("fallback decisions exceed all decisions")
        latencies = (self.p50_latency_ms, self.p95_latency_ms, self.p99_latency_ms)
        if self.decisions == 0:
            if any(item is not None for item in latencies):
                raise ValueError("zero-decision telemetry cannot report latency quantiles")
        else:
            if any(item is None or isinstance(item, bool) or not math.isfinite(item) or item < 0 for item in latencies):
                raise ValueError("decision latency quantiles must be finite and non-negative")
            if not self.p50_latency_ms <= self.p95_latency_ms <= self.p99_latency_ms:  # type: ignore[operator]
                raise ValueError("decision latency quantiles are not ordered")
        object.__setattr__(self, "trace_digest", _digest(self.trace_digest, "telemetry trace"))


class FaultKind(str, Enum):
    CRASH = "crash"
    TIMEOUT = "timeout"
    ILLEGAL_ACTION = "illegal_action"
    RESOURCE_OVERRUN = "resource_overrun"
    PROTOCOL = "protocol"
    INFRASTRUCTURE = "infrastructure"


@dataclass(frozen=True, slots=True)
class FaultAttribution:
    owner: Digest | str
    kind: FaultKind
    evidence_digest: Digest
    incident_digest: Digest
    hand_number: int | None = None
    decision_index: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, FaultKind):
            raise ValueError("unknown fault kind")
        if self.owner != "infrastructure":
            object.__setattr__(self, "owner", _digest(self.owner, "fault owner"))
        if (self.kind is FaultKind.INFRASTRUCTURE) != (self.owner == "infrastructure"):
            raise ValueError("infrastructure fault kind/owner mismatch")
        for name in ("evidence_digest", "incident_digest"):
            object.__setattr__(self, name, _digest(getattr(self, name), name))
        if self.hand_number is not None:
            _strict_int(self.hand_number, "fault hand", minimum=1, maximum=HANDS_PER_MATCH)
        if self.decision_index is not None:
            _strict_int(self.decision_index, "fault decision index")


@dataclass(frozen=True, slots=True)
class ReplayVerificationReceipt:
    """Pinned replay-verifier output, not a caller-supplied result boolean."""

    leg_plan_digest: Digest
    execution_binding_digest: Digest
    execution_binding_authority: str
    connection_identity_digests: tuple[Digest, Digest]
    run_ids_by_connection: tuple[Digest, Digest]
    process_tree_ids_by_connection: tuple[str, str]
    cgroup_paths_by_connection: tuple[str, str]
    resource_profile_digest: Digest
    decision_budget_ms: int
    platform_action_timeout_ms: int
    action_send_delay_ms: int
    verifier_digest: Digest
    issuer_digest: Digest
    rules_digest: Digest
    oracle_fixture_digest: Digest
    raw_wire_digest: Digest
    wire_semantics_verified: bool
    wire_semantic_binding_digest: Digest
    raw_replay_digest: Digest
    match_trace_digest: Digest
    verification_evidence_digest: Digest
    actual_dealt_prefix_digests: tuple[Digest, ...]
    verified_event_digests: tuple[Digest, ...]
    hands_started: int
    hands_played: int
    settlement_count: int
    net_chips_connection0: int
    timeout_count_by_connection: tuple[int, int]
    illegal_action_count_by_connection: tuple[int, int]
    decision_wait_ns_by_connection: tuple[tuple[int, ...], tuple[int, ...]]
    search_nodes_by_connection: tuple[int, int]
    fallback_decisions_by_connection: tuple[int, int]
    decision_trace_digest_by_connection: tuple[Digest, Digest]
    telemetry_complete_by_connection: tuple[bool, bool]
    adjudicated_fault: FaultAttribution | None
    result_finalized_epoch_ms: int | None
    hand70_evidence_digest: Digest | None
    supervisor_contract_digest: Digest | None = None
    supervisor_readiness_attestation_digest: Digest | None = None
    supervisor_launch_authorization_digest: Digest | None = None
    supervisor_leg_receipt_digest: Digest | None = None
    supervisor_attempt_journal_scope_digest: Digest | None = None
    supervisor_attempt_sequence: int | None = None
    supervisor_previous_attempt_entry_digest: Digest | None = None
    supervisor_leg_run_id: Digest | None = None
    supervisor_receipt_consumption_key: Digest | None = None
    supervisor_consumption_ledger_entry_digest: Digest | None = None
    supervisor_consumption_ledger_entry_inode: int | None = None
    supervisor_consumption_ledger_entry_path: str | None = None
    supervisor_control_session_digest: Digest | None = None
    supervisor_capture_session_digest: Digest | None = None
    supervisor_socket_identity_digests: tuple[Digest, Digest] | None = None
    supervisor_wire_semantic_digest: Digest | None = None
    supervisor_replay_digest: Digest | None = None
    supervisor_decision_trace_digest: Digest | None = None
    supervisor_fault_event_digest: Digest | None = None
    supervisor_termination_kinds: tuple[str, str] | None = None
    supervisor_cleanup_receipt_digest: Digest | None = None
    attestation_digest: Digest | None = None
    _verifier_token: object = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not hasattr(self, "_verifier_token"):
            object.__setattr__(self, "_verifier_token", None)
        for name in (
            "leg_plan_digest",
            "execution_binding_digest",
            "resource_profile_digest",
            "verifier_digest",
            "issuer_digest",
            "rules_digest",
            "oracle_fixture_digest",
            "raw_wire_digest",
            "wire_semantic_binding_digest",
            "raw_replay_digest",
            "match_trace_digest",
            "verification_evidence_digest",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), name))
        if self.execution_binding_authority not in {
            "development_diagnostic_only",
            "formal_enforcer_bound",
        }:
            raise ValueError("unknown replay execution binding authority")
        if type(self.wire_semantics_verified) is not bool:
            raise ValueError("wire semantics verification marker must be boolean")
        supervisor_fields = (
            "supervisor_contract_digest",
            "supervisor_readiness_attestation_digest",
            "supervisor_launch_authorization_digest",
            "supervisor_leg_receipt_digest",
            "supervisor_attempt_journal_scope_digest",
            "supervisor_attempt_sequence",
            "supervisor_previous_attempt_entry_digest",
            "supervisor_leg_run_id",
            "supervisor_receipt_consumption_key",
            "supervisor_consumption_ledger_entry_digest",
            "supervisor_consumption_ledger_entry_inode",
            "supervisor_consumption_ledger_entry_path",
            "supervisor_control_session_digest",
            "supervisor_capture_session_digest",
            "supervisor_socket_identity_digests",
            "supervisor_wire_semantic_digest",
            "supervisor_replay_digest",
            "supervisor_decision_trace_digest",
            "supervisor_fault_event_digest",
            "supervisor_termination_kinds",
            "supervisor_cleanup_receipt_digest",
        )
        if self.execution_binding_authority == "development_diagnostic_only":
            if any(getattr(self, name) is not None for name in supervisor_fields):
                raise ValueError(
                    "development replay receipt cannot carry formal supervisor claims"
                )
        else:
            missing = [name for name in supervisor_fields if getattr(self, name) is None]
            if missing:
                raise ValueError(
                    "formal replay receipt lacks signed supervisor fields: "
                    + ", ".join(missing)
                )
            for name in supervisor_fields:
                if name in {
                    "supervisor_socket_identity_digests",
                    "supervisor_termination_kinds",
                    "supervisor_attempt_sequence",
                    "supervisor_consumption_ledger_entry_inode",
                    "supervisor_consumption_ledger_entry_path",
                }:
                    continue
                object.__setattr__(
                    self,
                    name,
                    _digest(getattr(self, name), name.replace("_", " ")),
                )
            object.__setattr__(
                self,
                "supervisor_socket_identity_digests",
                tuple(
                    _digest(value, "supervisor socket identity")
                    for value in _two_items(
                        self.supervisor_socket_identity_digests,
                        "supervisor socket identities",
                    )
                ),
            )
            kinds = tuple(self.supervisor_termination_kinds or ())
            if len(kinds) != 2 or any(
                kind
                not in {
                    "normal",
                    "crash",
                    "timeout",
                    "resource",
                    "protocol",
                    "infrastructure",
                }
                for kind in kinds
            ):
                raise ValueError("formal replay receipt has invalid termination kinds")
            object.__setattr__(self, "supervisor_termination_kinds", kinds)
            _strict_int(
                self.supervisor_attempt_sequence,  # type: ignore[arg-type]
                "supervisor attempt sequence",
                minimum=1,
            )
            _strict_int(
                self.supervisor_consumption_ledger_entry_inode,  # type: ignore[arg-type]
                "supervisor consumption ledger inode",
                minimum=1,
            )
            ledger_path = self.supervisor_consumption_ledger_entry_path
            if (
                not isinstance(ledger_path, str)
                or not ledger_path.startswith("/")
                or "\x00" in ledger_path
            ):
                raise ValueError(
                    "formal replay receipt has invalid consumption ledger path"
                )
        identities = tuple(
            _digest(value, "replay connection identity")
            for value in _two_items(
                self.connection_identity_digests,
                "replay connection identities",
            )
        )
        runs = tuple(
            _digest(value, "replay connection run")
            for value in _two_items(
                self.run_ids_by_connection,
                "replay connection runs",
            )
        )
        processes = tuple(
            _nonempty_string(value, "replay process tree")
            for value in _two_items(
                self.process_tree_ids_by_connection,
                "replay process trees",
            )
        )
        cgroups = tuple(
            _nonempty_string(value, "replay cgroup")
            for value in _two_items(
                self.cgroup_paths_by_connection,
                "replay cgroups",
            )
        )
        if len(set(identities)) != 2 or len(set(runs)) != 2:
            raise ValueError("replay execution binding repeats identity or run")
        if len(set(processes)) != 2 or len(set(cgroups)) != 2:
            raise ValueError("replay execution binding lacks process/cgroup isolation")
        if any(not path.startswith("/sys/fs/cgroup/") for path in cgroups):
            raise ValueError("replay binding cgroups must be absolute cgroup-v2 paths")
        object.__setattr__(self, "connection_identity_digests", identities)
        object.__setattr__(self, "run_ids_by_connection", runs)
        object.__setattr__(self, "process_tree_ids_by_connection", processes)
        object.__setattr__(self, "cgroup_paths_by_connection", cgroups)
        _strict_int(self.decision_budget_ms, "replay decision budget", minimum=1, maximum=HARD_COMPUTE_STOP_MS)
        _strict_int(
            self.platform_action_timeout_ms,
            "replay platform action timeout",
            minimum=PLATFORM_ACTION_TIMEOUT_MS,
            maximum=PLATFORM_ACTION_TIMEOUT_MS,
        )
        _strict_int(self.action_send_delay_ms, "replay action send delay")
        if self.decision_budget_ms + self.action_send_delay_ms >= self.platform_action_timeout_ms:
            raise ValueError("replay compute budget plus send delay reaches platform timeout")
        deals = tuple(
            _digest(value, f"replay hand deal {index}")
            for index, value in enumerate(self.actual_dealt_prefix_digests)
        )
        events = tuple(_digest(value, "replay verified event") for value in self.verified_event_digests)
        if len(set(events)) != len(events):
            raise ValueError("replay verified events must be unique")
        object.__setattr__(self, "actual_dealt_prefix_digests", deals)
        object.__setattr__(self, "verified_event_digests", events)
        _strict_int(self.hands_started, "replay hands started", maximum=HANDS_PER_MATCH)
        _strict_int(self.hands_played, "replay hands played", maximum=HANDS_PER_MATCH)
        _strict_int(self.settlement_count, "replay settlement count", maximum=HANDS_PER_MATCH)
        if self.hands_played > self.hands_started or len(deals) != self.hands_started:
            raise ValueError("replay deal prefix and hand counters disagree")
        if self.settlement_count != self.hands_played:
            raise ValueError("local native replay settlements must equal completed hands")
        _strict_int(
            self.net_chips_connection0,
            "replay net chips",
            minimum=-HANDS_PER_MATCH * 20_000,
            maximum=HANDS_PER_MATCH * 20_000,
        )
        timeout_counts = _two_items(self.timeout_count_by_connection, "replay timeout counts")
        illegal_counts = _two_items(self.illegal_action_count_by_connection, "replay illegal counts")
        for name, counts in (("timeout", timeout_counts), ("illegal", illegal_counts)):
            for index, count in enumerate(counts):
                _strict_int(count, f"replay {name} count {index}")
        object.__setattr__(self, "timeout_count_by_connection", timeout_counts)
        object.__setattr__(self, "illegal_action_count_by_connection", illegal_counts)
        waits = tuple(
            tuple(
                _strict_int(value, f"decision wait {connection}/{index}")
                for index, value in enumerate(values)
            )
            for connection, values in enumerate(
                _two_items(
                    self.decision_wait_ns_by_connection,
                    "decision wait traces",
                )
            )
        )
        search_nodes = tuple(
            _strict_int(value, f"search nodes {connection}")
            for connection, value in enumerate(
                _two_items(self.search_nodes_by_connection, "search node totals")
            )
        )
        fallback_counts = tuple(
            _strict_int(value, f"fallback decisions {connection}")
            for connection, value in enumerate(
                _two_items(
                    self.fallback_decisions_by_connection,
                    "fallback decision totals",
                )
            )
        )
        traces = tuple(
            _digest(value, f"decision trace {connection}")
            for connection, value in enumerate(
                _two_items(
                    self.decision_trace_digest_by_connection,
                    "decision trace digests",
                )
            )
        )
        telemetry_complete = _two_items(
            self.telemetry_complete_by_connection,
            "telemetry completeness",
        )
        if any(type(value) is not bool for value in telemetry_complete):
            raise ValueError("telemetry completeness markers must be boolean")
        if any(fallback_counts[index] > len(waits[index]) for index in range(2)):
            raise ValueError("fallback decisions exceed replay-derived decisions")
        object.__setattr__(self, "decision_wait_ns_by_connection", waits)
        object.__setattr__(self, "search_nodes_by_connection", search_nodes)
        object.__setattr__(self, "fallback_decisions_by_connection", fallback_counts)
        object.__setattr__(self, "decision_trace_digest_by_connection", traces)
        object.__setattr__(
            self,
            "telemetry_complete_by_connection",
            telemetry_complete,
        )
        if self.result_finalized_epoch_ms is not None:
            _strict_int(self.result_finalized_epoch_ms, "result finalization time", minimum=1)
        if self.hands_played == HANDS_PER_MATCH and self.result_finalized_epoch_ms is None:
            raise ValueError("a complete 70-hand replay must bind result finalization")
        if self.hands_played < HANDS_PER_MATCH and self.result_finalized_epoch_ms is not None:
            raise ValueError("an incomplete replay cannot claim a finalized match result")
        if self.hand70_evidence_digest is not None:
            object.__setattr__(
                self,
                "hand70_evidence_digest",
                _digest(self.hand70_evidence_digest, "hand-70 evidence"),
            )
        if self.hands_played == HANDS_PER_MATCH and self.hand70_evidence_digest is None:
            raise ValueError("a complete match requires independent hand-70 evidence")
        if self.hands_played < HANDS_PER_MATCH and self.hand70_evidence_digest is not None:
            raise ValueError("an incomplete match cannot contain hand-70 evidence")
        if self.execution_binding_authority == "formal_enforcer_bound":
            if self.supervisor_replay_digest != self.raw_replay_digest:
                raise ValueError("signed supervisor replay digest differs from verified replay")
            if (
                self.supervisor_wire_semantic_digest
                != self.wire_semantic_binding_digest
            ):
                raise ValueError(
                    "signed supervisor wire semantics differ from replay verification"
                )
        expected_attestation = self.expected_attestation_digest()
        if self.attestation_digest is None:
            object.__setattr__(self, "attestation_digest", expected_attestation)
        else:
            object.__setattr__(self, "attestation_digest", _digest(self.attestation_digest, "replay attestation"))
            if self.attestation_digest != expected_attestation:
                raise ValueError("replay attestation does not bind its raw evidence and derived result")

    def expected_attestation_digest(self) -> Digest:
        payload = asdict(self)
        payload.pop("attestation_digest")
        payload.pop("_verifier_token")
        return _payload_digest(payload)

    def digest(self) -> Digest:
        payload = asdict(self)
        payload.pop("_verifier_token")
        return _payload_digest(payload)

    def derived_decision_telemetry(self, connection_index: int) -> DecisionTelemetry:
        _strict_int(connection_index, "telemetry connection", maximum=1)
        waits = self.decision_wait_ns_by_connection[connection_index]
        if not waits:
            quantiles: tuple[float | None, float | None, float | None] = (
                None,
                None,
                None,
            )
        else:
            ordered = sorted(waits)

            def nearest_rank_ms(probability: float) -> float:
                index = max(0, math.ceil(probability * len(ordered)) - 1)
                return ordered[index] / 1_000_000

            quantiles = (
                nearest_rank_ms(0.50),
                nearest_rank_ms(0.95),
                nearest_rank_ms(0.99),
            )
        return DecisionTelemetry(
            decisions=len(waits),
            p50_latency_ms=quantiles[0],
            p95_latency_ms=quantiles[1],
            p99_latency_ms=quantiles[2],
            search_nodes=self.search_nodes_by_connection[connection_index],
            fallback_decisions=(
                self.fallback_decisions_by_connection[connection_index]
            ),
            trace_digest=self.decision_trace_digest_by_connection[connection_index],
        )

    @classmethod
    def from_verified_native_replay(
        cls,
        *,
        leg_plan: LegPlan,
        verified_replay: object,
        issuer_digest: Digest,
        rules_digest: Digest,
        oracle_fixture_digest: Digest,
        adjudicated_fault: FaultAttribution | None,
    ) -> "ReplayVerificationReceipt":
        from .native_replay import (
            PartialFaultKind,
            VerifiedNativeReplay,
        )

        if not isinstance(verified_replay, VerifiedNativeReplay):
            raise ValueError("replay receipt requires typed native replay verification")
        verified_replay._assert_verified()
        if verified_replay.leg_plan_digest != leg_plan.digest():
            raise ValueError("verified replay belongs to a different LegPlan")
        if (
            verified_replay.connection_identity_digests
            != leg_plan.connection_to_identity
        ):
            raise ValueError("verified replay changed the ordered connection identities")
        partial = verified_replay.partial_fault
        if partial is None:
            if adjudicated_fault is not None:
                raise ValueError("clean verified replay cannot acquire an adjudicated fault")
        else:
            if adjudicated_fault is None:
                raise ValueError("partial verified replay requires matching adjudication")
            expected_kind = FaultKind(partial.kind.value)
            expected_owner: Digest | str = (
                "infrastructure"
                if partial.kind is PartialFaultKind.INFRASTRUCTURE
                else leg_plan.connection_to_identity[partial.owner_connection]
            )
            if (
                adjudicated_fault.kind is not expected_kind
                or adjudicated_fault.owner != expected_owner
                or adjudicated_fault.evidence_digest != partial.evidence_digest
                or adjudicated_fault.hand_number != partial.hand_number
            ):
                raise ValueError("partial replay fault differs from typed adjudication")
        receipt = cls(
            leg_plan_digest=leg_plan.digest(),
            execution_binding_digest=verified_replay.execution_binding_digest,
            execution_binding_authority=(
                verified_replay.execution_binding_authority
            ),
            connection_identity_digests=(
                verified_replay.connection_identity_digests
            ),
            run_ids_by_connection=verified_replay.run_ids_by_connection,
            process_tree_ids_by_connection=(
                verified_replay.process_tree_ids_by_connection
            ),
            cgroup_paths_by_connection=verified_replay.cgroup_paths_by_connection,
            resource_profile_digest=verified_replay.resource_profile_digest,
            decision_budget_ms=verified_replay.decision_budget_ms,
            platform_action_timeout_ms=(
                verified_replay.platform_action_timeout_ms
            ),
            action_send_delay_ms=verified_replay.action_send_delay_ms,
            verifier_digest=verified_replay.verifier_digest,
            issuer_digest=issuer_digest,
            rules_digest=rules_digest,
            oracle_fixture_digest=oracle_fixture_digest,
            raw_wire_digest=verified_replay.raw_wire_digest,
            wire_semantics_verified=verified_replay.wire_semantics_verified,
            wire_semantic_binding_digest=(
                verified_replay.wire_semantic_binding_digest
            ),
            raw_replay_digest=verified_replay.raw_replay_digest,
            match_trace_digest=verified_replay.match_trace_digest,
            verification_evidence_digest=verified_replay.verification_evidence_digest,
            actual_dealt_prefix_digests=verified_replay.actual_dealt_prefix_digests,
            verified_event_digests=verified_replay.verified_event_digests,
            hands_started=verified_replay.hands_started,
            hands_played=verified_replay.hands_played,
            settlement_count=verified_replay.settlement_count,
            net_chips_connection0=verified_replay.net_chips_connection0,
            timeout_count_by_connection=verified_replay.timeout_count_by_connection,
            illegal_action_count_by_connection=verified_replay.illegal_action_count_by_connection,
            decision_wait_ns_by_connection=(
                verified_replay.decision_wait_ns_by_connection
            ),
            search_nodes_by_connection=verified_replay.search_nodes_by_connection,
            fallback_decisions_by_connection=(
                verified_replay.fallback_decisions_by_connection
            ),
            decision_trace_digest_by_connection=(
                verified_replay.decision_trace_digest_by_connection
            ),
            telemetry_complete_by_connection=(
                verified_replay.telemetry_complete_by_connection
            ),
            adjudicated_fault=adjudicated_fault,
            result_finalized_epoch_ms=(
                verified_replay.result_finalized_epoch_ms
            ),
            hand70_evidence_digest=verified_replay.hand70_evidence_digest,
            supervisor_contract_digest=verified_replay.supervisor_contract_digest,
            supervisor_readiness_attestation_digest=verified_replay.supervisor_readiness_attestation_digest,
            supervisor_launch_authorization_digest=verified_replay.supervisor_launch_authorization_digest,
            supervisor_leg_receipt_digest=verified_replay.supervisor_leg_receipt_digest,
            supervisor_attempt_journal_scope_digest=verified_replay.supervisor_attempt_journal_scope_digest,
            supervisor_attempt_sequence=verified_replay.supervisor_attempt_sequence,
            supervisor_previous_attempt_entry_digest=verified_replay.supervisor_previous_attempt_entry_digest,
            supervisor_leg_run_id=verified_replay.supervisor_leg_run_id,
            supervisor_receipt_consumption_key=verified_replay.supervisor_receipt_consumption_key,
            supervisor_consumption_ledger_entry_digest=verified_replay.supervisor_consumption_ledger_entry_digest,
            supervisor_consumption_ledger_entry_inode=verified_replay.supervisor_consumption_ledger_entry_inode,
            supervisor_consumption_ledger_entry_path=verified_replay.supervisor_consumption_ledger_entry_path,
            supervisor_control_session_digest=verified_replay.supervisor_control_session_digest,
            supervisor_capture_session_digest=verified_replay.supervisor_capture_session_digest,
            supervisor_socket_identity_digests=verified_replay.supervisor_socket_identity_digests,
            supervisor_wire_semantic_digest=verified_replay.supervisor_wire_semantic_digest,
            supervisor_replay_digest=verified_replay.supervisor_replay_digest,
            supervisor_decision_trace_digest=verified_replay.supervisor_decision_trace_digest,
            supervisor_fault_event_digest=verified_replay.supervisor_fault_event_digest,
            supervisor_termination_kinds=verified_replay.supervisor_termination_kinds,
            supervisor_cleanup_receipt_digest=verified_replay.supervisor_cleanup_receipt_digest,
        )
        sealed_attestation = receipt.attestation_digest

        def issued_instance(
            candidate: object,
            owner: object = receipt,
            sealed: Digest | None = sealed_attestation,
        ) -> bool:
            return (
                candidate is owner
                and isinstance(candidate, ReplayVerificationReceipt)
                and candidate.attestation_digest == sealed
                and candidate.expected_attestation_digest() == sealed
            )

        object.__setattr__(receipt, "_verifier_token", issued_instance)
        return receipt

    def _assert_formal_verifier_authority(self) -> None:
        guard = self._verifier_token
        if not callable(guard) or guard(self) is not True:
            raise ValueError(
                "replay receipt was not issued by the pinned native replay verifier "
                "or was copied/altered"
            )
        if self.execution_binding_authority != "formal_enforcer_bound":
            raise ValueError(
                "formal replay was not bound to enforcer-issued execution/resource receipts"
            )
        if any(
            getattr(self, name) is None
            for name in (
                "supervisor_contract_digest",
                "supervisor_readiness_attestation_digest",
                "supervisor_launch_authorization_digest",
                "supervisor_leg_receipt_digest",
                "supervisor_attempt_journal_scope_digest",
                "supervisor_attempt_sequence",
                "supervisor_previous_attempt_entry_digest",
                "supervisor_leg_run_id",
                "supervisor_receipt_consumption_key",
                "supervisor_consumption_ledger_entry_digest",
                "supervisor_consumption_ledger_entry_inode",
                "supervisor_consumption_ledger_entry_path",
                "supervisor_control_session_digest",
                "supervisor_capture_session_digest",
                "supervisor_socket_identity_digests",
                "supervisor_wire_semantic_digest",
                "supervisor_replay_digest",
                "supervisor_decision_trace_digest",
                "supervisor_fault_event_digest",
                "supervisor_termination_kinds",
                "supervisor_cleanup_receipt_digest",
            )
        ):
            raise ValueError(
                "formal replay lacks an externally signed post-run supervisor binding"
            )
        if not self.wire_semantics_verified:
            raise ValueError(
                "formal replay requires byte-derived structured wire semantics"
            )
        if self.telemetry_complete_by_connection != (True, True):
            raise ValueError(
                "formal replay requires complete trusted worker decision telemetry"
            )


@dataclass(frozen=True, slots=True)
class MatchObservation:
    """Replay-verified output for one leg, including two per-bot receipts."""

    leg_plan: LegPlan
    execution_receipts: tuple[ExecutionReceipt, ExecutionReceipt]
    resource_receipts: tuple[ResourceReceipt, ResourceReceipt]
    replay_receipt: ReplayVerificationReceipt
    actual_dealt_prefix_digests: tuple[Digest, ...]
    actual_replay_digest: Digest
    match_trace_digest: Digest
    verified_event_digests: tuple[Digest, ...]
    hands_started: int
    hands_played: int
    net_chips_connection0: int
    match_wall_elapsed_ms: int
    telemetry_by_connection: tuple[DecisionTelemetry, DecisionTelemetry]
    timeout_count_by_connection: tuple[int, int] = (0, 0)
    illegal_action_count_by_connection: tuple[int, int] = (0, 0)
    terminal_fault: FaultAttribution | None = None
    retry_of_observation_digest: Digest | None = None

    def __post_init__(self) -> None:
        executions = _two_items(self.execution_receipts, "execution receipts")
        resources = _two_items(self.resource_receipts, "resource receipts")
        telemetry = _two_items(self.telemetry_by_connection, "decision telemetry")
        if tuple(item.connection_index for item in executions) != (0, 1):
            raise ValueError("execution receipts must be ordered by connection")
        if tuple(item.connection_index for item in resources) != (0, 1):
            raise ValueError("resource receipts must be ordered by connection")
        if len({item.run_id for item in executions}) != 2 or len({item.digest() for item in executions}) != 2:
            raise ValueError("each bot execution requires a unique run receipt")
        if len({item.process_tree_id for item in executions}) != 2:
            raise ValueError("each bot requires an isolated process tree")
        if len({item.cgroup_path for item in executions}) != 2:
            raise ValueError("each bot requires an isolated cgroup")
        if len({item.digest() for item in resources}) != 2:
            raise ValueError("each bot execution requires a unique resource receipt")
        object.__setattr__(self, "execution_receipts", executions)
        object.__setattr__(self, "resource_receipts", resources)
        object.__setattr__(self, "telemetry_by_connection", telemetry)
        if max(item.started_epoch_ms for item in resources) >= min(
            item.finished_epoch_ms for item in resources
        ):
            raise ValueError("the two isolated bot executions did not overlap in one match")

        for index, execution in enumerate(executions):
            if execution.leg_plan_digest != self.leg_plan.digest():
                raise ValueError("execution receipt escaped the immutable leg plan")
            if execution.identity_digest != self.leg_plan.connection_to_identity[index]:
                raise ValueError("execution identity differs from the planned connection")
            resource = resources[index]
            if resource.execution_receipt_digest != execution.digest():
                raise ValueError("resource receipt is not bound to this execution")
            if resource.connection_index != index or resource.identity_digest != execution.identity_digest:
                raise ValueError("resource receipt connection/identity mismatch")
        if self.replay_receipt.leg_plan_digest != self.leg_plan.digest():
            raise ValueError("replay receipt escaped the immutable leg plan")
        if (
            self.replay_receipt.connection_identity_digests
            != self.leg_plan.connection_to_identity
            or self.replay_receipt.run_ids_by_connection
            != tuple(item.run_id for item in executions)
            or self.replay_receipt.process_tree_ids_by_connection
            != tuple(item.process_tree_id for item in executions)
            or self.replay_receipt.cgroup_paths_by_connection
            != tuple(item.cgroup_path for item in executions)
        ):
            raise ValueError(
                "replay connection/session binding differs from the concrete executions"
            )
        if (
            self.replay_receipt.supervisor_termination_kinds is not None
            and self.replay_receipt.supervisor_termination_kinds
            != tuple(item.termination_kind.value for item in executions)
        ):
            raise ValueError(
                "signed supervisor termination kinds differ from execution receipts"
            )
        # Digest inequality is not provenance: canonical replay bytes are
        # legitimately also the match trace, while unrelated evidence can be
        # made unequal trivially.  Typed receipts and their explicit
        # cross-links below establish domains; formal supervisor signatures
        # are checked by the formal aggregation path.

        prefix = tuple(_digest(item, "actual hand deal") for item in self.actual_dealt_prefix_digests)
        object.__setattr__(self, "actual_dealt_prefix_digests", prefix)
        for name in ("actual_replay_digest", "match_trace_digest"):
            object.__setattr__(self, name, _digest(getattr(self, name), name))
        events = tuple(_digest(item, "verified event") for item in self.verified_event_digests)
        if len(set(events)) != len(events):
            raise ValueError("verified event evidence must be unique")
        object.__setattr__(self, "verified_event_digests", events)
        _strict_int(self.hands_started, "hands started", maximum=HANDS_PER_MATCH)
        _strict_int(self.hands_played, "hands played", maximum=HANDS_PER_MATCH)
        if self.hands_played > self.hands_started or len(prefix) != self.hands_started:
            raise ValueError("deal prefix, started hands, and completed hands disagree")
        _strict_int(self.net_chips_connection0, "net chips", minimum=-HANDS_PER_MATCH * 20_000, maximum=HANDS_PER_MATCH * 20_000)
        _strict_int(self.match_wall_elapsed_ms, "match wall time", minimum=1)
        timeout_counts = self._validate_counts(self.timeout_count_by_connection, "timeout")
        illegal_counts = self._validate_counts(self.illegal_action_count_by_connection, "illegal action")
        object.__setattr__(self, "timeout_count_by_connection", timeout_counts)
        object.__setattr__(self, "illegal_action_count_by_connection", illegal_counts)
        if (
            prefix != self.replay_receipt.actual_dealt_prefix_digests
            or self.actual_replay_digest != self.replay_receipt.raw_replay_digest
            or self.match_trace_digest != self.replay_receipt.match_trace_digest
            or events != self.replay_receipt.verified_event_digests
            or self.hands_started != self.replay_receipt.hands_started
            or self.hands_played != self.replay_receipt.hands_played
            or self.net_chips_connection0 != self.replay_receipt.net_chips_connection0
            or timeout_counts != self.replay_receipt.timeout_count_by_connection
            or illegal_counts != self.replay_receipt.illegal_action_count_by_connection
            or self.terminal_fault != self.replay_receipt.adjudicated_fault
        ):
            raise ValueError("observation result fields differ from the pinned replay-verifier receipt")
        replay_telemetry = tuple(
            self.replay_receipt.derived_decision_telemetry(index)
            for index in range(2)
        )
        if telemetry != replay_telemetry:
            raise ValueError(
                "decision telemetry differs from replay-derived timing/search evidence"
            )
        if self.retry_of_observation_digest is not None:
            object.__setattr__(
                self,
                "retry_of_observation_digest",
                _digest(self.retry_of_observation_digest, "retry observation"),
            )

        # Profile-dependent and plan-dependent checks are deliberately completed
        # by ``verify_against_plan``; construction alone is never formal proof.
        fault = self.terminal_fault
        candidate_digests = set(self.leg_plan.connection_to_identity)
        if fault is not None:
            if fault.owner not in candidate_digests | {"infrastructure"}:
                raise ValueError("terminal fault owner is outside this leg")
            if fault.hand_number is not None and fault.hand_number > self.hands_started:
                raise ValueError("fault hand lies beyond the replay-verified deal prefix")
            if fault.owner == "infrastructure":
                if fault.evidence_digest not in events:
                    raise ValueError("infrastructure fault lacks a replay-verified event")
            else:
                owner_index = self.leg_plan.connection_to_identity.index(fault.owner)
                if fault.decision_index is not None and fault.decision_index >= telemetry[owner_index].decisions:
                    raise ValueError("fault decision lies beyond verified candidate telemetry")
                if fault.kind in {FaultKind.TIMEOUT, FaultKind.ILLEGAL_ACTION}:
                    expected_evidence = events
                elif fault.kind in {FaultKind.CRASH, FaultKind.PROTOCOL}:
                    expected_evidence = (executions[owner_index].termination_evidence_digest,)
                elif fault.kind is FaultKind.RESOURCE_OVERRUN:
                    expected_evidence = (resources[owner_index].raw_evidence_digest,)
                else:
                    expected_evidence = ()
                if fault.evidence_digest not in expected_evidence:
                    raise ValueError("terminal fault evidence does not match its owner and kind")
        if self.hands_played != HANDS_PER_MATCH and fault is None:
            raise ValueError("an incomplete match requires a terminal fault")

        self._verify_fault_bijection(timeout_counts, illegal_counts)

    @property
    def supervisor_contract_digest(self) -> Digest | None:
        return self.replay_receipt.supervisor_contract_digest

    @property
    def supervisor_readiness_attestation_digest(self) -> Digest | None:
        return self.replay_receipt.supervisor_readiness_attestation_digest

    @property
    def supervisor_launch_authorization_digest(self) -> Digest | None:
        return self.replay_receipt.supervisor_launch_authorization_digest

    @property
    def supervisor_leg_receipt_digest(self) -> Digest | None:
        return self.replay_receipt.supervisor_leg_receipt_digest

    @property
    def supervisor_attempt_journal_scope_digest(self) -> Digest | None:
        return self.replay_receipt.supervisor_attempt_journal_scope_digest

    @property
    def supervisor_attempt_sequence(self) -> int | None:
        return self.replay_receipt.supervisor_attempt_sequence

    @property
    def supervisor_previous_attempt_entry_digest(self) -> Digest | None:
        return self.replay_receipt.supervisor_previous_attempt_entry_digest

    @property
    def supervisor_leg_run_id(self) -> Digest | None:
        return self.replay_receipt.supervisor_leg_run_id

    @property
    def supervisor_receipt_consumption_key(self) -> Digest | None:
        return self.replay_receipt.supervisor_receipt_consumption_key

    @property
    def supervisor_consumption_ledger_entry_digest(self) -> Digest | None:
        return self.replay_receipt.supervisor_consumption_ledger_entry_digest

    @property
    def supervisor_consumption_ledger_entry_inode(self) -> int | None:
        return self.replay_receipt.supervisor_consumption_ledger_entry_inode

    @property
    def supervisor_consumption_ledger_entry_path(self) -> str | None:
        return self.replay_receipt.supervisor_consumption_ledger_entry_path

    @property
    def supervisor_control_session_digest(self) -> Digest | None:
        return self.replay_receipt.supervisor_control_session_digest

    @property
    def supervisor_capture_session_digest(self) -> Digest | None:
        return self.replay_receipt.supervisor_capture_session_digest

    @property
    def supervisor_socket_identity_digests(self) -> tuple[Digest, Digest] | None:
        return self.replay_receipt.supervisor_socket_identity_digests

    @property
    def supervisor_wire_semantic_digest(self) -> Digest | None:
        return self.replay_receipt.supervisor_wire_semantic_digest

    @property
    def supervisor_replay_digest(self) -> Digest | None:
        return self.replay_receipt.supervisor_replay_digest

    @property
    def supervisor_decision_trace_digest(self) -> Digest | None:
        return self.replay_receipt.supervisor_decision_trace_digest

    @property
    def supervisor_fault_event_digest(self) -> Digest | None:
        return self.replay_receipt.supervisor_fault_event_digest

    @property
    def supervisor_cleanup_receipt_digest(self) -> Digest | None:
        return self.replay_receipt.supervisor_cleanup_receipt_digest

    @property
    def supervisor_termination_kinds(self) -> tuple[str, str] | None:
        return self.replay_receipt.supervisor_termination_kinds

    @staticmethod
    def _validate_counts(counts: Sequence[int], name: str) -> tuple[int, int]:
        normalized = _two_items(counts, f"{name} counts")
        for index, count in enumerate(normalized):
            _strict_int(count, f"{name} count {index}")
        return normalized  # type: ignore[return-value]

    def _verify_fault_bijection(
        self,
        timeout_counts: tuple[int, int],
        illegal_counts: tuple[int, int],
    ) -> None:
        fault = self.terminal_fault
        timeout_connections = {index for index, count in enumerate(timeout_counts) if count}
        illegal_connections = {index for index, count in enumerate(illegal_counts) if count}
        if len(timeout_connections) > 1 or len(illegal_connections) > 1:
            raise ValueError("one leg cannot charge the same terminal fault to both candidates")
        if timeout_connections and illegal_connections:
            raise ValueError("one leg cannot have two candidate terminal-fault kinds")
        if timeout_connections:
            index = next(iter(timeout_connections))
            if fault is None or fault.kind is not FaultKind.TIMEOUT or fault.owner != self.leg_plan.connection_to_identity[index]:
                raise ValueError("timeout counters and terminal fault are not bijective")
        elif fault is not None and fault.kind is FaultKind.TIMEOUT:
            raise ValueError("TIMEOUT fault requires a non-zero timeout counter")
        if illegal_connections:
            index = next(iter(illegal_connections))
            if fault is None or fault.kind is not FaultKind.ILLEGAL_ACTION or fault.owner != self.leg_plan.connection_to_identity[index]:
                raise ValueError("illegal-action counters and terminal fault are not bijective")
        elif fault is not None and fault.kind is FaultKind.ILLEGAL_ACTION:
            raise ValueError("ILLEGAL_ACTION fault requires a non-zero illegal counter")

    def verify_against_plan(self, plan: FormalEvaluationPlan) -> None:
        if plan.result_authority == "formal_strength":
            self.replay_receipt._assert_formal_verifier_authority()
            if (
                self.replay_receipt.supervisor_contract_digest
                != plan.infrastructure_monitor_digest
            ):
                raise ValueError(
                    "signed supervisor contract differs from the frozen infrastructure monitor"
                )
        if self.leg_plan.formal_plan_digest != plan.digest():
            raise ValueError("observation belongs to a different formal plan")
        block = plan.block(self.leg_plan.block_id)
        expected_leg = LegPlan.from_plan(plan, block, self.leg_plan.leg_index)
        if self.leg_plan != expected_leg:
            raise ValueError("observation leg differs from its preregistered LegPlan")
        if self.replay_receipt.verifier_digest != plan.replay_verifier_digest:
            raise ValueError("replay receipt verifier differs from the frozen evaluator")
        if self.replay_receipt.issuer_digest != plan.stratum.harness_digest:
            raise ValueError("replay receipt issuer differs from the frozen harness")
        if self.replay_receipt.rules_digest != plan.stratum.rules_digest:
            raise ValueError("replay receipt rules differ from the stratum")
        if self.replay_receipt.oracle_fixture_digest != plan.oracle_fixture_digest:
            raise ValueError("replay receipt oracle fixtures differ from the formal plan")
        if (
            self.replay_receipt.resource_profile_digest
            != plan.resource_profile.digest()
            or self.replay_receipt.decision_budget_ms
            != plan.resource_profile.decision_budget_ms
            or self.replay_receipt.platform_action_timeout_ms
            != plan.resource_profile.platform_action_timeout_ms
            or self.replay_receipt.action_send_delay_ms
            != plan.resource_profile.action_send_delay_ms
        ):
            raise ValueError("replay timing/resource binding differs from the formal plan")
        if self.actual_dealt_prefix_digests != block.deal_sequence.hand_deal_digests[: self.hands_started]:
            raise ValueError("actual replay deal prefix differs from the frozen 70-deal sequence")

        resource_overrun_connections: set[int] = set()
        termination_faults: list[tuple[int, FaultKind | str]] = []
        for index, (execution, resource) in enumerate(zip(self.execution_receipts, self.resource_receipts, strict=True)):
            if plan.result_authority == "formal_strength":
                execution._assert_formal_enforcer_authority()
                resource._assert_formal_enforcer_authority()
            artifact = plan.artifact(execution.identity_digest)
            if execution.launch_contract_digest != artifact.launch_contract_digest:
                raise ValueError("execution launch contract differs from the sealed artifact")
            if execution.launch_command_digest != artifact.launch_command_digest:
                raise ValueError("execution command differs from the sealed artifact")
            if execution.base_environment_digest != artifact.base_environment_digest:
                raise ValueError("execution base environment differs from the sealed artifact")
            if execution.thread_environment_digest != plan.resource_profile.thread_environment_digest:
                raise ValueError("execution thread environment differs from the resource profile")
            if execution.verifier_digest != plan.stratum.harness_digest or execution.issuer_digest != plan.stratum.harness_digest:
                raise ValueError("execution receipt verifier differs from the frozen harness")
            block_seed = block.policy_seed(execution.identity_digest)
            if execution.actual_policy_seed != block_seed:
                raise ValueError("actual policy RNG seed followed connection rather than identity")
            resource.verify(execution=execution, profile=plan.resource_profile)
            if resource.deadline_kill_count and not self.timeout_count_by_connection[index]:
                raise ValueError("deadline-kill evidence lacks a timeout counter")
            if resource.has_resource_overrun():
                resource_overrun_connections.add(index)
            kind_map: dict[TerminationKind, FaultKind | str | None] = {
                TerminationKind.NORMAL: None,
                TerminationKind.CRASH: FaultKind.CRASH,
                TerminationKind.TIMEOUT: FaultKind.TIMEOUT,
                TerminationKind.RESOURCE: FaultKind.RESOURCE_OVERRUN,
                TerminationKind.PROTOCOL: FaultKind.PROTOCOL,
                TerminationKind.INFRASTRUCTURE: "infrastructure",
            }
            mapped = kind_map[execution.termination_kind]
            if mapped is not None:
                termination_faults.append((index, mapped))

        if len(resource_overrun_connections) > 1:
            raise ValueError("both candidates exceeded resources; sample cannot assign one loss")
        fault = self.terminal_fault
        if resource_overrun_connections:
            index = next(iter(resource_overrun_connections))
            if fault is None or fault.kind is not FaultKind.RESOURCE_OVERRUN or fault.owner != self.leg_plan.connection_to_identity[index]:
                raise ValueError("resource measurements and terminal fault are not bijective")
        elif fault is not None and fault.kind is FaultKind.RESOURCE_OVERRUN:
            raise ValueError("RESOURCE_OVERRUN fault requires measured enforcement evidence")

        for index, mapped in termination_faults:
            if mapped == "infrastructure":
                if fault is None or fault.owner != "infrastructure":
                    raise ValueError("infrastructure termination lacks infrastructure attribution")
            else:
                expected_owner = self.leg_plan.connection_to_identity[index]
                if fault is None or fault.kind is not mapped or fault.owner != expected_owner:
                    raise ValueError("execution termination and terminal fault are not bijective")
        if fault is not None and fault.kind in {FaultKind.CRASH, FaultKind.PROTOCOL}:
            expected = TerminationKind.CRASH if fault.kind is FaultKind.CRASH else TerminationKind.PROTOCOL
            if not any(
                execution.termination_kind is expected and execution.identity_digest == fault.owner
                for execution in self.execution_receipts
            ):
                raise ValueError(f"{fault.kind.value} fault lacks matching execution termination")

        thermal_or_preempted = any(
            item.thermal_event or item.host_preemption_event for item in self.resource_receipts
        )
        if thermal_or_preempted and (fault is None or fault.owner != "infrastructure"):
            raise ValueError("thermal/preemption evidence must invalidate the leg as infrastructure")
        if fault is None:
            if self.hands_started != HANDS_PER_MATCH or self.hands_played != HANDS_PER_MATCH:
                raise ValueError("a clean strength sample must complete all 70 hands")
            if any(item.termination_kind is not TerminationKind.NORMAL for item in self.execution_receipts):
                raise ValueError("a clean sample has abnormal process termination")
            if self.replay_receipt.settlement_count != HANDS_PER_MATCH:
                raise ValueError("a clean local strength sample requires all 70 settlements")
        if self.match_wall_elapsed_ms > plan.resource_profile.match_wall_timeout_ms and fault is None:
            raise ValueError("match wall timeout lacks typed attribution")
        shared_execution_started = max(item.started_epoch_ms for item in self.resource_receipts)
        shared_execution_finished = min(item.finished_epoch_ms for item in self.resource_receipts)
        if self.match_wall_elapsed_ms > shared_execution_finished - shared_execution_started:
            raise ValueError("reported match wall time exceeds the shared bot execution interval")
        finalized = self.replay_receipt.result_finalized_epoch_ms
        if finalized is not None and not (
            max(item.started_epoch_ms for item in self.resource_receipts)
            <= finalized
            <= min(item.finished_epoch_ms for item in self.resource_receipts)
        ):
            raise ValueError("replay result finalized outside the overlapping bot executions")

    def input_digest(self) -> Digest:
        """Fields that an infrastructure retry is forbidden to change."""

        return _payload_digest(
            {
                "leg_plan": asdict(self.leg_plan),
                "connection_identities": self.leg_plan.connection_to_identity,
                "execution_launch_contracts": tuple(item.launch_contract_digest for item in self.execution_receipts),
                "execution_launch_commands": tuple(item.launch_command_digest for item in self.execution_receipts),
                "execution_launch_environments": tuple(item.launch_environment_digest for item in self.execution_receipts),
                "actual_policy_seeds": tuple(item.actual_policy_seed for item in self.execution_receipts),
                "profile_digests": tuple(item.profile_digest for item in self.resource_receipts),
            }
        )

    def observation_digest(self) -> Digest:
        # ``dataclasses.asdict(self)`` recursively includes the callable
        # capability guards embedded in formal execution/resource/replay
        # receipts.  Besides being non-serializable, those process-local
        # closures are not evidence.  Bind each typed receipt through its
        # canonical content digest and serialize only public observation
        # fields here.
        return _payload_digest(
            {
                "schema": "pok-match-observation-v2",
                "leg_plan_digest": self.leg_plan.digest(),
                "execution_receipt_digests": tuple(
                    item.digest() for item in self.execution_receipts
                ),
                "resource_receipt_digests": tuple(
                    item.digest() for item in self.resource_receipts
                ),
                "replay_receipt_digest": self.replay_receipt.digest(),
                "actual_dealt_prefix_digests": self.actual_dealt_prefix_digests,
                "actual_replay_digest": self.actual_replay_digest,
                "match_trace_digest": self.match_trace_digest,
                "verified_event_digests": self.verified_event_digests,
                "hands_started": self.hands_started,
                "hands_played": self.hands_played,
                "net_chips_connection0": self.net_chips_connection0,
                "match_wall_elapsed_ms": self.match_wall_elapsed_ms,
                "telemetry_by_connection": tuple(
                    asdict(item) for item in self.telemetry_by_connection
                ),
                "timeout_count_by_connection": self.timeout_count_by_connection,
                "illegal_action_count_by_connection": (
                    self.illegal_action_count_by_connection
                ),
                "terminal_fault": (
                    None if self.terminal_fault is None else asdict(self.terminal_fault)
                ),
                "retry_of_observation_digest": self.retry_of_observation_digest,
            }
        )

    def candidate_score(self, identity_digest: Digest) -> float:
        identity_digest = _digest(identity_digest, "score identity")
        if identity_digest not in self.leg_plan.connection_to_identity:
            raise KeyError(identity_digest)
        if self.terminal_fault is not None and self.terminal_fault.owner == "infrastructure":
            raise ValueError("unresolved infrastructure failure is not a strength sample")
        if self.terminal_fault is not None:
            return 0.0 if self.terminal_fault.owner == identity_digest else 1.0
        net = self.net_chips_connection0
        if self.leg_plan.connection_to_identity[0] != identity_digest:
            net = -net
        return outcome_score(net)


class InfrastructureFailureDomain(str, Enum):
    HARNESS = "harness"
    HOST_PREEMPTION = "host_preemption"
    THERMAL = "thermal"
    PLATFORM = "platform"
    NETWORK = "network"


@dataclass(frozen=True, slots=True)
class FormalInfrastructureAttributionBinding:
    """Content projection of the two external capabilities behind a retry.

    This value is evidence, not authority.  Authority remains process-local in
    the exact :class:`AuthorizedSupervisorLeg` and closed
    :class:`AuthorizedSupervisorAttemptJournal` instances retained by
    :class:`InfrastructureAttributionReceipt`.
    """

    schema: str
    formal_plan_digest: Digest
    observation_digest: Digest
    leg_plan_digest: Digest
    supervisor_contract_digest: Digest
    supervisor_readiness_attestation_digest: Digest
    supervisor_launch_authorization_digest: Digest
    supervisor_leg_receipt_digest: Digest
    supervisor_attempt_journal_scope_digest: Digest
    supervisor_attempt_journal_seal_digest: Digest
    supervisor_attempt_journal_entry_digest: Digest
    supervisor_attempt_sequence: int
    supervisor_previous_attempt_entry_digest: Digest
    supervisor_leg_run_id: Digest
    supervisor_receipt_consumption_key: Digest
    supervisor_consumption_ledger_entry_digest: Digest
    supervisor_consumption_ledger_entry_inode: int
    supervisor_consumption_ledger_entry_path: str
    supervisor_control_session_digest: Digest
    supervisor_capture_session_digest: Digest
    supervisor_socket_identity_digests: tuple[Digest, Digest]
    supervisor_raw_wire_digest: Digest
    supervisor_wire_semantic_digest: Digest
    supervisor_replay_digest: Digest
    replay_verification_digest: Digest
    replay_attestation_digest: Digest
    supervisor_decision_trace_digest: Digest
    supervisor_fault_trace_digest: Digest
    supervisor_fault_event_digests: tuple[Digest, ...]
    supervisor_fault_close_monotonic_ns: tuple[int, ...]
    supervisor_termination_kinds: tuple[str, str]
    supervisor_cleanup_receipt_digest: Digest
    run_ids_by_connection: tuple[Digest, Digest]
    execution_receipt_digests: tuple[Digest, Digest]
    execution_raw_evidence_digests: tuple[Digest, Digest]
    execution_termination_evidence_digests: tuple[Digest, Digest]
    resource_receipt_digests: tuple[Digest, Digest]
    resource_raw_evidence_digests: tuple[Digest, Digest]
    resource_intervals_epoch_ms: tuple[tuple[int, int], tuple[int, int]]
    resource_thermal_events: tuple[bool, bool]
    resource_host_preemption_events: tuple[bool, bool]
    fault_evidence_digest: Digest
    observation_fault_incident_digest: Digest
    derived_failure_domain: InfrastructureFailureDomain
    derived_fault_epoch_ms: int
    derived_affected_run_ids: tuple[Digest, ...]
    derived_incident_attestation_digest: Digest

    def __post_init__(self) -> None:
        if self.schema != "pok-formal-infrastructure-attribution-binding-v1":
            raise ValueError("unknown formal infrastructure binding schema")
        scalar_digests = (
            "formal_plan_digest",
            "observation_digest",
            "leg_plan_digest",
            "supervisor_contract_digest",
            "supervisor_readiness_attestation_digest",
            "supervisor_launch_authorization_digest",
            "supervisor_leg_receipt_digest",
            "supervisor_attempt_journal_scope_digest",
            "supervisor_attempt_journal_seal_digest",
            "supervisor_attempt_journal_entry_digest",
            "supervisor_previous_attempt_entry_digest",
            "supervisor_leg_run_id",
            "supervisor_receipt_consumption_key",
            "supervisor_consumption_ledger_entry_digest",
            "supervisor_control_session_digest",
            "supervisor_capture_session_digest",
            "supervisor_raw_wire_digest",
            "supervisor_wire_semantic_digest",
            "supervisor_replay_digest",
            "replay_verification_digest",
            "replay_attestation_digest",
            "supervisor_decision_trace_digest",
            "supervisor_fault_trace_digest",
            "supervisor_cleanup_receipt_digest",
            "fault_evidence_digest",
            "observation_fault_incident_digest",
            "derived_incident_attestation_digest",
        )
        for name in scalar_digests:
            object.__setattr__(self, name, _digest(getattr(self, name), name))
        pair_digest_fields = (
            "supervisor_socket_identity_digests",
            "run_ids_by_connection",
            "execution_receipt_digests",
            "execution_raw_evidence_digests",
            "execution_termination_evidence_digests",
            "resource_receipt_digests",
            "resource_raw_evidence_digests",
        )
        for name in pair_digest_fields:
            object.__setattr__(
                self,
                name,
                tuple(
                    _digest(value, name)
                    for value in _two_items(getattr(self, name), name)
                ),
            )
        events = tuple(
            _digest(value, "supervisor infrastructure fault event")
            for value in self.supervisor_fault_event_digests
        )
        if not events or len(set(events)) != len(events):
            raise ValueError(
                "formal infrastructure binding requires unique signed fault events"
            )
        object.__setattr__(self, "supervisor_fault_event_digests", events)
        fault_times = tuple(
            _strict_int(value, "signed infrastructure fault time", minimum=1)
            for value in self.supervisor_fault_close_monotonic_ns
        )
        if len(fault_times) != len(events) or tuple(sorted(fault_times)) != fault_times:
            raise ValueError("signed infrastructure fault event times are not ordered")
        object.__setattr__(self, "supervisor_fault_close_monotonic_ns", fault_times)
        _strict_int(
            self.supervisor_attempt_sequence,
            "supervisor infrastructure attempt sequence",
            minimum=1,
        )
        _strict_int(
            self.supervisor_consumption_ledger_entry_inode,
            "supervisor infrastructure consumption ledger inode",
            minimum=1,
        )
        path = self.supervisor_consumption_ledger_entry_path
        if not isinstance(path, str) or not path.startswith("/") or "\x00" in path:
            raise ValueError("formal infrastructure ledger path must be absolute")
        kinds = tuple(self.supervisor_termination_kinds)
        if len(kinds) != 2 or any(
            value
            not in {
                "normal",
                "crash",
                "timeout",
                "resource",
                "protocol",
                "infrastructure",
            }
            for value in kinds
        ):
            raise ValueError("formal infrastructure termination facts are invalid")
        object.__setattr__(self, "supervisor_termination_kinds", kinds)
        intervals = tuple(tuple(value) for value in self.resource_intervals_epoch_ms)
        if len(intervals) != 2:
            raise ValueError("formal infrastructure binding requires two resource intervals")
        for index, interval in enumerate(intervals):
            if len(interval) != 2:
                raise ValueError(f"resource interval {index} is malformed")
            start = _strict_int(interval[0], f"resource interval {index} start")
            finish = _strict_int(interval[1], f"resource interval {index} finish")
            if finish <= start:
                raise ValueError(f"resource interval {index} is not positive")
        object.__setattr__(self, "resource_intervals_epoch_ms", intervals)
        for name in (
            "resource_thermal_events",
            "resource_host_preemption_events",
        ):
            values = _two_items(getattr(self, name), name)
            if any(type(value) is not bool for value in values):
                raise ValueError(f"{name} must contain booleans")
            object.__setattr__(self, name, values)
        if not isinstance(self.derived_failure_domain, InfrastructureFailureDomain):
            raise ValueError("formal infrastructure binding has unknown failure domain")
        _strict_int(self.derived_fault_epoch_ms, "derived infrastructure fault time", minimum=1)
        affected = tuple(
            _digest(value, "derived affected run")
            for value in self.derived_affected_run_ids
        )
        if not affected or len(set(affected)) != len(affected):
            raise ValueError("formal infrastructure binding requires unique affected runs")
        if not set(affected) <= set(self.run_ids_by_connection):
            raise ValueError("formal infrastructure binding names an unrelated run")
        object.__setattr__(self, "derived_affected_run_ids", affected)

    def digest(self) -> Digest:
        return _payload_digest(asdict(self))


def _derive_formal_infrastructure_attribution_binding(
    *,
    plan: FormalEvaluationPlan,
    observation: MatchObservation,
    authorized_supervisor_leg: object,
    authorized_attempt_journal: object,
) -> FormalInfrastructureAttributionBinding:
    """Reverify and project the exact external evidence for one failed Leg."""

    from .resource_enforcer import (
        AuthorizedSupervisorAttemptJournal,
        AuthorizedSupervisorLeg,
    )

    if not isinstance(plan, FormalEvaluationPlan):
        raise ValueError("formal infrastructure attribution requires a typed plan")
    if plan.result_authority != "formal_strength":
        raise ValueError(
            "formal infrastructure attribution cannot be issued for a diagnostic plan"
        )
    plan._assert_formal_authority()
    if not isinstance(observation, MatchObservation):
        raise ValueError(
            "formal infrastructure attribution requires a typed original observation"
        )
    if not isinstance(authorized_supervisor_leg, AuthorizedSupervisorLeg):
        raise ValueError(
            "formal infrastructure attribution requires the exact AuthorizedSupervisorLeg"
        )
    if not isinstance(
        authorized_attempt_journal, AuthorizedSupervisorAttemptJournal
    ):
        raise ValueError(
            "formal infrastructure attribution requires an authorized closed attempt journal"
        )
    authorized_supervisor_leg._assert_authorized()
    authorized_attempt_journal._assert_authorized()
    observation.verify_against_plan(plan)

    fault = observation.terminal_fault
    if (
        fault is None
        or fault.owner != "infrastructure"
        or fault.kind is not FaultKind.INFRASTRUCTURE
    ):
        raise ValueError(
            "formal infrastructure attribution requires a replay-verified infrastructure failure"
        )
    if (
        observation.hands_played == HANDS_PER_MATCH
        or observation.replay_receipt.result_finalized_epoch_ms is not None
    ):
        raise ValueError("a completed result cannot be discarded as infrastructure")

    bridge = authorized_supervisor_leg
    journal = authorized_attempt_journal
    replay = observation.replay_receipt
    plan_digest = plan.digest()
    leg_digest = observation.leg_plan.digest()
    replay_attestation = replay.attestation_digest
    if replay_attestation is None:
        raise ValueError("formal infrastructure replay lacks verifier attestation")
    if bridge.supervisor_contract_digest != plan.infrastructure_monitor_digest:
        raise ValueError(
            "signed infrastructure supervisor differs from the frozen plan monitor"
        )

    expected_bridge_fields = {
        "leg_plan_digest": leg_digest,
        "supervisor_contract_digest": replay.supervisor_contract_digest,
        "readiness_attestation_digest": replay.supervisor_readiness_attestation_digest,
        "launch_authorization_digest": replay.supervisor_launch_authorization_digest,
        "supervisor_leg_receipt_digest": replay.supervisor_leg_receipt_digest,
        "attempt_journal_scope_digest": replay.supervisor_attempt_journal_scope_digest,
        "attempt_sequence": replay.supervisor_attempt_sequence,
        "previous_attempt_entry_digest": replay.supervisor_previous_attempt_entry_digest,
        "leg_run_id": replay.supervisor_leg_run_id,
        "receipt_consumption_key": replay.supervisor_receipt_consumption_key,
        "consumption_ledger_entry_digest": replay.supervisor_consumption_ledger_entry_digest,
        "consumption_ledger_entry_inode": replay.supervisor_consumption_ledger_entry_inode,
        "consumption_ledger_entry_path": replay.supervisor_consumption_ledger_entry_path,
        "control_session_digest": replay.supervisor_control_session_digest,
        "capture_session_digest": replay.supervisor_capture_session_digest,
        "raw_wire_digest": replay.raw_wire_digest,
        "wire_semantic_digest": replay.supervisor_wire_semantic_digest,
        "replay_digest": replay.raw_replay_digest,
        "decision_trace_digest": replay.supervisor_decision_trace_digest,
        "supervisor_fault_event_digest": replay.supervisor_fault_event_digest,
        "termination_kinds": replay.supervisor_termination_kinds,
        "cleanup_receipt_digest": replay.supervisor_cleanup_receipt_digest,
    }
    for name, value in expected_bridge_fields.items():
        if value is None or getattr(bridge, name) != value:
            raise ValueError(
                f"authorized supervisor leg differs from verified observation: {name}"
            )
    socket_digests = tuple(item.digest() for item in bridge.socket_identities)
    if socket_digests != replay.supervisor_socket_identity_digests:
        raise ValueError(
            "authorized supervisor socket identities differ from verified replay"
        )

    connections = tuple(bridge.connections)
    if len(connections) != 2 or tuple(
        item.connection_index for item in connections
    ) != (0, 1):
        raise ValueError("authorized supervisor leg lacks two ordered connections")
    executions = observation.execution_receipts
    resources = observation.resource_receipts
    for index, (connection, execution, resource) in enumerate(
        zip(connections, executions, resources, strict=True)
    ):
        expected_connection = {
            "leg_plan_digest": leg_digest,
            "leg_run_id": bridge.leg_run_id,
            "profile_digest": plan.resource_profile.digest(),
            "identity_digest": observation.leg_plan.connection_to_identity[index],
            "run_id": execution.run_id,
            "cgroup_path": execution.cgroup_path,
            "cgroup_inode": resource.cgroup_inode,
            "started_epoch_ms": resource.started_epoch_ms,
            "finished_epoch_ms": resource.finished_epoch_ms,
            "termination_kind": execution.termination_kind.value,
        }
        if any(
            getattr(connection, name) != value
            for name, value in expected_connection.items()
        ):
            raise ValueError(
                "authorized supervisor connection differs from formal receipt "
                f"at connection {index}"
            )
        if (
            connection.execution_raw_evidence_digest()
            != execution.raw_evidence_digest
            or connection.termination_evidence_digest()
            != execution.termination_evidence_digest
            or connection.resource_raw_evidence_digest()
            != resource.raw_evidence_digest
        ):
            raise ValueError(
                "authorized supervisor raw execution/resource facts differ from observation"
            )

    if (
        journal.scope_digest != bridge.attempt_journal_scope_digest
        or journal.contract_digest != bridge.supervisor_contract_digest
    ):
        raise ValueError(
            "closed attempt journal belongs to another supervisor contract/scope"
        )
    matching_rows = tuple(
        entry
        for entry in journal.entries
        if entry.attempt_sequence == bridge.attempt_sequence
    )
    if len(matching_rows) != 1:
        raise ValueError(
            "closed attempt journal lacks the exact supervisor attempt row"
        )
    row = matching_rows[0]
    if row.terminal_state != "infrastructure_failed":
        raise ValueError(
            "formal replay-bearing retry requires an infrastructure_failed journal row"
        )
    expected_row_fields = {
        "contract_digest": bridge.supervisor_contract_digest,
        "readiness_attestation_digest": bridge.readiness_attestation_digest,
        "control_session_digest": bridge.control_session_digest,
        "attempt_journal_scope_digest": bridge.attempt_journal_scope_digest,
        "attempt_sequence": bridge.attempt_sequence,
        "previous_attempt_entry_digest": bridge.previous_attempt_entry_digest,
        "launch_authorization_digest": bridge.launch_authorization_digest,
        "leg_plan_digest": bridge.leg_plan_digest,
        "leg_run_id": bridge.leg_run_id,
        "supervisor_leg_receipt_digest": bridge.supervisor_leg_receipt_digest,
        "capture_session_digest": bridge.capture_session_digest,
        "receipt_consumption_key": bridge.receipt_consumption_key,
        "cleanup_receipt_digest": bridge.cleanup_receipt_digest,
        "raw_wire_digest": bridge.raw_wire_digest,
        "wire_semantic_digest": bridge.wire_semantic_digest,
        "replay_digest": bridge.replay_digest,
        "replay_verification_digest": replay.verification_evidence_digest,
    }
    for name, value in expected_row_fields.items():
        if getattr(row, name) != value:
            raise ValueError(
                f"closed attempt journal row differs from failed observation: {name}"
            )

    fault_events = tuple(bridge.supervisor_fault_events)
    if not fault_events or any(
        event.fault_kind != "infrastructure"
        or event.fault_connection_index is not None
        or event.capture_session_digest != bridge.capture_session_digest
        for event in fault_events
    ):
        raise ValueError(
            "formal infrastructure failure lacks exact uncharged signed fault events"
        )
    if tuple(
        event for event in bridge.decision_events if event.fault_kind != "none"
    ) != fault_events:
        raise ValueError(
            "authorized supervisor fault-event projection is incomplete"
        )
    terminal_event = fault_events[-1]
    if (
        fault.hand_number != terminal_event.hand_index + 1
        or fault.decision_index != terminal_event.decision_index
    ):
        raise ValueError(
            "replay infrastructure fault location differs from signed decision event"
        )

    thermal_indexes = {
        index for index, resource in enumerate(resources) if resource.thermal_event
    }
    preemption_indexes = {
        index
        for index, resource in enumerate(resources)
        if resource.host_preemption_event
    }
    infrastructure_indexes = {
        index
        for index, execution in enumerate(executions)
        if execution.termination_kind is TerminationKind.INFRASTRUCTURE
    }
    if thermal_indexes and preemption_indexes:
        raise ValueError(
            "formal infrastructure failure has ambiguous thermal/preemption domains"
        )
    if thermal_indexes:
        domain = InfrastructureFailureDomain.THERMAL
        affected_indexes = thermal_indexes
    elif preemption_indexes:
        domain = InfrastructureFailureDomain.HOST_PREEMPTION
        affected_indexes = preemption_indexes
    elif infrastructure_indexes:
        domain = InfrastructureFailureDomain.HARNESS
        affected_indexes = infrastructure_indexes
    else:
        raise ValueError(
            "signed facts do not derive a supported infrastructure failure domain"
        )
    affected_run_ids = tuple(executions[index].run_id for index in sorted(affected_indexes))
    # Decision evidence uses CLOCK_MONOTONIC while the signed process/resource
    # interval uses epoch time.  Do not invent a clock conversion: use the
    # earliest signed affected-process terminal boundary as the epoch marker,
    # and retain every exact monotonic close separately below.
    fault_epoch_ms = min(resources[index].finished_epoch_ms for index in affected_indexes)
    if any(
        not resources[index].started_epoch_ms
        <= fault_epoch_ms
        <= resources[index].finished_epoch_ms
        for index in affected_indexes
    ):
        raise ValueError(
            "derived infrastructure terminal boundary escapes an affected run"
        )

    event_digests = tuple(event.digest() for event in fault_events)
    fault_times = tuple(event.decision_close_monotonic_ns for event in fault_events)
    execution_digests = tuple(item.digest() for item in executions)
    resource_digests = tuple(item.digest() for item in resources)
    incident_digest = _payload_digest(
        {
            "schema": "pok-formal-infrastructure-incident-v1",
            "formal_plan_digest": plan_digest,
            "leg_plan_digest": leg_digest,
            "supervisor_contract_digest": bridge.supervisor_contract_digest,
            "supervisor_leg_receipt_digest": bridge.supervisor_leg_receipt_digest,
            "attempt_journal_entry_digest": row.payload_digest(),
            "replay_verification_digest": replay.verification_evidence_digest,
            "supervisor_fault_trace_digest": bridge.supervisor_fault_event_digest,
            "supervisor_fault_event_digests": event_digests,
            "supervisor_fault_close_monotonic_ns": fault_times,
            "fault_evidence_digest": fault.evidence_digest,
            "observation_fault_incident_digest": fault.incident_digest,
            "derived_failure_domain": domain.value,
            "derived_fault_epoch_ms": fault_epoch_ms,
            "derived_affected_run_ids": affected_run_ids,
            "execution_receipt_digests": execution_digests,
            "resource_receipt_digests": resource_digests,
        }
    )
    return FormalInfrastructureAttributionBinding(
        schema="pok-formal-infrastructure-attribution-binding-v1",
        formal_plan_digest=plan_digest,
        observation_digest=observation.observation_digest(),
        leg_plan_digest=leg_digest,
        supervisor_contract_digest=bridge.supervisor_contract_digest,
        supervisor_readiness_attestation_digest=bridge.readiness_attestation_digest,
        supervisor_launch_authorization_digest=bridge.launch_authorization_digest,
        supervisor_leg_receipt_digest=bridge.supervisor_leg_receipt_digest,
        supervisor_attempt_journal_scope_digest=bridge.attempt_journal_scope_digest,
        supervisor_attempt_journal_seal_digest=journal.seal_digest,
        supervisor_attempt_journal_entry_digest=row.payload_digest(),
        supervisor_attempt_sequence=bridge.attempt_sequence,
        supervisor_previous_attempt_entry_digest=bridge.previous_attempt_entry_digest,
        supervisor_leg_run_id=bridge.leg_run_id,
        supervisor_receipt_consumption_key=bridge.receipt_consumption_key,
        supervisor_consumption_ledger_entry_digest=bridge.consumption_ledger_entry_digest,
        supervisor_consumption_ledger_entry_inode=bridge.consumption_ledger_entry_inode,
        supervisor_consumption_ledger_entry_path=bridge.consumption_ledger_entry_path,
        supervisor_control_session_digest=bridge.control_session_digest,
        supervisor_capture_session_digest=bridge.capture_session_digest,
        supervisor_socket_identity_digests=socket_digests,  # type: ignore[arg-type]
        supervisor_raw_wire_digest=bridge.raw_wire_digest,
        supervisor_wire_semantic_digest=bridge.wire_semantic_digest,
        supervisor_replay_digest=bridge.replay_digest,
        replay_verification_digest=replay.verification_evidence_digest,
        replay_attestation_digest=replay_attestation,
        supervisor_decision_trace_digest=bridge.decision_trace_digest,
        supervisor_fault_trace_digest=bridge.supervisor_fault_event_digest,
        supervisor_fault_event_digests=event_digests,
        supervisor_fault_close_monotonic_ns=fault_times,
        supervisor_termination_kinds=bridge.termination_kinds,
        supervisor_cleanup_receipt_digest=bridge.cleanup_receipt_digest,
        run_ids_by_connection=tuple(item.run_id for item in executions),  # type: ignore[arg-type]
        execution_receipt_digests=execution_digests,  # type: ignore[arg-type]
        execution_raw_evidence_digests=tuple(
            item.raw_evidence_digest for item in executions
        ),  # type: ignore[arg-type]
        execution_termination_evidence_digests=tuple(
            item.termination_evidence_digest for item in executions
        ),  # type: ignore[arg-type]
        resource_receipt_digests=resource_digests,  # type: ignore[arg-type]
        resource_raw_evidence_digests=tuple(
            item.raw_evidence_digest for item in resources
        ),  # type: ignore[arg-type]
        resource_intervals_epoch_ms=tuple(
            (item.started_epoch_ms, item.finished_epoch_ms) for item in resources
        ),  # type: ignore[arg-type]
        resource_thermal_events=tuple(
            item.thermal_event for item in resources
        ),  # type: ignore[arg-type]
        resource_host_preemption_events=tuple(
            item.host_preemption_event for item in resources
        ),  # type: ignore[arg-type]
        fault_evidence_digest=fault.evidence_digest,
        observation_fault_incident_digest=fault.incident_digest,
        derived_failure_domain=domain,
        derived_fault_epoch_ms=fault_epoch_ms,
        derived_affected_run_ids=affected_run_ids,
        derived_incident_attestation_digest=incident_digest,
    )


@dataclass(frozen=True, slots=True)
class InfrastructureAttributionReceipt:
    """Independent monitor record required before an infrastructure retry.

    ``create`` deliberately emits development-only evidence.  Formal evidence
    can only be issued from the exact signed supervisor-leg capability and its
    exact closed attempt-journal capability.  The process-local guard keeps a
    copied or directly constructed record from acquiring retry authority.
    """

    original_observation_digest: Digest
    incident_digest: Digest
    monitor_digest: Digest
    raw_monitor_evidence_digest: Digest
    verifier_digest: Digest
    failure_domain: InfrastructureFailureDomain
    fault_epoch_ms: int
    affected_run_ids: tuple[Digest, ...]
    result_was_unavailable: bool
    authority: str
    attestation_digest: Digest
    formal_binding: FormalInfrastructureAttributionBinding | None = None
    _authority_guard: object = field(init=False, repr=False, compare=False)
    _authorized_supervisor_leg: object = field(init=False, repr=False, compare=False)
    _authorized_attempt_journal: object = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not hasattr(self, "_authority_guard"):
            object.__setattr__(self, "_authority_guard", None)
        if not hasattr(self, "_authorized_supervisor_leg"):
            object.__setattr__(self, "_authorized_supervisor_leg", None)
        if not hasattr(self, "_authorized_attempt_journal"):
            object.__setattr__(self, "_authorized_attempt_journal", None)
        for name in (
            "original_observation_digest",
            "incident_digest",
            "monitor_digest",
            "raw_monitor_evidence_digest",
            "verifier_digest",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), name))
        if not isinstance(self.failure_domain, InfrastructureFailureDomain):
            raise ValueError("unknown infrastructure failure domain")
        _strict_int(self.fault_epoch_ms, "infrastructure fault time", minimum=1)
        run_ids = tuple(_digest(value, "affected infrastructure run") for value in self.affected_run_ids)
        if not run_ids or len(set(run_ids)) != len(run_ids):
            raise ValueError("infrastructure attribution requires unique affected runs")
        object.__setattr__(self, "affected_run_ids", run_ids)
        if type(self.result_was_unavailable) is not bool or not self.result_was_unavailable:
            raise ValueError("an available completed result cannot be relabeled as infrastructure")
        if self.authority not in {
            "development_diagnostic_only",
            "formal_signed_supervisor_and_closed_journal",
        }:
            raise ValueError("unknown infrastructure attribution authority")
        if self.authority == "development_diagnostic_only" and self.formal_binding is not None:
            raise ValueError(
                "development infrastructure attribution cannot carry formal supervisor evidence"
            )
        if self.authority == "formal_signed_supervisor_and_closed_journal" and not isinstance(
            self.formal_binding, FormalInfrastructureAttributionBinding
        ):
            raise ValueError(
                "formal infrastructure attribution requires a typed supervisor/journal binding"
            )
        if self.formal_binding is not None:
            binding = self.formal_binding
            if (
                self.original_observation_digest != binding.observation_digest
                or self.incident_digest != binding.observation_fault_incident_digest
                or self.monitor_digest != binding.supervisor_contract_digest
                or self.verifier_digest != binding.supervisor_contract_digest
                or self.raw_monitor_evidence_digest != binding.digest()
                or self.failure_domain is not binding.derived_failure_domain
                or self.fault_epoch_ms != binding.derived_fault_epoch_ms
                or self.affected_run_ids != binding.derived_affected_run_ids
            ):
                raise ValueError(
                    "formal infrastructure receipt differs from its derived signed binding"
                )
        object.__setattr__(self, "attestation_digest", _digest(self.attestation_digest, "infrastructure attestation"))
        if self.attestation_digest != self.expected_attestation_digest():
            raise ValueError("infrastructure attestation does not bind its canonical record")

    @classmethod
    def create(
        cls,
        *,
        original_observation_digest: Digest,
        incident_digest: Digest,
        monitor_digest: Digest,
        raw_monitor_evidence_digest: Digest,
        verifier_digest: Digest,
        failure_domain: InfrastructureFailureDomain,
        fault_epoch_ms: int,
        affected_run_ids: tuple[Digest, ...],
        result_was_unavailable: bool,
    ) -> "InfrastructureAttributionReceipt":
        normalized_runs = tuple(_digest(value, "affected infrastructure run") for value in affected_run_ids)
        constructor = {
            "original_observation_digest": _digest(original_observation_digest, "original observation"),
            "incident_digest": _digest(incident_digest, "incident"),
            "monitor_digest": _digest(monitor_digest, "monitor"),
            "raw_monitor_evidence_digest": _digest(raw_monitor_evidence_digest, "monitor evidence"),
            "verifier_digest": _digest(verifier_digest, "monitor verifier"),
            "failure_domain": failure_domain,
            "fault_epoch_ms": fault_epoch_ms,
            "affected_run_ids": normalized_runs,
            "result_was_unavailable": result_was_unavailable,
            "authority": "development_diagnostic_only",
            "formal_binding": None,
        }
        digest_payload = cls._attestation_payload(**constructor)
        return cls(**constructor, attestation_digest=_payload_digest(digest_payload))  # type: ignore[arg-type]

    @classmethod
    def from_authorized_supervisor_failure(
        cls,
        *,
        plan: FormalEvaluationPlan,
        original_observation: MatchObservation,
        authorized_supervisor_leg: object,
        authorized_attempt_journal: object,
    ) -> "InfrastructureAttributionReceipt":
        """Issue formal retry evidence without accepting incident facts.

        The failure domain, time, and affected run set are derived from the
        signed leg and resource facts.  The observation's incident identity is
        copied exactly (never accepted as a factory argument) and is included
        in a separate canonical incident attestation with the signed fault and
        journal facts.  Thus an arbitrary label grants no authority while the
        observation and retry receipt cannot silently name different incidents.
        The closed journal remains independently required by the final matrix
        ledger to prove global attempt completeness.
        """

        binding = _derive_formal_infrastructure_attribution_binding(
            plan=plan,
            observation=original_observation,
            authorized_supervisor_leg=authorized_supervisor_leg,
            authorized_attempt_journal=authorized_attempt_journal,
        )
        constructor = {
            "original_observation_digest": binding.observation_digest,
            "incident_digest": binding.observation_fault_incident_digest,
            "monitor_digest": binding.supervisor_contract_digest,
            "raw_monitor_evidence_digest": binding.digest(),
            "verifier_digest": binding.supervisor_contract_digest,
            "failure_domain": binding.derived_failure_domain,
            "fault_epoch_ms": binding.derived_fault_epoch_ms,
            "affected_run_ids": binding.derived_affected_run_ids,
            "result_was_unavailable": True,
            "authority": "formal_signed_supervisor_and_closed_journal",
            "formal_binding": binding,
        }
        digest_payload = cls._attestation_payload(**constructor)
        receipt = cls(
            **constructor,
            attestation_digest=_payload_digest(digest_payload),
        )
        sealed_attestation = receipt.attestation_digest
        sealed_binding = binding.digest()

        def issued(
            candidate: object,
            observation: object,
            candidate_plan: object,
            owner: object = receipt,
            source_observation: object = original_observation,
            source_plan: object = plan,
            source_leg: object = authorized_supervisor_leg,
            source_journal: object = authorized_attempt_journal,
            expected_attestation: Digest = sealed_attestation,
            expected_binding: Digest = sealed_binding,
        ) -> bool:
            return (
                candidate is owner
                and observation is source_observation
                and candidate_plan is source_plan
                and getattr(candidate, "attestation_digest", None)
                == expected_attestation
                and getattr(candidate, "formal_binding", None) is binding
                and binding.digest() == expected_binding
                and getattr(candidate, "expected_attestation_digest", lambda: None)()
                == expected_attestation
                and getattr(candidate, "_authorized_supervisor_leg", None)
                is source_leg
                and getattr(candidate, "_authorized_attempt_journal", None)
                is source_journal
            )

        object.__setattr__(receipt, "_authority_guard", issued)
        object.__setattr__(
            receipt, "_authorized_supervisor_leg", authorized_supervisor_leg
        )
        object.__setattr__(
            receipt, "_authorized_attempt_journal", authorized_attempt_journal
        )
        return receipt

    @staticmethod
    def _attestation_payload(
        *,
        original_observation_digest: Digest,
        incident_digest: Digest,
        monitor_digest: Digest,
        raw_monitor_evidence_digest: Digest,
        verifier_digest: Digest,
        failure_domain: InfrastructureFailureDomain,
        fault_epoch_ms: int,
        affected_run_ids: tuple[Digest, ...],
        result_was_unavailable: bool,
        authority: str,
        formal_binding: FormalInfrastructureAttributionBinding | None,
    ) -> dict:
        return {
            "original_observation_digest": original_observation_digest,
            "incident_digest": incident_digest,
            "monitor_digest": monitor_digest,
            "raw_monitor_evidence_digest": raw_monitor_evidence_digest,
            "verifier_digest": verifier_digest,
            "failure_domain": failure_domain.value,
            "fault_epoch_ms": fault_epoch_ms,
            "affected_run_ids": affected_run_ids,
            "result_was_unavailable": result_was_unavailable,
            "authority": authority,
            "formal_binding_digest": (
                None if formal_binding is None else formal_binding.digest()
            ),
        }

    def expected_attestation_digest(self) -> Digest:
        return _payload_digest(
            self._attestation_payload(
                original_observation_digest=self.original_observation_digest,
                incident_digest=self.incident_digest,
                monitor_digest=self.monitor_digest,
                raw_monitor_evidence_digest=self.raw_monitor_evidence_digest,
                verifier_digest=self.verifier_digest,
                failure_domain=self.failure_domain,
                fault_epoch_ms=self.fault_epoch_ms,
                affected_run_ids=self.affected_run_ids,
                result_was_unavailable=self.result_was_unavailable,
                authority=self.authority,
                formal_binding=self.formal_binding,
            )
        )

    def verify(self, observation: MatchObservation, plan: FormalEvaluationPlan) -> None:
        if plan.result_authority == "formal_strength":
            if self.authority != "formal_signed_supervisor_and_closed_journal":
                raise ValueError(
                    "formal infrastructure retry requires a fixed external signed "
                    "supervisor leg and closed attempt journal"
                )
            guard = self._authority_guard
            if not callable(guard) or guard(self, observation, plan) is not True:
                raise ValueError(
                    "formal infrastructure attribution was copied, forged, or is "
                    "being applied to a different observation/plan"
                )
            binding = _derive_formal_infrastructure_attribution_binding(
                plan=plan,
                observation=observation,
                authorized_supervisor_leg=self._authorized_supervisor_leg,
                authorized_attempt_journal=self._authorized_attempt_journal,
            )
            if self.formal_binding != binding or self.raw_monitor_evidence_digest != binding.digest():
                raise ValueError(
                    "formal infrastructure attribution differs from reverified external evidence"
                )
            return
        if self.authority != "development_diagnostic_only":
            raise ValueError(
                "formal supervisor attribution cannot authorize a diagnostic plan"
            )
        fault = observation.terminal_fault
        if fault is None or fault.owner != "infrastructure":
            raise ValueError("attribution receipt does not reference an infrastructure failure")
        if self.original_observation_digest != observation.observation_digest():
            raise ValueError("attribution receipt references a different observation")
        if self.incident_digest != fault.incident_digest:
            raise ValueError("attribution incident differs from the observation")
        if self.monitor_digest != plan.infrastructure_monitor_digest or self.verifier_digest != plan.infrastructure_monitor_digest:
            raise ValueError("infrastructure attribution did not use the frozen independent monitor")
        if self.raw_monitor_evidence_digest == fault.evidence_digest:
            raise ValueError("independent attribution cannot reuse the failing event as its only evidence")
        actual_runs = {item.run_id for item in observation.execution_receipts}
        if not set(self.affected_run_ids) <= actual_runs:
            raise ValueError("infrastructure attribution names a run outside the failed leg")
        intervals = {
            execution.run_id: (
                observation.resource_receipts[index].started_epoch_ms,
                observation.resource_receipts[index].finished_epoch_ms,
            )
            for index, execution in enumerate(observation.execution_receipts)
        }
        if any(
            not intervals[run_id][0] <= self.fault_epoch_ms <= intervals[run_id][1]
            for run_id in self.affected_run_ids
        ):
            raise ValueError("infrastructure incident time lies outside an affected run")
        if observation.hands_played == HANDS_PER_MATCH or observation.replay_receipt.result_finalized_epoch_ms is not None:
            raise ValueError("a completed result cannot be discarded as infrastructure")
        affected_indexes = [
            index
            for index, execution in enumerate(observation.execution_receipts)
            if execution.run_id in self.affected_run_ids
        ]
        if self.failure_domain is InfrastructureFailureDomain.THERMAL and not any(
            observation.resource_receipts[index].thermal_event for index in affected_indexes
        ):
            raise ValueError("thermal attribution lacks a thermal resource event")
        if self.failure_domain is InfrastructureFailureDomain.HOST_PREEMPTION and not any(
            observation.resource_receipts[index].host_preemption_event for index in affected_indexes
        ):
            raise ValueError("host-preemption attribution lacks a preemption resource event")
        if self.failure_domain is InfrastructureFailureDomain.HARNESS and not any(
            observation.execution_receipts[index].termination_kind is TerminationKind.INFRASTRUCTURE
            for index in affected_indexes
        ):
            raise ValueError("harness attribution lacks an infrastructure termination event")


@dataclass(frozen=True, slots=True)
class RetryLedgerEntry:
    original: MatchObservation
    retry: MatchObservation
    infrastructure_attribution: InfrastructureAttributionReceipt

    def __post_init__(self) -> None:
        if self.original.terminal_fault is None or self.original.terminal_fault.owner != "infrastructure":
            raise ValueError("only an independently attributed infrastructure failure may be retried")
        if self.retry.retry_of_observation_digest != self.original.observation_digest():
            raise ValueError("retry does not reference the retained original observation")
        if self.retry.leg_plan != self.original.leg_plan or self.retry.input_digest() != self.original.input_digest():
            raise ValueError("infrastructure retry changed the immutable leg or launch inputs")
        if min(item.started_epoch_ms for item in self.retry.resource_receipts) <= max(
            item.finished_epoch_ms for item in self.original.resource_receipts
        ):
            raise ValueError("infrastructure retry must start after the retained failed attempt")

    def digest(self) -> Digest:
        return _payload_digest(
            {
                "original_observation_digest": self.original.observation_digest(),
                "retry_observation_digest": self.retry.observation_digest(),
                "infrastructure_attribution_digest": self.infrastructure_attribution.attestation_digest,
            }
        )


@dataclass(frozen=True, slots=True)
class RetryLedger:
    entries: tuple[RetryLedgerEntry, ...] = ()

    def __post_init__(self) -> None:
        entries = tuple(sorted(self.entries, key=lambda entry: entry.original.observation_digest()))
        original_digests = [entry.original.observation_digest() for entry in entries]
        retry_digests = [entry.retry.observation_digest() for entry in entries]
        if len(set(original_digests)) != len(original_digests):
            raise ValueError("retry ledger forks one infrastructure failure")
        if len(set(retry_digests)) != len(retry_digests):
            raise ValueError("retry observation is reused")
        if set(original_digests) & set(retry_digests):
            # A chain legitimately shares retry[i] as original[i+1]; those are
            # checked below.  Anything else is a cycle or hidden fork.
            allowed = {
                entry.retry.observation_digest()
                for entry in entries
                if entry.retry.terminal_fault is not None and entry.retry.terminal_fault.owner == "infrastructure"
            }
            if (set(original_digests) & set(retry_digests)) - allowed:
                raise ValueError("only an infrastructure retry may continue a retry chain")
        object.__setattr__(self, "entries", entries)
        self._assert_acyclic()

    def _assert_acyclic(self) -> None:
        by_original = {entry.original.observation_digest(): entry for entry in self.entries}
        for start in by_original:
            seen: set[Digest] = set()
            current = start
            while current in by_original:
                if current in seen:
                    raise ValueError("retry ledger contains a cycle")
                seen.add(current)
                current = by_original[current].retry.observation_digest()

    def lineage_for(self, terminal: MatchObservation) -> tuple[RetryLedgerEntry, ...]:
        by_retry = {entry.retry.observation_digest(): entry for entry in self.entries}
        lineage: list[RetryLedgerEntry] = []
        current = terminal
        seen: set[Digest] = set()
        while current.retry_of_observation_digest is not None:
            current_digest = current.observation_digest()
            if current_digest in seen:
                raise ValueError("retry lineage contains a cycle")
            seen.add(current_digest)
            entry = by_retry.get(current_digest)
            if entry is None or entry.retry != current:
                raise ValueError("retry lineage is absent from the retained ledger")
            lineage.append(entry)
            current = entry.original
        lineage.reverse()
        return tuple(lineage)

    def digest(self) -> Digest:
        return _payload_digest(tuple(entry.digest() for entry in self.entries))


@dataclass(frozen=True, slots=True)
class PairedBlock:
    first: MatchObservation
    swapped: MatchObservation

    def __post_init__(self) -> None:
        if self.first.leg_plan.block_id != self.swapped.leg_plan.block_id:
            raise ValueError("paired observations must share a frozen block")
        if self.first.leg_plan.leg_index != 0 or self.swapped.leg_plan.leg_index != 1:
            raise ValueError("paired block requires canonical first and swapped leg indices")
        if self.first.leg_plan.formal_plan_digest != self.swapped.leg_plan.formal_plan_digest:
            raise ValueError("paired observations changed formal plan")
        if self.first.leg_plan.stratum_digest != self.swapped.leg_plan.stratum_digest:
            raise ValueError("paired observations changed evaluation stratum")
        if self.first.leg_plan.block_plan_digest != self.swapped.leg_plan.block_plan_digest:
            raise ValueError("paired observations changed block plan")
        if self.first.leg_plan.deal_sequence_digest != self.swapped.leg_plan.deal_sequence_digest:
            raise ValueError("paired observations changed the complete 70-deal sequence")
        if self.first.leg_plan.connection_to_identity != tuple(reversed(self.swapped.leg_plan.connection_to_identity)):
            raise ValueError("the swapped match did not exchange connection/seat mapping")
        if self.first.observation_digest() == self.swapped.observation_digest():
            raise ValueError("paired legs cannot reuse one observation")
        first_execution = {item.digest() for item in self.first.execution_receipts}
        swapped_execution = {item.digest() for item in self.swapped.execution_receipts}
        first_runs = {item.run_id for item in self.first.execution_receipts}
        swapped_runs = {item.run_id for item in self.swapped.execution_receipts}
        first_processes = {item.process_tree_id for item in self.first.execution_receipts}
        swapped_processes = {item.process_tree_id for item in self.swapped.execution_receipts}
        first_cgroups = {item.cgroup_path for item in self.first.execution_receipts}
        swapped_cgroups = {item.cgroup_path for item in self.swapped.execution_receipts}
        first_resources = {item.digest() for item in self.first.resource_receipts}
        swapped_resources = {item.digest() for item in self.swapped.resource_receipts}
        if (
            first_execution & swapped_execution
            or first_runs & swapped_runs
            or first_processes & swapped_processes
            or first_cgroups & swapped_cgroups
            or first_resources & swapped_resources
        ):
            raise ValueError("execution/resource receipts cannot be reused across paired legs")

    @property
    def block_id(self) -> Digest:
        return self.first.leg_plan.block_id

    def candidate_score(self, identity_digest: Digest) -> float:
        return (
            self.first.candidate_score(identity_digest) + self.swapped.candidate_score(identity_digest)
        ) / 2.0

    def raw_match_scores(self, identity_digest: Digest) -> tuple[float, float]:
        return self.first.candidate_score(identity_digest), self.swapped.candidate_score(identity_digest)


@dataclass(frozen=True, slots=True)
class AggregateResult:
    candidate: str
    candidate_identity_digest: Digest
    result_authority: str
    formal_plan_digest: Digest
    stratum_digest: Digest
    ordered_block_ids_digest: Digest
    observation_evidence_digest: Digest
    retry_ledger_digest: Digest
    analysis_parameters_digest: Digest
    wins: int
    draws: int
    losses: int
    match_score: float
    paired_blocks: int
    bootstrap_ci95_low: float
    bootstrap_ci95_high: float
    ci95_low: float
    ci95_high: float

    def digest(self) -> Digest:
        payload = asdict(self)
        payload.pop("candidate")  # Presentation labels are not evidence identity.
        return _payload_digest(payload)


@dataclass(frozen=True, slots=True)
class ClusterInterval:
    estimate: float
    bootstrap_low: float
    bootstrap_high: float
    guarded_low: float
    guarded_high: float
    clusters: int


def _percentile(sorted_values: Sequence[float], probability: float) -> float:
    if not sorted_values:
        raise ValueError("percentile requires data")
    position = probability * (len(sorted_values) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[lower]
    weight = position - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight


def cluster_bootstrap_ci(
    block_values: Sequence[float],
    *,
    seed: int,
    samples: int = 10_000,
) -> tuple[float, float]:
    if not block_values:
        raise ValueError("cluster bootstrap requires paired blocks")
    if samples < 1_000:
        raise ValueError("formal bootstrap requires at least 1000 resamples")
    rng = random.Random(seed)
    count = len(block_values)
    means = []
    for _ in range(samples):
        means.append(sum(block_values[rng.randrange(count)] for _ in range(count)) / count)
    means.sort()
    return _percentile(means, 0.025), _percentile(means, 0.975)


def guarded_cluster_interval(
    block_values: Sequence[float],
    *,
    lower_bound: float,
    upper_bound: float,
    seed: int,
    samples: int = 10_000,
    alpha: float = 0.05,
) -> ClusterInterval:
    """Percentile cluster bootstrap plus a finite-sample boundary guard.

    Resampling identical all-win clusters yields the misleading interval
    ``[1, 1]``.  We still report the requested cluster bootstrap, but claims use
    the union with a distribution-free Hoeffding interval over independent
    paired blocks.  This never invents certainty at a finite boundary sample.
    """

    if not block_values:
        raise ValueError("cluster interval requires data")
    if not lower_bound < upper_bound:
        raise ValueError("invalid bounded-cluster range")
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must lie in (0, 1)")
    if any(not math.isfinite(value) or not lower_bound <= value <= upper_bound for value in block_values):
        raise ValueError("cluster value outside declared finite bounds")
    estimate = sum(block_values) / len(block_values)
    bootstrap_low, bootstrap_high = cluster_bootstrap_ci(
        block_values,
        seed=seed,
        samples=samples,
    )
    radius = (upper_bound - lower_bound) * math.sqrt(
        math.log(2.0 / alpha) / (2.0 * len(block_values))
    )
    hoeffding_low = max(lower_bound, estimate - radius)
    hoeffding_high = min(upper_bound, estimate + radius)
    return ClusterInterval(
        estimate=estimate,
        bootstrap_low=bootstrap_low,
        bootstrap_high=bootstrap_high,
        guarded_low=min(bootstrap_low, hoeffding_low),
        guarded_high=max(bootstrap_high, hoeffding_high),
        clusters=len(block_values),
    )


def _candidate_identity(plan: FormalEvaluationPlan, candidate: str) -> tuple[str, Digest]:
    matches = [
        artifact
        for artifact in plan.artifacts
        if candidate in {artifact.display_label, artifact.identity_digest()}
    ]
    if len(matches) != 1:
        raise KeyError(f"candidate {candidate!r} does not uniquely resolve in the formal plan")
    return matches[0].display_label, matches[0].identity_digest()


def aggregate_blocks(
    blocks: Iterable[PairedBlock],
    candidate: str,
    *,
    plan: FormalEvaluationPlan,
    retry_ledger: RetryLedger = RetryLedger(),
) -> AggregateResult:
    """Resolve and aggregate the exact preregistered block set, fail closed."""

    materialized = sorted(tuple(blocks), key=lambda block: block.block_id)
    expected_by_id = {block.block_id: block for block in plan.blocks}
    actual_ids = [block.block_id for block in materialized]
    if len(set(actual_ids)) != len(actual_ids):
        raise ValueError("formal aggregate contains a duplicate paired block")
    if set(actual_ids) != set(expected_by_id):
        missing = sorted(set(expected_by_id) - set(actual_ids))
        extra = sorted(set(actual_ids) - set(expected_by_id))
        raise ValueError(f"formal aggregate differs from preregistration: missing={missing}, extra={extra}")

    display_label, candidate_digest = _candidate_identity(plan, candidate)
    used_retry_entries: set[Digest] = set()
    monitor_evidence_digests: set[Digest] = set()
    observation_digests: list[Digest] = []
    evidence_observations: dict[Digest, MatchObservation] = {}
    execution_digests: set[Digest] = set()
    run_ids: set[Digest] = set()
    process_tree_ids: set[str] = set()
    cgroup_paths: set[str] = set()
    cgroup_inodes: set[int] = set()
    resource_digests: set[Digest] = set()
    raw_execution_digests: set[Digest] = set()
    raw_resource_digests: set[Digest] = set()
    # Evidence identity is domain-qualified.  A canonical JSON capture can
    # legitimately have raw_replay_digest == match_trace_digest within one
    # observation; hash inequality is not a provenance proof.  Reuse is
    # forbidden within each typed domain across observations.
    replay_domain_digests: dict[str, set[Digest]] = {
        "raw_wire": set(),
        "raw_replay": set(),
        "match_trace": set(),
        "verification": set(),
        "attestation": set(),
        "hand70": set(),
    }
    supervisor_domain_digests: dict[str, set[Digest]] = {
        "launch_authorization": set(),
        "leg_receipt": set(),
        "receipt_consumption": set(),
        "capture_session": set(),
        "decision_trace": set(),
        "cleanup_receipt": set(),
        "socket_identity": set(),
        "consumption_ledger_entry": set(),
    }
    supervisor_consumption_ledger_inodes: set[int] = set()
    supervisor_consumption_ledger_paths: set[str] = set()
    supervisor_attempt_identities: set[tuple[Digest, int]] = set()

    for paired in materialized:
        expected_block = expected_by_id[paired.block_id]
        if paired.first.leg_plan.block_plan_digest != expected_block.digest():
            raise ValueError("paired block differs from its preregistered block plan")
        for observation in (paired.first, paired.swapped):
            observation.verify_against_plan(plan)
            if observation.terminal_fault is not None and observation.terminal_fault.owner == "infrastructure":
                raise ValueError("formal aggregate contains unresolved infrastructure failure")
            lineage = retry_ledger.lineage_for(observation)
            if len(lineage) > plan.max_infrastructure_retries_per_leg:
                raise ValueError("infrastructure retry lineage exceeds the preregistered cap")
            for entry in lineage:
                entry.original.verify_against_plan(plan)
                entry.retry.verify_against_plan(plan)
                entry.infrastructure_attribution.verify(entry.original, plan)
                used_retry_entries.add(entry.digest())
                monitor_raw = entry.infrastructure_attribution.raw_monitor_evidence_digest
                monitor_attestation = entry.infrastructure_attribution.attestation_digest
                if monitor_raw in monitor_evidence_digests or monitor_attestation in monitor_evidence_digests:
                    raise ValueError("formal retry ledger reused infrastructure monitor evidence")
                monitor_evidence_digests.update((monitor_raw, monitor_attestation))
                evidence_observations[entry.original.observation_digest()] = entry.original
                evidence_observations[entry.retry.observation_digest()] = entry.retry
            observation_digests.append(observation.observation_digest())
            evidence_observations[observation.observation_digest()] = observation

    all_retry_entries = {entry.digest() for entry in retry_ledger.entries}
    if used_retry_entries != all_retry_entries:
        raise ValueError("retry ledger contains an unused branch or hidden alternative result")
    for observation_digest in sorted(evidence_observations):
        observation = evidence_observations[observation_digest]
        for execution in observation.execution_receipts:
            if execution.digest() in execution_digests or execution.run_id in run_ids:
                raise ValueError("formal evidence reused an execution receipt/run")
            if execution.process_tree_id in process_tree_ids or execution.cgroup_path in cgroup_paths:
                raise ValueError("formal evidence reused a process tree or cgroup")
            if execution.raw_evidence_digest in raw_execution_digests:
                raise ValueError("formal evidence reused raw execution evidence")
            execution_digests.add(execution.digest())
            run_ids.add(execution.run_id)
            process_tree_ids.add(execution.process_tree_id)
            cgroup_paths.add(execution.cgroup_path)
            raw_execution_digests.add(execution.raw_evidence_digest)
        for resource in observation.resource_receipts:
            if resource.digest() in resource_digests or resource.cgroup_inode in cgroup_inodes:
                raise ValueError("formal evidence reused a resource receipt/cgroup inode")
            if resource.raw_evidence_digest in raw_resource_digests:
                raise ValueError("formal evidence reused raw resource evidence")
            resource_digests.add(resource.digest())
            cgroup_inodes.add(resource.cgroup_inode)
            raw_resource_digests.add(resource.raw_evidence_digest)
        replay_evidence_by_domain: dict[str, Digest | None] = {
            "raw_wire": observation.replay_receipt.raw_wire_digest,
            "raw_replay": observation.replay_receipt.raw_replay_digest,
            "match_trace": observation.replay_receipt.match_trace_digest,
            "verification": observation.replay_receipt.verification_evidence_digest,
            "attestation": observation.replay_receipt.attestation_digest,
            "hand70": observation.replay_receipt.hand70_evidence_digest,
        }
        for domain, digest in replay_evidence_by_domain.items():
            if digest is None:
                continue
            if digest in replay_domain_digests[domain]:
                raise ValueError(
                    f"formal evidence reused {domain.replace('_', ' ')} evidence"
                )
            replay_domain_digests[domain].add(digest)
        supervisor_evidence_by_domain: dict[str, Digest | None] = {
            "launch_authorization": observation.supervisor_launch_authorization_digest,
            "leg_receipt": observation.supervisor_leg_receipt_digest,
            "receipt_consumption": observation.supervisor_receipt_consumption_key,
            "capture_session": observation.supervisor_capture_session_digest,
            "decision_trace": observation.supervisor_decision_trace_digest,
            "cleanup_receipt": observation.supervisor_cleanup_receipt_digest,
            "consumption_ledger_entry": (
                observation.supervisor_consumption_ledger_entry_digest
            ),
        }
        for domain, digest in supervisor_evidence_by_domain.items():
            if digest is None:
                if plan.result_authority == "formal_strength":
                    raise ValueError(
                        f"formal evidence lacks supervisor {domain.replace('_', ' ')}"
                    )
                continue
            if digest in supervisor_domain_digests[domain]:
                raise ValueError(
                    f"formal evidence reused supervisor {domain.replace('_', ' ')}"
                )
            supervisor_domain_digests[domain].add(digest)
        socket_digests = observation.supervisor_socket_identity_digests
        if socket_digests is None:
            if plan.result_authority == "formal_strength":
                raise ValueError("formal evidence lacks supervisor socket identities")
        else:
            for digest in socket_digests:
                if digest in supervisor_domain_digests["socket_identity"]:
                    raise ValueError(
                        "formal evidence reused supervisor socket identity"
                    )
                supervisor_domain_digests["socket_identity"].add(digest)
        attempt_scope = observation.supervisor_attempt_journal_scope_digest
        attempt_sequence = observation.supervisor_attempt_sequence
        ledger_inode = observation.supervisor_consumption_ledger_entry_inode
        ledger_path = observation.supervisor_consumption_ledger_entry_path
        if plan.result_authority == "formal_strength" and (
            attempt_scope is None
            or attempt_sequence is None
            or ledger_inode is None
            or ledger_path is None
        ):
            raise ValueError(
                "formal evidence lacks durable supervisor attempt/consumption identity"
            )
        if attempt_scope is not None and attempt_sequence is not None:
            attempt_identity = (attempt_scope, attempt_sequence)
            if attempt_identity in supervisor_attempt_identities:
                raise ValueError("formal evidence reused a supervisor attempt sequence")
            supervisor_attempt_identities.add(attempt_identity)
        if ledger_inode is not None:
            if ledger_inode in supervisor_consumption_ledger_inodes:
                raise ValueError("formal evidence reused a supervisor ledger inode")
            supervisor_consumption_ledger_inodes.add(ledger_inode)
        if ledger_path is not None:
            if ledger_path in supervisor_consumption_ledger_paths:
                raise ValueError("formal evidence reused a supervisor ledger path")
            supervisor_consumption_ledger_paths.add(ledger_path)

    observation_intervals = sorted(
        (
            min(item.started_epoch_ms for item in observation.resource_receipts),
            max(item.finished_epoch_ms for item in observation.resource_receipts),
            observation_digest,
        )
        for observation_digest, observation in evidence_observations.items()
    )
    for previous, current in zip(observation_intervals, observation_intervals[1:], strict=False):
        if current[0] < previous[1]:
            raise ValueError("formal observations overlap despite concurrent_matches=1")

    raw = [score for block in materialized for score in block.raw_match_scores(candidate_digest)]
    wins = sum(score == 1.0 for score in raw)
    draws = sum(score == 0.5 for score in raw)
    losses = sum(score == 0.0 for score in raw)
    block_values = [block.candidate_score(candidate_digest) for block in materialized]
    interval = guarded_cluster_interval(
        block_values,
        lower_bound=0.0,
        upper_bound=1.0,
        seed=plan.bootstrap_seed,
        samples=plan.bootstrap_samples,
    )
    analysis_parameters = {
        "analysis_code_digest": plan.analysis_code_digest,
        "bootstrap_seed": plan.bootstrap_seed,
        "bootstrap_samples": plan.bootstrap_samples,
        "cluster": "paired_block",
        "boundary_guard": "hoeffding_union",
    }
    return AggregateResult(
        candidate=display_label,
        candidate_identity_digest=candidate_digest,
        result_authority=plan.result_authority,
        formal_plan_digest=plan.digest(),
        stratum_digest=plan.stratum.digest(),
        ordered_block_ids_digest=_payload_digest(actual_ids),
        observation_evidence_digest=_payload_digest(observation_digests),
        retry_ledger_digest=retry_ledger.digest(),
        analysis_parameters_digest=_payload_digest(analysis_parameters),
        wins=wins,
        draws=draws,
        losses=losses,
        match_score=(wins + 0.5 * draws) / len(raw),
        paired_blocks=len(materialized),
        bootstrap_ci95_low=interval.bootstrap_low,
        bootstrap_ci95_high=interval.bootstrap_high,
        ci95_low=interval.guarded_low,
        ci95_high=interval.guarded_high,
    )


def paired_difference_ci(
    first: Mapping[str, float],
    second: Mapping[str, float],
    *,
    bootstrap_seed: int,
    bootstrap_samples: int = 10_000,
) -> tuple[float, float, float]:
    if set(first) != set(second) or not first:
        raise ValueError("paired differences require identical non-empty block IDs")
    differences = [first[key] - second[key] for key in sorted(first)]
    estimate = sum(differences) / len(differences)
    interval = guarded_cluster_interval(
        differences,
        lower_bound=-1.0,
        upper_bound=1.0,
        seed=bootstrap_seed,
        samples=bootstrap_samples,
    )
    return estimate, interval.guarded_low, interval.guarded_high


def paired_sign_flip_p_value(
    differences: Sequence[float],
    *,
    null_margin: float = 0.0,
    alternative: str = "two-sided",
    seed: int = 0,
    samples: int = 100_000,
) -> float:
    """Paired block randomization p-value for a frozen estimand and margin."""

    if not differences or any(not math.isfinite(value) for value in differences):
        raise ValueError("finite paired differences are required")
    if alternative not in {"two-sided", "greater", "less"}:
        raise ValueError("unsupported alternative")
    centered = [value - null_margin for value in differences]
    observed = sum(centered) / len(centered)

    def extreme(value: float) -> bool:
        if alternative == "greater":
            return value >= observed - 1e-15
        if alternative == "less":
            return value <= observed + 1e-15
        return abs(value) >= abs(observed) - 1e-15

    total_patterns = 1 << len(centered) if len(centered) <= 20 else None
    exceed = 0
    total = 0
    if total_patterns is not None:
        for mask in range(total_patterns):
            value = sum(
                item if mask & (1 << index) else -item
                for index, item in enumerate(centered)
            ) / len(centered)
            exceed += int(extreme(value))
            total += 1
        return exceed / total

    if samples < 10_000:
        raise ValueError("Monte Carlo randomization requires at least 10000 samples")
    rng = random.Random(seed)
    for _ in range(samples):
        value = sum(item if rng.getrandbits(1) else -item for item in centered) / len(centered)
        exceed += int(extreme(value))
        total += 1
    return (exceed + 1) / (total + 1)


def required_paired_blocks_for_power(
    *,
    target_difference: float,
    block_variance: float,
    alpha: float = 0.05,
    power: float = 0.80,
    two_sided: bool = True,
) -> int:
    """Normal-approximation planning only; inputs must come from a blind pilot."""

    if target_difference <= 0 or block_variance <= 0:
        raise ValueError("effect and block variance must be positive")
    if not 0 < alpha < 1 or not 0 < power < 1:
        raise ValueError("alpha and power must lie in (0, 1)")
    normal = NormalDist()
    critical = normal.inv_cdf(1.0 - alpha / (2.0 if two_sided else 1.0))
    desired = normal.inv_cdf(power)
    return math.ceil(((critical + desired) ** 2) * block_variance / (target_difference**2))


def holm_adjust(p_values: Mapping[str, float]) -> dict[str, float]:
    if any(not 0.0 <= value <= 1.0 for value in p_values.values()):
        raise ValueError("p-values must be in [0, 1]")
    ordered = sorted(p_values.items(), key=lambda item: item[1])
    adjusted: dict[str, float] = {}
    running = 0.0
    total = len(ordered)
    for index, (name, value) in enumerate(ordered):
        running = max(running, min(1.0, (total - index) * value))
        adjusted[name] = running
    return adjusted
